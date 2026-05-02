# PRD-001: Deterministic discipline for the lodestone capture/recall loop

**Status:** Draft (pre-implementation, post-plugin-research revision)
**Date:** 2026-05-03
**Owner:** Claude Vandort

> **Revision note:** v2 of this PRD updates §6, §8, and §9 to reflect that
> Claude Code has a formal **plugin** system (`.claude-plugin/plugin.json`,
> marketplaces, `/plugin install`) which can bundle the MCP server, hooks,
> subagents, and slash commands as a single installable artifact. v1 of
> this PRD assumed manual `pip install` + manual settings.json edits + manual
> file copying as the install flow; that's superseded by the plugin path.

## 1. Background and motivation

### What we observed

Two dogfood passes (compass3, compass4) on identical user prompts and identical
lodestone server prompts produced markedly different lodestone usage:

| Session | recall calls | remember calls | Auto-memory files | Behavior on cross-project hit |
|---|---|---|---|---|
| compass3 | 2 (both cross-project) | 1 | 1 + MEMORY.md | ASKED before applying |
| compass4 | 2 (both cross-project) | 0 | 0 | Did NOT ask, went straight to building |

In both cases Claude built substantive code (compass4 actually produced *more*:
7 entities, full CRUD, frontend with views) — but the discipline around capture
(`remember`) and ASK-before-applying landed in compass3 and didn't in compass4.

### Why this happens (root cause)

We have been using **prompts as the only enforcement mechanism** for behaviors
that split cleanly into two categories:

1. **Mechanical behaviors** — operations with no judgment content (e.g. mirror
   an auto-memory write into lodestone). Prompts are a poor fit because
   they introduce LLM stochasticity into something that should be deterministic.
2. **Judgment behaviors** — operations that depend on context the system can't
   see (e.g. whether a memory's content is worth capturing, whether an insight
   from another project should be ASKed about, what `related` links semantically
   apply). Prompts are the right fit because Claude has the context.

Treating mechanical behaviors as judgment problems is what produces the
run-to-run variance we observed. The fix is to use the *right tool for each
category* — Claude Code's mechanical extension surfaces (hooks, subagents,
slash commands) for the deterministic parts; prompts for the judgment parts.

### What this PRD covers

Four features that together close the discipline gap surfaced by compass4
without removing the judgment parts that prompts handle correctly:

1. **PostToolUse hook** that auto-mirrors auto-memory writes into lodestone.
2. **Server-side cross-project auto-retry** when local recall returns empty,
   with an explicit signal back to the caller that the fallback fired.
3. **`lodestone-curator` subagent** for end-of-task capture, separating
   capture-as-reflection from build-mode task pressure.
4. **`/capture` slash command** as a user-initiated escape hatch when the
   above don't fire.

## 2. Goals and non-goals

### Goals

- Make auto-memory → lodestone mirroring an **invariant**, not a discipline.
  Compass4-style "Claude wrote files but no lodestone calls" failure mode
  should become impossible.
- Reduce the prompt's surface area. Behaviors enforced by hooks/subagents
  should be removed from the server-level instructions, not duplicated.
- Preserve all judgment-driven behavior (ASK, capture decision, link
  semantics) under prompt control where Claude has the relevant context.
- Make the cross-project auto-retry surfaceable to Claude so the existing
  ASK-before-applying discipline still kicks in for fallback results.
- **Distribute as a single Claude Code plugin** so the user runs ONE
  install command (`/plugin install lodestone@<marketplace>`) and gets
  the MCP server, hook, subagent, and slash command together — no manual
  settings edits, no separate `pip install`.

### Non-goals

- Replace the existing MCP tool surface. All four existing lodestone tools
  (`remember` / `recall` / `get_memory` / `update_memory` / `list_recent` /
  `forget`) keep their current semantics.
- Build a generic "memory ops automation" framework. Each feature is
  scoped to a specific failure mode, not extended for hypothetical needs.
- Sync edits to auto-memory files made *outside* the Write tool (manual
  edits in an editor). Out of scope for v1; addressable later via a
  filesystem watcher if needed.
- Publish a parallel `pip install lodestone-mcp` distribution for
  non-Claude-Code MCP clients. Plugin-only for v1; pip path can be added
  later when there's a real consumer.

## 3. Features

### Feature 1 — PostToolUse hook: auto-memory dual-write

