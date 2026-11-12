"""A proxy login saved once by `serpmaven login`, read by every later run.

The file holds either NodeMaven proxy credentials or a proxy URL for any other
provider. It is plain JSON in the user's config folder, readable by its owner
only, so the password is not encrypted: anyone who can read the user's files
can read it, as with any saved credential of a command-line tool.

The environment and the command line always win over the file.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Any

FILE = "login.json"


def directory() -> Path:
    """`SERPMAVEN_CONFIG_DIR`, else the platform's per-user config folder."""
    given = os.environ.get("SERPMAVEN_CONFIG_DIR")
    if given:
        return Path(given)
    if sys.platform == "win32":
        base = os.environ.get("APPDATA") or str(Path.home() / "AppData" / "Roaming")
        return Path(base) / "serpmaven"
    base = os.environ.get("XDG_CONFIG_HOME") or str(Path.home() / ".config")
    return Path(base) / "serpmaven"


def path() -> Path:
    return directory() / FILE


def load() -> dict[str, Any]:
    """The saved login, or an empty dict when there is none or it is unreadable."""
    try:
        data = json.loads(path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def save(data: dict[str, Any]) -> Path:
    """Write the login, readable by its owner only, and return where it went."""
    folder = directory()
    folder.mkdir(parents=True, exist_ok=True)
    target = path()
    # Created with owner-only permissions on POSIX before anything is written,
    # so the password is never in a file others can read; on Windows the
    # per-user config folder is already private to the user.
    fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=2)
    if sys.platform != "win32":
        os.chmod(target, 0o600)
    return target


def delete() -> bool:
    """Remove the saved login; False when there was none."""
    try:
        path().unlink()
    except FileNotFoundError:
        return False
    return True


def nodemaven_credentials() -> tuple[str, str] | None:
    """NodeMaven proxy username and password: the environment first, then the file."""
    login = os.environ.get("NODEMAVEN_LOGIN")
    password = os.environ.get("NODEMAVEN_PASSWORD")
    if login and password:
        return login, password
    saved = load()
    login, password = saved.get("nodemaven_login"), saved.get("nodemaven_password")
    if isinstance(login, str) and isinstance(password, str) and login and password:
        return login, password
    return None


def saved_proxy() -> str | None:
    """A proxy URL saved with `serpmaven login --proxy`."""
    url = load().get("proxy")
    return url if isinstance(url, str) and url else None
