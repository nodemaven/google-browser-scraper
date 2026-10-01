"""The /goto resolver against a local server that answers like Google."""

import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from google_search_scraper.links import LinkResolver


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
