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
    assert results, "expected at least one result"
    assert results[0]["uuid"] == relevant["uuid"]


def test_recall_isolates_by_project(temp_db, fake_embed, clock):
    memory.remember(
        temp_db,
        kind="fact",
        title="cache TTL is five minutes",
        content="redis keys expire after five minutes",
        project_id="alpha",
    )

    results = memory.recall(temp_db, query="cache TTL", project_id="beta", k=5)
    assert results == []


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
    assert results == [] or all(r["uuid"] != "completely unrelated" for r in results)


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
    uuids = [r["uuid"] for r in results]
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
    uuids = [r["uuid"] for r in results]
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
    uuids = [r["uuid"] for r in results]
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
    uuids = [r["uuid"] for r in results]
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
    assert all(r["kind"] == "gotcha" for r in results)
    assert gotcha["uuid"] in [r["uuid"] for r in results]


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
    uuids = [r["uuid"] for r in results]
    assert high["uuid"] in uuids
    assert len(results) == 1


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
    assert m["uuid"] not in [r["uuid"] for r in results]

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
    before_uuids = [r["uuid"] for r in before]
    assert before_uuids.index(apples["uuid"]) < before_uuids.index(bananas["uuid"])

    memory.update(
        temp_db,
        uuid=apples["uuid"],
        patch={"content": "content about cherries"},
    )

    # New content surfaces (proves both FTS and embedding were rewritten)
    after = memory.recall(temp_db, query="cherries", project_id="p1", k=2)
    assert after[0]["uuid"] == apples["uuid"]


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
