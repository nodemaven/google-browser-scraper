"""The /goto resolver against a local server that answers like Google."""

import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from google_browser_scraper import links
from google_browser_scraper.links import (
    PING_BUDGET_S,
    LinkResolver,
    held_refresh,
    ping_destination,
    ping_paths,
)
from google_browser_scraper.scraper import _fill_links


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):  # noqa: N802 - the stdlib's name
        if "sorry" in self.path:
            target = "https://www.google.com/sorry/index?continue=x"
        elif "none" in self.path:
            target = None
        elif "loop" in self.path:
            target = "https://www.google.com/url?q=x"
        else:
            target = "https://www.runnersworld.com/gear/a25750345/running-shoes-flat-feet/"
        self.send_response(302 if target else 200)
        if target:
            self.send_header("Location", target)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def log_message(self, *args):
        pass


@pytest.fixture
def goto_server():
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{server.server_port}"
    server.shutdown()


def test_a_302_location_is_the_destination_and_is_counted(goto_server):
    resolver = LinkResolver(None)  # direct, through the counting relay
    try:
        link = resolver.resolve(f"{goto_server}/goto?url=CAESabc")
    finally:
        resolver.close()
    assert link == "https://www.runnersworld.com/gear/a25750345/running-shoes-flat-feet/"
    assert resolver.resolved == 1 and resolver.bytes > 0


def test_a_sorry_redirect_disables_the_resolver(goto_server):
    resolver = LinkResolver(None)
    try:
        assert resolver.resolve(f"{goto_server}/goto?url=sorry") is None
        assert not resolver.enabled
        assert resolver.resolve(f"{goto_server}/goto?url=CAESabc") is None  # not even asked
    finally:
        resolver.close()
    assert resolver.failed == 1 and resolver.resolved == 0


def test_no_location_is_a_failure(goto_server):
    resolver = LinkResolver(None)
    try:
        assert resolver.resolve(f"{goto_server}/goto?url=none") is None
    finally:
        resolver.close()


def test_a_redirect_to_googles_own_page_is_a_miss_not_a_refusal(goto_server):
    resolver = LinkResolver(None)
    try:
        assert resolver.resolve(f"{goto_server}/goto?url=loop") is None
        assert resolver.enabled
        assert resolver.resolve(f"{goto_server}/goto?url=CAESabc") is not None
    finally:
        resolver.close()
    assert resolver.failed == 1 and resolver.resolved == 1


def test_fill_only_touches_unresolved_goto_results(goto_server):
    resolver = LinkResolver(None)
    results = [
        {"link": "https://kept.example/", "redirect_link": f"{goto_server}/goto?url=a"},
        {"link": None, "redirect_link": f"{goto_server}/goto?url=b"},
        {"link": None},
    ]
    try:
        resolver.fill(results)
    finally:
        resolver.close()
    assert [r["link"] for r in results] == [
        "https://kept.example/",
        "https://www.runnersworld.com/gear/a25750345/running-shoes-flat-feet/",
        None,
    ]


def test_an_upstream_the_relay_cannot_speak_disables_resolving():
    resolver = LinkResolver({"server": "socks5://127.0.0.1:1080"})
    assert not resolver.enabled
    assert resolver.resolve("https://www.google.com/goto?url=x") is None
    resolver.close()


def test_fill_resolves_several_links_at_once_and_keeps_their_order():
    resolver = LinkResolver(None, relay_factory=None)
    running, peak = [0], [0]
    lock = threading.Lock()

    def slow(url):
        with lock:
            running[0] += 1
            peak[0] = max(peak[0], running[0])
        time.sleep(0.05)
        with lock:
            running[0] -= 1
        return url.replace("https://www.google.com/goto?url=", "")

    resolver.resolve = slow
    results = [
        {"redirect_link": f"https://www.google.com/goto?url=https://e{i}.com/"} for i in range(8)
    ]
    resolver.fill(results)
    assert [r["link"] for r in results] == [f"https://e{i}.com/" for i in range(8)]
    assert peak[0] > 1