**Problem.** Compass4 demonstrated that the prompt-driven dual-write
(LODESTONE_INSTRUCTIONS' DUAL-WRITE PROCEDURE section) is unreliable when
Claude is task-pressured. We need it to fire deterministically.

**Behavior.**

1. A `PostToolUse` hook fires after every successful `Write` tool invocation.
2. The hook script inspects the written file path. If it does not match
   `~/.claude/projects/*/memory/*.md`, OR if it is `MEMORY.md` (the index),
   the hook exits with no action.
3. Otherwise, the hook reads the file, parses the YAML frontmatter, and
   maps fields to lodestone's schema:

   | Auto-memory frontmatter | Lodestone field | Notes |
   |---|---|---|
   | `name` | `title` | required |
   | `description` (or body if absent) | `content` | required; prefer body when both present |
   | `type` | `kind` | mapping: `feedback` → `preference`, `project` → `fact`, otherwise pick the best lodestone kind from content heuristics or fall back to `fact` |
   | (file path) | `source_file` | new column; used as the dedup key for upsert |
   | (project_id derived from cwd) | `project_id` / `project_label` | derived via existing `derive_project_id()` |

4. The hook calls lodestone via either:
   - **Option A:** Subprocess invoke a new `lodestone-mirror` console script
     that takes the file path and performs the upsert.
   - **Option B:** Direct Python import — the hook IS the script, requires
     lodestone to be importable.
   *Decision below in §6.*
5. **Upsert semantics.** Look up an existing memory by `source_file` for
   this project_id. If found, call `update_memory` with the patched
   fields. If not found, call `remember`. Either way the lodestone DB row
   stays in sync with the file.
6. Hook output: a one-line confirmation message printed to stdout (e.g.
   `lodestone: mirrored memory/foo.md → uuid=abc123…`), which Claude Code
   surfaces back to Claude via the hook output mechanism.
7. Hook errors (lodestone unavailable, parse failure, schema validation)
   exit non-zero with an error message; Claude Code surfaces the error but
   does NOT block the original Write call.

**Schema change.**
Add `source_file TEXT` to `memories`. Indexed for the upsert lookup. Existing
rows keep `NULL`. Migration handled in `db._migrate()`.

**Acceptance criteria.**

- Writing `~/.claude/projects/<p>/memory/foo.md` causes a corresponding
  lodestone row to appear within ~1 second.
- Editing the same file causes the same row to update (no duplicate).
- Writing `~/.claude/projects/<p>/memory/MEMORY.md` does NOT trigger the hook.
- A malformed YAML frontmatter logs an error to stderr but does not crash
  Claude's session.
- After feature lands, the DUAL-WRITE PROCEDURE section can be removed
  from `LODESTONE_INSTRUCTIONS` (mechanism is no longer prompt-driven).

**Effect on existing prompts.**

- DUAL-WRITE PROCEDURE section removed from `LODESTONE_INSTRUCTIONS`.
- The `remember` tool description's MAPPING note (auto-memory → lodestone
  fields) is moved/repurposed for the hook script's reference, since
  Claude no longer needs to do this mapping itself.

### Feature 2 — Server-side cross-project auto-retry on empty local recall

**Problem.** The recall discipline section says "if local returns nothing
useful, retry with `include_other_projects: true`." That's mechanical (when
local returns 0) — we can do it server-side. Saves a tool call and removes
a step Claude has to remember.

**Behavior.**

1. When `recall(...)` is called with `include_other_projects=False` (the
   default) AND the candidate retrieval returns 0 results matching the
   current project_id after filters, the server **automatically retries
   once** with `include_other_projects=True` (same query, k, filters).
2. The response signals to the caller that the fallback fired. Response
   shape changes from `list[dict]` to:

   ```python
   {
       "results": [...],          # list of memory dicts as today
       "meta": {
           "fallback_to_other_projects": bool,  # True if the auto-retry fired
           "local_count": int,                  # how many local results
           "returned_count": int,               # len(results)
       }
   }
   ```

3. Each result still carries `cross_project: bool` and `source_project: str`
   per the existing schema.
4. `recall` tool description updated to document the new response shape and
   to make clear that fallback results follow the same ASK-before-applying
   discipline as explicit cross-project results.

**Acceptance criteria.**

- `recall("nonexistent local term")` on an empty project returns
  `{"results": [...], "meta": {"fallback_to_other_projects": True, "local_count": 0, ...}}`
  with cross-project hits if any exist.
