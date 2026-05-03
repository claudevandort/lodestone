# Lodestone MCP — Design

A Model Context Protocol server that gives Claude Code persistent, project-aware
memory across sessions. Claude can record what was tried, what worked, what
didn't, and why — and recall those memories later via hybrid semantic + keyword
search so it doesn't repeat dead ends or forget hard-won decisions.

## Goals

- **Cross-session memory** scoped per project, retrievable by relevance not just recency.
- **Capture failure as well as success.** "We tried X and it failed because Y under
  conditions Z" is the highest-value memory and the easiest to lose.
- **Lightweight infrastructure.** Runs locally, single SQLite file, no daemon.
- **Schema-ready for team sharing** later (cross-teammate recall) without migration.

## Non-goals (v1)

- Team sync / multi-author replication.
- GraphRAG-style entity extraction and community summaries. Typed links between
  memories cover the high-value graph queries (supersedes, contradicts, related)
  without the indexing pipeline.
- A web UI. CLI + MCP tools only.

## Key decisions

| Decision | Choice | Why |
| --- | --- | --- |
| Embedding provider | **Voyage `voyage-code-3`** (1024 dim) | Best-in-class for code/technical content. API key dependency accepted. |
| Project ID derivation | **git remote URL hash, fallback to absolute path** | Remote URL is stable across clones and worktrees. Path fallback handles non-git dirs. |
| Storage location | **Single global DB** at `~/.lodestone/memory.db`, filtered by `project_id` | Trivial to add cross-project recall later. Simpler backup. |

## Storage layout

`~/.lodestone/memory.db` — SQLite with the `sqlite-vec` extension for ANN search
and FTS5 for keyword search. WAL mode. One row per memory, one vector per row,
typed links between memories.

See `src/lodestone_memory/schema.sql` for the canonical DDL.

### Tables

- `memories` — core record. Fields: `uuid`, `project_id`, `author_id`, `kind`,
  `title`, `content`, `outcome`, `confidence`, `context` (JSON), timestamps,
  `superseded_by`, `deleted_at`, `access_count`.
- `tags` + `memory_tags` — many-to-many tags.
- `memory_links` — typed edges: `supersedes`, `related`, `contradicts`, `caused_by`.
- `memory_vec` — `vec0` virtual table holding 1024-dim embeddings.
- `memory_fts` — FTS5 over title + content + tags, kept in sync via triggers.

### Memory kinds

| Kind | Use for | Notes |
| --- | --- | --- |
| `attempt` | "We tried X" | `outcome` required: worked / failed / partial / unknown |
| `decision` | "Chose X over Y because Z" | Pair with `context.commit_sha` if relevant |
| `gotcha` | "X looks right but breaks Y" | Highest-value at recall time |
| `preference` | User/team style preference | Often becomes a project rule |
| `fact` | Ambient context | Use sparingly — derivable facts belong in code |
| `question` | Open investigation thread | Treat as TODO with context |

### Staleness model

Memories carry `confidence` (0–1) and `verified_at`. Recall ranking applies an
exponential decay (60-day half-life) on the older of `verified_at` /
`created_at`, multiplied by `confidence`. Superseded memories are penalised but
still visible (set `include_superseded` to lift the penalty entirely).

## MCP tool surface

Six tools, deliberately small to keep prompt overhead low:

1. **`remember`** — write a memory. Embeds content via Voyage and inserts vector.
2. **`recall`** — hybrid search (vector + FTS) with reciprocal rank fusion,
   then re-ranked by recency × confidence × supersede penalty. Returns top-k
   with 1-hop link expansion (the GraphRAG-lite payoff).
3. **`list_recent`** — non-semantic browse for "what's been logged here lately."
4. **`get_memory`** — fetch one by uuid, with link expansion. Bumps `access_count`.
5. **`update_memory`** — single tool covers edit / verify / supersede to keep
   the surface small. Re-embeds when title or content changes.
6. **`forget`** — soft delete. Soft because "we already ruled that out" is
   itself useful signal.

Project ID is derived automatically from the MCP server's working directory; no
caller needs to pass it.

## Recall ranking

```
for each candidate (union of top-N vector + top-N FTS):
    rrf_score = sum(1 / (60 + rank_in_list))      # reciprocal rank fusion
    recency   = 0.5 ** (age_days / 60)            # 60-day half-life
    final     = rrf_score
              * confidence
              * (0.7 + 0.3 * recency)             # recency contributes 30%
              * (0.3 if superseded else 1.0)
```

RRF avoids having to normalise BM25 vs cosine distance — each list is just
ranked, fused by reciprocal rank.

## Future work (deferred)

- **Team sharing.** `author_id` is in the schema; replication is a separate
  protocol question. Likely: per-project SQLite with a sync layer (Litestream,
  Turso, or a simple central HTTP API).
- **GraphRAG.** If link density grows enough that 1-hop expansion isn't
  sufficient, revisit entity extraction over `content` to auto-create `related`
  edges.
- **Capture hooks.** A Claude Code hook that prompts the model to log
  `attempt`/`decision` memories at natural breakpoints (after a failed test, on
  branch switch).
- **Embedding migration.** Schema pins dimension at 1024 via `vec0`. Switching
  models means rebuilding the vector table — straightforward but worth a script.
