"""Command line: `search`, `serve`, `mcp`, `parse` and `doctor`."""

from __future__ import annotations

import argparse
import json
import math
import shlex
import sys
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from . import __version__, browser, config, engines, env, hints, installer, warmup
from .classify import classify
from .mcp import DEFAULT_LINKS as MCP_LINKS
from .output import WRITERS, answered, open_writer, use_utf8
from .parse import parse_serp
from .pool import Pool
from .progress import Progress
from .proxy import NodeMavenDemoSource, NodeMavenSource, ProxyTemplate
from .scraper import (
    DIRECT_MODES,
    RENEW_EVERY,
    ExitsRefused,
    ProxyRefused,
    Scraper,
    Settings,
    filter_params,
)
from .server import locale_problem


def main(argv: list[str] | None = None, prog: str = "serpmaven") -> int:
    """The command line; `prog` is the command name shown in usage and errors
    (the old names pass their own)."""
    use_utf8(sys.stdout)
    use_utf8(sys.stderr, errors="backslashreplace")
    parser = _parser(prog)
    args = parser.parse_args(argv)
    if not getattr(args, "command", None):
        parser.print_help()
        return 2
    try:
        return args.handler(args)
    except (ValueError, RuntimeError) as exc:
        if not hints.report(exc):
            print(f"error: {exc}", file=sys.stderr)
        return 1
    except Exception as exc:
        if hints.report(exc):
            return 1
        raise


def _parser(prog: str = "serpmaven") -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog=prog,
        description="Google results from a real browser through your own proxy.",
    )
    parser.add_argument("--version", action="version", version=__version__)
    sub = parser.add_subparsers(dest="command")

    search = sub.add_parser("search", help="run queries and write results")
    search.add_argument("queries", nargs="*", help="queries; or use --file")
    search.add_argument("-f", "--file", type=Path, help="one query per line")
    _scraper_options(search)
    search.add_argument("--pages", type=int, default=1, help="result pages per query (default 1)")
    search.add_argument(
        "--workers",
        type=int,
        default=1,
        metavar="N",
        help="proxy IPs to search on at once, one browser each (default 1)",
    )
    search.add_argument(
        "--format",
        choices=list(WRITERS),
        help="output format (default: from the --output file's extension, else jsonl)",
    )
    search.add_argument("-o", "--output", type=Path, help="output file (default stdout)")
    search.add_argument(
        "--resume",
        action="store_true",
        help="skip the queries the --output file already has results for and add to it "
        "(jsonl); failed ones are asked again",
    )
    search.add_argument(
        "--patient",
        type=_minutes,
        default=0,
        metavar="MINUTES",
        help="when Google refuses proxy IPs until the run stops, wait this long and go on "
        f"with the queries left, up to {PATIENT_WAITS} times (default: stop)",
    )
    search.add_argument(
        "--price-per-gb",
        type=float,
        help="your proxy price, to print the cost per 1000 pages at the end",
    )
    search.set_defaults(handler=_search)

    serve = sub.add_parser(
        "serve", help="HTTP API in the usual SERP API format: /search.json?q=..."
    )
    _scraper_options(serve)
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=8000)
    serve.add_argument("--max-pages", type=int, default=3)
    serve.add_argument(
        "--prewarm",
        action="store_true",
        help="find a proxy IP Google serves before the first request",
    )
    serve.set_defaults(handler=_serve)

    mcp = sub.add_parser("mcp", help="MCP server over stdio with a google_search tool")
    _scraper_options(mcp)
    mcp.add_argument(
        "--prewarm", action="store_true", help="find a proxy IP Google serves before the first call"
    )
    # An agent waits for the answer: the top results' links are what it reads.
    mcp.set_defaults(handler=_mcp, links=MCP_LINKS)

    parse = sub.add_parser("parse", help="parse a saved results page, offline")
    parse.add_argument("file", type=Path)
    parse.set_defaults(handler=_parse)

    doctor = sub.add_parser("doctor", help="check this machine's browser; sends nothing")
    doctor.add_argument("--engine", choices=list(engines.ENGINES), default=engines.DEFAULT_ENGINE)
    doctor.add_argument("--headless", action="store_true")
    doctor.add_argument("--channel")
    doctor.add_argument("--browser-arg", action="append", default=[], dest="browser_args")
    doctor.set_defaults(handler=_doctor)

    login = sub.add_parser(
        "login",
        help="save your proxy login once, so later runs need no flags",
        description="Save a proxy login for later runs. Without --proxy it asks for the "
        "NodeMaven Proxy Username and Proxy Password from the dashboard. The file is "
        "readable by your user only; the password in it is not encrypted.",
    )
    login.add_argument(
        "--proxy",
        help="save this proxy URL instead (any provider, with a {session} placeholder)",
    )
    login.set_defaults(handler=_login)

    logout = sub.add_parser("logout", help="delete the saved proxy login")
    logout.set_defaults(handler=_logout)

    install = sub.add_parser(
        "install", help="download the browser (about 150 MB, once per machine)"
    )
    install.add_argument(
        "--with-deps",
        action="store_true",
        help="also install the system libraries it needs (Linux; needs sudo)",
    )
    install.set_defaults(handler=_install)
    return parser


