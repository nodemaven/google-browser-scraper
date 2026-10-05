"""Google search results from a real browser through your own proxy.

from google_browser_scraper import Scraper, Settings, ProxyTemplate

proxy = ProxyTemplate("http://user-session-{session}:pass@gate.example.com:7000")
for record in Scraper(proxy, Settings(pages=2)).run(["best running shoes"]):
    print(record["organic_results"])
"""

__version__ = "0.0.1"

from .classify import Verdict, classify  # noqa: E402
from .parse import parse_serp  # noqa: E402
from .proxy import NodeMavenSource, ProxyTemplate  # noqa: E402
from .scraper import ExitsRefused, Scraper, Settings  # noqa: E402

__all__ = [
    "ExitsRefused",
    "NodeMavenSource",
    "ProxyTemplate",
    "Scraper",
    "Settings",
    "Verdict",
    "__version__",
    "classify",
    "parse_serp",
]
