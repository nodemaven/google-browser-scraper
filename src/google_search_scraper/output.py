"""Result records and the writers that put them on disk.

One record per results page, shaped like SerpApi's JSON plus an `exit` block of
our own that says which identity answered. A failed query is a record too, with
`search_metadata.status == "Error"` and an `error` string, so a run's output
accounts for every query it was given.
"""

from __future__ import annotations

import csv
import json
import uuid
from datetime import datetime, timezone
from typing import IO, Any

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
