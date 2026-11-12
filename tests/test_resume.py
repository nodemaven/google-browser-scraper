import json

import pytest

from serpmaven import cli
from serpmaven.output import answered


def _line(query, status="Success", page=1):
    record = {
        "search_metadata": {"status": status},
        "search_parameters": {"q": query, "page": page},
    }
    return json.dumps(record) + "\n"


def test_answered_keeps_served_queries_and_cuts_a_line_a_killed_run_left_half_written(tmp_path):
    out = tmp_path / "out.jsonl"
    out.write_text(
        _line("pizza")
        + _line("pizza", page=2)
        + _line("tacos", "Error")
        + _line("ramen")
        + _line("ramen", "Error", page=2)
        + "not json\n"
        + '{"search_metadata": {"status": "Succ',
        encoding="utf-8",
    )
    # A query with any failed page is asked again.
    assert answered(out) == {"pizza"}
    assert out.read_text(encoding="utf-8").endswith("not json\n")
    assert answered(tmp_path / "missing.jsonl") == set()


class _Recorder:
    stats = type("Stats", (), {"summary": lambda self, price: {}})()

    def __init__(self):
        self.asked = []

    def run(self, queries):
        for q in queries:
            self.asked.append(q)
            yield json.loads(_line(q))


def test_resume_asks_only_what_the_output_lacks_and_appends(tmp_path, monkeypatch):
    recorder = _Recorder()
    monkeypatch.setattr(cli, "_build", lambda args, proxy, **kw: (recorder, ""))
    out = tmp_path / "out.jsonl"
    out.write_text(_line("pizza") + _line("tacos", "Error"), encoding="utf-8")
    argv = ["search", "pizza", "tacos", "ramen", "--no-proxy", "-o", str(out), "--resume"]
    assert cli.main(argv) == 0
    assert recorder.asked == ["tacos", "ramen"]
    lines = out.read_text(encoding="utf-8").splitlines()
    assert [json.loads(x)["search_parameters"]["q"] for x in lines] == [
        "pizza",
        "tacos",
        "tacos",
        "ramen",
    ]


def test_resume_with_nothing_left_starts_nothing(tmp_path, monkeypatch, capsys):
    def no_build(*a, **kw):
        raise AssertionError("nothing to ask, nothing to start")

    monkeypatch.setattr(cli, "_build", no_build)
    out = tmp_path / "out.jsonl"
    out.write_text(_line("pizza"), encoding="utf-8")
    assert cli.main(["search", "pizza", "--no-proxy", "-o", str(out), "--resume"]) == 0
    assert "1 of 1 queries already in" in capsys.readouterr().err


