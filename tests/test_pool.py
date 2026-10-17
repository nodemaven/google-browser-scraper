"""Several scrapers sharing one queue of queries, with fake browsers."""

import threading
import time

import pytest
from test_scraper import REFUSED, SERVED, FakeRelay, FakeSession

from google_browser_scraper.cli import main
from google_browser_scraper.pool import Pool, merge_stats
from google_browser_scraper.proxy import ProxyTemplate
from google_browser_scraper.scraper import Breaker, ExitsRefused, Scraper, Settings, Templates


class Recording(FakeSession):
    def __init__(self, *args, worker, **kwargs):
        super().__init__(*args, **kwargs)
        self.worker = worker
        self.thread = threading.get_ident()

    def search(self, query, direct=False):
        # Long enough that one worker cannot drain the queue before the other
        # starts; the tests below are about sharing it.
        time.sleep(0.01)
        return super().search(query, direct)


def pool(scripts_per_worker, settings=None, cls=Recording, on_event=None, start=None):
    """`scripts_per_worker[i]` is the list of identity scripts worker i mints.
    `start` stands in for the pool's wait before each worker starts."""
    FakeSession.opened = []
    FakeRelay.by_address = {}
    queues = [list(scripts) for scripts in scripts_per_worker]

    def make(i):
        def factory(proxy, **options):
            return cls(proxy, script=queues[i].pop(0), worker=i, **options)

        return Scraper(
            ProxyTemplate("http://u-{session}:p@gate.example.com:7000"),
            settings or Settings(gap_seconds=(0, 0), dwell_seconds=(0, 0)),
            session_factory=factory,
            relay_factory=FakeRelay,
            resolver_factory=None,
            sleep=lambda seconds: None,
            on_event=on_event,
        )

    return Pool(make, len(scripts_per_worker), sleep=start or (lambda seconds: None))


def test_every_query_is_answered_once_across_the_workers():
    p = pool([[[SERVED] * 10], [[SERVED] * 10]])
    queries = [f"q{i}" for i in range(8)]
    records = list(p.run(queries))
    assert sorted(r["search_parameters"]["q"] for r in records) == sorted(queries)
    assert all(r["search_metadata"]["status"] == "Success" for r in records)
    searched = [q for s in FakeSession.opened for q in s.searched]
    assert sorted(searched) == sorted(queries)
    assert p.not_run == []
    assert all(s.closed for s in FakeSession.opened)


def test_each_worker_keeps_its_browsers_on_its_own_thread_and_tags_its_records():
    p = pool([[[SERVED] * 10], [[SERVED] * 10]])
    records = list(p.run([f"q{i}" for i in range(8)]))
    threads = {s.worker: s.thread for s in FakeSession.opened}
    by_worker = {}
    for s in FakeSession.opened:
        by_worker.setdefault(s.worker, set()).update(s.searched)
    for r in records:
        assert r["search_parameters"]["q"] in by_worker[r["search_metadata"]["worker"]]
    assert len(set(threads.values())) == len(threads) == 2


def test_one_tripped_breaker_stops_the_pool_and_reports_what_was_not_run():
    settings = Settings(breaker=2, gap_seconds=(0, 0), dwell_seconds=(0, 0))
    p = pool([[[REFUSED], [REFUSED]], [[SERVED] * 50]], settings)
    queries = [f"q{i}" for i in range(40)]
    with pytest.raises(ExitsRefused):
        records = []
        for r in p.run(queries):
            records.append(r)
    answered = {r["search_parameters"]["q"] for r in records}
    assert answered.isdisjoint(p.not_run)
    assert len(answered) + len(p.not_run) <= len(queries)
    assert p.not_run, "the pool ran everything despite the breaker"
    assert all(s.closed for s in FakeSession.opened)


def test_pool_stats_add_up_the_workers():
    p = pool([[[SERVED] * 10], [[SERVED] * 10]])
    list(p.run([f"q{i}" for i in range(6)]))
    stats = p.stats
    assert stats.pages_served == 6
    assert stats.identities == 2
    assert stats.summary()["pages_served"] == 6


def test_merge_stats_of_nothing_is_empty():
    assert merge_stats([]).summary()["pages_served"] == 0


@pytest.mark.parametrize("workers", ["0", "17"])
def test_the_command_line_refuses_a_worker_count_out_of_range(workers, capsys):
    assert main(["search", "x", "--no-proxy", "--workers", workers]) == 1
    assert "workers must be between" in capsys.readouterr().err


