"""The identity lifecycle, with a fake browser and no clock."""

import pages
import pytest

from google_browser_scraper.browser import Fetched
from google_browser_scraper.proxy import ProxyTemplate
from google_browser_scraper.scraper import ExitsRefused, Scraper, Settings

SEARCH = "https://www.google.com/search?q=x"
SERVED = Fetched(SEARCH, 200, pages.results_page())
REFUSED = Fetched(pages.SORRY_URL, 429, pages.SORRY_PAGE)


class FakeRelay:
    """Stands in for the loopback relay: remembers the upstream it was given and
    counts whatever the fake browser says it sent."""

    by_address = {}

    def __init__(self, upstream):
        self.upstream, self.total, self.stopped = upstream, 0, False

    def start(self):
        address = f"http://127.0.0.1:{len(FakeRelay.by_address) + 1}"
        FakeRelay.by_address[address] = self
        return address

    def stop(self):
        self.stopped = True


WARM_BYTES, PAGE_BYTES = 1_000, 500_000


class FakeSession:
    """Plays a scripted sequence of pages; records what it was asked."""

    opened = []

    def __init__(self, proxy, *, script, warm_ok=True, **options):
        self.proxy, self.options = proxy, options
        self.relay = FakeRelay.by_address.get((proxy or {}).get("server"))
        self.script, self.warm_ok = script, warm_ok
        self.visited, self.searched, self.closed = [], [], False
        FakeSession.opened.append(self)

    def _spend(self, n):
        if self.relay is not None:
            self.relay.total += n

    def __enter__(self):
        return self

    def close(self):
        self.closed = True

    def visit(self, url, dwell):
        self.visited.append(url)
        self._spend(WARM_BYTES)
        return self.warm_ok

    def search(self, query):
        self.searched.append(query)
        self._spend(PAGE_BYTES)
        return self._next()

    def next_page(self):
        if not self.script:
            return None
        self._spend(PAGE_BYTES)
        return self._next()

    def _next(self):
        item = self.script.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


def scraper(scripts, settings=None, warm_ok=True, events=None, relay=True):
    """One script per identity, consumed in mint order."""
    FakeSession.opened = []
    FakeRelay.by_address = {}
    scripts = list(scripts)

    def factory(proxy, **options):
        return FakeSession(proxy, script=scripts.pop(0), warm_ok=warm_ok, **options)

    return Scraper(
        ProxyTemplate("http://u-{session}:p@gate.example.com:7000"),
        settings or Settings(gap_seconds=(0, 0), dwell_seconds=(0, 0)),
        session_factory=factory,
        relay_factory=FakeRelay if relay else None,
        resolver_factory=None,
        sleep=lambda seconds: None,
        on_event=(events.append if events is not None else None),
    )


def test_served_exit_is_held_for_the_next_query():
    records = list(scraper([[SERVED, SERVED]]).run(["a", "b"]))
    assert [r["search_metadata"]["status"] for r in records] == ["Success", "Success"]
    assert len(FakeSession.opened) == 1
    session = FakeSession.opened[0]
    assert session.searched == ["a", "b"]
    assert records[1]["exit"]["queries_on_exit"] == 2
    assert records[0]["organic_results"][0]["position"] == 1


def test_refused_exit_is_dropped_and_the_query_asked_again_elsewhere():
    records = list(scraper([[REFUSED], [SERVED]]).run(["a"]))
    assert [r["search_metadata"]["status"] for r in records] == ["Success"]
    first, second = FakeSession.opened
    assert first.closed and first.searched == ["a"]
    assert second.searched == ["a"]
    # Each identity got its own sticky session upstream, behind its own relay.
    assert first.relay.upstream["username"] != second.relay.upstream["username"]
    assert first.relay.stopped and first.proxy == {"server": "http://127.0.0.1:1"}


def test_every_identity_is_warmed_with_the_ladder_before_its_first_query():
    scraper([[REFUSED], [SERVED]]).run(["a"]).__next__()
    for session in FakeSession.opened:
        assert len(session.visited) == 6
        assert session.visited[0] == "https://www.theverge.com/"


def test_query_cap_retires_a_served_exit():
    settings = Settings(max_queries_per_exit=2, gap_seconds=(0, 0), dwell_seconds=(0, 0))
    list(scraper([[SERVED, SERVED], [SERVED]], settings).run(["a", "b", "c"]))
    assert [s.searched for s in FakeSession.opened] == [["a", "b"], ["c"]]
    assert FakeSession.opened[0].closed


