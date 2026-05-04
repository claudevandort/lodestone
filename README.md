# Lodestone

Persistent project memory for Claude Code.

Lodestone lets Claude Code remember the things that matter, such as decisions
made, dead ends ruled out, gotchas hit, or preferences expressed. It recalls
them automatically when a future task touches the same problem class, even
across different projects.

Under the hood it bundles an MCP server (local SQLite with hybrid retrieval
via Voyage embeddings and FTS5), a PostToolUse hook that mirrors Claude
Code's auto-memory writes, and a curator subagent that captures durable
insights at end-of-task.

A lodestone is the naturally magnetized stone sailors used as a compass; this one steers Claude Code in the right direction given the task at hand.

## What you get

- **`recall`** — semantic + keyword search over everything you've captured,
  scoped to the current project by default. Falls back to cross-project
  automatically when the local project has nothing useful, so insights from
  one repo can surface in another when they apply.
- **`remember`** — store an insight (decision, attempt, gotcha, preference,
  fact, question) with tags, confidence, structured context, and typed links
  to related memories.
- **Auto-memory mirroring** — when Claude Code writes to its built-in
  `~/.claude/projects/<project>/memory/*.md` files, a hook mirrors them into
  Lodestone's searchable index automatically. The same insight ends up in
  both places without any extra prompt discipline.
- **`/remember` slash command** — runs the bundled `lodestone-memory-curator`
  subagent for an end-of-session capture pass. The curator runs in its own
  context (no build-mode pressure) and decides what's worth keeping —
  insights phrased as transferable lessons, not transcripts of what happened.

## Install

This repo is itself a single-plugin marketplace. Two commands in any Claude
Code session:

```
/plugin marketplace add claudevandort/lodestone
/plugin install lodestone-memory@lodestone
```

The first time the plugin enables, a `SessionStart` hook installs the runtime
Python deps (mcp SDK, voyageai, sqlite-vec, pysqlite3-binary, python-dotenv)
into the plugin's data directory. Expect a 30–60s pause on first session;
subsequent sessions skip the install.

## Configure

Lodestone uses [Voyage AI](https://dashboard.voyageai.com/) for embeddings
(`voyage-code-3`, 1024-dim — the free tier is generous; adding a payment
method lifts rate limits but doesn't cost anything until you exhaust free
tokens).

You need a `VOYAGE_API_KEY` available to Claude Code. Pick whichever path
suits you:

**Option 1 — shell environment (recommended).** Export once in your shell rc:

```bash
export VOYAGE_API_KEY=pa-...
```

The plugin's manifest forwards this into the MCP server automatically, so
every Claude Code session inherits it. No extra files.

**Option 2 — global Lodestone config file.** If you'd rather not export the
key in your shell:

```bash
mkdir -p ~/.lodestone && chmod 700 ~/.lodestone
cat > ~/.lodestone/.env <<'EOF'
VOYAGE_API_KEY=pa-...
EOF
chmod 600 ~/.lodestone/.env
```

**Option 3 — per-project `.env`.** If a particular project should use a
different key (or no key at all), drop a `.env` in the project root with
`VOYAGE_API_KEY=...`. Project-local wins over the global file.

Precedence is real env vars → project `./.env` → `~/.lodestone/.env`.

## Using it

You normally don't need to do anything explicit. The plugin's instructions
get loaded into Claude's context on session start; Claude is steered to
recall before tackling work, capture insights at natural reflection points,
and mirror auto-memory writes into Lodestone via the hook.

The cases below show what that looks like in practice.

### Recall an insight you forgot you had

```
> Let's switch our ORM to SQLAlchemy — the async story will be much better.

[Claude calls recall("sqlalchemy ORM migration")]

I found a memory from 2026-02 that's directly relevant: "ORM swap to
SQLAlchemy failed under heavy alembic+Django coupling — migration history
incompatible." Worth re-reading before we commit to the same approach
again. The note suggests the asyncio-bridge escape hatch as an alternative
when the goal is just async support.
```

Claude calls `recall` automatically on tasks involving choices, patterns,
or familiar problem classes. You don't have to ask.

### Capture a hard-won lesson

```
> Spent two hours on this — Pydantic v2's default serialization bypasses
> our custom validator entirely, so malformed payloads were going out
> without warning. Reverting v2 for now.

[Claude calls remember(kind="gotcha", title="Pydantic v2 default serializers
bypass custom validators", content="...")]

Captured. Future sessions will surface this if anyone proposes the v2
upgrade again.
```

Claude treats sustained failures, surprising library behavior, and
explicit decisions as capture-worthy moments. You can also be explicit:
"remember this," "save what we learned."

### End-of-task wrap-up with `/remember`

When you've finished a substantial work block and want a deliberate capture
pass, run:

```
/remember
```

The curator subagent reviews the recent conversation, recalls existing
memories to avoid duplicates, and writes 0–3 well-formed insights — or
reports "nothing worth capturing," which is also a valid outcome. Quality
over quantity is the goal.

Narrow the scope with an argument:

```
/remember the deployment workflow we just defined
```

### Cross-project knowledge transfer

When you start work in a new repo, Lodestone has nothing local — but it
auto-falls-back to other projects you've used:

```
> Set up CI for this Next.js app, mirroring how we do it on the other
> services.

[Claude calls recall("CI Next.js deploy") — local empty, server retries
across projects]

I found relevant memories from your "compass" project: the CI uses GitHub
Actions with a separate workflow for preview deploys. Worth applying here?
(memory: "Vercel preview deploys via GH Actions — split workflow keeps
prod path fast")
```

Claude is taught to surface the source project and ask before applying —
other projects' conventions aren't authoritative for the current one.

### Auto-memory dual-write (zero ceremony)

Claude Code already writes per-project notes into
`~/.claude/projects/<project>/memory/*.md` when it notices something worth
remembering about the user, the project, or a workflow preference. With
Lodestone installed, every such write also lands in the searchable index
via the PostToolUse hook — no additional prompts, no missed captures.
Editing the same file later updates the existing Lodestone row instead of
creating a duplicate.

## Where memories live

Everything lives in a single SQLite file at `~/.lodestone/memory.db`.

Per-project scoping is by `project_id`, derived from the project's git
remote URL (or the absolute path if there's no remote) so the same project
recognizes itself across clones and worktrees.

Wipe a project's memories:

```bash
lodestone-purge --current        # current project (cwd-derived)
lodestone-purge --project <id>   # specific project_id
lodestone-purge --all            # everything
```

Add `--yes` to skip the confirmation prompt.

## Architecture

See [`DESIGN.md`](DESIGN.md) for the schema, retrieval pipeline, and ranking
formula. See
[`docs/PRD-001-deterministic-discipline.md`](docs/PRD-001-deterministic-discipline.md)
for the design rationale behind the hook + subagent + slash command split.

## Reporting issues

Open an issue on
[github.com/claudevandort/lodestone](https://github.com/claudevandort/lodestone).

## License

MIT — see [LICENSE](LICENSE).
