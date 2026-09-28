# Conversation-first admin navigation

The admin entry point is **Conversations → conversation → turn → execution
observations**. One row represents one `agent_threads.id`, regardless of how
many runs (`agent_jobs`) the conversation contains. The list displays the safe
title, thread ID, owner, repository, current conversation state, latest agent
and model, turn counts, and last activity. CPU, memory and accounting retain
their existing execution scope in the turn detail.

The admin UI names the levels **session → turn → step**: a session is a
conversation (`agent_threads`), a turn is one user message and the agent work
it starts (`agent_jobs`), and a step is one model request, listed as LLM
requests on the turn page. Only the UI labels changed; the database and the
API keep `thread`, `job` and `conversations`.

Attempts are not a level of the admin. A turn normally runs once; the
platform starts another run only when one stops reporting or cannot start.
The turn page shows the latest run's server, times and resource measurements,
lists the LLM requests of every run in the order the runs happened, and says in
one line when a turn was restarted and how each earlier run ended.

The admin's **Activity** tab lives at `/admin/activity`. The previous
`/admin/sessions`, `/admin/conversations` and `/admin/runs` entries render the
same page, preserving existing bookmarks and usage links; one session's page is
`/admin/sessions/{thread_id}`. The tab switches between three levels of one
selection:
**Sessions** (one row per session with a matching turn), **Turns** (one row per
matching turn, from every session, newest first) and **Steps** (one row per
model request of those turns, newest first). All three share the same window,
result and linked filters, so switching changes what a row stands for, not
which turns match. `?level=turns` or `?level=steps` opens that level, and the
former `/admin/runs/all` table redirects to Turns. Existing
`/admin/runs/{job_id}` links and APIs continue to work; a turn detail links
back to its session with the originating selection.

## Steps

The gateway lists requests one grant window at a time; there is no read
across turns. `GET /admin/agent/steps` therefore reads a page of turns with
the runs list's own filters, order and cursor (five turns by default, at most
ten), then each turn's stored references, and asks the gateway for up to 50
requests per run, four reads in flight at once. The rows are merged newest
first; the order is exact within a page, and a long turn on a later page can
hold requests newer than a page's last row.

Every turn read is listed with its coverage. `partial`: a run had more than 50
requests, or the turn more runs than a page reads; the turn's own page lists
everything. `unavailable`: a run's requests could not be read, with the stored
reference's reason or `gateway_unavailable`. A run that never started is
skipped, as on the turn page. A page above the 256 KiB admin response ceiling
is refused whole rather than trimmed. Like the per-turn request page, the read
writes nothing and renews no grant.

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
