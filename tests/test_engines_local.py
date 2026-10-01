"""The whole scraper, every engine, through the counting relay, against a fake
Google on 127.0.0.1. No network.

What it proves: each engine types, submits, paginates and is classified and
parsed; and the relay sees the browser's traffic - Chromium bypasses a proxy
for loopback by default, so `<-loopback>` makes it route through.

Skipped per engine when its package or browser is not installed.
"""

import importlib

import fakegoogle
import pytest

from google_search_scraper import browser, engines
from google_search_scraper.scraper import Scraper, Settings

PACKAGES = {"patchright": "patchright.sync_api", "cloak": "cloakbrowser"}


@pytest.fixture
def local_google(monkeypatch):
    server, base = fakegoogle.start()
    monkeypatch.setattr(browser, "HOME_URL", base + "/")
    yield base
    server.shutdown()


@pytest.mark.parametrize("engine", list(engines.ENGINES))
def test_engine_end_to_end_through_the_relay(engine, local_google):
    try:
        importlib.import_module(PACKAGES[engine])
    except ImportError:
        pytest.skip(f"{engine} is not installed")
    settings = Settings(
        engine=engine,
        warmup="off",
        pages=2,
        headless=True,
        gap_seconds=(0, 0),
        dwell_seconds=(0, 0),
        browser_args=("--proxy-bypass-list=<-loopback>",),
        resolve_links=False,  # the fake page's /goto points at real Google
    )
    # cloak is experimental and occasionally fails to start under load, so it
    # gets one retry; patchright gets none.
    attempts = 2 if engine == "cloak" else 1
    for attempt in range(attempts):
        scraper = Scraper(None, settings)
        try:
            records = list(scraper.run(["swim lessons", "pool near me"]))
        except Exception as exc:
            if "Executable doesn't exist" in str(exc) or "not found" in str(exc).lower():
                pytest.skip(f"{engine} browser binary missing: {exc}")
            if attempt + 1 == attempts:
                raise
            continue
        if (
            all(r["search_metadata"]["status"] == "Success" for r in records)
            or attempt + 1 == attempts
        ):
            break
    statuses = [
        (
            r["search_parameters"]["q"],
            r["search_parameters"]["page"],
            r["search_metadata"]["status"],
        )
        for r in records
    ]
    assert statuses == [
        ("swim lessons", 1, "Success"),
        ("swim lessons", 2, "Success"),
        ("pool near me", 1, "Success"),
        ("pool near me", 2, "Success"),
    ]
    assert records[0]["organic_results"][0]["link"].startswith("https://www.usms.org/")
    assert records[0]["exit"]["consent_dismissed"] is True
    # One identity served everything, and the relay counted every page.
    assert {r["exit"]["session"] for r in records} == {records[0]["exit"]["session"]}
    assert all(r["exit"]["bytes"] and r["exit"]["bytes"] > 1000 for r in records)
    summary = scraper.stats.summary()
    assert summary["identities"] == 1 and summary["pages_served"] == 4
    assert summary["traffic_mb"] > 0
