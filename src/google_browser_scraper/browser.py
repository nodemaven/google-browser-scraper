"""One browser identity: one profile, one proxy session, one cookie jar.

- Patchright, headful: headless builds announce `HeadlessChrome` in the UA.
- A persistent context with `no_viewport=True`, so the page reports the real
  screen rather than an emulated one.
- `locale` is never set: setting it through the context makes the main thread
  and Web Workers disagree. `timezone_id` goes through CDP emulation and agrees
  in workers, and is off unless asked for.
- The first query on an exit is typed into the box. Later ones on the same
  exit can go straight to `/search?q=` (`direct=True`): once Google has served
  the exit, it serves those as readily, faster and without the autocomplete
  traffic each keystroke fires.
"""

from __future__ import annotations

import random
import shutil
import tempfile
import time
from dataclasses import dataclass, field, replace
from urllib.parse import urlencode

HOME_URL = "https://www.google.com/?hl=en"
GOOGLE_SEARCH = "https://www.google.com/search"


def results_url(query: str, hl: str = "en", gl: str | None = None, start: int = 0) -> str:
    """Google's results page for `query`: `hl` is the interface language, `gl`
    the country the results are for (Google picks one from the exit if unset),
    `start` the first result's offset (10 for the second page)."""
    params = {"q": query, "hl": hl}
    if gl:
        params["gl"] = gl
    if start:
        params["start"] = str(start)
    return "https://www.google.com/search?" + urlencode(params)


#: How a query gets into the search box: key by key, all at once, or one word
#: at a time.
INPUT_MODES = ("type", "insert", "words")
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
TYPE_ATTEMPTS = 2


