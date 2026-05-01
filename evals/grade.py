"""Score an eval run against each scenario's expectations.

Scoring rule schema (per scenario, under `expects`):
  must_call: [tool_name, ...]
      every named tool must appear in the trace
  must_not_call: [tool_name, ...]
      none of the named tools may appear
  tool_call_args_must_match: {tool_name: {arg_field: <rule>}}
      at least ONE call to <tool> must satisfy ALL its field rules
      <rule> shapes:
        {"any_of_substrings":  [...]}  case-insensitive substring match
        {"none_of_substrings": [...]}  case-insensitive — none may be present
        {"equals": value}              exact equality (good for enum fields)

Each scenario gets a binary score: PASS_SCORE (10) or FAIL_SCORE (0).

`grade(payload)` is pure — no I/O, returns a GradeReport.
`print_report(report)` formats a report to stdout.

Standalone exit codes:
  0 = mean == PASS_SCORE (every scenario passed)
  1 = mean  < PASS_SCORE (one or more failures)
  2 = no results found
"""
from __future__ import annotations

import json
import statistics
import sys
from dataclasses import dataclass
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
LATEST = REPO_ROOT / "evals" / "results" / "latest.json"

PASS_SCORE = 10
FAIL_SCORE = 0


# ---- public types ----

@dataclass
class CheckResult:
    name: str         # "must_call" | "must_not_call" | "args_match"
    passed: bool
    message: str


@dataclass
class ScenarioResult:
    id: str
    score: int        # PASS_SCORE or FAIL_SCORE
    passed: bool
    checks: list[CheckResult]
    error: str | None = None


@dataclass
class GradeReport:
    scenarios: list[ScenarioResult]
    pass_count: int
    total: int
    mean: float
    median: float
    modes: list[int]


# ---- scorers (each returns (passed, human-readable message)) ----

def scorer_must_call(trace: dict, expected: list[str]) -> tuple[bool, str]:
    if not expected:
        return True, "no required calls"
    called = {tc["name"] for tc in trace["tool_calls"]}
    missing = [t for t in expected if t not in called]
    if not missing:
        return True, f"called all of {expected}"
    return False, f"missing: {missing} (called: {sorted(called) or 'nothing'})"


def scorer_must_not_call(trace: dict, forbidden: list[str]) -> tuple[bool, str]:
    if not forbidden:
        return True, "no forbidden calls"
    called = {tc["name"] for tc in trace["tool_calls"]}
    bad = [t for t in forbidden if t in called]
    if not bad:
        return True, f"avoided all of {forbidden}"
    return False, f"called forbidden: {bad}"


def scorer_args_match(trace: dict, expected: dict) -> tuple[bool, str]:
    """For each tool, at least one call must satisfy ALL its field rules."""
    if not expected:
        return True, "no arg checks"

    msgs: list[str] = []
    overall_ok = True

    for tool_name, field_rules in expected.items():
        calls = [tc for tc in trace["tool_calls"] if tc["name"] == tool_name]
        if not calls:
            overall_ok = False
            msgs.append(f"{tool_name}: no calls captured")
            continue

        any_satisfying = False
        last_failures: list[str] = []
        for c in calls:
            failures = []
            for field_name, rule in field_rules.items():
                value = (c.get("input") or {}).get(field_name)
                ok, why = _check_rule(value, rule)
                if not ok:
                    failures.append(f"{field_name} {why}")
            if not failures:
                any_satisfying = True
                msgs.append(f"{tool_name}: one call satisfied all rules")
                break
            last_failures = failures

        if not any_satisfying:
            overall_ok = False
            msgs.append(f"{tool_name}: no call satisfied all rules; closest miss: {last_failures}")

    return overall_ok, "; ".join(msgs)


