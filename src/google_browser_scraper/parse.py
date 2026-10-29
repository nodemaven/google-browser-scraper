"""Read results out of a Google results page that `classify` called served.

Field names follow the usual SERP API JSON.

`ai_overview` is the AI Overview's text when the page carries one; its source
links are loaded by script after the page and are not part of it.

`local_results` are the places in the map pack of a local query: name, rating,
review count, and the lines Google prints under them (price, type, address,
hours, a quoted review).

Google wraps every result link in an encrypted `/goto?url=...` redirect. Some
pages still pair each token with its destination in their inline JSON, so the
parser tries that first; a link it cannot find keeps its `redirect_link`, and
`links.LinkResolver` asks the redirect for it.
"""

from __future__ import annotations

import copy
import json
import re
from typing import Any
from urllib.parse import parse_qs, urljoin, urlsplit

from lxml import html as lxml_html

GOOGLE = "https://www.google.com"

# In the inline JSON a result reads `..."/goto?url=<token>"],["<destination>",...`.
_AFTER_TOKEN = re.compile(r'"\],\["(https?:[^"\\]*(?:\\.[^"\\]*)*)"')


def parse_serp(page: str, *, base_url: str = GOOGLE) -> dict[str, Any]:
    """Parse one results page into blocks shaped like a SERP API's.

    Missing blocks come back as empty lists rather than absent keys, so a
    caller can tell "this page had no ads" from "this parser has no ads field".
    """
    root = lxml_html.fromstring(page)
    return {
        "search_information": _search_information(root),
        "ads": _ads(root, base_url),
        "organic_results": _organic(root, page, base_url),
        "related_questions": _related_questions(root),
        "related_searches": _related_searches(root, base_url),
        "pagination": _pagination(root, base_url),
        "ai_overview": _ai_overview(root),
        "local_results": _local_results(root),
    }


def resolve_goto(page: str, token: str) -> str | None:
    """Find the destination the page's inline JSON pairs with a /goto token.

    Two shapes, tried in order. A web result puts the destination right after
    the token, `...<token>"],["https://...`. A video result does not; its entry
    carries the token several times and ends with `["VIDEO_RESULT", ...,
    "https://www.youtube.com/watch?v=..."]`, so the fallback takes the first
    non-Google URL after that marker, without crossing into the next result's
    entry.

    The fallback reads nothing before the marker and nothing in an entry that
    has none. Those stretches hold other URLs - an SVG namespace, a thumbnail -
    and returning one of them gives a confident wrong link. With no marker the
    answer is None, and `links.LinkResolver` asks the redirect instead.
    """
    occurrences = []
    start = 0
    while (i := page.find(token, start)) >= 0:
        match = _AFTER_TOKEN.match(page, i + len(token))
        if match:
            return _unescape(match.group(1))
        if page.startswith('"', i + len(token)):  # inside JSON, not an href or ping
            occurrences.append(i + len(token))
        start = i + 1
    for end in occurrences:
        window = page[end : end + _WINDOW]
        cut = window.find(_NEXT_ENTRY)
        if cut >= 0:
            window = window[:cut]
        marker = window.find(_VIDEO_MARKER)
        if marker < 0:
            continue
        for match in _JSON_URL.finditer(window, marker):
            url = _unescape(match.group(1))
            if not _is_google(url):
                return url
    return None


# The fallback reads at most this far past a token, and never past the start of
# the next result's entry.
_WINDOW = 1500
_NEXT_ENTRY = "[null,1,[null,null,5,"
_VIDEO_MARKER = '"VIDEO_RESULT"'
_JSON_URL = re.compile(r'"(https?:[^"\\]*(?:\\.[^"\\]*)*)"')


def _unescape(json_string: str) -> str:
    return json.loads(f'"{json_string}"')


def _is_google(url: str) -> bool:
    host = urlsplit(url).netloc.lower()
    return (
        host.endswith(("google.com", "gstatic.com", "googleusercontent.com")) or ".google." in host
    )


# -- blocks ------------------------------------------------------------------


