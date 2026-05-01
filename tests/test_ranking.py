"""Pure-function tests for the ranking module.

These don't need a DB or embeddings — they exercise the formula directly.
sqlite3.Row isn't required at runtime; any mapping that supports `row["key"]`
access works, so we use plain dicts.
"""
from __future__ import annotations

import pytest

from lodestone_mcp import ranking

NOW = 1_700_000_000  # arbitrary fixed epoch ~Nov 2023


def _row(*, id, confidence=1.0, verified_at=None, created_at=NOW, superseded_by=None):
    return {
        "id": id,
        "confidence": confidence,
        "verified_at": verified_at,
        "created_at": created_at,
        "superseded_by": superseded_by,
    }


# ---- fuse_rrf ----

def test_fuse_rrf_single_list_strictly_decreasing():
    scores = ranking.fuse_rrf([10, 20, 30])
    assert scores[10] > scores[20] > scores[30]
    assert scores[10] == pytest.approx(1 / (ranking.RRF_K + 1))


def test_fuse_rrf_overlap_boosts_score():
    """A memory present in both lists outranks one in only the top of one list."""
    scores = ranking.fuse_rrf([1, 2, 3], [3, 4, 5])
    assert scores[3] > scores[1]


def test_fuse_rrf_empty_inputs_yield_empty_dict():
    assert ranking.fuse_rrf() == {}
    assert ranking.fuse_rrf([], []) == {}


def test_fuse_rrf_higher_k_flattens_curve():
    sharp = ranking.fuse_rrf([1, 2], k=5)
    flat = ranking.fuse_rrf([1, 2], k=500)
    assert sharp[1] / sharp[2] > flat[1] / flat[2]


# ---- apply_postretrieval_factors ----

def test_recency_decay_lowers_score_for_older_rows():
    base = {1: 0.1, 2: 0.1}
    rows = [
        _row(id=1, created_at=NOW),
        _row(id=2, created_at=NOW - 365 * ranking.SECONDS_PER_DAY),
    ]
    ranked = dict((r["id"], s) for s, r in ranking.apply_postretrieval_factors(rows, base, NOW))
    assert ranked[1] > ranked[2]


def test_supersede_penalty_applied_exactly():
    """Superseded row's score equals base × confidence × multiplier × penalty."""
    base = {1: 0.1, 2: 0.1}
    rows = [
        _row(id=1, created_at=NOW),
        _row(id=2, created_at=NOW, superseded_by=999),
    ]
    ranked = dict((r["id"], s) for s, r in ranking.apply_postretrieval_factors(rows, base, NOW))
    assert ranked[2] == pytest.approx(ranked[1] * ranking.SUPERSEDE_PENALTY)


def test_confidence_scales_linearly():
    base = {1: 0.1, 2: 0.1}
    rows = [
        _row(id=1, confidence=1.0, created_at=NOW),
        _row(id=2, confidence=0.1, created_at=NOW),
    ]
    ranked = dict((r["id"], s) for s, r in ranking.apply_postretrieval_factors(rows, base, NOW))
    assert ranked[1] == pytest.approx(ranked[2] * 10)


def test_verified_at_resets_recency_anchor():
    """Old row that's been re-verified should rank above one that hasn't."""
    base = {1: 0.1, 2: 0.1}
    rows = [
        _row(id=1, created_at=NOW - 365 * ranking.SECONDS_PER_DAY),
        _row(id=2, created_at=NOW - 365 * ranking.SECONDS_PER_DAY, verified_at=NOW),
    ]
    ranked = dict((r["id"], s) for s, r in ranking.apply_postretrieval_factors(rows, base, NOW))
    assert ranked[2] > ranked[1]


def test_results_sorted_descending_by_score():
    base = {1: 0.1, 2: 0.1, 3: 0.1}
    rows = [
        _row(id=1, confidence=0.5),
        _row(id=2, confidence=0.9),
        _row(id=3, confidence=0.7),
    ]
    ranked = ranking.apply_postretrieval_factors(rows, base, NOW)
    scores = [score for score, _ in ranked]
    assert scores == sorted(scores, reverse=True)


def test_weights_sum_to_one():
    """Sanity check on the constants — they form a convex combination."""
    assert ranking.RECENCY_WEIGHT + ranking.BASELINE_WEIGHT == pytest.approx(1.0)


# ---- cross-project penalty ----

def _row_with_project(*, id, project_id, **kw):
    base = _row(id=id, **kw)
    base["project_id"] = project_id
    return base


def test_cross_project_penalty_applied_when_current_project_id_set():
    base = {1: 0.1, 2: 0.1}
    rows = [
        _row_with_project(id=1, project_id="local"),
        _row_with_project(id=2, project_id="other"),
    ]
    ranked = dict(
        (r["id"], s)
        for s, r in ranking.apply_postretrieval_factors(
            rows, base, NOW, current_project_id="local"
        )
    )
    assert ranked[2] == pytest.approx(ranked[1] * ranking.CROSS_PROJECT_PENALTY)


def test_no_cross_project_penalty_when_current_project_id_none():
    """Backward-compat: callers that don't pass current_project_id get the
    pre-existing behavior (no cross-project distinction)."""
    base = {1: 0.1, 2: 0.1}
    rows = [
        _row_with_project(id=1, project_id="local"),
        _row_with_project(id=2, project_id="other"),
    ]
    ranked = dict(
        (r["id"], s) for s, r in ranking.apply_postretrieval_factors(rows, base, NOW)
    )
    assert ranked[1] == ranked[2]


def test_cross_project_penalty_does_not_affect_same_project_rows():
    base = {1: 0.1}
    rows = [_row_with_project(id=1, project_id="local")]
    ranked = ranking.apply_postretrieval_factors(
        rows, base, NOW, current_project_id="local"
    )
    # Same project → no penalty; result equals the no-current-project baseline
    baseline = ranking.apply_postretrieval_factors(rows, base, NOW)
    assert ranked[0][0] == pytest.approx(baseline[0][0])
