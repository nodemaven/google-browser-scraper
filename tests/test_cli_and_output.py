import io
import json

import pages
import pytest

from google_browser_scraper import cli
from google_browser_scraper.classify import classify
from google_browser_scraper.doctor import FAIL, PASS, WARN, assess
from google_browser_scraper.output import build_record, open_writer
from google_browser_scraper.parse import parse_serp


def record():
    page = pages.results_page()
    return build_record(
        "swimming",
        page=1,
        verdict=classify("https://www.google.com/search", page),
        url="https://www.google.com/search?q=swimming",
        elapsed=1.234,
        exit_info={"session": "abc"},
        parsed=parse_serp(page),
    )


def test_record_is_serp_api_shaped():
    r = record()
    assert r["search_metadata"]["status"] == "Success"
    assert r["search_parameters"] == {
        "engine": "google",
        "q": "swimming",
        "page": 1,
        "hl": "en",
        "device": "desktop",
    }
    for key in ("organic_results", "ads", "related_questions", "related_searches", "pagination"):
        assert key in r
    assert "error" not in r


def test_jsonl_and_csv_writers():
    out = io.StringIO()
    writer = open_writer("jsonl", out)
    writer.write(record())
    assert json.loads(out.getvalue())["search_parameters"]["q"] == "swimming"

    out = io.StringIO()
    open_writer("csv", out).write(record())
    lines = out.getvalue().splitlines()
    assert lines[0].startswith("query,page,position,title,link")
    assert len(lines) == 4


def test_parse_command_reads_a_saved_page(tmp_path, capsys):
    saved = tmp_path / "serp.html"
    saved.write_text(pages.results_page(), encoding="utf-8")
    assert cli.main(["parse", str(saved)]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["page_verdict"] == "ok"
    assert len(out["organic_results"]) == 3


def test_parse_command_refuses_to_parse_a_refusal(tmp_path, capsys):
    saved = tmp_path / "sorry.html"
    saved.write_text(pages.SORRY_PAGE, encoding="utf-8")
    assert cli.main(["parse", str(saved)]) == 4
    assert "organic_results" not in json.loads(capsys.readouterr().out)


@pytest.mark.parametrize(
    "argv",
    [
        ["search"],
        ["search", "q"],
        ["search", "q", "--proxy", "http://u:p@h:1", "--country", "us"],
        ["search", "q", "--no-proxy", "--spare", "9"],
    ],
)
def test_search_argument_errors(argv, capsys, monkeypatch):
    monkeypatch.delenv("GBS_PROXY", raising=False)
    assert cli.main(argv) == 1
    assert capsys.readouterr().err.startswith("error:")


def test_doctor_assessment():
    windows = {
        "user_agent": "Mozilla/5.0 (Windows NT 10.0) Chrome/151.0.0.0",
        "platform": "Win32",
        "webdriver": False,
        "webgl_renderer": "ANGLE (Intel, UHD Graphics, D3D11)",
        "hardware_concurrency": 12,
        "screen": "1920x1080",
        "color_depth": 24,
    }
    assert {c.status for c in assess(windows)} == {PASS}

    linux_server = dict(
        windows,
        platform="Linux x86_64",
        webgl_renderer=None,
        hardware_concurrency=2,
        screen="1280x1024",
    )
    by_name = {c.name: c.status for c in assess(linux_server)}
    assert by_name["WebGL"] == WARN
    assert by_name["CPU cores"] == WARN and by_name["platform"] == WARN

    headless = dict(windows, user_agent="Mozilla/5.0 HeadlessChrome/151.0.0.0")
    assert {c.name: c.status for c in assess(headless)}["user agent"] == FAIL


def test_a_query_file_saved_with_a_byte_order_mark(tmp_path, monkeypatch):
    # Windows tools (PowerShell 5 `Out-File -Encoding utf8`, older Notepad)
    # write a BOM; it must not end up in the first query.
    asked = []

    class Recorder:
        stats = type("Stats", (), {"summary": lambda self, price: {}})()

        def run(self, queries):
            asked.extend(queries)
            return iter(())

    monkeypatch.setattr(cli, "_build", lambda args, proxy, **kw: (Recorder(), ""))
    path = tmp_path / "q.txt"
    path.write_bytes("\ufeffbest pizza\nkimchi\n".encode("utf-8"))
    assert cli.main(["search", "-f", str(path), "--no-proxy"]) == 0
    assert asked == ["best pizza", "kimchi"]


def test_verbose_events_carry_a_timestamp(capsys):
    args = cli._parser().parse_args(["search", "q", "--no-proxy", "-v"])
    scraper, _ = cli._build(args, None)
    scraper._emit({"event": "retired", "session": "abc"})
    event = json.loads(capsys.readouterr().err.splitlines()[-1])
    assert event["event"] == "retired" and isinstance(event["ts"], float)


def test_workers_share_one_demo_sub_user(monkeypatch):
    made, closed, given = [], [], []

    class Demo:
        sticky = True

        def __init__(self, **kw):
            made.append(self)

        def close(self, keep=False):
            closed.append(keep)

    class FakePool:
        stats = type("Stats", (), {"summary": lambda self, price: {}})()

        def __init__(self, make, workers):
            self.make, self.workers = make, workers

        def run(self, queries):
            for i in range(self.workers):
                self.make(i)
            return iter(())

    def build(args, proxy, **kw):
        given.append(proxy)
        return None, ""

    monkeypatch.setattr(cli, "NodeMavenDemoSource", Demo)
    monkeypatch.setattr(cli, "Pool", FakePool)
    monkeypatch.setattr(cli, "_build", build)
    assert cli.main(["search", "q", "--nodemaven-demo", "--workers", "3"]) == 0
    assert len(made) == 1 and given == made * 3 and closed == [False]


def test_a_bad_output_path_still_deletes_the_demo_sub_user(tmp_path, monkeypatch):
    closed = []

    class Demo:
        sticky = True

        def __init__(self, **kw):
            pass

        def close(self, keep=False):
            closed.append(keep)

    monkeypatch.setattr(cli, "NodeMavenDemoSource", Demo)
    monkeypatch.setattr(cli, "_build", lambda args, proxy, **kw: (None, ""))
    missing = tmp_path / "no-such-dir" / "out.jsonl"
    with pytest.raises(OSError):
        cli.main(["search", "q", "--nodemaven-demo", "-o", str(missing)])
    assert closed == [False]


def test_copies_are_on_by_default_and_can_be_turned_off():
    parser = cli._parser()
    assert parser.parse_args(["search", "q", "--no-proxy"]).clone == 10
    assert parser.parse_args(["search", "q", "--no-proxy", "--clone", "0"]).clone == 0
