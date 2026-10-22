"""The HTTP API and the MCP server, over a fake scraper on the worker thread."""

import io
import json
import os
import subprocess
import sys
import threading
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

import pytest

from google_browser_scraper.mcp import McpServer, compact
from google_browser_scraper.scraper import ExitsRefused, Settings, Stats
from google_browser_scraper.server import Worker, make_handler


def record(query, page=1, error=None):
    r = {
        "search_metadata": {"status": "Error" if error else "Success"},
        "search_parameters": {"q": query, "page": page},
    }
    if error:
        r["error"] = error
    else:
        r["organic_results"] = [
            {
                "position": 1,
                "title": "T",
                "link": "https://a.example",
                "snippet": "S",
                "displayed_link": "a.example",
            }
        ]
        r["related_questions"] = [{"question": "Q?"}]
        r["related_searches"] = [{"query": "R", "link": "https://www.google.com/search?q=R"}]
    return r


class FakeScraper:
    def __init__(self, refuse=False):
        self.settings = Settings(gap_seconds=(0, 0), dwell_seconds=(0, 0))
        self.stats = Stats()
        self.refuse = refuse
        self.threads, self.calls, self.closed, self.resets = set(), [], False, 0
        self.settle_flags, self.settled = [], 0

    def search(self, query, settle=True):
        self.threads.add(threading.get_ident())
        self.calls.append((query, self.settings.pages))
        self.settle_flags.append(settle)
        if self.refuse:
            raise ExitsRefused("6 exits in a row")
        return [record(query, p) for p in range(1, self.settings.pages + 1)]

    def prewarm(self):
        self.threads.add(threading.get_ident())

    def reset_breaker(self):
        self.resets += 1

    def settle(self):
        self.threads.add(threading.get_ident())
        self.settled += 1

    def close(self):
        self.closed = True


@pytest.fixture
def worker():
    fake = FakeScraper()
    w = Worker(lambda: fake, prewarm=True, log=lambda m: None).start()
    assert w.ready.wait(5)
    w.fake = fake
    yield w
    w.stop()


def test_worker_runs_every_job_on_one_thread(worker):
    worker.call(lambda s: s.search("a"))
    threading.Thread(target=lambda: worker.call(lambda s: s.search("b"))).start()
    worker.call(lambda s: s.search("c"))
    assert len(worker.fake.threads) == 1
    worker.stop()
    assert worker.fake.closed


@pytest.fixture
def api(worker, monkeypatch):
    def start(api_key=None):
        server = ThreadingHTTPServer(
            ("127.0.0.1", 0), make_handler(worker, api_key=api_key, max_pages=3)
        )
        threading.Thread(target=server.serve_forever, daemon=True).start()
        return server, f"http://127.0.0.1:{server.server_port}"

    servers = []

    def factory(**kw):
        server, base = start(**kw)
        servers.append(server)
        return base

    yield factory
    for s in servers:
        s.shutdown()


def get(url, headers=None):
    request = urllib.request.Request(url, headers=headers or {})
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            return response.status, json.loads(response.read())
    except urllib.error.HTTPError as err:
        return err.code, json.loads(err.read())


def test_one_page_returns_the_record_itself(api, worker):
    status, body = get(api() + "/search.json?q=running+shoes")
    assert status == 200
    assert body["search_parameters"] == {"q": "running shoes", "page": 1}
    assert worker.fake.calls == [("running shoes", 1)]


def test_the_upkeep_is_left_until_the_answer_is_out(api, worker):
    assert get(api() + "/search.json?q=x")[0] == 200
    worker.call(lambda s: None)  # jobs run in order: the settle job is done by now
    assert worker.fake.settle_flags == [False]
    assert worker.fake.settled == 1
    assert len(worker.fake.threads) == 1


def test_no_upkeep_is_queued_for_a_failed_search(monkeypatch):
    fake = FakeScraper(refuse=True)
    worker = Worker(lambda: fake, log=lambda m: None).start()
    server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(worker, api_key=None, max_pages=3))
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        assert get(f"http://127.0.0.1:{server.server_port}/search.json?q=x")[0] == 503
        worker.call(lambda s: None)
    finally:
        server.shutdown()
        worker.stop()
    assert fake.settled == 0


def test_a_failed_background_job_is_logged():
    logs = []
    worker = Worker(lambda: FakeScraper(), log=logs.append).start()
    try:
        worker.after(lambda s: 1 / 0)
        worker.call(lambda s: None)
    finally:
        worker.stop()
    assert any("background job failed: ZeroDivisionError" in m for m in logs)


def test_several_pages_return_an_array_and_restore_the_setting(api, worker):
    status, body = get(api() + "/search?q=x&pages=2")
    assert status == 200 and [r["search_parameters"]["page"] for r in body] == [1, 2]
    assert worker.fake.settings.pages == 1


