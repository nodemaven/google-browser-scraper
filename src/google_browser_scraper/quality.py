"""Whether a served results page answers the query it was asked for.

Google is reported to answer some automated traffic not with a block but with a
normal-looking page of results for other queries. On such a page almost no
title carries the query's words. A single page like that happens on an honest
exit too - a brand query whose titles name only the brand, a local query
answered with business names - so an exit counts as poisoned only after
`POISON_STREAK` such pages in a row. A query with fewer than `MIN_WORDS`
content words says too little to judge by, and neither adds to a streak nor
ends one.
"""

from __future__ import annotations

import re

#: Below this share of titles holding half of the query's words, a page is
#: unrelated to its query.
POISON_RELEVANCE = 0.2
POISON_STREAK = 3
MIN_WORDS = 2
STOP_WORDS = frozenset(
    "how to the a an of for and in on is what are best with do does you your my can i vs".split()
)


def _stem(word: str) -> str:
    """Plural and verb endings off, so "puppies" finds "puppy" in a title."""
    for suffix in ("ies", "es", "s", "ing", "ed"):
        if word.endswith(suffix) and len(word) - len(suffix) >= 3:
            return word[: -len(suffix)]
    return word


def _words(query: str) -> list[str]:
    # Letters of any script, so a query in Cyrillic or with accents is judged too.
    found = re.findall(r"[^\W_]+", query.lower())
    return [_stem(w) for w in found if w not in STOP_WORDS and len(w) > 2]


def relevance(query: str, titles: list[str]) -> float:
    """Share of titles holding at least half of the query's content words; 1.0
    when there is nothing to compare."""
    words = _words(query)
    if not words or not titles:
        return 1.0
    need = max(1, (len(words) + 1) // 2)
    return sum(1 for t in titles if sum(w in t.lower() for w in words) >= need) / len(titles)


def unrelated(query: str, titles: list[str]) -> bool | None:
    """Whether the titles are unrelated to the query; None when the query or
    the page gives too little to tell."""
    if len(_words(query)) < MIN_WORDS or not titles:
        return None
    return relevance(query, titles) < POISON_RELEVANCE
