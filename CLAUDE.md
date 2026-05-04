# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this repo is

A Claude Code **plugin** that ships persistent project memory: an MCP server,
a PostToolUse hook, an end-of-task curator subagent, and a `/remember` slash
command — bundled together so users get all four with a single
`/plugin install`. The repo doubles as a single-plugin marketplace.

The design rationale lives in `docs/PRD-001-deterministic-discipline.md`. Read
it first when touching anything multi-component; it explains *why* mechanical
behaviors (auto-memory mirroring, cross-project fallback) are mechanism-driven
and *why* judgment behaviors (insight phrasing, ASK-before-applying) stay
prompt-driven.

## Plugin components and how they fit together

```
.claude-plugin/plugin.json     # manifest: declares MCP server + hooks ref
.claude-plugin/marketplace.json # makes this repo its own marketplace
hooks/hooks.json               # SessionStart deps install + PostToolUse mirror
agents/lodestone-memory-curator.md  # end-of-task capture subagent
commands/remember.md           # /remember -> spawns curator
lodestone_memory/              # the Python package (MCP server + hook script)
```

### MCP server (`lodestone_memory/server.py`)

Six FastMCP tools — `remember`, `recall`, `list_recent`, `get_memory`,
`update_memory`, `forget`. The module-level `LODESTONE_INSTRUCTIONS` string
is the server-side system prompt that Claude Code surfaces on session start
(PREFLIGHT, RECALL discipline, insight-form, cross-project ASK protocol).
Trim/edit it deliberately — it's a load-bearing prompt, not a comment.

The server owns the SQLite connection via FastMCP's `lifespan`. Tools reach
the connection through `ctx.request_context.lifespan_context["conn"]`. Tests
inject a different conn by overriding lifespan, never by monkey-patching
module state.

Storage: single global DB at `~/.lodestone/memory.db` filtered by
`project_id`. Project ID precedence (in `lodestone_memory/project.py`):
`LODESTONE_PROJECT_ID` env (used by evals to scope sandboxes) → git remote
URL → absolute cwd. The first 16 chars of `sha256(label)` is the id.

### PostToolUse hook (`lodestone_memory/mirror.py`)

Fires after every Write tool call. If the path matches
`~/.claude/projects/*/memory/*.md` and is not `MEMORY.md`, it parses the YAML
frontmatter and upserts a lodestone row keyed by `(source_file, project_id)`.
The mapping `feedback → preference`, `project → fact` lives in `_TYPE_TO_KIND`
near the top of the file.

Two non-obvious things to preserve when editing this file:

1. `load_dotenv()` MUST run **before** `from lodestone_memory import db, memory`.
   The PostToolUse process inherits a stripped env that doesn't carry the
   user's Voyage key; `embeddings.py` caches the client on first import-time
   call, and without an explicit dotenv load the hook crashes silently. There
   is a regression test for this in `tests/test_plugin_artifacts.py`.
2. Pure helpers (`is_auto_memory_path`, `parse_frontmatter`,
   `map_to_lodestone_fields`) stay free of DB/IO so they can be unit-tested
   without a sandbox.

### Curator subagent (`agents/lodestone-memory-curator.md`)

Markdown file with YAML frontmatter. The `tools:` line declares **both**
`mcp__lodestone__*` (global MCP registration, used in dev/eval) and
`mcp__plugin_lodestone-memory_lodestone__*` (plugin install) prefixes — the curator
must work in both contexts. `tests/test_plugin_artifacts.py` enforces this
and also that `forget` is NOT granted (PRD §10 open question 2).

The body teaches insight-form discipline, restraint (most passes should
produce 0–3 memories), and the exact `links: [{to_uuid, kind}]` shape (NOT
`target_uuid`/`type` — the validator in `memory.py:_validate_links` rejects
those loudly because silent drops caused a real curator bug).

### Slash command (`commands/remember.md`)

`/remember [focus]` — spawns the curator via the Task tool, passes
`$ARGUMENTS` as focus when provided. Test enforces both that it references
the curator and that `$ARGUMENTS` flows through.

### How the four parts collaborate

1. User starts a session → SessionStart hook installs/refreshes deps into
   `${CLAUDE_PLUGIN_DATA}/site-packages` (idempotent diff check).
2. Claude calls `ToolSearch(query="lodestone")` (preflight, prompted by
   `LODESTONE_INSTRUCTIONS`) so deferred tool schemas load.
