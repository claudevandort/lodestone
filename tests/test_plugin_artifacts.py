"""Validate the shape of plugin-distribution artifacts.

These aren't unit tests of behavior — they're cheap structural checks that
the JSON manifests and markdown agent files conform to the formats Claude
Code's plugin system expects. Catches typos, missing required fields, and
references to tools that don't exist.
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
PLUGIN_MANIFEST = REPO / ".claude-plugin" / "plugin.json"
MARKETPLACE_MANIFEST = REPO / ".claude-plugin" / "marketplace.json"
HOOKS_CONFIG = REPO / "hooks" / "hooks.json"
CURATOR_AGENT = REPO / "agents" / "lodestone-memory-curator.md"
REMEMBER_COMMAND = REPO / "commands" / "remember.md"

# Source of truth for valid lodestone tool names. Returns BOTH naming forms
# the curator might see depending on how lodestone is installed:
#   - global MCP registration (dev/eval):  mcp__lodestone__<tool>
#   - plugin install (production):         mcp__plugin_<plugin>_<server>__<tool>
# The curator's frontmatter must declare both forms to work in both contexts.
def _live_lodestone_tool_names() -> set[str]:
    import asyncio
    from lodestone_memory.server import mcp
    base = [t.name for t in asyncio.run(mcp.list_tools())]
    manifest = json.loads(PLUGIN_MANIFEST.read_text())
    plugin_name = manifest["name"]
    server_names = list(manifest.get("mcpServers", {}).keys())
    forms = {f"mcp__lodestone__{n}" for n in base}
    for server_name in server_names:
        forms |= {f"mcp__plugin_{plugin_name}_{server_name}__{n}" for n in base}
    return forms


def _is_lodestone_tool_name(name: str) -> bool:
    return name.startswith("mcp__lodestone__") or name.startswith("mcp__plugin_")


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


def test_plugin_manifest_declares_lodestone_memory_server():
    m = json.loads(PLUGIN_MANIFEST.read_text())
    assert "mcpServers" in m
    assert "lodestone" in m["mcpServers"]
    server_cfg = m["mcpServers"]["lodestone"]
    assert server_cfg["command"] == "python"
    assert server_cfg["args"] == ["-m", "lodestone_memory"]
    # PYTHONPATH must include the plugin root for `python -m lodestone_memory`
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

def test_marketplace_manifest_is_valid_json_and_lists_lodestone_memory():
    m = json.loads(MARKETPLACE_MANIFEST.read_text())
    assert "plugins" in m and isinstance(m["plugins"], list)
    names = [p["name"] for p in m["plugins"]]
    assert "lodestone-memory" in names


def test_marketplace_manifest_has_required_owner_field():
    """Regression: Claude Code's marketplace schema rejects manifests missing
    a top-level `owner` object with at least a `name`. Found during §4 first
    install attempt: `/plugin marketplace add` failed with
    `owner: Invalid input: expected object, received undefined`. The schema
    isn't versioned in the file, so any new field the validator adds in the
    future would surface the same way — keep this test honest by re-running
    `claude plugin validate` when bumping plugin metadata.
    """
    m = json.loads(MARKETPLACE_MANIFEST.read_text())
    assert "owner" in m, "marketplace.json must declare a top-level `owner`"
    owner = m["owner"]
    assert isinstance(owner, dict), "`owner` must be an object"
    assert owner.get("name"), "`owner.name` must be a non-empty string"


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
        "PostToolUse Write hook should run lodestone_memory/mirror.py"


# ---- agents/lodestone-memory-curator.md ----

_FRONTMATTER_RE = re.compile(r"^---\n(.*?)\n---\n", re.DOTALL)


def _parse_frontmatter(text: str) -> dict[str, str]:
    """Light parser for the YAML-ish frontmatter agents use. Same shape as
    lodestone_memory.mirror.parse_frontmatter but local to keep tests independent.
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
    assert fm["name"] == "lodestone-memory-curator"


