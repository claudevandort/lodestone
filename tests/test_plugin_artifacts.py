"""Validate the shape of plugin-distribution artifacts.

These aren't unit tests of behavior — they're cheap structural checks that
the JSON manifests and markdown agent files conform to the formats Claude
Code's plugin system expects. Catches typos, missing required fields, and
references to tools that don't exist.
"""
from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
PLUGIN_MANIFEST = REPO / ".claude-plugin" / "plugin.json"
MARKETPLACE_MANIFEST = REPO / ".claude-plugin" / "marketplace.json"
HOOKS_CONFIG = REPO / "hooks" / "hooks.json"
CURATOR_AGENT = REPO / "agents" / "lodestone-curator.md"
CAPTURE_COMMAND = REPO / "commands" / "capture.md"

# Source of truth for valid lodestone tool names — derived from the actual
# server registration so this test fails if a tool is renamed/removed.
def _live_lodestone_tool_names() -> set[str]:
    import asyncio
    from lodestone_mcp.server import mcp
    return {f"mcp__lodestone__{t.name}" for t in asyncio.run(mcp.list_tools())}


# ---- plugin.json ----

def test_plugin_manifest_is_valid_json():
    json.loads(PLUGIN_MANIFEST.read_text())


def test_plugin_manifest_has_required_fields():
    m = json.loads(PLUGIN_MANIFEST.read_text())
    for field in ("name", "version", "description"):
        assert field in m, f"plugin.json missing required field: {field}"
    assert isinstance(m["name"], str) and m["name"]
    assert re.match(r"^\d+\.\d+\.\d+", m["version"]), \
        f"version should be SemVer-shaped, got {m['version']!r}"


def test_plugin_manifest_declares_lodestone_mcp_server():
    m = json.loads(PLUGIN_MANIFEST.read_text())
    assert "mcpServers" in m
    assert "lodestone" in m["mcpServers"]
    server_cfg = m["mcpServers"]["lodestone"]
    assert server_cfg["command"] == "python"
    assert server_cfg["args"] == ["-m", "lodestone_mcp"]
    # PYTHONPATH must include the plugin root for `python -m lodestone_mcp`
    assert "${CLAUDE_PLUGIN_ROOT}" in server_cfg["env"]["PYTHONPATH"]


def test_plugin_manifest_references_hooks_file_that_exists():
    m = json.loads(PLUGIN_MANIFEST.read_text())
    hooks_ref = m.get("hooks")
    assert hooks_ref, "plugin.json should reference a hooks file"
    hooks_path = (PLUGIN_MANIFEST.parent / hooks_ref).resolve() \
        if hooks_ref.startswith("./") else REPO / hooks_ref
    # Allow ./hooks/hooks.json relative to plugin.json's dir, or repo-relative
    assert hooks_path.exists() or HOOKS_CONFIG.exists()


# ---- marketplace.json ----

def test_marketplace_manifest_is_valid_json_and_lists_lodestone():
    m = json.loads(MARKETPLACE_MANIFEST.read_text())
    assert "plugins" in m and isinstance(m["plugins"], list)
    names = [p["name"] for p in m["plugins"]]
    assert "lodestone" in names


# ---- hooks/hooks.json ----

def test_hooks_config_is_valid_json():
    json.loads(HOOKS_CONFIG.read_text())


def test_hooks_config_declares_session_start_and_post_tool_use():
    h = json.loads(HOOKS_CONFIG.read_text())
    assert "hooks" in h
    assert "SessionStart" in h["hooks"], "missing SessionStart for deps install"
    assert "PostToolUse" in h["hooks"], "missing PostToolUse for dual-write"


def test_post_tool_use_matches_write():
    h = json.loads(HOOKS_CONFIG.read_text())
    pt = h["hooks"]["PostToolUse"]
    assert any(group.get("matcher") == "Write" for group in pt), \
        "PostToolUse should match the Write tool"


def test_post_tool_use_invokes_mirror_script():
    h = json.loads(HOOKS_CONFIG.read_text())
    cmds = []
    for group in h["hooks"]["PostToolUse"]:
        for hook in group.get("hooks", []):
            cmds.append(hook.get("command", ""))
    assert any("mirror.py" in c for c in cmds), \
        "PostToolUse Write hook should run lodestone_mcp/mirror.py"


# ---- agents/lodestone-curator.md ----

_FRONTMATTER_RE = re.compile(r"^---\n(.*?)\n---\n", re.DOTALL)


def _parse_frontmatter(text: str) -> dict[str, str]:
    """Light parser for the YAML-ish frontmatter agents use. Same shape as
    lodestone_mcp.mirror.parse_frontmatter but local to keep tests independent.
    """
    m = _FRONTMATTER_RE.match(text)
    assert m, "agent file must start with --- frontmatter ---"
    fields = {}
    for line in m.group(1).splitlines():
        if ":" in line:
            k, _, v = line.partition(":")
            fields[k.strip()] = v.strip()
    return fields


def test_curator_agent_file_exists():
    assert CURATOR_AGENT.exists(), f"missing agent file: {CURATOR_AGENT}"


def test_curator_agent_has_required_frontmatter_fields():
    fm = _parse_frontmatter(CURATOR_AGENT.read_text())
    for field in ("name", "description", "tools"):
        assert field in fm, f"curator agent missing frontmatter field: {field}"
    assert fm["name"] == "lodestone-curator"


def test_curator_agent_only_references_real_lodestone_tools():
    fm = _parse_frontmatter(CURATOR_AGENT.read_text())
    declared = [t.strip() for t in fm["tools"].split(",")]
    declared_lodestone = {t for t in declared if t.startswith("mcp__lodestone__")}
    real_tools = _live_lodestone_tool_names()
    extras = declared_lodestone - real_tools
    assert not extras, f"agent references non-existent lodestone tools: {extras}"


def test_curator_agent_does_not_grant_forget():
    """PRD §10 open question 2: curator should NOT have forget access in v1."""
    fm = _parse_frontmatter(CURATOR_AGENT.read_text())
    declared = [t.strip() for t in fm["tools"].split(",")]
    assert "mcp__lodestone__forget" not in declared, \
        "v1 curator should not be able to forget; revisit per PRD §10 Q2"


def test_curator_agent_body_is_substantive():
    """A frontmatter-only agent file is a useless agent."""
    text = CURATOR_AGENT.read_text()
    body = _FRONTMATTER_RE.sub("", text, count=1)
    assert len(body.strip()) > 500, "curator agent system prompt is suspiciously short"


# ---- commands/capture.md ----

def test_capture_command_file_exists():
    assert CAPTURE_COMMAND.exists(), f"missing command file: {CAPTURE_COMMAND}"


def test_capture_command_has_description_frontmatter():
    fm = _parse_frontmatter(CAPTURE_COMMAND.read_text())
    assert "description" in fm and fm["description"], \
        "capture.md should have a description (used by /capture autocomplete)"


def test_capture_command_invokes_curator():
    """The slash command's whole job is to spawn the curator subagent."""
    text = CAPTURE_COMMAND.read_text()
    assert "lodestone-curator" in text, \
        "capture.md should reference the lodestone-curator subagent"


def test_capture_command_passes_arguments_through():
    """`/capture <topic>` should expose $ARGUMENTS to focus the curator."""
    text = CAPTURE_COMMAND.read_text()
    assert "$ARGUMENTS" in text, \
        "capture.md should reference $ARGUMENTS so /capture <topic> works"
