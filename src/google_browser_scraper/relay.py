"""A loopback CONNECT relay that authenticates upstream and counts the wire.

The browser is pointed at 127.0.0.1 with no credentials; the relay opens each
tunnel to the real proxy with `Proxy-Authorization` and copies bytes both ways.
It solves two problems at once:

- **Traffic.** Summing `Content-Length` misses headers, TLS overhead and chunked
  responses, so it undercounts what a provider bills. Counting sockets counts
  what the gateway sees.
- **Credentials.** Chrome takes none in `--proxy-server`, so a driver that
  talks to Chrome over raw CDP cannot use an authenticated proxy without this.

It never retries (a failed tunnel fails in the browser exactly as it would
without the relay), never touches the bytes inside a tunnel (TLS is end to end,
so the target sees the browser's own handshake), and never logs the header it
builds, which is base64 and not encryption.

One host is refused locally: Chrome's Optimization Guide, which a fresh profile
otherwise downloads a large on-device model from. No search needs it.
"""

from __future__ import annotations

import base64
import select
import socket
import threading
from urllib.parse import urlsplit

CONNECT_TIMEOUT = 30.0
IDLE_TIMEOUT = 120.0
CHUNK = 65536
MAX_HEAD_BYTES = 65536
VENDOR_FETCH = frozenset({"optimizationguide-pa.googleapis.com"})


