"""Plain-language progress on stderr, for a person watching a run.

The scraper's events are JSON for machines (`-v` prints them as they are).
Without `-v`, this turns the few that matter to a person into short lines: a
new proxy IP and its warm-up, Google refusing one, each finished query, and a
summary at the end, so a first run that spends minutes warming up does not
look like a hang.
"""

from __future__ import annotations

import re
import sys
import threading
import time
from typing import Any, TextIO

from .classify import REFUSED


def _duration(seconds: float) -> str:
    seconds = int(round(seconds))
    if seconds < 60:
        return f"{seconds} s"
    minutes, seconds = divmod(seconds, 60)
    if minutes < 60:
        return f"{minutes} min {seconds} s" if seconds else f"{minutes} min"
    hours, minutes = divmod(minutes, 60)
    return f"{hours} h {minutes} min"


def _why(reason: str) -> str:
    """A retire reason in a few words: the network error code when there is one."""
    code = re.search(r"net::(ERR_[A-Z_]+)", reason)
    if code:
        return f"the connection through it failed ({code.group(1)})"
    if "interrupted by another navigation" in reason:
        return "a page navigated away during the search"
    if "Timeout" in reason:
        return "a page did not load in time"
    return reason.removeprefix("error: ")[:160]


def _mb(n: float | None) -> str:
    return f"{(n or 0):.1f} MB"


class Progress:
    """Receives scraper events and records; writes lines a person can read."""

    def __init__(
        self,
        stream: TextIO | None = None,
        *,
        total: int | None = None,
        breaker: int = 6,
        clock=time.monotonic,
    ) -> None:
        self._stream = stream or sys.stderr
        self._total = total
        self._breaker = breaker
        self._clock = clock
        self._started = clock()
        self._lock = threading.Lock()
        self._done = 0
        self._ips = 0
        self._explained = False

    def _say(self, line: str) -> None:
        with self._lock:
            print(line, file=self._stream, flush=True)

    def on_event(self, event: dict[str, Any]) -> None:
        kind = event.get("event")
        if kind == "warming":
            self._ips += 1
            if event.get("spare"):
                self._say(f"proxy IP {self._ips}: warming up in the background as a spare")
            elif event.get("donor"):
                self._say(f"proxy IP {self._ips}: starting from cookies Google already accepts")
            elif event.get("pages"):
                if not self._explained:
                    self._explained = True
                    self._say(
                        f"proxy IP {self._ips}: warming up the browser on {event['pages']} "
                        "ordinary pages before the first search. This takes a few minutes; "
                        "later proxy IPs start from its cookies and skip it."
                    )
                else:
                    self._say(f"proxy IP {self._ips}: warming up (a few minutes)")
            else:
                self._say(f"proxy IP {self._ips}: starting the browser")
        elif kind == "minted" and event.get("warm_pages") and not event.get("donor"):
            self._say(
                f"  warmed up in {_duration(event.get('seconds') or 0)}, "
                f"{_mb((event.get('warm_bytes') or 0) / (1024 * 1024))}"
            )
        elif kind == "query" and event.get("verdict") in REFUSED:
            self._say(
                f"  Google refused this proxy IP ({event['verdict']}); trying a new one. "
                f"This is normal; the run stops after {self._breaker} in a row."
            )
        elif kind == "retired":
            reason = str(event.get("reason") or "")
            if reason.startswith("error") or reason.startswith("proxy"):
                self._say(f"  proxy IP dropped: {_why(reason)}; trying a new one")

    def record(self, record: dict[str, Any]) -> None:
        meta = record.get("search_metadata") or {}
        params = record.get("search_parameters") or {}
        query = params.get("q", "")
        page = params.get("page", 1)
        if page == 1:
            self._done += 1
        count = f"[{self._done}/{self._total}]" if self._total else f"[{self._done}]"
        label = query if page == 1 else f"{query} (page {page})"
        if meta.get("status") == "Success":
            results = len(record.get("organic_results") or [])
            took = meta.get("total_time_taken")
            timing = f", {took:.1f} s" if isinstance(took, (int, float)) else ""
            self._say(f"{count} {label}: {results} results{timing}")
        else:
            self._say(f"{count} {label}: failed ({record.get('error') or meta.get('status')})")

    def summary(self, summary: dict[str, Any]) -> None:
        elapsed = _duration(self._clock() - self._started)
        served = summary.get("pages_served", 0)
        failed = summary.get("queries_failed", 0)
        if not served and not failed and not summary.get("identities"):
            return  # nothing started (a setup error says why on its own)
        lines = [f"done in {elapsed}: {served} pages served, {failed} queries failed"]
        if "traffic_mb" in summary:
            total = summary["traffic_mb"]
            warm = summary.get("warmup_mb", 0)
            line = f"traffic: {_mb(total)} in all"
            if warm:
                line += f", of which {_mb(warm)} warming up proxy IPs"
            if served:
                line += f"; {_mb(max(total - warm, 0) / served)} a page after the warm-up"
            lines.append(line)
            if summary.get("cost_per_1000_pages") is not None:
                lines.append(f"cost: {summary['cost_per_1000_pages']} per 1000 pages")
        ips = summary.get("identities", 0)
        refused = summary.get("exits_refused", 0)
        lines.append(f"proxy IPs: {ips} used, {refused} refused by Google")
        if served and summary.get("warmup_mb"):
            lines.append(
                "most of the warm-up is paid at the start of a run; long runs cost much "
                "less per page"
            )
        for line in lines:
            self._say(line)
