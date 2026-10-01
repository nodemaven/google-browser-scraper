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


def results_page(*, goto: bool = True, with_json: bool = True) -> str:
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
        f'<div id="search">{ad}<div id="rso">{"".join(organic[:1])}{paa}{"".join(organic[1:])}'
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