- `recall("term that has local hits")` returns the local results with
  `meta.fallback_to_other_projects = False`.
- Explicit `recall(..., include_other_projects=True)` does NOT trigger a
  second retry; honors the caller's choice and returns mixed results.
- Existing behavior preserved: results still ranked by recency × confidence
  × supersede × cross-project penalty.
- `LODESTONE_INSTRUCTIONS` recall section can drop the manual "if local
  empty, retry with cross-project" step.

**Open question (decided).**

> User constraint: "if on recall we get 0 local results and then we respond
> to that tool call with results from other projects, we need to let the
> caller (claude) know that that was the case."

Honored via the `meta.fallback_to_other_projects` field on the response.

### Feature 3 — `lodestone-curator` subagent

**Problem.** Compass4 produced 86 Write tool calls and 0 lodestone remember
calls. Claude was deep in build mode and "natural reflection points" never
naturally arose. A subagent with a single, focused responsibility runs in
its own context window without the build-mode pressure.

**Behavior.**

1. New custom subagent at `~/.claude/agents/lodestone-curator.md`.
2. Frontmatter:
   ```yaml
   ---
   name: lodestone-curator
   description: Reviews recent work and captures memorable insights into lodestone. Use after substantial work blocks or when explicitly asked to "save what we learned".
   tools: mcp__lodestone__remember, mcp__lodestone__recall, mcp__lodestone__get_memory, mcp__lodestone__update_memory, mcp__lodestone__list_recent, Read, Glob
   ---
   ```
3. System prompt: a focused-and-shorter version of the lodestone capture
   guidance. No competing concerns. Specifically:
   - Insight-form discipline (transferable lessons, not transcripts)
   - Per-pattern `related` linking (one entry per source memory)
   - Restraint (quality over quantity; not every session produces a memory)
   - Cross-project ASK-before-applying still applies if the curator
     surfaces cross-project results
   - The curator is read-and-write into lodestone; it does NOT write to
     auto-memory (the hook covers the inverse direction)
4. Invocation: main Claude calls
   `Task(subagent_type="lodestone-curator", prompt="Review the recent work and capture insights worth keeping.")`
   at end of substantial tasks. The user can also invoke it via the
   `/capture` command (Feature 4).
5. Output: a structured report of memories captured (uuid, title, kind,
   any links) so the main session and the user can see what was kept.

**Effect on existing prompts.**

- The "CALL `remember` AS DELIBERATE SYNTHESIS" section in
  `LODESTONE_INSTRUCTIONS` updated to add: "At end of substantial work,
  invoke the `lodestone-curator` subagent rather than capturing memories
  inline. This preserves your build-mode focus and gives capture its own
  context for proper reflection."

**Acceptance criteria.**

- `Task(subagent_type="lodestone-curator", ...)` from main Claude returns
  a summary of memories captured (or "nothing worth capturing", which is
  also valid).
- The curator writes well-formed insight-style memories (verifiable
  manually; not testable via rule-based eval).
- The curator populates `related` links per source memory when applicable.
- Repeated invocations don't duplicate captures (curator should `recall`
  first and prefer `update_memory` on near-duplicates).

### Feature 4 — `/capture` slash command

**Problem.** Even with the subagent, main Claude must remember to invoke it.
A user-side escape hatch covers the cases where it doesn't.

**Behavior.**

1. New slash command at `~/.claude/commands/capture.md`.
2. Body:
   ```markdown
   Invoke the lodestone-curator subagent to review the conversation so far
   and capture any insights worth keeping in lodestone. Optional argument
   $ARGUMENTS narrows the curator's focus to a specific topic.
   ```
3. When user types `/capture`, main Claude spawns the curator subagent
   (Feature 3) and reports its findings.
4. `/capture <topic>` passes the topic as focus.

**Acceptance criteria.**

- `/capture` invokable at any point in a session; produces a curator
  report summarizing captures (or none).
- `/capture <topic>` narrows scope appropriately.
- No errors on empty / trivial sessions ("nothing worth capturing" is a
  valid outcome).

## 4. Schema changes

Single additive change:

- `memories.source_file TEXT` (NULLable). Used by Feature 1's hook for
  the upsert lookup. Indexed:
  ```sql
  CREATE INDEX IF NOT EXISTS idx_mem_source_file
    ON memories(source_file) WHERE source_file IS NOT NULL;
  ```
