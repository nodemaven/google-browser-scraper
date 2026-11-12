"""The browser serpmaven drives: is it there, and fetching it when it is not.

Patchright downloads its Chromium once per machine into a shared folder. A
fresh install has none, so before a run starts its browser this checks, and in
a terminal offers to download it; elsewhere (a service, an MCP client) it stops
with the one command that fixes it.
"""

from __future__ import annotations

import os
import subprocess
import sys
from collections.abc import Callable

INSTALL_COMMAND = "serpmaven install"


def browser_path() -> str | None:
    """Where patchright's Chromium is expected; None if that cannot be told."""
    try:
        from patchright.sync_api import sync_playwright

        with sync_playwright() as pw:
            return pw.chromium.executable_path
    except Exception:
        return None


def browser_installed(path_of: Callable[[], str | None] = browser_path) -> bool:
    """True when the browser is on disk, or when that cannot be checked (the
    launch then reports its own error)."""
    path = path_of()
    return path is None or os.path.exists(path)


def install(with_deps: bool = False, run=subprocess.call) -> int:
    """Download the browser with patchright's own installer; its exit code."""
    command = [sys.executable, "-m", "patchright", "install"]
    if with_deps:
        command.append("--with-deps")
    command.append("chromium")
    return run(command)


def interactive() -> bool:
    """Whether a person can answer a question here."""
    try:
        return sys.stdin.isatty() and sys.stderr.isatty()
    except (AttributeError, ValueError):
        return False


def ensure_browser(
    *,
    ask: bool = True,
    installed: Callable[[], bool] | None = None,
    prompt: Callable[[str], str] = input,
    do_install: Callable[[], int] | None = None,
    can_ask: Callable[[], bool] | None = None,
) -> None:
    """Make sure the browser is there before a run needs it.

    Missing and a person at the terminal: ask, then download. Otherwise raise
    RuntimeError naming the command to run.
    """
    installed = installed or browser_installed
    do_install = do_install or install
    can_ask = can_ask or interactive
    if installed():
        return
    if ask and can_ask():
        print(
            "SERP Maven needs its Chrome browser, a download of about 150 MB, once per machine.",
            file=sys.stderr,
        )
        answer = prompt("Download it now? [Y/n] ").strip().lower()
        if answer in ("", "y", "yes"):
            if do_install() == 0 and installed():
                return
            raise RuntimeError(f"the browser download did not finish; run: {INSTALL_COMMAND}")
    raise RuntimeError(f"the browser is not installed. Run: {INSTALL_COMMAND}")