def ping_answer(url):
    # Google's answer to a ping URL, as served: a script and a refresh tag.
    script_url = url.replace("&", "\x26")
    return (
        '<html lang="en"><head><meta content="origin" name="referrer"><script nonce="n">'
        f"(function(){{var redirectUrl='{script_url}';"
        "google.navigateTo(parent,window,redirectUrl);})();"
        f'</script><noscript><meta content="0;url={url.replace("&", "&amp;")}" '
        'http-equiv="refresh"></noscript></head></html>'
    )


PAGE = (
    "<html><body>"
    '<a href="/goto?url=AAA" ping="/url?sa=t&amp;source=web&amp;url=AAA&amp;ved=1">one</a>'
    '<a href="/goto?url=BBB" ping="/url?sa=t&amp;source=web&amp;url=BBB&amp;ved=2">two</a>'
    '<a href="/goto?url=CCC">no ping</a>'
    "</body></html>"
)


def test_a_ping_answer_names_the_destination():
    assert ping_destination(ping_answer("https://e.com/a?b=1&c=2")) == "https://e.com/a?b=1&c=2"
    script_only = r"<script>var redirectUrl='https://e.com/x\x3dy';</script>"
    assert ping_destination(script_only) == "https://e.com/x=y"
    assert ping_destination("<html>Loading...</html>") is None


def interstitial(token: str, delay: int = 3) -> str:
    """Google's "Verifying your request" page, which names the same /goto again."""
    return (
        f'<html><head><meta content="{delay};url=/goto?url={token}" http-equiv="refresh"></head>'
        "<body><div>Verifying your request</div>"
        '<div disabled="" role="button">Continue</div>'
        f"<script>(function(){{var delayMillis=926;var redirectUrl='/goto?url\\x3d{token}';"
        "})();</script></body></html>"
    )


def test_googles_own_redirects_are_not_destinations():
    assert ping_destination(interstitial("AAA")) is None
    assert ping_destination(ping_answer("https://www.google.com/goto?url=AAA")) is None
    assert ping_destination(ping_answer("https://www.google.com/url?q=x")) is None
    assert ping_destination(ping_answer("https://google.co.uk/url?q=x")) is None
    assert ping_destination(ping_answer("javascript:void(0)")) is None
    assert ping_destination(ping_answer("http://[::1/x")) is None  # malformed, not raised
    assert ping_destination(ping_answer("https://user@/path")) is None  # no host
    assert ping_destination(ping_answer("http://example.com:bad/path")) is None
    assert ping_destination(ping_answer("https://example.com:70000/path")) is None
    assert ping_destination(ping_answer("https://www.google.cat/url?q=x")) is None
    assert ping_destination(ping_answer("https://www.google.com./goto?url=A")) is None
    for kept in (
        "https://www.google.com/travel/flights/flights-to-lisbon.html",
        "https://notgoogle.com/url",
        "https://google.example.com/goto",
    ):
        assert ping_destination(ping_answer(kept)) == kept


def test_an_interstitial_or_a_redirect_back_to_google_leaves_the_link_empty(monkeypatch):
    monkeypatch.setattr(links, "_sleep", lambda seconds: None)
    answers = {
        "/url?sa=t&source=web&url=AAA&ved=1": (200, "", interstitial("AAA")),
        "/url?sa=t&source=web&url=BBB&ved=2": (302, "https://www.google.com/goto?url=BBB", ""),
        # The held link asked again only names Google's own redirect once more.
        "/goto?url=AAA": (302, "https://www.google.com/goto?url=AAA", ""),
    }
    resolver = LinkResolver(None, relay_factory=None)
    results = [
        {"link": None, "redirect_link": f"https://www.google.com/goto?url={t}"}
        for t in ("AAA", "BBB")
    ]
    resolver.fill_from_pings(results, PAGE, lambda path, timeout: answers[path])
    # Left for the /goto resolver rather than counted as resolved.
    assert [r["link"] for r in results] == [None, None]
    assert resolver.resolved == 0 and resolver.enabled


