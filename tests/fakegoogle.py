"""A fake Google front page and results page, served from 127.0.0.1."""

import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit

import pages

# Google's box is a textarea whose Enter is handled by script; a plain textarea
# would insert a newline instead of submitting.
BOX = (
    """<textarea name="q" onkeydown="if (event.key === 'Enter') """
    """{ event.preventDefault(); this.form.submit(); }">{query}</textarea>"""
)

HOME = (
    """<html><body>
<div id="overlay" style="position:fixed;inset:0;background:#fff;z-index:9">
  <button id="L2AGLb" onclick="document.getElementById('overlay').remove()">Accept</button>
</div>
<form action="/search">"""
    + BOX.replace("{query}", "")
    + """</form>
</body></html>"""
)

# Front pages that interfere once, on the fifth character typed: one moves the
# focus elsewhere, the other replaces the box with a fresh empty one. Either
# leaves only the start of the query in the box the browser was typing into.
_ONCE = """<script>
let typed = 0, done = false;
document.addEventListener('input', (event) => {
  if (done || event.target.name !== 'q' || ++typed < 5) return;
  done = true;
  %s
});
</script>"""
FOCUS_THIEF = HOME.replace(
    "</body>",
    '<button id="thief">x</button>'
    + _ONCE % "document.getElementById('thief').focus();"
    + "</body>",
)
BOX_SWAP = HOME.replace(
    "</body>", _ONCE % "event.target.replaceWith(event.target.cloneNode(false));" + "</body>"
)


#: Show a consent wall over the results to a browser that has not accepted yet.
CONSENT_ON_SEARCH = False
CONSENT_WALL = (
    '<div id="overlay" style="position:fixed;inset:0;background:#fff;z-index:9">'
    '<button id="L2AGLb" onclick="document.cookie=\'SOCS=1; path=/\'; '
    "document.getElementById('overlay').remove()\">Accept</button></div>"
)


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):  # noqa: N802 - the stdlib's name
        parts = urlsplit(self.path)
        if parts.path == "/search":
            query = parse_qs(parts.query).get("q", [""])[0]
            body = pages.results_page().replace("swimming - Google Search", query)
            box = BOX.replace("{query}", query)
            body = body.replace(
                '<div id="search">', f'<div id="search"><form action="/search">{box}</form>'
            )
            if CONSENT_ON_SEARCH and "SOCS=" not in (self.headers.get("Cookie") or ""):
                body = body.replace("<body>", "<body>" + CONSENT_WALL, 1)
        elif parts.path == "/focus-thief":
            body = FOCUS_THIEF
        elif parts.path == "/box-swap":
            body = BOX_SWAP
        else:
            body = HOME
        data = body.encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, *args):
        pass


def start():
    """Serve the fake on a free port; returns (server, base_url)."""
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, f"http://127.0.0.1:{server.server_port}"
