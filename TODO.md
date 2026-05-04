# TODO — Lodestone release path

Open work items in order. Check off as we go.

## 1. Rename the project to `lodestone`

Set up the umbrella so future sibling plugins (lodestone-X) can live under
the same marketplace.

- [x] Created `claudevandort/lodestone` on GitHub via
      `gh repo create --public` (fresh create; no prior `lodestone-memory`
      repo to rename).
- [x] Phase 2 — renamed `~/Github/lodestone-memory` → `~/Github/lodestone`,
      rebuilt venv, updated `.mcp.json` (still uncommitted in working tree),
      reopened Claude Code from the new dir, `pytest -q` passes 114/114.
      Lodestone memories carried over because `project_id` is derived from
      the git remote URL (unchanged). Old session log dir
      `~/.claude/projects/-home-...-lodestone-memory/` is now dead weight
      — safe to delete or leave.
- [x] Added remote `https://github.com/claudevandort/lodestone.git`
      (HTTPS via `gh auth setup-git`, not SSH; see "Environment notes" at
      the bottom for why).
- [x] Manifests verified pointing at `claudevandort/lodestone`
      (`marketplace.json` source.repo confirmed pre-push).

## 2. Manually test the plugin (local plugin-dir install)

Run this BEFORE §4 — `--plugin-dir` loading exercises the same plugin
artifacts that the marketplace path will. If anything breaks here, it's
the plugin itself, not the install path.

Tests pass (118 ✓) but those don't cover the end-to-end
Claude-Code-loads-the-plugin path.

**Bugs found and fixed during §2 (2026-05-04):**

1. **Cold-cache race condition.** First launch of `--plugin-dir` against a
   never-populated `${CLAUDE_PLUGIN_DATA}` showed `✘ failed` because Claude
   Code starts the MCP server in parallel with the SessionStart hook, and
   the server's `import mcp` lost the race. Diagnosed via
   `~/.cache/claude-cli-nodejs/<encoded-cwd>/mcp-logs-plugin-*/<ts>.jsonl`.
   Fixed in `lodestone_memory/__main__.py` with a synchronous self-heal
   (pip-install if the marker is missing, BEFORE importing `.server`).
   SessionStart hook stays as a warmup. Tests added.

2. **Plugin manifest `${VOYAGE_API_KEY}` substitution leaves a literal
   string when shell var unset.** Symptoms: `recall`/`remember` failed with
   "Provided API key is invalid" even though the key in `~/.lodestone/.env`
   was valid. Root cause: Claude Code's manifest substitution doesn't drop
   or empty an unset `${VAR}`; it passes the literal string. `load_dotenv`
   (default `override=False`) then refuses to fill from .env files. Fixed
   in `lodestone_memory/{server,mirror}.py` by sanitizing
   `VOYAGE_API_KEY` (strip empty / strip `${`-prefixed) BEFORE the dotenv
   chain runs. Tests added covering sanitize + preserve-real-key paths.

**Bugs surfaced but NOT fixed (logged for follow-up):**

3. **`remember` returns an error when embedding fails, but the DB row
   commits anyway.** When the Voyage embedding step fails (e.g. the key
   bug above), the memory is already in the `memories` table by the time
   the embedding error surfaces; users see a tool error and may retry,
   producing duplicates. Should make `remember` resilient: store the
   memory, attempt embedding, return success-with-warning on embedding
   failure (FTS still works without the vector). Discovered via the
   curator subagent's diagnostic in the §2 smoke test session.

- [ ] In a fresh project dir (NOT this repo), launch:
      `claude --plugin-dir /home/claudevandort/Github/lodestone`
- [ ] Confirm `/mcp` shows the lodestone server connected
- [ ] Run the preflight: `ToolSearch(query="lodestone")` should surface
      all six tools under the `mcp__plugin_lodestone-memory_lodestone__*`
      prefix (this is the rename-sensitive part)
