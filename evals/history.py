"""Append-only history of eval runs.

Each line of `history.jsonl` is a slim summary of one run (timestamp, scores,
pointer to the full results file, optional note). Full traces stay in
`results-<timestamp>.json`; this file is what you scan to answer "did my last
prompt change help?".

Designed append-only so concurrent runs (or aborted ones) can't corrupt prior
history.
"""
from __future__ import annotations

import json
from pathlib import Path

import grade as grader  # sibling module, evals/grade.py


def append(
    history_path: Path,
    report: grader.GradeReport,
    *,
    results_file: str,
    timestamp: str,
    note: str | None = None,
) -> dict:
    """Write one summary line to history.jsonl. Returns the entry dict."""
    entry = {
        "timestamp": timestamp,
        "mean": report.mean,
        "median": report.median,
        "modes": report.modes,
        "pass_count": report.pass_count,
        "total": report.total,
        "per_scenario": {s.id: s.score for s in report.scenarios},
        "results_file": results_file,
        "note": note,
    }
    history_path.parent.mkdir(parents=True, exist_ok=True)
    with history_path.open("a") as f:
        f.write(json.dumps(entry) + "\n")
    return entry


def read_tail(history_path: Path, n: int = 5) -> list[dict]:
    """Return the most recent `n` entries, oldest-first.

    Missing file → []. Malformed lines are silently dropped (history is
    append-only, but a half-written line on crash shouldn't break reads).
    """
    if not history_path.exists():
        return []
    lines = history_path.read_text().splitlines()
    entries: list[dict] = []
    for line in lines[-n:]:
        line = line.strip()
        if not line:
            continue
        try:
            entries.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return entries


def print_tail(history_path: Path, n: int = 5) -> None:
    """Render the last `n` history entries as a compact table to stdout."""
    entries = read_tail(history_path, n)
    if not entries:
        return

    print(f"\nlast {len(entries)} run(s):")
    print(f"  {'timestamp':<17}  {'mean':>5}  {'pass':>5}  note")
    print(f"  {'-' * 17}  {'-' * 5}  {'-' * 5}  {'-' * 40}")
    for e in entries:
        mean = f"{e['mean']:.2f}"
        passing = f"{e['pass_count']}/{e['total']}"
        note = (e.get("note") or "")[:40]
        print(f"  {e['timestamp']:<17}  {mean:>5}  {passing:>5}  {note}")