def _scraper_options(p: argparse.ArgumentParser) -> None:
    source = p.add_mutually_exclusive_group()
    source.add_argument(
        "--proxy",
        help="proxy URL with a {session} placeholder for sticky sessions; "
        "also read from SERPMAVEN_PROXY",
    )
    source.add_argument(
        "--nodemaven",
        action="store_true",
        help="NodeMaven gateway, with the login from `serpmaven login` or "
        "NODEMAVEN_LOGIN / NODEMAVEN_PASSWORD (the default once a NodeMaven login is saved)",
    )
    source.add_argument(
        "--nodemaven-demo",
        action="store_true",
        help="auto-provision a capped NodeMaven sub-user for this run, from "
        "NODEMAVEN_API_KEY (the dashboard API key, not the proxy login/password)",
    )
    source.add_argument("--no-proxy", action="store_true", help="use this machine's own connection")
    p.add_argument("--country", help="proxy IP country for --nodemaven / --nodemaven-demo, e.g. us")
    p.add_argument(
        "--traffic-limit-mb",
        type=float,
        default=300.0,
        metavar="MB",
        help="traffic cap on a --nodemaven-demo sub-user; a fresh warm-up alone uses ~60 "
        "(default 300)",
    )
    p.add_argument(
        "--keep-subuser",
        action="store_true",
        help="don't delete the --nodemaven-demo sub-user when the run ends",
    )
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
        help=f"pages opened before the first query on a proxy IP (default {warmup.DEFAULT}; "
        "light skips Google Trends for much less traffic per fresh proxy IP)",
    )
    p.add_argument(
        "--max-per-exit",
        type=int,
        default=300,
        help="queries before a proxy IP is retired even if still served (default 300)",
    )
    p.add_argument(
        "--no-clone-cache",
        action="store_true",
        help="copies carry the cookie jar only, not the browser cache (by default they "
        "carry both, until two copies with the cache are refused)",
    )
    p.add_argument(
        "--restart-every",
        type=int,
        default=50,
        metavar="N",
        help="results pages before the browser is restarted on the same profile and proxy IP, "
        "which keeps its memory from growing (default 50; 0 never)",
    )
    p.add_argument(
        "--spare",
        type=int,
        default=0,
        metavar="N",
        help="proxy IPs to warm in the background while the current one serves, so a "
        "retired one is replaced without waiting (default 0; each costs a warm-up)",
    )
    p.add_argument(
        "--clone",
        type=int,
        default=10,
        metavar="N",
        help="with --renew-cookies 0: start up to N new proxy IPs from copies of the first "
        "served one's cookies instead of warming each one (default 10; 0 turns it off)",
    )
    p.add_argument(
        "--clone-max-age",
        type=float,
        default=180.0,
        metavar="MIN",
        help="minutes copied cookies stay in use (default 180)",
    )
    p.add_argument(
        "--clone-refusals",
        type=int,
        default=1,
        metavar="N",
        help="copies refused on their first query, in a row, that end the copying (default 1)",
    )
    p.add_argument(
        "--input",
        choices=browser.INPUT_MODES,
        default="type",
        help="how a query is entered into Google's search box: typed key by key "
        "(default), inserted at once, or inserted word by word",
    )
    p.add_argument(
        "--direct",
        choices=DIRECT_MODES,
        default="all",
        help="which queries open the results page directly instead of going through "
        "the search box: all (default), all but the first on each proxy IP, or none",
    )
    p.add_argument(
        "--stop-after-read",
        action="store_true",
        help="leave each results page for about:blank once it is read (experimental: "
        "saves traffic Google's late scripts would spend)",
    )
    p.add_argument(
        "--gap",
        type=_pair,
        default=(2.0, 5.0),
        metavar="LOW,HIGH",
        help="seconds between queries on one proxy IP (default 2,5)",
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
        help="extra Chromium switch, repeatable; also SERPMAVEN_BROWSER_ARGS",
    )
    p.add_argument(
        "--no-relay",
        action="store_true",
        help="hand the proxy to the browser directly, without the traffic count",
    )
    p.add_argument(
        "--no-hybrid",
        action="store_true",
        help="fetch every results page in the browser; by default they come over HTTP "
        "with the browser's cookies, and the browser handles only Google's checks",
    )
    p.add_argument(
        "--cookies",
        metavar="FILE",
        action="append",
        help="start every new proxy IP with the Google cookies in FILE (JSON) instead of "
        "warming it up; repeat for more files, the next one taking over after two proxy "
        "IPs in a row are refused on their first query, and warm-ups again once every "
        "file has been refused that way; turns --clone off",
    )
    p.add_argument(
        "--renew-cookies",
        type=_non_negative,
        default=RENEW_EVERY,
        metavar="N",
        help="every N served queries on a proxy IP, its Google cookies replace the set new "
        "proxy IPs start from, so the set stays fresh; without --cookies the first renewal "
        f"ends the warm-ups (default {RENEW_EVERY}; 0 turns it off and brings back --clone)",
    )
    p.add_argument(
        "--cookies-out",
        metavar="DIR",
        help="write each renewed cookie set to DIR, to start the next run with --cookies",
    )
    p.add_argument("--hl", default="en", help="interface language of the results, e.g. de")
    p.add_argument(
        "--gl",
        help="country the results are for, e.g. de; unset, Google picks it from the proxy IP",
    )
    p.add_argument(
        "--location",
        metavar="PLACE",
        help='search as from this place, by its canonical name, e.g. "Austin,Texas,United States"',
    )
    p.add_argument(
        "--tbs",
        metavar="FILTER",
        help="Google's tbs filter as it is, e.g. qdr:w for the past week",
    )
    p.add_argument(
        "--no-personalization",
        action="store_true",
        help="results without personalization (pws=0)",
    )
    p.add_argument(
        "--no-autocorrect",
        action="store_true",
        help="search a misspelt query as typed, not as Google corrects it (nfpr=1)",
    )
    p.add_argument(
        "--cr", metavar="COUNTRIES", help="only documents from these countries, e.g. countryUS"
    )
    p.add_argument(
        "--lr", metavar="LANGUAGES", help="only documents in these languages, e.g. lang_en"
    )
    p.add_argument(
        "--save-html",
        type=Path,
        metavar="DIR",
        help="keep every page's HTML here, gzipped (they carry the proxy IP's location)",
    )
    p.add_argument(
        "--no-resolve-links",
        action="store_true",
        help="leave result links as Google /goto redirects (saves one small request per result)",
    )
    p.add_argument(
        "--links",
        type=int,
        metavar="N",
        help="resolve the links of the first N organic results on each page only "
        "(0: none; default all, mcp 5); the rest carry their /goto redirect",
    )
    p.add_argument("-v", "--verbose", action="store_true", help="log identities to stderr")