def _organic(root, page: str, base_url: str) -> list[dict[str, Any]]:
    container = _by_id(root, "rso")
    if container is None:
        return []
    results, seen = [], set()
    for h3 in container.iter("h3"):
        anchor = _ancestor(h3, lambda el: el.tag == "a" and el.get("href"))
        if anchor is None or _inside_related_question(h3):
            continue
        link, redirect = _destination(anchor.get("href"), page, base_url)
        key = link or redirect
        if not key or key in seen:
            continue
        seen.add(key)
        # lxml elements are falsy when they have no children, so `or` would
        # silently skip a valid block. Compare with None, always.
        block = _ancestor(h3, lambda el: _has_class(el, "MjjYud"))
        if block is None:
            block = anchor.getparent()
        result: dict[str, Any] = {
            "position": len(results) + 1,
            "title": _text(h3),
            "link": link,
        }
        if redirect:
            result["redirect_link"] = redirect
        cite = _first(block, ".//cite")
        if cite is not None:
            result["displayed_link"] = _text(cite)
        source = _first(block, ".//span[contains(concat(' ', @class, ' '), ' VuuXrf ')]")
        if source is not None:
            result["source"] = _text(source)
        snippet = _first(block, ".//div[contains(concat(' ', @class, ' '), ' VwiC3b ')]")
        if snippet is not None:
            date = _first(snippet, ".//span[contains(concat(' ', @class, ' '), ' YrbPuc ')]/span")
            text = _text(snippet)
            if date is not None:
                result["date"] = _text(date)
                text = text.removeprefix(result["date"]).lstrip(" —-·")
            result["snippet"] = text
        results.append(result)
    return results


def _ads(root, base_url: str) -> list[dict[str, Any]]:
    ads = []
    for block in root.xpath("//div[@data-text-ad]"):
        anchor = _first(block, ".//a[@data-pcu]")
        if anchor is None:
            anchor = _first(block, ".//a[@href]")
        if anchor is None:
            continue
        heading = _first(block, ".//div[@role='heading']")
        landing = (anchor.get("data-pcu") or "").split(",")[0].strip()
        ad: dict[str, Any] = {
            "position": len(ads) + 1,
            "block_position": _ad_block(block),
            "title": _text(heading) if heading is not None else _text(anchor),
            "link": landing or urljoin(base_url, anchor.get("href", "")),
        }
        displayed = _first(block, ".//span[@data-dtld]")
        if displayed is not None:
            ad["displayed_link"] = _text(displayed)
            ad["source"] = displayed.get("data-dtld")
        ads.append(ad)
    return ads


# Interface text inside the AI Overview block, not part of the answer.
_AI_CHROME = (
    "AI responses may include mistakes.",
    "Learn more",
    "Show more",
    "Show all",
    "Dive deeper in AI Mode",
    "Thank you",
    "Your feedback helps Google improve.",
)
_AI_MIN_TEXT = 200


def _ai_overview(root) -> dict[str, Any]:
    """The AI Overview's text, or {} when the page has none.

    Found by its visible "AI Overview" heading rather than by class names, which
    Google renames: the block is the nearest ancestor of the heading that holds
    real text. Source links load later by script and are not in the page.
    """
    heading = next(
        (
            el
            for el in root.iter()
            if isinstance(el.tag, str)
            and el.tag not in ("script", "style")
            and (el.text or "").strip() == "AI Overview"
        ),
        None,
    )
    if heading is None:
        return {}
    block = heading
    for _ in range(10):
        block = block.getparent()
        # An ancestor holding a result link (an anchor around a heading) is past
        # the block: stopping there keeps an empty overview from borrowing the
        # results' text.
        if block is None or block.xpath(".//a[.//h3]"):
            return {}
        if len(_visible_text(block)) >= _AI_MIN_TEXT:
            break
    else:
        return {}
    text = _visible_text(block, skip_label=True)
    text = text.removeprefix("AI Overview").strip()
    return {"text": text} if len(text) >= _AI_MIN_TEXT else {}


_RATING = re.compile(r"(\d+(?:[.,]\d+)?)")
_COUNT = re.compile(r"\(?\s*([\d.,]+)\s*([KkMm]?)\s*\)?")


