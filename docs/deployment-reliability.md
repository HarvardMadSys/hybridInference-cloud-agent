# Deployment failure checks and queue monitoring

Deployment checks protect the serving generation and detect work that remains
queued after a rollout. Every activation must pass the exact-SHA readiness
fence. The control-plane wait has a 900-second budget for cold host builds;
host-local checks retain a 120-second budget after the build.

When an Actions deployment sets `DEPLOY_PEER_JOB`,
[`deploy/watch_peer.py`](../deploy/watch_peer.py) supervises only the read-only
readiness wait. It reads the peer host job from the **current run attempt** with
an Actions read token. A failed or cancelled job ends the wait and removes its
named one-off container. If the Actions API is unavailable, the bounded
readiness fence continues; activation still requires success. The watcher never
interrupts migrations or service replacement. Deployments outside Actions use
the bounded check directly.

## Drain through both activations

A single-host rollout that must not interrupt work uses one explicit host as a
database-backed admission fence. The orchestrator runs on the control-plane
machine, outside the pool it is draining. It first reads the host's current
owner token and canonical `drain_fence_epoch`, then asks the API to replace that
exact pair with a fresh opaque owner token. The database accepts the change only
if both observations are still current and rotates the epoch as it commits.

The orchestrator then waits until that exact host still reports `draining` and
`slots_used=0`. A claim that committed just before Drain is allowed to finish;
no later claim can commit while the status remains draining. The host is
activated while the fence stays in place. Control-plane activation sets
`AGENT_DEPLOY_DRAINING_HOST` to the exact enrollment name, which authorizes the
deployment readiness check to accept only that named draining host. The check
requires zero occupied slots, the target deployment capability, a fresh
heartbeat and a successful reverse broker probe, then rereads the same
conditions after the network await. Ordinary readiness remains online-only.

Only a normal fenced release holding the exact active owner token may put the host online.
An identical acquire or release retry is idempotent, including after a lost
success response. A successor may transfer ownership only from the exact token
and epoch it observed; an operator action, lifecycle or credential change,
placement change, or competing rollout rotates the epoch and makes the stale
request fail. The database also refuses deletion of a host while its fence is
active. Deployment remains serialized for the rest of the activation protocol,
but drain safety no longer depends on that serialization or on operators
leaving the row untouched.

On cancellation or an uncertain result, fail closed: leave the host draining
and retry with the same verified owner. Queued work waits; an in-flight turn is
never killed or replayed. Migration `0051_host_drain_fence_epoch` and the
`AGENT_DEPLOY_DRAIN_FENCE_V2` recipe marker are the rollback floor after V2
activation. The V1 marker remains only for an older orchestrator performing the
bridge upgrade.

## Before stopping a serving runner

Source and artifact deployments resolve the candidate runner image through
Compose, then run `cloud_agent_host.candidate_check` before stopping the old
runners. The candidate container has no network, enrollment state, broker token
or live stack mounts other than the Docker socket. It creates and removes its
own temporary guest on network `none`, verifies kernel isolation and the guest
observer, and compares the **built image's** runtime/helper labels with the
candidate's pins.

The stop signal drains the host service itself: it stops claiming and waits for
every tracked turn child to exit without forwarding SIGTERM to those children.
Each child owns exactly one turn and exits naturally when that turn settles.
Turn children also install their drain handler before preflight as protection
against an out-of-band signal, but deployment does not rely on that fallback.

If that check fails, the old readiness marker and runners remain in place. The
replacement runners, relays and broker must still pass their live checks before
`mark-ready`; a later failure uses the recorded-generation recovery path.

### Host runtime qualification

The host service performs the full Kata probe at startup, whenever the sandbox
image or root-owned runtime epoch changes, and at least every 30 minutes. This
last interval bounds detection of changes made outside the supported installer.
A Docker restart also restarts the runner, which probes again.

