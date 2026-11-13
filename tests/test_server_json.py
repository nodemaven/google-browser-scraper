"""server.json is what the MCP Registry lists; it has to match the package it names."""

import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def test_server_json_names_this_package_at_its_version():
    server = json.loads((ROOT / "server.json").read_text(encoding="utf-8"))
    pyproject = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    project = dict(re.findall(r'^(name|version) = "([^"]+)"', pyproject, re.M))  # 3.10: no tomllib
    (package,) = server["packages"]
    assert package["identifier"] == project["name"]
    # The registry lists the version a release uploaded: bump all three together.
    assert server["version"] == package["version"] == project["version"]
    assert [a["value"] for a in package["packageArguments"]] == ["mcp"]


def test_the_readme_carries_the_name_the_registry_checks_on_pypi():
    server = json.loads((ROOT / "server.json").read_text(encoding="utf-8"))
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    marker = f"<!-- mcp-name: {server['name']} -->"
    assert re.search(rf"(?m)^{re.escape(marker)}$", readme)
