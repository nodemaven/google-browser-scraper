"""Warn when a change makes the code longer in the places that are hard to read.

A function longer than FUNCTION_LINES or a module longer than MODULE_LINES is
reported. With `--base REF` only what is new or longer than at REF is reported,
so a pull request is told about what it adds, not about old debt; the old debt
is counted in one line. Findings are GitHub Actions warnings, and the exit code
is 0 unless `--strict` is given.

    python scripts/code_audit.py --base origin/main
"""

from __future__ import annotations

import argparse
import ast
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

FUNCTION_LINES = 80
MODULE_LINES = 1000
SOURCE = Path("src/serpmaven")


@dataclass(frozen=True)
class Finding:
    path: str
    name: str  # a function's qualified name, or "" for the module itself
    line: int
    length: int

    @property
    def limit(self) -> int:
        return FUNCTION_LINES if self.name else MODULE_LINES

    def message(self) -> str:
        what = f"function {self.name}" if self.name else "module"
        return f"{what} is {self.length} lines (limit {self.limit}); split it"


def scan(path: str, text: str) -> list[Finding]:
    """Functions and the module over their limits, in one source file."""
    found = []
    lines = len(text.splitlines())
    if lines > MODULE_LINES:
        found.append(Finding(path, "", 1, lines))

    def visit(node: ast.AST, prefix: str) -> None:
        for child in ast.iter_child_nodes(node):
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                name = f"{prefix}{child.name}"
                if not isinstance(child, ast.ClassDef):
                    length = child.end_lineno - child.lineno + 1
                    if length > FUNCTION_LINES:
                        found.append(Finding(path, name, child.lineno, length))
                visit(child, name + ".")
            else:
                visit(child, prefix)

    visit(ast.parse(text), "")
    return found


def new(head: list[Finding], base: list[Finding]) -> list[Finding]:
    """Findings that are not in `base`, or are longer than they were there."""
    before = {(f.path, f.name): f.length for f in base}
    return [f for f in head if f.length > before.get((f.path, f.name), 0)]


def _base_text(ref: str, path: str) -> str | None:
    result = subprocess.run(
        ["git", "show", f"{ref}:{path}"], capture_output=True, text=True, encoding="utf-8"
    )
    return result.stdout if result.returncode == 0 else None


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--base", help="git ref to compare with; without it, report everything")
    parser.add_argument("--strict", action="store_true", help="exit 1 when anything is reported")
    args = parser.parse_args(argv)

    head, base = [], []
    for file in sorted(SOURCE.rglob("*.py")):
        path = file.as_posix()
        head += scan(path, file.read_text(encoding="utf-8"))
        if args.base:
            text = _base_text(args.base, path)
            if text is not None:
                base += scan(path, text)
    report = new(head, base) if args.base else head
    for f in report:
        print(f"::warning file={f.path},line={f.line}::{f.message()}")
    if args.base:
        print(f"{len(head) - len(report)} known over the limits, unchanged or shorter")
    return 1 if args.strict and report else 0


if __name__ == "__main__":
    sys.exit(main())
