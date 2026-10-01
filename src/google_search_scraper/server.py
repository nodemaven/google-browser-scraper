"""`serve`: a small SerpApi-style HTTP API over one scraper.

    GET /search.json?q=best+running+shoes[&pages=2][&api_key=...]
    GET /health

One page returns the record itself, in SerpApi's field names, so a client
written for a paid SERP API can point its base URL here. More pages return a
JSON array of records. Requests are served one at a time: a single browser
identity is held between them, which is the point - a served exit answers the
next query too.

Set GSS_API_KEY to require `api_key=` or `Authorization: Bearer`; without it the
server refuses to listen anywhere but loopback.
"""

from __future__ import annotations

import hmac
import json
import os
import queue
import sys
import threading
from collections.abc import Callable
from concurrent.futures import Future
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import parse_qs, urlsplit

from .scraper import ExitsRefused, Scraper

REQUEST_TIMEOUT_S = 900


class Worker:
    """Owns the scraper on one thread.

    Playwright's sync API is bound to the thread that started it, so every
    call into the scraper - from HTTP handler threads or the MCP loop - is a
    job on this thread's queue.
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
        self._thread = threading.Thread(target=self._loop, name="gss-worker", daemon=True)
        self.scraper: Scraper | None = None

    def start(self) -> Worker:
        self._thread.start()
        return self

    def submit(self, job: Callable[[Scraper], Any]) -> Future:
        future: Future = Future()
        self._jobs.put((job, future))
        return future

    def call(self, job: Callable[[Scraper], Any], timeout: float = REQUEST_TIMEOUT_S) -> Any:
        return self.submit(job).result(timeout=timeout)

    def stop(self) -> None:
        self._jobs.put(None)
        self._thread.join(timeout=30)

    def _loop(self) -> None:
        self.scraper = self._make()
        try:
            if self._prewarm:
                try:
                    self.scraper.prewarm()
                    self._log("prewarm: identity ready")
                except Exception as exc:
                    self._log(f"prewarm failed: {type(exc).__name__}: {exc}")
                    self.scraper.reset_breaker()
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
        finally:
            self.scraper.close()


def search_job(query: str, pages: int) -> Callable[[Scraper], list[dict[str, Any]]]:
    """A job that runs one query for `pages` pages and leaves the identity open."""

    def job(scraper: Scraper) -> list[dict[str, Any]]:
        saved = scraper.settings.pages
        scraper.settings.pages = pages
        try:
            return scraper.search(query)
        except ExitsRefused:
            # Report it to this caller, but let the next request try again.
            scraper.reset_breaker()
            raise
        finally:
            scraper.settings.pages = saved

    return job


def make_handler(worker: Worker, *, api_key: str | None, max_pages: int):
    class Handler(BaseHTTPRequestHandler):
        server_version = "google-search-scraper"

        def do_GET(self):  # noqa: N802 - the stdlib's name
            parts = urlsplit(self.path)
            params = {k: v[0] for k, v in parse_qs(parts.query).items()}
            if api_key and not self._authorized(params):
                return self._send(401, {"error": "missing or wrong api_key"})
            if parts.path == "/health":
                summary = worker.call(lambda s: s.stats.summary(), timeout=30)
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
            try:
                records = worker.call(search_job(query, pages))
            except ExitsRefused as exc:
                return self._send(503, {"error": str(exc)})
            except Exception as exc:
                return self._send(500, {"error": f"{type(exc).__name__}: {exc}"})
            return self._send(200, records[0] if pages == 1 and len(records) == 1 else records)

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
    api_key = os.environ.get("GSS_API_KEY") or None
    if not api_key and host not in ("127.0.0.1", "localhost", "::1"):
        raise ValueError(
            "set GSS_API_KEY before listening on a non-loopback address: "
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
