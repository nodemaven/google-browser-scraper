"""The old name of `gserp`.

`google-browser-scraper` was renamed to `gserp`. This package installs `gserp`
and keeps the old import, command and `python -m` working: every
`google_browser_scraper.<module>` is the same module object as `gserp.<module>`.
"""

import importlib
import pkgutil
import sys
import warnings

import gserp
from gserp import *  # noqa: F403
from gserp import __all__, __version__  # noqa: F401

# FutureWarning, not DeprecationWarning: Python shows it by default wherever the
# import happens, and this one is meant for the people running the code.
warnings.warn(
    "google_browser_scraper is now gserp: pip install gserp and import gserp",
    FutureWarning,
    stacklevel=2,
)

# Every public gserp module, read from the installed package so a module added
# later is aliased too. `__main__` and private modules are left out: importing
# `gserp.__main__` would run the CLI.
MODULES = tuple(
    info.name for info in pkgutil.iter_modules(gserp.__path__) if not info.name.startswith("_")
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
