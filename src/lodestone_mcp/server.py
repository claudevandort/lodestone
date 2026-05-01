from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from dotenv import load_dotenv
from mcp.server.fastmcp import Context, FastMCP

from . import db, memory

# Project .env (cwd) wins over global ~/.lodestone/.env, both lose to real env vars.
load_dotenv()
load_dotenv(Path.home() / ".lodestone" / ".env")


@asynccontextmanager
async def lifespan(_: FastMCP) -> AsyncIterator[dict[str, Any]]:
    """Open the DB once per server lifetime, close on shutdown.

    Tools reach the connection via ctx.request_context.lifespan_context.
    Letting FastMCP own the lifecycle keeps the connection out of module state,
    makes shutdown deterministic, and means tests can inject a different conn
    by overriding lifespan rather than monkey-patching a global.
    """
    conn = db.open_db()
    try:
        yield {"conn": conn}
    finally:
        conn.close()


def _conn(ctx: Context):
    return ctx.request_context.lifespan_context["conn"]


LODESTONE_INSTRUCTIONS = """\
Lodestone is shared, persistent memory across Claude sessions and human \
teammates working on this project. Treat it as the team's collective notebook — \
not as a chat scratchpad. Memories should "click" in future contexts that may \
look unrelated on the surface.

THE FORM OF A GOOD MEMORY: INSIGHTS, NOT TRANSCRIPTS
A good memory distills the transferable lesson, not the event. Compare:
- BAD:  "we tried SQLAlchemy and it failed"
- GOOD: "ORM swaps requiring a migration-history rewrite usually fail under \
heavy alembic+Django coupling — prefer the asyncio-bridge escape hatch"
The good version teaches a class of situation. Phrase content as "in situation \
X, approach Y has property Z" rather than "I did X."

Every memory must STAND ALONE semantically — no "as we discussed", no implicit \
context. Test: would this make sense to someone with zero knowledge of the \
conversation that produced it?

CALL `recall` PROACTIVELY AND BROADLY
- Before any non-trivial task, search not just for the task's literal terms \
but for ADJACENT CONCEPTS that might share the same shape. Working on auth? \
Also try "session management", "state machines", "expiring resources." The \
"click" — when an apparently unrelated insight applies — only happens if you \
probe broadly. Multiple cheap recalls beat one narrow miss.
- When the user proposes an approach that may have been tried before.
- When you encounter unfamiliar conventions, file structures, or unexplained \
constraints — there may be a memory explaining why.

CALL `remember` AS DELIBERATE SYNTHESIS, NOT REACTIVE CHECKBOX
Don't trigger `remember` mechanically on every event matching a kind. At \
natural reflection points (a decision made, an attempt resolved, a debugging \
revelation, a non-obvious user preference, the close of a substantial task), \
ask: "what is the durable lesson worth carrying out of this conversation?" \
Not every conversation produces one — that's fine. Quality of insight beats \
frequency of capture.

Use the kinds:
- decision: architecture, dependencies, tradeoffs ("chose X over Y because Z")
- attempt: things tried with their outcome (always set `outcome`); content \
should generalize the lesson, not just narrate
- gotcha: code that looks right but breaks, surprising library behavior, \
footguns — the conditions matter as much as the symptom
- preference: durable team/user style choices worth honoring later
- fact / question: rarer; use sparingly

DO NOT REMEMBER trivial edits, generic programming knowledge already in your \
training, info that belongs in code/docs/commits, or single-conversation \
transient context. Sparse high-signal beats dense noise.

Use `update_memory` or `supersede_with` rather than accumulating near-duplicates.
"""

mcp = FastMCP("lodestone", instructions=LODESTONE_INSTRUCTIONS, lifespan=lifespan)


