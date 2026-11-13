"""The Gemini CLI extension (gemini-extension.json, GEMINI.md) installs this repository and
starts its MCP server; it has to name this version and a command the CLI accepts."""

import json
import re
from pathlib import Path

from serpmaven import cli

ROOT = Path(__file__).resolve().parent.parent


def test_the_extension_names_this_version_and_its_context_file():
    extension = json.loads((ROOT / "gemini-extension.json").read_text(encoding="utf-8"))
    pyproject = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    version = re.search(r'^version = "([^"]+)"', pyproject, re.M).group(1)
    assert extension["version"] == version  # bump it with every release
    assert (ROOT / extension["contextFileName"]).is_file()


def test_the_extension_starts_a_command_the_cli_accepts():
    extension = json.loads((ROOT / "gemini-extension.json").read_text(encoding="utf-8"))
    (server,) = extension["mcpServers"].values()
    assert server["command"] == "uvx" and server["args"][0] == f"serpmaven=={extension['version']}"
    args = cli._parser().parse_args(server["args"][1:])
    assert args.command == "mcp"
