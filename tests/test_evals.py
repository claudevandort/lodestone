"""Tests for the eval pipeline's pure functions.

Covers:
  - evals/grade.py: _check_rule, scorer_*, grade()
  - evals/run.py:   _parse_stream_json, extract_trace

`evals/` is on sys.path via pytest config (pyproject.toml pythonpath).
"""
from __future__ import annotations

import json
import pytest

from grade import (
    _check_rule,
    grade,
    scorer_args_match,
    scorer_must_call,
    scorer_must_not_call,
    PASS_SCORE,
    FAIL_SCORE,
)
from run import _parse_stream_json, extract_trace
import history


# ---- _check_rule ----

def test_check_rule_substring_hit_is_case_insensitive():
    ok, _ = _check_rule("Hello World", {"any_of_substrings": ["WORLD"]})
    assert ok


def test_check_rule_substring_miss():
    ok, _ = _check_rule("hello", {"any_of_substrings": ["foo", "bar"]})
    assert not ok


def test_check_rule_substring_rejects_non_string_value():
    """Numbers etc. don't substring-match — returns False rather than crashing."""
    ok, _ = _check_rule(42, {"any_of_substrings": ["42"]})
    assert not ok


def test_check_rule_equals():
    assert _check_rule("attempt", {"equals": "attempt"})[0]
    assert not _check_rule("attempt", {"equals": "decision"})[0]


def test_check_rule_unknown_keys_fails_explicitly():
    ok, msg = _check_rule("anything", {"weird_thing": "x"})
    assert not ok
    assert "unknown rule keys" in msg


def test_check_rule_none_of_substrings_passes_when_clean():
    ok, _ = _check_rule(
        "ORM swaps usually fail under heavy coupling",
        {"none_of_substrings": ["today", "we tried", "this morning"]},
    )
    assert ok


def test_check_rule_none_of_substrings_fails_on_violation():
    ok, msg = _check_rule(
        "Today we tried switching ORMs",
        {"none_of_substrings": ["today", "we tried"]},
    )
    assert not ok
    assert "today" in msg.lower()
    assert "we tried" in msg.lower()


def test_check_rule_none_of_substrings_vacuously_passes_for_non_string():
    """Missing/None field should not fail a 'must not contain' rule —
    use any_of_substrings if presence is required."""
    ok, _ = _check_rule(None, {"none_of_substrings": ["today"]})
    assert ok


# ---- scorers ----

def _trace(*calls):
    """Build a trace dict from (name, input_dict) tuples."""
    return {
        "tool_calls": [{"name": n, "input": i} for n, i in calls],
        "final_text": "",
    }


def test_scorer_must_call_passes_when_all_present():
    trace = _trace(("recall", {}), ("remember", {}))
    assert scorer_must_call(trace, ["recall", "remember"])[0]


def test_scorer_must_call_fails_with_missing_tool():
    trace = _trace(("recall", {}))
    ok, msg = scorer_must_call(trace, ["recall", "remember"])
    assert not ok
    assert "remember" in msg


def test_scorer_must_not_call_violation_lists_offender():
    trace = _trace(("recall", {}), ("remember", {}))
    ok, msg = scorer_must_not_call(trace, ["remember"])
    assert not ok
    assert "remember" in msg


def test_scorer_args_match_finds_satisfying_call_among_many():
    """Multiple calls — only one needs to satisfy all rules."""
    trace = _trace(
        ("recall", {"query": "wrong"}),
        ("recall", {"query": "the right sqlalchemy query"}),
    )
    expected = {"recall": {"query": {"any_of_substrings": ["sqlalchemy"]}}}
    assert scorer_args_match(trace, expected)[0]


def test_scorer_args_match_requires_all_rules_in_one_call():
    """Multi-field rules must be satisfied TOGETHER, not split across calls."""
    trace = _trace(
        ("remember", {"kind": "attempt", "outcome": "worked"}),
        ("remember", {"kind": "gotcha", "outcome": "failed"}),
    )
    # No single call has kind=attempt AND outcome=failed
    expected = {"remember": {
        "kind": {"equals": "attempt"},
        "outcome": {"equals": "failed"},
    }}
    assert not scorer_args_match(trace, expected)[0]


# ---- grade() ----

def _payload(**by_id):
    """Build a results payload from id → (expects, tool_calls)."""
    return {
        "scenarios": [{"id": sid, "expects": e} for sid, (e, _) in by_id.items()],
        "results": [
            {"id": sid, "trace": {"tool_calls": calls, "final_text": ""}}
            for sid, (_, calls) in by_id.items()
        ],
    }


def test_grade_full_pass_returns_pass_score():
    report = grade(_payload(t1=({"must_call": ["recall"]},
                                [{"name": "recall", "input": {}}])))
    assert report.scenarios[0].score == PASS_SCORE
    assert report.scenarios[0].passed
    assert report.mean == PASS_SCORE


def test_grade_full_fail_returns_fail_score():
    report = grade(_payload(t1=({"must_call": ["recall"]}, [])))
    assert report.scenarios[0].score == FAIL_SCORE
    assert not report.scenarios[0].passed
    assert report.mean == FAIL_SCORE


