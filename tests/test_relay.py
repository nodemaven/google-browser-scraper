"""The relay against a fake upstream proxy and an echo server, all on 127.0.0.1."""

import base64
import socket
import threading

import pytest

from google_search_scraper.relay import Relay


def _serve(handler):
    server = socket.socket()
    server.bind(("127.0.0.1", 0))
    server.listen(16)

    def loop():
        while True:
            try:
                conn, _ = server.accept()
            except OSError:
                return
            threading.Thread(target=handler, args=(conn,), daemon=True).start()

    threading.Thread(target=loop, daemon=True).start()
    return server, server.getsockname()[1]


def _read_head(sock):
    data = b""
    while b"\r\n\r\n" not in data:
        chunk = sock.recv(4096)
        if not chunk:
            break
        data += chunk
    return data.decode("latin-1")


@pytest.fixture
def echo():
    def handle(conn):
        with conn:
            while data := conn.recv(4096):
                conn.sendall(data)

    server, port = _serve(handle)
    yield port
    server.close()


@pytest.fixture
def upstream(echo):
    """A proxy that wants user:pass, then tunnels to the echo server."""
    seen = []
    expected = "Basic " + base64.b64encode(b"user-session-abc:pa ss").decode()

    def handle(conn):
        with conn:
            head = _read_head(conn)
            seen.append(head)
            if f"Proxy-Authorization: {expected}" not in head:
                conn.sendall(b"HTTP/1.1 407 Proxy Authentication Required\r\n\r\n")
                return
            target = socket.create_connection(("127.0.0.1", echo))
            conn.sendall(b"HTTP/1.1 200 Connection established\r\n\r\n")
            with target:
                while data := conn.recv(4096):
                    target.sendall(data)
                    conn.sendall(target.recv(4096))

    server, port = _serve(handle)
    yield port, seen
    server.close()


def _tunnel(relay_address, authority, payload=b"hello"):
    host, port = relay_address.removeprefix("http://").split(":")
    with socket.create_connection((host, int(port)), timeout=5) as sock:
        sock.sendall(f"CONNECT {authority} HTTP/1.1\r\nHost: {authority}\r\n\r\n".encode())
        head = _read_head(sock)
        if " 200 " not in head.split("\r\n")[0] + " ":
            return head, b""
        sock.sendall(payload)
        return head, sock.recv(4096)


def test_tunnel_authenticates_upstream_and_counts_both_ways(upstream, echo):
    port, seen = upstream
    relay = Relay(
        {"server": f"http://127.0.0.1:{port}", "username": "user-session-abc", "password": "pa ss"}
    )
    address = relay.start()
    try:
        head, echoed = _tunnel(address, f"127.0.0.1:{echo}", b"x" * 1000)
        assert head.startswith("HTTP/1.1 200")
        assert echoed == b"x" * 1000
    finally:
        relay.stop()
    assert relay.tunnels == 1
    # The CONNECT request and reply are on the wire too, so the count is above
    # the payload in both directions.
    assert relay.bytes_up > 1000 and relay.bytes_down > 1000
    assert "CONNECT 127.0.0.1" in seen[0]


def test_gateway_refusal_is_passed_through_unchanged(upstream, echo):
    port, _ = upstream
    relay = Relay({"server": f"http://127.0.0.1:{port}", "username": "wrong", "password": "x"})
    address = relay.start()
    try:
        head, _ = _tunnel(address, f"127.0.0.1:{echo}")
    finally:
        relay.stop()
    assert head.startswith("HTTP/1.1 407")
    assert relay.failures == 1 and relay.tunnels == 0


def test_optimization_guide_is_refused_locally(upstream):
    port, seen = upstream
    relay = Relay({"server": f"http://127.0.0.1:{port}"})
    address = relay.start()
    try:
        head, _ = _tunnel(address, "optimizationguide-pa.googleapis.com:443")
    finally:
        relay.stop()
    assert head.startswith("HTTP/1.1 403")
    assert relay.blocked == 1
    assert seen == []  # nothing reached the upstream


def test_direct_mode_counts_without_an_upstream(echo):
    relay = Relay(None)
    address = relay.start()
    try:
        head, echoed = _tunnel(address, f"127.0.0.1:{echo}", b"abc")
    finally:
        relay.stop()
    assert head.startswith("HTTP/1.1 200") and echoed == b"abc"
    assert relay.total >= 6


def test_only_http_upstreams_are_accepted():
    with pytest.raises(ValueError):
        Relay({"server": "socks5://127.0.0.1:1080"})


def test_plain_http_is_forwarded_in_absolute_form_with_credentials():
    seen = []

    def handle(conn):
        with conn:
            seen.append(_read_head(conn))
            conn.sendall(b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\nConnection: close\r\n\r\nok")

    server, port = _serve(handle)
    relay = Relay({"server": f"http://127.0.0.1:{port}", "username": "u", "password": "p"})
    address = relay.start()
    host, rport = address.removeprefix("http://").split(":")
    try:
        with socket.create_connection((host, int(rport)), timeout=5) as sock:
            sock.sendall(
                b"GET http://example.com/x HTTP/1.1\r\nHost: example.com\r\n"
                b"Proxy-Authorization: Basic leaked\r\n\r\n"
            )
            reply = sock.recv(4096)
    finally:
        relay.stop()
        server.close()
    assert reply.endswith(b"ok")
    assert seen[0].startswith("GET http://example.com/x HTTP/1.1")
    assert "Basic leaked" not in seen[0]  # the browser's header is dropped, ours added
    assert "Proxy-Authorization: Basic " + base64.b64encode(b"u:p").decode() in seen[0]
