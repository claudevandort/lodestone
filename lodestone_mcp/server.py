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

RECALL BEFORE EVERY TASK
Treat `recall` as your FIRST move on any task involving a choice, a pattern, \
or a problem class you might have hit before — which is MOST tasks. Skip \
recall only for purely mechanical edits (typo fix, rename, formatting change \
with no design content). Recall is cheap; missing a relevant insight is not.

THE PROCEDURE — every task:

1. LOCAL RECALL FIRST. Query with the literal task vocabulary AND adjacent \
concepts. Working on auth? Also try "session management", "state machines", \
"expiring resources." The "click" — when an apparently unrelated insight \
applies — only happens if you probe beyond the surface. Multiple cheap \
recalls beat one narrow miss.

2. WIDEN TO CROSS-PROJECT WHEN NEEDED. The server automatically retries \
with `include_other_projects: true` whenever local recall returns ZERO \
results, and signals this in the response's \
`meta.fallback_to_other_projects` field. You only need to opt in MANUALLY \
when local returned SOME results that don't materially inform your next \
step (a judgment call). Either way, when results contain cross-project \
memories, follow the ASK-before-applying protocol in the CROSS-PROJECT \
RECALL section below.

3. INTEGRATE WHAT YOU FIND BEFORE ACTING:
- Local memories: apply directly. Briefly mention you found prior context so \
the user knows your reply is informed by it.
- Cross-project memories: surface their `source_project` explicitly and ASK \
before applying (see CROSS-PROJECT RECALL section for the full protocol).

4. PROCEED WITH IMPROVED CONTEXT. Recall is not a ceremony — the point is to \
start the task from a better baseline, not to query and then ignore. \
Additional triggers worth a recall call mid-task: an unfamiliar convention, \
file structure, or unexplained constraint may have a memory explaining why; \
the user proposing an approach that may have been tried before.

CALL `remember` AS DELIBERATE SYNTHESIS, NOT REACTIVE CHECKBOX
Don't trigger `remember` mechanically on every event matching a kind. At \
natural reflection points (a decision made, an attempt resolved, a debugging \
revelation, a non-obvious user preference, the close of a substantial task), \
ask: "what is the durable lesson worth carrying out of this conversation?" \
Not every conversation produces one — that's fine. Quality of insight beats \
frequency of capture.

AUTO-MEMORY MIRRORING (A SAFETY NET, NOT THE PRIMARY PATH)
Claude Code maintains its own per-session, file-based memory at \
~/.claude/projects/<project>/memory/, which is auto-loaded at session \
start for THIS project. When the lodestone plugin is installed, a \
PostToolUse hook mirrors auto-memory writes into lodestone's searchable \
index — so an insight captured to auto-memory also becomes findable \
across sessions and projects.

That's a SAFETY NET, not the primary capture path. Your primary path is \
`remember` directly: it's faster (no file IO), it lets you control fields \
auto-memory doesn't cover (`kind`, `tags`, `confidence`, `links`), and it \
works in every session — not just ones where the plugin is installed. \
When the user says something insight-worthy, call `remember` first; if \
you also write an auto-memory file for unrelated reasons (Claude Code's \
built-in flow), the hook keeps lodestone in sync without you needing to \
call `remember` again for the same content.

CROSS-PROJECT RECALL
By default, `recall` searches only THIS project's memories. You can opt in to \
searching across ALL projects this user has worked on by passing \
`include_other_projects: true`. Use it when:
- The current project is new and likely has no relevant local memories yet
- You're tackling a generalizable problem (architecture pattern, tooling \
choice, library gotcha, code convention) where insights from related \
projects often apply
- An initial same-project recall returned nothing useful AND the underlying \
problem isn't project-specific

Do NOT default-on cross-project recall — it adds noise for project-specific \
questions ("how does our auth work" should never reach into other projects).

When a cross-project recall surfaces something potentially relevant (each \
result will have `cross_project: true` and `source_project: <label>`):
1. NEVER apply it silently. The other project's stack, context, or \
constraints may differ from the current one.
2. Explicitly mention which project it came from in your reply.
3. ASK the user whether to apply it to the current task before doing so. \
Example: "Memory from <source_project> suggests X — worth applying here?"
4. ATTACH per-pattern `related` LINKS. When you write a current-project \
memory that builds on, adapts, or was informed by recalled memories (local \
or cross-project), populate the `links` array with ONE `related` entry PER \
source memory you drew from — not a single summary link, not zero links. \
Links live as METADATA on the memories you'd write anyway; you do NOT need \
to create extra memories just to record provenance.

For wholesale inheritance (you adopted multiple patterns from another \
project but have no project-specific insight to capture yet), write ONE \
"inherited <patterns> from <source_project>" memory and attach one \
`related` link per source memory adopted. Don't fan out into one memory per \
pattern — the links carry the granularity, not the memory count.

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
