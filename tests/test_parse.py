import pages

from google_browser_scraper.parse import parse_serp, resolve_goto


def test_organic_results_in_page_order_without_people_also_ask():
    organic = parse_serp(pages.results_page())["organic_results"]
    assert [r["position"] for r in organic] == [1, 2, 3]
    assert [r["title"] for r in organic] == [
        "Swimming 101: The Beginner's Guide",
        "A Beginner's Guide to Techniques",
        "Unresolvable result",
    ]


def test_goto_links_resolve_through_the_inline_json():
    first = parse_serp(pages.results_page())["organic_results"][0]
    # JSON escapes are decoded, not copied.
    assert first["link"] == "https://www.usms.org/fitness-and-training/guides/swimming-101?a=1"
    assert first["redirect_link"].startswith("https://www.google.com/goto?url=")


def test_unresolvable_goto_keeps_the_redirect_and_says_so():
    third = parse_serp(pages.results_page())["organic_results"][2]
    assert third["link"] is None
    assert third["redirect_link"] == f"https://www.google.com/goto?url={pages.GOTO_B}"


def test_url_q_and_direct_hrefs():
    organic = parse_serp(pages.results_page(goto=False))["organic_results"]
    assert organic[0]["link"] == "https://www.usms.org/guide"
    assert "redirect_link" not in organic[0]
    assert organic[1]["link"] == "https://www.swimnow.co.uk/guide"


def test_result_fields():
    first = parse_serp(pages.results_page())["organic_results"][0]
    assert first["source"] == "U.S. Masters Swimming"
    assert first["displayed_link"] == "https://www.usms.org › guides"
    assert first["date"] == "7 Aug 2025"
    assert first["snippet"] == "In this guide we cover swimming lingo."


def test_ads():
    ads = parse_serp(pages.results_page())["ads"]
    assert ads == [
        {
            "position": 1,
            "block_position": "top",
            "title": "Swim better",
            "link": "https://eolab.com/",
            "displayed_link": "https://www.eolab.com › swim",
            "source": "eolab.com",
        }
    ]


def test_related_questions_and_searches_are_deduplicated():
    parsed = parse_serp(pages.results_page())
    assert parsed["related_questions"] == [
        {"question": "Is swimming good exercise?"},
        {"question": "How do I start swimming?"},
    ]
    assert [s["query"] for s in parsed["related_searches"]] == ["swimming for beginners"]


def test_pagination_and_totals():
    parsed = parse_serp(pages.results_page())
    assert parsed["pagination"]["next"].startswith("https://www.google.com/search?q=swimming")
    assert parsed["pagination"]["current"] == 1
    assert parsed["search_information"]["total_results"] == 1230000


def test_blocks_are_empty_lists_when_absent():
    parsed = parse_serp("<html><body><div id='search'></div></body></html>")
    assert parsed["organic_results"] == [] and parsed["ads"] == []
    assert parsed["related_questions"] == [] and parsed["related_searches"] == []
    assert parsed["pagination"] == {}


def test_resolve_goto_skips_occurrences_not_followed_by_a_destination():
    page = (
        '"/goto?url\\u003dTOK",null,null'
        ' ... "/goto?url\\u003dTOK"],["https://dest.example/a\\u0026b","t"]'
    )
    assert resolve_goto(page, "TOK") == "https://dest.example/a&b"
    assert resolve_goto(page, "MISSING") is None


def test_video_results_resolve_through_their_video_entry():
    token = "CAESvideo"
    page = (
        f'[null,1,[null,null,5,null,"A video",null,"/goto?url\u003d{token}"],null,'
        f'["/goto?url\u003d{token}",null,35,"Source: YouTube"],2,null,'
        '["VIDEO_RESULT","CKoG",null,"https://www.youtube.com/watch?v\u003dabc"]]'
        ',[null,1,[null,null,5,null,"next",null,"/goto?url\u003dOTHER"],'
        '["https://wrong.example/"]]'
    )
    assert resolve_goto(page, token) == "https://www.youtube.com/watch?v=abc"


