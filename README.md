<div align="center">

<img src="https://raw.githubusercontent.com/nodemaven/google-browser-scraper/main/assets/banner.png" alt="Google Browser Scraper">

<h1>google-browser-scraper</h1>

<p><b>Self-hosted SERP API alternative: Google search results from a real Chrome browser and your own residential proxy.</b></p>

[![pypi](https://img.shields.io/pypi/v/google-browser-scraper?style=flat-square)](https://pypi.org/project/google-browser-scraper/)
[![python](https://img.shields.io/badge/python-3.10%2B-blue?style=flat-square)](https://pypi.org/project/google-browser-scraper/)
[![tests](https://img.shields.io/github/actions/workflow/status/nodemaven/google-browser-scraper/ci.yml?branch=main&style=flat-square&label=tests)](https://github.com/nodemaven/google-browser-scraper/actions/workflows/ci.yml)
[![license](https://img.shields.io/pypi/l/google-browser-scraper?style=flat-square)](https://opensource.org/licenses/MIT)
[![docker](https://img.shields.io/badge/docker-ghcr.io-2496ED?style=flat-square&logo=docker&logoColor=white)](https://github.com/nodemaven/google-browser-scraper/pkgs/container/google-browser-scraper)
[![2 GB free with code GOOGLENM](https://img.shields.io/badge/%F0%9F%8E%81%202%20GB%20free-code%20GOOGLENM-1153FC?style=flat-square)](https://dashboard.nodemaven.com/accounts/signup/?utm_source=github&utm_medium=readme&utm_campaign=google_browser_scraper&utm_content=promo_badge)

<a href="https://devhunt.org/tool/google-search-scraper"><img src="https://devhunt.org/badge/google-search-scraper.svg" alt="Google Browser Scraper on DevHunt" height="44"></a>

[Quickstart](#quickstart) · [Output](#what-you-get) · [Poisoned results](#poisoned-results-protection) · [HTTP API](#run-it-as-an-api) · [MCP](#use-it-from-an-ai-agent-mcp) · [Docker](#docker) · [How it works](#how-it-works) · [Options](#options)

</div>

<br>

<p align="center">
  <img src="https://raw.githubusercontent.com/nodemaven/google-browser-scraper/main/assets/demo.gif" alt="A real Chrome window showing Google results next to the JSON the scraper produced for them" width="900">
</p>

<div align="center">

| ✅ **100%** | 📉 **0.4 MB** | ⚡ **1.4 s** |
|:---:|:---:|:---:|
| success rate | per search | per search on a warm proxy IP |

<sub>Latest version benchmarks (v0.1)</sub>

</div>

Scrape Google search results (SERP) with a real Chrome browser and your own
residential proxy, and get clean JSON back. Use it as a CLI, a Python library,
a local HTTP API, or an MCP server that gives AI agents live Google search.

Plain HTTP is no longer a reliable way to scrape Google, and a fresh browser on a
fresh proxy IP is refused quickly. So the scraper warms a sticky proxy session
with ordinary browsing, lets the browser pass Google's checks, and then fetches
results pages directly over HTTPS with the browser's own cookies. Captchas and
blocks are reported as such, never as "no results".

## Features

<table>
<tr>
<td width="50%" valign="top">

⚡ **Hybrid mode**<br>
Results pages over HTTPS between Google's checks: a fraction of a browser's traffic and time. The browser takes over by itself when needed.

</td>
<td width="50%" valign="top">

🛡️ **Poisoned results protection**<br>
Pages of results for some other query are caught and never returned; the query is asked again on a fresh proxy IP.

</td>
</tr>
<tr>
<td valign="top">

🌐 **Real browser**<br>
Real Chrome via Patchright, warmed up before every new identity, so Google's checks pass.

</td>
<td valign="top">

🧩 **SERP API-style JSON**<br>
`organic_results`, `ads`, `related_questions`, `related_searches`, `pagination`, `ai_overview`, `local_results`.

</td>
</tr>
<tr>
<td valign="top">

🔌 **Bring your own proxy**<br>
Any residential proxy with sticky sessions, built-in [NodeMaven](https://nodemaven.com/?utm_source=github&utm_medium=readme&utm_campaign=google_browser_scraper&utm_content=features) support.

</td>
<td valign="top">

🚦 **Honest failures**<br>
Captchas and blocks come back labeled, not as empty results.

</td>
</tr>
<tr>
<td valign="top">

🤖 **For AI agents**<br>
An MCP server that gives agents live Google search, with the AI Overview and map places.

</td>
<td valign="top">

🐳 **Run it your way**<br>
CLI, Python library, HTTP API, MCP server or Docker image. Traffic and cost per 1000 pages in every run summary.

</td>
</tr>
</table>

## Use cases

- SEO rank tracking and SERP monitoring for your own keywords
- Keyword and competitor research from live Google results
- Web search for AI agents and LLM apps through MCP
- A self-hosted alternative to paid Google search APIs when you already pay for proxies

## Install

```bash
pip install google-browser-scraper
patchright install chromium
```

## Quickstart

> [!NOTE]
> You need a residential proxy with **sticky sessions**. Put `{session}` where
> your provider expects a session id in the username.

```bash
google-browser-scraper search "best running shoes" \
  --proxy "http://USER-session-{session}:PASS@gate.example.com:7000" \
  -o results.jsonl
```

The first query takes a few minutes while a new proxy IP warms up; later queries on
the same proxy IP take seconds. Every run ends with a summary of pages served and
traffic used.

### With NodeMaven

The session id is handled for you and the credentials come from the environment:

```bash
pip install "google-browser-scraper[nodemaven]"
export NODEMAVEN_LOGIN=... NODEMAVEN_PASSWORD=...
google-browser-scraper search -f queries.txt --nodemaven --country us -o results.jsonl
```

> [!TIP]
> 🎁 **2 GB of free traffic.** No NodeMaven account yet? [Sign up](https://dashboard.nodemaven.com/accounts/signup/?utm_source=github&utm_medium=readme&utm_campaign=google_browser_scraper&utm_content=signup_promo) and enter the code **GOOGLENM**. Once per account.

<details>
<summary><b>Cap a run's traffic with a demo sub-user</b></summary>

<br>

`--nodemaven-demo` creates a sub-user with a traffic limit from your dashboard API
key, shares it across workers, and deletes it when the run ends (`--keep-subuser`
keeps it). The account needs sub-users enabled. A fresh warm-up alone uses about
60 MB, so keep the cap well above that:

```bash
export NODEMAVEN_API_KEY=...
google-browser-scraper search -f queries.txt --nodemaven-demo --traffic-limit-mb 300 -o results.jsonl
```

</details>

<details>
<summary><b>Try it without a proxy</b></summary>

<br>

```bash
google-browser-scraper search "best running shoes" --no-proxy
```

Google then sees one address for every query, so it starts answering with
captchas sooner than it would across a pool of proxy IPs. Those pages come back as
`captcha` in `page_verdict`, not as empty results.

</details>

### From Python

```python
from google_browser_scraper import ProxyTemplate, Scraper, Settings

proxy = ProxyTemplate("http://USER-session-{session}:PASS@gate.example.com:7000")
for page in Scraper(proxy, Settings(pages=2)).run(["best running shoes"]):
    for result in page.get("organic_results", []):
        print(result["position"], result["title"], result["link"])
```

## What you get

One JSON object per results page:

```json
{
  "search_metadata": {"status": "Success", "page_verdict": "ok", "fetched_with": "http"},
  "search_parameters": {"engine": "google", "q": "best running shoes", "page": 1},
  "organic_results": [
    {"position": 1, "title": "...", "link": "https://...", "snippet": "..."}
  ],
  "related_questions": [{"question": "..."}],
  "ai_overview": {"text": "..."},
  "exit": {"session": "...", "queries_on_exit": 1, "bytes": "..."}
}
```

The field names are the ones common to SERP APIs, so simple consumers can often
switch with minimal mapping.

<details>
<summary><b>All fields</b></summary>

<br>

- `organic_results`, `ads`, `related_questions`, `related_searches`, `pagination`, `search_metadata`
- `ai_overview`: the text of Google's AI Overview when the page shows one (pages in English, `--hl en`, the default)
- `local_results`: the places of the map pack on local queries (name, rating, reviews, type, address, phone, opening hours)
- `exit`: which proxy IP (sticky session) answered and how many bytes the page cost
- `search_metadata.fetched_with`: `http` or `browser`
- `search_metadata.timings`: seconds per step (`http` for a page fetched over HTTP; `type`, `load`, `ready` and `read` for one the browser rendered; `parse`; `links`, which overlaps the next query)
- A query that failed is a record too, with an `error`

Knowledge panels, shopping, images and other special blocks are not parsed.
`--format csv` writes the organic results as a table, and `--price-per-gb` adds
the cost per 1000 pages to the summary.

</details>

## Poisoned results protection

Google does not always block a scraper. Sometimes it answers with a normal-looking
results page for a different query: no captcha, no error, just wrong data that
slips into your pipeline unnoticed.

The scraper checks every results page against the query it asked. When a proxy IP
starts returning unrelated results, it is dropped and the query is asked again on
a fresh one, so poisoned pages never reach your output.

- No extra requests, no extra traffic: the check runs on the page already downloaded
- On by default

## Run it as an API

```bash
google-browser-scraper serve --nodemaven --country us --prewarm
curl "http://127.0.0.1:8000/search.json?q=best+running+shoes"
```

- Same record format as above
- `hl` (language) and `gl` (country) per request: `/search.json?q=...&hl=de&gl=at`
- `--prewarm` finds a proxy IP Google serves at startup, so the first request does not wait for a warm-up; `/health` answers 503 `warming` until then
- Requests run one at a time on one held proxy IP
- Set `GBS_API_KEY` to require a key; without one the server only listens on localhost

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

One tool, `google_search(query, pages, hl, gl)`. It returns the organic results,
"People also ask", related searches, and the AI Overview and map places when
Google shows them.

## Docker

A ready image is published to GitHub Container Registry with every release:

```bash
docker run --rm ghcr.io/nodemaven/google-browser-scraper doctor
docker run --rm -p 8000:8000 -e GBS_API_KEY=change-me \
  -e GBS_PROXY='http://USER-session-{session}:PASS@gate.example.com:7000' \
  ghcr.io/nodemaven/google-browser-scraper serve --host 0.0.0.0
```

To build it yourself: `docker build -t google-browser-scraper .`

## How it works

1. **New identity:** a sticky proxy session and a fresh browser profile.
2. **Warm-up:** a few ordinary pages, staying on each for 20-45 seconds.
3. **Search:** the first query on a proxy IP goes through the browser. Once Google
   serves it, the next pages come over HTTPS with the browser's cookies, until
   Google asks for its check again.
4. **Check before parsing:** a captcha or a block is reported as one, never
   parsed as an empty result.
5. **Keep or drop:** a proxy IP Google answers is reused; one it refuses is dropped
   and the query retried on a new one. If one proxy IP after another is refused, the run
   stops instead of spending more traffic.

<details>
<summary><b>More detail: HTTP between checks, holding proxy IPs, copied cookies, result links, traffic</b></summary>

<br>

**HTTP between checks.** After the browser passes Google's check, results pages
are fetched over HTTPS with the browser's own headers and cookies. If that stops
working, the scraper goes back to the browser on its own. `--no-hybrid` uses the
browser for every page.

**Unrelated results.** A proxy IP that starts returning results unrelated to the
queries is dropped, and the query is asked again on a new one.

**Holding proxy IPs.** A served proxy IP is kept for up to 300 queries. Every new one
pays for a warm-up, so a short cap mostly buys more warm-up traffic. Every 50
results pages it renders, the browser is restarted on the same profile and proxy IP, which keeps
its memory from growing over a long hold. A browser error on a search is retried
once on the same proxy IP before it is dropped; a network error, running out of
memory or a crashed browser drops it at once.

**Copying cookies.** The command line copies the cookies of the first proxy IP
Google serves and starts new proxy IPs from that copy, on new sticky sessions,
instead of warming each one; all workers share the copy. Google's acceptance
follows the cookies more than the address, so a copy usually starts out served
and costs a few MB instead of a full warm-up. A copy waits 40-60 seconds after
its browser starts before the first search.

**Copying the cache.** Pages get cheaper as a proxy IP is held, because the
browser cache fills with Google's shared scripts and images. Copies carry that
cache too, so a new proxy IP starts with cheap pages, and the copy is refreshed
from the held one at each browser restart, when its cache is warm. If two
copies carrying the cache are refused on their first search, the rest of the
run copies cookies only (`--no-clone-cache` does that from the start).

**Result links.** Google wraps result links in encrypted `/goto` redirects.
Each one is resolved with a small request through a separate short-lived proxy
session, a few at a time, in the background while the browser goes on to the
next query. Records still come out in the order each browser searched them.

**Traffic.** Everything goes through a small local relay that counts bytes per
host and blocks a large model download Chrome otherwise makes on every fresh
profile.

</details>

The defaults come from measurements in
[proxy-benchmark](https://github.com/nodemaven/proxy-benchmark).

## Options

<details>
<summary><b>All search options</b></summary>

<br>

| Option | Default | What it does |
|---|---|---|
| `--workers N` | 1 | Search on N proxy IPs at once, one browser each, from one list of queries |
| `--max-per-exit N` | 300 | Queries before a served proxy IP is retired |
| `--restart-every N` | 50 | Results pages the browser rendered before it restarts on the same profile and proxy IP; `0` never |
| `--no-hybrid` | | Fetch every results page in the browser instead of over HTTP between Google's checks |
| `--hl`, `--gl` | `en`, unset | Language and country of the results |
| `--clone N` | 10 | New proxy IPs started from copied cookies; `0` turns copying off |
| `--clone-max-age` | 180 | Minutes copied cookies stay in use |
| `--clone-refusals N` | 1 | Copies refused in a row on their first query that end the copying |
| `--no-clone-cache` | | Copies carry cookies only, not the browser cache |
| `--warmup` | `L3` | Pages a fresh proxy IP opens before its first query: `L3`, `light` (without Google Trends: much less traffic, served on the first query less often), `L2`, `L1`, `off` |
| `--spare N` | 0 | Proxy IPs warmed in the background so a retired one is replaced at once; each costs a warm-up |
| `--direct` | `all` | Open results pages directly: `all`, `follow-ups` (all but the first query on each proxy IP), `none` |
| `--input` | `type` | How a query goes into the search box: `type`, `insert`, `words` |
| `--gap LOW,HIGH` | 2,5 | Seconds between queries on one proxy IP |
| `--pages N` | 1 | Result pages per query |
| `--format` | `jsonl` | `jsonl` or `csv` |
| `--price-per-gb` | | Your proxy price, to print the cost per 1000 pages |
| `--no-resolve-links` | | Keep result links as Google `/goto` redirects |
| `--save-html DIR` | | Keep every page's HTML, gzipped |

`google-browser-scraper search --help` lists the rest.

</details>

## Proxies

Any provider with sticky sessions works through `--proxy` and a `{session}`
placeholder. A proxy without one is accepted with a warning: if the proxy IP changes
on every request, warming it does nothing. SOCKS5 needs an IP-whitelisted
endpoint, because Chrome cannot send a SOCKS5 username and password.

## Check your machine

```bash
google-browser-scraper doctor
```

Starts the browser on a blank page, sends nothing, and reports what a website
would see: whether the browser looks headless, whether WebGL works, the screen
size. Google answers some machines much less than others, so run this first.

## What this is not

Not a free Google Search API, and not a guarantee that every proxy IP will
work. It is a browser-based collector that makes failures explicit, drops proxy IPs
Google refuses, and tells you what each page cost.

## Status

Early. The `cloak` engine (`--engine cloak`) is experimental; `patchright` is
the default.

Google changes its pages often. If results come back empty or wrong, run with
`--save-html DIR` and open an issue with what you see.

You are responsible for using this within Google's terms and the law where you
run it.

## Adding an engine

An engine joins this tool only after it has been measured on
[proxy-benchmark](https://github.com/nodemaven/proxy-benchmark). Add it there
and run the warm-up ladder against Google:

```bash
python scripts/run_ladder.py --engines your-engine
```

Then open a pull request here with the run file. The ladder runs identities
with and without the warm-up in the same hours, so the result shows whether the
engine actually gets served, not whether it got lucky once.

## License

[MIT](https://github.com/nodemaven/google-browser-scraper/blob/main/LICENSE)
