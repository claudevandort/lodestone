import pytest

from lodestone_mcp import memory


# ---- core CRUD ----

def test_remember_and_get_round_trip(temp_db, fake_embed, clock):
    result = memory.remember(
        temp_db,
        kind="decision",
        title="Use Postgres for primary store",
        content="Chose Postgres over MySQL for richer JSON support.",
        tags=["db", "architecture"],
        project_id="p1",
    )
    assert "uuid" in result and "id" in result

    fetched = memory.get(temp_db, uuid=result["uuid"])
    assert fetched["title"] == "Use Postgres for primary store"
    assert fetched["kind"] == "decision"
    assert sorted(fetched["tags"]) == ["architecture", "db"]
    assert fetched["confidence"] == 0.7  # default


def test_get_returns_none_for_missing(temp_db, fake_embed, clock):
    assert memory.get(temp_db, uuid="not-a-real-uuid") is None


def test_attempt_defaults_outcome_to_unknown(temp_db, fake_embed, clock):
    result = memory.remember(
        temp_db, kind="attempt", title="t", content="c", project_id="p1"
    )
    fetched = memory.get(temp_db, uuid=result["uuid"])
    assert fetched["outcome"] == "unknown"


def test_invalid_kind_raises(temp_db, fake_embed, clock):
    with pytest.raises(ValueError, match="invalid kind"):
        memory.remember(
            temp_db, kind="bogus", title="t", content="c", project_id="p1"
        )


def test_invalid_outcome_raises(temp_db, fake_embed, clock):
    with pytest.raises(ValueError, match="invalid outcome"):
        memory.remember(
            temp_db,
            kind="attempt",
            title="t",
            content="c",
            outcome="kinda",
            project_id="p1",
        )


# ---- confidence coercion (forgives string slips from auto-memory mapping) ----

def test_confidence_word_high_is_coerced(temp_db, fake_embed, clock):
    """Observed in dogfood: Claude sometimes passes 'high' instead of a float."""
    result = memory.remember(
        temp_db, kind="fact", title="t", content="c",
        confidence="high", project_id="p1",
    )
    fetched = memory.get(temp_db, uuid=result["uuid"])
    assert fetched["confidence"] == 0.9


def test_confidence_numeric_string_is_coerced(temp_db, fake_embed, clock):
    result = memory.remember(
        temp_db, kind="fact", title="t", content="c",
        confidence="0.85", project_id="p1",
    )
    fetched = memory.get(temp_db, uuid=result["uuid"])
    assert fetched["confidence"] == 0.85


def test_confidence_unknown_word_raises(temp_db, fake_embed, clock):
    with pytest.raises(ValueError, match="confidence must be"):
        memory.remember(
            temp_db, kind="fact", title="t", content="c",
            confidence="extremely-confident", project_id="p1",
        )


# ---- source_file + kind in patch (Feature 1 plumbing in memory.py) ----

def test_remember_persists_source_file(temp_db, fake_embed, clock):
    result = memory.remember(
        temp_db, kind="fact", title="t", content="c",
        project_id="p1",
        source_file="/abs/path/to/file.md",
    )
    row = temp_db.execute(
        "SELECT source_file FROM memories WHERE uuid = ?", (result["uuid"],),
    ).fetchone()
    assert row["source_file"] == "/abs/path/to/file.md"


def test_update_can_change_kind(temp_db, fake_embed, clock):
    """Required for the hook: when an auto-memory file's `type` changes,
    we update the lodestone row's `kind`."""
    m = memory.remember(
        temp_db, kind="fact", title="t", content="c", project_id="p1",
    )
    memory.update(temp_db, uuid=m["uuid"], patch={"kind": "preference"})
    fetched = memory.get(temp_db, uuid=m["uuid"])
    assert fetched["kind"] == "preference"


def test_update_rejects_invalid_kind_in_patch(temp_db, fake_embed, clock):
    m = memory.remember(
        temp_db, kind="fact", title="t", content="c", project_id="p1",
    )
    with pytest.raises(ValueError, match="invalid kind"):
        memory.update(temp_db, uuid=m["uuid"], patch={"kind": "bogus"})


def test_remember_rejects_link_with_target_uuid_field_name(temp_db, fake_embed, clock):
    """Regression: curator subagent built links as {target_uuid, type, ...}
    instead of {to_uuid, kind, ...} and they were silently dropped. Now
    that's a loud ValueError instead of a silent no-op."""
    other = memory.remember(
        temp_db, kind="fact", title="other", content="o", project_id="p1",
    )
    with pytest.raises(ValueError, match="to_uuid"):
        memory.remember(
            temp_db, kind="fact", title="t", content="c", project_id="p1",
            links=[{"target_uuid": other["uuid"], "type": "related"}],
        )