@mcp.tool()
def remember(
    ctx: Context,
    kind: str,
    title: str,
    content: str,
    tags: list[str] | None = None,
    context: dict[str, Any] | None = None,
    outcome: str | None = None,
    confidence: float = 0.7,
    links: list[dict[str, str]] | None = None,
) -> dict:
    """Store an INSIGHT, not a transcript. The reader is a future Claude or
    teammate who has zero context from this conversation — they must be able
    to act on the memory cold.

    Phrase content as "in situation X, approach Y has property Z" rather than
    "I did X." Compare:
      BAD:  "we tried SQLAlchemy and it failed"
      GOOD: "ORM swaps requiring a migration-history rewrite usually fail
             under heavy alembic+Django coupling; prefer the asyncio-bridge
             escape hatch"
    The good version teaches a class of situation, not just one event.

    Call as deliberate synthesis at natural reflection points (decision made,
    attempt resolved, gotcha hit, preference expressed, end of substantial
    task) — not as a reactive checkbox. If a near-duplicate exists, prefer
    `update_memory` or `supersede_with` over creating a sibling.

    kind: attempt | decision | gotcha | preference | fact | question
    outcome (set for kind='attempt'): worked | failed | partial | unknown
    title: scannable, <=80 chars, encodes the LESSON not the event
    content: standalone insight; explains WHY and WHEN it applies
    tags: cross-cutting concerns for filtering (e.g. ["fts5","schema"])
    context: structured pins, e.g. {"files":[...],"commit_sha":"...","branch":"..."}
    confidence: 0..1 — lower for speculative, higher for verified
    links: [{to_uuid, kind: supersedes|related|contradicts|caused_by}]
    """
    return memory.remember(
        _conn(ctx),
        kind=kind,
        title=title,
        content=content,
        tags=tags,
        context=context,
        outcome=outcome,
        confidence=confidence,
        links=links,
    )


@mcp.tool()
def recall(
    ctx: Context,
    query: str,
    k: int = 5,
    filters: dict[str, Any] | None = None,
) -> list[dict]:
    """Search project memories. Call BEFORE tackling work — and BROADLY.

    The most valuable hits often come from probing ADJACENT CONCEPTS, not
    the literal task. Working on auth? Also try "session management",
    "expiring resources", "state machines." The "click" — when an apparently
    unrelated insight applies — only happens if you query beyond the surface.
    Multiple cheap recalls beat one narrow miss; if the first query returns
    nothing useful, try a different framing of the underlying problem.

    Hybrid retrieval (semantic + keyword), so paraphrased queries work —
    describe the underlying problem in natural language, not the syntax.

    Returns top-k re-ranked by recency × confidence × supersede penalty,
    with 1-hop link expansion (so superseded / related / contradicting
    memories surface together).

    filters: {kind, tags, outcome, min_confidence, since, include_superseded}
    """
    return memory.recall(_conn(ctx), query=query, k=k, filters=filters)


@mcp.tool()
def list_recent(
    ctx: Context,
    limit: int = 20,
    kind: list[str] | None = None,
    since: int | None = None,
) -> list[dict]:
    """Browse the most recent memories without a query. Use to get oriented
    at the start of a session, or to scan what was recorded after a recent
    work block. For targeted lookup by topic, prefer `recall`.
    """
    return memory.list_recent(_conn(ctx), limit=limit, kind=kind, since=since)


@mcp.tool()
def get_memory(ctx: Context, uuid: str, expand_links: bool = True) -> dict | None:
    """Fetch one memory by uuid (with linked memories expanded). Use after
    `recall` returns a uuid you want to inspect more deeply, or to follow a
    `superseded_by` / `related` link from another memory.
    """
    return memory.get(_conn(ctx), uuid=uuid, expand_links=expand_links)


@mcp.tool()
def update_memory(
    ctx: Context,
    uuid: str,
    patch: dict[str, Any] | None = None,
    verify: bool = False,
    supersede_with: str | None = None,
) -> dict | None:
    """Edit, verify, or supersede an existing memory. Prefer this over
    creating near-duplicates with `remember`.

    - `patch`: partial update — title, content, confidence, outcome, tags, context
    - `verify=True`: confirm a memory is still accurate (resets the recency
      decay anchor so it ranks higher in future recalls)
    - `supersede_with=<new_uuid>`: declare this memory replaced by a newer
      one — old stays retrievable with `include_superseded` but ranks low
    """
    return memory.update(
        _conn(ctx),
        uuid=uuid,
        patch=patch,
        verify=verify,
        supersede_with=supersede_with,
    )


@mcp.tool()
def forget(ctx: Context, uuid: str, reason: str | None = None) -> dict:
    """Soft-delete a memory (still in DB but excluded from recall). Use only
    when a memory is wrong or actively misleading. For outdated-but-still-
    historical info, prefer `update_memory` with `supersede_with` so the
    trail is preserved. Reason is stored in context.forget_reason.
    """
    ok = memory.forget(_conn(ctx), uuid=uuid, reason=reason)
    return {"ok": ok}


def main() -> None:
    mcp.run()


if __name__ == "__main__":
    main()