def test_one_worker_drawing_refused_exits_does_not_stop_a_pool_another_serves():
    # Lockstep: worker 1 may start a search only when worker 0, about to be
    # refused, hands it two permits, and worker 0 goes on once both searches
    # have started. Worker 1's first of the two has then been served and has
    # reset the breaker after worker 0's previous refusal was counted, so a
    # served page lands between any two refusals. A breaker of 2 per worker
    # would stop the pool on worker 0's second refusal.
    searched = {"n": 0}
    turn = threading.Condition()
    permits = threading.Semaphore(0)
    refusing_done = threading.Event()
    refusals = {"n": 0}

    class Waiting(Recording):
        def search(self, query, direct=False):
            if self.worker == 1:
                deadline = time.monotonic() + 10
                while not refusing_done.is_set() and not permits.acquire(timeout=0.05):
                    assert time.monotonic() < deadline
                with turn:
                    searched["n"] += 1
                    turn.notify_all()
            elif self.script and self.script[0] is REFUSED:
                refusals["n"] += 1
                with turn:
                    start = searched["n"]
                    permits.release(2)
                    assert turn.wait_for(lambda: searched["n"] >= start + 2, timeout=10)
                if refusals["n"] == 3:
                    refusing_done.set()
            return super().search(query, direct)

    settings = Settings(breaker=2, gap_seconds=(0, 0), dwell_seconds=(0, 0))
    worker0 = [[REFUSED], [REFUSED], [REFUSED]] + [[SERVED] * 20 for _ in range(3)]
    p = pool([worker0, [[SERVED] * 50]], settings, cls=Waiting)
    queries = [f"q{i}" for i in range(20)]
    records = list(p.run(queries))
    assert sorted(r["search_parameters"]["q"] for r in records) == sorted(queries)
    assert p.not_run == []
    assert refusals["n"] == 3


def test_a_pool_still_stops_when_no_worker_is_served():
    settings = Settings(breaker=3, gap_seconds=(0, 0), dwell_seconds=(0, 0))
    p = pool([[[REFUSED] for _ in range(5)], [[REFUSED] for _ in range(5)]], settings)
    with pytest.raises(ExitsRefused, match="none served in between"):
        list(p.run([f"q{i}" for i in range(10)]))
    assert len(FakeSession.opened) <= 4  # three in a row across the pool, plus one in flight


def test_a_worker_starts_from_the_template_another_worker_left():
    made = threading.Event()
    events = []

    def on_event(e):
        events.append(e)
        if e["event"] == "template":
            made.set()

    settings = Settings(
        clones=3,
        max_queries_per_exit=50,
        gap_seconds=(0, 0),
        dwell_seconds=(0, 0),
        settle_seconds=(0, 0),
    )
    # Worker 1 starts only once worker 0 has left a template.
    p = pool(
        [[[SERVED] * 30 for _ in range(3)], [[SERVED] * 30 for _ in range(3)]],
        settings,
        on_event=on_event,
        start=lambda seconds: made.wait(5) if seconds else None,
    )
    records = list(p.run([f"q{i}" for i in range(12)]))
    assert all(r["search_metadata"]["status"] == "Success" for r in records)
    first_of_worker1 = next(s for s in FakeSession.opened if s.worker == 1)
    assert first_of_worker1.visited == []  # a copy, not a fresh warm-up
    assert "profile_dir" in first_of_worker1.options
    assert [e["event"] for e in events].count("template") == 1
    assert p.stats.clones == 1


def test_a_scraper_leaves_a_shared_template_for_its_owner_to_drop():
    from test_scraper import scraper

    templates, breaker = Templates(3, 3600.0), Breaker(6)
    s = scraper(
        [[SERVED, SERVED]],
        Settings(clones=3, max_queries_per_exit=5, gap_seconds=(0, 0), dwell_seconds=(0, 0)),
    )
    s.share(breaker, templates)
    list(s.run(["a", "b"]))
    assert templates.present()  # another worker may still copy it
    templates.drop("run finished", lambda e: None)
    assert not templates.present()


def test_a_refused_copy_drops_only_the_template_it_came_from(tmp_path):
    dropped = []
    templates = Templates(3, 3600.0)
    for name in ("old", "new"):
        (tmp_path / name).mkdir()
    assert templates.claim()
    templates.put(str(tmp_path / "old"))
    templates.drop("too old", dropped.append)
    assert templates.claim()
    templates.put(str(tmp_path / "new"))
    templates.drop("a copy was refused on its first query", dropped.append, generation=1)
    assert templates.present() and len(dropped) == 1
    templates.drop("a copy was refused on its first query", dropped.append, generation=2)
    assert not templates.present()


def test_only_one_worker_at_a_time_may_leave_a_template(tmp_path):
    templates = Templates(3, 3600.0)
    assert templates.claim()
    assert not templates.claim() and not templates.wanted()
    templates.abort()
    assert templates.wanted() and templates.claim()
    templates.put(str(tmp_path))
    assert not templates.claim()
