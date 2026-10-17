<p align="center">
  <img src="https://raw.githubusercontent.com/nodemaven/google-browser-scraper/main/assets/banner.png" alt="Google Browser Scraper">
</p>

# google-browser-scraper

Google Search results from a real browser, through your own sticky proxy.

Plain HTTP is no longer a reliable way to scrape Google: it often expects a
browser that runs JavaScript, and a fresh browser on a fresh proxy exit can be
refused quickly. This tool takes a browser path instead. It warms a sticky proxy
session with ordinary browsing, opens Google's results page for each query in
that browser, checks whether Google served results or refused, and writes one
JSON record per results page. Captchas, blocks and failures are reported as such, never as "no results".

**Run it on Windows** - a desktop or a Windows VPS. That is where it is tested
and where Google serves it. On a Linux server without a GPU, Google refused
almost every search in testing; Chrome has no WebGL there, which is the main
suspect, so Linux and Docker are experimental for now. `google-browser-scraper
doctor` shows what your machine looks like to a website.

## Install

```
pip install google-browser-scraper
patchright install chromium
```

## Quickstart

You need a residential proxy with **sticky sessions**. Put `{session}` where
your provider expects a session id in the username:

```
google-browser-scraper search "best running shoes" \
  --proxy "http://USER-session-{session}:PASS@gate.example.com:7000" \
  -o results.jsonl
```

Success is a `results.jsonl` with one JSON object per results page:

```json
{
  "search_metadata": {"status": "Success", "page_verdict": "ok"},
  "search_parameters": {"engine": "google", "q": "best running shoes", "page": 1},
  "organic_results": [
    {"position": 1, "title": "...", "link": "https://...", "snippet": "..."}
  ],
  "related_questions": [{"question": "..."}],
  "exit": {"session": "...", "queries_on_exit": 1, "bytes": "..."}
}
```

The first query takes a few minutes, because a new exit is warmed up first, and
the warm-up costs noticeably more traffic than a search. Later queries on the
same exit take seconds. A summary of pages served and traffic used, with the
hosts that took most of it, is printed at the end of every run.

With NodeMaven, the session id is handled for you and the credentials come from
the environment:

```
pip install "google-browser-scraper[nodemaven]"
export NODEMAVEN_LOGIN=... NODEMAVEN_PASSWORD=...
google-browser-scraper search -f queries.txt --nodemaven --country us -o results.jsonl
```

To try it without a proxy, use your own connection:

```
google-browser-scraper search "best running shoes" --no-proxy
```

Google then sees one address for every query, so it starts answering with
captchas sooner than it would across a pool of exits. Those pages come back as
`captcha` in `page_verdict`, not as empty results.

From Python:

```python
from google_browser_scraper import ProxyTemplate, Scraper, Settings

proxy = ProxyTemplate("http://USER-session-{session}:PASS@gate.example.com:7000")
for page in Scraper(proxy, Settings(pages=2)).run(["best running shoes"]):
    for result in page.get("organic_results", []):
        print(result["position"], result["title"], result["link"])
```

## What you get

The schema is intentionally SerpApi-like: `organic_results`, `ads`,
`related_questions`, `related_searches`, `pagination` and `search_metadata`, so
simple consumers can often switch with minimal mapping. Knowledge panels,
shopping, images and other special blocks are not parsed.

Each record also has an `exit` block: which proxy session answered and how many
bytes the page cost. `search_metadata.timings` says where the page's time went,
in seconds per step (`type`, `load`, `ready`, `read`, `parse`, and `links`, which
overlaps the next query and is not part of `total_time_taken`). A query
that failed is a record too, with an `error`.
`--format csv` writes the organic results as a table, and `--price-per-gb` adds
the cost per 1000 pages to the summary.

## How it works

1. **New identity**: a sticky proxy session and a fresh browser profile.
2. **Warm-up**: a few ordinary pages.
3. **Search**: each query opens Google's results page directly, which is
   faster than the search box and skips the autocomplete traffic of typing.
   `--direct follow-ups` sends an exit's first query through the box and
   `--direct none` every query; `--input` sets how a query goes into the box
   (`type` key by key, `insert` all at once, `words` word by word).
4. **Check before parsing**: the page is classified first, so a captcha or a
   block is reported as one and never parsed as an empty result.
5. **Keep or drop**: an exit Google answers is reused for the next queries; an
   exit it refuses is dropped and the query is retried on a new one. If exit
   after exit is refused, the run stops instead of spending more traffic.