def test_curator_agent_only_references_real_lodestone_tools():
    fm = _parse_frontmatter(CURATOR_AGENT.read_text())
    declared = [t.strip() for t in fm["tools"].split(",")]
    declared_lodestone = {t for t in declared if _is_lodestone_tool_name(t)}
    real_tools = _live_lodestone_tool_names()
    extras = declared_lodestone - real_tools
    assert not extras, f"agent references non-existent lodestone tools: {extras}"


def test_curator_agent_declares_both_global_and_plugin_prefixes():
    """Curator must work in dev (global MCP config) AND prod (plugin install).
    Tool names differ between modes — declare both."""
    fm = _parse_frontmatter(CURATOR_AGENT.read_text())
    declared = {t.strip() for t in fm["tools"].split(",")}
    has_global = any(t.startswith("mcp__lodestone__") for t in declared)
    has_plugin = any(t.startswith("mcp__plugin_") for t in declared)
    assert has_global, "missing mcp__lodestone__* declarations (needed for dev/eval)"
    assert has_plugin, "missing mcp__plugin_* declarations (needed for plugin install)"


def test_curator_agent_does_not_grant_forget():
    """PRD §10 open question 2: curator should NOT have forget access in v1."""
    fm = _parse_frontmatter(CURATOR_AGENT.read_text())
    declared = [t.strip() for t in fm["tools"].split(",")]
    assert "mcp__lodestone__forget" not in declared, \
        "v1 curator should not be able to forget (global form); revisit per PRD §10 Q2"
    assert "mcp__plugin_lodestone-memory_lodestone__forget" not in declared, \
        "v1 curator should not be able to forget (plugin form); revisit per PRD §10 Q2"


def test_curator_agent_body_is_substantive():
    """A frontmatter-only agent file is a useless agent."""
    text = CURATOR_AGENT.read_text()
    body = _FRONTMATTER_RE.sub("", text, count=1)
    assert len(body.strip()) > 500, "curator agent system prompt is suspiciously short"


# ---- commands/remember.md ----

def test_remember_command_file_exists():
    assert REMEMBER_COMMAND.exists(), f"missing command file: {REMEMBER_COMMAND}"


def test_remember_command_has_description_frontmatter():
    fm = _parse_frontmatter(REMEMBER_COMMAND.read_text())
    assert "description" in fm and fm["description"], \
        "remember.md should have a description (used by /remember autocomplete)"


def test_remember_command_invokes_curator():
    """The slash command's whole job is to spawn the curator subagent."""
    text = REMEMBER_COMMAND.read_text()
    assert "lodestone-memory-curator" in text, \
        "remember.md should reference the lodestone-memory-curator subagent"


def test_remember_command_passes_arguments_through():
    """`/remember <topic>` should expose $ARGUMENTS to focus the curator."""
    text = REMEMBER_COMMAND.read_text()
    assert "$ARGUMENTS" in text, \
        "remember.md should reference $ARGUMENTS so /remember <topic> works"


# ---- __main__.py self-heal on cold cache ----

def test_main_bootstraps_deps_before_importing_server():
    """Regression: __main__.py must self-heal deps before `from .server import main`.

    The SessionStart hook installs deps asynchronously with MCP server startup.
    On a cold cache (first session after `/plugin install`) the server can lose
    the race and crash with `ModuleNotFoundError: No module named 'mcp'`,
    surfacing as a connection failure to the user. __main__.py must call the
    bootstrap function BEFORE importing from .server, so a synchronous fallback
    install closes the race window.
    """
    text = (REPO / "lodestone_memory" / "__main__.py").read_text()
    bootstrap_pos = text.find("_bootstrap_plugin_deps_if_missing()")
    server_import_pos = text.find("from .server import main")
    assert 0 < bootstrap_pos < server_import_pos, (
        "__main__.py must call _bootstrap_plugin_deps_if_missing() before "
        "importing from .server (cold-cache race fix)"
    )


