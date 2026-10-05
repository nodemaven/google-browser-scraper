"""One browser identity: one profile, one proxy session, one cookie jar.

- Patchright, headful: headless builds announce `HeadlessChrome` in the UA.
- A persistent context with `no_viewport=True`, so the page reports the real
  screen rather than an emulated one.
- `locale` is never set: setting it through the context makes the main thread
  and Web Workers disagree. `timezone_id` goes through CDP emulation and agrees
  in workers, and is off unless asked for.
- The query is typed into the box, not sent as `/search?q=`.
"""

from __future__ import annotations

import random
import shutil
import tempfile
import time
from dataclasses import dataclass

HOME_URL = "https://www.google.com/?hl=en"
SEARCH_BOX = "textarea[name='q'], input[name='q']"
READY = "#rso, #search"
# Reject first: it sets the smaller cookie. The middle selector is the
# redirect form of the wall served to EU exits.
CONSENT_BUTTONS = (
    "button#W0wltc",
    'form[action="https://consent.google.com/save"]'
    ':has(input[name="set_eom"][value="true"]) button',
    "button#L2AGLb",
)
NAV_TIMEOUT_MS = 60_000
WARM_TIMEOUT_MS = 30_000


@dataclass(frozen=True)
class Fetched:
    """What one navigation left on screen."""

    url: str
    status: int | None
    html: str
    consent_dismissed: bool = False


class BrowserSession:
    """A Patchright persistent context bound to one proxy session.

    Use as a context manager. The profile directory is temporary and removed on
    close, so an identity is never reused by accident after it is retired.

    `browser_args` are extra Chromium switches. **Never pass a
    `--disable-features` here**: Chromium honours only the last such switch, so
    one from the caller silently discards Playwright's own list, which includes
    `OptimizationHints`.
    """

    name = "patchright"

    def __init__(
        self,
        proxy: dict[str, str] | None = None,
        *,
        headless: bool = False,
        channel: str | None = None,
        timezone_id: str | None = None,
        browser_args: list[str] | tuple[str, ...] = (),
        rng: random.Random | None = None,
    ) -> None:
        if any(a.startswith(("--disable-features", "--enable-features")) for a in browser_args):
            raise ValueError(
                "--disable-features/--enable-features would replace the browser's "
                "own list rather than add to it; not accepted"
            )
        self._proxy = proxy
        self._headless = headless
        self._channel = channel
        self._timezone_id = timezone_id
        self._args = list(browser_args)
        self._rng = rng or random.Random()
        self._pw = None
        self._context = None
        self._profile = None
        self.page = None

    def __enter__(self) -> BrowserSession:
        self._profile = tempfile.mkdtemp(prefix="gbs-profile-")
        try:
            self._context = self._launch(self._profile)
        except Exception:
            self.close()
            raise
        pages = self._context.pages
        self.page = pages[0] if pages else self._context.new_page()
        return self

    def _launch(self, profile: str):
        from patchright.sync_api import sync_playwright

        self._pw = sync_playwright().start()
        return self._pw.chromium.launch_persistent_context(
            user_data_dir=profile,
            headless=self._headless,
            channel=self._channel,
            proxy=self._proxy,
            no_viewport=True,
            timezone_id=self._timezone_id,
            args=self._args or None,
        )

    def __exit__(self, *exc) -> None:
        self.close()

    def close(self) -> None:
        for step in (
            lambda: self._context and self._context.close(),
            lambda: self._pw and self._pw.stop(),
        ):
            try:
                step()
            except Exception:
                pass
        if self._profile:
            shutil.rmtree(self._profile, ignore_errors=True)
        self._context = self._pw = self._profile = self.page = None

    # -- warming -------------------------------------------------------------

    def visit(self, url: str, dwell_seconds: float) -> bool:
        """Open a warm-up page and stay on it. Returns whether it arrived."""
        try:
            self.page.goto(url, wait_until="domcontentloaded", timeout=WARM_TIMEOUT_MS)
        except Exception:
            return False
        time.sleep(dwell_seconds)
        return True

    # -- searching -----------------------------------------------------------

    def search(self, query: str) -> Fetched:
        """Type `query` into Google's box and return the page it led to."""
        page = self.page
        if not any(h.is_visible() for h in page.query_selector_all(SEARCH_BOX)):
            page.goto(HOME_URL, wait_until="domcontentloaded", timeout=NAV_TIMEOUT_MS)
        dismissed = self._dismiss_consent()
        # Act on the handle that was found, never on the selector again: the
        # selector also matches a hidden field, and `page.click` would pick it.
        box = page.wait_for_selector(SEARCH_BOX, state="visible", timeout=NAV_TIMEOUT_MS)
        box.click()
        # A results page keeps the previous query in the box. Typing after it
        # appends, and the appended query returns a real page for the wrong
        # question. Select it so the typing replaces it.
        if box.input_value():
            box.press("ControlOrMeta+a")
        box.type(query, delay=self._rng.randint(45, 140))
        typed = box.input_value()
        if typed != query:
            raise RuntimeError(f"search box holds {typed!r} after typing {query!r}; not submitting")
        # `no_wait_after=True` keeps `press` from waiting for the navigation a
        # second time with Playwright's own 30 s default under ours.
        with page.expect_navigation(wait_until="domcontentloaded", timeout=NAV_TIMEOUT_MS) as nav:
            box.press("Enter", no_wait_after=True, timeout=NAV_TIMEOUT_MS)
        return self._snapshot(nav.value, dismissed)

    def next_page(self) -> Fetched | None:
        """Click "Next" on the current results page, if there is one."""
        link = self.page.query_selector("a#pnnext")
        if link is None or not link.is_visible():
            return None
        with self.page.expect_navigation(
            wait_until="domcontentloaded", timeout=NAV_TIMEOUT_MS
        ) as nav:
            link.click()
        return self._snapshot(nav.value, False)

    def _snapshot(self, response, dismissed: bool) -> Fetched:
        # Google builds results in the browser; reading at domcontentloaded
        # catches the "enable JavaScript" scaffold. A refusal never grows the
        # container, so the short timeout is expected to expire on those.
        try:
            self.page.wait_for_selector(READY, state="attached", timeout=8_000)
        except Exception:
            pass
        return Fetched(
            url=self.page.url,
            status=response.status if response is not None else None,
            html=self.page.content(),
            consent_dismissed=dismissed,
        )

    def _dismiss_consent(self) -> bool:
        """Clear the consent overlay if one is up. It is intermittent, so
        finding none is normal and not an error."""
        for selector in CONSENT_BUTTONS:
            for handle in self.page.query_selector_all(selector):
                if handle.is_visible():
                    handle.click(timeout=10_000)
                    try:
                        self.page.wait_for_load_state("domcontentloaded", timeout=10_000)
                    except Exception:
                        pass
                    self.page.wait_for_timeout(500)
                    return True
        return False
