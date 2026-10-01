"""Where a browser identity gets its exit from.

The scraper holds one exit for a series of queries, so it needs a *sticky*
session: the same exit for every request a browser makes until it is retired.
Most residential gateways select a sticky exit by putting a session id in the
proxy username, and each spells it differently, so the provider is described by
a URL with a `{session}` placeholder where its id goes:

    http://user-session-{session}:pass@gate.example.com:7000

A URL without the placeholder is accepted and reported as rotating. Against a
rotating gateway every request may leave from a different address, and an exit
that was warmed is not the exit that asks the question.
"""

from __future__ import annotations

import os
import secrets
from dataclasses import dataclass
from typing import Protocol
from urllib.parse import unquote, urlsplit

PLACEHOLDER = "{session}"
_SCHEMES = ("http", "https", "socks5")


class ProxySource(Protocol):
    """Anything that can hand a browser a proxy for a given session id."""

    sticky: bool

    def for_session(self, session_id: str) -> dict[str, str]:
        """A Playwright `proxy=` dict for this session."""

    def describe(self) -> str:
        """A one-line description that never contains the password."""


def new_session_id() -> str:
    """Twelve hex characters: unique enough per run, and legal in every username
    format seen so far."""
    return secrets.token_hex(6)


@dataclass(frozen=True)
class ProxyTemplate:
    """A proxy URL with an optional `{session}` placeholder."""

    url: str

    def __post_init__(self) -> None:
        parts = urlsplit(self.url.replace(PLACEHOLDER, "session"))
        if parts.scheme not in _SCHEMES:
            raise ValueError(
                f"proxy scheme must be one of {', '.join(_SCHEMES)}, got {parts.scheme!r}"
            )
        if not parts.hostname or not parts.port:
            raise ValueError("proxy URL needs a host and a port, e.g. http://user:pass@host:port")
        if parts.scheme == "socks5" and parts.username:
            # Chromium does not implement SOCKS5 authentication, so the browser
            # would connect without credentials and the gateway would refuse it.
            raise ValueError(
                "Chromium cannot authenticate to a SOCKS5 proxy. Use the provider's "
                "HTTP port, or whitelist this machine's address and drop the credentials."
            )

    @property
    def sticky(self) -> bool:
        return PLACEHOLDER in self.url

    def for_session(self, session_id: str) -> dict[str, str]:
        parts = urlsplit(self.url.replace(PLACEHOLDER, session_id))
        proxy = {"server": f"{parts.scheme}://{parts.hostname}:{parts.port}"}
        if parts.username:
            proxy["username"] = unquote(parts.username)
        if parts.password:
            proxy["password"] = unquote(parts.password)
        return proxy

    def describe(self) -> str:
        parts = urlsplit(self.url.replace(PLACEHOLDER, "{session}"))
        user = f"{unquote(parts.username)}:***@" if parts.username else ""
        kind = "sticky" if self.sticky else "rotating - no {session} placeholder"
        return f"{parts.scheme}://{user}{parts.hostname}:{parts.port} ({kind})"


class NodeMavenSource:
    """NodeMaven's gateway, built by the `nodemaven` SDK from the environment.

    Reads NODEMAVEN_LOGIN and NODEMAVEN_PASSWORD - the Proxy Username and Proxy
    Password from the dashboard - so credentials never pass through the
    command line or shell history.
    """

    sticky = True

    def __init__(self, country: str | None = None, **params: str) -> None:
        try:
            from nodemaven import Proxy
        except ImportError as exc:  # pragma: no cover - depends on the extra
            raise RuntimeError(
                "the NodeMaven source needs the SDK: pip install 'google-serp[nodemaven]'"
            ) from exc
        login = os.environ.get("NODEMAVEN_LOGIN")
        password = os.environ.get("NODEMAVEN_PASSWORD")
        if not login or not password:
            raise RuntimeError("set NODEMAVEN_LOGIN and NODEMAVEN_PASSWORD in the environment")
        if country:
            params["country"] = country
        self._proxy = Proxy(login=login, password=password, **params)
        self._params = params

    def for_session(self, session_id: str) -> dict[str, str]:
        return self._proxy.session(session_id).playwright()

    def describe(self) -> str:
        shown = ", ".join(f"{k}={v}" for k, v in sorted(self._params.items())) or "any country"
        return f"NodeMaven gateway ({shown}, sticky)"
