"""Results pages over HTTPS requests between the browser's checks.

A browser that Google serves has passed a JavaScript check, and the cookies it
holds afterwards are what Google accepts. An HTTP client with Chromium's TLS
fingerprint, sending Chromium's own request headers and those cookies, is
served the same results page for a few dozen queries until Google asks for the
check again. Then the browser runs one query, passes the check, and the HTTP
client carries on with its fresh cookies. A results page over HTTP is a single
document, so it costs a fraction of the traffic of a page the browser renders
with every script and image.

The request headers are read once per run, from a NetLog of a served browser
search: their order and client hints are Chromium's own rather than a guess.
Until they are read, every query goes through the browser.

The hybrid steps back to the browser when the HTTP side stops working: after
`FALLBACK_SERIES` short series in a row on one exit (fewer than `FALLBACK_MIN`
pages before a check, a refusal, an error, or a served page with no results,
which is what a change in Google's markup looks like), that exit searches in the browser
only. When `FALLBACK_EXITS` exits in a row step back, the rest of the run does.
"""

from __future__ import annotations

import json
import os
import re
import tempfile
import threading
import time
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from typing import Any
from urllib.parse import urlsplit

from .browser import Fetched, results_url, search_page
from .classify import classify
from .parse import parse_serp

#: What curl_cffi impersonates when the browser's version cannot be read.
IMPERSONATE = "chrome152"
FALLBACK_MIN = 3
FALLBACK_SERIES = 2
FALLBACK_EXITS = 3
#: A series this long shows the HTTP side works, and clears the run's count of
#: exits that stepped back.
GOOD_SERIES = 20
#: Seconds for one results page over HTTP; one that hangs costs this before the
#: browser takes the query, and a served page takes one or two.
TIMEOUT = 20
GOOGLE = "https://www.google.com/search"


# -- request headers ------------------------------------------------------------


def _split(line: str) -> tuple[str, str]:
    if line.startswith(":"):
        name, _, value = line[1:].partition(": ")
        return ":" + name, value
    name, _, value = line.partition(": ")
    return name.lower(), value


def read_netlog(path: str, host: str = "www.google.com", prefix: str = "/search") -> list[dict]:
    """Every HTTP/2 request Chromium sent to `host` for a path starting with
    `prefix`, in the order sent: its headers as a list of (name, value) pairs
    and the headers of its response."""
    with open(path, encoding="utf-8", errors="replace") as fh:
        raw = fh.read()
    try:
        log = json.loads(raw)
    except json.JSONDecodeError:  # cut short: close the events array
        log = json.loads(raw.rstrip().rstrip(",") + "]}")
    names = {v: k for k, v in log["constants"]["logEventTypes"].items()}
    sent, received = [], {}
    for event in log.get("events", []):
        kind = names.get(event.get("type"))
        params = event.get("params") or {}
        key = ((event.get("source") or {}).get("id"), params.get("stream_id"))
        if kind == "HTTP2_SESSION_SEND_HEADERS":
            headers = [_split(h) for h in params.get("headers") or []]
            pseudo = dict(h for h in headers if h[0].startswith(":"))
            if pseudo.get(":authority") == host and pseudo.get(":path", "").startswith(prefix):
                sent.append({"key": key, "headers": headers})
        elif kind == "HTTP2_SESSION_RECV_HEADERS":
            received.setdefault(key, [_split(h) for h in params.get("headers") or []])
    for request in sent:
        request["response"] = received.get(request["key"], [])
    return sent


def replay_headers(request: dict, cookie: str) -> list[tuple[str, str]]:
    """Chromium's headers for `request`, in its order, with `cookie` in place
    of the one it sent."""
    out = []
    for name, value in request["headers"]:
        if name.startswith(":"):
            continue
        out.append((name, cookie if name == "cookie" else value))
    if cookie and not any(name == "cookie" for name, _ in out):
        out.append(("cookie", cookie))
    return out