def test_remember_rejects_link_with_type_instead_of_kind(temp_db, fake_embed, clock):
    other = memory.remember(
        temp_db, kind="fact", title="other", content="o", project_id="p1",
    )
    with pytest.raises(ValueError, match="kind"):
        memory.remember(
            temp_db, kind="fact", title="t", content="c", project_id="p1",
            links=[{"to_uuid": other["uuid"], "type": "related"}],
        )


def test_remember_rejects_link_with_invalid_kind(temp_db, fake_embed, clock):
    other = memory.remember(
        temp_db, kind="fact", title="other", content="o", project_id="p1",
    )
    with pytest.raises(ValueError, match="invalid kind"):
        memory.remember(
            temp_db, kind="fact", title="t", content="c", project_id="p1",
            links=[{"to_uuid": other["uuid"], "kind": "totally-bogus"}],
        )


def test_remember_accepts_well_formed_link(temp_db, fake_embed, clock):
    """Sanity: the validation doesn't reject legitimate links."""
    a = memory.remember(
        temp_db, kind="fact", title="a", content="a content", project_id="p1",
    )
    b = memory.remember(
        temp_db, kind="fact", title="b", content="b content", project_id="p1",
        links=[{"to_uuid": a["uuid"], "kind": "related"}],
    )
    fetched = memory.get(temp_db, uuid=b["uuid"])
    assert any(
        l["kind"] == "related" and l["to_uuid"] == a["uuid"]
        for l in fetched["links"]
    )


def test_open_db_handles_old_db_without_migration_columns(tmp_path, fake_embed):
    """Regression: opening an existing DB whose `memories` table predates a
    migration-added column (e.g. source_file) must not crash on schema.sql's
    index re-creation. Caught when the plugin tried to open the user's
    existing ~/.lodestone/memory.db post-Feature-1.
    """
    from lodestone_mcp.db import open_db
    db_path = tmp_path / "old.db"

    # Bootstrap a current-schema DB, then strip migration-added columns to
    # simulate a DB created before those migrations existed.
    conn = open_db(db_path)
    conn.execute("DROP INDEX IF EXISTS idx_mem_source_file")
    conn.execute("ALTER TABLE memories DROP COLUMN source_file")
    conn.execute("ALTER TABLE memories DROP COLUMN project_label")
    conn.commit()
    conn.close()

    # Re-open: must not raise. The bug was that schema.sql's
    # `CREATE INDEX ... source_file` ran BEFORE _migrate() added the column.
    conn = open_db(db_path)
    cols = {row[1] for row in conn.execute("PRAGMA table_info(memories)")}
    assert "source_file" in cols, "migration didn't restore source_file"
    assert "project_label" in cols, "migration didn't restore project_label"
    indexes = {
        row[0] for row in conn.execute(
            "SELECT name FROM sqlite_master "
            "WHERE type='index' AND tbl_name='memories'"
        )
    }
    assert "idx_mem_source_file" in indexes, "migration didn't restore the index"
    conn.close()


# ---- recall: relevance ----

def test_recall_surfaces_semantically_similar(temp_db, fake_embed, clock):
    relevant = memory.remember(
        temp_db,
        kind="gotcha",
        title="postgres connection pool exhaustion",
        content="pgbouncer needed because asyncpg connection limits hit",
        project_id="p1",
    )
    memory.remember(
        temp_db,
        kind="fact",
        title="frontend uses tailwind",
        content="settled on tailwind for utility-first styling",
        project_id="p1",
    )

    results = memory.recall(
        temp_db, query="postgres connection pool", project_id="p1", k=2
    )
    assert results["results"], "expected at least one result"
    assert results["results"][0]["uuid"] == relevant["uuid"]


def test_recall_isolates_by_project_when_local_has_results(temp_db, fake_embed, clock):
    """When local has matching memories, default recall stays project-scoped
    — no auto-fallback, no other-project memories surface."""
    memory.remember(
        temp_db, kind="fact",
        title="cache TTL is five minutes",
        content="redis keys expire after five minutes",
        project_id="alpha",
    )
    memory.remember(
        temp_db, kind="fact",
        title="cache TTL is ten seconds",
        content="redis keys expire after ten seconds",
        project_id="beta",
    )
    results = memory.recall(temp_db, query="cache TTL", project_id="beta", k=5)
    assert results["meta"]["fallback_to_other_projects"] is False
    assert results["meta"]["local_count"] >= 1
    assert all(r["cross_project"] is False for r in results["results"])


