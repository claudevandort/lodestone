import json
import sqlite3
import struct
import time
import uuid as uuidlib
from typing import Any

from . import embeddings, ranking
from .project import derive_project_id

VALID_KINDS = {"attempt", "decision", "gotcha", "preference", "fact", "question"}
VALID_OUTCOMES = {"worked", "failed", "partial", "unknown"}
VALID_LINK_KINDS = {"supersedes", "related", "contradicts", "caused_by"}

# Natural-language confidence terms callers sometimes pass instead of a float.
# Coerced to numeric so a string slip ("high") is forgiving rather than fatal —
# observed in practice when Claude maps from auto-memory's loose vocabulary.
_CONFIDENCE_WORDS = {"high": 0.9, "medium": 0.5, "med": 0.5, "low": 0.2}


def _coerce_confidence(value) -> float:
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value)
        except ValueError:
            mapped = _CONFIDENCE_WORDS.get(value.strip().lower())
            if mapped is not None:
                return mapped
    raise ValueError(
        f"confidence must be a number 0..1 or one of "
        f"{sorted(_CONFIDENCE_WORDS)}; got {value!r}"
    )


def _now() -> int:
    return int(time.time())


def _pack(vec: list[float]) -> bytes:
    return struct.pack(f"{len(vec)}f", *vec)


