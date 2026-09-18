# Deployment failure checks and queue monitoring

The exact-SHA fence remains mandatory. The control-plane waiter keeps its
900-second cold-build budget; host-local checks retain 120 seconds after the
build. During staging's read-only readiness wait, `deploy/watch_peer.py` reads
the **current run attempt's** runner-host job with an Actions read token. A
failed/cancelled host job stops that wait and removes only its named one-off
container. An unavailable Actions API leaves the ordinary bounded fence in
charge; it does not authorize activation. Migrations and service replacement
are never interrupted by this watcher. Source deployments outside Actions
continue to use the bounded readiness check directly.

## Before stopping a serving runner

Both source and artifact host deployments resolve the candidate runner image
through Compose and run `cloud_agent_host.candidate_check`. Its container has
no network, enrollment state, broker token or live stack mounts other than the
Docker socket. The production preflight creates and removes its own temporary
guest with network `none`, exercises kernel isolation and the guest observer,
then compares the **built image's** runtime/helper labels to the candidate's
pins. A failure leaves the old marker and runners alone. The checks on the
actual replacement runners, relays and broker still run before mark-ready;
their failure uses the existing recorded-generation recovery.

CI additionally runs the same candidate check on the real Kata host for
reviewed commits on `dev`, before `CI Gate` can release the staging workflow.
It builds uniquely tagged candidate images and never invokes Compose on the
deployed stack. PR code is not sent to this machine by this job. Static
cross-artifact checks run on PRs too. A local Docker-only test is not evidence
that the real Kata check passed; the named CI job supplies that evidence.

## Old process, candidate schema

The Postgres CI job resolves the latest `agent-*` release tag reachable from
the change's base and runs `scripts/check_rolling_upgrade.py`. It creates a
random `rolling_test_*` database, migrates it with the previous release's own
Alembic graph, starts that release's storage code and warms its pool with real
conversation reads/writes. While the process stays alive, the candidate
migration runs, then those operations run again on the same database backend.
An idempotent migration retry is tested too. The database and archived source
are removed afterward. This guard covers common storage paths; it is not a
substitute for authenticated browser tests or every historical release.

Example against a disposable local Postgres:

```bash
TEST_DB_HOST=127.0.0.1 TEST_DB_PORT=5432 \
  uv run python scripts/check_rolling_upgrade.py --base agent-<previous-sha>
```

## Detection after deployment

The main API owns one queue monitor; the MCP listener does not. It reports
ten-minute-old queue heads after two consecutive minute-spaced observations:

- `queued_without_host`: no enrolled, fresh, compatible host for some waiting
  work, honoring retained ownership and the new-chat placement pin.
- `queue_not_starting`: old queue heads remain and no job is running, even
  though host heartbeats may still look healthy.
- `queue_health_unavailable`: the database observation repeatedly fails.
- `queue_recovered`: a reported problem cleared.

Normal follow-ups behind an earlier turn are excluded. Busy slots with running
work are normal backpressure, not an outage. Detection does not claim jobs,
restart hosts, relax isolation, or alter scheduling. A dead API process cannot
run its own monitor: existing external process/HTTP monitoring remains needed.

Events are always logged. For notifications beyond GitHub's normal failed-run
notifications, configure an operator-owned receiver:

- Control-plane `.env`: `AGENT_ALERT_WEBHOOK_URL` (runtime queue monitoring).

It sends JSON with `text` and `event`; runtime notifications add
`deployment_sha` and aggregate `queue` counts. Deployment workflow alerting is
owned by the downstream distribution.
The receiver must accept this JSON contract; a chat provider may need a small
adapter. No prompts, repository names, job IDs, host credentials or log dumps
are sent. HTTP redirects are refused. Delivery errors do not affect requests
or the reaper, and are logged without exposing the webhook URL. Runtime sends
on state changes and at most once every 30 minutes while unchanged; failed
delivery retries next minute. Secrets/recipients must be configured by the
deployment owner before claiming external alerts are active.

## Recovery boundaries

A failed host rollout still reports failure even if it restored the recorded
generation. A control-plane failure leaves backwards-compatible migrations in
place; there is no automatic database downgrade. Inspect both deployment
markers and the run's failure reason, then retry the same verified target.
If the host succeeded and the control plane did not, the exact-SHA gate can
leave new work queued until that retry succeeds; the queue monitor detects
that condition. These safeguards reduce and expose interrupted deployments;
they do not implement a transaction spanning two machines and PostgreSQL.