# ---- cross-project recall ----

def test_recall_with_include_other_projects_surfaces_them_tagged(temp_db, fake_embed, clock):
    """Opt-in cross-project mode: other projects' memories appear, marked."""
    other = memory.remember(
        temp_db,
        kind="fact",
        title="cache TTL is five minutes",
        content="redis keys expire after five minutes",
        project_id="alpha",
        project_label="alpha-project",
    )

    results = memory.recall(
        temp_db,
        query="cache TTL",
        project_id="beta",
        k=5,
        include_other_projects=True,
    )
    uuids = [r["uuid"] for r in results["results"]]
    assert other["uuid"] in uuids

    hit = next(r for r in results["results"] if r["uuid"] == other["uuid"])
    assert hit["cross_project"] is True
    assert hit["source_project"] == "alpha-project"


def test_recall_auto_retries_cross_project_when_local_empty(temp_db, fake_embed, clock):
    """When local recall returns zero hits, server auto-retries with
    cross-project enabled and signals via meta.fallback_to_other_projects."""
    other = memory.remember(
        temp_db, kind="fact",
        title="cache TTL is five minutes",
        content="redis keys expire after five minutes",
        project_id="alpha",
        project_label="alpha-project",
    )
    results = memory.recall(temp_db, query="cache TTL", project_id="beta", k=5)
    assert results["meta"]["fallback_to_other_projects"] is True
    assert results["meta"]["local_count"] == 0
    uuids = [r["uuid"] for r in results["results"]]
    assert other["uuid"] in uuids
    hit = next(r for r in results["results"] if r["uuid"] == other["uuid"])
    assert hit["cross_project"] is True
    assert hit["source_project"] == "alpha-project"


def test_recall_meta_no_fallback_on_explicit_include_other_projects(temp_db, fake_embed, clock):
    """Explicit include_other_projects=True is the caller's choice, NOT a
    fallback — meta should reflect that even when local was empty."""
    memory.remember(
        temp_db, kind="fact",
        title="cache TTL is five minutes",
        content="redis keys expire after five minutes",
        project_id="alpha",
        project_label="alpha-project",
    )
    results = memory.recall(
        temp_db, query="cache TTL", project_id="beta", k=5,
        include_other_projects=True,
    )
    assert results["meta"]["fallback_to_other_projects"] is False
    assert results["meta"]["local_count"] == 0
    assert results["meta"]["returned_count"] >= 1


def test_recall_meta_returned_count_matches_results_length(temp_db, fake_embed, clock):
    """meta.returned_count is the actual length of results (after top-k)."""
    for i in range(5):
        memory.remember(
            temp_db, kind="fact",
            title=f"alpha thing {i}",
            content=f"alpha thing {i} content",
            project_id="p1",
        )
    results = memory.recall(temp_db, query="alpha thing", project_id="p1", k=3)
    assert results["meta"]["returned_count"] == len(results["results"])
    assert results["meta"]["returned_count"] <= 3
    assert results["meta"]["fallback_to_other_projects"] is False
    assert results["meta"]["local_count"] >= 3


def test_cross_project_results_outranked_by_local_at_equal_match(temp_db, fake_embed, clock):
    """Same content in two projects: local memory ranks above cross-project
    one because the cross-project penalty is applied."""
    memory.remember(
        temp_db,
        kind="fact",
        title="connection pool starvation",
        content="external calls inside transactions starve the pool",
        project_id="alpha",
        project_label="alpha-project",
    )
    local = memory.remember(
        temp_db,
        kind="fact",
        title="connection pool starvation",
        content="external calls inside transactions starve the pool",
        project_id="beta",
    )

    results = memory.recall(
        temp_db,
        query="connection pool starvation external calls",
        project_id="beta",
        k=5,
        include_other_projects=True,
    )
    uuids = [r["uuid"] for r in results["results"]]
    assert local["uuid"] in uuids
    # local must outrank the cross-project copy
    local_idx = uuids.index(local["uuid"])
    cross = next(r for r in results["results"] if r["cross_project"])
    cross_idx = uuids.index(cross["uuid"])
    assert local_idx < cross_idx