@pytest.mark.parametrize(
    "query, code",
    [
        ("", 400),
        ("q=x&pages=9", 400),
        ("q=x&pages=two", 400),
    ],
)
def test_bad_requests(api, query, code):
    assert get(api() + "/search.json?" + query)[0] == code


def test_unknown_path_and_health(api):
    base = api()
    assert get(base + "/nope")[0] == 404
    status, body = get(base + "/health")
    assert status == 200 and body["status"] == "ok" and "identities" in body["stats"]


def test_api_key_is_enforced(api):
    base = api(api_key="s3cret")
    assert get(base + "/search.json?q=x")[0] == 401
    assert get(base + "/search.json?q=x&api_key=wrong")[0] == 401
    assert get(base + "/search.json?q=x&api_key=s3cret")[0] == 200
    assert get(base + "/search.json?q=x", {"Authorization": "Bearer s3cret"})[0] == 200


def test_tripped_breaker_is_a_503_and_is_reset_for_the_next_request(monkeypatch):
    fake = FakeScraper(refuse=True)
    worker = Worker(lambda: fake, log=lambda m: None).start()
    server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(worker, api_key=None, max_pages=3))
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        status, body = get(f"http://127.0.0.1:{server.server_port}/search.json?q=x")
    finally:
        server.shutdown()
        worker.stop()
    assert status == 503 and "6 exits" in body["error"]
    assert fake.resets == 1


def test_serve_refuses_a_public_address_without_a_key(monkeypatch):
    from google_browser_scraper import server

    monkeypatch.delenv("GBS_API_KEY", raising=False)
    with pytest.raises(ValueError, match="GBS_API_KEY"):
        server.serve(None, host="0.0.0.0", port=0)


# -- MCP ----------------------------------------------------------------------


def rpc(server, method, params=None, ident=1):
    message = {"jsonrpc": "2.0", "method": method, "params": params or {}}
    if ident is not None:
        message["id"] = ident
    return server.handle(message)


def test_mcp_handshake_and_tool_listing(worker):
    server = McpServer(worker, stdout=io.StringIO())
    init = rpc(
        server,
        "initialize",
        {
            "protocolVersion": "2025-03-26",
            "capabilities": {},
            "clientInfo": {"name": "t", "version": "0"},
        },
    )
    assert init["result"]["protocolVersion"] == "2025-03-26"
    assert init["result"]["capabilities"] == {"tools": {"listChanged": False}}
    assert rpc(server, "notifications/initialized", ident=None) is None
    tools = rpc(server, "tools/list")["result"]["tools"]
    assert [t["name"] for t in tools] == ["google_search"]
    assert tools[0]["inputSchema"]["required"] == ["query"]
    assert rpc(server, "ping")["result"] == {}


def test_mcp_unknown_version_gets_the_newest_supported(worker):
    server = McpServer(worker, stdout=io.StringIO())
    reply = rpc(server, "initialize", {"protocolVersion": "1999-01-01"})
    assert reply["result"]["protocolVersion"] == "2025-06-18"


def test_mcp_tool_call_returns_compact_results(worker):
    server = McpServer(worker, stdout=io.StringIO())
    reply = rpc(
        server, "tools/call", {"name": "google_search", "arguments": {"query": "shoes", "pages": 2}}
    )
    result = reply["result"]
    assert result["isError"] is False
    body = json.loads(result["content"][0]["text"])
    assert body["results"][0] == {
        "position": 1,
        "title": "T",
        "link": "https://a.example",
        "snippet": "S",
    }
    assert len(body["results"]) == 2 and body["related_questions"] == ["Q?", "Q?"]
    assert worker.fake.calls[-1] == ("shoes", 2)
    worker.call(lambda s: None)
    assert worker.fake.settle_flags == [False] and worker.fake.settled == 1


def test_mcp_errors(worker):
    server = McpServer(worker, stdout=io.StringIO())
    assert rpc(server, "nope")["error"]["code"] == -32601
    assert rpc(server, "tools/call", {"name": "other"})["error"]["code"] == -32602
    empty = rpc(server, "tools/call", {"name": "google_search", "arguments": {}})
    assert empty["result"]["isError"] is True


def test_mcp_stdio_loop_writes_one_line_per_reply(worker):
    out = io.StringIO()
    stdin = io.StringIO(
        json.dumps({"jsonrpc": "2.0", "id": 1, "method": "ping"})
        + "\n"
        + json.dumps({"jsonrpc": "2.0", "method": "notifications/initialized"})
        + "\n"
        + "not json\n"
    )
    McpServer(worker, stdout=out).serve(stdin)
    lines = [json.loads(line) for line in out.getvalue().splitlines()]
    assert lines[0] == {"jsonrpc": "2.0", "id": 1, "result": {}}
    assert lines[1]["error"]["code"] == -32700
    assert len(lines) == 2


