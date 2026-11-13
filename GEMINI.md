# SERP Maven: live Google search

This extension gives you the `google_search` tool: Google results from a real Chrome
browser through the user's residential proxy, as structured data.

## Before the first search

The tool needs a few things set up once, in a terminal (not through the tool):

0. [uv](https://docs.astral.sh/uv/), which runs the server: `curl -LsSf https://astral.sh/uv/install.sh | sh`
   on macOS and Linux, `powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 | iex"`
   on Windows, then a new terminal.
1. A proxy login: `uvx serpmaven login`. It asks for the NodeMaven Proxy Username and
   Proxy Password, or `uvx serpmaven login --proxy "http://USER-session-{session}:PASS@host:port"`
   for any other provider with sticky sessions. No account yet: NodeMaven gives 2 GB free
   with the code GOOGLENM.
2. The browser, about 150 MB once: `uvx serpmaven install`.

If the serpmaven server does not start, or the tool answers that there is no proxy or that
the browser is not installed, tell the user which of these commands to run, then restart
the session.

## Using `google_search`

- Arguments: `query` (required); `pages` (1-3, default 1); `hl` (interface language, e.g.
  `en`); `gl` (country the results are for, e.g. `us`).
- The first call of a session can take a few minutes while a fresh proxy IP warms up;
  later calls take seconds. Say so instead of retrying.
- Results come with position, title, link and snippet, plus "People also ask", related
  searches, the AI Overview and local places when Google shows them. Cite the links you use.
- Ask one query per call and keep `pages` at 1 unless deeper results are needed: each page
  is proxy traffic the user pays for.
