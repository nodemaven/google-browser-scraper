"""What a person sees on a first run: progress lines, one-line setup errors,
and the hosts the relay refuses so a fresh profile does not spend proxy
traffic on downloads no search needs."""

from __future__ import annotations

import io
import socket

import pytest

from serpmaven import cli, hints
from serpmaven.progress import Progress
from serpmaven.relay import Relay


def _connect(address: str, authority: str) -> str:
    host, port = address.removeprefix("http://").split(":")
    with socket.create_connection((host, int(port)), timeout=5) as sock:
        sock.sendall(f"CONNECT {authority} HTTP/1.1\r\nHost: {authority}\r\n\r\n".encode())
        return sock.recv(4096).decode("latin-1")


def _get(address: str, url: str) -> str:
    host, port = address.removeprefix("http://").split(":")
    with socket.create_connection((host, int(port)), timeout=5) as sock:
        sock.sendall(f"GET {url} HTTP/1.1\r\nHost: x\r\n\r\n".encode())
        return sock.recv(4096).decode("latin-1")


@pytest.mark.parametrize(
    "authority",
    [
        "dl.google.com:443",
        "edgedl.me.gvt1.com:443",
        "redirector.gvt1.com:443",
        "r3---sn-4g5e6nzz.gvt1.com:443",
        "volume-assets.voxmedia.com:443",
        "optimizationguide-pa.googleapis.com:443",
    ],
)
def test_downloads_no_search_needs_are_refused_locally(authority):
    relay = Relay(None)
    address = relay.start()
    try:
        head = _connect(address, authority)
    finally:
        relay.stop()
    assert head.startswith("HTTP/1.1 403")
    assert relay.blocked == 1 and relay.tunnels == 0


def test_plain_http_component_downloads_are_refused_too():
    relay = Relay(None)
    address = relay.start()
    try:
        head = _get(address, "http://edgedl.me.gvt1.com/edgedl/release2/chrome_component/x.crx3")
    finally:
        relay.stop()
    assert head.startswith("HTTP/1.1 403") and relay.blocked == 1


def test_a_suffix_entry_does_not_catch_lookalike_hosts():
    relay = Relay(None)
    assert relay.is_blocked("gvt1.com") and relay.is_blocked("A.GVT1.COM")
    assert not relay.is_blocked("notgvt1.com")
    assert not relay.is_blocked("www.google.com")
    assert not relay.is_blocked("www.theverge.com")


@pytest.mark.parametrize(
    ("message", "expected"),
    [
        (
            "BrowserType.launch_persistent_context: Executable doesn't exist at /x/chrome\n"
            "Looks like Playwright was just installed or updated. patchright install",
            hints.NO_BROWSER,
        ),
        (
            "Looks like you launched a headed browser without having a XServer running.\n"
            "Set either 'headless: true' or use 'xvfb-run <your-playwright-app>'\n"
            "Missing X server or $DISPLAY",
            hints.NO_DISPLAY,
        ),
        (
            "Host system is missing dependencies to run browsers. Please install them with:\n"
            "    sudo patchright install-deps",
            hints.NO_SYSTEM_LIBS,
        ),
        ("net::ERR_TUNNEL_CONNECTION_FAILED", None),
    ],
)
def test_setup_errors_are_recognised(message, expected):
    assert hints.explain(Exception(message)) == expected


def test_a_missing_browser_is_one_line_not_a_traceback(monkeypatch, capsys):
    class NoBrowser:
        stats = type("Stats", (), {"summary": lambda self, price: {}})()

        def run(self, queries):
            raise Exception("BrowserType.launch_persistent_context: Executable doesn't exist at x")

    monkeypatch.setattr(cli, "_build", lambda args, proxy, **kw: (NoBrowser(), ""))
    assert cli.main(["search", "q", "--no-proxy"]) == 1
    err = capsys.readouterr().err
    assert "error: the browser is not installed. Run: serpmaven install" in err
    assert "Traceback" not in err


def test_a_missing_query_file_names_the_file(tmp_path, capsys):
    missing = tmp_path / "queries.txt"
    assert cli.main(["search", "-f", str(missing), "--no-proxy"]) == 1
    err = capsys.readouterr().err
    assert err.startswith("error: query file not found:") and "queries.txt" in err


def _progress():
    out = io.StringIO()
    ticks = iter([0.0, 330.0])
    return out, Progress(out, total=2, breaker=6, clock=lambda: next(ticks))


