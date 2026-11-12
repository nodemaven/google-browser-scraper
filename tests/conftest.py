import pytest

from serpmaven import installer


@pytest.fixture(autouse=True)
def isolated_setup(monkeypatch, tmp_path_factory):
    """No test reads the developer's saved login, and none depends on whether
    this machine has downloaded the browser."""
    monkeypatch.setenv("SERPMAVEN_CONFIG_DIR", str(tmp_path_factory.mktemp("config")))
    monkeypatch.setattr(installer, "browser_path", lambda: None)
