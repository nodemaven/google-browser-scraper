"""The identity lifecycle: warm an exit, hold it while Google serves it, drop it
the moment Google refuses it.

The expensive event is finding an exit Google will serve. Once one is found it
usually keeps being served, so a served exit is worth a series of queries, and
a refused one is worth nothing - retrying it only burns the address further.

Traffic is counted by a loopback relay per identity (see `relay`);
`use_relay=False` hands the proxy to the browser directly instead.
"""

from __future__ import annotations

import gzip
import random
import re
import time
from collections.abc import Callable, Iterable, Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from . import engines
from . import warmup as ladder
from .classify import Verdict, classify
from .links import LinkResolver
from .output import build_record
from .parse import parse_serp
from .proxy import ProxySource, new_session_id
from .relay import Relay

MB = 1024 * 1024


_CREDENTIALS = re.compile(r"//[^/\s:@]+:[^@\s]+@")


def describe_error(exc: BaseException) -> str:
    """One line naming an exception, for records and events.

    Browser errors can be long and can quote the proxy URL they were given, so
    only the first line is kept, it is truncated, and `user:pass@` is masked.
    """
    lines = str(exc).strip().splitlines()
    first = _CREDENTIALS.sub("//***@", lines[0] if lines else "")[:200]
    return f"{type(exc).__name__}: {first}" if first else type(exc).__name__


class ExitsRefused(RuntimeError):
    """Too many consecutive exits were refused or unreachable. Raised instead
    of carrying on, because on a shared, metered pool every further identity
    costs traffic and reputation and the evidence says it will fail too."""


@dataclass
class Settings:
    engine: str = engines.DEFAULT_ENGINE
    warmup: str = ladder.DEFAULT
    pages: int = 1
    max_queries_per_exit: int = 10
    max_exits_per_query: int = 3
    breaker: int = 6
    gap_seconds: tuple[float, float] = (8.0, 20.0)
    dwell_seconds: tuple[float, float] = ladder.DWELL_SECONDS
    headless: bool = False
    channel: str | None = None
    timezone_id: str | None = None
    browser_args: tuple[str, ...] = ()
    use_relay: bool = True
    hl: str = "en"
    #: Directory to keep every page's HTML in, gzipped, for debugging a parse.
    #: The pages carry the exit's location, so keep them out of anything public.
    save_html: str | None = None
    #: Ask Google's /goto redirect for each result's destination, from a proxy
    #: session of its own; pages no longer carry the URLs themselves.
    resolve_links: bool = True

    def __post_init__(self) -> None:
        engines.get(self.engine)
        ladder.pages(self.warmup)
        for name in ("pages", "max_queries_per_exit", "max_exits_per_query", "breaker"):
            if getattr(self, name) < 1:
                raise ValueError(f"{name} must be at least 1")
        for name in ("gap_seconds", "dwell_seconds"):
            low, high = getattr(self, name)
            if not 0 <= low <= high:
                raise ValueError(f"{name} must be a (low, high) pair with 0 <= low <= high")


@dataclass
class Identity:
    session_id: str
    session: Any
    relay: Any
    warm_pages: int
    warm_delivered: int
    warm_bytes: int | None
    queries: int = 0
    consent_dismissed: bool = False

    def bytes(self) -> int | None:
        return self.relay.total if self.relay is not None else None

    def info(self, level: str, page_bytes: int | None = None) -> dict[str, Any]:
        return {
            "session": self.session_id,
            "queries_on_exit": self.queries,
            "warmup": level,
            "warm_pages_delivered": f"{self.warm_delivered}/{self.warm_pages}",
            "warm_bytes": self.warm_bytes,
            "bytes": page_bytes,
            "consent_dismissed": self.consent_dismissed,
        }


@dataclass
class Stats:
    """What a run cost, counted across every identity including refused ones."""

    identities: int = 0
    pages_served: int = 0
    queries_failed: int = 0
    exits_refused: int = 0
    exits_unreachable: int = 0
    bytes_total: int = 0
    bytes_warm: int = 0
    links_resolved: int = 0
    links_failed: int = 0
    counted: bool = True
    _open: list = field(default_factory=list, repr=False)

    def summary(self, price_per_gb: float | None = None) -> dict[str, Any]:
        total = self.bytes_total + sum(i.bytes() or 0 for i in self._open)
        out: dict[str, Any] = {
            "identities": self.identities,
            "pages_served": self.pages_served,
            "queries_failed": self.queries_failed,
            "exits_refused": self.exits_refused,
            "exits_unreachable": self.exits_unreachable,
            "links_resolved": self.links_resolved,
            "links_failed": self.links_failed,
        }
        if self.counted:
            out["traffic_mb"] = round(total / MB, 2)
            out["warmup_mb"] = round(self.bytes_warm / MB, 2)
            if self.pages_served:
                per_page = total / MB / self.pages_served
                out["mb_per_served_page"] = round(per_page, 2)
                if price_per_gb is not None:
                    out["cost_per_1000_pages"] = round(per_page / 1024 * price_per_gb * 1000, 2)
        return out


