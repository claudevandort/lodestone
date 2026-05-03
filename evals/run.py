"""End-to-end eval pipeline: load scenarios, drive Claude headless, store, grade.

Per scenario:
  1. Spin up an isolated sandbox (temp dir, temp DB, scoped project_id).
  2. Seed the sandbox with the scenario's memories (direct lodestone import).
  3. Write a temp .mcp.json pointing at lodestone with sandbox env vars.
  4. Invoke `claude -p <user_message>` with --strict-mcp-config.
  5. Parse the stream-json output to extract tool_use blocks + final text.
  6. Append to evals/results/results-<timestamp>.json + latest.json.

After all scenarios run, grade them and exit with the grade's status (0 = pass).

Usage:
  .venv/bin/python evals/run.py                 # run + grade (default)
  .venv/bin/python evals/run.py --no-grade      # run only; inspect later
  .venv/bin/python evals/grade.py               # re-grade the last run
"""
from __future__ import annotations

import argparse
import functools
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path

from dotenv import load_dotenv

import grade as grader  # sibling module, evals/grade.py
import history  # sibling module, evals/history.py

REPO_ROOT = Path(__file__).resolve().parent.parent
LODESTONE_BIN = REPO_ROOT / ".venv" / "bin" / "lodestone-memory"
SCENARIOS_PATH = REPO_ROOT / "evals" / "scenarios.json"
RESULTS_DIR = REPO_ROOT / "evals" / "results"
HISTORY_PATH = RESULTS_DIR / "history.jsonl"

# Bound any single scenario to keep a stuck Claude from hanging the run.
SCENARIO_TIMEOUT_SECONDS = 180

# Brief wait after subprocess exit so Claude Code's session-log writer
# finishes flushing before we sweep ~/.claude/projects for orphans.
CLAUDE_LOG_FLUSH_DELAY_SECONDS = 2


@functools.cache
def lodestone_tool_names() -> list[str]:
    """Pull live tool names from the lodestone server registration so the
    eval allowlist can never drift out of sync with what's actually shipped.
    """
    import asyncio
    from lodestone_memory.server import mcp
    return [f"mcp__lodestone__{t.name}" for t in asyncio.run(mcp.list_tools())]


def seed_db(db_path: Path, project_id: str, seeds: list[dict]) -> None:
    """Insert the scenario's memories into a fresh sandbox DB.

    Uses lodestone's own remember() so embeddings + FTS rows are populated
    exactly the same way they would be in production.
    """
    # Import locally so that callers who only want to grade don't need to
    # boot lodestone or its embedding deps.
    from lodestone_memory import db, memory

    conn = db.open_db(db_path)
    for seed in seeds:
        memory.remember(conn, project_id=project_id, **seed)
    conn.close()


def write_mcp_config(path: Path, db_path: Path, project_id: str) -> None:
    config = {
        "mcpServers": {
            "lodestone": {
                "command": str(LODESTONE_BIN),
                "args": [],
                "env": {
                    "LODESTONE_DB": str(db_path),
                    "LODESTONE_PROJECT_ID": project_id,
                    # Pass the user's Voyage key through; the spawned MCP
                    # server inherits this via the env block above.
                    "VOYAGE_API_KEY": os.environ.get("VOYAGE_API_KEY", ""),
                },
            }
        }
    }
    path.write_text(json.dumps(config, indent=2))


def run_claude(user_message: str, mcp_config: Path, cwd: Path) -> tuple[list[dict], str]:
    cmd = [
        "claude",
        "-p", user_message,
        "--output-format", "stream-json",
        "--verbose",
        "--mcp-config", str(mcp_config),
        "--strict-mcp-config",
        "--allowedTools", ",".join(lodestone_tool_names()),
        "--permission-mode", "bypassPermissions",
    ]
    result = subprocess.run(
        cmd,
        cwd=cwd,
        capture_output=True,
        text=True,
        timeout=SCENARIO_TIMEOUT_SECONDS,
    )
    if result.returncode != 0:
        print(f"  WARNING: claude exited {result.returncode}", file=sys.stderr)
        if result.stderr.strip():
            print(f"  stderr: {result.stderr.strip()[:500]}", file=sys.stderr)

    events, parse_failures = _parse_stream_json(result.stdout)
    if parse_failures:
        print(
            f"  WARNING: dropped {parse_failures} unparseable line(s) from "
            f"stream-json output (format may have changed)",
            file=sys.stderr,
        )
    return events, result.stderr


def _parse_stream_json(stdout: str) -> tuple[list[dict], int]:
    """Parse Claude Code's stream-json output (one JSON object per line).

    Returns (events, parse_failure_count) so callers can surface format drift.
    """
    events: list[dict] = []
    failures = 0
    for line in stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            events.append(json.loads(line))
        except json.JSONDecodeError:
            failures += 1
    return events, failures


