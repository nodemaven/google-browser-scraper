import pages
import pytest

from google_browser_scraper.classify import (
    BLOCK,
    CONSENT,
    EMPTY,
    NO_JS,
    NO_RESULTS,
    OK,
    SORRY,
    WALL,
    classify,
)

SEARCH = "https://www.google.com/search?q=x"


@pytest.mark.parametrize(
    ("url", "html", "status", "kind"),
    [
        (SEARCH, pages.results_page(), 200, OK),
        (SEARCH, "", 200, EMPTY),
        (pages.SORRY_URL, pages.SORRY_PAGE, 429, SORRY),
        # The same refusal has been served as 200; the URL decides, not the status.
        (pages.SORRY_URL, pages.SORRY_PAGE, 200, SORRY),
        (SEARCH, '<a href="/sorry/index?continue=x">here</a>', 302, SORRY),
        (SEARCH, pages.SORRY_PAGE, 200, SORRY),
        ("https://consent.google.com/m?continue=x", "<html>consent</html>", 200, CONSENT),
        (SEARCH, pages.CONSENT_PAGE, 200, CONSENT),
        (SEARCH, pages.NO_RESULTS_PAGE, 200, NO_RESULTS),
        (SEARCH, pages.WALL_PAGE, 200, WALL),
        (SEARCH, pages.NO_JS_PAGE, 200, NO_JS),
        (SEARCH, "<html><title>Error 403 (Forbidden)!!1</title></html>", 403, BLOCK),
        (SEARCH, "<html><body>something else</body></html>", 200, BLOCK),
    ],
)
def test_classify(url, html, status, kind):
    assert classify(url, html, status).kind == kind


def test_no_js_scaffold_is_ours_not_a_refusal():
    # The scaffold carries `emsg=SG_REL` too; `enablejs` is what tells it apart
    # from the wall, so it is tested first.
    verdict = classify(SEARCH, "<html><body><noscript>enable</noscript>enablejs</body></html>")
    assert verdict.kind == NO_JS
    assert not verdict.refused and not verdict.served


def test_served_page_with_dormant_consent_panel_is_still_served():
    html = pages.results_page() + '<form action="https://consent.google.com/save"></form>'
    assert classify(SEARCH, html).kind == OK


def test_refused_and_served_partition():
    assert classify(pages.SORRY_URL, pages.SORRY_PAGE).refused
    assert classify(SEARCH, pages.NO_RESULTS_PAGE).served
    assert not classify(SEARCH, pages.NO_RESULTS_PAGE).refused
