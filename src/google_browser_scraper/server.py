"""`serve`: a small HTTP API in the usual SERP API format over one scraper.

    GET /search.json?q=best+running+shoes[&pages=2][&api_key=...]
    GET /health

One page returns the record itself, in the usual SERP API field names, so a client
written for a paid SERP API can point its base URL here. More pages return a
JSON array of records. Requests are served one at a time: a single browser
identity is held between them, which is the point - a served exit answers the
next query too.

`/health` answers 503 `warming` while `--prewarm` is still looking for an exit
Google serves, and 503 `busy` when a long search holds the scraper.

Set GBS_API_KEY to require `api_key=` or `Authorization: Bearer`; without it the
server refuses to listen anywhere but loopback.
"""

from __future__ import annotations

import hmac
import json
import os
import queue
import re
import sys
import threading
from collections.abc import Callable
from concurrent.futures import Future
from concurrent.futures import TimeoutError as FutureTimeout
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import parse_qs, urlsplit

from .scraper import ExitsRefused, Scraper

REQUEST_TIMEOUT_S = 900
STARTUP_TIMEOUT_S = 60
#: How long `stop()` waits for the job in hand. Upkeep after a query - a browser
#: restart or a template copy, each with a 40-60 s settle - can take this long.
STOP_TIMEOUT_S = 120


class Worker:
    """Owns the scraper on one thread.

    Playwright's sync API is bound to the thread that started it, so every
    call into the scraper - from HTTP handler threads or the MCP loop - is a
    job on this thread's queue.

    `start()` returns only once the scraper exists, and raises whatever stopped
    it from being built - a missing proxy or missing credentials - so `serve`
    and `mcp` exit with that message instead of accepting requests nobody will
    answer. If the thread ends later for any reason, queued and new jobs fail
    at once rather than waiting out their timeout.
    """

    def __init__(
        self,
        make_scraper: Callable[[], Scraper],
        *,
        prewarm: bool = False,
        log: Callable[[str], None] | None = None,
    ) -> None:
        self._make = make_scraper
        self._prewarm = prewarm
        self._log = log or (lambda message: print(message, file=sys.stderr))
        self._jobs: queue.Queue = queue.Queue()
        self._thread = threading.Thread(target=self._loop, name="gbs-worker", daemon=True)
        self._started = threading.Event()
        #: Set once the prewarm is over, served or not (at once without one).
        self.ready = threading.Event()
        self._stopping = threading.Event()
        self._lock = threading.Lock()
        self._build_error: BaseException | None = None
        self._stopped: BaseException | None = None
        self.scraper: Scraper | None = None

    def start(self, timeout: float = STARTUP_TIMEOUT_S) -> Worker:
        self._thread.start()
        if not self._started.wait(timeout):
            raise RuntimeError(f"the scraper did not start within {timeout:g} s")
        if self._build_error is not None:
            raise self._build_error
        return self

    def submit(self, job: Callable[[Scraper], Any]) -> Future:
        future: Future = Future()
        with self._lock:
            if self._stopped is not None:
                future.set_exception(self._unavailable())
            else:
                self._jobs.put((job, future))
        return future

    def call(self, job: Callable[[Scraper], Any], timeout: float = REQUEST_TIMEOUT_S) -> Any:
        return self.submit(job).result(timeout=timeout)

    def after(self, job: Callable[[Scraper], Any]) -> None:
        """Queue a job nobody waits for; a failure is logged, not lost.

        Skipped once `stop()` has been called: it is upkeep for a next query
        that will not come, and running it would delay closing the browser.
        """

        def guarded(scraper: Scraper) -> Any:
            return None if self._stopping.is_set() else job(scraper)

        def report(future: Future) -> None:
            if not future.cancelled() and future.exception() is not None:
                exc = future.exception()
                self._log(f"background job failed: {type(exc).__name__}: {exc}")

        self.submit(guarded).add_done_callback(report)

    def stop(self, timeout: float = STOP_TIMEOUT_S) -> None:
        self._stopping.set()
        if self._thread.is_alive():
            self._jobs.put(None)
            self._thread.join(timeout=timeout)

    def _loop(self) -> None:
        try:
            self.scraper = self._make()
        except BaseException as exc:  # raised again by start()
            self._build_error = exc
            self._shut_down(exc)
            self._started.set()
            return
        self._started.set()
        cause: BaseException = RuntimeError("stopped")
        try:
            if self._prewarm:
                try:
                    self.scraper.prewarm()
                    self._log("prewarm: an exit is served")
                except Exception as exc:
                    self._log(f"prewarm failed: {type(exc).__name__}: {exc}")
                    self.scraper.reset_breaker()
            self.ready.set()
            while True:
                item = self._jobs.get()
                if item is None:
                    return
                job, future = item
                if not future.set_running_or_notify_cancel():
                    continue
                try:
                    future.set_result(job(self.scraper))
                except BaseException as exc:  # handed to the waiting caller
                    future.set_exception(exc)
        except BaseException as exc:
            cause = exc
            self._log(f"worker stopped: {type(exc).__name__}: {exc}")
        finally:
            try:
                self.scraper.close()
            except Exception as exc:
                self._log(f"closing the scraper failed: {type(exc).__name__}: {exc}")
            self._shut_down(cause)

    def _shut_down(self, cause: BaseException) -> None:
        """Refuse new jobs and fail the ones still queued."""
        self.ready.set()  # nothing is warming any more; /health reports the failure
        with self._lock:
            self._stopped = cause
            while True:
                try:
                    item = self._jobs.get_nowait()
                except queue.Empty:
                    break
                if item is not None and item[1].set_running_or_notify_cancel():
                    item[1].set_exception(self._unavailable())

    def _unavailable(self) -> RuntimeError:
        cause = self._stopped
        return RuntimeError(f"the scraper is not running ({type(cause).__name__}: {cause})")


