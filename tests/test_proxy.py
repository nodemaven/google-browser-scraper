import sys
import types

import pytest

from serpmaven.proxy import NodeMavenDemoSource, NodeMavenSource, ProxyTemplate, new_session_id


def test_session_placeholder_is_filled_per_identity():
    template = ProxyTemplate("http://user-session-{session}:p%40ss@gate.example.com:7000")
    assert template.sticky
    assert template.for_session("abc123") == {
        "server": "http://gate.example.com:7000",
        "username": "user-session-abc123",
        "password": "p@ss",
    }


def test_describe_never_shows_the_password():
    described = ProxyTemplate("http://user-{session}:secret@gate.example.com:7000").describe()
    assert "secret" not in described
    assert "sticky" in described


def test_without_placeholder_it_is_reported_as_rotating():
    template = ProxyTemplate("http://user:pass@gate.example.com:7000")
    assert not template.sticky
    assert "rotating" in template.describe()


@pytest.mark.parametrize(
    "url",
    [
        "ftp://user:pass@gate.example.com:21",
        "http://gate.example.com",
        "socks5://user:pass@gate.example.com:1080",
    ],
)
def test_unusable_urls_are_refused(url):
    with pytest.raises(ValueError):
        ProxyTemplate(url)


def test_socks5_without_credentials_is_allowed():
    assert ProxyTemplate("socks5://gate.example.com:1080").for_session("x") == {
        "server": "socks5://gate.example.com:1080"
    }


def test_session_ids_are_distinct_hex():
    ids = {new_session_id() for _ in range(200)}
    assert len(ids) == 200
    assert all(len(i) == 12 and int(i, 16) >= 0 for i in ids)


class _FakeNodeMavenProxy:
    def __init__(self, login=None, password=None, **params):
        self.login, self.password, self.params = login, password, params

    sessions = []

    def session(self, session_id):
        type(self).sessions.append(session_id)
        return self

    def playwright(self):
        return {
            "server": "http://gate.example.com:7000",
            "username": self.login,
            "password": self.password,
        }


def _fake_client_cls(created, deleted):
    class _FakeClient:
        def __init__(self, api_key=None):
            self.api_key = api_key

        def create_sub_user(
            self, proxy_username, proxy_password, *, traffic_limit=None, is_traffic_limited=None
        ):
            created.append((proxy_username, proxy_password, traffic_limit, is_traffic_limited))
            return {
                "id": "sub-1",
                "proxy_username": proxy_username,
                "proxy_password": proxy_password,
            }

        def delete_sub_user(self, sub_user_id):
            deleted.append(sub_user_id)

    return _FakeClient


def _install_fake_nodemaven(monkeypatch, created, deleted):
    fake_pkg = types.ModuleType("nodemaven")
    fake_pkg.Proxy = _FakeNodeMavenProxy
    fake_api = types.ModuleType("nodemaven.api")
    fake_api.Client = _fake_client_cls(created, deleted)
    monkeypatch.setitem(sys.modules, "nodemaven", fake_pkg)
    monkeypatch.setitem(sys.modules, "nodemaven.api", fake_api)


def test_demo_source_provisions_a_sub_user_and_deletes_it_on_close(monkeypatch):
    created, deleted = [], []
    _install_fake_nodemaven(monkeypatch, created, deleted)
    monkeypatch.setenv("NODEMAVEN_API_KEY", "key-123")
    source = NodeMavenDemoSource(traffic_limit_mb=10, country="us")
    assert source.sticky
    assert "capped" in source.describe()
    username, password, limit, limited = created[0]
    assert len(created) == 1 and limit == 10 * 1024 * 1024 and limited is True
    assert source.for_session("abc") == {
        "server": "http://gate.example.com:7000",
        "username": username,
        "password": password,
    }
    assert _FakeNodeMavenProxy.sessions[-1] == "serpmavenabc"
    source.close()
    assert deleted == ["sub-1"]


def test_nodemaven_session_ids_carry_the_scraper_tag_and_other_proxies_do_not(monkeypatch):
    _install_fake_nodemaven(monkeypatch, [], [])
    monkeypatch.setenv("NODEMAVEN_LOGIN", "user")
    monkeypatch.setenv("NODEMAVEN_PASSWORD", "pass")
    NodeMavenSource(country="us").for_session("0a1b2c3d4e5f")
    assert _FakeNodeMavenProxy.sessions[-1] == "serpmaven0a1b2c3d4e5f"
    plain = ProxyTemplate("http://u-{session}:p@gate.example.com:7000").for_session("0a1b2c")
    assert plain["username"] == "u-0a1b2c"


def test_demo_source_close_with_keep_does_not_delete(monkeypatch):
    created, deleted = [], []
    _install_fake_nodemaven(monkeypatch, created, deleted)
    monkeypatch.setenv("NODEMAVEN_API_KEY", "key-123")
    source = NodeMavenDemoSource()
    source.close(keep=True)
    assert deleted == []


def test_demo_source_close_retries_a_failed_delete(monkeypatch):
    created, deleted = [], []
    _install_fake_nodemaven(monkeypatch, created, deleted)
    monkeypatch.setenv("NODEMAVEN_API_KEY", "key-123")
    source = NodeMavenDemoSource()
    delete, failures = source._client.delete_sub_user, [OSError("timeout")]

    def flaky(sub_user_id):
        if failures:
            raise failures.pop()
        delete(sub_user_id)

    source._client.delete_sub_user = flaky
    with pytest.raises(OSError):
        source.close()
    source.close()
    assert deleted == ["sub-1"]


def test_demo_source_needs_an_api_key(monkeypatch):
    _install_fake_nodemaven(monkeypatch, [], [])
    monkeypatch.delenv("NODEMAVEN_API_KEY", raising=False)
    with pytest.raises(RuntimeError):
        NodeMavenDemoSource()


@pytest.mark.parametrize("cap", [0, -5, float("inf"), float("nan")])
def test_demo_source_refuses_a_bad_cap_before_provisioning(monkeypatch, cap):
    created = []
    _install_fake_nodemaven(monkeypatch, created, [])
    monkeypatch.setenv("NODEMAVEN_API_KEY", "key-123")
    with pytest.raises(ValueError):
        NodeMavenDemoSource(traffic_limit_mb=cap)
    assert created == []


def test_demo_source_refuses_an_sdk_with_no_create_sub_user(monkeypatch):
    monkeypatch.setenv("NODEMAVEN_API_KEY", "key-123")
    fake_pkg = types.ModuleType("nodemaven")
    fake_pkg.Proxy = _FakeNodeMavenProxy
    fake_api = types.ModuleType("nodemaven.api")

    class _OldClient:
        def __init__(self, api_key=None):
            pass

    fake_api.Client = _OldClient
    monkeypatch.setitem(sys.modules, "nodemaven", fake_pkg)
    monkeypatch.setitem(sys.modules, "nodemaven.api", fake_api)
    with pytest.raises(RuntimeError):
        NodeMavenDemoSource()
