import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from serpmaven import cli, config, usage


@pytest.fixture
def endpoint(monkeypatch):
    monkeypatch.setattr(usage, "ENDPOINT", "http://127.0.0.1:9/ping")
    for name in usage.OFF:
        monkeypatch.delenv(name, raising=False)


def test_the_ping_carries_no_proxy_query_or_login_and_one_id_per_machine(endpoint, capsys):
    sent = []
    usage.ping("search", "url", send=sent.append, wait=True)
    usage.ping("serve", "nodemaven", send=sent.append, wait=True)
    first, second = sent
    assert set(first) == {"v", "os", "py", "cmd", "proxy", "id"}
    assert (first["cmd"], first["proxy"]) == ("search", "url")
    assert (second["cmd"], second["proxy"]) == ("serve", "nodemaven")
    assert first["id"] == second["id"] and len(first["id"]) == 32
    # The first run says so, once.
    assert capsys.readouterr().err.count("SERPMAVEN_NO_TELEMETRY=1") == 1
    assert usage.payload("search", "http://u:p@host:1", None)["proxy"] == "url"


@pytest.mark.parametrize("name", usage.OFF)
@pytest.mark.parametrize("value", ["1", "true", "yes"])
def test_either_variable_turns_it_off(endpoint, monkeypatch, name, value):
    monkeypatch.setenv(name, value)
    sent = []
    assert usage.ping("search", "none", send=sent.append, wait=True) is None
    assert sent == [] and not (config.directory() / usage.ID_FILE).exists()


def test_no_endpoint_sends_nothing(monkeypatch):
    monkeypatch.setattr(usage, "ENDPOINT", "")
    sent = []
    assert usage.ping("search", "none", send=sent.append) is None and sent == []


def test_a_dead_endpoint_is_silent(endpoint):
    usage._post({"cmd": "search"})  # port 9 refuses; nothing is raised


def test_the_request_is_json_to_the_endpoint(monkeypatch):
    got = []

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):  # noqa: N802
            got.append(json.loads(self.rfile.read(int(self.headers["Content-Length"]))))
            self.send_response(204)
            self.end_headers()

        def log_message(self, *a):
            pass

    server = HTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.handle_request, daemon=True).start()
    monkeypatch.setattr(usage, "ENDPOINT", f"http://127.0.0.1:{server.server_port}/v1/ping")
    usage._post({"cmd": "mcp"})
    server.server_close()
    assert got == [{"cmd": "mcp"}]


def test_a_run_pings_with_its_command_and_proxy_kind(endpoint, monkeypatch):
    seen = []
    monkeypatch.setattr(usage, "ping", lambda command, kind, **kw: seen.append((command, kind)))
    args = cli._parser().parse_args(["search", "q", "--no-proxy"])
    with cli._proxy_source(args):
        pass
    assert seen == [("search", "none")]
