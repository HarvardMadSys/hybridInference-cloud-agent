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
does not mount these routes. Active runs refresh on demand when the usage
drawer is opened or its refresh button is used. No usage work blocks task
completion. Repeated manual requests reuse in-flight work.

The run details drawer displays the Gateway and runtime sources separately.
Gateway input includes cache tokens; Claude's runtime input reports uncached
input separately. A difference is therefore not automatically an accounting
error. Partial metrics and missing attempts remain visible alongside sums.
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
run two tool-using turns, then retry or fail one attempt. Verify distinct turn
windows with the same saved residency grant, inclusion of retries, and source
labels. Refresh after a late log write; simulate a failed Gateway refresh and
verify cached values survive. Historical untracked turns must stay unavailable.

Until the producer is deployed, the expected state is an unsupported-schema
reason with runtime usage still readable. That is compatibility behavior, not
an end-to-end billing verification.
