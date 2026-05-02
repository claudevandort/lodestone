"""Tests for server-side wiring: lifespan-state plumbing and auto-attach
of co_recalled_with links on remember after a recent recall.

The MCP @mcp.tool() decorator preserves callability, so we invoke the
decorated functions directly with a manually-constructed Context-like
object whose request_context.lifespan_context dict carries the same keys
the lifespan() context manager would yield.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from lodestone_mcp import db, server


# ---- fake context ----

class _FakeReqCtx:
    def __init__(self, lifespan_context):
        self.lifespan_context = lifespan_context


class _FakeCtx:
    def __init__(self, conn, recall_ledger=None):
        self.request_context = _FakeReqCtx({
            "conn": conn,
            "recall_ledger": [] if recall_ledger is None else recall_ledger,
        })


@pytest.fixture
def srv_ctx(tmp_path: Path, fake_embed):
    """Temp DB + empty ledger; conftest's fake_embed monkey-patches embeddings."""
    conn = db.open_db(tmp_path / "test.db")
    ctx = _FakeCtx(conn)
    yield ctx
    conn.close()


# ---- pure ledger helper ----

def test_build_co_recall_links_empty_ledger_returns_empty():
    assert server._build_co_recall_links_from_ledger([]) == []


def test_build_co_recall_links_uses_most_recent_recall_only():
    ledger = [
        (1.0, ["uuid-old-a", "uuid-old-b"]),
        (2.0, ["uuid-new-a", "uuid-new-b"]),
    ]
    links = server._build_co_recall_links_from_ledger(ledger)
    assert {l["to_uuid"] for l in links} == {"uuid-new-a", "uuid-new-b"}
    assert all(l["kind"] == "co_recalled_with" for l in links)


def test_build_co_recall_links_caps_at_max():
    many = [f"uuid-{i}" for i in range(20)]
    links = server._build_co_recall_links_from_ledger([(1.0, many)], max_links=3)
    assert len(links) == 3
    assert [l["to_uuid"] for l in links] == ["uuid-0", "uuid-1", "uuid-2"]


# ---- integration: recall → remember populates co_recalled_with ----

def test_recall_then_remember_auto_attaches_co_recalled_with(srv_ctx):
    a = server.remember(srv_ctx, kind="fact", title="alpha thing",
                        content="alpha thing content")
    b = server.remember(srv_ctx, kind="fact", title="beta thing",
                        content="beta thing content")
    server.recall(srv_ctx, query="alpha beta thing")

    new = server.remember(srv_ctx, kind="fact", title="gamma thing",
                          content="gamma thing content")

    fetched = server.get_memory(srv_ctx, uuid=new["uuid"])
    co_recalled = {l["to_uuid"] for l in fetched["links"]
                   if l["kind"] == "co_recalled_with"}
    assert a["uuid"] in co_recalled
    assert b["uuid"] in co_recalled


def test_remember_without_prior_recall_has_no_auto_links(srv_ctx):
    """No recall in the ledger → no co_recalled_with links."""
    new = server.remember(srv_ctx, kind="fact", title="lonely", content="alone")
    fetched = server.get_memory(srv_ctx, uuid=new["uuid"])
    assert all(l["kind"] != "co_recalled_with" for l in fetched["links"])


def test_explicit_related_link_coexists_with_auto_co_recalled(srv_ctx):
    """Manual `related` link to a uuid AND auto `co_recalled_with` to the same
    uuid should both be stored — they encode different semantics."""
    a = server.remember(srv_ctx, kind="fact", title="alpha thing",
                        content="alpha thing content")
    server.recall(srv_ctx, query="alpha thing")

    new = server.remember(
        srv_ctx, kind="fact", title="another thing",
        content="another thing content",
        links=[{"to_uuid": a["uuid"], "kind": "related"}],
    )

    fetched = server.get_memory(srv_ctx, uuid=new["uuid"])
    kinds_targeting_a = {l["kind"] for l in fetched["links"]
                         if l["to_uuid"] == a["uuid"]}
    assert "related" in kinds_targeting_a
    assert "co_recalled_with" in kinds_targeting_a


def test_recall_ledger_grows_then_caps(srv_ctx):
    """After many recalls, the ledger keeps only the last _RECALL_LEDGER_MAX entries."""
    server.remember(srv_ctx, kind="fact", title="seed", content="seed content")
    for i in range(server._RECALL_LEDGER_MAX + 5):
        server.recall(srv_ctx, query=f"seed query {i}")
    ledger = srv_ctx.request_context.lifespan_context["recall_ledger"]
    assert len(ledger) == server._RECALL_LEDGER_MAX