class Relay:
    """One upstream identity on a loopback port.

    `upstream` is a Playwright-style proxy dict (`server`, optional `username`
    and `password`) with an `http://` server, or None to connect directly - the
    counting is the same either way.
    """

    def __init__(self, upstream: dict[str, str] | None, *, block_hosts=VENDOR_FETCH) -> None:
        self._auth = ""
        self._upstream_addr = None
        if upstream is not None:
            parts = urlsplit(upstream["server"])
            if parts.scheme != "http":
                raise ValueError(f"the relay speaks HTTP to the upstream proxy, not {parts.scheme}")
            self._upstream_addr = (parts.hostname, parts.port or 80)
            if upstream.get("username"):
                pair = f"{upstream['username']}:{upstream.get('password', '')}".encode()
                self._auth = f"Proxy-Authorization: Basic {base64.b64encode(pair).decode()}\r\n"
        self.block_hosts = frozenset(block_hosts)
        self._lock = threading.Lock()
        self.bytes_up = 0
        self.bytes_down = 0
        self.tunnels = 0
        self.failures = 0
        self.blocked = 0
        self._server: socket.socket | None = None
        self._threads: list[threading.Thread] = []
        self._closing = threading.Event()

    # -- lifecycle -------------------------------------------------------------

    def start(self) -> str:
        """Listen on a free loopback port and return `http://127.0.0.1:<port>`."""
        server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        server.bind(("127.0.0.1", 0))
        server.listen(64)
        server.settimeout(0.5)
        self._server = server
        thread = threading.Thread(target=self._accept_loop, name="gbs-relay", daemon=True)
        thread.start()
        self._threads.append(thread)
        return f"http://127.0.0.1:{server.getsockname()[1]}"

    def stop(self) -> None:
        self._closing.set()
        if self._server is not None:
            _close(self._server)
            self._server = None

    def __enter__(self) -> Relay:
        self.start()
        return self

    def __exit__(self, *exc) -> None:
        self.stop()

    @property
    def total(self) -> int:
        with self._lock:
            return self.bytes_up + self.bytes_down

    # -- serving ---------------------------------------------------------------

    def _accept_loop(self) -> None:
        while not self._closing.is_set():
            try:
                client, _ = self._server.accept()
            except TimeoutError:
                continue
            except OSError:
                return
            threading.Thread(target=self._handle, args=(client,), daemon=True).start()

    def _handle(self, client: socket.socket) -> None:
        try:
            head = _read_head(client)
            if not head:
                return
            line = head.split("\r\n", 1)[0]
            method, _, rest = line.partition(" ")
            if method.upper() == "CONNECT":
                self._tunnel(client, rest.split(" ", 1)[0])
            else:
                self._forward(client, head)
        except Exception:
            # Counted, never narrated: a traceback would carry the request line.
            with self._lock:
                self.failures += 1
        finally:
            _close(client)

    def _open(self, host: str, port: int) -> socket.socket:
        sock = socket.create_connection((host, port), timeout=CONNECT_TIMEOUT)
        sock.settimeout(CONNECT_TIMEOUT)
        return sock

    def _refuse(self, client: socket.socket) -> None:
        with self._lock:
            self.blocked += 1
        try:
            client.sendall(b"HTTP/1.1 403 Forbidden\r\nContent-Length: 0\r\n\r\n")
        except OSError:
            pass

    def _fail(self) -> None:
        with self._lock:
            self.failures += 1

    def _tunnel(self, client: socket.socket, authority: str) -> None:
        host, port = _split_authority(authority, 443)
        if host in self.block_hosts:
            self._refuse(client)
            return
        try:
            if self._upstream_addr is None:
                upstream = self._open(host, port)
            else:
                upstream = self._open(*self._upstream_addr)
                request = (
                    f"CONNECT {authority} HTTP/1.1\r\nHost: {authority}\r\n"
                    f"{self._auth}Proxy-Connection: Keep-Alive\r\n\r\n"
                ).encode()
                upstream.sendall(request)
                self._count(up=len(request))
                reply = _read_head(upstream)
                self._count(down=len(reply))
                if _status(reply) != 200:
                    # Pass the gateway's own answer through: its status is the
                    # evidence (a bad parameter is 406 or 407 on some gateways).
                    self._fail()
                    if reply:
                        client.sendall(reply.encode("latin-1"))
                    _close(upstream)
                    return
        except OSError:
            self._fail()
            return
        with self._lock:
            self.tunnels += 1
        try:
            client.sendall(b"HTTP/1.1 200 Connection established\r\n\r\n")
        except OSError:
            _close(upstream)
            return
        self._pump(client, upstream)

    def _forward(self, client: socket.socket, head: str) -> None:
        """Plain HTTP in absolute-URI form (revocation checks, http:// redirects)."""
        target = urlsplit(head.split("\r\n", 1)[0].split(" ")[1])
        if (target.hostname or "") in self.block_hosts:
            self._refuse(client)
            return
        lines = [ln for ln in head.split("\r\n") if not ln.lower().startswith("proxy-")]
        try:
            if self._upstream_addr is None:
                upstream = self._open(target.hostname, target.port or 80)
                path = target.path or "/"
                if target.query:
                    path += "?" + target.query
                method, _, rest = lines[0].partition(" ")
                lines[0] = f"{method} {path} {rest.rsplit(' ', 1)[-1]}"
                rebuilt = "\r\n".join(lines)
            else:
                upstream = self._open(*self._upstream_addr)
                rebuilt = lines[0] + "\r\n" + self._auth + "\r\n".join(lines[1:])
            data = rebuilt.encode("latin-1")
            upstream.sendall(data)
            self._count(up=len(data))
        except OSError:
            self._fail()
            return
        self._pump(client, upstream)

    def _pump(self, client: socket.socket, upstream: socket.socket) -> None:
        """Copy both ways until either end closes, counting per chunk so a
        keep-alive tunnel's bytes land on the page that used them."""
        client.settimeout(None)
        upstream.settimeout(None)
        try:
            while not self._closing.is_set():
                readable, _, broken = select.select(
                    [client, upstream], [], [client, upstream], IDLE_TIMEOUT
                )
                if broken or not readable:
                    break
                for source in readable:
                    data = source.recv(CHUNK)
                    if not data:
                        return
                    if source is client:
                        upstream.sendall(data)
                        self._count(up=len(data))
                    else:
                        client.sendall(data)
                        self._count(down=len(data))
        except OSError:
            pass
        finally:
            _close(upstream)

    def _count(self, *, up: int = 0, down: int = 0) -> None:
        with self._lock:
            self.bytes_up += up
            self.bytes_down += down


def _read_head(sock: socket.socket) -> str:
    buffer = b""
    while b"\r\n\r\n" not in buffer:
        try:
            chunk = sock.recv(CHUNK)
        except OSError:
            return ""
        if not chunk:
            return ""
        buffer += chunk
        if len(buffer) > MAX_HEAD_BYTES:
            return ""
    return buffer.decode("latin-1")


def _status(reply: str) -> int:
    parts = reply.split("\r\n", 1)[0].split(" ")
    return int(parts[1]) if len(parts) > 1 and parts[1].isdigit() else 0


def _split_authority(authority: str, default_port: int) -> tuple[str, int]:
    if authority.startswith("["):
        host, _, rest = authority[1:].partition("]")
        port = rest.lstrip(":")
    else:
        host, _, port = authority.rpartition(":") if ":" in authority else (authority, "", "")
    return host.lower(), int(port) if port.isdigit() else default_port


def _close(sock: socket.socket) -> None:
    try:
        sock.close()
    except OSError:
        pass