def remember(
    conn: sqlite3.Connection,
    *,
    kind: str,
    title: str,
    content: str,
    tags: list[str] | None = None,
    context: dict[str, Any] | None = None,
    outcome: str | None = None,
    confidence: float = 0.7,
    links: list[dict[str, str]] | None = None,
    project_id: str | None = None,
    project_label: str | None = None,
) -> dict:
    if kind not in VALID_KINDS:
        raise ValueError(f"invalid kind: {kind}")
    if kind == "attempt" and outcome is None:
        outcome = "unknown"
    if outcome is not None and outcome not in VALID_OUTCOMES:
        raise ValueError(f"invalid outcome: {outcome}")
    confidence = _coerce_confidence(confidence)

    if project_id is None:
        pid, derived_label = derive_project_id()
        plabel = project_label or derived_label
    else:
        pid = project_id
        plabel = project_label or project_id  # caller-passed id stands in for label

    uid = str(uuidlib.uuid4())
    now = _now()

    cur = conn.execute(
        """INSERT INTO memories
             (uuid, project_id, project_label, kind, title, content, outcome,
              confidence, context, created_at, updated_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            uid, pid, plabel, kind, title, content, outcome, confidence,
            json.dumps(context) if context else None, now, now,
        ),
    )
    mid = cur.lastrowid

    _set_tags(conn, mid, tags or [])
    _set_links(conn, mid, links or [])
    _set_embedding(conn, mid, title, content)

    conn.commit()
    return {"uuid": uid, "id": mid}


def recall(
    conn: sqlite3.Connection,
    *,
    query: str,
    k: int = 5,
    filters: dict[str, Any] | None = None,
    project_id: str | None = None,
    include_other_projects: bool = False,
) -> dict:
    """Hybrid semantic+FTS recall, scoped to the current project by default.

    When called with the default `include_other_projects=False` and the
    local-only result set is empty, automatically retries once with
    cross-project enabled. The response's `meta.fallback_to_other_projects`
    signals when this happened so callers can apply the
    ASK-before-applying discipline appropriate for cross-project results.

    Returns:
        {
            "results": [<memory dict>, ...],
            "meta": {
                "fallback_to_other_projects": bool,
                "local_count":     int,  # rows matching CURRENT project after filters
                "returned_count":  int,  # len(results) after rerank + top-k
            }
        }
    """
    pid = project_id or derive_project_id()[0]
    filters = filters or {}
    pool = max(k * 4, 20)

    vec_ids, fts_ids = _retrieve_candidates(conn, query, pool)
    base_scores = ranking.fuse_rrf(vec_ids, fts_ids)
    if not base_scores:
        return _empty_recall_response(local_count=0, fallback_used=False)

    # Always probe local first so we know the local_count (used both to
    # decide on the auto-retry and to surface in meta regardless of mode).
    local_rows = _fetch_filtered(
        conn, list(base_scores.keys()), pid, filters,
        include_other_projects=False,
    )
    local_count = len(local_rows)

    if include_other_projects:
        # Caller explicitly asked for cross-project; not a fallback.
        rows = _fetch_filtered(
            conn, list(base_scores.keys()), pid, filters,
            include_other_projects=True,
        )
        fallback_used = False
    elif local_count > 0:
        # Local has hits; use them.
        rows = local_rows
        fallback_used = False
    else:
        # Local empty — auto-retry across projects so the caller doesn't have
        # to re-issue the call. Signaled in meta.fallback_to_other_projects.
        rows = _fetch_filtered(
            conn, list(base_scores.keys()), pid, filters,
            include_other_projects=True,
        )
        fallback_used = True

    if not rows:
        return _empty_recall_response(local_count=local_count, fallback_used=fallback_used)

    ranked = ranking.apply_postretrieval_factors(
        rows, base_scores, _now(),
        current_project_id=pid,
    )[:k]

    if ranked:
        conn.executemany(
            "UPDATE memories SET access_count = access_count + 1 WHERE id = ?",
            [(r[1]["id"],) for r in ranked],
        )
        conn.commit()

    serialized = [
        _serialize(conn, row, score=score, expand_links=True, current_project_id=pid)
        for score, row in ranked
    ]
    return {
        "results": serialized,
        "meta": {
            "fallback_to_other_projects": fallback_used,
            "local_count": local_count,
            "returned_count": len(serialized),
        },
    }


def _empty_recall_response(*, local_count: int, fallback_used: bool) -> dict:
    return {
        "results": [],
        "meta": {
            "fallback_to_other_projects": fallback_used,
            "local_count": local_count,
            "returned_count": 0,
        },
    }


def _retrieve_candidates(
    conn: sqlite3.Connection, query: str, pool: int
) -> tuple[list[int], list[int]]:
    """Return (vector-ranked ids, FTS-ranked ids) for the query."""
    qvec = embeddings.embed(query, input_type="query")
    vec_ids = [
        r["memory_id"] for r in conn.execute(
            """SELECT memory_id FROM memory_vec
               WHERE embedding MATCH ? AND k = ?
               ORDER BY distance""",
            (_pack(qvec), pool),
        )
    ]
    fts_ids = [
        r["memory_id"] for r in conn.execute(
            """SELECT rowid AS memory_id FROM memory_fts
               WHERE memory_fts MATCH ?
               ORDER BY rank LIMIT ?""",
            (_fts_escape(query), pool),
        )
    ]
    return vec_ids, fts_ids


def list_recent(
    conn: sqlite3.Connection,
    *,
    limit: int = 20,
    kind: list[str] | None = None,
    since: int | None = None,
    project_id: str | None = None,
) -> list[dict]:
    pid = project_id or derive_project_id()[0]
    sql = "SELECT * FROM memories WHERE project_id = ? AND deleted_at IS NULL"
    params: list[Any] = [pid]
    if kind:
        kinds = kind if isinstance(kind, list) else [kind]
        sql += f" AND kind IN ({','.join('?' * len(kinds))})"
        params.extend(kinds)
    if since is not None:
        sql += " AND created_at >= ?"
        params.append(since)
    sql += " ORDER BY created_at DESC LIMIT ?"
    params.append(limit)
    rows = conn.execute(sql, params).fetchall()
    return [_serialize(conn, r) for r in rows]


def get(
    conn: sqlite3.Connection, *, uuid: str, expand_links: bool = True
) -> dict | None:
    cur = conn.execute(
        "UPDATE memories SET access_count = access_count + 1 "
        "WHERE uuid = ? AND deleted_at IS NULL",
        (uuid,),
    )
    conn.commit()
    if cur.rowcount == 0:
        return None
    row = conn.execute("SELECT * FROM memories WHERE uuid = ?", (uuid,)).fetchone()
    return _serialize(conn, row, expand_links=expand_links)


def update(
    conn: sqlite3.Connection,
    *,
    uuid: str,
    patch: dict[str, Any] | None = None,
    verify: bool = False,
    supersede_with: str | None = None,
) -> dict | None:
    row = conn.execute("SELECT * FROM memories WHERE uuid = ?", (uuid,)).fetchone()
    if not row:
        return None
    mid = row["id"]
    now = _now()

    if patch:
        cols: list[str] = []
        vals: list[Any] = []
        for col in ("title", "content", "confidence", "outcome"):
            if col in patch:
                cols.append(f"{col} = ?")
                vals.append(patch[col])
        if "context" in patch:
            cols.append("context = ?")
            vals.append(json.dumps(patch["context"]) if patch["context"] is not None else None)
        if cols:
            cols.append("updated_at = ?")
            vals.append(now)
            vals.append(mid)
            conn.execute(f"UPDATE memories SET {', '.join(cols)} WHERE id = ?", vals)

        if "tags" in patch:
            conn.execute("DELETE FROM memory_tags WHERE memory_id = ?", (mid,))
            _set_tags(conn, mid, patch["tags"] or [])

        if "title" in patch or "content" in patch:
            new_row = conn.execute(
                "SELECT title, content FROM memories WHERE id = ?", (mid,)
            ).fetchone()
            _set_embedding(conn, mid, new_row["title"], new_row["content"], replace=True)

    if verify:
        conn.execute("UPDATE memories SET verified_at = ? WHERE id = ?", (now, mid))

    if supersede_with:
        new_row = conn.execute(
            "SELECT id FROM memories WHERE uuid = ?", (supersede_with,)
        ).fetchone()
        if new_row:
            conn.execute(
                "UPDATE memories SET superseded_by = ? WHERE id = ?",
                (new_row["id"], mid),
            )
            conn.execute(
                "INSERT OR IGNORE INTO memory_links(from_id, to_id, kind) VALUES (?, ?, 'supersedes')",
                (new_row["id"], mid),
            )

    conn.commit()
    return get(conn, uuid=uuid, expand_links=False)


def forget(
    conn: sqlite3.Connection, *, uuid: str, reason: str | None = None
) -> bool:
    now = _now()
    cur = conn.execute(
        "UPDATE memories SET deleted_at = ?, updated_at = ? "
        "WHERE uuid = ? AND deleted_at IS NULL",
        (now, now, uuid),
    )
    if reason and cur.rowcount > 0:
        conn.execute(
            "UPDATE memories "
            "SET context = json_set(coalesce(context, '{}'), '$.forget_reason', ?) "
            "WHERE uuid = ?",
            (reason, uuid),
        )
    conn.commit()
    return cur.rowcount > 0


# ---- internals ----

def _set_tags(conn: sqlite3.Connection, mid: int, tags: list[str]) -> None:
    for tag in tags:
        conn.execute("INSERT OR IGNORE INTO tags(name) VALUES (?)", (tag,))
        conn.execute(
            "INSERT OR IGNORE INTO memory_tags(memory_id, tag_id) "
            "SELECT ?, id FROM tags WHERE name = ?",
            (mid, tag),
        )
    conn.execute(
        "UPDATE memory_fts SET tags = ? WHERE rowid = ?",
        (" ".join(tags), mid),
    )


def _set_links(
    conn: sqlite3.Connection, mid: int, links: list[dict[str, str]]
) -> None:
    for link in links:
        if link.get("kind") not in VALID_LINK_KINDS:
            continue
        target = conn.execute(
            "SELECT id FROM memories WHERE uuid = ?", (link["to_uuid"],)
        ).fetchone()
        if not target:
            continue
        conn.execute(
            "INSERT OR IGNORE INTO memory_links(from_id, to_id, kind) VALUES (?, ?, ?)",
            (mid, target["id"], link["kind"]),
        )
        if link["kind"] == "supersedes":
            conn.execute(
                "UPDATE memories SET superseded_by = ? WHERE id = ?",
                (mid, target["id"]),
            )


def _set_embedding(
    conn: sqlite3.Connection, mid: int, title: str, content: str, *, replace: bool = False
) -> None:
    vec = embeddings.embed(f"{title}\n\n{content}", input_type="document")
    if replace:
        conn.execute("DELETE FROM memory_vec WHERE memory_id = ?", (mid,))
    conn.execute(
        "INSERT INTO memory_vec(memory_id, embedding) VALUES (?, ?)",
        (mid, _pack(vec)),
    )


def _fetch_filtered(
    conn: sqlite3.Connection,
    ids: list[int],
    pid: str,
    filters: dict[str, Any],
    *,
    include_other_projects: bool = False,
) -> list[sqlite3.Row]:
    placeholders = ",".join("?" * len(ids))
    sql = (
        f"SELECT * FROM memories "
        f"WHERE id IN ({placeholders}) "
        f"  AND deleted_at IS NULL"
    )
    params: list[Any] = [*ids]

    if not include_other_projects:
        sql += " AND project_id = ?"
        params.append(pid)

    if filters.get("kind"):
        kinds = filters["kind"] if isinstance(filters["kind"], list) else [filters["kind"]]
        sql += f" AND kind IN ({','.join('?' * len(kinds))})"
        params.extend(kinds)
    if filters.get("outcome"):
        outs = filters["outcome"] if isinstance(filters["outcome"], list) else [filters["outcome"]]
        sql += f" AND outcome IN ({','.join('?' * len(outs))})"
        params.extend(outs)
    if filters.get("min_confidence") is not None:
        sql += " AND confidence >= ?"
        params.append(filters["min_confidence"])
    if filters.get("since") is not None:
        sql += " AND created_at >= ?"
        params.append(filters["since"])
    if not filters.get("include_superseded"):
        sql += " AND superseded_by IS NULL"

    return conn.execute(sql, params).fetchall()


def _serialize(
    conn: sqlite3.Connection,
    row: sqlite3.Row,
    *,
    score: float | None = None,
    expand_links: bool = False,
    current_project_id: str | None = None,
) -> dict:
    tags = [
        r["name"]
        for r in conn.execute(
            "SELECT t.name FROM tags t "
            "JOIN memory_tags mt ON mt.tag_id = t.id "
            "WHERE mt.memory_id = ? ORDER BY t.name",
            (row["id"],),
        )
    ]

    is_cross = (
        current_project_id is not None
        and row["project_id"] != current_project_id
    )

    out: dict[str, Any] = {
        "uuid": row["uuid"],
        "kind": row["kind"],
        "title": row["title"],
        "content": row["content"],
        "outcome": row["outcome"],
        "confidence": row["confidence"],
        "context": json.loads(row["context"]) if row["context"] else None,
        "tags": tags,
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
        "verified_at": row["verified_at"],
        "superseded_by_uuid": None,
        "access_count": row["access_count"],
        "cross_project": is_cross,
        "source_project": (row["project_label"] or row["project_id"]) if is_cross else None,
    }

    if row["superseded_by"]:
        sup = conn.execute(
            "SELECT uuid FROM memories WHERE id = ?", (row["superseded_by"],)
        ).fetchone()
        if sup:
            out["superseded_by_uuid"] = sup["uuid"]

    if score is not None:
        out["score"] = score

    if expand_links:
        out["links"] = [
            {"kind": r["kind"], "to_uuid": r["uuid"], "to_title": r["title"]}
            for r in conn.execute(
                "SELECT ml.kind, m.uuid, m.title FROM memory_links ml "
                "JOIN memories m ON m.id = ml.to_id "
                "WHERE ml.from_id = ? AND m.deleted_at IS NULL",
                (row["id"],),
            )
        ]

    return out


def _fts_escape(query: str) -> str:
    """FTS5 MATCH treats special characters; quote each token defensively."""
    tokens = [t for t in query.replace('"', " ").split() if t]
    return " ".join(f'"{t}"' for t in tokens) if tokens else query
