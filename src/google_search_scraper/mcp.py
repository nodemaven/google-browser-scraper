"""`mcp`: a Model Context Protocol server over stdio with one tool, `google_search`.

JSON-RPC 2.0, one message per line on stdin and stdout, written against the
MCP specification without the SDK so the package keeps two dependencies.
stdout carries protocol messages only; everything else goes to stderr. Both
streams are UTF-8, as the specification requires, whatever the system's
default encoding is.

Register it with an MCP client as a command, for example:

    {"command": "google-search-scraper", "args": ["mcp", "--nodemaven", "--country", "us"],
     "env": {"NODEMAVEN_LOGIN": "...", "NODEMAVEN_PASSWORD": "..."}}

The first call warms an exit and can take a minute or two; `--prewarm` starts
that at launch instead.
"""

from __future__ import annotations

import json
import sys
from typing import IO, Any

from . import __version__
from .output import use_utf8
from .scraper import ExitsRefused
from .server import Worker, search_job

SUPPORTED_VERSIONS = ("2025-06-18", "2025-03-26", "2024-11-05")
MAX_PAGES = 3

TOOL = {
    "name": "google_search",
    "title": "Google search",
    "description": (
        "Search Google through a real browser and the configured proxy. Returns "
        "organic results (position, title, link, snippet), 'People also ask' "
        "questions and related searches. The first call can take a minute or two "
        "while a proxy exit is warmed up; later calls take seconds."
    ),
    "inputSchema": {
        "type": "object",
        "properties": {
            "query": {"type": "string", "description": "The search query."},
            "pages": {
                "type": "integer",
                "minimum": 1,
                "maximum": MAX_PAGES,
                "default": 1,
                "description": "Result pages to fetch (about 10 results each).",
            },
        },
        "required": ["query"],
    },
}


def compact(records: list[dict[str, Any]]) -> dict[str, Any]:
    """What a model needs from the records, without SerpApi's bookkeeping."""
    out: dict[str, Any] = {"results": [], "related_questions": [], "related_searches": []}
    errors = []
    for record in records:
        if "error" in record:
            errors.append(record["error"])
            continue
        for r in record.get("organic_results", []):
            out["results"].append(
                {
                    k: r.get(k)
                    for k in ("position", "title", "link", "snippet")
                    if r.get(k) is not None
                }
            )
        out["related_questions"] += [q["question"] for q in record.get("related_questions", [])]
        out["related_searches"] += [s["query"] for s in record.get("related_searches", [])]
    if errors:
        out["errors"] = errors
    return out


class McpServer:
    def __init__(self, worker: Worker, *, stdout: IO[str] | None = None) -> None:
        self._worker = worker
        # One message per line: "\n" exactly, not the platform's "\r\n".
        self._out = stdout if stdout is not None else use_utf8(sys.stdout, newline="\n")

    def serve(self, stdin: IO[str] | None = None) -> None:
        if stdin is None:
            # A byte that is not UTF-8 becomes a parse error, not a crash.
            stdin = use_utf8(sys.stdin, errors="replace")
        for line in stdin:
            line = line.strip()
            if not line:
                continue
            try:
                message = json.loads(line)
            except json.JSONDecodeError:
                self._write(
                    {
                        "jsonrpc": "2.0",
                        "id": None,
                        "error": {"code": -32700, "message": "parse error"},
                    }
                )
                continue
            reply = self.handle(message)
            if reply is not None:
                self._write(reply)

    def handle(self, message: dict[str, Any]) -> dict[str, Any] | None:
        method, ident = message.get("method"), message.get("id")
        if ident is None:  # a notification: never answered
            return None
        try:
            result = self._dispatch(method, message.get("params") or {})
        except _RpcError as exc:
            return {"jsonrpc": "2.0", "id": ident, "error": {"code": exc.code, "message": str(exc)}}
        return {"jsonrpc": "2.0", "id": ident, "result": result}

    def _dispatch(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        if method == "initialize":
            asked = params.get("protocolVersion")
            return {
                "protocolVersion": asked if asked in SUPPORTED_VERSIONS else SUPPORTED_VERSIONS[0],
                "capabilities": {"tools": {"listChanged": False}},
                "serverInfo": {"name": "google-search-scraper", "version": __version__},
            }
        if method == "ping":
            return {}
        if method == "tools/list":
            return {"tools": [TOOL]}
        if method == "tools/call":
            return self._call(params)
        raise _RpcError(-32601, f"method not found: {method}")

    def _call(self, params: dict[str, Any]) -> dict[str, Any]:
        if params.get("name") != TOOL["name"]:
            raise _RpcError(-32602, f"unknown tool: {params.get('name')}")
        args = params.get("arguments") or {}
        query = str(args.get("query") or "").strip()
        if not query:
            return _tool_error("query is required")
        try:
            pages = int(args.get("pages", 1))
        except (TypeError, ValueError):
            return _tool_error("pages must be an integer")
        pages = max(1, min(MAX_PAGES, pages))
        try:
            records = self._worker.call(search_job(query, pages))
        except ExitsRefused as exc:
            return _tool_error(str(exc))
        except Exception as exc:
            return _tool_error(f"{type(exc).__name__}: {exc}")
        body = compact(records)
        return {
            "content": [{"type": "text", "text": json.dumps(body, ensure_ascii=False)}],
            "isError": not body["results"] and bool(body.get("errors")),
        }

    def _write(self, message: dict[str, Any]) -> None:
        self._out.write(json.dumps(message, ensure_ascii=False) + "\n")
        self._out.flush()


class _RpcError(Exception):
    def __init__(self, code: int, message: str) -> None:
        super().__init__(message)
        self.code = code


def _tool_error(text: str) -> dict[str, Any]:
    return {"content": [{"type": "text", "text": text}], "isError": True}
