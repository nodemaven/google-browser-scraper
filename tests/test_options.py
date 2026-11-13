"""Every command-line option is documented and changes what runs.

The checks walk the parser, so an option added later fails here until it is in
the README and, on the commands that scrape, has a sample below showing what
it changes. A new field of `Settings` fails until an option sets it or it is
listed as having none.
"""

import argparse
import dataclasses
import inspect
import json
import re
from pathlib import Path

from serpmaven import cli, engines, warmup
from serpmaven.scraper import Settings

README = (Path(__file__).parent.parent / "README.md").read_text(encoding="utf-8")

#: Commands that build a scraper from their options.
SCRAPING = ("search", "serve", "mcp")

COOKIES = "cookies.json"
OTHER_ENGINE = next(e for e in engines.ENGINES if e != engines.DEFAULT_ENGINE)
OTHER_WARMUP = next(w for w in warmup.LADDERS if w != warmup.DEFAULT)


@dataclasses.dataclass(frozen=True)
class Sample:
    """A value for an option that is not its default."""

    value: str | None = None
    #: The field of `Settings` the option changes; None for an option the
    #: command reads itself.
    field: str | None = None
    #: Options it needs beside it to change anything.
    beside: tuple[str, ...] = ()


SAMPLES = {
    # Where the proxy comes from.
    "--proxy": Sample("http://u-{session}:p@gate.example.com:7000"),
    "--nodemaven": Sample(),
    "--nodemaven-demo": Sample(),
    "--no-proxy": Sample(),
    "--country": Sample("us"),
    "--traffic-limit-mb": Sample("50"),
    "--keep-subuser": Sample(),
    # The browser.
    "--engine": Sample(OTHER_ENGINE, "engine"),
    "--headless": Sample(field="headless"),
    "--channel": Sample("chrome", "channel"),
    "--timezone": Sample("Europe/Berlin", "timezone_id"),
    "--browser-arg": Sample("--disable-gpu", "browser_args"),
    "--restart-every": Sample("7", "restart_every"),
    "--no-relay": Sample(field="use_relay"),
    # Proxy IPs and what they start from.
    "--warmup": Sample(OTHER_WARMUP, "warmup"),
    "--max-per-exit": Sample("7", "max_queries_per_exit"),
    "--spare": Sample("2", "spare"),
    "--clone": Sample("3", "clones", beside=("--renew-cookies=0",)),
    "--clone-max-age": Sample("7", "clone_max_age"),
    "--clone-refusals": Sample("3", "clone_refusals"),
    "--no-clone-cache": Sample(field="clone_cache"),
    "--cookies": Sample(COOKIES, "donors"),
    "--renew-cookies": Sample("7", "renew_donors_every"),
    "--cookies-out": Sample("sets", "donors_out"),
    # Queries and pages.
    "--input": Sample("insert", "input"),
    "--direct": Sample("none", "direct"),
    "--stop-after-read": Sample(field="stop_after_read"),
    "--gap": Sample("0,1", "gap_seconds"),
    "--no-hybrid": Sample(field="hybrid"),
    "--hl": Sample("de", "hl"),
    "--gl": Sample("de", "gl"),
    "--location": Sample("Austin,Texas,United States", "location"),
    "--tbs": Sample("qdr:w", "tbs"),
    "--no-personalization": Sample(field="personalized"),
    "--no-autocorrect": Sample(field="autocorrect"),
    "--cr": Sample("countryUS", "cr"),
    "--lr": Sample("lang_en", "lr"),
    "--save-html": Sample("pages", "save_html"),
    "--no-resolve-links": Sample(field="resolve_links"),
    "--links": Sample("3", "links"),
    "--verbose": Sample(),
    # search
    "--file": Sample("queries.txt"),
    "--pages": Sample("2"),
    "--workers": Sample("2"),
    "--format": Sample("csv"),
    "--output": Sample("results.jsonl"),
    "--resume": Sample(),
    "--patient": Sample("10"),
    "--price-per-gb": Sample("3"),
    # serve, mcp
    "--host": Sample("0.0.0.0"),
    "--port": Sample("8001"),
    "--max-pages": Sample("5"),
    "--prewarm": Sample(),
}