- Migration in `db._migrate()` adds the column for existing DBs (idempotent).

No changes to `memory_links`, no new link kinds, no other column changes.

## 5. Prompt changes

After all four features land, `LODESTONE_INSTRUCTIONS` should be **shorter**:

| Section | After |
|---|---|
| Opening framing | Keep |
| THE FORM OF A GOOD MEMORY | Keep |
| RECALL BEFORE EVERY TASK | Keep step 1 (local recall first); **drop step 2** (server auto-retries); keep step 3-4 |
| CALL `remember` AS DELIBERATE SYNTHESIS | Keep, but add: "At end of substantial work, invoke the `lodestone-curator` subagent rather than capturing inline" |
| DUAL-WRITE WITH CLAUDE CODE'S AUTO-MEMORY | **Remove entirely** — handled by hook |
| CROSS-PROJECT RECALL | Keep, including the per-pattern linking guidance from PRD-0 (already shipped in `a9824f1`) |
| Use the kinds | Keep |
| DO NOT REMEMBER trivia | Keep |

The `remember` tool description loses its MAPPING block (no longer needed
since the hook does the mapping; manual `remember` callers don't need it).

## 6. Implementation decisions

### 6.1 Distribution unit: a Claude Code plugin

All four features ship as components of a single Claude Code **plugin**.
A plugin is a directory containing a `.claude-plugin/plugin.json` manifest
plus standard subdirectories for components. Claude Code's plugin system
supports bundling MCP servers, hooks, subagents, and slash commands together;
users install the whole bundle with one command.

**Why plugin (not pip + manual wiring):**
- One install command instead of three+ manual steps
- Versioning and updates flow through `/plugin update`
- No PATH / settings.json / `~/.claude/agents/` editing required
- The MCP server, hook, subagent, and command share one lifecycle

### 6.2 Repo layout (the plugin IS the repo root)

```
lodestone-mcp/                       # repo root = plugin root
├── .claude-plugin/
│   ├── plugin.json                  # manifest (name, version, MCP config)
│   └── marketplace.json             # repo doubles as a marketplace
├── lodestone_mcp/                   # Python package (was src/lodestone_mcp/)
│   ├── __init__.py
│   ├── __main__.py                  # `python -m lodestone_mcp` → server
│   ├── server.py
│   ├── memory.py
│   ├── ranking.py
│   ├── embeddings.py
│   ├── db.py
│   ├── project.py
│   ├── schema.sql
│   ├── admin.py
│   └── mirror.py                    # NEW (Feature 1) — hook entry point
├── hooks/
│   └── hooks.json                   # SessionStart (deps install) + PostToolUse (mirror)
├── agents/
│   └── lodestone-curator.md         # Feature 3
├── commands/
│   └── capture.md                   # Feature 4
├── requirements.txt                 # voyageai, sqlite-vec, mcp, pysqlite3-binary, python-dotenv
├── README.md                        # user-facing install/usage
├── LICENSE
├── CHANGELOG.md
├── pyproject.toml                   # dev only — `pip install -e .` for tests/evals
├── tests/                           # dev only — not part of plugin install footprint
├── evals/                           # dev only
└── docs/                            # dev only — PRDs, design notes
    └── PRD-001-deterministic-discipline.md
```

Imports stay `from lodestone_mcp.X import Y` — only the directory location
changes (`src/lodestone_mcp/` → top-level `lodestone_mcp/`). Tests, evals,
the existing console scripts (`lodestone-mcp`, `lodestone-purge`) all keep
working without code changes.

### 6.3 MCP server bundling

`plugin.json` declares the MCP server inline (or via a sibling `.mcp.json`):

```json
{
  "mcpServers": {
    "lodestone": {
      "command": "python",
      "args": ["-m", "lodestone_mcp"],
      "env": {
        "PYTHONPATH": "${CLAUDE_PLUGIN_ROOT}:${CLAUDE_PLUGIN_DATA}/site-packages"
      }
    }
  }
}
```

When the plugin is enabled, Claude Code launches the server with stdio
transport. The `__main__.py` we add (small wrapper around `server.main()`)
makes `python -m lodestone_mcp` the entry point.

### 6.4 Python dependencies (Voyage, sqlite-vec, mcp SDK, etc.)

Plugins handle Python deps via a `SessionStart` hook that installs from
`requirements.txt` into `${CLAUDE_PLUGIN_DATA}/site-packages` on first run
(or when the requirements file changes). Documented pattern in Claude Code
docs:

```json
{
  "hooks": {
    "SessionStart": [
      {
        "hooks": [
          {
            "type": "command",
            "command": "diff -q \"${CLAUDE_PLUGIN_ROOT}/requirements.txt\" \"${CLAUDE_PLUGIN_DATA}/requirements.txt\" >/dev/null 2>&1 || (cd \"${CLAUDE_PLUGIN_DATA}\" && cp \"${CLAUDE_PLUGIN_ROOT}/requirements.txt\" . && pip install --target=site-packages -r requirements.txt)"
          }
        ]
      }
    ]
  }
}
```

The `${CLAUDE_PLUGIN_DATA}` directory is plugin-scoped, persistent across
plugin updates, and isolated from the user's site-packages. No global pollution.

### 6.5 PostToolUse hook for dual-write (Feature 1)

```json
{
  "hooks": {
    "PostToolUse": [
      {
        "matcher": "Write",
        "hooks": [
          {
            "type": "command",
            "command": "python ${CLAUDE_PLUGIN_ROOT}/lodestone_mcp/mirror.py"
          }
        ]
      }
    ]
  }
}
```

The `mirror.py` script reads the hook's stdin (Claude Code passes the tool
invocation context as JSON), determines whether the written path is an
auto-memory file, and if so calls into `lodestone_mcp.memory` directly to
upsert the row. Same Python process as the rest of the plugin — no
subprocess to a separate console script needed.

