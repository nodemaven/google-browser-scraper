import pytest

from google_browser_scraper.proxy import ProxyTemplate, new_session_id


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