#: Fields of `Settings` no option above sets, and where they come from.
NOT_FROM_AN_OPTION = {
    "pages": "search passes --pages itself; serve and mcp take it from each request",
    "max_exits_per_query": "library only",
    "breaker": "library only",
    "dwell_seconds": "library only",
    "settle_seconds": "library only",
}


#: The name the options before any command go under.
ROOT = ""


def _parsers() -> dict[str, argparse.ArgumentParser]:
    root = cli._parser()
    commands = next(a for a in root._actions if isinstance(a, argparse._SubParsersAction))
    return {ROOT: root, **commands.choices}


def _options() -> list[tuple[str, str, argparse.Action]]:
    """(command, long option, action) for every option of every command, but
    for the ones argparse answers itself: --help and --version."""
    return [
        (command, long, action)
        for command, parser in _parsers().items()
        for action in parser._actions
        if not isinstance(action, (argparse._HelpAction, argparse._VersionAction))
        for long in [o for o in action.option_strings if o.startswith("--")][:1]
    ]


OPTIONS = _options()


def _source(command: str) -> str:
    """The code a command runs: its handler and every function of the cli
    module the handler can reach."""
    start = cli.main if command == ROOT else _parsers()[command].get_default("handler")
    seen: dict[str, str] = {}
    queue = [start]
    while queue:
        fn = inspect.unwrap(queue.pop())
        if fn.__qualname__ in seen:
            continue
        seen[fn.__qualname__] = inspect.getsource(fn)
        codes = [fn.__code__]
        while codes:
            code = codes.pop()
            codes += [c for c in code.co_consts if inspect.iscode(c)]
            for name in code.co_names:
                other = getattr(cli, name, None)
                if inspect.isfunction(other) and other.__module__ == cli.__name__:
                    queue.append(other)
    return "\n".join(seen.values())


def test_every_option_is_in_the_readme():
    missing = sorted(
        {
            option
            for _, option, _ in OPTIONS
            if not re.search(rf"(?<![\w-]){re.escape(option)}(?![\w-])", README)
        }
    )
    assert not missing, f"not in README.md: {missing}"


def test_every_option_is_read_by_its_command():
    source = {command: _source(command) for command in _parsers()}
    unread = sorted(
        f"{command} {option}".strip()
        for command, option, a in OPTIONS
        if not re.search(rf"\bargs\.{a.dest}\b", source[command])
    )
    assert not unread, f"parsed and never read: {unread}"


def test_every_option_of_a_scraping_command_has_a_sample():
    options = {option for command, option, _ in OPTIONS if command in SCRAPING}
    assert options == set(SAMPLES)


def test_every_sample_changes_what_the_command_runs_with(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    nid = {"name": "NID", "value": "v", "domain": ".google.com", "path": "/"}
    (tmp_path / COOKIES).write_text(json.dumps([nid]), encoding="utf-8")
    parse = cli._parser().parse_args

    def settings(args) -> Settings:
        return cli._build(args, None)[0].settings

    idle = []
    for command, option, action in OPTIONS:
        if command not in SCRAPING:
            continue
        sample = SAMPLES[option]
        base = [command, *sample.beside]
        before = parse(base)
        after = parse([*base, option if sample.value is None else f"{option}={sample.value}"])
        if sample.field is None:
            changed = getattr(before, action.dest) != getattr(after, action.dest)
        else:
            changed = getattr(settings(before), sample.field) != getattr(
                settings(after), sample.field
            )
        if not changed:
            idle.append(f"{command} {option}")
    assert not idle, f"changes nothing: {idle}"


def test_every_setting_is_set_by_an_option_or_listed_as_having_none():
    fields = {f.name for f in dataclasses.fields(Settings)}
    from_options = {s.field for s in SAMPLES.values() if s.field}
    assert fields - from_options == set(NOT_FROM_AN_OPTION)
    assert from_options <= fields