### 6.6 Subagent and slash command (Features 3 & 4)

Both ship as plain `.md` files in the plugin's `agents/` and `commands/`
directories. Claude Code auto-loads them when the plugin is enabled; no
disk install steps for the user.

### 6.7 Distribution

The repo doubles as a single-plugin marketplace via
`.claude-plugin/marketplace.json`:

```json
{
  "name": "lodestone-mcp",
  "plugins": [
    {
      "name": "lodestone",
      "source": { "source": "github", "repo": "<owner>/lodestone-mcp" }
    }
  ]
}
```

Users install with:

```bash
/plugin marketplace add <owner>/lodestone-mcp
/plugin install lodestone@lodestone-mcp
```

That's the full installation flow. Updates: `/plugin update lodestone`.

### 6.8 Versioning

`plugin.json` carries an explicit `"version": "X.Y.Z"`. Bump on each release
that we want users to pull. Git SHA versioning (omit the field) is for
internal/dev use only.

## 7. Testing strategy

| Feature | Unit tests | Integration tests | Dogfood |
|---|---|---|---|
| 1: dual-write hook | Mapping function (frontmatter → lodestone fields), upsert dedup logic | End-to-end: write a temp .md file, invoke hook script directly, assert lodestone row exists | compass5 replay |
| 2: cross-project auto-retry | `_fetch_filtered` empty local + non-empty cross; meta-shape correctness | recall round-trip via memory module against temp DB | compass5 replay |
| 3: curator subagent | n/a (markdown spec) | n/a (subagent invocation) | compass5 replay |
| 4: /capture command | n/a | n/a | manual user test |

The eval pipeline (`evals/run.py`) is unaffected by these features in v1 —
its scenarios run with `--allowedTools mcp__lodestone__*` only, so no Write
tool means no hook firing. Validation is via dogfood and unit tests.

## 8. Rollout order

Sequenced so each step is independently committable and verifiable.

0. **Repo restructure** (prerequisite, no behavior change). Move
   `src/lodestone_mcp/` → top-level `lodestone_mcp/`. Add empty plugin
   scaffolding: `.claude-plugin/{plugin.json,marketplace.json}`, empty
   `hooks/`, `agents/`, `commands/` (with `.gitkeep`), and
   `requirements.txt`. Update `pyproject.toml` packages config. Reinstall
   `pip install -e .`. Run tests + eval to confirm the move is invisible
   to behavior.
1. **Feature 2 (server-side cross-project auto-retry).** Internal-only
   change; smallest blast radius; tests cover behavior. Lands first as a
   warm-up. Includes the response-shape change to
   `{results, meta: {fallback_to_other_projects, ...}}`.
