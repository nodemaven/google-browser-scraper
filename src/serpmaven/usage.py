"""One anonymous usage ping per run, so the project knows how many people use it.

`search`, `serve` and `mcp` send one small JSON request when they start: the serpmaven
version, the operating system, the Python version, the command, which kind of proxy it
runs with (`nodemaven`, `nodemaven-demo`, `url` or `none` - never the proxy itself) and a
random id made on the first run and kept in the config folder, so one machine counts once.
No query, result, address, login or file name is sent. It goes straight to ENDPOINT, not
through the proxy, on a background thread that gives up after a few seconds; a failure is
silent and never affects the run.

Set `SERPMAVEN_NO_TELEMETRY=1` (or the common `DO_NOT_TRACK=1`) to turn it off. The first
run prints one line saying so. The endpoint's code is in `usage-endpoint/` in the repository.
"""

from __future__ import annotations

import json
import os
import platform
import secrets
import sys
import threading
import urllib.request
from collections.abc import Callable
from typing import Any

from . import __version__, config

#: Where the ping goes; empty sends nothing.
ENDPOINT = ""
OFF = ("SERPMAVEN_NO_TELEMETRY", "DO_NOT_TRACK")
ID_FILE = "install-id"
TIMEOUT_S = 3
KINDS = ("nodemaven", "nodemaven-demo", "url", "none")


def enabled() -> bool:
    if not ENDPOINT:
        return False
    return not any(
        os.environ.get(name, "").strip().lower() not in ("", "0", "false", "no") for name in OFF
    )


def install_id() -> tuple[str | None, bool]:
    """The machine's random id and whether it was made just now; (None, False) when
    the config folder cannot be written."""
    path = config.directory() / ID_FILE
    try:
        value = path.read_text(encoding="utf-8").strip()
        if len(value) == 32 and all(c in "0123456789abcdef" for c in value):
            return value, False
    except OSError:
        pass
    value = secrets.token_hex(16)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(value, encoding="utf-8")
    except OSError:
        return None, False
    return value, True


def payload(command: str, proxy_kind: str, machine: str | None) -> dict[str, Any]:
    return {
        "v": __version__,
        "os": platform.system().lower() or "unknown",
        "py": f"{sys.version_info[0]}.{sys.version_info[1]}",
        "cmd": command,
        "proxy": proxy_kind if proxy_kind in KINDS else "url",
        "id": machine,
    }


def _post(body: dict[str, Any]) -> None:
    request = urllib.request.Request(
        ENDPOINT,
        data=json.dumps(body).encode("utf-8"),
        headers={"Content-Type": "application/json", "User-Agent": f"serpmaven/{__version__}"},
        method="POST",
    )
    try:
        urllib.request.urlopen(request, timeout=TIMEOUT_S).close()
    except Exception:  # noqa: BLE001 - a ping must never affect the run
        pass


def ping(
    command: str,
    proxy_kind: str,
    *,
    send: Callable[[dict[str, Any]], None] = _post,
    wait: bool = False,
) -> threading.Thread | None:
    """Send the run's ping in the background; returns the thread, or None when off."""
    if not enabled():
        return None
    machine, new = install_id()
    if new:
        print(
            "serpmaven sends one anonymous usage ping per run (version, OS, command, proxy "
            "kind); set SERPMAVEN_NO_TELEMETRY=1 to turn it off",
            file=sys.stderr,
        )
    thread = threading.Thread(
        target=send,
        args=(payload(command, proxy_kind, machine),),
        daemon=True,
        name="serpmaven-usage",
    )
    thread.start()
    if wait:
        thread.join(TIMEOUT_S + 1)
    return thread
