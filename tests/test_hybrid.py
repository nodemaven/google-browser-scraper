"""The hybrid: results pages over HTTP between the browser's checks, with a fake
browser and a fake HTTP client."""

import json

import pages

from google_browser_scraper import hybrid, quality
from google_browser_scraper.browser import Fetched
from google_browser_scraper.hybrid import Headers, HybridSession

SEARCH = "https://www.google.com/search?q=x"
SERVED = Fetched(SEARCH, 200, pages.results_page())
REQUEST = {
    "headers": [
        (":method", "GET"),
        (":authority", "www.google.com"),
        ("sec-ch-ua", '"Chromium";v="151"'),
        ("user-agent", "Mozilla/5.0 Chrome/151"),
        ("cookie", "OLD=1"),
        ("accept-language", "en-US,en;q=0.9"),
    ],
    "response": [],
}


class Response:
    def __init__(self, html, status=200, url=SEARCH, set_cookie=()):
        self.text, self.status_code, self.url = html, status, url
        self.headers = Cookies(set_cookie)


class Cookies(dict):
    def __init__(self, values):
        super().__init__()
        self.values_ = list(values)

    def get_list(self, name):
        return list(self.values_) if name == "set-cookie" else []


class Client:
    def __init__(self, script):
        self.script, self.sent = list(script), []

    def get(self, url, headers):
        self.sent.append((url, headers))
        item = self.script.pop(0)
        if isinstance(item, Exception):
            raise item
        return item

    def close(self):
        pass


class Inner:
    name = "patchright"
    hl, gl = "en", None
    profile_dir = "profile"

    def __init__(self, script=()):
        self.script = list(script)
        self.searched, self.pushed, self.opened = [], [], []
        self.netlog = None
        self.netlogs = []
        self.cookie = "SID=browser"
        self.cookie_after_search = None

    def __enter__(self):
        self.netlogs.append(self.netlog)
        return self

    def resume(self):
        self.netlogs.append(self.netlog)

    def suspend(self):
        return self.profile_dir

    def close(self):
        pass

    def search(self, query, direct=False, **locale):
        self.searched.append(query)
        # Passing Google's check leaves the browser with new cookies.
        self.cookie = self.cookie_after_search or self.cookie
        return self.script.pop(0) if self.script else SERVED

    def open_results(self, query, start=0):
        self.opened.append((query, start))
        return SERVED

    def next_page(self):
        return SERVED

    def google_cookies(self):
        return self.cookie

    def set_google_cookies(self, header, deleted=()):
        self.pushed.append(header)


def session(client, headers=None, inner=None, opened=True):
    """A hybrid session; with `opened`, as if the browser had already been
    served on its exit, which the HTTP side waits for."""
    if headers is None:
        headers = Headers()
        headers.request = REQUEST
    s = HybridSession(inner or Inner(), "http://127.0.0.1:1", headers)
    s._http = client
    s.__enter__()
    s._opened_served = opened
    return s, headers


def test_replay_headers_keep_chromiums_order_and_swap_the_cookie():
    out = hybrid.replay_headers(REQUEST, "NEW=2")
    assert [n for n, _ in out] == ["sec-ch-ua", "user-agent", "cookie", "accept-language"]
    assert dict(out)["cookie"] == "NEW=2"


def test_fold_cookies_updates_adds_and_drops():
    cookie = hybrid.fold_cookies("A=1; B=2", ["A=9; Path=/", "C=3", "B=x; Max-Age=0"])
    assert cookie == "A=9; C=3"


def test_read_netlog_finds_the_search_request_and_its_response(tmp_path):
    types = {"HTTP2_SESSION_SEND_HEADERS": 1, "HTTP2_SESSION_RECV_HEADERS": 2}
    log = {
        "constants": {"logEventTypes": types},
        "events": [
            {
                "type": 1,
                "source": {"id": 7},
                "params": {
                    "stream_id": 1,
                    "headers": [
                        ":authority: www.google.com",
                        ":path: /search?q=a",
                        "user-agent: UA",
                        "cookie: S=1",
                    ],
                },
            },
            {
                "type": 1,
                "source": {"id": 7},
                "params": {
                    "stream_id": 3,
                    "headers": [":authority: www.google.com", ":path: /complete/search?q=a"],
                },
            },
            {
                "type": 2,
                "source": {"id": 7},
                "params": {"stream_id": 1, "headers": [":status: 200", "set-cookie: N=2"]},
            },
        ],
    }
    path = tmp_path / "netlog.json"
    # Chromium leaves the file open-ended when it is cut short.
    path.write_text(json.dumps(log)[:-2] + ",")
    found = hybrid.read_netlog(str(path))
    assert len(found) == 1
    assert ("user-agent", "UA") in found[0]["headers"]
    assert ("set-cookie", "N=2") in found[0]["response"]


