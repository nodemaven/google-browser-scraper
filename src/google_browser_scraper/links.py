"""Recover result destinations by asking Google's /goto redirect.

A results page usually carries only an encrypted `/goto?url=<token>` per result.
Requesting that redirect answers with the destination in `Location`, and a token
works from any client, so a link costs one small request.

The resolver uses its own sticky proxy session, never the browser's, so link
resolution does not touch the warmed browser session, and its traffic goes
through a relay so it is counted like everything else.

A page that came over HTTP is resolved on its own exit instead: each result's
anchor carries a ping URL, which Google answers there with a small page naming
the destination. Its /goto answers with a destination only for the few results
it prefetches, and asked from a fresh exit it is refused.
"""

from __future__ import annotations

import html
import re
import threading
import time
import urllib.error
import urllib.request
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from typing import Any
from urllib.parse import urlsplit

from lxml import html as lxml_html

from .relay import Relay

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/151.0.0.0 Safari/537.36"
)
TIMEOUT_S = 30
#: /goto links resolved at once for one page.
PARALLEL = 4
#: Seconds one page's ping lookups may take; the rest go to the /goto resolver.
PING_BUDGET_S = 30.0
#: Google's answer to a ping URL names the destination in a refresh tag and in
#: the script that navigates to it.
_REFRESH = re.compile(r'<meta[^>]*content="0;url=([^"]+)"')
_SCRIPT = re.compile(r"redirectUrl='([^']+)'")


def ping_paths(page: str) -> dict[str, str]:
    """Each result anchor's /goto href, mapped to the same anchor's ping URL."""
    paths: dict[str, str] = {}
    for anchor in lxml_html.fromstring(page).iter("a"):
        href, ping = anchor.get("href") or "", anchor.get("ping") or ""
        if href.startswith("/goto?") and ping.startswith("/url?"):
            paths.setdefault(href, ping)
    return paths


def ping_destination(body: str) -> str | None:
    """The destination named by Google's answer to a ping URL."""
    if match := _REFRESH.search(body):
        return html.unescape(match.group(1))
    if match := _SCRIPT.search(body):
        return re.sub(r"\\x([0-9a-fA-F]{2})", lambda m: chr(int(m.group(1), 16)), match.group(1))
    return None


def _refused(location: str) -> bool:
    host = urlsplit(location).netloc.lower()
    return "/sorry" in location or host.startswith(("sorry.", "consent."))


class _AlwaysProxy(urllib.request.ProxyHandler):
    """A ProxyHandler that never bypasses its proxy.

    The stock one asks `proxy_bypass`, which on Windows reads the registry's
    ProxyOverride list, so a host the system marks local goes direct - out of
    the relay's count and, for a real host, out of the proxy.
    """

    def proxy_open(self, req, proxy, type):
        req.set_proxy(urlsplit(proxy).netloc, "http")
        return None


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


class LinkResolver:
    """Turn `https://www.google.com/goto?url=...` into the destination URL.

    `upstream` is a Playwright-style proxy dict for a session of its own, or
    None for a direct connection. A refusal (a redirect to /sorry, or no
    Location at all) disables the resolver for the rest of the run rather than
    spending more requests on an exit Google has started refusing.
    """

    def __init__(
        self,
        upstream: dict[str, str] | None,
        *,
        relay_factory: Callable[..., Any] | None = Relay,
        timeout: float = TIMEOUT_S,
    ) -> None:
        self._relay = None
        self._lock = threading.Lock()
        self.resolved = 0
        self.failed = 0
        proxy_url = None
        if relay_factory is not None and (
            upstream is None or upstream["server"].startswith("http://")
        ):
            self._relay = relay_factory(upstream)
            proxy_url = self._relay.start()
        elif upstream is not None:
            # A SOCKS5 or HTTPS upstream the relay cannot speak; urllib cannot
            # either, so there is no way to resolve through it.
            self.enabled = False
            self._opener = None
            return
        handlers: list[Any] = [_NoRedirect]
        if proxy_url:
            handlers.insert(0, _AlwaysProxy({"http": proxy_url, "https": proxy_url}))
        else:
            handlers.insert(0, urllib.request.ProxyHandler({}))  # ignore proxy env vars
        self._opener = urllib.request.build_opener(*handlers)
        self._timeout = timeout
        self.enabled = True

    @property
    def bytes(self) -> int:
        return self._relay.total if self._relay is not None else 0

    def hosts(self) -> dict[str, int]:
        return self._relay.hosts() if self._relay is not None else {}

    def resolve(self, goto_url: str) -> str | None:
        if not self.enabled:
            return None
        request = urllib.request.Request(
            goto_url,
            headers={"User-Agent": UA, "Accept": "text/html", "Accept-Language": "en-US,en;q=0.9"},
        )
        try:
            response = self._opener.open(request, timeout=self._timeout)
            location = response.headers.get("Location")
            response.read()
        except urllib.error.HTTPError as err:
            location = err.headers.get("Location") if 300 <= err.code < 400 else None
            err.read()
        except Exception:
            with self._lock:
                self.failed += 1
            return None
        if not location or _refused(location):
            with self._lock:
                self.failed += 1
                self.enabled = False
            return None
        with self._lock:
            self.resolved += 1
        return location

    def fill(self, results: list[dict[str, Any]]) -> None:
        """Resolve every result that has a /goto redirect and no link yet.

        Up to PARALLEL requests run at once. A refusal disables the resolver,
        so the requests still queued behind it are not sent.
        """
        todo = [
            r
            for r in results
            if r.get("link") is None and "/goto?" in (r.get("redirect_link") or "")
        ]
        if not todo:
            return
        with ThreadPoolExecutor(max_workers=min(PARALLEL, len(todo))) as pool:
            links = list(pool.map(lambda r: self.resolve(r["redirect_link"]), todo))
        for result, link in zip(todo, links, strict=True):
            result["link"] = link

    def fill_from_pings(
        self,
        results: list[dict[str, Any]],
        page: str,
        get: Callable[[str, float], tuple[int, str, str] | None],
    ) -> None:
        """Resolve a page's /goto links on the exit the page came from.

        `get(path, timeout)` asks Google's host for `path` there within
        `timeout` seconds and returns the status, the Location and the body, or
        None on an error. Each result is asked
        through its anchor's ping URL. A refusal or an error ends asking for
        this page, as does `PING_BUDGET_S`; the resolver itself stays on, since
        another exit is another answer. A result left without a link is counted
        by `fill`, which tries it next, or here when `fill` will not.
        """
        pings = ping_paths(page)
        deadline = time.monotonic() + PING_BUDGET_S
        for result in results:
            redirect = result.get("redirect_link") or ""
            if result.get("link") is not None or "/goto?" not in redirect:
                continue
            path = pings.get("/goto?" + redirect.split("/goto?", 1)[1])
            if path is None:
                continue
            left = deadline - time.monotonic()
            if left <= 0:
                return
            answer = get(path, left)
            if answer is None:
                return
            status, location, body = answer
            if _refused(location):
                return
            link = None
            if 300 <= status < 400:
                link = location or None
            elif status == 200:
                link = ping_destination(body)
            with self._lock:
                if link:
                    self.resolved += 1
                    result["link"] = link
                elif not self.enabled:
                    self.failed += 1

    def close(self) -> None:
        if self._relay is not None:
            self._relay.stop()