def test_grade_central_tendency_with_mixed_results():
    payload = _payload(
        a=({"must_call": ["x"]}, [{"name": "x", "input": {}}]),
        b=({"must_call": ["x"]}, [{"name": "x", "input": {}}]),
        c=({"must_call": ["x"]}, []),
    )
    report = grade(payload)
    assert report.pass_count == 2
    assert report.total == 3
    assert report.mean == pytest.approx(20 / 3)
    assert report.median == 10
    assert report.modes == [10]


def test_grade_propagates_run_errors_as_failure():
    payload = {
        "scenarios": [{"id": "t1", "expects": {"must_call": []}}],
        "results": [{
            "id": "t1",
            "trace": {"tool_calls": [], "final_text": ""},
            "error": "timeout",
        }],
    }
    report = grade(payload)
    assert not report.scenarios[0].passed
    assert report.scenarios[0].error == "timeout"


# ---- _parse_stream_json ----

def test_parse_stream_json_skips_blank_and_counts_failures():
    stdout = "\n".join([
        '{"type":"system"}',
        "",
        "not-json",
        '{"type":"result","result":"ok"}',
        "{ truncated",
    ])
    events, failures = _parse_stream_json(stdout)
    assert len(events) == 2
    assert failures == 2  # "not-json" and "{ truncated"


# ---- extract_trace ----

def test_extract_trace_captures_tool_use_blocks():
    events = [
        {"type": "system", "subtype": "init"},
        {"type": "assistant", "message": {"content": [
            {"type": "tool_use", "name": "recall", "input": {"query": "x"}}
        ]}},
    ]
    trace = extract_trace(events)
    assert trace["tool_calls"] == [{"name": "recall", "input": {"query": "x"}}]


def test_extract_trace_prefers_result_event_over_text_chunks():
    """When a `result` event arrives, it takes precedence over interim text."""
    events = [
        {"type": "assistant", "message": {"content": [
            {"type": "text", "text": "thinking..."}
        ]}},
        {"type": "result", "result": "the answer"},
    ]
    assert extract_trace(events)["final_text"] == "the answer"


def test_extract_trace_falls_back_to_concatenated_text():
    events = [
        {"type": "assistant", "message": {"content": [
            {"type": "text", "text": "hello "},
            {"type": "text", "text": "world"},
        ]}},
    ]
    assert extract_trace(events)["final_text"] == "hello world"


# ---- history ----

def _build_report(scores: dict[str, int]):
    """Construct a GradeReport directly (skip grade()) for history tests."""
    from grade import GradeReport, ScenarioResult
    scenarios = [
        ScenarioResult(id=sid, score=s, passed=(s == PASS_SCORE), checks=[])
        for sid, s in scores.items()
    ]
    score_list = list(scores.values())
    import statistics
    return GradeReport(
        scenarios=scenarios,
        pass_count=sum(1 for s in score_list if s == PASS_SCORE),
        total=len(score_list),
        mean=statistics.mean(score_list) if score_list else 0.0,
        median=statistics.median(score_list) if score_list else 0.0,
        modes=sorted(statistics.multimode(score_list)) if score_list else [],
    )


def test_history_append_then_read_round_trip(tmp_path):
    hist = tmp_path / "history.jsonl"
    report = _build_report({"a": 10, "b": 0})
    written = history.append(
        hist, report,
        results_file="results-X.json", timestamp="20260101T000000Z",
        note="first run",
    )
    entries = history.read_tail(hist)
    assert len(entries) == 1
    assert entries[0] == written
    assert entries[0]["per_scenario"] == {"a": 10, "b": 0}
    assert entries[0]["mean"] == 5
    assert entries[0]["note"] == "first run"


def test_history_read_tail_returns_at_most_n_oldest_first(tmp_path):
    hist = tmp_path / "history.jsonl"
    for i in range(7):
        history.append(
            hist, _build_report({"a": 10}),
            results_file=f"r{i}.json",
            timestamp=f"2026010{i}T000000Z",
        )
    tail = history.read_tail(hist, n=3)
    assert len(tail) == 3
    assert [e["timestamp"] for e in tail] == [
        "20260104T000000Z", "20260105T000000Z", "20260106T000000Z",
    ]


def test_history_read_tail_handles_missing_file(tmp_path):
    assert history.read_tail(tmp_path / "nope.jsonl") == []


def test_history_read_tail_skips_malformed_lines(tmp_path):
    hist = tmp_path / "history.jsonl"
    hist.write_text(
        '{"timestamp":"a","mean":10,"median":10,"modes":[10],'
        '"pass_count":1,"total":1,"per_scenario":{},"results_file":"x","note":null}\n'
        "this-is-not-json\n"
        '{"timestamp":"b","mean":0,"median":0,"modes":[0],'
        '"pass_count":0,"total":1,"per_scenario":{},"results_file":"y","note":null}\n'
    )
    entries = history.read_tail(hist)
    assert [e["timestamp"] for e in entries] == ["a", "b"]


def test_history_append_creates_parent_directory(tmp_path):
    hist = tmp_path / "deep" / "nested" / "history.jsonl"
    history.append(
        hist, _build_report({"a": 10}),
        results_file="r.json", timestamp="20260101T000000Z",
    )
    assert hist.exists()
