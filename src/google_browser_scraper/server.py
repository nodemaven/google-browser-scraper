"""`serve`: a small HTTP API in the usual SERP API format over one scraper.

    GET /search.json?q=best+running+shoes[&pages=2][&links=5][&links_wait=5][&async=true]
    GET /searches/<id>.json
    GET /health

One page returns the record itself, in the usual SERP API field names, so a client
written for a paid SERP API can point its base URL here. More pages return a
JSON array of records. Pages are read one query at a time: a single browser
identity is held between them, which is the point - a served exit answers the
next query too. Result links resolve after the page is read, on threads of
their own, so the browser goes on to the next request while a caller waits for
its links.

`links=N` asks the links of the first N organic results only (0: none).
`links_wait=S` answers after S seconds with the links that are in by then; the
rest are null and `search_metadata.links_pending` counts them. `async=true`
answers at once with `search_metadata.id`; `/searches/<id>.json` answers with
status `Processing` until the search and its links are done, then with the
record. Every search, async or not, is kept there for up to `ARCHIVE_TTL_S`,
the newest `ARCHIVE_SIZE` of them. At most `ASYNC_QUEUED_MAX` async searches
wait for the browser at once; a request past that is answered 429.

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
import time
import uuid
from collections import OrderedDict
from collections.abc import Callable
from concurrent.futures import Future
from concurrent.futures import TimeoutError as FutureTimeout
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import parse_qs, urlsplit

from .scraper import ExitsRefused, PendingLinks, Scraper

REQUEST_TIMEOUT_S = 900
#: How long `/searches/<id>.json` keeps a search, and how many it keeps.
ARCHIVE_TTL_S = 3600
ARCHIVE_SIZE = 500
#: `async=true` searches waiting for the browser at once; more are answered 429.
ASYNC_QUEUED_MAX = 50
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
    return _query_job("search", query, pages, hl, gl)


def start_job(
    query: str,
    pages: int,
    hl: str | None = None,
    gl: str | None = None,
    links: int | None = None,
) -> Callable[[Scraper], PendingLinks]:
    """As `search_job`, but the job ends once the pages are read: their links
    go on resolving off the worker thread, and the caller waits for them there."""
    return _query_job("start_search", query, pages, hl, gl, links)


def _query_job(method: str, query, pages, hl, gl, links=None) -> Callable[[Scraper], Any]:
    def job(scraper: Scraper) -> Any:
        saved = scraper.settings.pages
        scraper.settings.pages = pages
        try:
            options: dict[str, Any] = {k: v for k, v in (("hl", hl), ("gl", gl)) if v}
            if links is not None:
                options["links"] = links
            return getattr(scraper, method)(query, settle=False, **options)
        except ExitsRefused:
            # Report it to this caller, but let the next request try again.
            scraper.reset_breaker()
            raise
        finally:
            scraper.settings.pages = saved

    return job


class Search:
    """One request's search as `/searches/<id>.json` tells it: still running,
    failed, or its records (status `Processing` while links are pending)."""

    def __init__(self, ident: str, parameters: dict[str, Any], created: float) -> None:
        self.id = ident
        self.parameters = parameters
        self.created = created
        self.pending: PendingLinks | None = None
        self.error: str | None = None

    @property
    def endpoint(self) -> str:
        return f"/searches/{self.id}.json"

    def land(self, pending: PendingLinks) -> None:
        with pending.lock:
            for record in pending.records:
                record["search_metadata"]["id"] = self.id
                record["search_metadata"]["json_endpoint"] = self.endpoint
        self.pending = pending

    def fail(self, error: str) -> None:
        self.error = error

    def body(self) -> Any:
        metadata = {"id": self.id, "json_endpoint": self.endpoint}
        if self.error is not None:
            return {
                "search_metadata": {**metadata, "status": "Error"},
                "search_parameters": self.parameters,
                "error": self.error,
            }
        pending = self.pending
        if pending is None:
            return {
                "search_metadata": {**metadata, "status": "Processing"},
                "search_parameters": self.parameters,
            }
        done = pending.done()
        records = pending.snapshot()
        if not done and records is not pending.records:  # a copy: safe to mark
            for record in records:
                record["search_metadata"]["status"] = "Processing"
        # The shape the search's own answer had: one page is the record itself.
        one = self.parameters.get("pages", 1) == 1 and len(records) == 1
        return records[0] if one else records


class Archive:
    """The searches of the last `ttl` seconds, the newest `size` of them."""

    def __init__(
        self,
        ttl: float = ARCHIVE_TTL_S,
        size: int = ARCHIVE_SIZE,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._ttl, self._size, self._clock = ttl, size, clock
        self._items: OrderedDict[str, Search] = OrderedDict()
        self._lock = threading.Lock()

    def new(self, parameters: dict[str, Any]) -> Search:
        search = Search(uuid.uuid4().hex, parameters, self._clock())
        with self._lock:
            self._items[search.id] = search
            limit = self._clock() - self._ttl
            while self._items and (
                len(self._items) > self._size or next(iter(self._items.values())).created < limit
            ):
                self._items.popitem(last=False)
        return search

    def get(self, ident: str) -> Search | None:
        with self._lock:
            search = self._items.get(ident)
        if search is None or search.created < self._clock() - self._ttl:
            return None
        return search


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


_ARCHIVED = re.compile(r"/searches/([0-9a-f]{32})\.json")


def _landed(search: Search, done: Future, worker: Worker) -> None:
    """An async search's job ended, on the worker thread: keep what it gave,
    and queue the upkeep it left."""
    if done.cancelled():
        search.fail("cancelled")
        return
    exc = done.exception()
    if exc is not None:
        search.fail(str(exc) if isinstance(exc, ExitsRefused) else f"{type(exc).__name__}: {exc}")
        return
    search.land(done.result())
    worker.after(settle_job)


def make_handler(
    worker: Worker, *, api_key: str | None, max_pages: int, archive: Archive | None = None
):
    archive = archive or Archive()
    #: Async searches submitted and not yet run.
    queued = [0]
    queued_lock = threading.Lock()

    def ran(search: Search):
        def done(future: Future) -> None:
            with queued_lock:
                queued[0] -= 1
            _landed(search, future, worker)

        return done

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
            kept = _ARCHIVED.fullmatch(parts.path)
            if kept:
                search = archive.get(kept.group(1))
                if search is None:
                    return self._send(
                        404, {"error": f"no search with that id in the last {ARCHIVE_TTL_S} s"}
                    )
                return self._send(200, search.body())
            if parts.path not in ("/search", "/search.json"):
                return self._send(
                    404, {"error": "use /search.json?q=..., /searches/<id>.json or /health"}
                )
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
            links = links_wait = None
            try:
                if params.get("links", "") != "":
                    links = int(params["links"])
                if params.get("links_wait", "") != "":
                    links_wait = float(params["links_wait"])
            except ValueError:
                return self._send(400, {"error": "links must be an integer, links_wait a number"})
            if links is not None and links < 0:
                return self._send(400, {"error": "links must be 0 or more"})
            if links_wait is not None and not 0 <= links_wait <= REQUEST_TIMEOUT_S:
                return self._send(
                    400, {"error": f"links_wait must be between 0 and {REQUEST_TIMEOUT_S}"}
                )
            asynchronous = params.get("async", "").lower() in ("1", "true")
            if asynchronous:
                with queued_lock:
                    if queued[0] >= ASYNC_QUEUED_MAX:
                        return self._send(
                            429, {"error": f"{queued[0]} async searches are already waiting"}
                        )
                    queued[0] += 1
            parameters = {"q": query, "pages": pages, "hl": hl, "gl": gl, "links": links}
            search = archive.new({k: v for k, v in parameters.items() if v is not None})
            job = start_job(query, pages, hl, gl, links)
            if asynchronous:
                worker.submit(job).add_done_callback(ran(search))
                return self._send(200, search.body())
            future = worker.submit(job)
            try:
                pending = future.result(timeout=REQUEST_TIMEOUT_S)
            except FutureTimeout:
                if future.cancel():  # still queued: it never runs
                    search.fail("cancelled: no answer in time")
                else:  # running: its answer still reaches /searches/<id>.json
                    future.add_done_callback(lambda done: _landed(search, done, worker))
                message = f"TimeoutError: no answer within {REQUEST_TIMEOUT_S} s"
                return self._send(500, {"error": message})
            except ExitsRefused as exc:
                search.fail(str(exc))
                return self._send(503, {"error": str(exc)})
            except Exception as exc:
                search.fail(f"{type(exc).__name__}: {exc}")
                return self._send(500, {"error": f"{type(exc).__name__}: {exc}"})
            # Queued before the links are waited for and the answer is
            # written: the worker does the upkeep, or the next query, while
            # this thread waits and sends.
            worker.after(settle_job)
            search.land(pending)
            pending.wait(REQUEST_TIMEOUT_S if links_wait is None else links_wait)
            records = pending.snapshot()
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