def test_progress_explains_the_first_warm_up_once_and_counts_queries():
    out, p = _progress()
    p.on_event({"event": "warming", "pages": 6, "donor": False, "spare": False})
    p.on_event(
        {
            "event": "minted",
            "warm_pages": 6,
            "warm_delivered": 6,
            "warm_bytes": 32 * 1024 * 1024,
            "seconds": 190,
        }
    )
    p.on_event({"event": "query", "verdict": "sorry"})
    p.on_event({"event": "warming", "pages": 6, "donor": False, "spare": False})
    p.on_event({"event": "warming", "pages": 0, "donor": True, "spare": False})
    p.record(
        {
            "search_metadata": {"status": "Success", "total_time_taken": 5.06},
            "search_parameters": {"q": "best running shoes", "page": 1},
            "organic_results": [{}] * 8,
        }
    )
    p.record(
        {
            "search_metadata": {"status": "Error"},
            "search_parameters": {"q": "kimchi", "page": 1},
            "error": "captcha on 3 proxy IPs in a row",
        }
    )
    lines = out.getvalue().splitlines()
    assert lines[0].startswith("proxy IP 1: warming up the browser on 6 ordinary pages")
    assert lines[1] == "  warmed up in 3 min 10 s, 32.0 MB"
    assert "Google refused this proxy IP (sorry)" in lines[2] and "6 in a row" in lines[2]
    assert lines[3] == "proxy IP 2: warming up (a few minutes)"
    assert lines[4] == "proxy IP 3: starting from cookies Google already accepts"
    assert lines[5] == "[1/2] best running shoes: 8 results, 5.1 s"
    assert lines[6] == "[2/2] kimchi: failed (captcha on 3 proxy IPs in a row)"


def test_progress_summary_separates_the_warm_up_from_the_page_cost():
    out, p = _progress()
    p.summary(
        {
            "identities": 1,
            "exits_refused": 0,
            "pages_served": 3,
            "queries_failed": 0,
            "traffic_mb": 34.44,
            "warmup_mb": 32.44,
        }
    )
    lines = out.getvalue().splitlines()
    assert lines[0] == "done in 5 min 30 s: 3 pages served, 0 queries failed"
    assert lines[1] == (
        "traffic: 34.4 MB in all, of which 32.4 MB warming up proxy IPs; "
        "0.7 MB a page after the warm-up"
    )
    assert lines[2] == "proxy IPs: 1 used, 0 refused by Google"


def test_verbose_keeps_the_machine_readable_summary(monkeypatch, capsys):
    class Quiet:
        stats = type(
            "Stats", (), {"summary": lambda self, price: {"pages_served": 0, "identities": 1}}
        )()

        def run(self, queries):
            return iter(())

    monkeypatch.setattr(cli, "_build", lambda args, proxy, **kw: (Quiet(), ""))
    assert cli.main(["search", "q", "--no-proxy", "-v"]) == 0
    assert 'summary: {"pages_served": 0, "identities": 1}' in capsys.readouterr().err
    assert cli.main(["search", "q", "--no-proxy"]) == 0
    err = capsys.readouterr().err
    assert "summary: {" not in err and err.startswith("done in ")


def test_no_summary_when_nothing_started():
    out, p = _progress()
    p.summary({"identities": 0, "pages_served": 0, "queries_failed": 0, "traffic_mb": 0.0})
    assert out.getvalue() == ""


def test_a_dropped_proxy_ip_is_explained_in_a_few_words():
    out, p = _progress()
    p.on_event(
        {
            "event": "retired",
            "reason": "error: Error: Page.goto: net::ERR_SSL_PROTOCOL_ERROR at https://x/search",
        }
    )
    p.on_event({"event": "retired", "reason": "run finished"})
    assert out.getvalue().splitlines() == [
        "  proxy IP dropped: the connection through it failed (ERR_SSL_PROTOCOL_ERROR); "
        "trying a new one"
    ]


def test_dropped_proxy_ips_say_why_and_routine_ends_stay_quiet():
    out, p = _progress()
    for reason in (
        "unreachable",
        "poisoned",
        "no_js",
        "sorry",
        "proxy: HTTP/1.1 407 Proxy Authentication Required",
        "run finished",
        "query cap reached",
        "spare unused",
        "http side off",
    ):
        p.on_event({"event": "retired", "reason": reason})
    assert out.getvalue().splitlines() == [
        "  proxy IP dropped: its warm-up pages did not load; trying a new one",
        "  proxy IP dropped: Google answered with results for other queries; trying a new one",
        "  proxy IP dropped: Google's answer was not a results page (no_js); trying a new one",
    ]


def test_a_warm_up_that_reached_nothing_is_not_reported_as_warmed_up():
    out, p = _progress()
    p.on_event({"event": "minted", "warm_pages": 6, "warm_delivered": 0, "seconds": 182})
    p.on_event({"event": "minted", "warm_pages": 0, "donor": 0, "seconds": 3})
    assert out.getvalue() == ""


