"""Command line: `search`, `serve`, `mcp`, `parse` and `doctor`."""

from __future__ import annotations

import argparse
import json
import os
import shlex
import sys
from pathlib import Path

from . import __version__, engines, warmup
from .classify import classify
from .output import WRITERS, open_writer, use_utf8
from .parse import parse_serp
from .proxy import NodeMavenSource, ProxyTemplate
from .scraper import ExitsRefused, Scraper, Settings


def main(argv: list[str] | None = None) -> int:
    use_utf8(sys.stdout)
    use_utf8(sys.stderr, errors="backslashreplace")
    parser = _parser()
    args = parser.parse_args(argv)
    if not getattr(args, "command", None):
        parser.print_help()
        return 2
    try:
        return args.handler(args)
    except (ValueError, RuntimeError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="google-browser-scraper",
        description="Google results from a real browser through your own proxy.",
    )
    parser.add_argument("--version", action="version", version=__version__)
    sub = parser.add_subparsers(dest="command")

    search = sub.add_parser("search", help="run queries and write results")
    search.add_argument("queries", nargs="*", help="queries; or use --file")
    search.add_argument("-f", "--file", type=Path, help="one query per line")
    _scraper_options(search)
    search.add_argument("--pages", type=int, default=1, help="result pages per query (default 1)")
    search.add_argument("--format", choices=list(WRITERS), default="jsonl")
    search.add_argument("-o", "--output", type=Path, help="output file (default stdout)")
    search.add_argument(
        "--price-per-gb",
        type=float,
        help="your proxy price, to print the cost per 1000 pages at the end",
    )
    search.set_defaults(handler=_search)

    serve = sub.add_parser("serve", help="SerpApi-style HTTP API: /search.json?q=...")
    _scraper_options(serve)
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=8000)
    serve.add_argument("--max-pages", type=int, default=3)
    serve.add_argument("--prewarm", action="store_true", help="warm an exit at startup")
    serve.set_defaults(handler=_serve)

    mcp = sub.add_parser("mcp", help="MCP server over stdio with a google_search tool")
    _scraper_options(mcp)
    mcp.add_argument("--prewarm", action="store_true", help="warm an exit at startup")
    mcp.set_defaults(handler=_mcp)

    parse = sub.add_parser("parse", help="parse a saved results page, offline")
    parse.add_argument("file", type=Path)
    parse.set_defaults(handler=_parse)

    doctor = sub.add_parser("doctor", help="check this machine's browser; sends nothing")
    doctor.add_argument("--engine", choices=list(engines.ENGINES), default=engines.DEFAULT_ENGINE)
    doctor.add_argument("--headless", action="store_true")
    doctor.add_argument("--channel")
    doctor.add_argument("--browser-arg", action="append", default=[], dest="browser_args")
    doctor.set_defaults(handler=_doctor)
    return parser


def _scraper_options(p: argparse.ArgumentParser) -> None:
    source = p.add_mutually_exclusive_group()
    source.add_argument(
        "--proxy",
        help="proxy URL with a {session} placeholder for sticky sessions; also read from GBS_PROXY",
    )
    source.add_argument(
        "--nodemaven",
        action="store_true",
        help="NodeMaven gateway from NODEMAVEN_LOGIN / NODEMAVEN_PASSWORD",
    )
    source.add_argument("--no-proxy", action="store_true", help="use this machine's own connection")
    p.add_argument("--country", help="exit country for --nodemaven, e.g. us")
    p.add_argument(
        "--engine",
        choices=list(engines.ENGINES),
        default=engines.DEFAULT_ENGINE,
        help="browser engine: patchright (default) or the experimental cloak",
    )
    p.add_argument(
        "--warmup",
        choices=list(warmup.LADDERS),
        default=warmup.DEFAULT,
        help=f"pages opened before the first query on an exit (default {warmup.DEFAULT})",
    )
    p.add_argument(
        "--max-per-exit",
        type=int,
        default=10,
        help="queries before an exit is retired even if still served",
    )
    p.add_argument(
        "--gap",
        type=_pair,
        default=(8.0, 20.0),
        metavar="LOW,HIGH",
        help="seconds between queries on one exit (default 8,20)",
    )
    p.add_argument(
        "--headless",
        action="store_true",
        help="run without a window. Not recommended: see `doctor`",
    )
    p.add_argument("--channel", help="browser channel, e.g. chrome to use installed Chrome")
    p.add_argument("--timezone", help="IANA timezone for the browser")
    p.add_argument(
        "--browser-arg",
        action="append",
        default=[],
        dest="browser_args",
        help="extra Chromium switch, repeatable; also GBS_BROWSER_ARGS",
    )
    p.add_argument(
        "--no-relay",
        action="store_true",
        help="hand the proxy to the browser directly, without the traffic count",
    )
    p.add_argument(
        "--save-html",
        type=Path,
        metavar="DIR",
        help="keep every page's HTML here, gzipped (they carry the exit's location)",
    )
    p.add_argument(
        "--no-resolve-links",
        action="store_true",
        help="leave result links as Google /goto redirects (saves one small request per result)",
    )
    p.add_argument("-v", "--verbose", action="store_true", help="log identities to stderr")


