# Runner lease failures

A retained sandbox can still exist when the worker driving its current turn
loses its lease. The default lease lasts 120 seconds. Healthy workers renew
every 30 seconds; after a failed heartbeat they retry after 5 seconds instead
of adding another 30-second sleep to the HTTP request's timeout. A successful
renewal restores the normal interval. A rejected lease (HTTP 409) always stops
renewal and execution; retries do not resurrect an expired owner.

Two dropped heartbeat requests can each consume their 30-second read timeout.
With normal sleeps between failures, the next request would start 150 seconds
after claim, too late for an unrenewed lease even if connectivity returned at
100 seconds. The shorter retry interval allows that case to renew at 100 seconds.
It cannot preserve a lease through an outage longer than the remaining lease.

The lease is not the agent's execution timeout (default 3,600 seconds). A quiet
tool or model call may span many lease periods: renewal runs on a separate
thread and does not depend on message/progress events. The regression suite
exercises a silent six-minute turn with the real heartbeat thread, stream loop
and HTTP client against a simulated lease endpoint. It succeeds both with a
healthy connection and with two timed-out reads followed by recovery at 100
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

Runner logs now include:

- `agent_attempt_claimed`: job, attempt, worker and host identifiers.
- `agent_control_failure`: the failed operation/stage, job identifier,
  exception class and HTTP status when available. Request URLs, tokens,
  response bodies and exception messages are excluded from these diagnostics.
- `agent_heartbeat_retry`: consecutive failures, time since the last
  acknowledgement (or heartbeat start) and the next retry delay.
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