def _check_rule(value, rule: dict) -> tuple[bool, str]:
    if "any_of_substrings" in rule:
        substrings = rule["any_of_substrings"]
        if isinstance(value, str) and any(s.lower() in value.lower() for s in substrings):
            return True, f"matched one of {substrings}"
        return False, f"matched none of {substrings} (got {value!r})"

    if "none_of_substrings" in rule:
        substrings = [s for s in rule["none_of_substrings"] if s]
        # Non-string value vacuously passes — the field doesn't have a string
        # to contain anything. Use any_of_substrings if you want to require a value.
        if not isinstance(value, str):
            return True, "value is not a string; vacuously passed"
        present = [s for s in substrings if s.lower() in value.lower()]
        if present:
            return False, f"contained forbidden substring(s) {present}"
        return True, f"avoided all of {substrings}"

    if "equals" in rule:
        expected = rule["equals"]
        if value == expected:
            return True, f"== {expected!r}"
        return False, f"!= {expected!r} (got {value!r})"

    return False, f"unknown rule keys: {list(rule.keys())}"


# ---- grading (pure) ----

def grade(payload: dict) -> GradeReport:
    """Compute a GradeReport from a results payload. No I/O."""
    scenarios = {s["id"]: s for s in payload["scenarios"]}
    scenario_results: list[ScenarioResult] = []

    for r in payload["results"]:
        sid = r["id"]
        expects = scenarios.get(sid, {}).get("expects", {})
        trace = r["trace"]

        checks = _run_checks(trace, expects)
        all_passed = all(c.passed for c in checks) and not r.get("error")

        scenario_results.append(ScenarioResult(
            id=sid,
            score=PASS_SCORE if all_passed else FAIL_SCORE,
            passed=all_passed,
            checks=checks,
            error=r.get("error"),
        ))

    score_list = [s.score for s in scenario_results]
    if score_list:
        mean = statistics.mean(score_list)
        median = statistics.median(score_list)
        modes = sorted(statistics.multimode(score_list))
    else:
        mean = median = 0.0
        modes = []

    return GradeReport(
        scenarios=scenario_results,
        pass_count=sum(1 for s in scenario_results if s.passed),
        total=len(scenario_results),
        mean=mean,
        median=median,
        modes=modes,
    )


def _run_checks(trace: dict, expects: dict) -> list[CheckResult]:
    checks: list[CheckResult] = []
    if "must_call" in expects:
        ok, msg = scorer_must_call(trace, expects["must_call"])
        checks.append(CheckResult("must_call", ok, msg))
    if "must_not_call" in expects:
        ok, msg = scorer_must_not_call(trace, expects["must_not_call"])
        checks.append(CheckResult("must_not_call", ok, msg))
    if "tool_call_args_must_match" in expects:
        ok, msg = scorer_args_match(trace, expects["tool_call_args_must_match"])
        checks.append(CheckResult("args_match", ok, msg))
    return checks


# ---- presentation ----

def print_report(report: GradeReport) -> None:
    """Render a GradeReport to stdout for human consumption."""
    for sr in report.scenarios:
        marker = "PASS" if sr.passed else "FAIL"
        print(f"[{marker}] {sr.id}  (score: {sr.score}/10)")
        for c in sr.checks:
            sub_marker = "OK " if c.passed else "no "
            print(f"        [{sub_marker}] {c.name}: {c.message}")
        if sr.error:
            print(f"        [no ] error: {sr.error}")

    print()
    print("=== summary ===")
    print(f"  scenarios passed: {report.pass_count}/{report.total}")
    print(f"  mean   score: {report.mean:5.2f} / 10")
    print(f"  median score: {report.median:5.1f} / 10")
    print(f"  mode(s):      {report.modes}")


def main() -> None:
    if not LATEST.exists():
        print(f"no results at {LATEST}; run evals/run.py first", file=sys.stderr)
        sys.exit(2)
    payload = json.loads(LATEST.read_text())
    report = grade(payload)
    print_report(report)
    sys.exit(0 if report.mean == PASS_SCORE else 1)


if __name__ == "__main__":
    main()