@contextmanager
def _proxy_source(args) -> Iterator[Any]:
    """One proxy source per run, shared by every worker; a demo sub-user is deleted on exit."""
    kind, url = _source_kind(args)
    if args.country and kind not in ("nodemaven", "nodemaven-demo"):
        raise ValueError(
            "--country only applies to NodeMaven; put the country in your provider's "
            "proxy URL instead"
        )
    if kind == "nodemaven":
        proxy = NodeMavenSource(country=args.country)
    elif kind == "nodemaven-demo":
        proxy = NodeMavenDemoSource(country=args.country, traffic_limit_mb=args.traffic_limit_mb)
    elif kind == "none":
        proxy = None
    else:
        proxy = ProxyTemplate(url)
    if proxy is not None and not proxy.sticky:
        print(
            "warning: the proxy URL has no {session} placeholder, so the proxy IP may change "
            "between requests and warming it does nothing",
            file=sys.stderr,
        )
    try:
        yield proxy
    finally:
        _cleanup_subuser(proxy, args.keep_subuser)


def _source_kind(args) -> tuple[str, str | None]:
    """Which proxy to use: a flag, then the environment, then `serpmaven login`."""
    if args.nodemaven:
        return "nodemaven", None
    if args.nodemaven_demo:
        return "nodemaven-demo", None
    if args.no_proxy:
        return "none", None
    url = args.proxy or env.get("PROXY")
    if url:
        return "url", url
    if config.nodemaven_credentials() is not None:
        return "nodemaven", None
    url = config.saved_proxy()
    if url:
        return "url", url
    raise ValueError(
        "no proxy: run `serpmaven login` once, or give --proxy, --nodemaven or --no-proxy"
    )


