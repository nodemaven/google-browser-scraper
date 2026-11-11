"""The old name of `serpmaven`.

`gserp` was renamed to `serpmaven` (SERP Maven). This package
installs `serpmaven` and keeps the old import, command and `python -m` working:
every `gserp.<module>` is the same module object as
`serpmaven.<module>`.
"""

import importlib
import pkgutil
import sys
import warnings

import serpmaven
from serpmaven import *  # noqa: F403
from serpmaven import __all__, __version__  # noqa: F401

# FutureWarning, not DeprecationWarning: Python shows it by default wherever the
# import happens, and this one is meant for the people running the code.
warnings.warn(
    "gserp is now serpmaven: pip install serpmaven and import serpmaven",
    FutureWarning,
    stacklevel=2,
)

# Every public serpmaven module, read from the installed package so a module
# added later is aliased too. `__main__` and private modules are left out:
# importing `serpmaven.__main__` would run the CLI.
MODULES = tuple(
    info.name for info in pkgutil.iter_modules(serpmaven.__path__) if not info.name.startswith("_")
)

for _name in MODULES:
    _module = importlib.import_module(f"serpmaven.{_name}")
    sys.modules[f"{__name__}.{_name}"] = _module
    # The attribute is whatever serpmaven has under that name: the module, or a
    # function exported under the same name (`classify` is both).
    setattr(sys.modules[__name__], _name, getattr(serpmaven, _name, _module))


def main() -> int:
    """The old command, `gserp`: the `serpmaven` CLI."""
    from serpmaven.cli import main as serpmaven_main

    return serpmaven_main()


del _name, _module, serpmaven
