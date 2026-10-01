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
