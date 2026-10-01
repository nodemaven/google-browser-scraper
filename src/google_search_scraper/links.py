"""Recover result destinations by asking Google's /goto redirect.

A results page usually carries only an encrypted `/goto?url=<token>` per result.
Requesting that redirect answers with the destination in `Location`, and a token
works from any client, so a link costs one small request.

The resolver uses its own sticky proxy session, never the browser's, so link
resolution does not touch the warmed browser session, and its traffic goes
through a relay so it is counted like everything else.
"""

from __future__ import annotations

import urllib.error
import urllib.request
from collections.abc import Callable
from typing import Any
from urllib.parse import urlsplit

from .relay import Relay

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/151.0.0.0 Safari/537.36"
)
TIMEOUT_S = 30


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
            self.failed += 1
            return None
        host = urlsplit(location or "").netloc.lower()
        if not location or "/sorry" in location or host.startswith(("sorry.", "consent.")):
            self.failed += 1
            self.enabled = False
            return None
        self.resolved += 1
        return location

    def fill(self, results: list[dict[str, Any]]) -> None:
        """Resolve every result that has a /goto redirect and no link yet."""
        for result in results:
            redirect = result.get("redirect_link") or ""
            if result.get("link") is None and "/goto?" in redirect:
                result["link"] = self.resolve(redirect)

    def close(self) -> None:
        if self._relay is not None:
            self._relay.stop()