def test_ping_paths_map_each_goto_href_to_its_anchors_ping():
    assert ping_paths(PAGE) == {
        "/goto?url=AAA": "/url?sa=t&source=web&url=AAA&ved=1",
        "/goto?url=BBB": "/url?sa=t&source=web&url=BBB&ved=2",
    }


def test_pings_resolve_every_result_of_a_page_on_its_exit():
    asked = []

    def get(path, timeout):
        asked.append(path)
        token = path.split("url=")[1].split("&")[0]
        return 200, "", ping_answer(f"https://{token.lower()}.example/page")

    resolver = LinkResolver(None, relay_factory=None)
    results = [
        {"link": None, "redirect_link": "https://www.google.com/goto?url=AAA"},
        {"link": "https://kept.example/", "redirect_link": "https://www.google.com/goto?url=BBB"},
        {"link": None, "redirect_link": "https://www.google.com/goto?url=BBB"},
        {"link": None, "redirect_link": "https://www.google.com/goto?url=CCC"},
    ]
    resolver.fill_from_pings(results, PAGE, get)
    assert [r["link"] for r in results] == [
        "https://aaa.example/page",
        "https://kept.example/",
        "https://bbb.example/page",
        None,  # no ping on its anchor: left for the /goto resolver
    ]
    assert len(asked) == 2 and resolver.resolved == 2 and resolver.enabled


def test_a_refused_ping_ends_asking_for_that_page_only():
    asked = []

    def get(path, timeout):
        asked.append(path)
        return 302, "https://www.google.com/sorry/index?continue=x", ""

    resolver = LinkResolver(None, relay_factory=None)
    results = [
        {"link": None, "redirect_link": f"https://www.google.com/goto?url={t}"}
        for t in ("AAA", "BBB")
    ]
    resolver.fill_from_pings(results, PAGE, get)
    # Left for the /goto resolver, which tries them next and counts them.
    assert len(asked) == 1 and resolver.failed == 0 and resolver.enabled
    assert [r["link"] for r in results] == [None, None]


def test_an_error_on_a_ping_stops_asking_for_that_page():
    asked = []

    def get(path, timeout):
        asked.append(path)
        return None

    resolver = LinkResolver(None, relay_factory=None)
    results = [
        {"link": None, "redirect_link": f"https://www.google.com/goto?url={t}"}
        for t in ("AAA", "BBB")
    ]
    resolver.fill_from_pings(results, PAGE, get)
    assert len(asked) == 1


def test_a_miss_is_counted_once_when_no_goto_resolver_follows():
    resolver = LinkResolver(None, relay_factory=None)
    resolver.enabled = False
    results = [{"link": None, "redirect_link": "https://www.google.com/goto?url=AAA"}]
    resolver.fill_from_pings(
        results, PAGE, lambda path, timeout: (200, "", "<html>Loading...</html>")
    )
    resolver.fill(results)
    assert resolver.failed == 1 and resolver.resolved == 0


def test_a_ping_lookup_that_raises_still_leaves_the_rest_to_goto():
    class Client:
        def get(self, path, timeout):
            raise RuntimeError("broken")

        def close(self):
            pass

    resolver = LinkResolver(None, relay_factory=None)
    filled = []
    resolver.fill = lambda results: filled.append(len(results))
    record = {
        "organic_results": [{"link": None, "redirect_link": "https://www.google.com/goto?url=AAA"}],
        "search_metadata": {},
    }
    _fill_links(resolver, record, PAGE, Client())
    assert filled == [1]