def test_bootstrap_is_noop_outside_plugin_env(monkeypatch):
    """Bootstrap must not try to install anything in dev (no plugin env vars).

    When running via the .venv entry point (`lodestone-memory`), CLAUDE_PLUGIN_ROOT
    and CLAUDE_PLUGIN_DATA aren't set; deps come from the venv. Calling the
    bootstrap function in that mode should be a clean no-op.
    """
    from lodestone_memory.__main__ import _bootstrap_plugin_deps_if_missing
    monkeypatch.delenv("CLAUDE_PLUGIN_ROOT", raising=False)
    monkeypatch.delenv("CLAUDE_PLUGIN_DATA", raising=False)
    calls = []
    monkeypatch.setattr("subprocess.run", lambda *a, **kw: calls.append((a, kw)))
    _bootstrap_plugin_deps_if_missing()
    assert calls == [], "bootstrap must be a no-op when not running as a plugin"


def test_bootstrap_is_noop_when_marker_matches(monkeypatch, tmp_path):
    """Warm-cache fast path: marker file equals requirements.txt → no pip call."""
    from lodestone_memory.__main__ import _bootstrap_plugin_deps_if_missing
    plugin_root = tmp_path / "root"
    plugin_data = tmp_path / "data"
    plugin_root.mkdir()
    plugin_data.mkdir()
    (plugin_root / "requirements.txt").write_bytes(b"mcp==1.0\n")
    (plugin_data / "requirements.txt").write_bytes(b"mcp==1.0\n")
    monkeypatch.setenv("CLAUDE_PLUGIN_ROOT", str(plugin_root))
    monkeypatch.setenv("CLAUDE_PLUGIN_DATA", str(plugin_data))
    calls = []
    monkeypatch.setattr("subprocess.run", lambda *a, **kw: calls.append((a, kw)))
    _bootstrap_plugin_deps_if_missing()
    assert calls == [], "bootstrap must skip pip install when marker already matches"


def test_bootstrap_runs_pip_install_when_marker_missing(monkeypatch, tmp_path):
    """Cold-cache slow path: no marker → pip install with the right args, then write marker."""
    from lodestone_memory.__main__ import _bootstrap_plugin_deps_if_missing
    plugin_root = tmp_path / "root"
    plugin_data = tmp_path / "data"
    plugin_root.mkdir()
    plugin_data.mkdir()
    requirements_text = b"mcp==1.0\nvoyageai==0.3\n"
    (plugin_root / "requirements.txt").write_bytes(requirements_text)
    monkeypatch.setenv("CLAUDE_PLUGIN_ROOT", str(plugin_root))
    monkeypatch.setenv("CLAUDE_PLUGIN_DATA", str(plugin_data))

    calls = []
    def fake_run(cmd, **kw):
        calls.append((cmd, kw))
        # Simulate pip succeeding; bootstrap will write the marker after.
        class _Result:
            returncode = 0
        return _Result()
    monkeypatch.setattr("subprocess.run", fake_run)
    _bootstrap_plugin_deps_if_missing()

    assert len(calls) == 1, "bootstrap must invoke pip exactly once"
    cmd, kw = calls[0]
    assert cmd[:3] == [sys.executable, "-m", "pip"], \
        "must invoke pip via the current python (matches plugin's interpreter)"
    assert "install" in cmd and "--target" in cmd and "-r" in cmd
    target_idx = cmd.index("--target")
    assert cmd[target_idx + 1] == str(plugin_data / "site-packages")
    req_idx = cmd.index("-r")
    assert cmd[req_idx + 1] == str(plugin_root / "requirements.txt")
    assert kw.get("check") is True, "must raise on pip failure (don't silently swallow)"
    # And the marker is written afterwards so the next call is a no-op.
    assert (plugin_data / "requirements.txt").read_bytes() == requirements_text


