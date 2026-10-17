"""The identity lifecycle, with a fake browser and no clock."""

import os
import tempfile

import pages
import pytest

from google_browser_scraper.browser import Fetched
from google_browser_scraper.proxy import ProxyTemplate
from google_browser_scraper.scraper import ExitsRefused, ProxyRefused, Scraper, Settings

SEARCH = "https://www.google.com/search?q=x"
SERVED = Fetched(SEARCH, 200, pages.results_page())
REFUSED = Fetched(pages.SORRY_URL, 429, pages.SORRY_PAGE)


class FakeRelay:
    """Stands in for the loopback relay: remembers the upstream it was given and
    counts whatever the fake browser says it sent."""

    by_address = {}

    #: What the next relay's upstream proxy answers to CONNECT when refusing.
    refusal = None

    def __init__(self, upstream):
        self.upstream, self.total, self.stopped = upstream, 0, False
        self.by_host = {}
        self.refusal = FakeRelay.refusal

    def hosts(self):
        return dict(self.by_host)

    def start(self):
        address = f"http://127.0.0.1:{len(FakeRelay.by_address) + 1}"
        FakeRelay.by_address[address] = self
        return address

    def stop(self):
        self.stopped = True


WARM_BYTES, PAGE_BYTES = 1_000, 500_000
WARM_HOST, GOOGLE_HOST = "warm.example", "www.google.com"


COOKIES = os.path.join("Default", "Network", "Cookies")
CACHE = os.path.join("Default", "Cache", "data_0")


class FakeSession:
    """Plays a scripted sequence of pages; records what it was asked."""

    opened = []

    def __init__(self, proxy, *, script, warm_ok=True, **options):
        self.proxy, self.options = proxy, options
        self.relay = FakeRelay.by_address.get((proxy or {}).get("server"))
        self.script, self.warm_ok = script, warm_ok
        self.visited, self.searched, self.closed = [], [], False
        self.direct = []
        self.suspended = 0
        self.blanks = 0
        # A profile on disk, so copying it can be checked: the cookie database
        # names the session the profile was first made for, and a cache file
        # stands for everything a copy should leave behind.
        given = options.get("profile_dir")
        self.profile_dir = given or tempfile.mkdtemp(prefix="fake-profile-")
        if not given:
            for rel, text in (
                (COOKIES, str(id(self))),
                (COOKIES + "-journal", ""),
                ("Local State", "{}"),
                (CACHE, "x" * 1000),
            ):
                path = os.path.join(self.profile_dir, rel)
                os.makedirs(os.path.dirname(path), exist_ok=True)
                with open(path, "w") as fh:
                    fh.write(text)
        FakeSession.opened.append(self)

    def _spend(self, n, host):
        if self.relay is not None:
            self.relay.total += n
            self.relay.by_host[host] = self.relay.by_host.get(host, 0) + n

    def __enter__(self):
        return self

    def close(self):
        self.closed = True

    def suspend(self):
        self.suspended += 1
        return self.profile_dir

    def resume(self):
        pass

    def marker(self):
        with open(os.path.join(self.profile_dir, COOKIES)) as fh:
            return fh.read()

    def has(self, rel):
        return os.path.exists(os.path.join(self.profile_dir, rel))

    def visit(self, url, dwell):
        self.visited.append(url)
        self._spend(WARM_BYTES, WARM_HOST)
        return self.warm_ok

    def search(self, query, direct=False):
        self.searched.append(query)
        self.direct.append(direct)
        self._spend(PAGE_BYTES, GOOGLE_HOST)
        return self._next()

    def blank(self):
        self.blanks += 1

    def next_page(self):
        if not self.script:
            return None
        self._spend(PAGE_BYTES, GOOGLE_HOST)
        return self._next()

    def _next(self):
        item = self.script.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