def test_local_results_have_cross_project_false(temp_db, fake_embed, clock):
    m = memory.remember(
        temp_db,
        kind="fact",
        title="local memory",
        content="lives in this project only",
        project_id="p1",
    )
    results = memory.recall(temp_db, query="local memory", project_id="p1", k=5)
    hit = next(r for r in results["results"] if r["uuid"] == m["uuid"])
    assert hit["cross_project"] is False
    assert hit["source_project"] is None


def test_recall_returns_empty_when_no_candidates(temp_db, fake_embed, clock):
    memory.remember(
        temp_db,
        kind="fact",
        title="completely unrelated",
        content="zzzzz qqqqq",
        project_id="p1",
    )
    results = memory.recall(
        temp_db, query="something entirely different banana", project_id="p1", k=5
    )
    # No vector neighbors with overlap, no FTS match → empty
    assert results["results"] == [] or all(r["uuid"] != "completely unrelated" for r in results["results"])


# ---- recall: ranking ----

def test_supersede_penalty_lowers_rank(temp_db, fake_embed, clock):
    old = memory.remember(
        temp_db,
        kind="decision",
        title="use redis for caching",
        content="redis chosen for the caching layer",
        project_id="p1",
    )
    new = memory.remember(
        temp_db,
        kind="decision",
        title="use redis for caching",
        content="redis chosen for the caching layer",
        project_id="p1",
    )
    memory.update(temp_db, uuid=old["uuid"], supersede_with=new["uuid"])

    # Default: superseded is excluded
    results = memory.recall(
        temp_db, query="redis caching", project_id="p1", k=5
    )
    uuids = [r["uuid"] for r in results["results"]]
    assert old["uuid"] not in uuids
    assert new["uuid"] in uuids

    # Opt-in: superseded visible but ranks below
    results = memory.recall(
        temp_db,
        query="redis caching",
        project_id="p1",
        k=5,
        filters={"include_superseded": True},
    )
    uuids = [r["uuid"] for r in results["results"]]
    assert new["uuid"] in uuids and old["uuid"] in uuids
    assert uuids.index(new["uuid"]) < uuids.index(old["uuid"])


def test_recency_decay_lowers_rank(temp_db, fake_embed, clock):
    clock.set(1_000_000_000)
    old = memory.remember(
        temp_db,
        kind="fact",
        title="kafka broker is single node",
        content="single node kafka in dev environment",
        project_id="p1",
    )
    clock.set(1_700_000_000)  # ~22 years later, well past several half-lives
    new = memory.remember(
        temp_db,
        kind="fact",
        title="kafka broker is single node",
        content="single node kafka in dev environment",
        project_id="p1",
    )

    results = memory.recall(
        temp_db, query="kafka broker single node", project_id="p1", k=5
    )
    uuids = [r["uuid"] for r in results["results"]]
    assert uuids.index(new["uuid"]) < uuids.index(old["uuid"])


def test_confidence_multiplier(temp_db, fake_embed, clock):
    high = memory.remember(
        temp_db,
        kind="fact",
        title="auth uses jwt",
        content="auth uses jwt with one hour expiry",
        confidence=0.9,
        project_id="p1",
    )
    low = memory.remember(
        temp_db,
        kind="fact",
        title="auth uses jwt",
        content="auth uses jwt with one hour expiry",
        confidence=0.1,
        project_id="p1",
    )

    results = memory.recall(
        temp_db, query="auth jwt expiry", project_id="p1", k=5
    )
    uuids = [r["uuid"] for r in results["results"]]
    assert uuids.index(high["uuid"]) < uuids.index(low["uuid"])


# ---- recall: filters ----

def test_filter_by_kind(temp_db, fake_embed, clock):
    gotcha = memory.remember(
        temp_db,
        kind="gotcha",
        title="circular import in services",
        content="circular import broke services package on cold start",
        project_id="p1",
    )
    memory.remember(
        temp_db,
        kind="fact",
        title="circular reasoning is a fallacy",
        content="logic class fact about circular argumentation",
        project_id="p1",
    )

    results = memory.recall(
        temp_db,
        query="circular",
        project_id="p1",
        k=5,
        filters={"kind": ["gotcha"]},
    )
    assert all(r["kind"] == "gotcha" for r in results["results"])
    assert gotcha["uuid"] in [r["uuid"] for r in results["results"]]


