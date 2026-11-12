"""`serpmaven login`, `logout` and `install`, and the browser check before a run."""

from __future__ import annotations

import json
import os
import sys

import pytest

from serpmaven import cli, config, installer, proxy


@pytest.fixture
def no_env_login(monkeypatch):
    for name in ("NODEMAVEN_LOGIN", "NODEMAVEN_PASSWORD"):
        monkeypatch.delenv(name, raising=False)
    for prefix in ("SERPMAVEN_", "GSERP_", "GBS_"):
        monkeypatch.delenv(prefix + "PROXY", raising=False)


def _args(argv):
    return cli._parser().parse_args(argv)


def test_login_saves_the_nodemaven_login_readable_by_the_owner_only(no_env_login):
    answers = iter(["  user-abc  "])
    code = cli._login(_args(["login"]), prompt=lambda _: next(answers), secret=lambda _: "pa ss ")
    assert code == 0
    assert json.loads(config.path().read_text()) == {
        "nodemaven_login": "user-abc",
        "nodemaven_password": "pa ss",
    }
    if sys.platform != "win32":
        assert os.stat(config.path()).st_mode & 0o777 == 0o600
    assert config.nodemaven_credentials() == ("user-abc", "pa ss")


def test_the_environment_wins_over_the_saved_login(no_env_login, monkeypatch):
    config.save({"nodemaven_login": "saved", "nodemaven_password": "p"})
    monkeypatch.setenv("NODEMAVEN_LOGIN", "env")
    monkeypatch.setenv("NODEMAVEN_PASSWORD", "q")
    assert config.nodemaven_credentials() == ("env", "q")


def test_login_refuses_an_empty_answer(no_env_login):
    with pytest.raises(ValueError, match="both"):
        cli._login(_args(["login"]), prompt=lambda _: "", secret=lambda _: "x")
    assert not config.path().exists()


def test_login_with_a_proxy_url_saves_it_and_refuses_a_malformed_one(no_env_login):
    cli._login(_args(["login", "--proxy", "http://u-{session}:p@gate.example.com:7000"]))
    assert config.saved_proxy() == "http://u-{session}:p@gate.example.com:7000"
    with pytest.raises(ValueError):
        cli._login(_args(["login", "--proxy", "ftp://nope"]))


def test_logout_deletes_the_login(no_env_login, capsys):
    config.save({"proxy": "http://u-{session}:p@h:1"})
    assert cli.main(["logout"]) == 0
    assert not config.path().exists()
    assert cli.main(["logout"]) == 0
    assert "no saved login" in capsys.readouterr().out


def test_a_saved_nodemaven_login_is_the_default_source(no_env_login):
    pytest.importorskip("nodemaven")
    config.save({"nodemaven_login": "user", "nodemaven_password": "pw"})
    args = _args(["search", "q", "--country", "us"])
    assert cli._source_kind(args) == ("nodemaven", None)
    with cli._proxy_source(args) as source:
        assert isinstance(source, proxy.NodeMavenSource)
        assert "country=us" in source.describe()


def test_a_saved_proxy_url_is_the_default_source(no_env_login):
    config.save({"proxy": "http://u-{session}:p@gate.example.com:7000"})
    assert cli._source_kind(_args(["search", "q"])) == (
        "url",
        "http://u-{session}:p@gate.example.com:7000",
    )


def test_flags_and_the_environment_win_over_the_saved_login(no_env_login, monkeypatch):
    config.save({"nodemaven_login": "user", "nodemaven_password": "pw"})
    assert cli._source_kind(_args(["search", "q", "--no-proxy"])) == ("none", None)
    monkeypatch.setenv("SERPMAVEN_PROXY", "http://e-{session}:p@h:1")
    assert cli._source_kind(_args(["search", "q"])) == ("url", "http://e-{session}:p@h:1")


def test_no_login_at_all_says_to_run_login(no_env_login, capsys):
    assert cli.main(["search", "q"]) == 1
    assert "serpmaven login" in capsys.readouterr().err


def test_country_without_nodemaven_is_refused(no_env_login):
    config.save({"proxy": "http://u-{session}:p@h:1"})
    with pytest.raises(ValueError, match="NodeMaven"):
        with cli._proxy_source(_args(["search", "q", "--country", "us"])):
            pass


def test_install_runs_patchrights_installer():
    seen = []
    assert installer.install(run=lambda cmd: seen.append(cmd) or 0) == 0
    assert seen[0][1:] == ["-m", "patchright", "install", "chromium"]
    installer.install(with_deps=True, run=lambda cmd: seen.append(cmd) or 0)
    assert seen[1][-2:] == ["--with-deps", "chromium"]


def test_a_missing_browser_is_downloaded_after_a_yes():
    state = {"there": False}

    def download():
        state["there"] = True
        return 0

    installer.ensure_browser(
        installed=lambda: state["there"],
        prompt=lambda _: "",
        do_install=download,
        can_ask=lambda: True,
    )
    assert state["there"]


@pytest.mark.parametrize(
    ("answer", "can_ask", "ask"),
    [("n", True, True), ("", False, True), ("", True, False)],
)
def test_a_missing_browser_without_a_yes_is_an_error_naming_the_command(answer, can_ask, ask):
    downloads = []
    with pytest.raises(RuntimeError, match="serpmaven install"):
        installer.ensure_browser(
            ask=ask,
            installed=lambda: False,
            prompt=lambda _: answer,
            do_install=lambda: downloads.append(1) or 0,
            can_ask=lambda: can_ask,
        )
    assert downloads == []


def test_a_failed_download_is_an_error():
    with pytest.raises(RuntimeError, match="did not finish"):
        installer.ensure_browser(
            installed=lambda: False,
            prompt=lambda _: "y",
            do_install=lambda: 1,
            can_ask=lambda: True,
        )


def test_search_checks_the_browser_before_it_starts(no_env_login, monkeypatch, capsys):
    config.save({"proxy": "http://u-{session}:p@h:1"})
    monkeypatch.setattr(installer, "browser_installed", lambda path_of=None: False)
    monkeypatch.setattr(installer, "interactive", lambda: False)
    built = []
    monkeypatch.setattr(cli, "_build", lambda *a, **k: built.append(1))
    assert cli.main(["search", "q"]) == 1
    assert "Run: serpmaven install" in capsys.readouterr().err
    assert built == []


def test_browser_installed_trusts_a_path_on_disk(tmp_path):
    exe = tmp_path / "chrome"
    assert not installer.browser_installed(lambda: str(exe))
    exe.write_text("")
    assert installer.browser_installed(lambda: str(exe))
    assert installer.browser_installed(lambda: None)