def _build(args, *, pages: int = 1) -> tuple[Scraper, str]:
    if args.nodemaven:
        proxy = NodeMavenSource(country=args.country)
    elif args.no_proxy:
        proxy = None
    else:
        url = args.proxy or os.environ.get("GBS_PROXY")
        if not url:
            raise ValueError("give --proxy, --nodemaven or --no-proxy")
        proxy = ProxyTemplate(url)
    if args.country and not args.nodemaven:
        raise ValueError(
            "--country only applies to --nodemaven; put the country in your "
            "provider's proxy URL instead"
        )
    if proxy is not None and not proxy.sticky:
        print(
            "warning: the proxy URL has no {session} placeholder, so the exit may change "
            "between requests and warming it does nothing",
            file=sys.stderr,
        )
    browser_args = tuple(args.browser_args) + tuple(
        shlex.split(os.environ.get("GBS_BROWSER_ARGS", ""))
    )
    settings = Settings(
        engine=args.engine,
        warmup=args.warmup,
        pages=pages,
        max_queries_per_exit=args.max_per_exit,
        gap_seconds=args.gap,
        headless=args.headless,
        channel=args.channel,
        timezone_id=args.timezone,
        browser_args=browser_args,
        use_relay=not args.no_relay,
        save_html=str(args.save_html) if args.save_html else None,
        resolve_links=not args.no_resolve_links,
    )
    log = (lambda e: print(json.dumps(e), file=sys.stderr)) if args.verbose else None
    described = proxy.describe() if proxy else "none (this machine's own address)"
    if args.verbose:
        print(f"proxy: {described}; engine: {args.engine}", file=sys.stderr)
    return Scraper(proxy, settings, on_event=log), described


def _search(args) -> int:
    queries = list(args.queries)
    if args.file:
        queries += args.file.read_text(encoding="utf-8").splitlines()
    if not any(q.strip() for q in queries):
        raise ValueError("no queries given")
    scraper, _ = _build(args, pages=args.pages)
    stream = args.output.open("w", encoding="utf-8", newline="") if args.output else sys.stdout
    writer = open_writer(args.format, stream)
    failed, code = 0, 0
    try:
        for record in scraper.run(queries):
            failed += record["search_metadata"]["status"] != "Success"
            writer.write(record)
    except ExitsRefused as exc:
        print(f"stopped: {exc}", file=sys.stderr)
        code = 3
    finally:
        writer.close()
        if args.output:
            stream.close()
        print("summary: " + json.dumps(scraper.stats.summary(args.price_per_gb)), file=sys.stderr)
    return code or (4 if failed else 0)


def _serve(args) -> int:
    from .server import Worker, serve

    worker = Worker(lambda: _build(args)[0], prewarm=args.prewarm).start()
    serve(worker, host=args.host, port=args.port, max_pages=args.max_pages)
    return 0


def _mcp(args) -> int:
    from .mcp import McpServer
    from .server import Worker

    worker = Worker(lambda: _build(args)[0], prewarm=args.prewarm).start()
    try:
        McpServer(worker).serve()
    finally:
        worker.stop()
    return 0


def _parse(args) -> int:
    page = args.file.read_text(encoding="utf-8", errors="replace")
    verdict = classify("https://www.google.com/search", page)
    out = {"page_verdict": verdict.kind, "verdict_reason": verdict.reason}
    if verdict.served:
        out.update(parse_serp(page))
    json.dump(out, sys.stdout, ensure_ascii=False, indent=2)
    sys.stdout.write("\n")
    return 0 if verdict.served else 4


def _doctor(args) -> int:
    from .doctor import FAIL, assess, read_fingerprint

    browser_args = tuple(args.browser_args) + tuple(
        shlex.split(os.environ.get("GBS_BROWSER_ARGS", ""))
    )
    fp = read_fingerprint(
        engine=args.engine, headless=args.headless, channel=args.channel, browser_args=browser_args
    )
    checks = assess(fp)
    width = max(len(c.name) for c in checks)
    print(f"engine: {args.engine}")
    for c in checks:
        print(f"[{c.status:4}] {c.name:<{width}}  {c.detail}")
    return 1 if any(c.status == FAIL for c in checks) else 0


def _pair(text: str) -> tuple[float, float]:
    try:
        low, high = (float(x) for x in text.split(","))
    except ValueError:
        raise argparse.ArgumentTypeError("expected LOW,HIGH, e.g. 8,20") from None
    return low, high


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
