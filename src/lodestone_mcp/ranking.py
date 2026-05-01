"""Pure ranking math for the recall pipeline.

Two stages:
  1. fuse_rrf — combine multiple ranked candidate lists into a unified score
     via reciprocal rank fusion.
  2. apply_postretrieval_factors — adjust the fused score by each memory's
     confidence, age (recency decay), and supersede status.

Kept separate from memory.py so the formula is self-contained, testable in
isolation, and easy to tune without touching SQL.
"""
from __future__ import annotations

import sqlite3
from typing import Mapping

# Reciprocal rank fusion smoothing constant (Cormack et al., 2009).
# Higher = flatter (top ranks dominate less); lower = sharper.
RRF_K = 60

# Half-life for recency decay; a memory this many days old contributes half
# the recency-driven boost it would when fresh.
HALF_LIFE_DAYS = 60

# How much of the final multiplier comes from recency vs. baseline.
# The two MUST sum to 1.0 — they form a convex combination.
RECENCY_WEIGHT = 0.3
BASELINE_WEIGHT = 1.0 - RECENCY_WEIGHT  # 0.7

# Multiplier applied to memories that have been superseded by another.
# Keeps them visible (when explicitly requested) but consistently outranked.
SUPERSEDE_PENALTY = 0.3

SECONDS_PER_DAY = 86400


def fuse_rrf(*ranked_id_lists: list[int], k: int = RRF_K) -> dict[int, float]:
    """Combine multiple ranked lists of memory IDs into a single score map.

    Each list contributes 1 / (k + rank + 1) per appearance, summed across
    lists. Memories appearing in multiple lists score higher.
    """
    scores: dict[int, float] = {}
    for ids in ranked_id_lists:
        for rank, mid in enumerate(ids):
            scores[mid] = scores.get(mid, 0.0) + 1 / (k + rank + 1)
    return scores


def apply_postretrieval_factors(
    rows: list[sqlite3.Row],
    base_scores: Mapping[int, float],
    now: int,
    *,
    half_life_days: int = HALF_LIFE_DAYS,
) -> list[tuple[float, sqlite3.Row]]:
    """Re-score rows with confidence, recency decay, and supersede penalty.

    Returns rows paired with their final score, sorted descending by score.
    Recency anchor = `verified_at` if set, else `created_at`.
    """
    out: list[tuple[float, sqlite3.Row]] = []
    for row in rows:
        base = base_scores[row["id"]]
        anchor = row["verified_at"] or row["created_at"]
        age_days = max(0, (now - anchor) / SECONDS_PER_DAY)
        recency = 0.5 ** (age_days / half_life_days)
        penalty = SUPERSEDE_PENALTY if row["superseded_by"] else 1.0

        final = base * row["confidence"] * (BASELINE_WEIGHT + RECENCY_WEIGHT * recency) * penalty
        out.append((final, row))

    out.sort(key=lambda pair: -pair[0])
    return out