def _count(text: str) -> int | None:
    """ "(1.7K)" -> 1700, "(44)" -> 44."""
    found = _COUNT.fullmatch(text.strip())
    if not found:
        return None
    number, unit = found.groups()
    scale = {"k": 1_000, "m": 1_000_000}.get(unit.lower(), 1)
    try:
        if scale > 1:
            return round(float(number.replace(",", ".")) * scale)
        return int(number.replace(",", "").replace(".", ""))
    except ValueError:
        return None


_PHONE = re.compile(r"\+?[\d\s().-]{7,}")
_HOURS = re.compile(
    r"^(open|closed|closes|opens|temporarily closed)\b"
    r"|^(monday|tuesday|wednesday|thursday|friday|saturday|sunday|mon|tues?|wed|thu(rs?)?|fri|sat|sun)\b"
    r"|\b\d{1,2}(:\d{2})?\s?(am|pm)\b",
    re.IGNORECASE,
)


def _place_facts(lines: list[str]) -> dict[str, str]:
    """Address, phone and hours out of the lines under a place's name.

    A line holds one or more facts joined by middle dots ("Brooklyn, NY",
    "Open · Closes 11 PM · (646) 688-0366"). The address is the first fact that
    is neither a phone number nor opening hours; anything else stays in
    `extensions` only.
    """
    facts: dict[str, str] = {}
    hours: list[str] = []
    for line in lines:
        for part in (p.strip() for p in re.split("[·⋅]", line)):
            if not part:
                continue
            digits = sum(c.isdigit() for c in part)
            if "phone" not in facts and _PHONE.fullmatch(part) and digits >= 7:
                facts["phone"] = part
            elif _HOURS.search(part):
                hours.append(part)
            elif "address" not in facts and line is lines[0]:
                facts["address"] = part
    if hours:
        facts["hours"] = " · ".join(hours)
    return facts


def _local_results(root) -> list[dict[str, Any]]:
    """The map pack's places, in the order shown.

    Each place's details sit in one `rllt__details` block: a heading with the
    name, then a line per fact. The rating carries a `role=img` label; the line
    it sits on also holds the price and the type, joined by middle dots.
    """
    places: list[dict[str, Any]] = []
    for details in root.xpath("//*[contains(concat(' ', @class, ' '), ' rllt__details ')]"):
        heading = _first(details, ".//*[@role='heading']")
        if heading is None:
            continue
        place: dict[str, Any] = {"position": len(places) + 1, "title": _text(heading)}
        rating = _first(details, ".//*[@role='img'][@aria-label]")
        lines = [line for line in details.xpath("./div") if line is not heading]
        for line in lines:
            if rating is not None and rating in line.iterdescendants():
                stars = _first(rating, "./span[1]")
                found = _RATING.search(_text(stars) if stars is not None else "")
                if found:
                    place["rating"] = float(found.group(1).replace(",", "."))
                count = _first(rating, "./span[last()]")
                reviews = _count(_text(count)) if count is not None else None
                if reviews is not None:
                    place["reviews"] = reviews
                parts = [p.strip() for p in _text(line).split("·")][1:]
                parts = [p for p in parts if p]
                if len(parts) >= 2:
                    place["price"] = parts[0]
                if parts:
                    place["type"] = parts[-1]
            elif "pJ3Ci" in (line.get("class") or "").split():
                quote = _text(line).strip(' "“”')
                if quote:
                    place["description"] = quote
            else:
                text = _text(line)
                if text:
                    place.setdefault("extensions", []).append(text)
        place.update(_place_facts(place.get("extensions", [])))
        places.append(place)
    return places


def _only_chrome(el) -> bool:
    """Whether an element's text is nothing but the AI Overview's own labels."""
    rest = " ".join(el.text_content().split())
    if not rest:
        return False
    for chrome in _AI_CHROME:
        rest = rest.replace(chrome, "")
    return not rest.strip(" .")