A served exit is kept for up to 50 queries (`--max-per-exit`). Every new exit
pays for a warm-up, so a short cap mostly buys more warm-up traffic. A browser
error on a search is retried once on the same exit (through Google's front page
when queries go through the search box) before the exit is dropped; a network
error drops it at once.

`--workers N` searches on N exits at once, one browser each, sharing one list
of queries. Each browser needs memory and CPU of its own. The run stops when
exit after exit is refused with no worker served in between.

The command line copies the cookies of the first exit Google serves and
starts up to 10 new exits from that copy (`--clone N`, `--clone 0` turns it
off), on new sticky sessions, instead of warming each one; all workers share
the copy. Google's acceptance follows the cookies more than the address, so a
copy usually starts out served, and costs a few MB instead of a full warm-up.
A copy refused on its first query ends the copying; the copied cookies are
used for three hours at most (`--clone-max-age`). A copy waits 40-60 seconds
after its browser starts before the first search.

`--spare 1` warms the next exit in the background once the current one is
served, so a retired exit is replaced without waiting for a warm-up. A spare
costs a warm-up's traffic whether it is used or not.

Google wraps result links in encrypted `/goto` redirects. Each one is resolved
with a small request through a separate short-lived proxy session, a few at a
time, so link resolution does not touch the warmed browser session. It runs in
the background while the browser goes on to the next query; records still come
out in order. `--no-resolve-links` skips it.

Traffic goes through a small local relay that counts bytes and blocks a large
model download Chrome otherwise makes on every fresh profile.

The defaults come from measurements in
[proxy-benchmark](https://github.com/nodemaven/proxy-benchmark).

## Proxies

Any provider with sticky sessions works through `--proxy` and a `{session}`
placeholder. A proxy without one is accepted with a warning: if the exit changes
on every request, warming it does nothing. SOCKS5 needs an IP-whitelisted
endpoint, because Chrome cannot send a SOCKS5 username and password.

## Run it as an API

```
google-browser-scraper serve --nodemaven --country us --prewarm
curl "http://127.0.0.1:8000/search.json?q=best+running+shoes"
```

Same record format as above. Requests run one at a time on one held identity.
Set `GBS_API_KEY` to require a key; without one the server only listens on
localhost.

## Use it from an AI agent (MCP)

```json
{
  "mcpServers": {
    "google-search": {
      "command": "google-browser-scraper",
      "args": ["mcp", "--nodemaven", "--country", "us", "--prewarm"],
      "env": {"NODEMAVEN_LOGIN": "...", "NODEMAVEN_PASSWORD": "..."}
    }
  }
}
```

It exposes one tool, `google_search(query, pages)`.

## Docker

```
docker build -t google-browser-scraper .
docker run --rm google-browser-scraper doctor
docker run --rm -p 8000:8000 -e GBS_API_KEY=change-me \
  -e GBS_PROXY='http://USER-session-{session}:PASS@gate.example.com:7000' \
  google-browser-scraper serve --host 0.0.0.0
```

## Check your machine

```
google-browser-scraper doctor
```

Starts the browser on a blank page, sends nothing, and reports what a website
would see: whether the browser looks headless, whether WebGL works, the screen
size. Google answers some machines much less than others, so run this first.

## What this is not

Not a free Google Search API, and not a guarantee that every proxy exit will
work. It is a browser-based collector that makes failures explicit, drops exits
Google refuses, and tells you what each page cost.

## Status

Early. Windows is the tested and recommended platform. **Linux and Docker are
experimental**: on a machine without a GPU, Chrome has no WebGL, which a normal
desktop always has; the Docker image turns on a software renderer for it, and
whether Google serves that is not measured yet. The `cloak` engine
(`--engine cloak`) is experimental too; `patchright` is the default.

Google changes its pages often. If results come back empty or wrong, run with
`--save-html DIR` and open an issue with what you see.

You are responsible for using this within Google's terms and the law where you
run it.

## Adding an engine

An engine joins this tool only after it has been measured on
[proxy-benchmark](https://github.com/nodemaven/proxy-benchmark): add it there,
run the warm-up ladder against Google -

```
python scripts/run_ladder.py --engines your-engine
```

- and open a pull request here with the run file. The ladder runs identities
with and without the warm-up in the same hours, so the result shows whether the
engine actually gets served, not whether it got lucky once.

## License

MIT
