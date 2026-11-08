"""The HTTP API and the MCP server, over a fake scraper on the worker thread."""

import io
import json
import os
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from http.server import ThreadingHTTPServer
from pathlib import Path

import pytest

from google_browser_scraper.mcp import McpServer, compact
from google_browser_scraper.scraper import ExitsRefused, PendingLinks, Settings, Stats
from google_browser_scraper.server import Archive, Worker, make_handler


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
        self.locales = []
        self.link_limits = []
        #: Set to make start_search return records whose links are still pending.
        self.links_gate: threading.Event | None = None

    def start_search(self, query, settle=True, links=None, **locale):
        self.link_limits.append(links)
        records = self.search(query, settle, **locale)
        if self.links_gate is None:
            return PendingLinks(records)
        lock, gate = threading.Lock(), self.links_gate
        for r in records:
            r["organic_results"][0].update(
                link=None, redirect_link="https://www.google.com/goto?url=A"
            )

        def fill(record):
            assert gate.wait(10)
            with lock:
                record["organic_results"][0]["link"] = "https://late.example/"

        pool = ThreadPoolExecutor(2)
        jobs = {i: pool.submit(fill, r) for i, r in enumerate(records)}
        pool.shutdown(wait=False)
        return PendingLinks(records, jobs, lock)

    def search(self, query, settle=True, **locale):
        self.threads.add(threading.get_ident())
        self.calls.append((query, self.settings.pages))
        self.locales.append(locale)
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


def test_hl_and_gl_reach_the_scraper_for_one_query(api, worker):
    assert get(api() + "/search.json?q=x&hl=de&gl=at")[0] == 200
    assert get(api() + "/search.json?q=y")[0] == 200
    assert worker.fake.locales == [{"hl": "de", "gl": "at"}, {}]


def test_a_malformed_hl_or_gl_is_refused_before_the_url(api, worker):
    status, body = get(api() + "/search.json?q=x&gl=usa")
    assert status == 400 and "gl" in body["error"]
    status, body = get(api() + "/search.json?q=x&hl=en%26gl%3Dru")
    assert status == 400 and "hl" in body["error"]
    assert worker.fake.calls == []


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


def test_mcp_tool_call_takes_hl_and_gl(worker):
    server = McpServer(worker, stdout=io.StringIO())
    args = {"query": "shoes", "hl": "pt-BR", "gl": "br"}
    reply = rpc(server, "tools/call", {"name": "google_search", "arguments": args})
    assert reply["result"]["isError"] is False
    assert worker.fake.locales[-1] == {"hl": "pt-BR", "gl": "br"}
    bad_args = {"query": "q", "gl": "1"}
    bad = rpc(server, "tools/call", {"name": "google_search", "arguments": bad_args})
    assert bad["result"]["isError"] is True


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


def test_compact_carries_the_first_ai_overview():
    from google_browser_scraper.mcp import compact

    records = [
        {"organic_results": [], "ai_overview": {"text": "Page one answer."}},
        {"organic_results": [], "ai_overview": {"text": "Page two answer."}},
    ]
    assert compact(records)["ai_overview"] == "Page one answer."
    assert "ai_overview" not in compact([{"organic_results": [], "ai_overview": {}}])


def test_compact_carries_the_map_places():
    from google_browser_scraper.mcp import compact

    place = {
        "position": 1,
        "title": "P",
        "rating": 4.5,
        "reviews": 10,
        "price": "$",
        "type": "Cafe",
        "address": "1 Main St",
        "extensions": ["1 Main St"],
    }
    out = compact([{"organic_results": [], "local_results": [place]}])
    assert out["places"] == [
        {"title": "P", "rating": 4.5, "reviews": 10, "type": "Cafe", "address": "1 Main St"}
    ]


def test_an_ai_overview_alone_is_not_a_tool_error():
    class OverviewOnly:
        def call(self, job):
            return [{"organic_results": [], "ai_overview": {"text": "x" * 300}}, {"error": "no"}]

        def after(self, job):
            pass

    server = McpServer(OverviewOnly(), stdout=io.StringIO())
    args = {"name": "google_search", "arguments": {"query": "q", "pages": 2}}
    assert rpc(server, "tools/call", args)["result"]["isError"] is False


