"""The /goto resolver against a local server that answers like Google."""

import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from google_browser_scraper.links import LinkResolver, ping_destination, ping_paths
from google_browser_scraper.scraper import _fill_links


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):  # noqa: N802 - the stdlib's name
        if "sorry" in self.path:
            target = "https://www.google.com/sorry/index?continue=x"
        elif "none" in self.path:
            target = None
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
    script_only = "<script>var redirectUrl='https://e.com/x\x3dy';</script>"
    assert ping_destination(script_only) == "https://e.com/x=y"
    assert ping_destination("<html>Loading...</html>") is None


def test_ping_paths_map_each_goto_href_to_its_anchors_ping():
    assert ping_paths(PAGE) == {
        "/goto?url=AAA": "/url?sa=t&source=web&url=AAA&ved=1",
        "/goto?url=BBB": "/url?sa=t&source=web&url=BBB&ved=2",
    }


def test_pings_resolve_every_result_of_a_page_on_its_exit():
    asked = []

    def get(path):
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

    def get(path):
        asked.append(path)
        return 302, "https://www.google.com/sorry/index?continue=x", ""

    resolver = LinkResolver(None, relay_factory=None)
    results = [
        {"link": None, "redirect_link": f"https://www.google.com/goto?url={t}"}
        for t in ("AAA", "BBB")
    ]
    resolver.fill_from_pings(results, PAGE, get)
    assert len(asked) == 1 and resolver.failed == 1 and resolver.enabled
    assert [r["link"] for r in results] == [None, None]


def test_a_record_from_http_is_resolved_on_its_exit_and_the_client_closed():
    class Client:
        closed = False

        def get(self, path):
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
