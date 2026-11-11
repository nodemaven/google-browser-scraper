"""Settings read from the environment: `SERPMAVEN_*`, or the older `GSERP_*`
and `GBS_*` names."""

from __future__ import annotations

import os

PREFIXES = ("SERPMAVEN_", "GSERP_", "GBS_")


def get(name: str, default: str | None = None) -> str | None:
    """`SERPMAVEN_<name>`, else `GSERP_<name>`, else `GBS_<name>`, else
    `default`; an empty value counts as unset."""
    for prefix in PREFIXES:
        value = os.environ.get(prefix + name)
        if value:
            return value
    return default
