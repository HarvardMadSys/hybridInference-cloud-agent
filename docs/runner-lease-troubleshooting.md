# Runner lease failures

A retained sandbox can still exist when the worker driving its current turn
loses its lease. The default lease lasts 120 seconds. Healthy workers renew
every 30 seconds; after a failed heartbeat they retry after 5 seconds instead
of adding another 30-second sleep to the HTTP request's timeout. A successful
renewal restores the normal interval. A rejected lease (HTTP 409) always stops
renewal and execution; retries do not resurrect an expired owner.

Heartbeats now have a separate 10-second HTTP timeout. Ordinary control RPCs
retain their 30-second default. With a 30-second first heartbeat and repeated
10-second read failures, retry requests start at 30, 45, 60, 75, 90 and 105
seconds after claim. This leaves more chances to reconnect inside the original
120-second lease. These are read-timeout examples, not a wall-clock guarantee:
HTTP timeouts apply separately to connection, pool, write and read operations.
An outage longer than the remaining lease still cannot be hidden.

The lease is not the agent's execution timeout (default 3,600 seconds). A quiet
tool or model call may span many lease periods: renewal runs on a separate
thread and does not depend on message/progress events. The regression suite
exercises a silent six-minute turn with the real heartbeat thread, stream loop
and HTTP client against a simulated lease endpoint. It succeeds both with a
healthy connection and with timed-out reads followed by recovery at 100 or 105
seconds; the original retry cadence loses the latter turn's lease. This is a
controlled reproduction, not proof of the cause of a historical incident.

Classify historical failures using positive evidence. An execution-timeout
error, an explicit cancellation, or an agent exit is different from a renewal
request timeout. `lease_expired` alone proves only that no timely renewal was
persisted. Missing error events do not prove the worker remained healthy: the
same control outage can prevent both renewal and error reporting.

When execution fails, error-event upload and the terminal `finish` request are
independent. Failure of the event endpoint must not skip the terminal request
and leave the database recording a running attempt. If both control requests
remain unavailable, the reaper is still the final backstop.

Once both supervisor and control-plane settlement have succeeded, failure to
upload the final `shared_workspace_settled` notice also must not fail the turn:
the worker still submits its successful terminal state. Lease rejection is
handled separately and prevents that write. Earlier settlement failures are
not treated as successful completion.

Admission and settlement acknowledgement loss is also recoverable. The runner
retries those receipts up to three times, with 10-second HTTP timeouts and
5-second delays, only for transport failures and HTTP 408/502/503/504. A prompt
is sent exactly once, after its admission receipt succeeds. Supervisor
settlement remembers the last token's receipt until another activation takes
ownership. Control-plane settlement returns the recorded boundary only for the
same still-live attempt and activation; it does not increment the workspace
epoch, append another snapshot, or change the native turn ordinal. A 409/410
is definitive and never retried. A broker restart loses its in-memory receipt,
so an uncertain completion across that restart remains fail-closed.

Post-claim preparation outside the shared execution handler (for example,
image inspection or manifest validation) also has an outer failure handler.
It attempts a terminal failure before stopping heartbeat renewal, and records
the exception class even if terminal reporting fails. An explicit lease
rejection skips that write. This prevents an early setup error from silently
becoming an expired running attempt; it does not repair the setup error itself.

Runner logs now include:

- `agent_attempt_claimed`: job, attempt, worker and host identifiers.
- `agent_control_failure`: the failed operation/stage, job identifier,
  exception class and HTTP status when available. Request URLs, tokens,
  response bodies and exception messages are excluded from these diagnostics.
- `agent_heartbeat_retry`: consecutive failures, time since the last
  acknowledgement (or heartbeat start) and the next retry delay. The exception
  detail is recorded once per outage; this line carries the per-retry count.
- `agent_heartbeat_recovered`: a previously failing renewal succeeded.

Correlate these records with the attempt's persisted `lease_owner`,
`lease_expires_at` and event timestamps. Capture runner/control-plane logs
outside their containers: these diagnostics use ordinary process logging,
and replacing a container can delete its original Docker logs. This change
does not configure an external log collector or recover deleted history.

An `attempt_superseded` event does not prove that a replacement ran. Retained
sessions can end as lost without a second attempt, including uncertain
activation ownership during preparation. The UI only announces another
attempt when its events actually exist. Do not clear a lost flag or replay
an admitted prompt to repair this display state; native-session recovery
still requires proof of a safe execution boundary.

Before prompt admission, a reported preparation failure can leave an existing
conversation usable: the runner abandons the activation and reports a failed
turn, allowing the next message to resume the retained native session. This
requires a known retained container; failure before its identity was recorded
is not equivalent proof. Error-event availability must not gate this report.

Losing an admission acknowledgement outright is not the same as failing before
admission. A retried admission that never receives an acknowledgement can still
have committed, and the prompt is only sent after that acknowledgement. The
membership then records the job as admitted while no prompt ever reached the
CLI. `abandon` only releases a pre-admission turn (`admitted_job_id IS NULL`),
so that request does not take effect and the terminal transition marks the
context LOST. This is deliberate: an admitted-but-unconfirmed turn is
ambiguous, and the runner never replays it. Only the pre-admission case above
can keep a conversation usable.

The owner UI describes interrupted responses and unavailable conversations in
plain language. Original infrastructure details remain in API records and
operator logs. Task errors and assistant messages are not rewritten. Historical
LOST memberships remain disabled; changed wording does not repair their state.

Recovery after an expired activation or killed runner needs an additional
protocol: durably quarantine the member, fence the old runner's preparation
commands as well as its broker token, verify the exact retained container and
native turn boundary, then either resume the same session or report that it
cannot be recovered. A broker `busy=false` observation alone is insufficient:
workspace setup commands run outside the broker's agent-turn transport. Never
requeue a pre-admission turn solely because its admission field is empty.