# -- links after the page -----------------------------------------------------


def finished(url):
    """The archived search once it is no longer Processing."""
    for _ in range(500):
        body = get(url)[1]
        if body["search_metadata"]["status"] != "Processing":
            return body
        time.sleep(0.01)
    raise AssertionError("still processing")


def test_the_browser_is_free_while_a_caller_waits_for_its_links(api, worker):
    worker.fake.links_gate = gate = threading.Event()
    base, answer = api(), []
    try:
        caller = threading.Thread(target=lambda: answer.append(get(base + "/search.json?q=a")))
        caller.start()
        for _ in range(500):
            if worker.fake.calls:
                break
            time.sleep(0.01)
        # "a" is still waiting for its links; another job runs on the worker meanwhile.
        assert worker.submit(lambda s: s.search("b")).result(timeout=2)
        assert not answer
        gate.set()
        caller.join(10)
    finally:
        gate.set()
    status, body = answer[0]
    assert status == 200 and body["organic_results"][0]["link"] == "https://late.example/"
    assert "links_pending" not in body["search_metadata"]


def test_links_wait_answers_with_what_is_in_and_the_archive_has_the_rest(api, worker):
    worker.fake.links_gate = gate = threading.Event()
    base = api()
    try:
        started = time.monotonic()
        status, body = get(base + "/search.json?q=a&links_wait=0.2")
        assert time.monotonic() - started >= 0.2  # it did wait
        assert status == 200 and body["search_metadata"]["status"] == "Success"
        assert body["organic_results"][0]["link"] is None
        assert body["search_metadata"]["links_pending"] == 1
        endpoint = body["search_metadata"]["json_endpoint"]
        assert endpoint == f"/searches/{body['search_metadata']['id']}.json"
        assert get(base + endpoint)[1]["search_metadata"]["status"] == "Processing"
        gate.set()
        finished(base + endpoint)
    finally:
        gate.set()
    status, kept = get(base + endpoint)
    assert status == 200 and kept["organic_results"][0]["link"] == "https://late.example/"
    assert "links_pending" not in kept["search_metadata"]


def test_async_answers_at_once_and_the_archive_has_the_record_later(api, worker):
    worker.fake.links_gate = gate = threading.Event()
    base = api()
    try:
        status, body = get(base + "/search.json?q=a&async=true")
        assert status == 200 and body["search_metadata"]["status"] == "Processing"
        assert body["search_parameters"] == {"q": "a", "pages": 1}
        endpoint = body["search_metadata"]["json_endpoint"]
        gate.set()
        finished(base + endpoint)
    finally:
        gate.set()
    kept = get(base + endpoint)[1]
    assert kept["search_metadata"]["status"] == "Success"
    assert kept["search_metadata"]["id"] == body["search_metadata"]["id"]
    assert kept["organic_results"][0]["link"] == "https://late.example/"
    worker.call(lambda s: None)  # the upkeep was queued after the search
    assert worker.fake.settled == 1 and worker.fake.settle_flags == [False]