def test_concurrent_warm_ups_get_distinct_numbers_and_one_explanation():
    import threading

    out = io.StringIO()
    p = Progress(out, total=1)
    threads = [
        threading.Thread(
            target=p.on_event, args=({"event": "warming", "pages": 6, "donor": False},)
        )
        for _ in range(20)
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    lines = out.getvalue().splitlines()
    numbers = sorted(int(line.split()[2].rstrip(":")) for line in lines)
    assert numbers == list(range(1, 21))
    assert sum("ordinary pages" in line for line in lines) == 1


def test_a_pool_worker_setup_error_is_one_line(monkeypatch, capsys):
    from serpmaven.scraper import ExitsRefused

    class FailedPool:
        stats = type("Stats", (), {"summary": lambda self, price: {}})()

        def __init__(self, make, workers):
            pass

        def run(self, queries):
            raise ExitsRefused(
                "worker 0 failed: BrowserType.launch_persistent_context: "
                "Executable doesn't exist at /x/chrome"
            )

    monkeypatch.setattr(cli, "Pool", FailedPool)
    assert cli.main(["search", "q", "--no-proxy", "--workers", "2"]) == 3
    err = capsys.readouterr().err
    assert "error: the browser is not installed" in err and "stopped:" not in err


def _forwarded_head(request: str) -> str:
    seen = []
    server = socket.socket()
    server.bind(("127.0.0.1", 0))
    server.listen(1)
    port = server.getsockname()[1]

    def serve():
        conn, _ = server.accept()
        seen.append(conn.recv(4096).decode("latin-1"))
        conn.sendall(b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\n\r\nok")
        conn.close()

    import threading

    threading.Thread(target=serve, daemon=True).start()
    relay = Relay(None)
    address = relay.start()
    try:
        host, rport = address.removeprefix("http://").split(":")
        with socket.create_connection((host, int(rport)), timeout=5) as sock:
            sock.sendall(request.format(port=port).encode())
            while sock.recv(4096):
                pass
    finally:
        relay.stop()
        server.close()
    return seen[0].lower()


def test_an_upgrade_request_keeps_its_connection_header():
    head = _forwarded_head(
        "GET http://127.0.0.1:{port}/ws HTTP/1.1\r\nHost: 127.0.0.1\r\n"
        "Connection: Upgrade\r\nUpgrade: websocket\r\n\r\n"
    )
    assert "connection: upgrade" in head and "connection: close" not in head


def test_plain_http_is_one_request_per_connection():
    seen = []
    server = socket.socket()
    server.bind(("127.0.0.1", 0))
    server.listen(1)
    port = server.getsockname()[1]

    def serve():
        conn, _ = server.accept()
        seen.append(conn.recv(4096).decode("latin-1"))
        conn.sendall(b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\n\r\nok")
        conn.close()

    import threading

    t = threading.Thread(target=serve, daemon=True)
    t.start()
    relay = Relay(None)
    address = relay.start()
    try:
        host, rport = address.removeprefix("http://").split(":")
        with socket.create_connection((host, int(rport)), timeout=5) as sock:
            sock.sendall(
                f"GET http://127.0.0.1:{port}/x HTTP/1.1\r\nHost: 127.0.0.1\r\n"
                "Connection: keep-alive\r\nKeep-Alive: timeout=30\r\n\r\n".encode()
            )
            answer = b""
            while chunk := sock.recv(4096):
                answer += chunk  # ends when the relay closes the connection
    finally:
        relay.stop()
        server.close()
    assert answer.endswith(b"ok")
    head = seen[0].lower()
    assert "connection: close" in head and "keep-alive" not in head


class _ReloadingPage:
    """A page whose first goto is cut short by Google's check page loading
    `then` from its own script."""

    def __init__(self, then):
        self.then = then
        self.url = "about:blank"
        self.loads = []

    def goto(self, url, **kwargs):
        raise Exception(
            f'Page.goto: Navigation to "{url}" is interrupted by another '
            f'navigation to "{self.then}"'
        )

    def wait_for_load_state(self, state, **kwargs):
        self.loads.append(state)
        self.url = self.then

    def query_selector_all(self, selector):
        return []

    def wait_for_selector(self, *args, **kwargs):
        return None

    def content(self):
        return "<html>results</html>"


def test_google_reloading_its_results_page_is_not_a_failed_search():
    from serpmaven.browser import BrowserSession

    session = BrowserSession(None)
    then = "https://www.google.com/search?q=best+running+shoes&hl=en"
    session.page = _ReloadingPage(then)
    fetched = session.search("best running shoes", direct=True)
    assert session.page.loads == ["domcontentloaded"]
    assert fetched.url == then and fetched.status is None
    assert fetched.html == "<html>results</html>"


def test_a_navigation_taken_over_by_another_page_still_fails():
    from serpmaven.browser import BrowserSession

    session = BrowserSession(None)
    session.page = _ReloadingPage("https://trends.google.com/trends/?hl=en")
    with pytest.raises(Exception, match="interrupted"):
        session.search("best running shoes", direct=True)
