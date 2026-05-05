from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import os

from dotenv import load_dotenv
from mcp.server.fastmcp import Context, FastMCP

from . import db, memory

# Defensive sanitize before dotenv: when running as a Claude Code plugin, the
# manifest passes `"VOYAGE_API_KEY": "${VOYAGE_API_KEY}"`. If the shell var is
# unset, that substitution leaves the literal `${VOYAGE_API_KEY}` string in the
# server's process env. `load_dotenv` (default override=False) then refuses to
# overwrite it, and Voyage rejects the literal as "Provided API key is invalid".
# Strip any obviously-broken value so the dotenv chain can fill from the .env
# files. A real Voyage key (`pa-...`) never starts with `${`.
_voyage_env = os.environ.get("VOYAGE_API_KEY", "")
if not _voyage_env or _voyage_env.startswith("${"):
    os.environ.pop("VOYAGE_API_KEY", None)

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


# NOTE: Claude Code truncates server `instructions` to 2048 characters. The
# previous 5312-char version got cut mid-sentence inside the recall procedure,
# silently dropping the entire `remember` discipline section — so plugin users
# saw Claude do the preflight but never recall or remember on its own. This
# rewrite preserves the load-bearing directives in <2000 chars. There's a
# regression test (test_lodestone_instructions_fit_in_mcp_cap) that fails any
# future edit pushing this past 2000 chars.
LODESTONE_INSTRUCTIONS = """\
Lodestone is shared, persistent memory across Claude sessions on this project.

PREFLIGHT (ONCE PER SESSION, BEFORE FIRST USER REPLY)
Lodestone tools are deferred. Load schemas first or the first call fails:

  ToolSearch(query="lodestone", max_results=10)

RECALL BEFORE EVERY TASK
Call `recall` as your FIRST move on any task with a choice, pattern, or \
problem class you may have hit before — which is MOST tasks. Skip only for \
purely mechanical edits. Recall is cheap; missing a relevant insight is not.

- Query literal task words AND adjacent concepts ("auth" → also "session \
management"). Multiple cheap recalls beat one narrow miss.
- On 0 local hits, server falls back cross-project, flagged via \
`meta.fallback_to_other_projects`. Cross-project rows carry `cross_project: \
true` and `source_project`.
- Recall again mid-task when an unfamiliar convention may have a memory \
explaining why.

CROSS-PROJECT RESULTS — STOP AND ASK
When `meta.next_action` is set OR any result has `cross_project: true`, you \
MUST NOT apply silently. Use `AskUserQuestion` (NOT plain prose) to confirm \
before applying — include the `source_project` in the question text. When \
capturing a memory that builds on a recalled one, populate \
`links: [{to_uuid, kind: "related"}]` — one entry per source.

REMEMBER AS DELIBERATE SYNTHESIS
At natural reflection points (decision made, attempt resolved, gotcha hit, \
durable preference expressed), ask: "what is the transferable lesson?" — and \
call `remember`. Quality beats frequency.

Phrase memories as INSIGHTS that STAND ALONE — no "as we discussed", no \
implicit context. Format: "in situation X, approach Y has property Z."

Kinds: decision | attempt (set outcome) | gotcha | preference | fact.

If you finish substantial work without capturing along the way, invoke the \
`lodestone-memory-curator` subagent for a wrap-up review.

DO NOT REMEMBER trivial edits, generic programming knowledge, info in \
code/docs/commits, or transient state. Prefer `update_memory` or \
`supersede_with` over near-duplicates.
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
    confidence: float | str = 0.7,
    links: list[dict[str, str]] | None = None,
) -> dict:
    """Store an INSIGHT, not a transcript. The reader is a future Claude or
    teammate who has zero context from this conversation — they must be able
    to act on the memory cold.

    This is the PRIMARY capture path. When the user says something
    insight-worthy or you reach a natural reflection point, call `remember`
    directly — it works in every session, gives you control over `kind` /
    `tags` / `confidence` / `links`, and is faster than writing files. The
    plugin's auto-memory mirroring hook is a separate safety net for
    auto-memory writes triggered by Claude Code's built-in flow; it does
    NOT mean you should skip calling `remember` here.

    Phrase content as "in situation X, approach Y has property Z" rather than
    "I did X." Compare:
      BAD:  "we tried SQLAlchemy and it failed"
      GOOD: "ORM swaps requiring a migration-history rewrite usually fail
             under heavy alembic+Django coupling; prefer the asyncio-bridge
             escape hatch"

    Call as deliberate synthesis at natural reflection points (decision made,
    attempt resolved, gotcha hit, preference expressed, end of substantial
    task) — not as a reactive checkbox. If a near-duplicate exists, prefer
    `update_memory` or `supersede_with` over creating a sibling.

    REQUIRED args: kind, title, content. If a call fails validation
    (e.g. missing title), FIX the args and retry — do not give up after one
    error, especially when mirroring multiple auto-memory files.

    kind:       attempt | decision | gotcha | preference | fact | question
    outcome:    set for kind='attempt' — worked | failed | partial | unknown
    title:      REQUIRED — scannable, <=80 chars, encodes the LESSON
    content:    REQUIRED — standalone insight; explains WHY and WHEN it applies
    tags:       cross-cutting filters, e.g. ["fts5", "schema"]
    context:    structured pins, e.g. {"files":[...], "commit_sha":"..."}
    confidence: number 0..1 (NOT a word). 0.95 = verified by test/source,
                0.7 = default, 0.3 = speculative. Strings "high"/"medium"/"low"
                are coerced as a fallback but a number is preferred.
    links:      Provenance/semantic edges as METADATA on this memory — one
                entry per source memory:
                  [{to_uuid, kind: supersedes|related|contradicts|caused_by}]
                When this insight builds on, adapts, or was informed by
                recalled memories (local OR cross-project), populate
                `related` PER source — one link per memory drawn from.
                Don't summarize multiple sources into a single link, don't
                skip linking when the provenance is real, and don't write a
                separate memory just to record a link — the link goes on
                whatever memory you're already writing. The graph is
                traversable only if the links are here.
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
    include_other_projects: bool = False,
) -> dict:
    """Search project memories. Call as your FIRST move on any task involving
    a choice, pattern, or familiar problem class — which is most tasks. Skip
    only for purely mechanical edits. Recall is not a ceremony; integrate
    findings into your approach before acting.

    AUTO CROSS-PROJECT FALLBACK: when the default search (local project only)
    returns ZERO results, the server automatically retries with cross-project
    enabled. The response signals this in `meta.fallback_to_other_projects:
    true` — apply the ASK-before-applying discipline below for those results.
    You only need to opt in manually when local returned SOME results that
    don't materially inform your next step (a judgment call recall can't
    make for you).

    Hybrid retrieval (semantic + keyword), so paraphrased queries work —
    describe the underlying problem in natural language, not the syntax.
    Multiple cheap recalls beat one narrow miss; probe ADJACENT CONCEPTS
    ("auth" → also try "session management", "state machines").

    CROSS-PROJECT MODE: set `include_other_projects: true` to widen the
    search even when local has hits. Results from other projects come back
    with `cross_project: true` and `source_project: <label>`. They are
    reranked with a penalty so same-project results outrank them at similar
    raw match quality — a cross-project hit means the match was strong
    enough to overcome the penalty, so it deserves a closer look.

    Whenever results contain cross-project memories (whether via fallback or
    explicit opt-in), ALWAYS surface their origin to the user and ASK before
    applying ("Memory from <source_project> suggests X. Worth using here?").
    Don't merge cross-project insights into your answer as if they were
    established for the current project — they're suggestions, not local
    conventions.

    Returns:
      {
        "results": [<memory dict>, ...],   # top-k after rerank
        "meta": {
          "fallback_to_other_projects": bool,  # True when auto-retry fired
          "local_count":    int,               # rows matching THIS project
          "returned_count": int                # len(results)
        }
      }

    Each memory dict carries the usual fields plus `cross_project: bool` and
    `source_project: str | None`. Reranking applies recency × confidence ×
    supersede × cross-project penalty, with 1-hop link expansion.

    filters: {kind, tags, outcome, min_confidence, since, include_superseded}
    """
    return memory.recall(
        _conn(ctx),
        query=query,
        k=k,
        filters=filters,
        include_other_projects=include_other_projects,
    )


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