def scraper(scripts, settings=None, warm_ok=True, events=None, relay=True, cls=None, sleep=None):
    """One script per identity, consumed in mint order."""
    FakeSession.opened = []
    FakeRelay.by_address = {}
    FakeRelay.refusal = None
    scripts = list(scripts)

    def factory(proxy, **options):
        return (cls or FakeSession)(proxy, script=scripts.pop(0), warm_ok=warm_ok, **options)

    return Scraper(
        ProxyTemplate("http://u-{session}:p@gate.example.com:7000"),
        settings or Settings(gap_seconds=(0, 0), dwell_seconds=(0, 0)),
        session_factory=factory,
        relay_factory=FakeRelay if relay else None,
        resolver_factory=None,
        sleep=sleep or (lambda seconds: None),
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


def test_a_browser_error_twice_running_drops_the_exit():
    records = list(scraper([[TimeoutError("nav"), TimeoutError("nav")], [SERVED]]).run(["a"]))
    assert records[0]["search_metadata"]["status"] == "Success"
    assert FakeSession.opened[0].closed


def test_a_browser_error_is_retried_once_on_the_same_exit_from_the_front_page():
    events = []
    s = scraper([[SERVED, TimeoutError("click"), SERVED]], events=events)
    records = list(s.run(["a", "b"]))
    assert [r["search_metadata"]["status"] for r in records] == ["Success", "Success"]
    (session,) = FakeSession.opened
    assert session.searched == ["a", "b", "b"]
    assert session.visited[-1] == "https://www.google.com/?hl=en"  # reloaded before the retry
    assert records[1]["exit"]["queries_on_exit"] == 2
    assert [e["error"] for e in events if e["event"] == "search_retry"] == ["TimeoutError: click"]


def test_a_network_error_drops_the_exit_without_a_retry():
    events = []
    tunnel = RuntimeError("Page.goto: net::ERR_TUNNEL_CONNECTION_FAILED at https://www.google.com/")
    records = list(scraper([[SERVED, tunnel], [SERVED]], events=events).run(["a", "b"]))
    assert [r["search_metadata"]["status"] for r in records] == ["Success", "Success"]
    first, second = FakeSession.opened
    assert first.searched == ["a", "b"] and first.closed
    assert second.searched == ["b"]
    assert not [e for e in events if e["event"] == "search_retry"]


def test_a_served_exit_is_held_well_past_ten_queries_by_default():
    queries = [f"q{i}" for i in range(30)]
    list(scraper([[SERVED] * 30]).run(queries))
    assert len(FakeSession.opened) == 1


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
        {"spare": -1},
        {"spare": 4},
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

        def hosts(self):
            return {}

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
    script = [TimeoutError("navigation timed out"), TimeoutError("navigation timed out")]
    list(scraper([script, [SERVED]], events=events).run(["a"]))
    retired = [e for e in events if e["event"] == "retired"][0]
    assert retired["reason"] == "error: TimeoutError: navigation timed out"


def spare_settings(**overrides):
    return Settings(spare=1, gap_seconds=(0, 0), dwell_seconds=(0, 0), **overrides)


def test_a_spare_warmed_in_the_background_replaces_a_capped_exit():
    events = []
    s = scraper([[SERVED, SERVED], [SERVED]], spare_settings(max_queries_per_exit=2), events=events)
    records = list(s.run(["a", "b", "c"]))
    assert [r["search_metadata"]["status"] for r in records] == ["Success"] * 3
    first, second = FakeSession.opened
    assert first.searched == ["a", "b"] and second.searched == ["c"]
    assert len(second.visited) == 6  # warmed before it was handed a query
    minted = [e for e in events if e["event"] == "minted"]
    assert "spare" not in minted[0]
    assert minted[1]["spare"] is True and minted[1]["waited"] >= 0 and minted[1]["idle"] >= 0


def test_a_spare_takes_over_from_an_exit_refused_later():
    records = list(scraper([[SERVED, REFUSED], [SERVED]], spare_settings()).run(["a", "b"]))
    assert [r["search_metadata"]["status"] for r in records] == ["Success", "Success"]
    assert [x.searched for x in FakeSession.opened] == [["a", "b"], ["b"]]


def test_no_spare_is_warmed_when_nothing_is_left_to_ask():
    list(scraper([[SERVED]], spare_settings()).run(["a"]))
    assert len(FakeSession.opened) == 1


def test_an_unused_spare_is_closed_and_its_traffic_counted():
    events = []
    s = scraper([[SERVED, SERVED], [SERVED]], spare_settings(), events=events)
    list(s.run(["a", "b"]))
    first, spare = FakeSession.opened
    assert spare.searched == [] and first.closed and spare.closed
    summary = s.stats.summary()
    assert summary["identities"] == 2
    spent = sum(r.total for r in FakeRelay.by_address.values())
    assert summary["traffic_mb"] == round(spent / 2**20, 2)
    assert [e["reason"] for e in events if e["event"] == "retired"][-1] == "spare unused"


def test_a_spare_keeps_its_browser_on_one_thread_of_its_own():
    import threading

    class Recording(FakeSession):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            self.threads = {threading.get_ident()}

        def __enter__(self):
            self.threads.add(threading.get_ident())
            return self

        def visit(self, url, dwell):
            self.threads.add(threading.get_ident())
            return super().visit(url, dwell)

        def search(self, query, direct=False):
            self.threads.add(threading.get_ident())
            return super().search(query, direct)

        def close(self):
            self.threads.add(threading.get_ident())
            super().close()

    s = scraper([[SERVED], [SERVED]], spare_settings(max_queries_per_exit=1), cls=Recording)
    list(s.run(["a", "b"]))
    first, spare = FakeSession.opened
    assert spare.searched == ["b"]
    assert first.threads == {threading.get_ident()}
    assert len(spare.threads) == 1 and spare.threads != first.threads


def test_closing_cuts_a_spare_warm_up_short():
    import time as clock

    class Slow(FakeSession):
        def visit(self, url, dwell):
            clock.sleep(0.2)
            return super().visit(url, dwell)

    s = scraper([[SERVED, SERVED], [SERVED]], spare_settings(), cls=Slow)
    list(s.run(["a", "b"]))
    first, spare = FakeSession.opened
    assert len(first.visited) == 6
    assert len(spare.visited) < 6 and spare.closed


def test_a_spare_that_fails_to_launch_is_replaced_not_fatal():
    events = []

    class LaunchFails(FakeSession):
        def __enter__(self):
            if len(FakeSession.opened) == 2:
                raise RuntimeError("browser did not start")
            return self

    s = scraper(
        [[SERVED], [SERVED], [SERVED]],
        spare_settings(max_queries_per_exit=1),
        events=events,
        cls=LaunchFails,
    )
    records = list(s.run(["a", "b"]))
    assert [r["search_metadata"]["status"] for r in records] == ["Success", "Success"]
    first, spare, fresh = FakeSession.opened
    assert spare.searched == [] and fresh.searched == ["b"]
    assert [e["error"] for e in events if e["event"] == "spare_failed"] == [
        "RuntimeError: browser did not start"
    ]


@pytest.mark.parametrize("status", ["402 traffic exhausted", "407 Proxy Authentication Required"])
def test_an_account_wide_proxy_refusal_stops_at_the_first_exit(status):
    s = scraper([[SERVED]] * 6, warm_ok=False)
    FakeRelay.refusal = f"HTTP/1.1 {status}"
    with pytest.raises(ProxyRefused, match=status.split()[0]):
        list(s.run(["a", "b"]))
    assert len(FakeSession.opened) == 1
    assert FakeSession.opened[0].closed and FakeSession.opened[0].relay.stopped
    assert len(FakeSession.opened[0].visited) == 1  # the warm-up stops at the first refusal


def test_a_proxy_refusal_mid_run_stops_it_with_the_reason():
    events = []
    tunnel = RuntimeError("Page.goto: net::ERR_TUNNEL_CONNECTION_FAILED at https://www.google.com/")
    s = scraper([[SERVED, tunnel]], events=events)
    records = []
    with pytest.raises(ProxyRefused, match="402"):
        for r in s.run(["a", "b"]):
            records.append(r)
            FakeSession.opened[0].relay.refusal = "HTTP/1.1 402 traffic exhausted"
    assert [r["search_metadata"]["status"] for r in records] == ["Success"]
    assert FakeSession.opened[0].closed
    assert any(e.get("reason", "").startswith("proxy: HTTP/1.1 402") for e in events)


def test_an_exit_specific_proxy_refusal_does_not_stop_the_run():
    s = scraper(
        [[SERVED], [SERVED]],
        warm_ok=False,
        settings=Settings(breaker=2, gap_seconds=(0, 0), dwell_seconds=(0, 0)),
    )
    FakeRelay.refusal = "HTTP/1.1 502 Bad Gateway"
    with pytest.raises(ExitsRefused) as caught:
        list(s.run(["a"]))
    assert not isinstance(caught.value, ProxyRefused)
    assert len(FakeSession.opened) == 2


def clone_settings(**overrides):
    values = dict(
        clones=3,
        max_queries_per_exit=2,
        gap_seconds=(0, 0),
        dwell_seconds=(0, 0),
        settle_seconds=(45, 45),
    )
    values.update(overrides)
    return Settings(**values)


def test_new_exits_start_from_a_copy_of_the_served_profile():
    events, slept = [], []
    s = scraper([[SERVED, SERVED], [SERVED]], clone_settings(), events=events, sleep=slept.append)
    records = list(s.run(["a", "b", "c"]))
    assert [r["search_metadata"]["status"] for r in records] == ["Success"] * 3
    seed, copy = FakeSession.opened
    assert seed.suspended == 1  # closed once for the copy, then reopened
    assert copy.options["profile_dir"] != seed.profile_dir
    assert copy.marker() == seed.marker()
    assert copy.has("Local State") and copy.has(COOKIES + "-journal")
    assert not copy.has(CACHE)  # the jar travels, the rest of the profile does not
    assert copy.visited == []  # no warm-up of its own
    assert copy.searched == ["c"]
    assert slept.count(45) == 2  # after reopening the seed, and before the copy's first query
    assert s.stats.clones == 1
    assert [e["event"] for e in events].count("template") == 1


def test_a_copy_refused_on_its_first_query_ends_the_copying():
    events = []
    s = scraper([[SERVED, SERVED], [REFUSED], [SERVED]], clone_settings(), events=events)
    records = list(s.run(["a", "b", "c"]))
    assert [r["search_metadata"]["status"] for r in records] == ["Success"] * 3
    seed, copy, fresh = FakeSession.opened
    assert copy.searched == ["c"] and copy.visited == []
    assert len(fresh.visited) == 6  # warmed from scratch again
    dropped = [e for e in events if e["event"] == "template_dropped"]
    assert dropped[0]["reason"] == "a copy was refused on its first query"
    assert not os.path.exists(copy.profile_dir) or copy.closed


def test_a_template_past_its_age_is_not_used(monkeypatch):
    import google_browser_scraper.scraper as module

    clock = iter(range(0, 10**6, 100))  # every reading is 100 s after the last
    monkeypatch.setattr(module.time, "monotonic", lambda: next(clock))
    s = scraper([[SERVED, SERVED], [SERVED]], clone_settings(clone_max_age=150))
    list(s.run(["a", "b", "c"]))
    seed, fresh = FakeSession.opened
    assert len(fresh.visited) == 6


def test_copies_stop_when_the_template_is_used_up():
    s = scraper(
        [[SERVED, SERVED], [SERVED, SERVED], [SERVED]],
        clone_settings(clones=1),
    )
    list(s.run(["a", "b", "c", "d", "e"]))
    seed, copy, fresh = FakeSession.opened
    assert copy.visited == [] and len(fresh.visited) == 6
    assert s.stats.clones == 1


def test_no_profile_is_copied_when_copying_is_off_or_nothing_is_left_to_ask():
    list(scraper([[SERVED, SERVED]]).run(["a", "b"]))
    assert FakeSession.opened[0].suspended == 0
    list(scraper([[SERVED]], clone_settings()).run(["a"]))
    assert FakeSession.opened[0].suspended == 0


def test_the_template_is_removed_when_the_run_ends():
    events = []
    s = scraper([[SERVED, SERVED, SERVED]], clone_settings(max_queries_per_exit=5), events=events)
    list(s.run(["a", "b"]))
    dropped = [e for e in events if e["event"] == "template_dropped"]
    assert dropped and dropped[-1]["reason"] == "run finished"


def test_an_exit_capped_on_its_first_query_still_leaves_a_template():
    slept = []
    s = scraper([[SERVED], [SERVED]], clone_settings(max_queries_per_exit=1), sleep=slept.append)
    list(s.run(["a", "b"]))
    seed, copy = FakeSession.opened
    assert seed.suspended == 1 and seed.closed
    assert copy.visited == [] and copy.searched == ["b"]
    assert slept.count(45) == 1  # only before the copy's first query


def test_a_profile_with_no_cookie_database_leaves_no_template():
    class NoJar(FakeSession):
        def suspend(self):
            os.remove(os.path.join(self.profile_dir, COOKIES))
            return super().suspend()

    events = []
    s = scraper([[SERVED], [SERVED], [SERVED]], clone_settings(), events=events, cls=NoJar)
    records = list(s.run(["a", "b", "c"]))
    assert [r["search_metadata"]["status"] for r in records] == ["Success"] * 3
    failed = [e for e in events if e["event"] == "template_failed"]
    assert len(failed) == 2 and "no cookie database" in failed[0]["error"]
    assert s.stats.clones == 0
    assert all(len(f.visited) == 6 for f in FakeSession.opened)  # each warmed itself


def test_a_record_says_where_its_time_went():
    timed = Fetched(SEARCH, 200, pages.results_page(), timings={"type": 2.5, "load": 3.0})
    record = next(iter(scraper([[timed]]).run(["a"])))
    timings = record["search_metadata"]["timings"]
    assert timings["type"] == 2.5 and timings["load"] == 3.0
    assert "parse" in timings and "links" not in timings  # no resolver in these tests


def test_an_exits_first_query_is_typed_and_the_rest_open_the_results_page():
    list(scraper([[SERVED, SERVED, SERVED], [SERVED]]).run(["a", "b", "c"]))
    assert FakeSession.opened[0].direct == [False, True, True]


def test_every_query_is_typed_when_asked():
    settings = Settings(type_every_query=True, gap_seconds=(0, 0), dwell_seconds=(0, 0))
    list(scraper([[SERVED, SERVED]], settings).run(["a", "b"]))
    assert FakeSession.opened[0].direct == [False, False]


def test_a_retry_after_a_browser_error_types_from_the_front_page():
    list(scraper([[SERVED, TimeoutError("click"), SERVED]]).run(["a", "b"]))
    assert FakeSession.opened[0].direct == [False, True, False]


def test_copied_cookies_are_used_for_three_hours_by_default():
    assert Settings().clone_max_age == 3 * 3600


def test_traffic_is_split_by_host_for_the_warm_up_the_pages_and_the_run():
    events = []
    s = scraper([[SERVED, SERVED]], events=events)
    list(s.run(["a", "b"]))
    minted = next(e for e in events if e["event"] == "minted")
    retired = next(e for e in events if e["event"] == "retired")
    warm = WARM_BYTES * minted["warm_pages"]
    assert minted["warm_hosts"] == {WARM_HOST: warm}
    assert retired["page_hosts"] == {GOOGLE_HOST: 2 * PAGE_BYTES}
    assert retired["hosts"] == {GOOGLE_HOST: 2 * PAGE_BYTES, WARM_HOST: warm}
    by_host = s.stats.summary()["traffic_by_host_mb"]
    assert list(by_host) == [GOOGLE_HOST, WARM_HOST]
    assert by_host[GOOGLE_HOST] == round(2 * PAGE_BYTES / 2**20, 2)


def test_links_resolve_while_the_browser_goes_on_to_the_next_query():
    import threading

    second_search = threading.Event()

    class Resolver:
        resolved = failed = bytes = 0

        def __init__(self, upstream, relay_factory=None):
            pass

        def fill(self, results):
            # Blocking here until the browser has started query "b" proves the
            # browser did not wait for this page's links.
            assert second_search.wait(5)
            for r in results:
                if r["link"] is None and "/goto?" in (r.get("redirect_link") or ""):
                    r["link"] = "https://resolved.example/"

        def close(self):
            pass

        def hosts(self):
            return {}

    class Session(FakeSession):
        def search(self, query, direct=False):
            if query == "b":
                second_search.set()
            return super().search(query, direct)

    FakeSession.opened, FakeRelay.by_address = [], {}
    scripts = [[SERVED, SERVED]]
    s = Scraper(
        ProxyTemplate("http://u-{session}:p@gate.example.com:7000"),
        Settings(gap_seconds=(0, 0), dwell_seconds=(0, 0)),
        session_factory=lambda proxy, **o: Session(proxy, script=scripts.pop(0), **o),
        relay_factory=FakeRelay,
        resolver_factory=Resolver,
        sleep=lambda x: None,
    )
    records = list(s.run(["a", "b"]))
    assert [r["search_parameters"]["q"] for r in records] == ["a", "b"]
    for r in records:
        assert r["organic_results"][2]["link"] == "https://resolved.example/"
        assert "links" in r["search_metadata"]["timings"]


def test_stop_after_read_leaves_the_page_after_each_query_and_only_when_asked():
    list(scraper([[SERVED, SERVED]]).run(["a", "b"]))
    assert FakeSession.opened[0].blanks == 0
    settings = Settings(gap_seconds=(0, 0), dwell_seconds=(0, 0), stop_after_read=True)
    list(scraper([[SERVED, SERVED]], settings).run(["a", "b"]))
    assert FakeSession.opened[0].blanks == 2