def _expired(header: str) -> bool:
    low = header.lower()
    if "max-age=0" in low or re.search(r"max-age=-\d", low):
        return True
    found = re.search(r"expires=([^;]+)", header, re.IGNORECASE)
    if not found:
        return False
    try:
        when = parsedate_to_datetime(found.group(1).strip())
    except (TypeError, ValueError):
        return False
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    return when <= datetime.now(timezone.utc)


def fold_cookies(cookie: str, set_cookies: list[str], deleted: set[str] | None = None) -> str:
    """Apply Set-Cookie headers to a cookie string, keeping the order. Names a
    header expired are dropped, and added to `deleted` when given."""
    pairs = []
    for part in cookie.split(";"):
        if "=" in part:
            name, value = part.strip().split("=", 1)
            pairs.append([name, value])
    for header in set_cookies:
        first = header.split(";", 1)[0]
        if "=" not in first:
            continue
        name, value = first.split("=", 1)
        gone = _expired(header)
        for pair in pairs:
            if pair[0] == name:
                pair[1] = value
                break
        else:
            pairs.append([name, value])
        if gone:
            pairs = [p for p in pairs if p[0] != name]
            if deleted is not None:
                deleted.add(name)
        elif deleted is not None:
            deleted.discard(name)  # set again after it was deleted
    return "; ".join(f"{n}={v}" for n, v in pairs)


def set_cookie_headers(response: Any) -> list[str]:
    headers = response.headers
    for method in ("get_list", "getlist"):
        if hasattr(headers, method):
            return list(getattr(headers, method)("set-cookie"))
    if hasattr(headers, "multi_items"):
        return [v for k, v in headers.multi_items() if k.lower() == "set-cookie"]
    value = headers.get("set-cookie")
    return [value] if value else []


def impersonate_for(request: dict | None) -> str:
    """The Chrome version curl_cffi should present for the browser that sent
    `request`: the newest it can impersonate that is not newer than the browser.

    It can be behind the user agent: curl_cffi adds a Chrome version some time
    after Chrome ships it. A TLS fingerprint a version or two older than the
    headers is accepted here; a wider gap is what an update of curl_cffi is for.

    Read from the request's user agent, so that a new patchright or curl_cffi
    moves it without a code change.
    """
    agent = dict((request or {}).get("headers") or []).get("user-agent", "")
    found = re.search(r"Chrome/(\d+)\.", agent)
    try:
        from curl_cffi.requests import BrowserType
    except ImportError:
        return IMPERSONATE
    targets = sorted(
        int(m.group(1)) for b in BrowserType if (m := re.fullmatch(r"chrome(\d+)", b.value))
    )
    if not found or not targets:
        return IMPERSONATE
    major = int(found.group(1))
    usable = [t for t in targets if t <= major]
    return f"chrome{usable[-1] if usable else targets[0]}"