def test_a_query_refused_on_every_exit_is_reported_not_skipped():
    settings = Settings(max_exits_per_query=2, gap_seconds=(0, 0), dwell_seconds=(0, 0))
    records = list(scraper([[REFUSED], [REFUSED], [SERVED]], settings).run(["a", "b"]))
    assert records[0]["search_metadata"]["status"] == "Error"
    assert "refused on 2 exits" in records[0]["error"]
    assert records[0]["search_parameters"]["q"] == "a"
    assert records[1]["search_metadata"]["status"] == "Success"


def test_breaker_stops_the_run_after_consecutive_refusals():
    settings = Settings(breaker=3, gap_seconds=(0, 0), dwell_seconds=(0, 0))
    with pytest.raises(ExitsRefused):
        list(scraper([[REFUSED]] * 5, settings).run(["a", "b"]))
    assert len(FakeSession.opened) == 3
    assert all(s.closed for s in FakeSession.opened)


def test_a_served_page_resets_the_breaker():
    settings = Settings(breaker=2, gap_seconds=(0, 0), dwell_seconds=(0, 0))
    # refused, served (held), the held exit is refused on "b", a fresh one serves it.
    # Without the reset the second refusal would be the second in a row and trip.
    scripts = [[REFUSED], [SERVED, REFUSED], [SERVED]]
    records = list(scraper(scripts, settings).run(["a", "b"]))
    assert [r["search_metadata"]["status"] for r in records] == ["Success", "Success"]


def test_unreachable_exit_counts_toward_the_breaker():
    settings = Settings(breaker=2, gap_seconds=(0, 0), dwell_seconds=(0, 0))
    with pytest.raises(ExitsRefused):
        list(scraper([[SERVED], [SERVED]], settings, warm_ok=False).run(["a"]))
    assert all(not s.searched for s in FakeSession.opened)


def test_browser_error_drops_the_exit():
    records = list(scraper([[TimeoutError("nav")], [SERVED]]).run(["a"]))
    assert records[0]["search_metadata"]["status"] == "Success"
    assert FakeSession.opened[0].closed


def test_extra_pages_follow_next_on_the_same_exit():
    settings = Settings(pages=3, gap_seconds=(0, 0), dwell_seconds=(0, 0))
    records = list(scraper([[SERVED, SERVED, SERVED]], settings).run(["a"]))
    assert [r["search_parameters"]["page"] for r in records] == [1, 2, 3]
    assert len(FakeSession.opened) == 1


def test_a_refused_later_page_ends_the_query_and_the_exit():
    settings = Settings(pages=3, gap_seconds=(0, 0), dwell_seconds=(0, 0))
    records = list(scraper([[SERVED, REFUSED], [SERVED]], settings).run(["a", "b"]))
    assert [(r["search_parameters"]["q"], r["search_metadata"]["status"]) for r in records] == [
        ("a", "Success"),
        ("a", "Error"),
        ("b", "Success"),
    ]
    assert FakeSession.opened[0].closed


def test_the_run_closes_its_last_identity():
    list(scraper([[SERVED]]).run(["a"]))
    assert FakeSession.opened[0].closed


def test_events_name_sessions_and_never_carry_credentials():
    events = []
    list(scraper([[REFUSED], [SERVED]], events=events).run(["a"]))
    kinds = [e["event"] for e in events]
    assert kinds == ["minted", "query", "retired", "minted", "query", "retired"]
    assert all("password" not in e and "proxy" not in e for e in events)


@pytest.mark.parametrize(
    "bad",
    [
        {"warmup": "L9"},
        {"pages": 0},
        {"breaker": 0},
        {"gap_seconds": (5, 1)},
    ],
)
def test_settings_refuse_nonsense(bad):
    with pytest.raises(ValueError):
        Settings(**bad)


def test_traffic_is_counted_per_page_and_per_run():
    s = scraper([[REFUSED], [SERVED, SERVED]])
    records = list(s.run(["a", "b"]))
    assert [r["exit"]["bytes"] for r in records] == [PAGE_BYTES, PAGE_BYTES]
    assert records[0]["exit"]["warm_bytes"] == 6 * WARM_BYTES
    summary = s.stats.summary(price_per_gb=4.0)
    # The refused identity's warm-up and page are part of the cost of a served page.
    total = 2 * 6 * WARM_BYTES + 3 * PAGE_BYTES
    assert summary["traffic_mb"] == round(total / 2**20, 2)
    assert summary["pages_served"] == 2 and summary["exits_refused"] == 1
    assert summary["mb_per_served_page"] == round(total / 2**20 / 2, 2)
    assert summary["cost_per_1000_pages"] == round(total / 2**20 / 2 / 1024 * 4.0 * 1000, 2)