The installer writes an `updating` epoch before changing runtime files and a new
`ready` generation only after verification. Updating or malformed evidence
blocks acceptance. Deployment initializes a missing epoch on existing hosts
after the normal runtime check. A failed qualification stops claims, logs the
failure once, and retries after five seconds with exponential backoff capped at
five minutes. Probe containers carry `org.freeinference.chat-probe-created`, so
the broker excludes them from orphan-chat adoption. Each probe removes abandoned
probe containers in its namespace that are over an hour old.

The host adds its qualification verdict to the child's stdin payload after
stripping claim-supplied underscore keys. The child rereads the epoch, declines
during an update, and probes for itself if the epoch changed. It verifies the
host network before accepting the turn. A missing epoch requires the full
per-turn probe; deployment canaries without a payload always run it. The probe
reads the guest kernel from the same temporary container used for member
isolation and observer delivery.

See [Kata runtime qualification generation](../ENVIRONMENT.md#kata-runtime-qualification-generation)
for the marker format and installer requirements.

### Network verification and startup work

The broker verifies a group's bridge when acquiring a turn activation and
returns `network_verified`. The runner may rely on that proof for up to one
minute, matching the network guard's existing freshness window. Commands after
that minute verify again, including after a long setup script or at turn end.
Terminals and broker workspace operations always check, as does every command
when the broker did not supply that proof. The turn child still installs and
reads back host rules before acceptance.

Network-rule reads share a disposable helper; route reads use a separate helper
without capabilities. Reconciliation caches inspections only within one
command, rereads after mutations, and verifies the final bridge identity,
membership and relay addresses. It skips unchanged guest hosts/resolver writes
while retaining ownership, mode and symlink checks. These optimizations preserve
the network policy and freshness requirements.

A new chat's repository is cloned, scrubbed and archived on the host while the
container starts. Its `checkout` measurement includes the remaining clone wait
(`source_wait`) and import (`source_import`). Clone failures still fail at that
stage; an earlier stop discards prepared source. Startup measurements attribute
qualification to `preflight` and cover claim request through prompt dispatch,
excluding queue wait and model response time. Declined children do not publish a
successful startup. A host at capacity checks completed children every second.

### Empty-queue waits and CI evidence

After an empty claim, the host calls `/v1/agent/worker/wait`. The control plane
wakes waiting hosts when Postgres reports queued, requeued or ended work
(migration `0029`), or after five seconds. The host then claims again; the wait
endpoint never assigns work. Its dedicated database listener reconnects
independently and wakes all waiters on connection or disconnection. Changes the
triggers cannot observe, such as a moved placement pin or a finished Fork, are
therefore still found within the normal five-second interval.

If the endpoint returns 404, the host resumes polling and retries the endpoint
after five minutes. Hosts can deploy before the control plane.

CI runs the candidate check on the real Kata host for reviewed `dev` commits
before `CI Gate` permits release. It uses uniquely tagged images and never runs
Compose against the deployed stack. This job does not send PR code to that
machine; static checks across artifacts also run on PRs. A local Docker test
does not establish that the real Kata-host check passed: use the named CI job's
result as evidence.

## Old process, candidate schema

The Postgres CI job finds the latest `agent-*` release tag reachable from the
change's base and runs [`scripts/check_rolling_upgrade.py`](../scripts/check_rolling_upgrade.py).
It creates a random `rolling_test_*` database, migrates it with the previous
release's Alembic graph, and warms that release's storage pool with conversation
reads and writes. While the old process remains alive, it applies the candidate
migration and repeats those operations on the same database backend. It also
tests an idempotent migration retry, then removes the database and archived
source.

Run it against disposable local Postgres. Replace the quoted placeholder with
an existing prior `agent-*` release tag:

```bash
ROLLING_BASE_TAG='agent-<previous-sha>'
TEST_DB_HOST=127.0.0.1 TEST_DB_PORT=5432 \
  uv run python scripts/check_rolling_upgrade.py --base "$ROLLING_BASE_TAG"
```

This checks common storage paths against the previous release. Authenticated
browser flows and compatibility with every historical release need separate
evidence.

## Detection after deployment

The main API runs one queue monitor; the MCP listener runs none. After two
consecutive observations one minute apart, it reports queue heads at least ten
minutes old:

| Event | Meaning |
| --- | --- |
| `queued_without_host` | Waiting work has no enrolled, fresh, compatible host, accounting for retained ownership and the new-chat placement pin. |
| `queue_not_starting` | Old queue heads remain and no job is running, even if host heartbeats look healthy. |
| `queue_health_unavailable` | Repeated database observations failed. |
| `queue_recovered` | A reported problem cleared. |

Follow-ups waiting behind an earlier turn are excluded. Occupied slots with
running work are normal backpressure. The monitor reports state; it never claims
jobs, restarts hosts, changes scheduling or relaxes isolation. External
process/HTTP monitoring remains necessary because a dead API cannot monitor
itself.

Events are logged even without external delivery. To enable notifications, set
`AGENT_ALERT_WEBHOOK_URL` in the control-plane `.env` to an operator-owned
receiver. Queue alerts send JSON with `text`, `event`, `deployment_sha` and
aggregate `queue` counts. The receiver must accept that contract; chat providers
may need an adapter. Deployment-workflow alerts belong to the downstream
distribution.

Queue notifications contain no prompts, repository names, job IDs, host
credentials or log dumps. Redirects are refused. Delivery failures are logged
without the webhook URL and do not affect requests or the reaper. Notifications
follow state changes, repeat at most once every 30 minutes while unchanged, and
retry failed delivery the next minute. External alerts are active only after the
deployment owner configures the receiver and its credentials.

## Recovery boundaries

The [backup and recovery runbook](disaster-recovery.md) defines initial retention
and recovery targets, coordinated database/host recovery sets, encryption-key
preservation, restore-drill acceptance, and previous artifact-release recovery.

Inspect both machines' deployment markers and the failure reason, then retry
the same verified target. A failed host rollout remains a failed rollout even
if it restored the recorded generation. Control-plane failure leaves compatible
migrations in place; there is no automatic database downgrade.

A hard-cancelled host activation may leave the readiness marker pending while
Docker Engine continues the unbounded graceful runner stop. Running turns still
finish naturally without a deployment signal reaching their child processes;
after the runner exits, the host remains unavailable until activation is rerun
for the same verified target. Do not publish readiness by hand.

If the host succeeds before the control plane fails, the exact-SHA gate can keep
new work queued until the retry succeeds. The queue monitor detects this state.
These checks protect and expose interrupted deployments; activation still spans
two machines and PostgreSQL without a single transaction.

### Snapshot publication failures

Snapshot publication currently has no owner retry action or automatic recovery
after a failure. A rejected patch, unavailable GitHub credential, or failed
Git/PR request marks the snapshot `failed` and its sandbox group `dirty`; the
included Agent turns remain successful. Capture and publication require every
earlier snapshot in that group to be `published`, and later turn settlement
preserves `dirty`. A transient publication failure can therefore stop all
subsequent snapshots in the same retained workspace. Capture retries and
published-commit import retries do not recover this state. See
[`publish_worker.py`](../apps/backend/cloud_agent/publish_worker.py),
[`snapshot.py`](../apps/backend/cloud_agent/storage/repositories/snapshot.py)
and [`sandbox.py`](../apps/backend/cloud_agent/storage/repositories/sandbox.py).

Inspect the snapshot's `last_error`, the anchor job's
`shared_snapshot_publish_rejected` event, and GitHub's branch/PR state while
preserving the retained workspace and snapshot. Restoring GitHub access or
network availability does not requeue an already failed snapshot. There is no
supported database reset: marking it published or clearing the group's dirty
state would bypass the ordered publication lineage. Resetting it to captured
also cannot safely recover an ambiguous push: if the push succeeded before the
PR request failed, the publisher treats that remote commit as branch movement
and refuses it.

A future recovery path should persist the exact prepared commit SHA before
pushing, then offer an owner-authorized retry of the earliest failed snapshot.
Retries must repeat entitlement and patch checks, recognize only the persisted
commit as a prior push, preserve snapshot order, and continue to reject other
branch movement without force-pushing. Commit-message text and the patch hash
alone are insufficient evidence of the publisher's prior commit.