def test_an_exits_first_query_goes_through_the_browser():
    client = Client([Response(SERVED.html)])
    s, _ = session(client, opened=False)
    s.search("a b", direct=True)
    s.search("c d", direct=True)
    assert s.inner.searched == ["a b"] and len(client.sent) == 1


def test_without_headers_every_query_goes_through_the_browser():
    client = Client([])
    s, _ = session(client, headers=Headers())
    s.search("q", direct=True)
    assert client.sent == [] and s.inner.searched == ["q"]


def test_served_pages_come_over_http_with_the_browsers_cookies():
    client = Client([Response(SERVED.html, set_cookie=["NID=2"])] * 2)
    s, _ = session(client)
    first = s.search("best running shoes", direct=True)
    s.search("best trail shoes", direct=True)
    assert first.timings["http"] >= 0 and s.inner.searched == []
    assert dict(client.sent[0][1])["cookie"] == "SID=browser"
    assert dict(client.sent[1][1])["cookie"] == "SID=browser; NID=2"
    assert s.http_pages == 2


def test_a_check_goes_to_the_browser_with_the_http_cookies_and_back():
    client = Client(
        [
            Response(SERVED.html, set_cookie=["NID=2"]),
            Response(pages.NO_JS_PAGE),
            Response(SERVED.html),
        ]
    )
    s, _ = session(client)
    s.inner.cookie_after_search = "SID=fresh"
    s.search("a b", direct=True)
    passed = s.search("c d", direct=True)
    assert s.inner.searched == ["c d"] and passed is SERVED
    assert s.inner.pushed == ["SID=browser; NID=2"]
    s.search("e f", direct=True)
    assert dict(client.sent[-1][1])["cookie"] == "SID=fresh"


def test_typed_queries_stay_in_the_browser():
    client = Client([])
    s, _ = session(client)
    s.search("q", direct=False)
    assert client.sent == [] and s.inner.searched == ["q"]


def test_no_results_is_an_answer_but_an_empty_ordinary_page_is_not():
    empty_ok = '<html><body><div id="search"><div id="rso"></div></div></body></html>'
    client = Client([Response(pages.NO_RESULTS_PAGE), Response(empty_ok)])
    s, _ = session(client)
    assert s.search("zzqx nothing", direct=True).timings.get("http") is not None
    s.search("a b", direct=True)
    assert s.inner.searched == ["a b"]


def test_short_series_twice_turn_the_exit_to_the_browser():
    client = Client([Response(pages.NO_JS_PAGE), Response(pages.NO_JS_PAGE)])
    s, headers = session(client)
    s.search("a", direct=True)
    s.search("b", direct=True)
    s.search("c", direct=True)
    assert len(client.sent) == 2 and s.inner.searched == ["a", "b", "c"]
    assert headers.streak == 1 and not headers.disabled


def test_a_refusal_is_not_held_against_the_http_side():
    client = Client([Response(pages.SORRY_PAGE, 429, pages.SORRY_URL)] * 3)
    s, headers = session(client)
    for q in "abc":
        s.search(q, direct=True)
    assert len(client.sent) == 3 and headers.streak == 0


def test_three_exits_in_a_row_stepping_back_turn_the_run_to_the_browser():
    headers = Headers()
    assert [headers.stepped_back() for _ in range(3)] == [False, False, True]
    headers = Headers()
    headers.stepped_back(), headers.stepped_back()
    headers.worked()
    assert headers.stepped_back() is False


def test_next_page_follows_an_http_page_over_http():
    client = Client([Response(SERVED.html)] * 2)
    s, _ = session(client)
    s.search("a b", direct=True)
    s.next_page()
    assert client.sent[1][0].endswith("&start=10")