def test_a_record_from_http_is_resolved_on_its_exit_and_the_client_closed():
    class Client:
        closed = False

        def get(self, path, timeout):
            return 200, "", ping_answer("https://aaa.example/")

        def close(self):
            Client.closed = True

    resolver = LinkResolver(None, relay_factory=None)
    resolver.enabled = False  # the /goto resolver has nothing to add here
    record = {
        "organic_results": [{"link": None, "redirect_link": "https://www.google.com/goto?url=AAA"}],
        "search_metadata": {},
    }
    _fill_links(resolver, record, PAGE, Client())
    assert record["organic_results"][0]["link"] == "https://aaa.example/" and Client.closed


def test_each_ping_gets_what_is_left_of_the_page_budget():
    timeouts = []

    def get(path, timeout):
        timeouts.append(timeout)
        return 200, "", ping_answer("https://aaa.example/")

    resolver = LinkResolver(None, relay_factory=None)
    results = [
        {"link": None, "redirect_link": f"https://www.google.com/goto?url={t}"}
        for t in ("AAA", "BBB")
    ]
    resolver.fill_from_pings(results, PAGE, get)
    assert len(timeouts) == 2 and 0 < timeouts[1] <= timeouts[0] <= PING_BUDGET_S


def test_the_first_ping_never_gets_more_than_the_budget(monkeypatch):
    # (t + 30) - t is 30.000000000000057 at this clock reading.
    monkeypatch.setattr(time, "monotonic", lambda: 500.8484746493213)
    timeouts = []

    def get(path, timeout):
        timeouts.append(timeout)
        return 200, "", ping_answer("https://aaa.example/")

    results = [{"link": None, "redirect_link": "https://www.google.com/goto?url=AAA"}]
    LinkResolver(None, relay_factory=None).fill_from_pings(results, PAGE, get)
    assert timeouts == [PING_BUDGET_S]


PING_A, PING_B = "/url?sa=t&source=web&url=AAA&ved=1", "/url?sa=t&source=web&url=BBB&ved=2"


def held_page_resolver(monkeypatch, answers):
    """A resolver over PAGE whose requests are answered from `answers` (a path to
    a list of answers, taken in turn); the waits are recorded, not slept."""
    asked, slept = [], []
    monkeypatch.setattr(links, "_sleep", slept.append)

    def get(path, timeout):
        asked.append(path)
        return answers[path].pop(0)

    results = [
        {"link": None, "redirect_link": f"https://www.google.com/goto?url={t}"}
        for t in ("AAA", "BBB")
    ]
    resolver = LinkResolver(None, relay_factory=None)
    resolver.fill_from_pings(results, PAGE, get)
    return results, asked, slept, resolver


def test_the_held_page_names_its_delay_and_the_goto_it_reloads():
    assert held_refresh(interstitial("AAA", 7)) == (7, "/goto?url=AAA")
    assert held_refresh(ping_answer("https://e.com/")) is None
    assert held_refresh("<html>Loading...</html>") is None


def test_a_held_link_is_asked_again_once_its_delay_has_passed(monkeypatch):
    results, asked, slept, resolver = held_page_resolver(
        monkeypatch,
        {
            PING_A: [(200, "", ping_answer("https://aaa.example/"))],
            PING_B: [(200, "", interstitial("BBB", 3))],
            "/goto?url=BBB": [(302, "https://bbb.example/page", "")],
        },
    )
    assert [r["link"] for r in results] == ["https://aaa.example/", "https://bbb.example/page"]
    assert asked == [PING_A, PING_B, "/goto?url=BBB"]
    assert len(slept) == 1 and 3.0 < slept[0] <= 3.5
    assert resolver.resolved == 2


