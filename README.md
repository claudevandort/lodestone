# Lodestone

Persistent project memory for Claude Code — hybrid retrieval (Voyage embeddings
+ sqlite-vec + FTS5) with auto-memory dual-write, cross-project recall, and a
curator subagent for end-of-task capture.

## What it gives you

- **`recall`** — semantic + keyword search over everything you've captured,
  scoped to the current project by default. Falls back to cross-project
  automatically when local returns nothing useful.
- **`remember`** — store an insight (decision / attempt / gotcha / preference
  / fact / question) with tags, confidence, structured context, and typed
  links to related memories.
- **Auto-memory dual-write** — when Claude Code writes to its own
  `~/.claude/projects/<project>/memory/*.md` files, a PostToolUse hook
  mirrors them into the searchable lodestone index automatically. Same insight
  ends up in two places without any extra prompt discipline.
- **`/remember` slash command** — invokes the bundled `lodestone-curator`
  subagent for an end-of-session wrap-up review. The curator runs in its own
  context (no build-mode pressure) and decides what's worth keeping.

## Install

This repo is itself a single-plugin marketplace. Two commands in any Claude
Code session:

```
/plugin marketplace add claudevandort/lodestone-memory
/plugin install lodestone@lodestone-memory
```

The first time the plugin is enabled, a `SessionStart` hook installs the
runtime Python deps (mcp SDK, voyageai, sqlite-vec, pysqlite3-binary,
python-dotenv) into `${CLAUDE_PLUGIN_DATA}/site-packages`. Expect a 30–60s
pause on the first session; subsequent sessions skip the install.

## Configure

Lodestone needs a Voyage API key for embeddings. Get one at
[dashboard.voyageai.com](https://dashboard.voyageai.com/) (the
`voyage-code-3` model has a generous free tier; payment-method-on-file lifts
rate limits but doesn't cost anything until you exhaust free tokens).

Put the key in `~/.lodestone/.env`:

```bash
mkdir -p ~/.lodestone
chmod 700 ~/.lodestone
cat > ~/.lodestone/.env <<'EOF'
VOYAGE_API_KEY=your-key-here
EOF
chmod 600 ~/.lodestone/.env
```

Both the MCP server and the dual-write hook load env from
`~/.lodestone/.env` (and from `./.env` in the project, if present —
project-level wins). Real env vars override both.

## Usage notes

You normally don't need to do anything explicit. The plugin's instructions
get loaded into Claude's context on session start; Claude is steered to:

1. Run `ToolSearch(query="lodestone")` once per session to load the deferred
   tool schemas (preflight).
2. Call `recall` before tackling any task involving a choice, pattern, or
   familiar problem class.
3. Call `remember` (or rely on the dual-write hook) to capture insights at
   natural reflection points.
4. Treat cross-project hits as suggestions, not authoritative — surface their
   `source_project` and ask before applying.

When you finish substantial work and want a wrap-up capture pass:

```
/remember
```

Optional argument focuses the curator: `/remember the code conventions we just defined`.

## Where memories live

Everything goes into a single SQLite file at `~/.lodestone/memory.db`
(plus the Voyage-embedded vectors in `memory_vec`, the FTS5 index in
`memory_fts`). Per-project scoping is via the `project_id` column derived
from the project's git remote (or absolute path if no remote).

To clear out a project's memories:

```bash
lodestone-purge --current        # current project (cwd-derived)
lodestone-purge --project <id>   # specific project_id
lodestone-purge --all            # everything
```

## Architecture

See [`DESIGN.md`](DESIGN.md) for the schema, retrieval pipeline, and ranking
formula. See [`docs/PRD-001-deterministic-discipline.md`](docs/PRD-001-deterministic-discipline.md)
for the design rationale behind the hook + subagent + slash command split.

## Reporting issues

Open an issue on
[github.com/claudevandort/lodestone-memory](https://github.com/claudevandort/lodestone-memory).

## License

MIT — see [LICENSE](LICENSE).