# ---- VOYAGE_API_KEY env sanitize before load_dotenv ----

@pytest.mark.parametrize("module_name,filename", [
    ("server", "server.py"),
    ("mirror", "mirror.py"),
])
def test_sanitizes_voyage_key_before_load_dotenv(module_name, filename):
    """Regression: when running as a plugin, the manifest's `${VOYAGE_API_KEY}`
    substitution leaves the literal string in the process env if the shell var
    is unset. load_dotenv() with override=False then refuses to fill in the
    real key from ~/.lodestone/.env, and Voyage rejects the literal as
    "Provided API key is invalid". Both the MCP server and the PostToolUse
    mirror hook must strip an unsubstituted/empty VOYAGE_API_KEY BEFORE the
    first load_dotenv call so the dotenv chain can take effect.

    Caught during §2 manual smoke testing (2026-05-04) — recall/remember both
    failed with "Provided API key is invalid" even though the key in
    ~/.lodestone/.env was valid.
    """
    text = (REPO / "lodestone_memory" / filename).read_text()
    sanitize_pos = text.find('os.environ.pop("VOYAGE_API_KEY"')
    dotenv_pos = text.find("load_dotenv()")
    assert sanitize_pos != -1, \
        f"{filename} must sanitize VOYAGE_API_KEY before load_dotenv (look for os.environ.pop)"
    assert 0 < sanitize_pos < dotenv_pos, \
        f"{filename}: VOYAGE_API_KEY sanitize must run before load_dotenv()"


@pytest.mark.parametrize("bad_value", ["", "${VOYAGE_API_KEY}", "${VOYAGE}"])
def test_sanitize_strips_unsubstituted_or_empty(monkeypatch, bad_value):
    """An empty or `${...}` literal must be popped so the dotenv fallback kicks in."""
    monkeypatch.setenv("VOYAGE_API_KEY", bad_value)
    import os as _os
    _voyage = _os.environ.get("VOYAGE_API_KEY", "")
    if not _voyage or _voyage.startswith("${"):
        _os.environ.pop("VOYAGE_API_KEY", None)
    assert "VOYAGE_API_KEY" not in _os.environ, \
        f"sanitize must strip {bad_value!r} so load_dotenv can fill from .env files"


def test_sanitize_preserves_real_key(monkeypatch):
    """A legitimate shell-set key must NOT be stripped (Option 1 in README)."""
    monkeypatch.setenv("VOYAGE_API_KEY", "pa-shell-set-key")
    import os as _os
    _voyage = _os.environ.get("VOYAGE_API_KEY", "")
    if not _voyage or _voyage.startswith("${"):
        _os.environ.pop("VOYAGE_API_KEY", None)
    assert _os.environ.get("VOYAGE_API_KEY") == "pa-shell-set-key"


# ---- mirror.py hook env-loading ----

def test_mirror_loads_dotenv_at_import():
    """Regression: mirror.py must call load_dotenv() before importing memory.

    The PostToolUse hook is invoked by Claude Code with a process env that
    does NOT pass through the user's shell env (no VOYAGE_API_KEY). Without
    explicit load_dotenv() the embedding call in the upsert path fails with
    `RuntimeError: VOYAGE_API_KEY is not set` and the hook exits non-zero.

    Found in crm1 dogfood: the hook fired correctly on an auto-memory write
    but the memory never landed in lodestone because the embed call exploded.
    """
    text = (REPO / "lodestone_memory" / "mirror.py").read_text()
    assert "load_dotenv" in text, \
        "mirror.py must call load_dotenv() so the hook can find VOYAGE_API_KEY"
    # And specifically before the imports that consume env at first use
    dotenv_pos = text.find("load_dotenv()")
    memory_import_pos = text.find("from lodestone_memory import")
    assert 0 < dotenv_pos < memory_import_pos, \
        "load_dotenv() must run before importing lodestone_memory.{db,memory}"
