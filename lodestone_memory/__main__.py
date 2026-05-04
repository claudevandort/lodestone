"""`python -m lodestone_memory` → run the MCP server.

The plugin manifest (`.claude-plugin/plugin.json`) references this so Claude
Code spawns the server as a stdio process. Kept as a thin wrapper around
server.main() so the entry path is invariant under future server.py edits.
"""
from __future__ import annotations

import importlib
import os
import subprocess
import sys
from pathlib import Path


def _bootstrap_plugin_deps_if_missing() -> None:
    """Install plugin runtime deps if missing, before importing server.

    The SessionStart hook in hooks.json installs the same deps, but it runs
    asynchronously with MCP server startup. On a cold cache (first session
    after `/plugin install`) the server can lose the race and crash with
    `ModuleNotFoundError: No module named 'mcp'`, surfacing as a connection
    failure to the user. Self-heal by installing deps here too — idempotent
    no-op once the hook (or a previous session) has populated site-packages.

    Skipped in dev (when not invoked as a plugin) — the .venv supplies deps.
    """
    plugin_root = os.environ.get("CLAUDE_PLUGIN_ROOT")
    plugin_data = os.environ.get("CLAUDE_PLUGIN_DATA")
    if not plugin_root or not plugin_data:
        return
    requirements = Path(plugin_root) / "requirements.txt"
    if not requirements.exists():
        return
    marker = Path(plugin_data) / "requirements.txt"
    if marker.exists() and marker.read_bytes() == requirements.read_bytes():
        return
    target = Path(plugin_data) / "site-packages"
    target.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        [sys.executable, "-m", "pip", "install", "--quiet",
         "--target", str(target), "-r", str(requirements)],
        check=True,
    )
    marker.write_bytes(requirements.read_bytes())
    if str(target) not in sys.path:
        sys.path.insert(0, str(target))
    importlib.invalidate_caches()


_bootstrap_plugin_deps_if_missing()
from .server import main  # noqa: E402  (after deps bootstrap)


if __name__ == "__main__":
    main()