@dataclass(frozen=True)
class Fetched:
    """What one navigation left on screen."""

    url: str
    status: int | None
    html: str
    consent_dismissed: bool = False
    #: Seconds per step, e.g. {"type": 3.1, "load": 4.2}: where the time went.
    timings: dict[str, float] = field(default_factory=dict)


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
    #: Whether the hybrid (see `hybrid`) can drive this session.
    hybrid_capable = True

    def __init__(
        self,
        proxy: dict[str, str] | None = None,
        *,
        headless: bool = False,
        channel: str | None = None,
        timezone_id: str | None = None,
        browser_args: list[str] | tuple[str, ...] = (),
        rng: random.Random | None = None,
        profile_dir: str | None = None,
        input_mode: str = "type",
        hl: str = "en",
        gl: str | None = None,
    ) -> None:
        if input_mode not in INPUT_MODES:
            raise ValueError(f"input mode must be one of {', '.join(INPUT_MODES)}")
        self._input = input_mode
        self._hl = hl
        self._gl = gl
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
        # A given directory (a copy of another identity's profile) is used as
        # it is; either way the profile is removed on close.
        self._profile = profile_dir
        self.page = None
        #: A file to record Chromium's NetLog into from the next launch; the
        #: hybrid reads its request headers from one.
        self.netlog: str | None = None

    @property
    def hl(self) -> str:
        return self._hl

    @property
    def gl(self) -> str | None:
        return self._gl

    def launch_args(self) -> list[str] | None:
        args = list(self._args)
        if self.netlog:
            args += [f"--log-net-log={self.netlog}", "--net-log-capture-mode=IncludeSensitive"]
        return args or None

    @property
    def profile_dir(self) -> str | None:
        return self._profile

    def __enter__(self) -> BrowserSession:
        if self._profile is None:
            self._profile = tempfile.mkdtemp(prefix="gbs-profile-")
        try:
            self._open()
        except Exception:
            self.close()
            raise
        return self

    def _open(self) -> None:
        self._context = self._launch(self._profile)
        pages = self._context.pages
        self.page = pages[0] if pages else self._context.new_page()

    def suspend(self) -> str:
        """Close the browser and keep its profile on disk, so it can be
        copied; `resume()` opens it again on the same proxy."""
        self._shut()
        return self._profile

    def resume(self) -> None:
        self._open()

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
            args=self.launch_args(),
        )

    def __exit__(self, *exc) -> None:
        self.close()

    def close(self) -> None:
        self._shut()
        if self._profile:
            shutil.rmtree(self._profile, ignore_errors=True)
        self._profile = None

    def _shut(self) -> None:
        for step in (
            lambda: self._context and self._context.close(),
            lambda: self._pw and self._pw.stop(),
        ):
            try:
                step()
            except Exception:
                pass
        self._context = self._pw = self.page = None

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

    def search(
        self, query: str, direct: bool = False, hl: str | None = None, gl: str | None = None
    ) -> Fetched:
        """Type `query` into Google's box and return the page it led to; with
        `direct`, open the results page for it instead. `hl` and `gl` override
        the session's for this query, and open the results page directly."""
        if direct or hl or gl:
            return self._search_direct(query, hl=hl, gl=gl)
        page = self.page
        timings: dict[str, float] = {}
        mark = time.monotonic()
        if not self._box_visible():
            page.goto(HOME_URL, wait_until="domcontentloaded", timeout=NAV_TIMEOUT_MS)
            timings["home"] = round(time.monotonic() - mark, 2)
            mark = time.monotonic()
        dismissed, typed = False, None
        # The page can take focus or swap the box while the query is being
        # typed, which leaves only its first characters behind. The box is
        # found again and the query retyped once before giving up.
        for _ in range(TYPE_ATTEMPTS):
            dismissed = self._dismiss_consent() or dismissed
            box = self._type_query(query)
            try:
                typed = box.input_value()
            except Exception:
                typed = None  # the box was replaced and this handle is gone
            if typed == query:
                break
        else:
            raise RuntimeError(
                f"search box holds {typed!r} after typing {query!r} "
                f"{TYPE_ATTEMPTS} times; not submitting"
            )
        timings["type"] = round(time.monotonic() - mark, 2)
        mark = time.monotonic()
        # `no_wait_after=True` keeps `press` from waiting for the navigation a
        # second time with Playwright's own 30 s default under ours.
        with page.expect_navigation(wait_until="domcontentloaded", timeout=NAV_TIMEOUT_MS) as nav:
            box.press("Enter", no_wait_after=True, timeout=NAV_TIMEOUT_MS)
        timings["load"] = round(time.monotonic() - mark, 2)
        fetched = self._snapshot(nav.value, dismissed)
        return replace(fetched, timings={**timings, **fetched.timings})

    def open_results(
        self, query: str, start: int = 0, hl: str | None = None, gl: str | None = None
    ) -> Fetched:
        """Open the results page for `query` from result `start` on."""
        return self._search_direct(query, start, hl=hl, gl=gl)

    def google_cookies(self) -> str:
        """The cookies the browser would send to Google search, as one header."""
        cookies = self._context.cookies(GOOGLE_SEARCH)
        return "; ".join(f"{c['name']}={c['value']}" for c in cookies)

    def set_google_cookies(self, header: str) -> None:
        """Take the values in a cookie header, keeping each cookie's attributes."""
        known = {c["name"]: c for c in self._context.cookies(GOOGLE_SEARCH)}
        update = []
        for part in header.split(";"):
            if "=" not in part:
                continue
            name, value = part.strip().split("=", 1)
            base = known.get(name)
            if base is not None and base["value"] == value:
                continue
            cookie = dict(base) if base else {"domain": ".google.com", "path": "/", "secure": True}
            cookie.update(name=name, value=value)
            cookie.pop("partitionKey", None)
            update.append(cookie)
        if update:
            self._context.add_cookies(update)

    def _search_direct(
        self, query: str, start: int = 0, hl: str | None = None, gl: str | None = None
    ) -> Fetched:
        url = results_url(query, hl or self._hl, gl or self._gl, start)
        mark = time.monotonic()
        response = self.page.goto(url, wait_until="domcontentloaded", timeout=NAV_TIMEOUT_MS)
        # A fresh exit can be shown the consent wall instead of results; clear
        # it and open the results again, as the typed path does on the front page.
        dismissed = self._dismiss_consent()
        if dismissed:
            response = self.page.goto(url, wait_until="domcontentloaded", timeout=NAV_TIMEOUT_MS)
        load = round(time.monotonic() - mark, 2)
        fetched = self._snapshot(response, dismissed)
        return replace(fetched, timings={"load": load, **fetched.timings})

    def _box_visible(self) -> bool:
        try:
            return any(h.is_visible() for h in self.page.query_selector_all(SEARCH_BOX))
        except Exception:
            # The page navigated while it was being read, as a warm-up page
            # left idle can; the front page is loaded instead.
            return False

    def _type_query(self, query: str):
        page = self.page
        # Act on the handle that was found, never on the selector again: the
        # selector also matches a hidden field, and `page.click` would pick it.
        box = page.wait_for_selector(SEARCH_BOX, state="visible", timeout=NAV_TIMEOUT_MS)
        box.click()
        # A results page keeps the previous query in the box, and a failed
        # attempt leaves part of this one. Typing after either appends, and the
        # appended query returns a real page for the wrong question. Select it
        # so the typing replaces it.
        if box.input_value():
            box.press("ControlOrMeta+a")
        if self._input == "insert":
            page.keyboard.insert_text(query)
        elif self._input == "words":
            words = query.split(" ")
            for i, word in enumerate(words):
                last = i == len(words) - 1
                page.keyboard.insert_text(word if last else word + " ")
                if not last:
                    time.sleep(self._rng.uniform(0.15, 0.5))
        else:
            box.type(query, delay=self._rng.randint(45, 140))
        return box

    def blank(self) -> None:
        """Leave the current page, stopping whatever it is still loading."""
        self.page.goto("about:blank")

    def next_page(self) -> Fetched | None:
        """Click "Next" on the current results page, if there is one."""
        link = self.page.query_selector("a#pnnext")
        if link is None or not link.is_visible():
            return None
        mark = time.monotonic()
        with self.page.expect_navigation(
            wait_until="domcontentloaded", timeout=NAV_TIMEOUT_MS
        ) as nav:
            link.click()
        load = round(time.monotonic() - mark, 2)
        fetched = self._snapshot(nav.value, False)
        return replace(fetched, timings={"load": load, **fetched.timings})

    def _snapshot(self, response, dismissed: bool) -> Fetched:
        # Google builds results in the browser; reading at domcontentloaded
        # catches the "enable JavaScript" scaffold. A refusal never grows the
        # container, so the short timeout is expected to expire on those.
        mark = time.monotonic()
        try:
            self.page.wait_for_selector(READY, state="attached", timeout=8_000)
        except Exception:
            pass
        ready = round(time.monotonic() - mark, 2)
        mark = time.monotonic()
        html = self.page.content()
        return Fetched(
            url=self.page.url,
            status=response.status if response is not None else None,
            html=html,
            consent_dismissed=dismissed,
            timings={"ready": ready, "read": round(time.monotonic() - mark, 2)},
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