class Scraper:
    """Run queries through a pool of browser identities.

    `session_factory`, `relay_factory` and `sleep` are injectable so the
    lifecycle can be tested without a browser, a network or a clock.
    """

    def __init__(
        self,
        proxy: ProxySource | None = None,
        settings: Settings | None = None,
        *,
        session_factory: Callable[..., Any] | None = None,
        relay_factory: Callable[..., Any] | None = Relay,
        resolver_factory: Callable[..., Any] | None = LinkResolver,
        sleep: Callable[[float], None] = time.sleep,
        rng: random.Random | None = None,
        on_event: Callable[[dict[str, Any]], None] | None = None,
    ) -> None:
        self.proxy = proxy
        self.settings = settings or Settings()
        self._factory = session_factory or engines.get(self.settings.engine)
        self._relay_factory = relay_factory
        self._resolver_factory = resolver_factory
        self._resolver = None
        self._sleep = sleep
        self._rng = rng or random.Random()
        self._emit = on_event or (lambda event: None)
        self._identity: Identity | None = None
        self._failed_exits = 0
        self._saved = 0
        self.stats = Stats()

    # -- public --------------------------------------------------------------

    def run(self, queries: Iterable[str]) -> Iterator[dict[str, Any]]:
        """Yield one record per results page, then close the last identity."""
        try:
            for query in queries:
                query = query.strip()
                if query:
                    yield from self._query(query)
        finally:
            self.close()

    def search(self, query: str) -> list[dict[str, Any]]:
        """One query, keeping the identity open for the next call (serve, MCP)."""
        return list(self._query(query.strip()))

    def prewarm(self) -> None:
        """Mint and warm an identity now, so the first query does not pay for it."""
        self._current_identity()

    def reset_breaker(self) -> None:
        self._failed_exits = 0

    def close(self) -> None:
        if self._identity is not None:
            self._retire("run finished")
        if self._resolver is not None:
            self._resolver.close()
            self.stats.bytes_total += self._resolver.bytes
            self.stats.links_resolved += self._resolver.resolved
            self.stats.links_failed += self._resolver.failed
            self._resolver = None

    # -- one query -----------------------------------------------------------

    def _query(self, query: str) -> Iterator[dict[str, Any]]:
        s = self.settings
        last_verdict: Verdict | None = None
        last_error: str | None = None
        for _ in range(s.max_exits_per_query):
            identity = self._current_identity()
            if identity is None:
                last_error = "exit unreachable: no warm-up page arrived"
                continue
            if identity.queries:
                self._sleep(self._rng.uniform(*s.gap_seconds))
            started, before = time.monotonic(), identity.bytes()
            try:
                fetched = identity.session.search(query)
            except Exception as exc:
                last_error = describe_error(exc)
                self._retire(f"error: {describe_error(exc)}")
                self._count_failure()
                continue
            identity.queries += 1
            identity.consent_dismissed = identity.consent_dismissed or fetched.consent_dismissed
            verdict = classify(fetched.url, fetched.html, fetched.status)
            self._event("query", identity, q=query, page=1, verdict=verdict.kind)
            saved = self._keep(fetched, query, 1, verdict)
            if verdict.served:
                self._failed_exits = 0
                yield self._served(query, 1, fetched, verdict, identity, started, before, saved)
                yield from self._more_pages(query, identity)
                if self._identity is identity and identity.queries >= s.max_queries_per_exit:
                    self._retire("query cap reached")
                return
            last_verdict, last_error = verdict, None
            self.stats.exits_refused += verdict.refused
            self._retire(verdict.kind)
            self._count_failure()
        self.stats.queries_failed += 1
        reason = last_verdict.reason if last_verdict else "unknown"
        yield build_record(
            query,
            page=1,
            verdict=last_verdict,
            url=None,
            elapsed=0.0,
            hl=s.hl,
            exit_info={"session": None},
            error=last_error or f"refused on {s.max_exits_per_query} exits: {reason}",
        )

    def _more_pages(self, query: str, identity: Identity) -> Iterator[dict[str, Any]]:
        for page in range(2, self.settings.pages + 1):
            self._sleep(self._rng.uniform(*self.settings.gap_seconds))
            started, before = time.monotonic(), identity.bytes()
            try:
                fetched = identity.session.next_page()
            except Exception as exc:
                self._retire(f"error: {describe_error(exc)}")
                yield self._failed(query, page, None, describe_error(exc), identity)
                return
            if fetched is None:
                return  # Google has no further page for this query.
            identity.queries += 1
            verdict = classify(fetched.url, fetched.html, fetched.status)
            self._event("query", identity, q=query, page=page, verdict=verdict.kind)
            saved = self._keep(fetched, query, page, verdict)
            if not verdict.served:
                self.stats.exits_refused += verdict.refused
                record = self._failed(query, page, verdict, verdict.reason, identity)
                self._retire(verdict.kind)
                yield record
                return
            yield self._served(query, page, fetched, verdict, identity, started, before, saved)

    def _links(self):
        if not self.settings.resolve_links or self._resolver_factory is None:
            return None
        if self._resolver is None:
            upstream = self.proxy.for_session(new_session_id()) if self.proxy is not None else None
            relay = self._relay_factory if self.settings.use_relay else None
            self._resolver = self._resolver_factory(upstream, relay_factory=relay)
        return self._resolver

    def _keep(self, fetched, query: str, page: int, verdict: Verdict) -> str | None:
        if not self.settings.save_html:
            return None
        folder = Path(self.settings.save_html)
        folder.mkdir(parents=True, exist_ok=True)
        self._saved += 1
        slug = re.sub(r"[^a-z0-9]+", "-", query.lower()).strip("-")[:40] or "query"
        path = folder / f"{self._saved:04d}_{verdict.kind}_{slug}_p{page}.html.gz"
        with gzip.open(path, "wt", encoding="utf-8") as handle:
            handle.write(fetched.html)
        return str(path)

    def _served(
        self, query, page, fetched, verdict, identity, started, before, saved=None
    ) -> dict[str, Any]:
        after = identity.bytes()
        page_bytes = after - before if after is not None and before is not None else None
        self.stats.pages_served += 1
        parsed = parse_serp(fetched.html)
        resolver = self._links()
        if resolver is not None:
            resolver.fill(parsed["organic_results"])
        record = build_record(
            query,
            page=page,
            verdict=verdict,
            url=fetched.url,
            hl=self.settings.hl,
            elapsed=time.monotonic() - started,
            exit_info=identity.info(self.settings.warmup, page_bytes),
            parsed=parsed,
        )
        if saved:
            record["search_metadata"]["raw_html_file"] = saved
        return record

    def _failed(self, query, page, verdict, error, identity) -> dict[str, Any]:
        return build_record(
            query,
            page=page,
            verdict=verdict,
            url=None,
            elapsed=0.0,
            hl=self.settings.hl,
            exit_info=identity.info(self.settings.warmup),
            error=error,
        )

    # -- identities ----------------------------------------------------------

    def _current_identity(self) -> Identity | None:
        if self._identity is None:
            self._identity = self._mint()
        return self._identity

    def _mint(self) -> Identity | None:
        s = self.settings
        session_id = new_session_id()
        upstream = self.proxy.for_session(session_id) if self.proxy is not None else None
        relay, browser_proxy = self._relay_for(upstream)
        started = time.monotonic()
        session = self._factory(
            browser_proxy,
            headless=s.headless,
            channel=s.channel,
            timezone_id=s.timezone_id,
            browser_args=s.browser_args,
            rng=self._rng,
        )
        try:
            session.__enter__()
        except Exception:
            if relay is not None:
                relay.stop()
            raise
        self.stats.identities += 1
        pages = ladder.pages(s.warmup)
        delivered = sum(
            bool(session.visit(url, self._rng.uniform(*s.dwell_seconds))) for url in pages
        )
        warm_bytes = relay.total if relay is not None else None
        self.stats.bytes_warm += warm_bytes or 0
        identity = Identity(session_id, session, relay, len(pages), delivered, warm_bytes)
        self.stats._open.append(identity)
        self._event(
            "minted",
            identity,
            warm_delivered=delivered,
            warm_pages=len(pages),
            warm_bytes=warm_bytes,
            seconds=round(time.monotonic() - started, 1),
        )
        if pages and not delivered:
            # Nothing arrived: the proxy is down, the credentials are wrong or
            # the exit is dead. Not Google's verdict, but just as final.
            self._identity = identity
            self.stats.exits_unreachable += 1
            self._retire("unreachable")
            self._count_failure()
            return None
        return identity

    def _relay_for(self, upstream: dict[str, str] | None):
        """Decide whether this identity goes through the counting relay."""
        needs = getattr(self._factory, "needs_relay", False)
        relayable = upstream is None or upstream["server"].startswith("http://")
        if self._relay_factory is None or not (self.settings.use_relay or needs):
            self.stats.counted = False
            return None, upstream
        if not relayable:
            if needs:
                raise ValueError(
                    f"the {self.settings.engine} engine needs the relay, which "
                    "speaks HTTP to the upstream proxy; use an http:// proxy URL"
                )
            self.stats.counted = False
            return None, upstream
        relay = self._relay_factory(upstream)
        return relay, {"server": relay.start()}

    def _retire(self, reason: str) -> None:
        identity, self._identity = self._identity, None
        if identity is None:
            return
        try:
            identity.session.close()
        finally:
            if identity.relay is not None:
                identity.relay.stop()
                self.stats.bytes_total += identity.relay.total
            if identity in self.stats._open:
                self.stats._open.remove(identity)
            self._event(
                "retired", identity, reason=reason, queries=identity.queries, bytes=identity.bytes()
            )

    def _event(self, kind: str, identity: Identity, **fields) -> None:
        self._emit({"event": kind, "session": identity.session_id, **fields})

    def _count_failure(self) -> None:
        self._failed_exits += 1
        if self._failed_exits >= self.settings.breaker:
            raise ExitsRefused(
                f"{self._failed_exits} exits in a row were refused or unreachable. "
                "Stopping rather than spending more traffic: check the proxy, the "
                "machine (`google-browser-scraper doctor`) and the hour, then retry."
            )