def test_without_a_relay_traffic_is_not_claimed():
    s = scraper([[SERVED]], relay=False)
    record = list(s.run(["a"]))[0]
    assert record["exit"]["bytes"] is None
    assert "traffic_mb" not in s.stats.summary()
    assert FakeSession.opened[0].proxy["username"].startswith("u-")


def test_search_keeps_the_identity_for_the_next_call():
    s = scraper([[SERVED, SERVED]])
    assert s.search("a")[0]["search_metadata"]["status"] == "Success"
    assert not FakeSession.opened[0].closed
    s.search("b")
    assert len(FakeSession.opened) == 1
    s.close()
    assert FakeSession.opened[0].closed


def test_prewarm_mints_before_the_first_query():
    s = scraper([[SERVED]])
    s.prewarm()
    assert len(FakeSession.opened[0].visited) == 6
    assert FakeSession.opened[0].searched == []


def test_relay_is_required_for_engines_that_cannot_authenticate():
    class NeedsRelay(FakeSession):
        needs_relay = True

    s = Scraper(
        ProxyTemplate("socks5://gate.example.com:1080"),
        Settings(gap_seconds=(0, 0), dwell_seconds=(0, 0)),
        session_factory=NeedsRelay,
        relay_factory=FakeRelay,
        sleep=lambda x: None,
    )
    with pytest.raises(ValueError, match="needs the relay"):
        s.prewarm()


def test_save_html_keeps_every_page_and_names_it_in_the_record(tmp_path):
    settings = Settings(gap_seconds=(0, 0), dwell_seconds=(0, 0), save_html=str(tmp_path))
    records = list(scraper([[REFUSED], [SERVED]], settings).run(["Best Shoes!"]))
    files = sorted(p.name for p in tmp_path.iterdir())
    assert files == ["0001_sorry_best-shoes_p1.html.gz", "0002_ok_best-shoes_p1.html.gz"]
    assert records[0]["search_metadata"]["raw_html_file"].endswith("0002_ok_best-shoes_p1.html.gz")


def test_unresolved_links_are_filled_by_the_resolver_on_a_session_of_its_own():
    made = []

    class FakeResolver:
        resolved = failed = bytes = 0

        def __init__(self, upstream, relay_factory=None):
            self.upstream = upstream
            made.append(self)

        def fill(self, results):
            for r in results:
                if r["link"] is None and "/goto?" in (r.get("redirect_link") or ""):
                    r["link"] = "https://resolved.example/"
                    self.resolved += 1

        def close(self):
            self.closed = True

    FakeSession.opened, FakeRelay.by_address = [], {}
    scripts = [[SERVED, SERVED]]
    s = Scraper(
        ProxyTemplate("http://u-{session}:p@gate.example.com:7000"),
        Settings(gap_seconds=(0, 0), dwell_seconds=(0, 0)),
        session_factory=lambda proxy, **o: FakeSession(proxy, script=scripts.pop(0), **o),
        relay_factory=FakeRelay,
        resolver_factory=FakeResolver,
        sleep=lambda x: None,
    )
    records = list(s.run(["a", "b"]))
    third = records[0]["organic_results"][2]
    assert third["link"] == "https://resolved.example/"
    assert len(made) == 1 and made[0].closed  # one resolver for the run, closed with it
    browser_user = FakeSession.opened[0].relay.upstream["username"]
    assert made[0].upstream["username"] != browser_user  # never the browser's session
    assert s.stats.summary()["links_resolved"] == 2


def test_errors_are_one_line_with_credentials_masked():
    from google_browser_scraper.scraper import describe_error

    exc = RuntimeError("proxy http://user:secret@gate.example.com:7000 failed\nCall log: ...")
    text = describe_error(exc)
    assert text == "RuntimeError: proxy http://***@gate.example.com:7000 failed"
    assert describe_error(TimeoutError()) == "TimeoutError"


def test_a_browser_error_names_its_message_in_the_retired_event():
    events = []
    list(scraper([[TimeoutError("navigation timed out")], [SERVED]], events=events).run(["a"]))
    retired = [e for e in events if e["event"] == "retired"][0]
    assert retired["reason"] == "error: TimeoutError: navigation timed out"
