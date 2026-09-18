# Conversation-first admin navigation

The admin entry point is **Conversations → conversation → turn → execution
observations**. One row represents one `agent_threads.id`, regardless of how
many runs (`agent_jobs`) the conversation contains. The list displays the safe
title, thread ID, owner, repository, current conversation state, latest agent
and model, turn counts, and last activity. CPU, memory and accounting retain
their existing execution scope in the turn detail.

`/admin/conversations` is the canonical list. The previous `/admin/runs` entry
also renders this list, preserving existing usage bookmarks. The secondary
“All execution records” link opens the existing run table at `/admin/runs/all`.
Existing `/admin/runs/{job_id}` links and APIs continue to work; a turn detail
links back to its conversation with the originating selection.

## Selection and pagination

`GET /admin/agent/conversations` uses the same turn-creation cohort as the
existing Runs and Usage APIs: `[since, until)`, owner, repository, runtime,
model, turn state and host. An older conversation appears when it has a
matching recent turn. All criteria must match the same turn. Multiple
attempts cannot duplicate it.

The query groups by thread **before** applying pagination, ordered by newest
matching turn creation time and thread ID descending. The bounded cursor
keeps the time window and filters. This is live keyset pagination, not a frozen
snapshot across HTTP requests; changed turn states can change cohort membership.
The default window is seven days and the maximum is 366 days.

Each row shows the conversation's total number of turns. Its state, latest
model and last activity describe the whole conversation. State reuses the
owner surface's active-turn-first calculation: a settled latest turn cannot
hide an earlier running or queued turn. `idle` does not assert that the
user's overall task is complete.

`GET /admin/agent/conversations/{thread_id}` returns metadata and a bounded
page of turns, newest first. With no selection, it reads the entire history
without the list's default time window. With a selection, it still reads all
turns and marks `matches_filters`, so a Usage drilldown retains context without
presenting every turn as part of the original usage cohort. The cursor is
bound to the conversation and selection; older pages use the unique turn
ordinal. Fork copies are identified and link to their original executions.
Their copied history is not counted again as new inference spend.

Usage keeps its existing accounting and run counts. Only its drilldown
destination changes. No new conversation-level resource or cost total is
inferred by adding snapshots or peaks.

## Boundaries and verification

Both endpoints require current admin access, are served only by the main
listener, and return `Cache-Control: no-store`. Queries use explicit metadata
projections. Only manually edited or generated titles are exposed; fallback
and inherited title provenance remains excluded. Archived/deleted metadata
stays observable, as in the existing Runs and Usage cohorts, and is labelled.
No owner-content endpoint, gateway refresh, credential or prompt read is added.

Postgres tests cover grouping before pagination, equal timestamp boundaries,
same-turn filters, older conversations with recent turns, full history,
fork provenance, current state and safe titles. API tests cover authorization,
window validation, cursor scoping, metadata allowlists and listener isolation.
UI tests cover the original entry point, title/ID navigation, preserved usage
filters, full turn history, paging, aborted stale reads and revoked access.
