"""The old name of `gserp`.

`google-browser-scraper` was renamed to `gserp`. This package installs `gserp`
and keeps the old import, command and `python -m` working: every
`google_browser_scraper.<module>` is the same module object as `gserp.<module>`.
"""

import importlib
import sys
import warnings

import gserp
from gserp import *  # noqa: F403
from gserp import __all__, __version__  # noqa: F401

warnings.warn(
    "google_browser_scraper is now gserp: pip install gserp and import gserp",
    DeprecationWarning,
    stacklevel=2,
)

MODULES = (
    "browser",
    "classify",
    "cli",
    "doctor",
    "engines",
    "env",
    "hybrid",
    "links",
    "mcp",
    "output",
    "parse",
    "pool",
    "proxy",
    "quality",
    "relay",
    "scraper",
    "server",
    "warmup",
)

for _name in MODULES:
    _module = importlib.import_module(f"gserp.{_name}")
    sys.modules[f"{__name__}.{_name}"] = _module
    setattr(sys.modules[__name__], _name, _module)


def main() -> int:
    """The old command, `google-browser-scraper`: the `gserp` CLI."""
    from gserp.cli import main as gserp_main

    return gserp_main()


del _name, _module, gserp
