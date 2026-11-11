import importlib
import os
import subprocess
import sys
import warnings
from pathlib import Path

import pytest

from serpmaven import env

LEGACY = Path(__file__).resolve().parent.parent / "legacy"
#: The old import names and the folder of the package that provides each.
OLD_NAMES = {
    "gserp": LEGACY / "gserp" / "src",
    "google_browser_scraper": LEGACY / "google-browser-scraper" / "src",
}


@pytest.fixture
def clean_env(monkeypatch):
    for prefix in env.PREFIXES:
        monkeypatch.delenv(prefix + "PROXY", raising=False)
    return monkeypatch


def test_the_new_name_wins_and_the_old_ones_are_still_read(clean_env):
    assert env.get("PROXY") is None
    assert env.get("PROXY", "x") == "x"
    clean_env.setenv("GBS_PROXY", "oldest")
    assert env.get("PROXY") == "oldest"
    clean_env.setenv("GSERP_PROXY", "old")
    assert env.get("PROXY") == "old"
    clean_env.setenv("SERPMAVEN_PROXY", "new")
    assert env.get("PROXY") == "new"
    clean_env.setenv("SERPMAVEN_PROXY", "")
    assert env.get("PROXY") == "old"


@pytest.mark.parametrize("old", ["GSERP_API_KEY", "GBS_API_KEY"])
def test_an_old_api_key_name_still_lets_serve_listen_publicly(old, monkeypatch):
    from serpmaven import server

    class Listening(Exception):
        pass

    def listen(*args, **kwargs):
        raise Listening

    for prefix in env.PREFIXES:
        monkeypatch.delenv(prefix + "API_KEY", raising=False)
    monkeypatch.setenv(old, "k")
    monkeypatch.setattr(server, "ThreadingHTTPServer", listen)
    with pytest.raises(Listening):
        server.serve(None, host="0.0.0.0", port=0)


@pytest.fixture(params=sorted(OLD_NAMES))
def legacy(request, monkeypatch):
    name = request.param
    monkeypatch.syspath_prepend(str(OLD_NAMES[name]))
    for module in list(sys.modules):
        if module == name or module.startswith(name + "."):
            monkeypatch.delitem(sys.modules, module)
    return name


def test_an_old_import_name_warns_and_is_serpmaven(legacy):
    import serpmaven
    import serpmaven.scraper

    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        old = importlib.import_module(legacy)
    assert any(issubclass(w.category, FutureWarning) for w in caught)
    assert old.Scraper is serpmaven.Scraper and old.__version__ == serpmaven.__version__
    assert importlib.import_module(f"{legacy}.scraper").Settings is serpmaven.scraper.Settings
    # Every public module is aliased, read from the package rather than a list.
    public = {p.stem for p in Path(serpmaven.__file__).parent.glob("*.py")} - {
        "__init__",
        "__main__",
    }
    assert set(old.MODULES) == public
    assert "__main__" not in old.MODULES
    for name in public:
        new = importlib.import_module(f"serpmaven.{name}")
        assert importlib.import_module(f"{legacy}.{name}") is new
        assert getattr(old, name) is new


def test_an_old_module_runs_the_new_cli(legacy):
    import serpmaven

    src = Path(__file__).resolve().parent.parent / "src"
    path = os.pathsep.join([str(OLD_NAMES[legacy]), str(src)])
    out = subprocess.run(
        [sys.executable, "-W", "ignore", "-m", legacy, "--version"],
        capture_output=True,
        text=True,
        env={**os.environ, "PYTHONPATH": path},
        check=True,
    )
    assert out.stdout.strip() == serpmaven.__version__
