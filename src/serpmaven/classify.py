"""Decide what Google served before anything tries to read results out of it.

A refusal must never reach the parser, because a parser handed a refusal returns
zero results, and "Google found nothing" and "Google refused this exit" need
opposite responses: the first is an answer, the second means drop the exit and
ask again from a fresh one.

Only structural markers are used - ids, paths and endpoints - so a translated
page classifies the same as an English one.
"""

from __future__ import annotations

from dataclasses import dataclass
from urllib.parse import urlsplit

OK = "ok"
NO_RESULTS = "no_results"
SORRY = "sorry"
WALL = "wall"
CONSENT = "consent"
NO_JS = "no_js"
BLOCK = "block"
EMPTY = "empty"

#: Google looked at this exit and refused it. Drop the exit.
REFUSED = frozenset({SORRY, WALL, BLOCK})
#: Google answered the query. Keep the exit.
SERVED = frozenset({OK, NO_RESULTS})


@dataclass(frozen=True)
class Verdict:
    kind: str
    reason: str

    @property
    def served(self) -> bool:
        return self.kind in SERVED

    @property
    def refused(self) -> bool:
        return self.kind in REFUSED


def classify(url: str | None, html: str | None, status: int | None = None) -> Verdict:
    """Classify one Google response from its final URL, body and status.

    Order matters. A served page can carry a dormant consent panel, so the
    results test runs before the inline consent test; reversing them scores
    working pages as interstitials.
    """
    if not html:
        return Verdict(EMPTY, "no body")

    parts = urlsplit(url or "")
    host = parts.netloc.lower()
    path = parts.path.lower()
    low = html.lower()

    # The refusal redirects to /sorry/ with a reCAPTCHA. It has arrived as 429
    # and as 200 with the same body, so the status is a hint and not the rule.
    if host.startswith("sorry.") or path.startswith("/sorry"):
        return Verdict(SORRY, "redirected to /sorry/ with a captcha")
    # A redirect stub that was not followed: a tiny body pointing at /sorry/
    # is the refusal, not a page.
    if len(html) < 2000 and "/sorry/" in low:
        return Verdict(SORRY, "short body pointing at /sorry/")
    if status == 429:
        return Verdict(SORRY, "HTTP 429")
    if "unusual traffic from your computer" in low:
        return Verdict(SORRY, "unusual-traffic page")
    if host.startswith("consent."):
        return Verdict(CONSENT, "consent interstitial")

    has_rso = 'id="rso"' in low
    has_search = 'id="search"' in low
    if has_rso:
        return Verdict(OK, "results container present")

    # A real empty SERP keeps `#search`. Retiring an exit on it would throw away
    # a working identity for a query that simply has no results.
    if has_search:
        return Verdict(NO_RESULTS, "search container present, no results in it")

    if "consent.google.com" in low:
        return Verdict(CONSENT, "consent wall served inline, no results behind it")

    # The "enable JavaScript" scaffold. It also carries `emsg=SG_REL`, so it is
    # tested before the wall below. From a browser that ran scripts it means
    # Google declined to render, so the scraper drops the exit.
    if "enablejs" in low:
        return Verdict(NO_JS, "served the no-JavaScript scaffold")

    # A SERP-shaped page with an error marker and no results container: a
    # refusal that does not redirect.
    if "emsg=sg_rel" in low:
        return Verdict(WALL, "SERP-shaped wall without results (emsg=SG_REL)")

    if "<noscript" in low and "<h3" not in low:
        return Verdict(NO_JS, "a scaffold with no results and a noscript block")

    if "error 403" in low or status == 403:
        return Verdict(BLOCK, "HTTP 403 page")
    return Verdict(BLOCK, "no results and no known interstitial")