def _settings(args, *, pages: int = 1, worker: int | None = None) -> Settings:
    """The scraper's settings from the options; raises ValueError on a bad one."""
    problem = locale_problem(args.hl, args.gl)
    if problem:
        raise ValueError(problem.replace("hl", "--hl", 1).replace("gl", "--gl", 1))
    if args.links is not None and args.links < 0:
        raise ValueError("--links must be 0 or more")
    browser_args = tuple(args.browser_args) + tuple(shlex.split(env.get("BROWSER_ARGS", "")))
    return Settings(
        engine=args.engine,
        warmup=args.warmup,
        pages=pages,
        max_queries_per_exit=args.max_per_exit,
        restart_every=args.restart_every,
        clone_cache=not args.no_clone_cache,
        spare=args.spare,
        # Cookie sets replace the copies of a served profile.
        clones=0 if args.cookies or args.renew_cookies else args.clone,
        clone_max_age=args.clone_max_age * 60,
        clone_refusals=args.clone_refusals,
        input=args.input,
        direct=args.direct,
        stop_after_read=args.stop_after_read,
        gap_seconds=args.gap,
        headless=args.headless,
        channel=args.channel,
        timezone_id=args.timezone,
        browser_args=browser_args,
        use_relay=not args.no_relay,
        hybrid=not args.no_hybrid,
        save_html=_html_dir(args.save_html, worker),
        resolve_links=not args.no_resolve_links,
        links=args.links,
        hl=args.hl,
        gl=args.gl,
        location=args.location,
        tbs=args.tbs,
        personalized=not args.no_personalization,
        autocorrect=not args.no_autocorrect,
        cr=args.cr,
        lr=args.lr,
        donors=tuple(browser.load_cookies(f) for f in args.cookies or ()),
        renew_donors_every=args.renew_cookies,
        donors_out=args.cookies_out,
    )


def _build(
    args, proxy, *, pages: int = 1, worker: int | None = None, progress: Progress | None = None
) -> tuple[Scraper, str]:
    settings = _settings(args, pages=pages, worker=worker)
    log = None
    if args.verbose:
        tag = {} if worker is None else {"worker": worker}

        def _log(event: dict) -> None:
            print(
                json.dumps({"ts": round(time.time(), 3), **tag, **event}),
                file=sys.stderr,
                flush=True,
            )

        log = _log
    elif progress is not None:
        log = progress.on_event

    described = proxy.describe() if proxy else "none (this machine's own address)"
    if args.verbose and not worker:
        print(f"proxy: {described}; engine: {args.engine}", file=sys.stderr)
    return Scraper(proxy, settings, on_event=log), described


def _ready(args, *, ask: bool = True) -> None:
    """Fail early on what would only fail after minutes: no proxy, no browser."""
    _source_kind(args)
    if args.engine == "patchright" and not args.channel:
        installer.ensure_browser(ask=ask)


#: How many times `--patient` waits and goes on before the run gives up.
PATIENT_WAITS = 3


def _format_for(output: Path | None) -> str:
    """The output format a file's extension names; JSON Lines otherwise."""
    suffix = output.suffix.lower() if output else ""
    return {".csv": "csv", ".json": "json"}.get(suffix, "jsonl")


