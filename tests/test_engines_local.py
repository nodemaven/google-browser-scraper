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

from google_browser_scraper import browser, engines
from google_browser_scraper.scraper import Scraper, Settings

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
        direct="follow-ups",  # the typed path, with the front page's consent overlay
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


def test_a_spare_browser_launched_on_another_thread_serves_the_next_query(local_google):
    try:
        importlib.import_module(PACKAGES["patchright"])
    except ImportError:
        pytest.skip("patchright is not installed")
    settings = Settings(
        warmup="off",
        max_queries_per_exit=1,
        spare=1,
        headless=True,
        gap_seconds=(0, 0),
        dwell_seconds=(0, 0),
        browser_args=("--proxy-bypass-list=<-loopback>",),
        resolve_links=False,
    )
    scraper = Scraper(None, settings)
    records = list(scraper.run(["swim lessons", "pool near me"]))
    assert [r["search_metadata"]["status"] for r in records] == ["Success", "Success"]
    first, second = (r["exit"]["session"] for r in records)
    assert first != second  # the capped exit was replaced by the spare
    assert scraper.stats.summary()["identities"] == 2


def test_a_pool_of_real_browsers_shares_the_queries(local_google):
    """Two patchright workers, each on its own thread, against the fake Google."""
    try:
        importlib.import_module(PACKAGES["patchright"])
    except ImportError:
        pytest.skip("patchright is not installed")
    from google_browser_scraper.pool import Pool

    settings = Settings(
        warmup="off",
        headless=True,
        gap_seconds=(0, 0),
        dwell_seconds=(0, 0),
        browser_args=("--proxy-bypass-list=<-loopback>",),
        resolve_links=False,
    )
    pool = Pool(lambda i: Scraper(None, settings), 2, start_gap=0)
    queries = ["swim lessons", "pool near me", "kimchi", "tennis serve"]
    records = list(pool.run(queries))
    assert sorted(r["search_parameters"]["q"] for r in records) == sorted(queries)
    assert all(r["search_metadata"]["status"] == "Success" for r in records)
    assert {r["search_metadata"]["worker"] for r in records} == {0, 1}


def test_a_copied_profile_carries_the_served_session_to_a_new_browser(local_google):
    """Real patchright: copy a served profile, then search from the copy."""
    try:
        importlib.import_module(PACKAGES["patchright"])
    except ImportError:
        pytest.skip("patchright is not installed")
    settings = Settings(
        warmup="off",
        headless=True,
        clones=2,
        max_queries_per_exit=2,
        gap_seconds=(0, 0),
        dwell_seconds=(0, 0),
        settle_seconds=(0, 0),
        browser_args=("--proxy-bypass-list=<-loopback>",),
        resolve_links=False,
    )
    events = []
    scraper = Scraper(None, settings, on_event=events.append)
    records = list(scraper.run(["swim lessons", "pool near me", "kimchi"]))
    assert [r["search_metadata"]["status"] for r in records] == ["Success"] * 3
    kinds = [e["event"] for e in events]
    assert "template" in kinds
    minted = [e for e in events if e["event"] == "minted"]
    assert len(minted) == 2 and minted[1].get("clone") is True
    assert scraper.stats.clones == 1