3. During work, Claude calls `recall` / `remember` directly OR writes to
   `~/.claude/projects/.../memory/*.md` (Claude Code's auto-memory).
4. Each Write triggers the PostToolUse hook → `mirror.py` upserts the row
   into lodestone. Auto-memory and lodestone stay in sync without prompt
   discipline.
5. At end of substantial work (or on `/remember`), main Claude spawns the
   `lodestone-memory-curator` subagent, which runs in its own context and
   captures durable insights without build-mode pressure.

## Storage layout

Single SQLite DB at `~/.lodestone/memory.db` (override with `LODESTONE_DB`
env var; evals use a per-scenario sandbox path). Opened in WAL mode with
`PRAGMA foreign_keys = ON`, with the `sqlite-vec` extension loaded — the
db layer prefers `pysqlite3` over stdlib because stdlib's SQLite often
ships without loadable-extension support. Schema is in
`lodestone_memory/schema.sql`; additive migrations live in
`db._migrate()` (SQLite has no `ADD COLUMN IF NOT EXISTS`, so we
introspect `PRAGMA table_info` and ALTER conditionally).

### Tables

- **`memories`** — one row per insight. Keys/state: `id` (rowid),
  `uuid` (public), `project_id` (16-char sha256 prefix derived in
  `project.py`), `project_label` (human-readable origin, e.g. git remote URL),
  `source_file` (abs path of the auto-memory file when mirrored by the hook;
  used as the upsert key with `project_id`), `kind`, `title`, `content`,
  `outcome`, `confidence`, `context` (JSON blob), timestamps, `superseded_by`
  (FK to `memories.id`), `deleted_at` (soft delete), `access_count` (bumped
  on `recall` hits and `get_memory`).
- **`tags` + `memory_tags`** — many-to-many. Tags are *also* mirrored into
  the FTS row's `tags` column by `_set_tags` so keyword search can match
  on tag text. Don't add tags via direct INSERT — go through `_set_tags`
  or the FTS row drifts out of sync.
- **`memory_links`** — typed directed edges between memories. Four kinds:
  `supersedes`, `related`, `contradicts`, `caused_by`. The PK is
  `(from_id, to_id, kind)` so the same pair can carry multiple kinds.
  `_set_links` is the only writer; it also sets `memories.superseded_by`
  in lockstep when the kind is `supersedes`.
- **`memory_vec`** — `vec0` virtual table holding 1024-dim
  `voyage-code-3` embeddings. PK `memory_id` is NOT a SQLite foreign key
  (vec0 doesn't support FKs or triggers) — so deletes must clean it
  explicitly. `admin.py:purge` does this; `_set_embedding(replace=True)`
  does it on update; the `mem_ad` trigger does NOT.
- **`memory_fts`** — FTS5 virtual table over `(title, content, tags)`
  with porter+unicode61 tokenization. Kept in sync via three triggers
  (`mem_ai`, `mem_au`, `mem_ad`) — title/content sync on INSERT/UPDATE,
  rows drop on DELETE. Tag column is NOT updated by the triggers — it's
  populated by `_set_tags`'s explicit UPDATE.

### What gets embedded

`memory.py:_set_embedding` joins title and content with two newlines
(`f"{title}\n\n{content}"`) and embeds the result with `voyage-code-3`,
`output_dimension=1024`, `input_type="document"`. Tags, context JSON, and
metadata are NOT embedded — they're keyword-searchable through FTS5 (tags)
or filterable post-retrieval (kind, outcome, min_confidence, since,
include_superseded). Queries are embedded with the same model but
`input_type="query"` (Voyage uses asymmetric encoders).

Update path: when `update` patches `title` or `content`, the embedding is
deleted and re-inserted with the new joined text. Editing other fields
(tags, confidence, outcome, kind, context) does NOT regenerate the
embedding — saves a Voyage call and is safe because none of those go
into the vector.

### Recall ranking

Two pure stages in `ranking.py`:

1. `fuse_rrf` — reciprocal rank fusion across the vector and FTS hit
   lists. Each candidate's score is `Σ 1/(60 + rank + 1)` across the
   lists it appears in (smoothing constant `RRF_K = 60`). Avoids having
   to normalize cosine distance against BM25.
2. `apply_postretrieval_factors` —
   `final = rrf × confidence × (0.7 + 0.3 × recency) × supersede_penalty × cross_project_penalty`
   where `recency = 0.5 ** (age_days / 60)` (60-day half-life), anchored
   at `verified_at` if set else `created_at`; supersede penalty `0.3`;
   cross-project penalty `0.5` when the row's `project_id` differs from
   the caller's. Same-project results therefore outrank cross-project at
   similar raw match quality — a cross-project hit means the fusion
   score was strong enough to overcome the penalty.

The constants (RRF_K, HALF_LIFE_DAYS, RECENCY_WEIGHT, SUPERSEDE_PENALTY,
CROSS_PROJECT_PENALTY) live as module-level names in `ranking.py` so
tuning them is a one-file change. RECENCY_WEIGHT and BASELINE_WEIGHT
must sum to 1.0 (convex combination).

## Eval pipeline

The eval harness verifies that prompt changes (server `LODESTONE_INSTRUCTIONS`,
tool descriptions, the curator's body) actually move Claude's behavior in the
intended direction. Six scenarios in `evals/scenarios.json`, each driving
`claude -p` headlessly against a sandbox.

### Per-scenario flow (`evals/run.py`)

1. Create temp dir; allocate `project_id = f"eval-{sid}"`.
2. `seed_db()` calls `lodestone_memory.memory.remember()` directly to populate
   the sandbox DB with the scenario's `seed_memories` — same code path as
   production, so embeddings + FTS rows are real.
3. `write_mcp_config()` writes a temp `.mcp.json` with env vars
   `LODESTONE_DB`, `LODESTONE_PROJECT_ID`, and a passthrough of
   `VOYAGE_API_KEY`.
4. `run_claude()` invokes `claude -p <user_message>` with
   `--output-format stream-json --strict-mcp-config` and an `--allowedTools`
   list pulled from the **live** server registration via
   `lodestone_tool_names()` — this can never drift out of sync with what
   ships.
5. `extract_trace()` walks the stream-json events, collecting every
   `tool_use` block + the final `result` text into a trace dict.
6. After all scenarios run, `cleanup_eval_orphans()` sweeps
   `~/.claude/projects/-tmp-lodestone-eval-*` (Claude Code logs every
   headless run; our temp cwds never recur).

### Grading (`evals/grade.py`)

Pure function — `grade(payload) -> GradeReport`, no I/O. Each scenario's
`expects` block can declare:

- `must_call: [tool, ...]` — every tool listed must appear in the trace.
- `must_not_call: [tool, ...]` — none may appear.
- `tool_call_args_must_match: {tool: {arg: rule}}` — at least one call to
  `tool` must satisfy ALL its field rules. Rule shapes:
  `{"any_of_substrings": [...]}`, `{"none_of_substrings": [...]}` (case-
  insensitive substring containment), `{"equals": value}` (exact).

Each scenario gets binary 10 (pass) or 0 (fail). The scorer's `none_of_substrings`
intentionally treats non-string values as vacuously passing — pair with
`any_of_substrings` if you also need the field to exist.

### History (`evals/history.py`)

Each `run.py` invocation appends a slim summary line (mean, median, modes,
per-scenario pass/fail, optional `--note`) to `evals/results/history.jsonl`.
Full traces stay in `results-<timestamp>.json`. Use `--note "tightened recall description"`
to tag a run when iterating on prompts; the printed tail makes prompt-tuning
deltas visible at a glance.

### Dogfood replays

`evals/dogfood-replay-prompts.md` holds the 6 user prompts from the original
compass session. These are run *manually* in a fresh Claude Code session
(not by `run.py`) when validating end-to-end behavior of the hook + curator
together — rule-based eval can't cover the Write-driven dual-write path.

## Dev workflow

```bash
# Setup
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
# Voyage key — pick one (precedence: real env > project ./.env > ~/.lodestone/.env):
#   export VOYAGE_API_KEY=...                   # shell rc; passed through by plugin.json
#   echo "VOYAGE_API_KEY=..." > .env            # project-local override
#   echo "VOYAGE_API_KEY=..." > ~/.lodestone/.env  # global default

# Tests
pytest                                       # full suite
pytest tests/test_plugin_artifacts.py -q     # plugin manifests/agents/commands
pytest tests/test_memory.py::test_recall_basic -q   # single test

# Evals
.venv/bin/python evals/run.py                       # run + grade all
.venv/bin/python evals/run.py --scenario ID         # one scenario (repeatable)
.venv/bin/python evals/run.py --no-grade            # run only; inspect later
.venv/bin/python evals/run.py --note "<msg>"        # annotate this run
.venv/bin/python evals/grade.py                     # re-grade evals/results/latest.json

# DB admin
lodestone-purge --current   # wipe current project's memories
lodestone-purge --project <id>
lodestone-purge --all
lodestone-purge --current --yes   # skip confirmation
```

The `.mcp.json` at the repo root points at `.venv/bin/lodestone-memory` so
this repo dogfoods itself: open it in Claude Code and the local server is
the one being edited.

## Things that look optional but aren't

- **`source_file` upsert key.** The hook uses `(source_file, project_id)` to
  decide create vs update. Removing this column or changing the lookup keys
  will produce duplicates the next time Claude rewrites an auto-memory file.
- **Both global and plugin tool-name forms in the curator's `tools:` line.**
  Drop one and the curator stops working in either dev or production.
- **The `${CLAUDE_PLUGIN_DATA}/site-packages` PYTHONPATH entry in
  `plugin.json`.** Without it, `python -m lodestone_memory` can't find
  `voyageai` / `sqlite-vec` / `mcp` even though the SessionStart hook just
  installed them.
- **`load_dotenv()` ordering in `mirror.py`.** See above; there's a
  regression test guarding it.
- **`lodestone_tool_names()` in `evals/run.py`.** Pulling tool names from the
  live `mcp.list_tools()` rather than hardcoding them is what keeps eval
  allowlists in sync with shipped tools.