- [ ] Smoke test:
  - [ ] `recall("...")` against an empty project — should auto-fallback
        cross-project and report `meta.fallback_to_other_projects: true`
  - [ ] `remember(kind="gotcha", title="...", content="...")` round-trips
  - [ ] Trigger the auto-memory dual-write path. Note: "save a feedback
        memory" prompts make Claude call `remember` directly, not Write to
        `~/.claude/projects/<encoded-cwd>/memory/*.md`. To exercise the
        PostToolUse hook deterministically, ask Claude to use the Write
        tool to create that file with valid YAML frontmatter (kind,
        title, description), then `list_recent` and confirm a row with
        the expected `source_file` appears.
  - [ ] `/remember` spawns the curator subagent and returns a structured
        report
- [ ] If anything breaks: fix, re-test, then proceed to step 4.

## 3. Publish

- [x] Renamed branch `master` → `main` locally
- [x] `git push -u origin main` (commits 40745c8 + 02421af landed)
- [ ] Tag a release: `git tag v0.1.0 && git push --tags` (holding until
      step 4 confirms the marketplace install path works end-to-end; no
      point versioning a release we haven't validated)
- [x] GitHub repo is public (created with `gh repo create --public`)
- [ ] Sanity-check README renders well on github.com (visual eyeball)

## 4. Install from marketplace and re-test

Confirm the install path actually works for an end user — different from
local plugin-dir loading because deps install via the SessionStart hook
into `${CLAUDE_PLUGIN_DATA}/site-packages` rather than the local venv.

- [ ] Uninstall the local plugin-dir version if it's still loaded
- [ ] Run the README's two install commands in a fresh Claude Code session:
  - `/plugin marketplace add claudevandort/lodestone`
  - `/plugin install lodestone-memory@lodestone`
- [ ] Watch the SessionStart hook install deps (30–60s on first session).
      If pip install fails, capture the error before iterating.
- [ ] Re-run the smoke test from step 2 against the marketplace install
- [ ] Verify `VOYAGE_API_KEY` is being picked up via the shell-export path
      (Option 1 in README) — this is the new code path we added

## 5. Record demo video

- [ ] Decide format: screen recording with voice-over (most natural for a
      Claude Code plugin) vs. silent screencast with captions
- [ ] Plan 3–4 scenes — suggested:
  1. Install (the two `/plugin` commands)
  2. `recall` surfacing a relevant prior insight unprompted
  3. `remember` capturing a gotcha mid-session
  4. `/remember` end-of-session curator wrap-up
  5. (Optional) Cross-project fallback in a fresh repo
- [ ] Keep total length 60–90s for social platforms
- [ ] Tooling: OBS, Loom, or QuickTime; export to mp4

## 6. Post for exposure / feedback

- [ ] Draft the post copy. Hooks worth trying:
  - "I built a memory plugin for Claude Code that finally remembers what
    we tried last week."
  - "Claude Code's auto-memory is great. I made it searchable across
    sessions and projects."
  - Lead with the problem (forgetting hard-won lessons), end with the
    install command.
- [ ] Tailor per platform: LinkedIn (longer, story-shaped), X (one
      strong line + video), Threads (somewhere in between)
- [ ] Pin the install commands and the GitHub link in the first reply
- [ ] Schedule the post for a weekday morning (US time) for max engagement

## Out of scope for now

- Restructure `plugins/` subdirectories — defer until a second plugin
  has a concrete shape. The current layout works for one plugin.
- LLM-as-judge eval scenarios for memory-content quality — current
  rule-based eval is enough until usage tells us otherwise.
- Backfilling `source_file` for pre-rename auto-memory rows.

## Environment notes

- **Pushing to GitHub uses HTTPS, not SSH.** The local SSH key
  `~/.ssh/id_rsa` is passphrase-protected and no `ssh-agent` is loaded
  in this environment, so any `git push` over `git@github.com:...` hangs
  on a missing `ssh-askpass`. Workaround applied once via
  `gh auth setup-git`, which sets git's credential helper to use gh's
  stored OAuth token over HTTPS. Future pushes from this repo "just
  work"; future fresh clones will need the same setup step.
