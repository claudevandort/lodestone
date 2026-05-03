"""`python -m lodestone_memory` → run the MCP server.

The plugin manifest (`.claude-plugin/plugin.json`) references this so Claude
Code spawns the server as a stdio process. Kept as a thin wrapper around
server.main() so the entry path is invariant under future server.py edits.
"""
from .server import main


if __name__ == "__main__":
    main()