def _read_queries(args) -> list[str]:
    queries = list(args.queries)
    if args.file:
        try:
            queries += args.file.read_text(encoding="utf-8-sig").splitlines()
        except FileNotFoundError:
            raise ValueError(f"query file not found: {args.file} (one query per line)") from None
    if not any(q.strip() for q in queries):
        raise ValueError("no queries given")
    return queries


def _resume(args, fmt: str, queries: list[str]) -> list[str]:
    if args.output is None or fmt != "jsonl":
        raise ValueError("--resume needs --output FILE in the jsonl format")
    settings = _settings(args, pages=args.pages)
    # Only records made with the same language, country, location and filters count.
    same = {"hl": settings.hl, "gl": settings.gl, **filter_params(settings)}
    done = answered(args.output, args.pages, same)
    total = sum(1 for q in queries if q.strip())
    left = [q for q in queries if q.strip() and q.strip() not in done]
    print(
        f"resume: {total - len(left)} of {total} queries already in {args.output}", file=sys.stderr
    )
    return left


def _search(args) -> int:
    fmt = args.format or _format_for(args.output)
    queries = _read_queries(args)
    if args.resume:
        queries = _resume(args, fmt, queries)
        if not queries:
            return 0
    # Every option is checked before anything starts: no browser download, no
    # demo sub-user, and no existing output file emptied for a bad option.
    _settings(args, pages=args.pages)
    _ready(args)
    progress = None
    if not args.verbose:
        total = sum(1 for q in queries if q.strip())
        progress = Progress(total=total, breaker=Settings().breaker)
    failed: set[str] = set()
    code = 0
    with _proxy_source(args) as proxy:
        mode = "a" if args.resume else "w"
        stream = args.output.open(mode, encoding="utf-8", newline="") if args.output else sys.stdout
        writer = open_writer(fmt, stream)
        try:
            for attempt in range(PATIENT_WAITS + 1):
                if args.workers != 1:
                    scraper = Pool(
                        lambda i: _build(
                            args, proxy, pages=args.pages, worker=i, progress=progress
                        )[0],
                        args.workers,
                    )
                else:
                    scraper, _ = _build(args, proxy, pages=args.pages, progress=progress)
                done, stopped = _run(scraper, queries, writer, progress, failed, args.price_per_gb)
                code = 3 if stopped else 0
                queries = [q for q in queries if q.strip() and q.strip() not in done]
                again = stopped == "breaker" and args.patient
                if not again or attempt == PATIENT_WAITS or not queries:
                    break
                print(
                    f"waiting {args.patient:g} min, then going on with the {len(queries)} "
                    f"queries left ({attempt + 1} of {PATIENT_WAITS})",
                    file=sys.stderr,
                )
                time.sleep(args.patient * 60)
        finally:
            writer.close()
            if args.output:
                stream.close()
    return code or (4 if failed else 0)


def _run(
    scraper, queries, writer, progress, failed: set[str], price
) -> tuple[set[str], str | None]:
    """One scraper over the queries: the queries it answered with every page, and
    what stopped it - "breaker", "proxy" (the account was refused, which waiting
    does not change) or None. `failed` keeps the queries whose last answer failed."""
    done: set[str] = set()
    stopped = None
    try:
        for record in scraper.run(queries):
            q = record["search_parameters"]["q"].strip()
            if record["search_metadata"]["status"] == "Success":
                done.add(q)
                failed.discard(q)
            else:
                failed.add(q)
                done.discard(q)  # a later page failed: the query is not complete
            writer.write(record)
            if progress is not None:
                progress.record(record)
    except ExitsRefused as exc:
        # A pool worker that failed on setup (no browser, no display)
        # arrives wrapped here; its fix is one line too.
        if not hints.report(exc):
            print(f"stopped: {exc}", file=sys.stderr)
        stopped = "proxy" if isinstance(exc, ProxyRefused) else "breaker"
    finally:
        not_run = getattr(scraper, "not_run", [])
        if not_run:
            print(f"not run: {len(not_run)} queries", file=sys.stderr)
        summary = scraper.stats.summary(price)
        if progress is not None:
            progress.summary(summary)
        else:
            print("summary: " + json.dumps(summary), file=sys.stderr)
    return done, stopped