def test_next_page_is_none_when_google_has_no_further_page():
    client = Client([Response(SERVED.html.replace('id="pnnext"', 'id="other"'))])
    s, _ = session(client)
    s.search("a b", direct=True)
    assert s.next_page() is None


def test_one_session_records_a_netlog_and_reads_it_on_suspend(monkeypatch):
    headers = Headers()
    monkeypatch.setattr(hybrid, "read_netlog", lambda path: [REQUEST])
    s, _ = session(Client([]), headers=headers)
    other, _ = session(Client([]), headers=headers)
    assert s.inner.netlogs[0] and other.inner.netlogs == [None]
    assert not s.needs_restart()
    s.search("q", direct=True)
    assert s.needs_restart()
    s.suspend()
    s.resume()
    assert headers.request is REQUEST and s.inner.netlogs[-1] is None
    assert not s.needs_restart()


def test_poisoned_needs_a_streak_of_unrelated_pages():
    junk = ["Under Armour Official Store", "UNDER Definition & Meaning", "Apple"]
    assert quality.unrelated("symptoms of strep throat", junk) is True
    assert quality.unrelated("symptoms of strep throat", ["Strep throat - Symptoms"] * 3) is False
    assert quality.unrelated("costco", junk) is None


def test_hl_and_gl_of_one_query_go_into_its_http_url():
    client = Client([Response(SERVED.html)] * 2)
    s, _ = session(client)
    s.search("a b", direct=True, hl="de", gl="at")
    s.search("c d", direct=True)
    assert "hl=de" in client.sent[0][0] and "gl=at" in client.sent[0][0]
    assert "hl=en" in client.sent[1][0] and "gl=" not in client.sent[1][0]


def test_curl_presents_the_newest_chrome_it_can_that_is_not_newer_than_the_browser(monkeypatch):
    import enum
    import sys
    import types

    targets = {"a": "chrome146", "b": "chrome150", "c": "chrome152", "d": "edge99"}
    Browser = enum.Enum("Browser", targets)
    fake = types.ModuleType("curl_cffi.requests")
    fake.BrowserType = Browser
    monkeypatch.setitem(sys.modules, "curl_cffi.requests", fake)

    def request(ua):
        return {"headers": [("user-agent", ua)]}

    assert hybrid.impersonate_for(request("Mozilla/5.0 Chrome/151.0.7922.34")) == "chrome150"
    assert hybrid.impersonate_for(request("Mozilla/5.0 Chrome/156.0.8078.4")) == "chrome152"
    assert hybrid.impersonate_for(request("Mozilla/5.0 Chrome/140.0.1.1")) == "chrome146"
    assert hybrid.impersonate_for(None) == hybrid.IMPERSONATE


def test_any_past_expiry_deletes_a_cookie_and_is_reported():
    deleted = set()
    cookie = hybrid.fold_cookies(
        "A=1; B=2; C=3",
        ["A=x; Expires=Wed, 21 Oct 2015 07:28:00 GMT", "B=y; Max-Age=-1", "C=4; Path=/"],
        deleted,
    )
    assert cookie == "C=4" and deleted == {"A", "B"}


def test_cookies_deleted_over_http_leave_the_browser_on_the_next_check():
    client = Client(
        [Response(SERVED.html, set_cookie=["OLD=x; Max-Age=0"]), Response(pages.NO_JS_PAGE)]
    )
    inner = Inner()
    calls = []
    inner.set_google_cookies = lambda header, deleted=(): calls.append((header, deleted))
    s, _ = session(client, inner=inner)
    s.search("a b", direct=True)
    s.search("c d", direct=True)
    assert calls == [("SID=browser", ("OLD",))]


def test_a_failed_launch_releases_the_netlog_capture():
    headers = Headers()

    class Broken(Inner):
        def __enter__(self):
            raise RuntimeError("no browser")

    s = HybridSession(Broken(), "http://127.0.0.1:1", headers)
    try:
        s.__enter__()
    except RuntimeError:
        pass
    assert headers.claim_capture() is True


def test_a_query_word_must_start_a_title_word():
    assert quality.relevance("cat", ["Education policy"]) == 0.0
    assert quality.relevance("cat", ["Cat toys for kittens"]) == 1.0
