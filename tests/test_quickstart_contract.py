"""The README's Install and Quickstart sections are a contract with every new user.

Options come and go; what these sections tell people to type does not. Every command in
their code blocks must still be accepted by the command line, every name the Python
example imports must still exist, and the record fields people read must still be there.
A change that breaks one of these fails here, not on a user's first run.

The same file runs weekly against the package installed from PyPI
(.github/workflows/pypi-check.yml), so a release that broke the quickstart is caught too.
"""

import argparse
import re
import shlex
from pathlib import Path

import pages
import pytest

import serpmaven
from serpmaven import cli
from serpmaven.output import build_record
from serpmaven.parse import parse_serp

README = Path(__file__).resolve().parent.parent / "README.md"

#: Wrappers the README puts in front of a command.
PREFIXES = (("uvx",), ("xvfb-run", "-a"))

#: Fields of a record that users' code reads. Others may be added, none renamed or dropped.
RECORD_FIELDS = ("search_metadata", "search_parameters", "exit")
METADATA_FIELDS = ("status", "created_at", "total_time_taken", "page_verdict")
PARAMETER_FIELDS = ("q", "page", "hl")
ORGANIC_FIELDS = ("position", "title", "link", "displayed_link", "snippet")


def _sections() -> str:
    text = README.read_text(encoding="utf-8")
    start = text.index("\n## Install")
    end = text.index("\n## ", text.index("\n## Quickstart") + 1)
    while text[end:].startswith("\n### "):  # "### From Python" belongs to the quickstart
        end = text.index("\n## ", end + 1)
    return text[start:end]


def _blocks(language: str) -> list[str]:
    return re.findall(rf"```{language}\n(.*?)```", _sections(), re.S)


def _commands() -> list[list[str]]:
    found = []
    for block in _blocks("bash") + _blocks("powershell"):
        for line in block.splitlines():
            words = shlex.split(line.strip(), posix=True) if line.strip() else []
            for prefix in PREFIXES:
                if tuple(words[: len(prefix)]) == prefix:
                    words = words[len(prefix) :]
            if words and words[0] == "serpmaven":
                found.append(words[1:])
    return found


def test_the_quickstart_has_commands_to_check():
    commands = _commands()
    assert {c[0] for c in commands} >= {"login", "search", "install", "doctor"}


def _exact_parser() -> argparse.ArgumentParser:
    """The CLI's parser with abbreviations off: otherwise a renamed --country-x would
    still accept the README's --country as its prefix and hide the break."""
    parser = cli._parser()
    parser.allow_abbrev = False
    for action in parser._actions:
        if isinstance(action, argparse._SubParsersAction):
            for sub in action.choices.values():
                sub.allow_abbrev = False
    return parser


@pytest.mark.parametrize("argv", _commands(), ids=lambda argv: " ".join(argv))
def test_every_quickstart_command_is_accepted(argv):
    args = _exact_parser().parse_args(argv)
    assert args.command == argv[0]


def test_the_python_example_imports_what_exists():
    (block,) = _blocks("python")
    names = re.search(r"from serpmaven import ([\w, ]+)", block).group(1)
    for name in (n.strip() for n in names.split(",")):
        assert hasattr(serpmaven, name), name


def test_the_record_keeps_the_fields_users_read():
    # Through the parser, so a field it stops writing fails here too.
    parsed = parse_serp(pages.results_page(goto=False))
    record = build_record(
        "q", page=1, verdict=None, url=None, elapsed=1.0, exit_info={}, parsed=parsed
    )
    for field in RECORD_FIELDS:
        assert field in record, field
    for field in METADATA_FIELDS:
        assert field in record["search_metadata"], field
    for field in PARAMETER_FIELDS:
        assert field in record["search_parameters"], field
    first = record["organic_results"][0]
    assert all(first.get(field) for field in ORGANIC_FIELDS), first
