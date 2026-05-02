---
description: Review the recent conversation and capture insights worth keeping in lodestone. Optional argument $ARGUMENTS narrows the curator's focus to a specific topic.
---

Invoke the `lodestone-curator` subagent via the Task tool to review the
conversation so far and capture any durable insights worth keeping in
lodestone.

Pass the curator a brief description of what was worked on. If the user
provided a focus argument, include it: $ARGUMENTS

The curator will:
- recall first to avoid duplicating existing memories
- capture insight-shaped memories (not transcripts) with appropriate
  `kind`, `tags`, `confidence`, and `related` links per source memory
- skip the conversation entirely if there's nothing transferable to capture
- return a structured report of what was stored (or "nothing worth
  capturing" — a valid result)

Surface the curator's report to the user verbatim when it returns.
