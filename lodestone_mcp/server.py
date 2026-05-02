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
teammates working on this project. Treat it as the team's collective notebook \
— not a chat scratchpad. Memories should "click" in future contexts that may \
look unrelated on the surface.

THE FORM OF A GOOD MEMORY: INSIGHTS, NOT TRANSCRIPTS
A good memory distills the transferable lesson, not the event. Compare:
- BAD:  "we tried SQLAlchemy and it failed"
- GOOD: "ORM swaps requiring a migration-history rewrite usually fail under \
heavy alembic+Django coupling — prefer the asyncio-bridge escape hatch"
Phrase content as "in situation X, approach Y has property Z" rather than \
"I/we did X." Every memory must STAND ALONE — no "as we discussed", no \
implicit context. Test: would this make sense to someone with zero knowledge \
of the conversation that produced it?

RECALL BEFORE EVERY TASK
Treat `recall` as your FIRST move on any task involving a choice, pattern, or \
problem class you might have hit before — which is MOST tasks. Skip only for \
purely mechanical edits. Recall is cheap; missing a relevant insight is not.

Procedure:

1. Query with the literal task vocabulary AND adjacent concepts ("auth" → \
also try "session management", "state machines", "expiring resources"). The \
"click" — when an apparently unrelated insight applies — only happens if you \
probe beyond the surface. Multiple cheap recalls beat one narrow miss.

2. The server auto-retries with cross-project when local returns ZERO results \
and signals it via `meta.fallback_to_other_projects`. Manually opt in \
(`include_other_projects: true`) only when local returned SOME results that \
don't materially inform your next step.

3. Integrate findings before acting — recall is not a ceremony. Local \
memories: apply directly, briefly mention you found prior context. \
Cross-project memories (`cross_project: true`): follow the ASK-before-applying \
protocol below.

Mid-task, recall again whenever an unfamiliar convention, file structure, or \
unexplained constraint may have a memory explaining why, or when the user \
proposes an approach that may have been tried before.

CALL `remember` AS DELIBERATE SYNTHESIS
At natural reflection points (decision made, attempt resolved, debugging \
revelation, durable user preference), ask: "what is the durable lesson worth \
carrying out of this conversation?" Quality of insight beats frequency of \
capture; not every moment produces one.

If you finish substantial work without capturing along the way (deep in build \
mode, no natural pause to reflect), invoke the `lodestone-curator` subagent \
via `Task(subagent_type="lodestone-curator", ...)` for a wrap-up review. The \
curator runs in its own context, free of build-pressure.

Auto-memory note: when the lodestone plugin is installed, a PostToolUse hook \
mirrors auto-memory writes (`~/.claude/projects/.../memory/*.md`) into \
lodestone — but `remember` is still the primary capture path (faster, \
controls more fields, works without the plugin).

CROSS-PROJECT RECALL — APPLY WITH CARE
When results contain memories with `cross_project: true` (whether from \
auto-fallback or explicit opt-in):

1. NEVER apply silently — the other project's stack/context may differ.
2. Explicitly mention `source_project` in your reply.
3. ASK the user before applying. Example: "Memory from <source_project> \
suggests X — worth applying here?"
4. When you write a current-project memory that builds on or adapts a \
recalled one (local or cross-project), populate `links` with ONE `related` \
entry PER source memory drawn from — not a summary link, not zero. Links \
are metadata on the memory you're already writing; don't create extra \
memories just for provenance.

For wholesale inheritance (adopted multiple patterns from another project, \
no project-specific insight yet to capture), write ONE \
"inherited <patterns> from <source_project>" memory with one `related` link \
per adopted source.

Use the kinds:
- decision: architecture, dependencies, tradeoffs ("chose X over Y because Z")
- attempt: things tried with their outcome (always set `outcome`); content \
generalizes, not narrates
- gotcha: code that looks right but breaks, surprising library behavior, \
footguns
- preference: durable team/user style choices
- fact / question: rarer; use sparingly

DO NOT REMEMBER trivial edits, generic programming knowledge already in your \
training, info that belongs in code/docs/commits, or single-conversation \
transient context. Use `update_memory` or `supersede_with` rather than \
accumulating near-duplicates.
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