def test_compact_reports_errors_without_results():
    body = compact([record("x", error="refused on 3 exits: captcha")])
    assert body["results"] == [] and body["errors"] == ["refused on 3 exits: captcha"]


class Crash(BaseException):
    """Not an Exception, so nothing between the job loop and the thread catches it."""


def test_start_raises_what_stopped_the_scraper_from_being_built():
    def missing_credentials():
        raise RuntimeError("set NODEMAVEN_LOGIN and NODEMAVEN_PASSWORD in the environment")

    with pytest.raises(RuntimeError, match="NODEMAVEN_LOGIN"):
        Worker(missing_credentials, log=lambda m: None).start()


def test_a_worker_that_died_fails_calls_at_once():
    class DyingScraper(FakeScraper):
        def prewarm(self):
            raise Crash("the browser went away")

    fake = DyingScraper()
    worker = Worker(lambda: fake, prewarm=True, log=lambda m: None).start()
    with pytest.raises(RuntimeError, match="not running"):
        worker.call(lambda s: s.search("a"), timeout=10)
    with pytest.raises(RuntimeError, match="not running"):
        worker.call(lambda s: s.search("b"), timeout=10)
    assert fake.closed and fake.calls == []
    worker.stop()


@pytest.mark.parametrize("argv", [["mcp", "--nodemaven"], ["serve"]])
def test_serve_and_mcp_exit_on_a_bad_setup(argv, monkeypatch, capsys):
    from google_browser_scraper.cli import main

    for name in ("NODEMAVEN_LOGIN", "NODEMAVEN_PASSWORD", "GBS_PROXY"):
        monkeypatch.delenv(name, raising=False)
    assert main(argv) == 1
    assert capsys.readouterr().err.startswith("error: ")


MCP_CHILD = Path(__file__).with_name("stdio_mcp_child.py")


@pytest.mark.parametrize("encoding", ["", "ascii", "cp1252"])
def test_mcp_stdio_is_utf8_whatever_the_system_encoding(encoding):
    request = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "tools/call",
        "params": {"name": "google_search", "arguments": {"query": "кофе рядом"}},
    }
    env = {**os.environ, "PYTHONIOENCODING": encoding}
    done = subprocess.run(
        [sys.executable, str(MCP_CHILD)],
        input=(json.dumps(request, ensure_ascii=False) + "\n").encode("utf-8"),
        capture_output=True,
        env=env,
        timeout=60,
    )
    assert done.returncode == 0, done.stderr.decode("utf-8", "replace")
    assert b"\r\n" not in done.stdout
    reply = json.loads(done.stdout.decode("utf-8"))
    body = json.loads(reply["result"]["content"][0]["text"])
    assert body["results"][0]["title"] == "Café кофе рядом"


def test_parse_writes_utf8_when_piped(tmp_path):
    from pages import results_page

    page = tmp_path / "page.html"
    page.write_text(results_page().replace("Unresolvable result", "Café кофе"), encoding="utf-8")
    env = {
        **os.environ,
        "PYTHONIOENCODING": "ascii",
        "PYTHONPATH": str(Path(__file__).resolve().parents[1] / "src"),
    }
    done = subprocess.run(
        [sys.executable, "-m", "google_browser_scraper", "parse", str(page)],
        capture_output=True,
        env=env,
        timeout=60,
    )
    assert done.returncode == 0, done.stderr.decode("utf-8", "replace")
    titles = [r["title"] for r in json.loads(done.stdout.decode("utf-8"))["organic_results"]]
    assert "Café кофе" in titles


def test_health_says_warming_until_the_prewarm_is_over():
    release = threading.Event()

    class Slow(FakeScraper):
        def prewarm(self):
            assert release.wait(10)

    worker = Worker(lambda: Slow(), prewarm=True, log=lambda m: None).start()
    server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(worker, api_key=None, max_pages=3))
    threading.Thread(target=server.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{server.server_port}"
    try:
        assert get(base + "/health") == (503, {"status": "warming"})
        release.set()
        assert worker.ready.wait(5)
        status, body = get(base + "/health")
        assert status == 200 and body["status"] == "ok"
    finally:
        release.set()
        server.shutdown()
        worker.stop()


def test_stop_skips_queued_upkeep_and_closes_the_scraper():
    fake = FakeScraper()
    worker = Worker(lambda: fake, log=lambda m: None).start()
    busy, release = threading.Event(), threading.Event()

    def hold(scraper):
        busy.set()
        assert release.wait(10)

    worker.submit(hold)
    assert busy.wait(5)
    worker.after(lambda s: s.settle())  # queued behind the job in hand
    stopper = threading.Thread(target=worker.stop)
    stopper.start()
    while not worker._stopping.is_set():
        pass
    release.set()
    stopper.join(10)
    assert fake.settled == 0
    assert fake.closed