class Headers:
    """The run's request template and its state, shared by every exit (and
    every worker in a pool)."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.request: dict | None = None
        self._capturing = False
        #: Exits in a row that stepped back to the browser.
        self.streak = 0
        self.disabled = False

    def claim_capture(self) -> bool:
        """Whether this session should record a NetLog: only one at a time,
        and only until a template is read."""
        with self._lock:
            if self.request is not None or self._capturing:
                return False
            self._capturing = True
            return True

    def offer(self, requests: list[dict]) -> None:
        with self._lock:
            self._capturing = False
            if self.request is None and requests:
                self.request = requests[-1]

    def stepped_back(self) -> bool:
        """Count an exit that stepped back; True when the run should too."""
        with self._lock:
            self.streak += 1
            if self.streak >= FALLBACK_EXITS:
                self.disabled = True
            return self.disabled

    def worked(self) -> None:
        with self._lock:
            self.streak = 0


# -- the session ------------------------------------------------------------------


class HybridSession:
    """A browser session whose results pages come over HTTP while Google
    serves them there, and from the browser otherwise.

    Offers what the scraper calls on any session. `on_event` receives the
    hybrid's own events; it is called on the session's thread.
    """

    def __init__(self, inner: Any, proxy_url: str | None, headers: Headers, on_event=None) -> None:
        self.inner = inner
        self.name = getattr(inner, "name", "patchright")
        self._proxy_url = proxy_url
        self._headers = headers
        self._emit = on_event or (lambda event: None)
        self._netlog: str | None = None
        self._http = None
        self._cookie = ""
        #: Cookies a response over HTTP deleted, still in the browser's jar.
        self._deleted: set[str] = set()
        self._series = 0
        self._weak = 0
        self._off = False
        #: (query, page number, html) of the last page that came over HTTP,
        #: so "next page" can follow it there, and the hl and gl it was asked with.
        self._last: tuple[str, int, str] | None = None
        self._locale: dict[str, str | None] = {}
        self._last_source: str | None = None
        #: A served browser search happened while a NetLog was being recorded.
        self._recorded = False
        #: A recorded NetLog held no request; this session does not try again.
        self._capture_failed = False
        #: Whether the browser has been served on this exit since it opened.
        #: A new exit's first search goes through the browser: the HTTP side
        #: with a copied profile's cookies is refused on an exit's first query.
        self._opened_served = False
        self.http_pages = 0

    @property
    def profile_dir(self) -> str | None:
        return self.inner.profile_dir

    # -- lifecycle ------------------------------------------------------------

    def __enter__(self) -> HybridSession:
        self._start_netlog()
        try:
            self.inner.__enter__()
        except BaseException:
            self._read_netlog()  # releases the capture for another session
            raise
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    def resume(self) -> None:
        self._start_netlog()
        self.inner.resume()

    def suspend(self) -> str:
        path = self.inner.suspend()
        self._read_netlog()
        return path

    def close(self) -> None:
        try:
            self.inner.close()
        finally:
            self._read_netlog()
            if self._http is not None:
                try:
                    self._http.close()
                except Exception:
                    pass
                self._http = None

    def needs_restart(self) -> bool:
        """A NetLog is complete only once the browser is closed: after a served
        search on a recording browser, the scraper restarts it to read one."""
        return self._netlog is not None and self._recorded

    def _start_netlog(self) -> None:
        self._recorded = False
        if (
            self._headers.request is not None
            or self._capture_failed
            or not self._headers.claim_capture()
        ):
            self.inner.netlog = None
            return
        fd, self._netlog = tempfile.mkstemp(prefix="gbs-netlog-", suffix=".json")
        os.close(fd)
        self.inner.netlog = self._netlog

    def _read_netlog(self) -> None:
        path, self._netlog = self._netlog, None
        self.inner.netlog = None
        if path is None:
            return
        requests: list[dict] = []
        try:
            requests = read_netlog(path, host=urlsplit(search_page()).netloc)
        except Exception as exc:
            self._emit({"event": "hybrid_headers_failed", "error": type(exc).__name__})
        finally:
            # Nothing of it is kept but the parsed template.
            try:
                os.remove(path)
            except OSError:
                pass
            self._headers.offer(requests)
        if self._headers.request is not None and requests:
            self._emit({"event": "hybrid_headers", "requests": len(requests)})
        elif self._recorded:
            # A served search left no request in the log (an engine or a
            # browser that does not write it): one try, not a restart after
            # every search.
            self._capture_failed = True

    # -- browser pass-throughs ----------------------------------------------------

    def visit(self, url: str, dwell_seconds: float) -> bool:
        return self.inner.visit(url, dwell_seconds)

    def blank(self) -> None:
        self.inner.blank()

    # -- searching ----------------------------------------------------------------

    def _usable(self) -> bool:
        return (
            self._headers.request is not None
            and not self._headers.disabled
            and not self._off
            and self._opened_served
            and self._proxy_url is not None
        )

    def search(
        self, query: str, direct: bool = False, hl: str | None = None, gl: str | None = None
    ) -> Fetched:
        self._locale = {k: v for k, v in (("hl", hl), ("gl", gl)) if v}
        if (direct or self._locale) and self._usable():
            fetched = self._fetch(query, 1)
            if fetched is not None:
                return fetched
        return self._browser(lambda: self.inner.search(query, direct=direct, **self._locale), query)

    def next_page(self) -> Fetched | None:
        if self._last_source == "http" and self._last is not None:
            # The browser is not on this query's page, so its "Next" would be
            # another query's: the next page is opened from the query itself.
            query, page, html = self._last
            if 'id="pnnext"' not in html:
                return None  # Google has no further page
            fetched = self._fetch(query, page + 1) if self._usable() else None
            if fetched is not None:
                return fetched
            return self._browser(
                lambda: self.inner.open_results(query, page * 10, **self._locale), query, page + 1
            )
        return self._browser(self.inner.next_page, None)

    def _browser(self, call, query: str | None, page: int = 1) -> Fetched | None:
        if self._cookie:
            self.inner.set_google_cookies(self._cookie, tuple(sorted(self._deleted)))
            self._deleted.clear()
        fetched = call()
        self._last_source = "browser"
        self._last = None
        if fetched is not None and classify(fetched.url, fetched.html, fetched.status).served:
            self._cookie = self.inner.google_cookies()
            self._recorded = self._netlog is not None
            self._opened_served = True
        return fetched

    def _fetch(self, query: str, page: int) -> Fetched | None:
        """One results page over HTTP, or None when the browser has to take it."""
        url = results_url(
            query,
            self._locale.get("hl") or self.inner.hl,
            self._locale.get("gl") or self.inner.gl,
            (page - 1) * 10,
        )
        if not self._cookie:
            self._cookie = self.inner.google_cookies()
        mark = time.monotonic()
        try:
            response = self._client().get(
                url, headers=replay_headers(self._headers.request, self._cookie)
            )
        except Exception as exc:
            self._end_series("error", type(exc).__name__)
            return None
        load = round(time.monotonic() - mark, 2)
        self._cookie = fold_cookies(self._cookie, set_cookie_headers(response), self._deleted)
        html = response.text
        verdict = classify(response.url, html, response.status_code)
        if not verdict.served:
            self._end_series("check" if verdict.kind == "no_js" else verdict.kind)
            return None
        # "No results" is an answer; an ordinary page without results is not.
        parsed = parse_serp(html) if verdict.kind == "ok" else {}
        if verdict.kind == "ok" and not any(
            parsed.get(f) for f in ("organic_results", "ai_overview", "local_results")
        ):
            self._end_series("empty")
            return None
        self._series += 1
        if self._series == GOOD_SERIES:
            # A long series clears the run's count of exits that stepped back,
            # even when it never ends in a check (a capped or retired exit).
            self._headers.worked()
        self.http_pages += 1
        self._last_source = "http"
        self._last = (query, page, html)
        return Fetched(
            url=str(response.url),
            status=response.status_code,
            html=html,
            timings={"http": load},
        )

    def _client(self):
        if self._http is None:
            from curl_cffi import requests as cr

            proxies = {"http": self._proxy_url, "https": self._proxy_url}
            target = impersonate_for(self._headers.request)
            self._http = cr.Session(
                impersonate=target, default_headers=False, proxies=proxies, timeout=TIMEOUT
            )
            self._emit({"event": "hybrid_client", "impersonate": target})
        return self._http

    def _end_series(self, reason: str, error: str | None = None) -> None:
        """The HTTP side stopped; the browser takes the query."""
        served, self._series = self._series, 0
        self._emit(
            {
                "event": "hybrid_series",
                "pages": served,
                "stop": reason,
                **({"error": error} if error else {}),
            }
        )
        if served >= GOOD_SERIES:
            self._headers.worked()
        # Whatever stopped it: the browser takes the query next, and when the
        # exit itself is refused there the scraper retires it anyway. A refusal
        # the browser then gets past is Google turning the HTTP side away, and
        # asking again only collects more refusals.
        weak = served < FALLBACK_MIN
        self._weak = self._weak + 1 if weak else 0
        if self._weak >= FALLBACK_SERIES:
            self._off = True
            run = self._headers.stepped_back()
            self._emit({"event": "hybrid_fallback", "run": run, "reason": reason})