def search_job(
    query: str, pages: int, hl: str | None = None, gl: str | None = None
) -> Callable[[Scraper], list[dict[str, Any]]]:
    """A job that runs one query for `pages` pages and leaves the identity open.
    `hl` and `gl` override the scraper's for this query.

    The upkeep the query leaves (a template copy, a restart) is not done here:
    queue `settle_job` once the answer is out, so the caller does not wait for it.
    """

    def job(scraper: Scraper) -> list[dict[str, Any]]:
        saved = scraper.settings.pages
        scraper.settings.pages = pages
        try:
            locale = {k: v for k, v in (("hl", hl), ("gl", gl)) if v}
            return scraper.search(query, settle=False, **locale)
        except ExitsRefused:
            # Report it to this caller, but let the next request try again.
            scraper.reset_breaker()
            raise
        finally:
            scraper.settings.pages = saved

    return job


_HL = re.compile(r"[A-Za-z]{2,3}(-[A-Za-z0-9]{2,4})?")
_GL = re.compile(r"[A-Za-z]{2}")


def locale_problem(hl: str | None, gl: str | None) -> str | None:
    """Why `hl` or `gl` cannot go into Google's URL, if either cannot."""
    if hl and not _HL.fullmatch(hl):
        return "hl must be a language code such as en or pt-BR"
    if gl and not _GL.fullmatch(gl):
        return "gl must be a two-letter country code such as us"
    return None


def settle_job(scraper: Scraper) -> None:
    scraper.settle()


def make_handler(worker: Worker, *, api_key: str | None, max_pages: int):
    class Handler(BaseHTTPRequestHandler):
        server_version = "google-browser-scraper"

        def do_GET(self):  # noqa: N802 - the stdlib's name
            parts = urlsplit(self.path)
            params = {k: v[0] for k, v in parse_qs(parts.query).items()}
            if api_key and not self._authorized(params):
                return self._send(401, {"error": "missing or wrong api_key"})
            if parts.path == "/health":
                if not worker.ready.is_set():
                    return self._send(503, {"status": "warming"})
                try:
                    summary = worker.call(lambda s: s.stats.summary(), timeout=30)
                except FutureTimeout:
                    return self._send(503, {"status": "busy"})
                except Exception as exc:
                    return self._send(503, {"status": "down", "error": str(exc)})
                return self._send(200, {"status": "ok", "stats": summary})
            if parts.path not in ("/search", "/search.json"):
                return self._send(404, {"error": "use /search.json?q=... or /health"})
            query = (params.get("q") or "").strip()
            if not query:
                return self._send(400, {"error": "q is required"})
            try:
                pages = int(params.get("pages", "1"))
            except ValueError:
                return self._send(400, {"error": "pages must be an integer"})
            if not 1 <= pages <= max_pages:
                return self._send(400, {"error": f"pages must be between 1 and {max_pages}"})
            hl, gl = params.get("hl") or None, params.get("gl") or None
            problem = locale_problem(hl, gl)
            if problem:
                return self._send(400, {"error": problem})
            try:
                records = worker.call(search_job(query, pages, hl, gl))
            except ExitsRefused as exc:
                return self._send(503, {"error": str(exc)})
            except Exception as exc:
                return self._send(500, {"error": f"{type(exc).__name__}: {exc}"})
            # Queued before the answer is written: the worker does the upkeep
            # while this thread sends, and a client that hangs up mid-answer
            # does not leave it for the next request to pay for.
            worker.after(settle_job)
            self._send(200, records[0] if pages == 1 and len(records) == 1 else records)

        def _authorized(self, params: dict[str, str]) -> bool:
            given = params.get("api_key") or ""
            header = self.headers.get("Authorization", "")
            if header.startswith("Bearer "):
                given = header[7:]
            return hmac.compare_digest(given.encode(), api_key.encode())

        def _send(self, status: int, body: Any) -> None:
            data = json.dumps(body, ensure_ascii=False).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def log_message(self, fmt, *args):
            # The path carries the api_key; log the method and status only.
            print(
                f"{self.command} {self.path.split('?')[0]} {args[1] if len(args) > 1 else ''}",
                file=sys.stderr,
            )

    return Handler


def serve(worker: Worker, *, host: str, port: int, max_pages: int = 3) -> None:
    api_key = os.environ.get("GBS_API_KEY") or None
    if not api_key and host not in ("127.0.0.1", "localhost", "::1"):
        raise ValueError(
            "set GBS_API_KEY before listening on a non-loopback address: "
            "anyone who can reach the port spends your proxy traffic"
        )
    server = ThreadingHTTPServer(
        (host, port), make_handler(worker, api_key=api_key, max_pages=max_pages)
    )
    print(f"listening on http://{host}:{server.server_port}/search.json?q=...", file=sys.stderr)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        worker.stop()
