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
_MB = 1024 * 1024


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
    """NodeMaven's gateway, built by the `nodemaven` SDK.

    Takes the Proxy Username and Proxy Password from the dashboard out of
    NODEMAVEN_LOGIN and NODEMAVEN_PASSWORD, or from `serpmaven login`, so
    credentials never pass through the command line or shell history.
    """

    sticky = True

    def __init__(self, country: str | None = None, **params: str) -> None:
        try:
            from nodemaven import Proxy
        except ImportError as exc:  # pragma: no cover - depends on the extra
            raise RuntimeError(
                "the NodeMaven source needs the SDK: pip install --upgrade serpmaven"
            ) from exc
        from . import config

        credentials = config.nodemaven_credentials()
        if credentials is None:
            raise RuntimeError(
                "no NodeMaven proxy login: run `serpmaven login`, or set NODEMAVEN_LOGIN "
                "and NODEMAVEN_PASSWORD"
            )
        login, password = credentials
        if country:
            params["country"] = country
        self._proxy = Proxy(login=login, password=password, **params)
        self._params = params

    def for_session(self, session_id: str) -> dict[str, str]:
        return self._proxy.session(session_id).playwright()

    def describe(self) -> str:
        shown = ", ".join(f"{k}={v}" for k, v in sorted(self._params.items())) or "any country"
        return f"NodeMaven gateway ({shown}, sticky)"


class NodeMavenDemoSource:
    """A traffic-capped sub-user made from NODEMAVEN_API_KEY for one run; `close()` deletes it."""

    sticky = True

    def __init__(
        self,
        *,
        traffic_limit_mb: float = 300,
        country: str | None = None,
        api_key: str | None = None,
        **params: str,
    ) -> None:
        if not 0 < traffic_limit_mb < float("inf"):
            raise ValueError("traffic_limit_mb must be a positive, finite number")
        try:
            from nodemaven import Proxy
            from nodemaven.api import Client
        except ImportError as exc:  # pragma: no cover - depends on the extra
            raise RuntimeError(
                "the NodeMaven demo source needs the SDK: pip install 'serpmaven[nodemaven]'"
            ) from exc
        if not hasattr(Client, "create_sub_user"):
            raise RuntimeError(
                "the installed nodemaven SDK is too old for sub-user provisioning: "
                "pip install --upgrade 'serpmaven[nodemaven]'"
            )
        key = api_key or os.environ.get("NODEMAVEN_API_KEY")
        if not key:
            raise RuntimeError(
                "set NODEMAVEN_API_KEY in the environment (the dashboard API key, "
                "not the proxy login or password)"
            )
        if country:
            params["country"] = country
        client = Client(api_key=key)
        username = f"gbs{secrets.token_hex(6)}"
        password = secrets.token_hex(16)
        created = client.create_sub_user(
            username,
            password,
            traffic_limit=int(traffic_limit_mb * _MB),
            is_traffic_limited=True,
        )
        self._client = client
        self._sub_user_id = created["id"]
        self._params = params
        try:
            self._proxy = Proxy(
                login=created.get("proxy_username", username),
                password=created.get("proxy_password", password),
                **params,
            )
        except Exception:
            client.delete_sub_user(self._sub_user_id)
            raise
        self._closed = False

    def for_session(self, session_id: str) -> dict[str, str]:
        return self._proxy.session(session_id).playwright()

    def describe(self) -> str:
        shown = ", ".join(f"{k}={v}" for k, v in sorted(self._params.items())) or "any country"
        return f"NodeMaven demo sub-user ({shown}, sticky, capped)"

    def close(self, *, keep: bool = False) -> None:
        """Delete the provisioned sub-user, unless `keep` is set. Idempotent."""
        if self._closed:
            return
        if not keep:
            self._client.delete_sub_user(self._sub_user_id)
        self._closed = True
