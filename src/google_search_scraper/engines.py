"""The browsers a scraper can drive, behind one interface.

Every session offers `__enter__`, `close`, `visit(url, dwell)`, `search(query)`
and `next_page()`, so the scraper does not know which one it holds.

- `patchright` (default).
- `cloak`: CloakBrowser's patched Chromium; experimental.

An engine is added only after it has been run on proxy-benchmark's warm-up
ladder against Google.
"""

from __future__ import annotations

from .browser import BrowserSession


class CloakSession(BrowserSession):
    """CloakBrowser's patched Chromium; it hands back a Playwright context, so
    every step after launch is the patchright code path."""

    name = "cloak"

    def _launch(self, profile: str):
        try:
            import cloakbrowser
        except ImportError as exc:
            raise RuntimeError("the cloak engine needs: pip install 'google-serp[cloak]'") from exc
        # `viewport=None` reports the real window, the same choice as
        # `no_viewport=True` on patchright. Closing the context stops Playwright.
        return cloakbrowser.launch_persistent_context(
            profile,
            headless=self._headless,
            proxy=self._proxy,
            viewport=None,
            timezone=self._timezone_id,
            args=self._args or None,
        )


ENGINES = {
    "patchright": BrowserSession,
    "cloak": CloakSession,
}
DEFAULT_ENGINE = "patchright"


def get(name: str):
    try:
        return ENGINES[name]
    except KeyError:
        raise ValueError(f"engine must be one of {', '.join(ENGINES)}, got {name!r}") from None
