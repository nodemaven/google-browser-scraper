"""Several scrapers at once, each on its own exit and its own thread.

One browser identity answers one query at a time, and most of that time is the
page and the gap between queries, not the machine. Running `workers` scrapers
side by side multiplies throughput by roughly that number until the machine
runs out of CPU or memory for the browsers.

Each worker is an ordinary `Scraper` with its own identities, spares and
traffic count. The workers share the queue of queries, one breaker and one
cookie-jar template. Playwright's sync API is bound to the thread that starts
it, and every worker starts its browsers on its own thread, so no browser is
shared across threads.

The breaker counts refused exits across the pool and any served page resets
it: one worker drawing bad exits while another keeps being served is not a bad
hour. When nobody is served for `breaker` exits in a row the whole pool stops,
since the other workers would only spend traffic finding the same thing.
Queries nobody answered by then are listed in `not_run`.

The template is shared for the same reason: a served cookie jar is worth as
much to every worker, and a worker whose copy ran out should start from the
other worker's jar rather than warm fresh exits.
"""

from __future__ import annotations

import queue
import threading
import time
from collections.abc import Callable, Iterable, Iterator
from typing import Any

from .scraper import Breaker, ExitsRefused, Scraper, Stats, Templates

MAX_WORKERS = 16
_DONE = object()


class Pool:
    """Run queries through `workers` scrapers made by `make(index)`.

    Records arrive in the order they are produced, not the order of the
    queries; every record carries its query and, in `search_metadata.worker`,
    the index of the worker that made it. `start_gap` seconds separate the
    workers' starts, so their browsers do not all launch in the same second.
    """

    def __init__(
        self,
        make: Callable[[int], Scraper],
        workers: int,
        *,
        start_gap: float = 5.0,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        if not 1 <= workers <= MAX_WORKERS:
            raise ValueError(f"workers must be between 1 and {MAX_WORKERS}")
        self._make = make
        self.workers = workers
        self._start_gap = start_gap
        self._sleep = sleep
        self._scrapers: list[Scraper] = []
        self._stop = threading.Event()
        self.not_run: list[str] = []

    def stop(self) -> None:
        """Take no new queries; workers finish the one in hand and close."""
        self._stop.set()

    @property
    def stats(self) -> Stats:
        return merge_stats(s.stats for s in self._scrapers)

    def run(self, queries: Iterable[str]) -> Iterator[dict[str, Any]]:
        """Yield records as workers produce them. Raises `ExitsRefused` after
        the pool has stopped if any worker's breaker tripped."""
        todo: queue.Queue[str] = queue.Queue()
        for q in queries:
            if q.strip():
                todo.put(q.strip())
        out: queue.Queue[Any] = queue.Queue()
        stopped: list[ExitsRefused] = []
        self._scrapers = [self._make(i) for i in range(self.workers)]
        first = self._scrapers[0].settings
        for scraper in self._scrapers[1:]:
            s = scraper.settings
            if (s.clones, s.clone_max_age, s.breaker) != (
                first.clones,
                first.clone_max_age,
                first.breaker,
            ):
                raise ValueError(
                    "workers share one cookie template and one breaker, so clones, "
                    "clone_max_age and breaker must be the same for every worker"
                )
        templates = Templates(first.clones, first.clone_max_age)
        breaker = Breaker(first.breaker)
        for scraper in self._scrapers:
            scraper.share(breaker, templates)
        threads = [
            threading.Thread(
                target=self._work,
                args=(i, scraper, todo, out, stopped),
                name=f"gbs-worker-{i}",
                daemon=True,
            )
            for i, scraper in enumerate(self._scrapers)
        ]
        for t in threads:
            t.start()
        finished = 0
        try:
            while finished < len(threads):
                item = out.get()
                if item is _DONE:
                    finished += 1
                else:
                    yield item
        finally:
            # An abandoned generator (Ctrl-C, a closed consumer) still has to
            # close every browser.
            self._stop.set()
            for t in threads:
                t.join()
            templates.drop("run finished", lambda event: None)
        while not todo.empty():
            self.not_run.append(todo.get_nowait())
        if stopped:
            raise stopped[0]

    def _work(self, index, scraper, todo, out, stopped) -> None:
        taken: list[str] = []

        def pending() -> Iterator[str]:
            while not self._stop.is_set():
                try:
                    q = todo.get_nowait()
                except queue.Empty:
                    return
                taken.append(q)
                yield q

        try:
            self._sleep(index * self._start_gap)
            if self._stop.is_set():
                return
            for record in scraper.run(pending()):
                q = record["search_parameters"]["q"]
                if record["search_parameters"].get("page", 1) == 1 and q in taken:
                    taken.remove(q)
                record["search_metadata"]["worker"] = index
                out.put(record)
        except ExitsRefused as exc:
            stopped.append(exc)
            self._stop.set()
        except Exception as exc:  # a worker's crash must not hang the pool
            stopped.append(ExitsRefused(f"worker {index} failed: {exc}"))
            self._stop.set()
        finally:
            # Taken but never answered: Scraper.run looks one query ahead.
            for q in taken:
                todo.put(q)
            out.put(_DONE)


def merge_stats(parts: Iterable[Stats]) -> Stats:
    """One `Stats` adding up several scrapers'; `summary()` works on it as usual."""
    merged = Stats()
    for s in parts:
        for name in (
            "identities",
            "clones",
            "pages_served",
            "queries_failed",
            "exits_refused",
            "exits_unreachable",
            "bytes_total",
            "bytes_warm",
            "links_resolved",
            "links_failed",
        ):
            setattr(merged, name, getattr(merged, name) + getattr(s, name))
        merged.counted = merged.counted and s.counted
        for host, n in s.bytes_by_host.items():
            merged.bytes_by_host[host] = merged.bytes_by_host.get(host, 0) + n
        merged._open.extend(s._open)
    return merged
