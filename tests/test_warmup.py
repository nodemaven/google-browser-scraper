from google_browser_scraper import warmup
from google_browser_scraper.cli import _parser
from google_browser_scraper.scraper import Settings


def test_light_is_the_default_ladder_without_trends():
    light = warmup.pages("light")
    assert light == tuple(u for u in warmup.pages("L3") if "trends.google.com" not in u)
    assert len(light) == len(warmup.pages("L3")) - 1


def test_light_is_accepted_by_settings_and_the_command_line():
    assert Settings(warmup="light").warmup == "light"
    args = _parser().parse_args(["search", "q", "--no-proxy", "--warmup", "light"])
    assert args.warmup == "light"