def extract_trace(events: list[dict]) -> dict:
    """Walk the stream-json events and pull out tool calls + final text."""
    tool_calls: list[dict] = []
    text_chunks: list[str] = []
    final_result: str | None = None

    for ev in events:
        ev_type = ev.get("type")
        if ev_type == "assistant":
            for block in ev.get("message", {}).get("content", []) or []:
                btype = block.get("type")
                if btype == "tool_use":
                    tool_calls.append({
                        "name": block.get("name"),
                        "input": block.get("input"),
                    })
                elif btype == "text":
                    text_chunks.append(block.get("text", ""))
        elif ev_type == "result":
            r = ev.get("result")
            if isinstance(r, str):
                final_result = r

    return {
        "tool_calls": tool_calls,
        "final_text": final_result if final_result is not None else "".join(text_chunks),
    }


def run_scenario(scenario: dict) -> dict:
    sid = scenario["id"]
    project_id = f"eval-{sid}"

    with tempfile.TemporaryDirectory(prefix=f"lodestone-eval-{sid}-") as tmp:
        tmp_dir = Path(tmp)
        db_path = tmp_dir / "sandbox.db"
        cfg_path = tmp_dir / ".mcp.json"

        seed_db(db_path, project_id, scenario.get("seed_memories", []))
        write_mcp_config(cfg_path, db_path, project_id)

        events, stderr = run_claude(scenario["user_message"], cfg_path, tmp_dir)
        trace = extract_trace(events)

    return {
        "id": sid,
        "trace": trace,
        "raw_event_count": len(events),
        "stderr_tail": stderr.strip().splitlines()[-5:] if stderr.strip() else [],
    }


def cleanup_eval_orphans() -> int:
    """Sweep ~/.claude/projects for project dirs created by past eval runs.

    Each headless `claude -p` writes a session-log dir under ~/.claude/projects,
    named after the cwd with `/` → `-`. Our eval cwds live under /tmp and never
    recur, so anything matching `-tmp-lodestone-eval-*` is dead on arrival.
    """
    base = Path.home() / ".claude" / "projects"
    if not base.exists():
        return 0
    removed = 0
    for entry in base.glob("-tmp-lodestone-eval-*"):
        if entry.is_dir():
            shutil.rmtree(entry)
            removed += 1
    return removed


def main() -> None:
    parser = argparse.ArgumentParser(description="Run lodestone prompt evals.")
    parser.add_argument("--no-grade", action="store_true",
                        help="skip grading; just run scenarios and dump results")
    parser.add_argument("--scenario", action="append", metavar="ID",
                        help="run only the named scenario (repeatable)")
    parser.add_argument("--note", type=str, default=None, metavar="TEXT",
                        help="annotate this run in history.jsonl "
                             "(e.g. 'tightened recall description')")
    args = parser.parse_args()

    load_dotenv(REPO_ROOT / ".env")
    load_dotenv(Path.home() / ".lodestone" / ".env")

    if not LODESTONE_BIN.exists():
        sys.exit(f"lodestone-memory binary not found at {LODESTONE_BIN}; run `pip install -e .`")
    if not os.environ.get("VOYAGE_API_KEY"):
        sys.exit("VOYAGE_API_KEY is not set (needed for seeding embeddings)")

    all_scenarios = json.loads(SCENARIOS_PATH.read_text())
    if args.scenario:
        wanted = set(args.scenario)
        scenarios = [s for s in all_scenarios if s["id"] in wanted]
        missing = wanted - {s["id"] for s in scenarios}
        if missing:
            sys.exit(f"unknown scenario(s): {sorted(missing)}")
    else:
        scenarios = all_scenarios

    RESULTS_DIR.mkdir(parents=True, exist_ok=True)

    print(f"running {len(scenarios)} scenario(s)\n")
    results = []
    for scenario in scenarios:
        sid = scenario["id"]
        print(f"running: {sid}")
        try:
            result = run_scenario(scenario)
            ncalls = len(result["trace"]["tool_calls"])
            print(f"  -> {ncalls} tool call(s) captured, {result['raw_event_count']} events")
        except subprocess.TimeoutExpired:
            print(f"  -> TIMEOUT after {SCENARIO_TIMEOUT_SECONDS}s")
            result = {"id": sid, "trace": {"tool_calls": [], "final_text": ""},
                      "raw_event_count": 0, "error": "timeout"}
        results.append(result)

    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    payload = {"timestamp": timestamp, "scenarios": scenarios, "results": results}
    out_path = RESULTS_DIR / f"results-{timestamp}.json"
    out_path.write_text(json.dumps(payload, indent=2))
    (RESULTS_DIR / "latest.json").write_text(json.dumps(payload, indent=2))
    print(f"\nwrote {out_path}")

    # Settle, then sweep ~/.claude/projects for orphan dirs Claude Code wrote
    # for our throwaway temp cwds.
    time.sleep(CLAUDE_LOG_FLUSH_DELAY_SECONDS)
    swept = cleanup_eval_orphans()
    if swept:
        print(f"cleaned {swept} orphan project dir(s) from ~/.claude/projects")

    if args.no_grade:
        print("skipped grading (--no-grade); run `evals/grade.py` to grade later")
        return

    print()
    report = grader.grade(payload)
    grader.print_report(report)

    history.append(
        HISTORY_PATH,
        report,
        results_file=out_path.name,
        timestamp=timestamp,
        note=args.note,
    )
    history.print_tail(HISTORY_PATH)

    sys.exit(0 if report.mean == grader.PASS_SCORE else 1)


if __name__ == "__main__":
    main()
