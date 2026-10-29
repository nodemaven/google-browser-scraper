"""Synthetic Google pages for the offline tests.

Built by hand rather than saved from Google: a saved results page carries the
location of the exit it was fetched from, which is not something to commit.
"""

GOTO_A = "CAESdQHrOzAVat9YfzzCS78CJFjg4kgZKige7dVy07UiYTbvGT"
GOTO_B = "CAESiwEB6zswFVamDUs51KMKO7m25UC"


def _organic(href: str, title: str, cite: str, source: str, snippet: str, date: str = "") -> str:
    date_html = f'<span class="YrbPuc"><span>{date}</span> — </span>' if date else ""
    return (
        '<div class="MjjYud"><div class="A6K0A"><div class="tF2Cxc">'
        f'<div class="yuRUbf"><a href="{href}"><h3 class="LC20lb">{title}</h3>'
        f'<div><span class="VuuXrf">{source}</span><cite>{cite}</cite></div></a></div>'
        f'<div class="VwiC3b">{date_html}<span>{snippet}</span></div>'
        "</div></div></div>"
    )


AI_ANSWER = (
    "To start swimming, get comfortable with putting your face in the water and "
    "blowing bubbles, then practise floating on your front and back. "
    "Freestyle is the usual first stroke: keep your body horizontal, kick from the hips "
    "and breathe to the side every two or three strokes."
)


def ai_overview_block(answer: str = AI_ANSWER) -> str:
    """The shape of Google's AI Overview: a heading, a hidden label, the answer,
    interface text, and a script that is not part of it."""
    return (
        '<div class="CKgc1d"><div class="SGF5Lb"><div><div class="ux3nGb">'
        '<div role="heading"><strong>AI Overview</strong></div></div></div></div>'
        '<div><span class="hidden">AI Mode reply for swimming</span>'
        f"<div><span>{answer}</span></div>"
        "<script>var notText = 1;</script>"
        "<div>AI responses may include mistakes. <a>Learn more</a></div>"
        "<div>Show more</div></div></div>"
    )


def _place(name: str, stars: str, count: str, facts: str, address: str, quote: str = "") -> str:
    rating = (
        f'<span class="Y0A0hc" aria-label="Rated {stars} out of 5, {count} user reviews" '
        f'role="img"><span class="yi40Hd" aria-hidden="true">{stars}</span>'
        '<span aria-hidden="true" role="img" aria-label="Rated"><span></span></span>'
        f'<span class="RDApEe" aria-hidden="true">({count})</span></span>'
    )
    said = f'<div class="pJ3Ci"><span>"{quote}"</span></div>' if quote else ""
    return (
        '<div class="VkpGBb"><div class="rllt__details">'
        f'<div class="dbg0pd" aria-level="3" role="heading"><span>{name}</span></div>'
        f"<div><span>{rating}</span>{facts}</div><div>{address}</div>{said}</div></div>"
    )


def local_pack_block() -> str:
    return (
        '<div class="Qq3Lb"><div role="heading">Places</div>'
        + _place("Lane Swim Center", "4.6", "1.7K", " · $$ · Swimming pool", "12 Main St",
                 "Clean lanes and friendly staff.")
        + _place("City Aquatics", "4.1", "44", " · Gym", "Open ⋅ Closes 9 PM")
        + "</div>"
    )


def results_page(
    *, goto: bool = True, with_json: bool = True, ai_overview: str = "", local: str = ""
) -> str:
    first = f"/goto?url={GOTO_A}" if goto else "https://www.usms.org/guide"
    organic = [
        _organic(
            first,
            "Swimming 101: The Beginner&#39;s Guide",
            "https://www.usms.org › guides",
            "U.S. Masters Swimming",
            "In this guide we cover swimming lingo.",
            date="7 Aug 2025",
        ),
        _organic(
            "/url?q=https://www.swimnow.co.uk/guide&amp;sa=U",
            "A Beginner&#39;s Guide to Techniques",
            "https://www.swimnow.co.uk › guide",
            "SwimNow",
            "Different swimming techniques explained.",
        ),
        _organic(
            f"/goto?url={GOTO_B}",
            "Unresolvable result",
            "https://example.org",
            "Example",
            "This token has no JSON pair.",
        ),
    ]
    paa = (
        '<div class="related-question-pair" data-q="Is swimming good exercise?">'
        '<a href="https://answers.example/q"><h3>Not an organic result</h3></a></div>'
        '<div class="related-question-pair" data-q="How do I start swimming?"></div>'
    )
    ad = (
        '<div id="tads"><div data-text-ad="1"><a data-pcu="https://eolab.com/,https://www.eolab.com/"'
        ' href="/goto?url=CAESadtoken"><div role="heading"><span>Swim better</span></div>'
        '<span data-dtld="eolab.com">https://www.eolab.com › swim</span></a></div></div>'
    )
    href = "/search?hl=en&amp;q=swimming+for+beginners&amp;sa=X"
    related = (
        f'<div id="bres"><a class="ngTNl" href="{href}">swimming for beginners</a>'
        f'<a class="ngTNl" href="{href}">dup</a></div>'
    )
    nav = (
        '<table><tr><td><a id="pnnext" href="/search?q=swimming&amp;start=10&amp;sa=N">'
        "Next</a></td></tr></table>"
    )
    script = ""
    if with_json:
        script = (
            '<script>var data=[null,1,[null,null,5,null,"Swimming 101",null,'
            f'"/goto?url\\u003d{GOTO_A}"],["https://www.usms.org/fitness-and-training/'
            'guides/swimming-101?a\\u003d1","Swimming 101"]];</script>'
        )
    return (
        "<html><head><title>swimming - Google Search</title></head><body>"
        '<div id="result-stats">About 1,230,000 results (0.42 seconds)</div>'
        f'<div id="search">{ai_overview}{local}{ad}'
        f'<div id="rso">{"".join(organic[:1])}{paa}{"".join(organic[1:])}'
        f"</div></div>{related}{nav}{script}</body></html>"
    )


SORRY_URL = "https://www.google.com/sorry/index?continue=https://www.google.com/search"
SORRY_PAGE = "<html><body>Our systems have detected unusual traffic from your computer network."
NO_RESULTS_PAGE = '<html><body><div id="search"><p>did not match any documents</p></div></body>'
WALL_PAGE = '<html><head><title>Google Search</title></head><body><a href="/x?emsg=SG_REL">'
NO_JS_PAGE = (
    '<html><body><noscript><meta content="0;url=/search?q=x&amp;emsg=SG_REL&amp;'
    'enablejs=1"></noscript><div id="main"></div></body></html>'
)
CONSENT_PAGE = (
    '<html><body><form action="https://consent.google.com/save">'
    "<button>Reject all</button></form></body></html>"
)
