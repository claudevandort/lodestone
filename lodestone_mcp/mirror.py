"""PostToolUse hook script: mirrors Claude Code auto-memory writes into lodestone.

Invoked by the plugin's hooks/hooks.json on every successful Write tool call.
Reads the hook payload as JSON on stdin, determines if the Write touched an
auto-memory file (~/.claude/projects/<project>/memory/*.md, excluding
MEMORY.md), and if so upserts the corresponding lodestone memory keyed by
`source_file`.

Designed so the mechanical dual-write (auto-memory → lodestone) is enforced
by the plugin, not by Claude's prompt discipline.

Exit codes:
  0 — handled (mirrored, or no-op because the file isn't an auto-memory file)
  1 — soft error (parse failure, IO failure); reported to stderr but does
      NOT block the original Write call.
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path
from typing import Any

from lodestone_mcp import db, memory
from lodestone_mcp.project import derive_project_id

# Auto-memory paths look like:
#   <home>/.claude/projects/<sanitized-project-path>/memory/<name>.md
_AUTO_MEMORY_RE = re.compile(r".+/\.claude/projects/[^/]+/memory/[^/]+\.md$")
_INDEX_FILE = "MEMORY.md"

# Map auto-memory's `type` frontmatter to lodestone's `kind` enum.
_TYPE_TO_KIND = {
    "feedback":   "preference",   # Claude Code auto-memory uses "feedback" for user prefs
    "project":    "fact",         # project-context type → ambient fact
    "decision":   "decision",
    "attempt":    "attempt",
    "gotcha":     "gotcha",
    "preference": "preference",
    "fact":       "fact",
    "question":   "question",
}
_DEFAULT_KIND = "fact"


# ---- pure helpers (testable without a DB or hook payload) ----

def is_auto_memory_path(path: Path) -> bool:
    """True iff the path is an auto-memory file (and not the MEMORY.md index)."""
    s = str(path)
    return bool(_AUTO_MEMORY_RE.match(s)) and path.name != _INDEX_FILE


def parse_frontmatter(text: str) -> tuple[dict[str, str], str]:
    """Parse `---\\n<key>: <value>\\n---\\n<body>` frontmatter.

    Hand-rolled (no PyYAML dep) because auto-memory frontmatter is flat
    key:value lines — no nested structures, no multi-line values.

    Returns (fields_dict, body). If the text has no frontmatter, returns
    ({}, text).
    """
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        return {}, text

    fields: dict[str, str] = {}
    body_start: int | None = None
    for i, line in enumerate(lines[1:], start=1):
        if line.strip() == "---":
            body_start = i + 1
            break
        if ":" not in line:
            continue
        key, _, value = line.partition(":")
        fields[key.strip()] = value.strip()

    if body_start is None:
        # Unterminated frontmatter — treat the whole thing as body.
        return {}, text

    body = "\n".join(lines[body_start:]).lstrip("\n")
    return fields, body


def map_to_lodestone_fields(
    frontmatter: dict[str, str], body: str
) -> dict[str, Any] | None:
    """Map an auto-memory file's parsed contents to lodestone fields.

    Returns the dict ready to pass into upsert(), or None if the file is
    missing fields lodestone requires (no `name` for title, no usable content).
    """
    title = (frontmatter.get("name") or "").strip()
    if not title:
        return None

    # Per PRD: prefer the body text over the frontmatter `description` line —
    # body holds the substantive insight, description is just a one-liner.
    body_clean = body.strip()
    description = (frontmatter.get("description") or "").strip()
    content = body_clean or description
    if not content:
        return None

    type_ = (frontmatter.get("type") or "").strip().lower()
    kind = _TYPE_TO_KIND.get(type_, _DEFAULT_KIND)

    return {"title": title, "content": content, "kind": kind}


# ---- DB-touching upsert ----

def upsert_memory(
    conn,
    *,
    source_file: str,
    project_id: str,
    project_label: str,
    fields: dict[str, Any],
) -> tuple[str, str]:
    """Upsert a memory keyed by (source_file, project_id).

    If a row exists, update title/content/kind in place. Otherwise insert
    a new memory carrying source_file as the dedup key.

    Returns (action, uuid) where action is 'created' or 'updated'.
    """
    existing = conn.execute(
        "SELECT uuid FROM memories "
        "WHERE source_file = ? AND project_id = ? AND deleted_at IS NULL",
        (source_file, project_id),
    ).fetchone()

    if existing:
        memory.update(conn, uuid=existing["uuid"], patch=fields)
        return ("updated", existing["uuid"])

    result = memory.remember(
        conn,
        project_id=project_id,
        project_label=project_label,
        source_file=source_file,
        **fields,
    )
    return ("created", result["uuid"])


# ---- entry point ----

def main() -> int:
    """Hook entry. Reads JSON from stdin, returns exit code (0 = handled)."""
    try:
        payload = json.load(sys.stdin)
    except json.JSONDecodeError as e:
        print(f"lodestone-mirror: invalid hook payload: {e}", file=sys.stderr)
        return 1

    tool_name = payload.get("tool_name", "")
    if tool_name != "Write":
        return 0  # not our event

    file_path_str = (payload.get("tool_input") or {}).get("file_path")
    if not file_path_str:
        return 0

    file_path = Path(file_path_str).resolve()

    if not is_auto_memory_path(file_path):
        return 0

    if not file_path.exists():
        # File deleted/moved between Write and hook fire — nothing to mirror.
        return 0

    try:
        text = file_path.read_text()
    except OSError as e:
        print(f"lodestone-mirror: read failed for {file_path}: {e}", file=sys.stderr)
        return 1

    frontmatter, body = parse_frontmatter(text)
    fields = map_to_lodestone_fields(frontmatter, body)
    if fields is None:
        print(
            f"lodestone-mirror: skipped {file_path.name} — missing name or content",
            file=sys.stderr,
        )
        return 0

    cwd = payload.get("cwd")
    project_id, project_label = derive_project_id(Path(cwd) if cwd else None)

    conn = db.open_db()
    try:
        action, uuid = upsert_memory(
            conn,
            source_file=str(file_path),
            project_id=project_id,
            project_label=project_label,
            fields=fields,
        )
    finally:
        conn.close()

    print(f"lodestone-mirror: {action} memory {uuid[:8]} from {file_path.name}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
