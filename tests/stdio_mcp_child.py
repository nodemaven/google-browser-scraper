"""Child process for the stdio encoding test: the real McpServer on the real
standard streams, over a scraper that echoes the query back with a non-ASCII
title. Run by tests/test_serve_and_mcp.py, never imported."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from google_search_scraper.mcp import McpServer  # noqa: E402
from google_search_scraper.scraper import Settings  # noqa: E402


class EchoScraper:
    settings = Settings()

    def search(self, query):
        return [
            {
                "search_metadata": {"status": "Success"},
                "organic_results": [
                    {"position": 1, "title": f"Café {query}", "link": "https://a.example"}
                ],
            }
        ]


class InlineWorker:
    def call(self, job, timeout=None):
        return job(EchoScraper())


McpServer(InlineWorker()).serve()