@pytest.mark.parametrize("extra", [[], ["-o", "out.csv", "--format", "csv"]])
def test_resume_needs_a_jsonl_output_file(extra, tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    assert cli.main(["search", "pizza", "--no-proxy", "--resume", *extra]) == 1
    assert "--resume needs --output" in capsys.readouterr().err


def test_a_location_google_cannot_take_is_refused_before_anything_starts(capsys):
    assert cli.main(["search", "pizza", "--no-proxy", "--location", "x" * 64]) == 1
    assert "location must be" in capsys.readouterr().err


class _Stopping:
    """Answers `serves` queries, then stops as the breaker does."""

    stats = type("Stats", (), {"summary": lambda self, price: {}})()

    def __init__(self, serves, asked):
        self.serves, self.asked = serves, asked

    def run(self, queries):
        from serpmaven.scraper import ExitsRefused

        for q in queries:
            if self.serves == 0:
                raise ExitsRefused("6 exits in a row refused or unreachable")
            self.serves -= 1
            self.asked.append(q)
            yield json.loads(_line(q))


def test_patient_waits_and_goes_on_with_the_queries_left(tmp_path, monkeypatch):
    asked, slept = [], []
    scrapers = iter([_Stopping(1, asked), _Stopping(5, asked)])
    monkeypatch.setattr(cli, "_build", lambda args, proxy, **kw: (next(scrapers), ""))
    monkeypatch.setattr(cli.time, "sleep", slept.append)
    out = tmp_path / "out.jsonl"
    argv = ["search", "a", "b", "c", "--no-proxy", "-o", str(out), "--patient", "2"]
    assert cli.main(argv) == 0
    assert asked == ["a", "b", "c"] and slept == [120]
    assert len(out.read_text(encoding="utf-8").splitlines()) == 3


def test_without_patient_the_run_stops_once(tmp_path, monkeypatch):
    asked = []
    monkeypatch.setattr(cli, "_build", lambda args, proxy, **kw: (_Stopping(1, asked), ""))
    assert cli.main(["search", "a", "b", "--no-proxy", "-o", str(tmp_path / "o.jsonl")]) == 3
    assert asked == ["a"]


def test_patient_gives_up_after_its_waits(tmp_path, monkeypatch):
    asked, slept = [], []
    monkeypatch.setattr(cli, "_build", lambda args, proxy, **kw: (_Stopping(0, asked), ""))
    monkeypatch.setattr(cli.time, "sleep", slept.append)
    argv = ["search", "a", "--no-proxy", "-o", str(tmp_path / "o.jsonl"), "--patient", "1"]
    assert cli.main(argv) == 3
    assert len(slept) == cli.PATIENT_WAITS


@pytest.mark.parametrize(
    "name, fmt", [("r.csv", "csv"), ("r.JSON", "json"), ("r.jsonl", "jsonl"), ("r.txt", "jsonl")]
)
def test_the_output_format_follows_the_file_extension(name, fmt, tmp_path):
    from pathlib import Path

    assert cli._format_for(Path(name)) == fmt
    assert cli._format_for(None) == "jsonl"


def test_a_query_that_failed_and_was_then_answered_counts_as_answered(tmp_path):
    out = tmp_path / "out.jsonl"
    out.write_text(
        _line("tacos", "Error")
        + _line("tacos")
        + _line("ramen")
        + _line("ramen", "Error", page=2)
        + _line("pho")
        + _line("pho", "Error")
        + _line("udon", "Error")
        + _line("udon")
        + _line("udon", page=2),
        encoding="utf-8",
    )
    assert answered(out) == {"tacos", "udon"}


def test_a_record_with_a_unicode_line_separator_stays_one_record(tmp_path):
    out = tmp_path / "out.jsonl"
    record = json.loads(_line("pizza"))
    record["organic_results"] = [{"snippet": "one two three\x85four"}]
    out.write_text(json.dumps(record, ensure_ascii=False) + "\n", encoding="utf-8")
    assert answered(out) == {"pizza"}


@pytest.mark.parametrize("value", ["-1", "nan", "inf", "soon"])
def test_patient_refuses_what_it_cannot_wait(value, capsys):
    with pytest.raises(SystemExit):
        cli._parser().parse_args(["search", "q", "--patient", value])
    assert "--patient" in capsys.readouterr().err


def test_patient_does_not_wait_out_a_refused_proxy_account(tmp_path, monkeypatch):
    from serpmaven.scraper import ProxyRefused

    class Refused(_Stopping):
        def run(self, queries):
            raise ProxyRefused("the proxy answered 407")
            yield  # pragma: no cover

    slept = []
    monkeypatch.setattr(cli, "_build", lambda args, proxy, **kw: (Refused(0, []), ""))
    monkeypatch.setattr(cli.time, "sleep", slept.append)
    argv = ["search", "a", "--no-proxy", "-o", str(tmp_path / "o.jsonl"), "--patient", "1"]
    assert cli.main(argv) == 3
    assert slept == []


def test_patient_asks_again_a_query_whose_later_page_failed(tmp_path, monkeypatch):
    asked = []

    class SecondPageFails(_Stopping):
        def run(self, queries):
            from serpmaven.scraper import ExitsRefused

            for q in queries:
                asked.append(q)
                yield json.loads(_line(q))
                if len(asked) == 1:
                    yield json.loads(_line(q, "Error", page=2))
                    raise ExitsRefused("6 exits in a row refused or unreachable")
                yield json.loads(_line(q, page=2))

    monkeypatch.setattr(cli, "_build", lambda args, proxy, **kw: (SecondPageFails(9, []), ""))
    monkeypatch.setattr(cli.time, "sleep", lambda s: None)
    argv = ["search", "a", "--no-proxy", "--pages", "2", "-o", str(tmp_path / "o.jsonl")]
    assert cli.main([*argv, "--patient", "1"]) == 0
    assert asked == ["a", "a"]


def test_a_bad_location_leaves_an_existing_output_file_alone(tmp_path):
    out = tmp_path / "out.jsonl"
    out.write_text(_line("pizza"), encoding="utf-8")
    argv = ["search", "q", "--no-proxy", "-o", str(out), "--location", "x" * 64]
    assert cli.main(argv) == 1
    assert out.read_text(encoding="utf-8") == _line("pizza")


def test_settings_refuse_a_location_google_cannot_take():
    from serpmaven.scraper import Settings

    with pytest.raises(ValueError, match="location"):
        Settings(location="x" * 64)


def _paged(query, page, more):
    record = json.loads(_line(query, page=page))
    record["pagination"] = {"current": page, **({"next": "https://g.example/n"} if more else {})}
    return json.dumps(record) + "\n"


def test_with_several_pages_a_query_cut_short_is_asked_again(tmp_path):
    out = tmp_path / "out.jsonl"
    out.write_text(
        _paged("cut", 1, True)  # killed after page 1 of 3
        + _paged("short", 1, True)
        + _paged("short", 2, False)  # Google had no page 3
        + "".join(_paged("full", p, True) for p in (1, 2, 3)),
        encoding="utf-8",
    )
    assert answered(out, pages=3) == {"short", "full"}
    assert answered(out, pages=1) == {"cut", "short", "full"}