2. **Feature 1 (PostToolUse hook + plugin manifest).** This is where the
   plugin actually becomes installable. Adds:
   - `lodestone_mcp/mirror.py` (hook entry point)
   - `lodestone_mcp/__main__.py` (entry for `python -m lodestone_mcp`)
   - `hooks/hooks.json` (SessionStart deps install + PostToolUse mirror)
   - `.claude-plugin/plugin.json` (manifest with MCP server config)
   - Schema column `source_file` + migration in `db._migrate()`
3. **Feature 3 (curator subagent).** Adds `agents/lodestone-curator.md`.
4. **Feature 4 (/capture command).** Adds `commands/capture.md`.

After all four land:
5. **Prompt audit.** Trim `LODESTONE_INSTRUCTIONS` per §5.
6. **Dogfood validation.** Install the plugin in-place via
   `claude --plugin-dir .` (or `/plugin install lodestone@<this-repo>`) and
   run a compass5-style replay; verify (a) hook fires deterministically,
   (b) cross-project auto-retry produces the meta signal, (c) curator
   captures at end-of-task or via `/capture`.
7. **Marketplace publish.** Tag a release, push to GitHub, document the
   `/plugin marketplace add` flow in README.md.

## 9. Risks and mitigations

| Risk | Likelihood | Mitigation |
|---|---|---|
| Hook fires on auto-memory Write that Claude later overwrites/deletes (orphan in lodestone) | Med | Soft-delete propagation later; for v1, accept stale rows. |
| Frontmatter parser breaks on edge-case YAML | Low | Try/except in hook; log, don't crash. Schema validation at lodestone catches downstream. |
| Subagent invokes are forgotten by main Claude (Compass4-style failure mode shifts to "didn't invoke curator") | Med | `/capture` as user escape hatch; over time, prompt trims may help; ultimately accept some loss until eval coverage exists for this. |
| Subagent's output isn't visible to main Claude in a useful way | Low | Subagent returns a structured report; `Task` tool results are passed back. |
| Cross-project auto-retry surfaces noise on genuinely-local-empty queries (project-specific question with no answer anywhere) | Low | `meta.fallback_to_other_projects` lets Claude see the fallback fired and apply ASK-before-applying. |
| Coupling to Claude Code's hook/subagent/command surface means lodestone's value-add becomes Claude-Code-specific | Med | Acceptable — auto-memory itself is Claude-Code-specific; the dual-write problem is too. Other MCP clients aren't affected because they don't have auto-memory. The MCP server (`lodestone_mcp/`) remains transport-neutral by construction; nothing inside it depends on the plugin layer. |
| Hook latency (Python startup + lodestone call) blocks the user | Low | Hook should run in <1s for small writes; if it becomes slow, run async or move to a long-lived helper. |
| Schema migration breaks an existing user's DB | Low | Idempotent ALTER TABLE in `_migrate()`; same pattern as `project_label`. |
| Plugin distribution depends on a marketplace repo URL — if we rename or move the repo, existing installs break | Med | Mitigation: use a stable repo URL from day 1; document it in README. Acceptable cost for the install-with-one-command UX. |
| `requirements.txt` deps install at SessionStart could be slow on first session | Low | Subsequent sessions skip the install (diff check). First-time slowness is acceptable. |

## 10. Open questions

1. Should the hook also fire on `Edit` tool calls to memory files, or only
   `Write`? Decision in §6: only Write for v1.
2. Should the curator subagent be allowed to call `forget` on memories it
   identifies as obsolete? Lean yes (it's the right context for cleanup
   judgment) but skipping for v1 to keep the surface small.
3. Should `/capture` accept a `--dry-run` flag so the user can preview
   what the curator would store? Useful but extra scope; defer.
4. After Feature 1 ships, should we backfill `source_file` for existing
   lodestone rows by inspecting auto-memory dirs across `~/.claude/projects/`?
   Not for v1; auto-memory rows for compass1/2/3 stay unlinked to their
   files. Acceptable since we have no current need to update them via the
   hook.

## 11. Out of scope (explicitly deferred)

- Filesystem-watcher-based mirroring for hand-edits to auto-memory files
- Bidirectional sync (lodestone change → write back to auto-memory file)
- Cross-machine team sync of `~/.claude/projects/.../memory/` files
- Eval scenarios specifically testing dual-write, ASK, or capture (these
  are now enforced by mechanism, not prompt; rule-based eval doesn't
  meaningfully exercise them)
- LLM-as-judge scoring for memory content quality
