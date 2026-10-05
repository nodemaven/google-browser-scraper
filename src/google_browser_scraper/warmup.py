"""Which pages a fresh exit opens before it is asked anything.

The browser opens each page, waits, and moves on: no clicking, no scrolling, no
typing. It ends on Google's front page, which is where the query is typed. A
single page of warm-up does little; the default six-page ladder is the one that
measured best in proxy-benchmark.
"""

from __future__ import annotations

LADDERS: dict[str, tuple[str, ...]] = {
    "off": (),
    "L1": ("https://www.google.com/imghp?hl=en",),
    "L2": (
        "https://translate.google.com/?hl=en",
        "https://scholar.google.com/?hl=en",
        "https://www.google.com/imghp?hl=en",
    ),
    "L3": (
        "https://www.theverge.com/",
        "https://www.wikihow.com/Main-Page",
        "https://translate.google.com/?hl=en",
        "https://scholar.google.com/?hl=en",
        "https://www.google.com/imghp?hl=en",
        "https://trends.google.com/trends/?hl=en",
    ),
}
DEFAULT = "L3"

#: Seconds on each warm-up page, drawn uniformly.
DWELL_SECONDS = (3.0, 8.0)


def pages(level: str) -> tuple[str, ...]:
    try:
        return LADDERS[level]
    except KeyError:
        raise ValueError(
            f"warm-up level must be one of {', '.join(LADDERS)}, got {level!r}"
        ) from None