def _visible_text(el, *, skip_label: bool = False) -> str:
    """Text of an element without scripts, styles and (optionally) the hidden
    "AI Mode reply for ..." label Google puts before the answer."""
    el = copy.deepcopy(el)
    for node in el.xpath(".//script|.//style"):
        node.drop_tree()
    if skip_label:
        for node in el.xpath(".//*[starts-with(normalize-space(text()), 'AI Mode reply for')]"):
            node.drop_tree()
        # The block's own buttons and notices, dropped only where an element
        # holds nothing else: the same words inside the answer stay.
        labels = [node for node in el.iter() if isinstance(node.tag, str) and _only_chrome(node)]
        for node in labels:
            if node.getparent() is not None:
                node.drop_tree()
    # Text nodes joined with a space: block elements carry no separator of their own.
    return " ".join(" ".join(el.itertext()).split())


def _related_questions(root) -> list[dict[str, str]]:
    questions, seen = [], set()
    for el in root.xpath("//div[contains(concat(' ', @class, ' '), ' related-question-pair ')]"):
        question = (el.get("data-q") or "").strip()
        if question and question not in seen:
            seen.add(question)
            questions.append({"question": question})
    return questions


def _related_searches(root, base_url: str) -> list[dict[str, str]]:
    container = _by_id(root, "bres")
    if container is None:
        return []
    searches, seen = [], set()
    for anchor in container.xpath(".//a[@href]"):
        href = anchor.get("href", "")
        query = _query_param(href, "q")
        if not href.startswith("/search") or not query or query in seen:
            continue
        seen.add(query)
        searches.append({"query": query, "link": urljoin(base_url, href)})
    return searches


def _pagination(root, base_url: str) -> dict[str, Any]:
    pagination: dict[str, Any] = {}
    nxt = _by_id(root, "pnnext")
    if nxt is not None and nxt.get("href"):
        pagination["next"] = urljoin(base_url, nxt.get("href"))
        start = _query_param(nxt.get("href"), "start")
        if start and start.isdigit():
            pagination["current"] = int(start) // 10
    return pagination


def _search_information(root) -> dict[str, Any]:
    info: dict[str, Any] = {}
    stats = _by_id(root, "result-stats")
    if stats is not None:
        text = _text(stats)
        info["total_results_text"] = text
        # "Page 2 of about 1,230,000 results (0.42 seconds)": drop the timing,
        # then the largest number is the count, in any digit grouping.
        counts = re.findall(r"\d[\d,.   ]*\d|\d", re.sub(r"\(.*?\)", "", text))
        numbers = [int(re.sub(r"\D", "", c)) for c in counts]
        if numbers:
            info["total_results"] = max(numbers)
    return info


# -- helpers -----------------------------------------------------------------


def _destination(href: str, page: str, base_url: str) -> tuple[str | None, str | None]:
    """Return (link, redirect_link) for a result anchor's href."""
    if href.startswith("/goto"):
        token = _query_param(href, "url")
        return (resolve_goto(page, token) if token else None), urljoin(base_url, href)
    if href.startswith("/url"):
        return _query_param(href, "q") or _query_param(href, "url"), urljoin(base_url, href)
    if href.startswith(("http://", "https://")):
        return href, None
    return None, None


def _ad_block(el) -> str:
    for ancestor in el.iterancestors():
        ident = ancestor.get("id")
        if ident == "tads":
            return "top"
        if ident in ("tadsb", "bottomads"):
            return "bottom"
    return "unknown"


def _inside_related_question(el) -> bool:
    return _ancestor(el, lambda a: _has_class(a, "related-question-pair")) is not None


def _ancestor(el, predicate):
    for candidate in el.iterancestors():
        if predicate(candidate):
            return candidate
    return None


def _has_class(el, name: str) -> bool:
    return name in (el.get("class") or "").split()


def _by_id(root, ident: str):
    found = root.xpath(f"//*[@id='{ident}']")
    return found[0] if found else None


def _first(el, xpath: str):
    found = el.xpath(xpath)
    return found[0] if found else None


def _text(el) -> str:
    return " ".join(el.text_content().split())


def _query_param(href: str, name: str) -> str | None:
    values = parse_qs(urlsplit(href).query).get(name)
    return values[0] if values else None