def test_fallback_ignores_urls_before_the_video_marker():
    token = "CAESvideo"
    page = (
        f'[null,1,[null,null,5,null,"A video",null,"/goto?url={token}"],null,'
        '["http://www.w3.org/2000/svg"],'
        '["VIDEO_RESULT","CKoG",null,"https://www.youtube.com/watch?v=abc"]]'
    )
    assert resolve_goto(page, token) == "https://www.youtube.com/watch?v=abc"


def test_fallback_leaves_a_result_without_a_video_marker_to_the_resolver():
    """A thumbnail or an SVG namespace near the token is not the destination."""
    page = (
        '[null,1,[null,null,5,null,"t",null,"/goto?url=TOK"],null,'
        '["http://www.w3.org/2000/svg"],["https://cdn.example/thumb.jpg"]]'
    )
    assert resolve_goto(page, "TOK") is None


def test_fallback_does_not_cross_into_the_next_entry():
    page = (
        '[null,1,[null,null,5,null,"t",null,"/goto?url\u003dTOK"],null,'
        '["https://www.gstatic.com/icon.png"]],'
        '[null,1,[null,null,5,null,"n",null,"/goto?url\u003dNEXT"],["https://next.example/"]]'
    )
    assert resolve_goto(page, "TOK") is None


def test_ai_overview_text_without_heading_label_interface_or_script():
    page = pages.results_page(ai_overview=pages.ai_overview_block())
    parsed = parse_serp(page)
    assert parsed["ai_overview"] == {"text": pages.AI_ANSWER}
    assert len(parsed["organic_results"]) == 3


def test_no_ai_overview_is_an_empty_block():
    assert parse_serp(pages.results_page())["ai_overview"] == {}


def test_an_ai_overview_heading_with_no_answer_is_not_a_block():
    page = pages.results_page(ai_overview=pages.ai_overview_block("Loading"))
    assert parse_serp(page)["ai_overview"] == {}


def test_local_results_read_the_map_pack():
    parsed = parse_serp(pages.results_page(local=pages.local_pack_block()))
    first, second = parsed["local_results"]
    assert first == {
        "position": 1,
        "title": "Lane Swim Center",
        "rating": 4.6,
        "reviews": 1700,
        "price": "$$",
        "type": "Swimming pool",
        "extensions": ["12 Main St"],
        "address": "12 Main St",
        "description": "Clean lanes and friendly staff.",
    }
    assert second["reviews"] == 44 and second["type"] == "Gym" and "price" not in second
    assert second["hours"] == "Open · Closes 9 PM" and "address" not in second
    # The pack's places are not organic results.
    assert "Lane Swim Center" not in [r["title"] for r in parsed["organic_results"]]


def test_a_page_without_a_map_pack_has_no_local_results():
    assert parse_serp(pages.results_page())["local_results"] == []


def test_place_lines_split_into_address_phone_and_hours():
    from google_browser_scraper.parse import _place_facts

    lines = [
        "JFK International Airport, NY · (718) 656-1890",
        "Open · Closes 1 AM",
        "Rentals available from US$ 44/day",
    ]
    assert _place_facts(lines) == {
        "address": "JFK International Airport, NY",
        "phone": "(718) 656-1890",
        "hours": "Open · Closes 1 AM",
    }
    assert _place_facts(["Sunset Blvd 12"]) == {"address": "Sunset Blvd 12"}
    assert _place_facts(["Pasadena, MD", "Sat: 7 AM–12 PM"])["hours"] == "Sat: 7 AM–12 PM"


def test_ai_overview_keeps_label_words_that_are_part_of_the_answer():
    answer = pages.AI_ANSWER + " Learn more about pacing in the second week."
    page = pages.results_page(ai_overview=pages.ai_overview_block(answer))
    text = parse_serp(page)["ai_overview"]["text"]
    assert text.endswith("Learn more about pacing in the second week.")


def test_service_options_are_not_an_address_and_a_bare_rating_has_no_review_count():
    from google_browser_scraper.parse import _place_facts

    assert _place_facts(["Dine-in \u00b7 Takeout", "12 Main St"]) == {"address": "12 Main St"}
    lone = pages._place("Solo", "4.6", "1", "", "1 Elm St").replace(
        '<span class="RDApEe" aria-hidden="true">(1)</span>', ""
    )
    place = parse_serp(pages.results_page(local=lone))["local_results"][0]
    assert place["rating"] == 4.6 and "reviews" not in place
