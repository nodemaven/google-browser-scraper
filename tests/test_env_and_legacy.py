import importlib
import os
import subprocess
import sys
import warnings
from pathlib import Path

import pytest

from gserp import env

LEGACY_SRC = Path(__file__).resolve().parent.parent / "legacy" / "src"


@pytest.fixture
def clean_env(monkeypatch):
    for prefix in env.PREFIXES:
        monkeypatch.delenv(prefix + "PROXY", raising=False)
    return monkeypatch


def test_the_new_name_wins_and_the_old_one_is_still_read(clean_env):
    assert env.get("PROXY") is None
    assert env.get("PROXY", "x") == "x"
    clean_env.setenv("GBS_PROXY", "old")
    assert env.get("PROXY") == "old"
    clean_env.setenv("GSERP_PROXY", "new")
    assert env.get("PROXY") == "new"
    clean_env.setenv("GSERP_PROXY", "")
    assert env.get("PROXY") == "old"


def test_the_old_api_key_name_still_lets_serve_listen_publicly(monkeypatch):
    from gserp import server

    class Listening(Exception):
        pass

    def listen(*args, **kwargs):
        raise Listening

    monkeypatch.delenv("GSERP_API_KEY", raising=False)
    monkeypatch.setenv("GBS_API_KEY", "k")
    monkeypatch.setattr(server, "ThreadingHTTPServer", listen)
    with pytest.raises(Listening):
        server.serve(None, host="0.0.0.0", port=0)


@pytest.fixture
def legacy(monkeypatch):
    monkeypatch.syspath_prepend(str(LEGACY_SRC))
    for name in list(sys.modules):
        if name == "google_browser_scraper" or name.startswith("google_browser_scraper."):
            monkeypatch.delitem(sys.modules, name)
    return LEGACY_SRC


def test_the_old_import_name_warns_and_is_gserp(legacy):
    import gserp
    import gserp.scraper

    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        old = importlib.import_module("google_browser_scraper")
    assert any(issubclass(w.category, FutureWarning) for w in caught)
    assert old.Scraper is gserp.Scraper and old.__version__ == gserp.__version__
    from google_browser_scraper.scraper import Settings

    assert Settings is gserp.scraper.Settings
    # Every public module is aliased, read from the package rather than a list.
    public = {p.stem for p in Path(gserp.__file__).parent.glob("*.py")} - {"__init__", "__main__"}
    assert set(old.MODULES) == public
    assert "__main__" not in old.MODULES
    for name in public:
        new = importlib.import_module(f"gserp.{name}")
        assert importlib.import_module(f"google_browser_scraper.{name}") is new
        assert getattr(old, name) is new


def test_the_old_module_runs_the_new_cli(legacy):
    import gserp

    src = Path(__file__).resolve().parent.parent / "src"
    environ = {**os.environ, "PYTHONPATH": os.pathsep.join([str(legacy), str(src)])}
    out = subprocess.run(
        [sys.executable, "-W", "ignore", "-m", "google_browser_scraper", "--version"],
        capture_output=True,
        text=True,
        env=environ,
        check=True,
    )
    assert out.stdout.strip() == gserp.__version__
