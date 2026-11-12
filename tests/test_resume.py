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
