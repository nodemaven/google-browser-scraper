"""One-line fixes for the setup errors a first run usually hits.

The browser library reports these as long tracebacks with the useful part at
the bottom. Each is recognised by its message and replaced with what to run.
"""

from __future__ import annotations

import sys

NO_BROWSER = "the browser is not installed. Run: serpmaven install (then run this command again)"
NO_DISPLAY = (
    "no display found, and the browser runs with a window. On a Linux server "
    "run it under a virtual display: sudo apt install xvfb, then "
    "xvfb-run -a serpmaven ... (add SERPMAVEN_BROWSER_ARGS=--enable-unsafe-swiftshader "
    "for WebGL), or use the Docker image, which has both"
)
NO_SYSTEM_LIBS = (
    "the browser cannot start: system libraries are missing. Run: "
    "serpmaven install --with-deps (needs sudo)"
)


def explain(exc: BaseException) -> str | None:
    """A short fix for a known setup error, or None."""
    text = str(exc)
    # Checked first: its message also names "patchright install".
    if "install-deps" in text or "error while loading shared libraries" in text:
        return NO_SYSTEM_LIBS
    if "Executable doesn't exist" in text or "patchright install" in text:
        return NO_BROWSER
    if "Missing X server" in text or "xvfb-run" in text or "$DISPLAY" in text:
        return NO_DISPLAY
    return None


def report(exc: BaseException) -> bool:
    """Print the fix for a known setup error to stderr; False if unknown."""
    hint = explain(exc)
    if hint is None:
        return False
    print(f"error: {hint}", file=sys.stderr)
    return True