def _serve(args) -> int:
    from .server import Worker, serve

    _ready(args)
    progress = None if args.verbose else Progress(breaker=Settings().breaker)
    with _proxy_source(args) as proxy:
        worker = Worker(
            lambda: _build(args, proxy, progress=progress)[0], prewarm=args.prewarm
        ).start()
        serve(worker, host=args.host, port=args.port, max_pages=args.max_pages)
    return 0


def _mcp(args) -> int:
    from .mcp import McpServer
    from .server import Worker

    # stdin is the MCP client's, so nobody can be asked: a missing browser is an error.
    _ready(args, ask=False)
    progress = None if args.verbose else Progress(breaker=Settings().breaker)
    with _proxy_source(args) as proxy:
        worker = Worker(
            lambda: _build(args, proxy, progress=progress)[0], prewarm=args.prewarm
        ).start()
        try:
            McpServer(worker).serve()
        finally:
            worker.stop()
    return 0


def _cleanup_subuser(proxy, keep: bool) -> None:
    close = getattr(proxy, "close", None)
    if close is None:
        return
    try:
        close(keep=keep)
    except Exception as exc:
        print(f"warning: could not delete the provisioned sub-user: {exc}", file=sys.stderr)


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

    if args.engine == "patchright" and not args.channel:
        installer.ensure_browser()

    browser_args = tuple(args.browser_args) + tuple(shlex.split(env.get("BROWSER_ARGS", "")))
    fp = read_fingerprint(
        engine=args.engine, headless=args.headless, channel=args.channel, browser_args=browser_args
    )
    checks = assess(fp)
    width = max(len(c.name) for c in checks)
    print(f"engine: {args.engine}")
    for c in checks:
        print(f"[{c.status:4}] {c.name:<{width}}  {c.detail}")
    if any(c.status == FAIL for c in checks):
        print("not ready: fix the [fail] lines above first")
        return 1
    print("ready: run serpmaven search with your proxy")
    return 0


def _html_dir(folder: Path | None, worker: int | None) -> str | None:
    """Each worker numbers its pages from 1, so each gets a folder of its own."""
    if folder is None:
        return None
    return str(folder if worker is None else folder / f"w{worker}")


def _minutes(text: str) -> float:
    try:
        value = float(text)
    except ValueError:
        raise argparse.ArgumentTypeError("expected a number of minutes") from None
    if not math.isfinite(value) or value < 0:
        raise argparse.ArgumentTypeError("must be a finite number, 0 or more")
    return value


def _non_negative(text: str) -> int:
    try:
        value = int(text)
    except ValueError:
        raise argparse.ArgumentTypeError("expected a whole number") from None
    if value < 0:
        raise argparse.ArgumentTypeError("must be 0 or more")
    return value


def _pair(text: str) -> tuple[float, float]:
    try:
        low, high = (float(x) for x in text.split(","))
    except ValueError:
        raise argparse.ArgumentTypeError("expected LOW,HIGH, e.g. 2,5") from None
    return low, high


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())


def _login(args, *, prompt=input, secret=None) -> int:
    if args.proxy:
        ProxyTemplate(args.proxy)  # refuse a malformed URL before saving it
        where = config.save({"proxy": args.proxy})
        print(f"saved the proxy URL to {where}")
        return 0
    if not installer.interactive() and prompt is input:
        raise ValueError(
            "run `serpmaven login` in a terminal to type the login, or save a proxy URL with "
            "`serpmaven login --proxy URL`"
        )
    if secret is None:
        import getpass

        secret = getpass.getpass
    print("NodeMaven dashboard -> Proxy Username and Proxy Password.")
    login = prompt("Proxy Username: ").strip()
    password = secret("Proxy Password (not shown): ").strip()
    if not login or not password:
        raise ValueError("both the username and the password are needed")
    where = config.save({"nodemaven_login": login, "nodemaven_password": password})
    print(f"saved to {where}, readable by your user only")
    print('next: serpmaven search "best running shoes" -o results.jsonl')
    return 0


def _logout(args) -> int:
    if config.delete():
        print(f"deleted {config.path()}")
    else:
        print("no saved login")
    return 0


def _install(args) -> int:
    code = installer.install(with_deps=args.with_deps)
    if code == 0:
        print("browser installed; check it with: serpmaven doctor")
    return code
