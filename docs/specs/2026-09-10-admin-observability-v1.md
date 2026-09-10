# Admin activity and resource observability

Status: implementation scope agreed on 2026-09-10.

## Purpose

Administrators need to inspect agent activity, inference usage, and resource
measurements across task owners. The first release adds a cross-user reading
surface over data the Agent already owns. It does not add a troubleshooting
workflow, inferred failure categories, queue diagnostics, or retry analysis.

The navigation is **Admin → Runs → a task's activity, usage, and resources**.
Servers remains available. A cross-task Usage page is a separate release.

## First release

- List tasks across owners, defaulting to the last seven days, with bounded
  pagination and filters for time, owner, state, runtime, model, host, and repo.
- Identify tasks using their id, stored title, repository, owner id, runtime,
  model, recorded state, and creation time. Do not fetch full prompts or make
  one Gateway identity lookup per row.
- Inspect runtime activity and tool events within a task. Keep attempt identity
  for attribution and pagination; it is not a new retry-analysis interface.
- Show existing Gateway and runtime usage summaries with the same accounting
  rules as the owner surface. Keep the sources separate, including freshness,
  attribution windows, missing values, and incomplete accounting.
- Read one selected attempt's existing sandbox, tool, and process measurements
  on demand. Report their source and completeness rather than filling gaps
  with zero or synthesizing whole-run totals from child processes.
- Reveal a single event's recorded content only on an explicit administrator
  action, with a durable audit record and a bounded response.

Existing states and tool error flags remain useful observations. Raw recorded
errors may be revealed as content; this does not introduce failure inference.

## Data semantics

### Activity is not a request ledger

A runtime message is not one LLM request. A response can produce multiple
blocks with repeated usage, while a terminal record can carry an attempt total.
The existing usage summarizer preserves this distinction and response identity.
The activity surface must not count messages as model calls or derive LLM
latency from gaps between received messages.

The Agent currently persists Gateway totals snapshots. Complete per-request
Gateway records require a later contract integration and attribution work;
the activity view does not claim to provide them.

Event sequence numbers are local to an attempt. Pagination, ordering and tool
association include the attempt identity. A cursor belongs to its task and
filters, so switching those cannot silently skip or mix records.

### Measurements retain their scope

- Event timestamps are receive timestamps.
- Tool duration keeps its stored timing source: sandbox-hook boundaries
  (`exact`) or runtime-message boundaries (`message`). The presence of a
  duration alone does not make it exact.
- Process CPU and I/O measure attributed processes. A process memory peak is
  the largest individual process peak, not concurrent tool or sandbox memory.
- Overlapping tools, incomplete observation, missing counters and truncation
  retain their existing markers.
- Shared-member whole-sandbox measurements with zero coverage remain
  unavailable. Periodic whole-member resource collection is separate work.

## Interfaces and authorization

All new operations use the existing `/admin` authorization dependency. Owner
routes retain their ownership checks. Accounting helpers and pure presentation
logic can be shared; owner-authenticated API loaders are not reused by the
administrator page.

| Operation | Purpose |
|---|---|
| `GET /admin/agent/runs` | Filtered task metadata with a continuation cursor |
| `GET /admin/agent/runs/{job_id}` | Task metadata, attribution identities and existing usage summaries |
| `GET /admin/agent/runs/{job_id}/activity` | Bounded, metadata-only runtime events |
| `GET /admin/agent/runs/{job_id}/attempts/{attempt_id}/resources` | Existing measurements for an attempt belonging to this task |
| `POST /admin/agent/runs/{job_id}/events/{event_id}/content` | Explicit, audited read of one event's recorded content |

Default responses and queries use an explicit metadata allowlist. Removing a
few known content keys from an arbitrary payload is not a privacy boundary.
Prompts, tool arguments, outputs, diffs, raw error text and service credentials
do not belong in default responses. Generated and manually edited task titles
are intentional metadata fields and are displayed as bounded plain text.
Fallback titles quote the prompt, so SQL excludes them from both list and detail
responses. A missing or unrecognized title source also yields no title; the
page displays the task id instead.

Content access records the administrator identity, task owner, task and event
identity, and request time through the existing Agent audit store. Content is
not returned if the audit write fails. Content responses enforce a byte limit
and explicit truncation for old oversized records. Responses are not cached.

This release reads observations and snapshots; visiting a page does not trigger
a Gateway ledger refresh or mutate task execution. The explicit content audit
is the only write introduced by this feature.

## Implementation boundaries

Python models and pure conversion helpers live in a dedicated admin
observability module. Cross-user SQL lives in a separate storage query module;
new routes live in `cloud_agent/api/admin_runs.py`. Existing owner usage
calculation is shared without weakening either authorization path.

Web routes live under the existing Admin area and follow the Servers page's
layout, access guard and visual styles. The list and detail pages handle
loading, empty results, failed reads, revoked access, pagination and stale
requests. There is no inactive Usage navigation item before that page exists.

## Acceptance

- An administrator can inspect tasks belonging to different owners; a normal
  session and an unauthenticated request cannot use any of the new routes.
- Default responses contain no content canaries, including nested arbitrary
  payload fields and raw error text. Explicit event reveal is separately
  requested, audited and bounded; audit failure returns no content.
- Time windows and continuation cursors produce no duplicates or omissions
  for equal timestamps and repeated event sequence numbers across attempts.
- Task usage matches the existing accounting rules, including repeated block
  usage, completed attempt totals, multiple attempts and unavailable snapshots.
- Zero-coverage resources stay unavailable; approximate timing and incomplete
  process observations retain their meaning in the API and UI.
- Real Postgres tests exercise queries. API and UI tests exercise authorization,
  scoped loading and explicit content access. Verify the running application
  before declaring the deployed feature complete.

## Later work

Cross-task usage aggregation is delivered by Admin -> Usage under the
[Usage aggregation implementation design](2026-09-10-admin-usage-aggregation-design.zh.md),
which records its cohort, accounting, API, UI and acceptance criteria. Complete
LLM request tracing and shared-member whole-run resource measurements require
additional data collection or integration. They remain part of the broader
observability objective, not claims of this release. Additional host capacity
columns are the next reading surface. Alerting and research export can follow
after those foundations are stable.
