# Inference usage snapshots

The Gateway remains the authority for inference request logs. The Agent
control plane stores totals snapshots so completed jobs remain readable when
the Gateway is unavailable. Resource accounting and runtime self-reported
tokens remain separate sources.

## Attribution

The credential a retained CLI uses belongs to its residency. The temporary
`agent_attempts.grant_id` is not that credential and can be cleared during
cleanup. Migration `0026_attempt_ledger` adds an immutable reference for each
attempt and actual grant, including its residency generation and Gateway
binding identity. References are copied inside the fenced residency/admission
transaction, before the CLI receives the prompt. A new grant within the same
attempt adds another reference; re-admission of the same grant is idempotent.

Each query uses `[attempt.started_at, attempt.finished_at)`. While running,
the upper bound is the time the refresh is claimed. A following attempt's
start clamps the predecessor's upper bound. Windows do not include additional
slack or the idle time between turns. Different grants are filtered separately
by the Gateway; failed attempts and retries are included in a job's sum.

This is time-window attribution, not a causal per-request attempt label.
Gateway request start and Agent boundaries use wall clocks, which must be
synchronized. Background CLI requests that start outside an attempt's window
are not assigned to that attempt. An exact causal ledger would require a
Gateway request label propagated by the relay.

No historical grant is guessed from the current residency. Existing residency
rows have no binding marker until a new grant is minted by this version.
Attempts without a recorded reference remain unavailable, including new turns
that reuse such an older residency. They cannot become free/zero-cost turns
through a backfill. New sessions use the new attribution path immediately.

## Gateway contract

`GrantClient.usage` sends explicit `since`, `until`, and `detail=totals` to the
existing grant usage route. The mirrored contract is in
`contracts/inference-grants.openapi.yaml`; the producer is Gateway PR #1396.
The consumer rejects a successful legacy response, unsupported schema versions,
invalid metrics, and a response naming another grant or window.

`totals` sums only known values. `unknown_rows` accompanies every metric;
`estimated_calls` identifies estimates. Costs remain decimal strings.
An empty window has zero recorded calls and null token/cost metrics. None of
these snapshots is finalized: late asynchronous log writes can change it.

The grant path and service credential are configurable. Set a stable
`GATEWAY_GRANT_BINDING_ID` before the first ledger-enabled deployment: this
logical installation identity survives URL/path changes during the repo split
and ordinary credential rotation. Without it, the identity is derived from
the origin and grant path. Operators changing the installation must change
the explicit binding ID even if its URL stays the same.
Refreshing an old reference against a different binding is refused; its cached
snapshot remains readable. No credential or grant identity enters the usage UI.

## Refresh and presentation

A dedicated collector runs independently of grant renewal and the reaper,
with at most four HTTP reads in flight and a timeout on each read. It observes
terminal attempts durably, then makes two further reads once the attempt has
been finished for 30 seconds and five minutes to pick up late rows. Manual
reads before a boundary can update the cache but do not consume that later
reconciliation. Eligibility uses the query's claimed start time, so a slow
early response cannot satisfy a later stage. Failures at due stages consume
these bounded automatic retries too. A crashed collector's lease can be reclaimed;
an expired or superseded refresh cannot overwrite a newer result.

Owner endpoints are:

- `GET /v1/agent/jobs/{job_id}/usage`: cached ledger snapshots and runtime
  usage for this job, including all its execution attempts.
- `POST /v1/agent/jobs/{job_id}/usage/refresh`: enqueue a throttled refresh and
  return cached data immediately. It never waits for the Gateway.

Both check job ownership before reading or queueing anything. The MCP listener
does not mount these routes. While the job is displayed, active runs request
fresh usage periodically, with backoff on transient failures. Polling reads
the resulting cache and follows delayed terminal refreshes; it does not
equate five elapsed minutes with final settlement. No usage work blocks task
completion. Repeated requests reuse in-flight work.

The owner chat has no Run details entry or drawer. A small usage summary below
the current assistant reply shows this job's model calls and input/output tokens
from Gateway snapshots. If there is no event reply, it uses the current job's
durable reply, or appears at the end of the run when neither is available.
One job-level hook owns fetching and polling, so new streaming messages do not
restart accounting requests. The current aggregate is never attached to an
unrelated historical reply or a superseded attempt. Historical rows do not
issue additional usage requests, and a sibling's conversation refresh does not
keep a settled job polling.

Active or scheduled refreshes are labelled using their actual state; incomplete
coverage uses a missing-usage message, without guessing how many turns are
missing. Missing reasoning alone does not produce a warning. Conversation
totals, cost estimates, token breakdowns, update timestamps, configuration tables,
and diagnostic disclosures are not shown in the owner chat. The API retains
those fields for diagnostic clients. The event API and stored execution history
also remain available.

Successful replies have no redundant completion badge or Outcome heading.
Existing View changes and PR actions remain in chat, with the full diff in the
Git workspace. Empty workspaces have no View changes button or absent-PR
placeholder. Failure reasons, recovery guidance, publication status, and blocked
network warnings remain visible in chat; existing denial events do not acquire a
duplicate aggregate warning.

Only when Gateway totals are absent does the summary fall back to agent-reported
tokens, with a source label and no invented cost or call count. The additive
`runtime.normalized_input_tokens` projection includes cache reads and writes
for Claude and pi, whose raw input excludes both. Codex input already includes
cache hits. Missing required counters or unknown runtime conventions leave
that projection null; original runtime counters remain unchanged.

The additive `conversation` field summarizes cached snapshots from jobs in the
same owned thread, using one database read and no Gateway fan-out. Forks have
separate thread IDs even when sharing a workspace. Incomplete coverage is
labelled, and the oldest contributing timestamp plus scheduled refresh state
remain available. Conversation totals never substitute runtime data into a
missing Gateway window.

`available` means a readable snapshot, not complete or settled billing.
The aggregate refresh timestamp is the oldest contributing snapshot's timestamp.
Failed refreshes preserve the last successful values and timestamp and mark
them cached. Runtime totals are computed only from this job's events, never
from sibling turns in the thread.

This first phase persists totals only. Request details remain at the Gateway;
there is no offline request-by-request copy or admin-wide aggregation here.

## Staging acceptance

Merge and deploy the Gateway producer before interpreting new snapshots.
After the Agent PR is merged and deployed, start a new retained session and
run two tool-using turns, then fail or cancel one. Verify distinct turn
windows with the same saved residency grant and inclusion of failed/cancelled
usage. Admitted retained turns are not automatically replayed; historical
attempt aggregation remains supported. Refresh after a late log write; simulate a failed Gateway refresh and
verify cached values survive. Historical untracked turns must stay unavailable.

Until the producer is deployed, the expected state is an unsupported-schema
reason with runtime usage still readable. That is compatibility behavior, not
an end-to-end billing verification.
