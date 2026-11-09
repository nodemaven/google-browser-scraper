"""Settings read from the environment: `GSERP_*`, or the older `GBS_*` names."""

from __future__ import annotations

import os

PREFIXES = ("GSERP_", "GBS_")


def get(name: str, default: str | None = None) -> str | None:
    """`GSERP_<name>`, else `GBS_<name>`, else `default`; an empty value counts as unset."""
    for prefix in PREFIXES:
        value = os.environ.get(prefix + name)
        if value:
            return value
    return default
