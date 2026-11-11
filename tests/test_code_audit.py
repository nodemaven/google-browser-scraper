import importlib.util
import sys
from pathlib import Path

SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "code_audit.py"
spec = importlib.util.spec_from_file_location("code_audit", SCRIPT)
audit = importlib.util.module_from_spec(spec)
sys.modules["code_audit"] = audit  # dataclasses look their module up here
spec.loader.exec_module(audit)


def _function(name: str, lines: int, indent: str = "") -> str:
    body = "".join(f"{indent}    x = {i}\n" for i in range(lines - 1))
    return f"{indent}def {name}():\n{body}"


def test_long_functions_and_modules_are_found_with_their_qualified_names():
    text = (
        _function("short", 10)
        + "class Box:\n"
        + _function("long", audit.FUNCTION_LINES + 1, "    ")
        + _function("edge", audit.FUNCTION_LINES)
    )
    found = audit.scan("m.py", text)
    assert [(f.name, f.length) for f in found] == [("Box.long", audit.FUNCTION_LINES + 1)]

    big = "x = 1\n" * (audit.MODULE_LINES + 1)
    assert [(f.name, f.length) for f in audit.scan("big.py", big)] == [("", audit.MODULE_LINES + 1)]


def test_only_what_a_change_adds_or_lengthens_is_reported():
    base = audit.scan("m.py", _function("old", 90) + _function("kept", 100))
    head = audit.scan(
        "m.py", _function("old", 95) + _function("kept", 100) + _function("fresh", 81)
    )
    assert sorted(f.name for f in audit.new(head, base)) == ["fresh", "old"]


def test_a_warning_is_a_github_annotation_and_strict_fails(tmp_path, monkeypatch, capsys):
    src = tmp_path / "src" / "serpmaven"
    src.mkdir(parents=True)
    (src / "m.py").write_text(_function("long", 90))
    monkeypatch.chdir(tmp_path)
    assert audit.main([]) == 0
    out = capsys.readouterr().out
    assert out.startswith("::warning file=src/serpmaven/m.py,line=1::function long is 90 lines")
    assert audit.main(["--strict"]) == 1