def test_a_link_held_again_gets_one_more_round_and_then_is_left(monkeypatch):
    results, asked, _, _ = held_page_resolver(
        monkeypatch,
        {
            PING_A: [(200, "", interstitial("AAA", 3))],
            PING_B: [(200, "", interstitial("BBB", 3))],
            "/goto?url=AAA": [(200, "", interstitial("AAA", 5)), (302, "https://aaa.example/", "")],
            "/goto?url=BBB": [(200, "", interstitial("BBB", 5)), (200, "", interstitial("BBB", 8))],
        },
    )
    assert [r["link"] for r in results] == ["https://aaa.example/", None]
    assert asked.count("/goto?url=BBB") == 2


def test_a_hold_longer_than_worth_waiting_is_not_waited(monkeypatch):
    results, asked, slept, _ = held_page_resolver(
        monkeypatch,
        {
            PING_A: [(200, "", interstitial("AAA", links.HELD_WAIT_MAX_S + 1))],
            PING_B: [(200, "", ping_answer("https://bbb.example/"))],
        },
    )
    assert [r["link"] for r in results] == [None, "https://bbb.example/"]
    assert asked == [PING_A, PING_B] and slept == []


def test_a_refusal_while_following_held_links_ends_it_for_the_page(monkeypatch):
    results, asked, _, resolver = held_page_resolver(
        monkeypatch,
        {
            PING_A: [(200, "", interstitial("AAA", 3))],
            PING_B: [(200, "", interstitial("BBB", 4))],
            "/goto?url=AAA": [(302, "https://www.google.com/sorry/index?continue=x", "")],
        },
    )
    assert [r["link"] for r in results] == [None, None]
    assert "/goto?url=BBB" not in asked and resolver.enabled


def test_held_links_past_the_budget_are_left(monkeypatch):
    monkeypatch.setattr(links, "HELD_BUDGET_S", 2.0)
    results, asked, slept, _ = held_page_resolver(
        monkeypatch,
        {
            PING_A: [(200, "", interstitial("AAA", 3))],
            PING_B: [(200, "", interstitial("BBB", 3))],
        },
    )
    assert [r["link"] for r in results] == [None, None]
    assert asked == [PING_A, PING_B] and slept == []


def test_links_held_before_the_ping_budget_ran_out_are_still_followed(monkeypatch):
    clock = iter([0.0, 0.0, 10.0, 31.0] + [31.0] * 20)
    monkeypatch.setattr(links.time, "monotonic", lambda: next(clock))
    results, asked, _, _ = held_page_resolver(
        monkeypatch,
        {
            PING_A: [(200, "", interstitial("AAA", 3))],
            "/goto?url=AAA": [(302, "https://aaa.example/", "")],
        },
    )
    # BBB's ping is past the page's ping budget; AAA, held before it, is still asked again.
    assert [r["link"] for r in results] == ["https://aaa.example/", None]
    assert asked == [PING_A, "/goto?url=AAA"]


def test_the_resolvers_own_requests_stay_within_parallel_across_pages(monkeypatch):
    import threading as _threading

    in_flight, peak, lock = [0], [0], _threading.Lock()
    release = _threading.Event()

    class Opener:
        def open(self, request, timeout):
            with lock:
                in_flight[0] += 1
                peak[0] = max(peak[0], in_flight[0])
            release.wait(2)
            with lock:
                in_flight[0] -= 1

            class Response:
                headers = {"Location": "https://dest.example/"}

                def read(self):
                    return b""

            return Response()

    resolver = LinkResolver(None, relay_factory=None)
    resolver._opener = Opener()
    pages = [
        [
            {"link": None, "redirect_link": f"https://www.google.com/goto?url=P{p}R{r}"}
            for r in range(4)
        ]
        for p in range(3)
    ]
    threads = [_threading.Thread(target=resolver.fill, args=(page,)) for page in pages]
    for t in threads:
        t.start()
    _threading.Timer(0.5, release.set).start()
    for t in threads:
        t.join(5)
    assert peak[0] <= links.PARALLEL
    assert all(r["link"] == "https://dest.example/" for page in pages for r in page)
