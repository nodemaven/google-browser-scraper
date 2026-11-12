"""Result records and the writers that put them on disk.

One record per results page, shaped like the usual SERP API JSON plus an `exit` block of
our own that says which identity answered. A failed query is a record too, with
`search_metadata.status == "Error"` and an `error` string, so a run's output
accounts for every query it was given.
"""

from __future__ import annotations

import csv
import io
import json
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import IO, Any


def use_utf8(stream: IO[str], **options: str) -> IO[str]:
    """Switch a standard stream to UTF-8, whatever the system's default is.

    A redirected or piped stream otherwise uses the locale's code page - cp1251
    or cp1252 on Windows - which cannot encode most of what a results page
    contains and garbles what it reads. An interactive console is unaffected
    either way. Streams that cannot be reconfigured are left as they are.
    """
    reconfigure = getattr(stream, "reconfigure", None)
    if callable(reconfigure):
        try:
            reconfigure(encoding="utf-8", **options)
        except (ValueError, io.UnsupportedOperation):
            pass
    return stream


ORGANIC_COLUMNS = (
    "query",
    "page",
    "position",
    "title",
    "link",
    "displayed_link",
    "source",
    "date",
    "snippet",
)


def build_record(
    query: str,
    *,
    page: int,
    verdict,
    url: str | None,
    elapsed: float,
    exit_info: dict[str, Any],
    parsed: dict[str, Any] | None = None,
    error: str | None = None,
    hl: str = "en",
    gl: str | None = None,
    timings: dict[str, float] | None = None,
    params: dict[str, str] | None = None,
) -> dict[str, Any]:
    ok = error is None and parsed is not None
    record: dict[str, Any] = {
        "search_metadata": {
            "id": uuid.uuid4().hex,
            "status": "Success" if ok else "Error",
            "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "total_time_taken": round(elapsed, 2),
            "google_url": url,
            "page_verdict": getattr(verdict, "kind", None),
            "verdict_reason": getattr(verdict, "reason", None),
        },
        "search_parameters": {
            "engine": "google",
            "q": query,
            "page": page,
            "hl": hl,
            "device": "desktop",
        },
    }
    if gl:
        record["search_parameters"]["gl"] = gl
    record["search_parameters"].update(params or {})
    if timings:
        # Seconds per step of this page: home, type, load, ready, read, parse, links.
        record["search_metadata"]["timings"] = timings
    if parsed:
        record.update(parsed)
    record["exit"] = exit_info
    if error is not None:
        record["error"] = error
    return record


class JsonlWriter:
    """One record per line, flushed as it arrives, so a killed run keeps what
    it already paid for."""

    def __init__(self, stream: IO[str]) -> None:
        self._stream = stream

    def write(self, record: dict[str, Any]) -> None:
        self._stream.write(json.dumps(record, ensure_ascii=False) + "\n")
        self._stream.flush()

    def close(self) -> None:
        pass


class JsonWriter:
    """A single JSON array, written when the run ends."""

    def __init__(self, stream: IO[str]) -> None:
        self._stream = stream
        self._records: list[dict[str, Any]] = []

    def write(self, record: dict[str, Any]) -> None:
        self._records.append(record)

    def close(self) -> None:
        json.dump(self._records, self._stream, ensure_ascii=False, indent=2)
        self._stream.write("\n")


class CsvWriter:
    """Organic results only, one row each. Pages that failed write no rows;
    use JSONL when the failures matter."""

    def __init__(self, stream: IO[str]) -> None:
        self._writer = csv.DictWriter(stream, fieldnames=ORGANIC_COLUMNS, extrasaction="ignore")
        self._writer.writeheader()
        self._stream = stream

    def write(self, record: dict[str, Any]) -> None:
        params = record["search_parameters"]
        for result in record.get("organic_results", []):
            self._writer.writerow({"query": params["q"], "page": params["page"], **result})
        self._stream.flush()

    def close(self) -> None:
        pass


WRITERS = {"jsonl": JsonlWriter, "json": JsonWriter, "csv": CsvWriter}


def open_writer(fmt: str, stream: IO[str]):
    try:
        return WRITERS[fmt](stream)
    except KeyError:
        raise ValueError(f"format must be one of {', '.join(WRITERS)}, got {fmt!r}") from None


#: The search_parameters that make two records answers to different searches.
SAME_SEARCH = ("hl", "gl", "location", "tbs", "pws", "nfpr", "cr", "lr")


def answered(path: Path, pages: int = 1, params: dict[str, Any] | None = None) -> set[str]:
    """The queries a JSON Lines output file already holds every page for, for a
    run that resumes into it with `pages` pages per query and the language,
    country, location and filters in `params` (records made with others are
    another search and do not count). Each query counts by
    its latest attempt (a record for page 1 starts one): it is complete when every
    page so far succeeded and the last one is page `pages` or has no next page on
    Google. So a query that failed and was then answered is not asked again; one
    with a failed page, or cut short by a killed run, is. The file is read a line
    at a time, split on newlines only. A last line cut off by a killed run is
    removed from the file, so the records appended after it stay one per line."""
    # query -> (every page so far succeeded, last page, Google has another page)
    state: dict[str, tuple[bool, int, bool]] = {}
    end = 0
    try:
        with path.open("rb") as f:
            for line in f:
                if not line.endswith(b"\n"):
                    break  # the cut-off tail; `end` stays at its start
                end += len(line)
                try:
                    record = json.loads(line)
                    asked = record["search_parameters"]
                    if params is not None and any(
                        asked.get(k) != params.get(k) for k in SAME_SEARCH
                    ):
                        continue
                    query = record["search_parameters"]["q"].strip()
                    page = record["search_parameters"].get("page", 1)
                    if type(page) is not int or page < 1:
                        continue  # not a page number this program writes
                    ok = record["search_metadata"]["status"] == "Success"
                    more = bool((record.get("pagination") or {}).get("next"))
                except (ValueError, KeyError, TypeError, AttributeError):
                    continue
                before = state.get(query, (False, 0, False))[0] if page != 1 else True
                state[query] = (before and ok, page, more)
            tail = f.seek(0, 2) != end
    except FileNotFoundError:
        return set()
    if tail:
        with path.open("r+b") as f:
            f.truncate(end)
    return {q for q, (ok, last, more) in state.items() if ok and (last >= pages or not more)}