def test_filter_by_min_confidence(temp_db, fake_embed, clock):
    high = memory.remember(
        temp_db,
        kind="fact",
        title="rate limit",
        content="api rate limit set to one hundred requests per minute",
        confidence=0.9,
        project_id="p1",
    )
    memory.remember(
        temp_db,
        kind="fact",
        title="rate limit",
        content="api rate limit set to one hundred requests per minute",
        confidence=0.3,
        project_id="p1",
    )

    results = memory.recall(
        temp_db,
        query="api rate limit",
        project_id="p1",
        k=5,
        filters={"min_confidence": 0.5},
    )
    uuids = [r["uuid"] for r in results["results"]]
    assert high["uuid"] in uuids
    assert len(results["results"]) == 1


# ---- update / forget ----

def test_forget_soft_deletes_and_stores_reason(temp_db, fake_embed, clock):
    m = memory.remember(
        temp_db,
        kind="fact",
        title="something to forget",
        content="ephemeral information here",
        project_id="p1",
    )

    assert memory.forget(temp_db, uuid=m["uuid"], reason="no longer relevant") is True
    assert memory.get(temp_db, uuid=m["uuid"]) is None

    results = memory.recall(
        temp_db, query="something to forget ephemeral", project_id="p1", k=5
    )
    assert m["uuid"] not in [r["uuid"] for r in results["results"]]

    row = temp_db.execute(
        "SELECT context, deleted_at FROM memories WHERE uuid = ?", (m["uuid"],)
    ).fetchone()
    assert row["deleted_at"] is not None
    assert "no longer relevant" in row["context"]


def test_update_re_embeds_on_content_change(temp_db, fake_embed, clock):
    # Two competing memories so vector k-NN ranking is meaningful
    apples = memory.remember(
        temp_db,
        kind="fact",
        title="apples doc",
        content="content about apples",
        project_id="p1",
    )
    bananas = memory.remember(
        temp_db,
        kind="fact",
        title="bananas doc",
        content="content about bananas",
        project_id="p1",
    )

    before = memory.recall(temp_db, query="apples", project_id="p1", k=2)
    before_uuids = [r["uuid"] for r in before["results"]]
    assert before_uuids.index(apples["uuid"]) < before_uuids.index(bananas["uuid"])

    memory.update(
        temp_db,
        uuid=apples["uuid"],
        patch={"content": "content about cherries"},
    )

    # New content surfaces (proves both FTS and embedding were rewritten)
    after = memory.recall(temp_db, query="cherries", project_id="p1", k=2)
    assert after["results"][0]["uuid"] == apples["uuid"]


def test_update_verify_sets_verified_at(temp_db, fake_embed, clock):
    clock.set(1_000_000_000)
    m = memory.remember(
        temp_db, kind="fact", title="x", content="y", project_id="p1"
    )

    clock.set(1_700_000_000)
    memory.update(temp_db, uuid=m["uuid"], verify=True)

    fetched = memory.get(temp_db, uuid=m["uuid"])
    assert fetched["verified_at"] == 1_700_000_000


def test_links_supersedes_via_remember(temp_db, fake_embed, clock):
    old = memory.remember(
        temp_db, kind="decision", title="t", content="c", project_id="p1"
    )
    new = memory.remember(
        temp_db,
        kind="decision",
        title="t2",
        content="c2",
        project_id="p1",
        links=[{"to_uuid": old["uuid"], "kind": "supersedes"}],
    )

    new_id = temp_db.execute(
        "SELECT id FROM memories WHERE uuid = ?", (new["uuid"],)
    ).fetchone()["id"]
    old_row = temp_db.execute(
        "SELECT superseded_by FROM memories WHERE uuid = ?", (old["uuid"],)
    ).fetchone()
    assert old_row["superseded_by"] == new_id

    fetched_new = memory.get(temp_db, uuid=new["uuid"])
    link_kinds = {l["kind"]: l["to_uuid"] for l in fetched_new["links"]}
    assert link_kinds.get("supersedes") == old["uuid"]


def test_access_count_bumps_on_get_and_recall(temp_db, fake_embed, clock):
    m = memory.remember(
        temp_db,
        kind="fact",
        title="access counter test",
        content="access counter test content",
        project_id="p1",
    )

    assert memory.get(temp_db, uuid=m["uuid"])["access_count"] == 1
    assert memory.get(temp_db, uuid=m["uuid"])["access_count"] == 2

    memory.recall(
        temp_db, query="access counter test", project_id="p1", k=5
    )
    final = memory.get(temp_db, uuid=m["uuid"])
    # 2 prior gets + 1 recall + 1 final get = 4
    assert final["access_count"] == 4