def test_a_failed_async_search_is_kept_as_an_error(monkeypatch):
    fake = FakeScraper(refuse=True)
    worker = Worker(lambda: fake, log=lambda m: None).start()
    server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(worker, api_key=None, max_pages=3))
    threading.Thread(target=server.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{server.server_port}"
    try:
        endpoint = get(base + "/search.json?q=x&async=1")[1]["search_metadata"]["json_endpoint"]
        worker.call(lambda s: None)
        status, kept = get(base + endpoint)
    finally:
        server.shutdown()
        worker.stop()
    assert status == 200 and kept["search_metadata"]["status"] == "Error"
    assert "6 exits" in kept["error"] and fake.resets == 1 and fake.settled == 0


def test_links_reaches_the_scraper(api, worker):
    base = api()
    assert get(base + "/search.json?q=a&links=5")[0] == 200
    assert get(base + "/search.json?q=b")[0] == 200
    assert worker.fake.link_limits == [5, None]


@pytest.mark.parametrize(
    "query", ["links=-1", "links=five", "links=2.5", "links_wait=-1", "links_wait=soon"]
)
def test_bad_link_parameters(api, worker, query):
    assert get(api() + "/search.json?q=x&" + query)[0] == 400
    assert worker.fake.calls == []


def test_links_wait_answers_as_soon_as_the_links_are_in(api, worker):
    worker.fake.links_gate = gate = threading.Event()
    threading.Timer(0.05, gate.set).start()
    started = time.monotonic()
    status, body = get(api() + "/search.json?q=a&links_wait=5")
    assert time.monotonic() - started < 4
    assert status == 200 and body["organic_results"][0]["link"] == "https://late.example/"
    assert "links_pending" not in body["search_metadata"]


def test_an_unknown_or_expired_search_is_a_404(worker):
    now = [1000.0]
    server = ThreadingHTTPServer(
        ("127.0.0.1", 0),
        make_handler(
            worker, api_key=None, max_pages=3, archive=Archive(ttl=60, clock=lambda: now[0])
        ),
    )
    threading.Thread(target=server.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{server.server_port}"
    try:
        endpoint = get(base + "/search.json?q=a")[1]["search_metadata"]["json_endpoint"]
        now[0] += 59
        assert get(base + endpoint)[0] == 200
        now[0] += 2
        assert get(base + endpoint)[0] == 404  # past its hour, though nothing evicted it
        assert get(base + "/searches/" + "0" * 32 + ".json")[0] == 404
    finally:
        server.shutdown()


def test_a_several_page_search_is_archived_as_an_array_even_with_one_page():
    search = Archive().new({"q": "a", "pages": 2})
    search.land(PendingLinks([record("a")]))
    assert isinstance(search.body(), list) and len(search.body()) == 1
    single = Archive().new({"q": "a", "pages": 1})
    single.land(PendingLinks([record("a")]))
    assert single.body()["search_parameters"]["q"] == "a"


def test_the_archive_keeps_the_newest_searches():
    archive = Archive(size=2)
    first, second, third = (archive.new({"q": q}) for q in "abc")
    assert archive.get(first.id) is None
    assert archive.get(second.id) is second and archive.get(third.id) is third


def _busy(worker):
    """Hold the worker on a job until the returned event is set."""
    busy, release = threading.Event(), threading.Event()

    def hold(scraper):
        busy.set()
        assert release.wait(10)

    worker.submit(hold)
    assert busy.wait(5)
    return release


def test_a_search_that_times_out_in_the_queue_never_runs(api, worker, monkeypatch):
    from google_browser_scraper import server

    monkeypatch.setattr(server, "REQUEST_TIMEOUT_S", 0.3)
    release = _busy(worker)
    try:
        status, body = get(api() + "/search.json?q=late")
    finally:
        release.set()
    worker.call(lambda s: None)
    assert status == 500 and "no answer within" in body["error"]
    assert worker.fake.calls == []  # cancelled before the worker took it


def test_async_searches_waiting_at_once_are_bounded(api, worker, monkeypatch):
    from google_browser_scraper import server

    monkeypatch.setattr(server, "ASYNC_QUEUED_MAX", 1)
    base = api()
    release = _busy(worker)
    try:
        first = get(base + "/search.json?q=a&async=true")
        refused = get(base + "/search.json?q=b&async=true")
    finally:
        release.set()
    assert first[0] == 200 and refused[0] == 429
    kept = finished(base + first[1]["search_metadata"]["json_endpoint"])
    assert kept["search_metadata"]["status"] == "Success"
    assert get(base + "/search.json?q=c&async=true")[0] == 200  # the slot is free again


def test_a_timeout_error_of_the_search_itself_is_not_reported_as_no_answer(api, worker):
    def times_out(*args, **kwargs):
        raise TimeoutError("page load took too long")

    worker.fake.search = times_out
    status, body = get(api() + "/search.json?q=x")
    assert status == 500 and body["error"] == "TimeoutError: page load took too long"
