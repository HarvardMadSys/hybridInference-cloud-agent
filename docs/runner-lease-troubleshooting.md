# Runner lease failures

A retained sandbox may still exist after the worker driving its turn loses its
lease. Start with the attempt records and runner logs: container presence alone
does not prove that the native session can safely continue.

## Lease timing and retries

The lease and execution timeout govern different things:

| Setting | Default | Purpose |
| --- | --- | --- |
| Attempt lease | 120 seconds | Fences the worker's authority to write. |
| Heartbeat interval | 30 seconds | Renews that authority on a separate thread. |
| Heartbeat HTTP timeout | 10 seconds | Limits each renewal request. |
| Retry delay after heartbeat failure | 5 seconds | Retries while the current lease remains valid. |
| Agent execution timeout | 3,600 seconds | Limits the turn itself. |
| Ordinary control RPC timeout | 30 seconds | Default for other control requests. |

A successful heartbeat restores the normal interval. HTTP 409 rejects the
lease permanently: renewal and execution stop, and retries cannot restore the
expired owner. Renewal does not depend on model output, tool activity or
progress events, so a quiet call can span many lease periods.

With the first heartbeat at 30 seconds and repeated ten-second read timeouts,
retry requests start at 30, 45, 60, 75, 90 and 105 seconds after claim. This
illustrates opportunities to reconnect before the original 120-second lease
ends; it is not a wall-clock guarantee. HTTP timeout limits apply separately to
connection, pool, write and read operations. An outage longer than the remaining
lease still loses the lease.

The regression suite drives a silent six-minute turn through the real heartbeat
thread, stream loop and HTTP client against a simulated lease endpoint. It
covers healthy renewal and timed-out reads followed by recovery at 100 or 105
seconds. The previous cadence loses the latter case. These tests reproduce a
failure mechanism; they do not establish the cause of a historical incident.

## Diagnose from positive evidence

Distinguish a renewal-request timeout from an execution timeout, explicit
cancellation or agent exit. `lease_expired` proves only that the database did
not persist a timely renewal. Missing error events do not prove the worker was
healthy: the control outage that blocks renewal can also block error reporting.

Use these ordinary process-log records:

| Record | Evidence |
| --- | --- |
| `agent_attempt_claimed` | Job, attempt, worker and host identifiers. |
| `agent_control_failure` | Failed operation/stage, job ID, exception class and HTTP status when available. |
| `agent_heartbeat_retry` | Consecutive failures, time since the last acknowledgement (or heartbeat start), and next retry delay. |
| `agent_heartbeat_recovered` | Renewal succeeded after failures. |

The failure diagnostics exclude request URLs, tokens, response bodies and
exception messages. The heartbeat's exception class/status are logged once per
outage; retry records carry the changing count and timing.

Correlate logs with the attempt's persisted `lease_owner`, `lease_expires_at`
and event timestamps. Collect runner and control-plane logs outside their
containers: replacing a container can delete its Docker log history. The runner
does not configure an external collector or recover deleted logs.

An `attempt_superseded` event does not establish that another attempt ran.
Retained sessions can become lost without a replacement, including when
activation ownership is uncertain during preparation. The UI announces another
attempt only when its events exist.

## Reporting and acknowledgement failures

Error-event upload and the terminal `finish` request are independent. The
runner attempts the terminal transition even if event upload fails. If both
remain unavailable, the reaper is the final backstop.

Post-claim preparation outside the shared execution handler, such as image
inspection or manifest validation, has an outer failure handler too. It attempts
a terminal failure before stopping heartbeat renewal and logs the exception
class even if reporting fails. An explicit lease rejection skips that write.
This records early setup failures without repairing their cause.

After supervisor and control-plane settlement both succeed, a failed upload of
the final `shared_workspace_settled` notice does not fail the turn: the runner
still submits its successful terminal state. Lease rejection prevents that
write. Earlier settlement failures remain failures.

Admission and settlement receipts allow **up to three attempts**, using
ten-second HTTP timeouts and five-second delays. Only transport failures and
HTTP 408/502/503/504 are retried; 409/410 are definitive. The prompt is sent
exactly once, after its admission acknowledgement succeeds.

Supervisor settlement retains the last token's receipt until another activation
takes ownership. Control-plane settlement returns the recorded boundary only
for the same still-live attempt and activation. Repeating it does not increment
the workspace epoch, append a snapshot or change the native turn ordinal. A
sandbox manager restart loses its in-memory receipt, so uncertain completion across a
restart fails closed.

## When a retained conversation can continue

A reported preparation failure **before admission** can leave an existing
conversation usable. The runner abandons the activation and reports a failed
turn; the next message can resume the retained native session. This requires a
known retained container. Failure before its identity was recorded does not
provide that proof, and error-event availability must not gate the report.

An admission request with no received acknowledgement is different: the
database may have committed it even though the runner has not sent the prompt.
The membership then records the job as admitted. `abandon` only releases
pre-admission turns (`admitted_job_id IS NULL`), so it cannot release this
ambiguous turn; the terminal transition marks its context LOST. The runner never
replays an unconfirmed admitted prompt.

Recovery after an expired activation or killed runner requires a further
protocol: durably quarantine the member, fence the old runner's preparation
commands and sandbox manager token, verify the exact retained container and native turn
boundary, then resume that session or report it unrecoverable. A sandbox manager
`busy=false` observation is insufficient because workspace setup commands run
outside its agent-turn transport. Never requeue a turn solely because its
admission field is empty, clear a lost flag, or replay an admitted prompt to
repair a display state.

Owner-facing wording describes interrupted responses and unavailable
conversations in plain language. Infrastructure details stay in API records and
operator logs; task errors and assistant messages remain intact. Historical
LOST memberships remain disabled after wording changes.

## The safe release case: a child never accepted its turn

The host runs each turn in a child process. It can release a claim back to the
queue only when it can prove that child never accepted the turn:

- Process creation failed.
- Payload delivery failed.
- The child closed its handover descriptor without accepting, including a
  failed read-only Docker/network preflight before acceptance.

A silent child that remains alive is not proof and is never released. After
acceptance, crashes, kills and lost acknowledgements belong to the lease
protocol; the conversation may become lost instead of repeating the turn.

Release uses `POST /v1/agent/worker/jobs/<job>/release` with that turn's worker
token. The control plane requires the same still-running attempt and lease
generation **and** no membership that admitted the job. The database statement
enforces the admission condition, so an incorrect host observation cannot
requeue admitted work.

Consecutive launch failures stop claims on that host and back off. Heartbeats
report `launch_error`, which appears beside the host on Admin → Servers.
