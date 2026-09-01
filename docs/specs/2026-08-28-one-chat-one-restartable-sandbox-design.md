# One chat, one restartable sandbox

- **Status:** historical v5 baseline. New capable chats are superseded by
  `2026-08-31-native-session-fork-shared-workspace-design.md`; `legacy_1to1`
  retains only its one-process-per-turn resume behavior for rolling
  compatibility and rejects retained Fork. The Docker-clone implementation
  described below was removed before merge.
- **Date:** 2026-08-28
- **Historical scope:** Docker-owned same-host storage, live interaction while
  running, native Agent CLI resume after `docker stop` / `docker start`, and a
  proposed isolated full-sandbox clone at the latest settled turn
- **Defers:** sandbox cleanup, container removal, persistent/cold storage,
  cross-host restore, and recovery after container loss
- **Supersedes for new work:** the runtime lifecycle, WARM ownership, fork,
  model-switch, and residency decisions in
  `2026-08-17-session-resident-harness-threads-design.md`. That document
  remains the implementation history and input to later persistent-storage
  work.
- **Implemented successor for new shared-live chats:**
  `2026-08-31-native-session-fork-shared-workspace-design.md` moves retained
  CLI process ownership into the supervisor, keeps one live transport per chat
  while Docker runs, and performs immediate native Agent CLI forks whose chats
  run concurrently in one retained container/workspace.

> The runner-owned one-process-per-turn path remains the `legacy_1to1`
> compatibility baseline. The full Docker-clone Fork sections below document a
> superseded proposal only: its route, host implementation, saga table, and
> acceptance test were removed before merge. The current Fork API either makes
> a shared native-session fork or rejects the retained legacy chat.

---

## 0. Historical preview implementation boundary

The v5 branch implemented the retained Docker container, durable sandbox
ledger, host Session Supervisor, 30-minute settled-idle `docker stop`, same-
container `docker start`, Docker-only file/Git/terminal access, control-plane
state reconciliation through the host heartbeat, and strict native resume for
the pinned Claude Code, Codex, Pi, OpenCode, and Kilo CLIs. A published
follow-up base that is not yet in the retained repository
is fetched by the trusted runner and transferred as a credential-free Git
bundle; importing objects does not move the chat's HEAD, index, local commits,
or working tree. The original sandbox image digest is part of the immutable
Docker label identity and is used directly if a pre-container activation must
be retried during a rolling image update. Prompt admission is durably recorded
before Agent CLI stdin; an expired retained activation fails closed as
STOPPED_DIRTY/LOST instead of replaying the message. Host heartbeats also carry
active-operation state and
the remaining monotonic idle window, so a terminal or file/Git operation resets
the durable deadline rather than merely delaying an already-expired stop
command. The frontend uses context (`new`/`live`/`restored`/`lost`) as the
canonical product state; the older continuity rung is legacy diagnostic
telemetry and is shown only on rows that predate context status.

The v5 preview was deliberately default-off. `AGENT_RESTARTABLE_RUNTIMES` defaults
to `claude-code,codex,pi,opencode,kilo`. Their exact pinned native-session
commands are implemented in the shared driver path; Kata and live-provider
conformance remain rollout gates beyond the local real-Docker matrix. R5 is
implemented as a durable
PREPARING saga plus a paused Docker commit, independent clone, transactional
history-prefix publication, exact rollback, and ordinary native resume. The
real-Docker acceptance preserves dirty/staged/untracked workspace and opaque
session state, then proves source and clone diverge independently. Only the
latest settled turn is eligible; an older anchor has no matching filesystem
snapshot and fails closed. The snapshot image content id is recorded as the
common immutable base fingerprint; the clone's new chat metadata and Git
branch are deliberate post-snapshot identity changes and are verified
separately before publication.

## 1. Decision

The staged v5 phase had six product invariants:

1. **One chat owns one sandbox container.** A chat is the existing
   `agent_thread`. No other chat may execute in that container.
2. **One running sandbox has at most one platform-managed Agent CLI process at
   a time.** The implemented baseline exits after each message and launches the
   next process with that CLI's native resume operation. A future transport may
   keep it across clean turn boundaries as an optimization. PID continuity is
   not a product invariant; native context continuity is.
3. **Settled idle timeout stops, but does not remove, the container.** The host
   uses `docker stop`; it does not pause, export, clean, or `docker rm` the
   sandbox in this phase.
4. **The next message starts the same container and natively resumes the
   chat.** `docker start` restarts the container, then a new Agent CLI process
   loads the session id and state files left inside that container. Transcript
   copy/paste is not the normal resume path.
5. **Chat state is Docker-owned.** `/workspace`, the Agent CLI home/session
   files, installed dependencies, and uncommitted changes are ordinary
   directories in that container's writable layer. The application has no
   per-chat host path and uses no bind, named, or anonymous volume for durable
   chat data. It reads and writes chat data only through the Docker Engine API.
6. **Fork is a platform-owned full sandbox copy.** At the latest settled turn,
   fork copies the source conversation and the source container's complete
   non-secret filesystem state, then allocates a new thread, container,
   writable layer, residency, credentials, and job sequence. It does not call
   an Agent CLI fork command and does not replay old prompts through the model.
   Source and fork share no writable resource.

The ownership model is:

```text
one agent_thread
  = one retained sandbox container
  = one Docker writable layer containing workspace + Agent CLI state
  = many sequential activation generations

one activation generation
  = one fenced turn-admission window (and its fresh credentials)
  = one current Agent CLI process generation
  = at most one Agent CLI root process at any instant
  = one agent_job / agent_attempt turn window

one docker start -> docker stop interval may contain many sequential
activation generations. Advancing this fence more often than the container
PID changes is intentional: a late writer from the previous turn must not gain
authority over the next prompt merely because Docker stayed RUNNING.
```

`agent_job` and `agent_attempt` remain per turn. They are still the units of
queueing, cancellation, lease fencing, events, usage windows, retry, patch
publication, and audit. They no longer imply a new sandbox container.

"Full copy" is a logical isolation guarantee, not a promise to duplicate every
physical disk block. Docker image layers may be shared read-only with
copy-on-write storage. All future writes, platform ids, credentials,
processes, and lifecycles are independent. A CLI's opaque local session id may
be identical in two cloned containers; the platform namespaces it by
`sandbox_id` and never treats it as a globally unique or shared resource.

### 1.1 End-to-end topology

The frontend does not contact a host directly. The current public API and
queue remain the control boundary:

```text
frontend POST /v1/agent/jobs
  -> control plane authenticates, authorizes repository/model/MCP, and creates
     agent_thread + job 1
  -> scheduler exposes the queued turn to an eligible host
  -> host runner claims one fenced attempt
  -> host Session Supervisor creates the thread's retained sandbox
  -> source import + setup + live Agent CLI start happen in that sandbox
  -> normalized events/results flow back through the control plane

frontend POST /jobs/{job_id}/follow-ups
  -> control plane appends another job to the same agent_thread
  -> scheduler routes it to the recorded host/sandbox residency
  -> supervisor either sends it to the live CLI or restores the CLI first
```

The database thread may briefly exist in QUEUED/PROVISIONING before a host has
capacity to create the container. Once provisioned, one executable chat owns
exactly one retained sandbox; a job or attempt never creates a second one.

"Everything happens in the sandbox" applies to untrusted execution and chat
state: workspace, dependencies, Agent CLI state, tools, setup, file operations,
Git inspection, and terminals. Authentication, authorization, scheduling,
credential minting, event storage, source preparation, egress relays, and PR
publishing remain trusted services outside the chat sandbox. Moving those
credentials or authorities into the sandbox would break the existing security
boundary, not simplify the topology.

## 2. Important correction: stop is not pause

`docker stop` terminates the container's processes. Docker sends the main
process `SIGTERM`, waits for the configured grace period, and then uses
`SIGKILL` if it has not exited. `docker start` starts a stopped container; it
does not resurrect the old PID or process memory:

- <https://docs.docker.com/reference/cli/docker/container/stop/>
- <https://docs.docker.com/reference/cli/docker/container/start/>

Therefore this design cannot truthfully promise the same Agent CLI process
across an idle timeout. What survives is filesystem-backed state:

| Preserved by retaining the stopped container | Not preserved by stop/start |
|---|---|
| container id, labels, and writable layer | Agent CLI PID and memory |
| repository/worktree and uncommitted files | shell variables and in-memory caches |
| native CLI session files and session id | background processes and open sockets |
| chat configuration and local journals | an in-flight or partially drained turn |

Continuity is consequently classified as:

- **NEW:** the first turn of a newly created chat. There is no earlier native
  session to continue, so it is neither LIVE nor RESTORED.
- **LIVE:** the follow-up found the same retained chat container already
  running and continued its native local session. The implementation may use a
  fresh CLI process; this status makes no PID claim.
- **RESTORED:** the follow-up first started that chat's stopped container, then
  a new Agent CLI process loaded the retained native session and continued
  from the recorded settled context.
- **LOST:** the retained container or native session cannot be resumed. Before
  persistent storage exists, the platform does not silently claim recovery.

The older `hot`, `warm_native`, `cold_native`, and `replay` values describe a
legacy execution/fallback ladder, not the context's correctness state. They do
not map one-to-one: RESTORED may eventually load from either local or cold
storage, while replay is a recovery method rather than a state. The required
invariant is that the retained path proves the new turn continues from
`last_settled_job_id` without transcript injection. Exact same-PID
continuation is neither required nor part of this phase.

## 3. Docker-only local storage boundary

For local chat filesystem and process state, the host presents one control/data
boundary to this service: Docker Engine, through a local socket or a mutually
authenticated remote Engine endpoint. In a retained-only deployment the
Session Supervisor may run as an infrastructure container, but it does not
receive an `AGENT_WORKDIR_ROOT`, Agent CLI state root, repository checkout, or
other per-chat host filesystem mapping.

```text
host OS
└── Docker Engine / Docker-owned data root
    ├── Session Supervisor (Docker API only)
    ├── source preparer + egress/relay infrastructure
    ├── chat A container -> writable layer A
    ├── chat B container -> writable layer B
    └── chat C container -> writable layer C

no AGENT_WORKDIR_ROOT -> /workspace bind
no host Agent CLI state directory -> container bind
```

The checked-in production compose is intentionally still a **mixed-mode
rollout**: the preview flag defaults off, so its shared broker/runner services
retain `AGENT_WORKDIR_ROOT` for legacy one-shot jobs. Retained requests never
address chat state through that mount. The local retained stack uses a
Docker-managed infrastructure volume rather than a host-path bind, and every
chat container still has an empty `Mounts` list. Removing the legacy bind from
the production services is a retained-only cutover step after all runtimes
pass their rollout gates; it cannot land unconditionally while the original
default-off execution path must remain usable.

For every chat container:

- `/workspace`, `/home/agent`, and any runtime-specific `/agent-state` path are
  ordinary container directories in the writable layer;
- `docker inspect .Mounts` contains no bind, named volume, or anonymous volume
  carrying chat state;
- activation secrets may use an in-memory `tmpfs` such as
  `/run/agent-activation`; that mount is intentionally empty after a stop and
  intentionally excluded from a fork snapshot;
- the base container's `Config.Env`, image layers, writable layer, and native
  CLI config contain no grant, repository token, API key, or MCP credential;
- every application operation is expressed as container
  create/start/stop/inspect/exec/stats/archive/commit through Docker Engine.

Neither application code nor operators use `/var/lib/docker` (or another
daemon data-root path) as a file API. Docker Engine owns its on-disk format;
inspection and transfer go through the Engine API only.

This removes host-path coupling, but does not make the state host-independent.
Docker's storage driver still stores the writable layer under the daemon's
data root on a physical host disk. A daemon restart or host reboot retains an
existing stopped container; deleting the container, running an unfiltered
container/system prune, losing the Docker data root, or reprovisioning the
host loses the chat in this phase. That is reported as LOST until persistent
storage is implemented.

Docker commit does not include mounted volume contents. Therefore the
no-chat-state-volume rule is also a correctness prerequisite for full-state
fork, not merely a deployment preference:

- <https://docs.docker.com/reference/cli/docker/container/commit/>
- <https://docs.docker.com/engine/storage/drivers/>
- <https://docs.docker.com/engine/manage-resources/pruning/>

### 3.1 Exact resource and I/O scope

The one-chat/one-container invariant includes all chat-specific work:

- A trusted source-preparer fetches the repository with the short-lived clone
  token in an ephemeral temporary directory, removes credentials, and streams
  a sanitized tar into `/workspace` using Docker's archive API. The clone token
  never enters the chat container. Tar paths/types are validated, uid/gid are
  normalized to the sandbox user, and the imported checkout is verified before
  setup. The temporary clone is deleted before prompt admission and is never a
  persistent, mounted, or resumable per-chat host worktree.
- On the first activation, setup runs inside the chat's container. The
  supervisor gives it setup egress, waits for setup to settle, removes that
  network, verifies removal, switches irreversibly to agent egress, and only
  then starts the Agent CLI.
- Later activations do not rebuild the worktree or rerun setup. They reuse the
  same writable layer, repository, dependencies, and state directory. If the
  trusted publisher advanced the thread branch, the runner fetches that exact
  commit outside the sandbox and imports only a bounded credential-free Git
  bundle; the source chat's Git refs/index/worktree are not reset.
- Terminal access uses `exec` in the same retained container. If it is stopped,
  the Session Supervisor starts the chat activation before granting terminal
  access. It never creates a terminal sandbox for that chat.
- Interactive terminal streams are broker-process state, not chat checkpoints.
  A graceful broker shutdown closes them. After an abrupt broker restart, the
  supervisor matches each unattachable shell by its unguessable pidfile and
  exact wrapper argv, kills only that process group, and keeps the retained
  container/workspace running. It never uses a broad `pkill` or replaces the
  chat container.
- Each pinned driver exits at a clean turn boundary and the next turn starts a
  new process using that CLI's explicit native session locator. A reconnectable
  bridge may later reduce startup latency, but it is not required for context
  correctness and does not change NEW/LIVE/RESTORED semantics.
- File browsing and patch generation run through `exec`; file upload/download
  uses the archive API. The workspace broker never opens a host path.
- File writes, bounded commands, and terminal create/input/resize are also
  fenced by the durable thread sandbox row. An active turn must have published
  `workspace_ready`, and a fork must have released its saga fence; therefore a
  broker restart or a request through an older turn URL cannot mutate while
  setup, final capture, or a Docker snapshot owns the workspace.
- Platform relay/proxy containers are infrastructure and are not chat
  sandboxes.
- A platform-managed activation has at most one Agent CLI root. Tool
  subprocesses and the container init shim do not violate that invariant.

The one-CLI rule describes platform lifecycle ownership. Preventing arbitrary
untrusted shell code from invoking another installed agent binary would
require per-runtime images or a separate execution policy and is outside this
continuity guarantee.

### 3.2 Activation secrets are not snapshot state

The current Pi/OpenCode/Kilo wrappers materialize the literal provider key in
configuration below `$HOME`. That behavior cannot be used with retained or
forked writable layers. The restartable wrappers must instead write any
credential-bearing generated config under activation `tmpfs`, or reference a
fresh value supplied only to the `docker exec` that starts the CLI. They must
never put a secret in an image, container creation environment, CLI argument,
durable home directory, transcript, or fork snapshot.

At stop the supervisor revokes the activation credentials and the tmpfs
contents disappear. At start it provisions a new activation tmpfs payload
before launching the CLI. A cloned chat gets newly minted credentials only
after its new container identity is established. See
<https://docs.docker.com/engine/storage/tmpfs/>.

### 3.3 No cleanup in this phase

STOPPED containers are retained indefinitely by the automatic lifecycle:

- no stopped-container TTL;
- no LRU eviction;
- no `docker rm` after idle timeout;
- no native-session export;
- no upload to persistent storage.

The service and its deployment must not run `docker container prune`,
unfiltered `docker system prune`, or a generic age-based Docker cleanup on a
session host. Session containers are not Compose-managed and carry no Compose
project labels, so `docker compose down` for the host infrastructure must not
remove them. An operator may explicitly remove one only through a later
audited administrative lifecycle; until then that action produces LOST.

An explicit operator deletion or unrecoverable host/container loss marks the
chat LOST. It is never reported as an ordinary timeout transition. Because
stopped containers still consume disk and container metadata, the scheduler
must cap new-chat admission and alert on disk pressure; it must not solve
pressure by silently deleting an existing chat.

## 4. Legacy baseline and remaining preview gaps

The first column below describes the legacy/default-off path. R1-R5 landed in
the historical v5 preview, including native-session adapters for all five
pinned CLIs and its then-current latest-settled full Docker fork. That fork is
no longer reachable for a new API request; its implementation remains only for
recovery/cleanup of historical operations. Local real-Docker coverage landed;
Kata and live-provider conformance for every advertised CLI, the
context-fingerprint preflight described below, and R6 disk-pressure admission
remain rollout work rather than claims about this branch:

| Legacy/default-off implementation | Restartable target |
|---|---|
| Agent containers use random `hyi-job-*` names and `docker run --rm` | Stable chat identity/labels and explicit create/start/stop lifecycle without `--rm` |
| The runner creates a host worktree and bind-mounts it at `/workspace`; optional Agent state is another bind | Import a sanitized repository archive and keep workspace/home/session state only in the container writable layer; no chat-state volume or host path |
| Setup snapshots are tar files below the host `AGENT_SNAPSHOT_ROOT` | Omit the shared setup cache initially because each retained chat already keeps its installed dependencies; any later cache is Docker-managed infrastructure, never a chat-state mount |
| Setup, agent execution, and terminal access can use separate container invocations | One container with an init shim; setup and terminal use `exec`; at most one Agent CLI runs at a time |
| `SandboxProcess` exposes kill/wait; only terminal containers expose suspend via pause/unpause, and neither path retains an agent container for stop/start | Add create/start/stop/inspect/exec to a chat-container backend contract |
| The CLI's stdio pipe is owned by one runner process and cannot be safely inherited after that runner exits | End the process at the settled boundary and make native session files/id the continuity contract; a reconnectable bridge is an optional latency optimization |
| `ResidentPool` and live stdio belong to one runner replica | Move container and activation ownership to one host-level Session Supervisor |
| `ResidentPool` applies local TTL/LRU eviction and runner housekeeping purges host workspaces | Remove both cleanup paths from restartable chats; use Docker-layer accounting, admission refusal, and alerts instead |
| A host defaults to three independent runners while affinity names only the host | Let any runner dispatch to the supervisor's exact thread/container generation |
| `workspace_broker.py` opens the host worktree and launches a separate terminal container | Read/archive/exec against the retained container through Docker Engine; terminal uses that container |
| Claude turns are one-shot processes | Persist the native id from turn 1 and use strict `--resume` for every follow-up |
| Codex, Pi, OpenCode, and Kilo were generic one-shot invocations | Persist their native locator/state and use `codex exec resume`, Pi `--session`, and OpenCode/Kilo `run --session` on every follow-up |
| Current follow-ups can restart on model/effort changes | Preserve the API: reconfigure in-band when supported, otherwise replace only the CLI process and resume the same local context in the same container |
| MCP credentials currently follow an attempt fence | The landed fresh-process baseline keeps that fence and injects fresh turn credentials through activation tmpfs. A future cross-turn live transport would require an activation-scoped credential or stable sandbox-facing relay; it must not stretch an attempt credential past its fence. |
| Pi/OpenCode/Kilo wrappers write provider credentials into `$HOME` config | Move generated credential material to activation tmpfs and prove it is absent from commit/fork layers |
| Fork copies database history and later reconstructs files from a patch | At latest settled state, atomically copy database history plus the complete Docker filesystem snapshot; use ordinary resume in the clone and never depend on a runtime fork command |

Legacy one-shot threads may coexist during rollout, but they are a separate
execution mode. They must never report restartable RUNNING/STOPPED or proven
LIVE/RESTORED context under this contract.

### 4.1 Existing feature-preservation contract

Changing container ownership must not narrow the product. Public route shapes
stay compatible while their workspace backend changes from a host path to a
thread container:

| Existing behavior | Restartable-sandbox implementation requirement |
|---|---|
| Create/list/read a task; thread history; project grouping; archive/restore; pin/unpin; thread stats | Keep the existing control-plane APIs and database rows. `agent_thread` becomes the sandbox owner; these metadata operations do not create or copy containers. |
| Follow-up jobs with sequential turn numbers | Keep one `agent_job` per user message and serialize them through the thread supervisor. A follow-up never provisions another sandbox. |
| Runtime selection and exact CLI version stamping | Runtime and compatible CLI version stay fixed for the chat/container. A host that cannot run the stamped version refuses the attempt rather than silently substituting another binary. |
| Per-turn model and reasoning-effort changes | Keep the current API semantics. Use an in-band driver control when proven; otherwise flush and replace the one CLI process, then resume the same local session with the requested configuration before admitting the turn. Do not replace the container or paste transcript context. |
| Live normalized events, raw event backfill, SSE stream, trajectory, usage, and thread stats | Preserve per-attempt sequence/idempotency and normalized event contracts. The supervisor bridges the live CLI stream into the existing worker event endpoints; process rotation must not reset job event sequence. |
| Queued/running cancellation, attempt retries, leases, and generation fencing | Preserve `agent_job`/`agent_attempt` as control units. Cancellation reaches the current in-container turn. A retry before CLI admission is safe; an ambiguous post-admission retry never resends the prompt. |
| File tree/read/write and upload/download | Keep the public file API. The broker validates repository-relative paths as today, then uses Docker exec/archive against `/workspace`; it never resolves a per-chat host path. |
| Git status/diff view | Run the bounded, unprivileged Git inspection in the retained container and return the existing response schema. Repository-owned Git config is never executed with host privileges. |
| Bounded commands and interactive terminal create/list/stream/input/resize/delete | Keep the public terminal APIs and SSE behavior, but exec the PTY in the same chat container. Active terminals count as chat activity and block a mid-operation stop; reaping an inactive PTY preserves its last activity deadline instead of starting a second chat timeout. Terminal metadata/transcript is not fabricated as still running after restart. |
| Patch, workspace snapshot, and other fenced artifacts | Generate patch and changed-file workspace snapshot at the settled barrier inside the container and upload through the existing worker artifact endpoint. Portable Agent CLI checkpoint export is deliberately disabled on the retained path until the persistent-storage phase; it is not the normal same-container resume source. |
| Draft-PR publishing and source-control entitlement checks | Keep clone/push/PR credentials and the publisher outside the sandbox. The sandbox emits the same validated patch artifact; the trusted publisher revalidates and publishes it as today. |
| GitHub/GitLab repository access and first-turn setup | A trusted source preparer fetches and scrubs the repository, streams it into the new container, and setup runs once under setup egress. Stop/start and follow-ups neither reclone nor rerun setup. |
| MCP integrations and gateway inference grants | Preserve fail-fast entitlement/tool checks and egress relay isolation. Credentials are activation/process material in tmpfs, never part of the retained or cloned filesystem. |
| Per-turn resource windows and container accounting | Sample the retained container id with a separate accounting window for each attempt; STOPPED storage bytes are reported separately from active CPU/memory usage. |

Every row needs API/component parity tests before the restartable mode becomes
the default. Legacy fallback may remain during rollout, but the UI must not
silently lose an action merely because a chat uses the new container backend.

## 5. Timeout semantics

Only a **settled idle timeout** stops a container:

```text
latest turn durably settled
  -> idle timer starts
  -> no queued/running turn, active terminal, file mutation, artifact capture,
     or fork barrier at deadline
  -> graceful Agent CLI shutdown
  -> docker stop the retained container
```

The idle deadline is a product timer, not the residency lease. The supervisor
keeps ownership fenced while the container is STOPPED.

An **active-turn execution timeout** is different. It may have a model request,
tool process, partial filesystem edits, or output in flight. The platform
first cancels the turn and asks the driver to reach a verified terminal state.
If it cannot prove a clean boundary, it may stop the container to contain the
work, but records the chat as `STOPPED_DIRTY` or LOST and does not automatically
inject the next message. A user or later recovery design must decide whether
to keep partial workspace changes.

Other reasons that may use the clean stop path are an explicit user sleep or a
controlled host drain after every active turn and terminal operation settles.
A connected or running interactive terminal keeps the activation alive; the
platform does not claim that `docker stop` preserves its process. CLI crash,
OOM, a proven-missing container/data-root, and forced `SIGKILL` before state
flush are failure paths, not normal STOPPED continuity. Mere heartbeat loss or
temporary host unreachability remains an unavailable, hard-pinned residency
until the owning Docker daemon can be reconciled.

## 6. Lifecycle

```text
                         first job
                            |
                            v
  NONE -> PROVISIONING -> RUNNING <-------------------------+
                            |                               |
                    settled + idle timeout                  |
                            |                               |
                            v                               |
                         STOPPING -> STOPPED -> STARTING ----+
                                      |
                           missing/corrupt container or
                              native session state
                                      |
                                      v
                                    LOST
```

Rules:

- `RUNNING` means the container is running. A turn is accepted only while one
  platform-managed Agent CLI process is healthy; a settled process rotation
  may briefly leave zero, but never two, CLI processes. It does not mean a
  turn is active.
- `STOPPING` is the durable containment state for a failed pre-admission or
  orphaned activation. Ordinary settled-idle stop may be observed as a direct
  RUNNING -> STOPPED reconciliation because the host-local supervisor performs
  the Docker stop under its own activity lock.
- `STOPPED` means the same container exists but no Agent CLI or tool process is
  running. `agent_cli_pid` is null in this state.
- `STARTING` verifies the retained container and state, starts it, launches one
  new Agent CLI with native resume, and completes a protocol health check.
- Each successful STARTING transition increments `activation_generation`.
  Every CLI replacement increments `process_generation`; PID is diagnostic
  only. `container_id` does not change.
- A failed native resume never starts a blank session and calls it continuity.

### 6.1 First turn

```text
create thread + job 1
  -> establish residency and stable container identity
  -> create one labelled container without --rm
  -> run setup in that container and switch to agent egress
  -> start one Agent CLI process and create its native session
  -> create and persist one native CLI session id
  -> send job 1 through the driver's supported protocol
  -> settle attempt
  -> keep the container RUNNING until idle timeout; the driver may retain or
     cleanly exit the CLI process at the settled boundary
```

The first turn records NEW. Every later turn launches from the persisted native
session locator/state; whether a future implementation reuses a process is
irrelevant to correctness.

### 6.2 Follow-up while running

```text
append job N
  -> route to the residency owner
  -> Session Supervisor locks the thread
  -> observe that the retained sandbox is already RUNNING
  -> launch one process in place from native state
  -> open attempt N's event/accounting window
  -> send only the new turn to the current Agent CLI
  -> wait for the driver terminal event
  -> close the attempt window and refresh idle deadline
```

This path records LIVE because the chat's retained sandbox was already running;
it does not claim that the previous PID survived. Only the new prompt is sent.

### 6.3 Follow-up while stopped

```text
append job N
  -> route to the retained container's host/generation
  -> reserve active host capacity
  -> atomically STOPPED -> STARTING
  -> inspect the exact container id and local state
  -> mint fresh activation credentials
  -> docker start the same container
  -> launch a new Agent CLI process with the native session id
  -> verify native resume through the driver protocol
  -> RUNNING
  -> send job N
```

This turn is RESTORED: same container/workspace/native conversation, new
process. No checkpoint is downloaded and no platform transcript is pasted into
the prompt.

## 7. Host ownership and scheduling

The current per-runner `ResidentPool` cannot own a retained container safely:
one host runs multiple runner replicas, so a different replica can claim the
next turn and create another sandbox while the original still exists.

One host-level **Session Supervisor** is the sole identity, lifecycle, and
concurrency coordinator for chat containers:

```text
Control plane                 Host
-------------                 ---------------------------------------
thread residency row <------> Session Supervisor
                                     |
runner replica A ---- local RPC -----+
runner replica B ---- local RPC -----+--> one retained chat container
runner replica C ---- local RPC -----+       `- zero or one current Agent CLI
```

- Only the supervisor may create, start, stop, fork, or explicitly remove a
  chat container. File/Git/terminal operations also enter through the broker
  and take a supervisor activity token. During an acquired, generation-fenced
  turn, the runner performs the sanitized source import, one-way network
  transition, Agent CLI `docker exec`, and patch capture against that exact
  already-owned container; it cannot choose a replacement identity or run a
  second activation.
- Runners still claim jobs, but dispatch their turn window to the supervisor.
- The supervisor serializes operations by `(thread_id, container_generation,
  activation_generation)`.
- A stopped container consumes no active execution slot. A new message must
  reserve a slot before STARTING; otherwise the job remains queued and the
  container remains STOPPED.
- STOPPED affinity is hard, not preferred. A different host must not create a
  replacement container when the owner is unavailable, because only the
  recorded host has this phase's workspace and native session. The follow-up
  waits or reports host unavailable until that host returns.
- A host singleton lock and reconciliation prevent two supervisors from
  owning the same host identity.
- On supervisor restart, labels plus the residency row identify the exact
  container. In the preview the durable database row supplies the native
  session locator and admission/settlement fence, and the next runner starts a
  replacement CLI with strict native resume. An already-running container is
  reported LIVE and a stopped-then-started container RESTORED; neither status
  asserts that a PID survived the supervisor restart. Any identity mismatch is
  fenced and contained rather than creating a replacement container.

The existing singleton `workspace-broker` is the natural deployment seam: it
already has the Docker socket, authenticated runner RPC, and terminal
lifecycle ownership. It may be extended or paired with a sibling singleton,
but its host-workdir access is removed and runner replicas must not retain
independent lifecycle, filesystem, or stdio state after the fenced turn.

The STOPPED residency row is durable and does not lapse merely because an
active-attempt lease or host heartbeat expires. Loss of host heartbeat makes
the residency unreachable and fences start operations; it does not authorize
takeover or a new container. When the same supervisor host returns, it
reconciles the exact labelled container before the row becomes startable
again. Cross-host takeover is deferred with persistent storage.

Stable server-generated labels include:

```text
org.freeinference.chat-sandbox=true
org.freeinference.chat-thread-id=<thread_id>
org.freeinference.chat-sandbox-id=<sandbox_id>
org.freeinference.chat-generation=<generation>
org.freeinference.chat-runtime=<runtime_id>
org.freeinference.chat-runtime-version=<runtime_version>  # when pinned
org.freeinference.chat-image-digest=<sandbox_image_digest> # when known
```

## 8. Residency record and turn fencing

The landed `agent_thread_sandboxes` row is the minimum durable record used by
the preview today:

```text
{
  thread_id, sandbox_id, generation, host,
  container_id, container_name,
  runtime_id, runtime_version, sandbox_image_digest, execution_profile,
  state, context_status,
  activation_generation, process_generation,
  active_attempt_id, admitted_job_id, admitted_at,
  last_settled_job_id, native_session_id,
  idle_deadline, last_error, fork_operation_id,
  created_at, updated_at
}
```

`last_settled_job_id` is the restore-through anchor and `/agent-state` is the
fixed container-local native state root. PID identity is deliberately not
durable. The richer record below is a later observability/compatibility target,
not the schema currently stored by this branch; in particular,
`context_fingerprint`, model/effort/MCP fingerprints, supervisor identity, and
a diagnostic CLI PID have not landed yet.

```text
SandboxSessionResidency {
  thread_id,
  container_generation,
  activation_generation,
  process_generation,
  host_id,
  supervisor_id,
  sandbox_id,
  state: provisioning | running | stopping | stopped | starting |
         stopped_dirty | lost,
  agent_cli_pid,
  native_session_id,
  native_state_path,              # absolute path inside the container
  restored_through_job_id,
  context_fingerprint,
  storage_mode: docker_writable_layer,
  runtime_id,
  runtime_version,
  sandbox_image_digest,
  current_process_model,
  current_process_reasoning_effort,
  mcp_fingerprint,
  admitted_job_id,
  active_attempt_id,
  idle_deadline,
  stopped_at,
  owner_last_seen_at,
  activation_lease_expires_at,
  last_settled_job_id
}
```

Required invariants:

- one current residency and container generation per thread;
- one immutable sandbox id for the normal lifetime of that chat;
- `agent_cli_pid` is a diagnostic for the current `process_generation`, not a
  continuity identity;
- a native session locator is `(sandbox_id, native_session_id,
  native_state_path)`; `native_session_id` alone is never globally unique;
- one active attempt and one admitted job per thread;
- every supervisor command carries the thread, container, and activation
  generations;
- STOPPED keeps its durable `host_id`/`sandbox_id` binding without an active
  attempt lease; host unreachability never causes automatic reassignment;
- stale attempts, supervisors, heartbeats, events, and credentials are
  rejected;
- a job becomes running only after its residency is RUNNING and its attempt
  window is open.

### 8.1 At-most-once turn admission

After a user message reaches CLI stdin, an old attempt may still modify the
workspace even if its database writes are fenced. The supervisor therefore
admits each `job_id` at most once:

1. Persist the admission reservation before writing to the CLI.
2. A retry attaches to the admitted turn or returns its durable terminal
   result; it never writes the same prompt again.
3. If a crash leaves delivery ambiguous, stop the container, record
   STOPPED_DIRTY/LOST, and report an indeterminate turn instead of replaying it.
4. Clear admission only after the matching terminal event and outcome are
   durable as `last_settled_job_id`.

The preview implements the conservative subset of step 2: any retained
activation whose attempt lease expires is marked LOST and is not retried,
including the small pre-admission window. This trades availability during a
runner crash for a universal no-duplicate-prompt guarantee. Reattaching a retry
to an in-progress native turn requires a separately proven per-CLI message-id
or bridge journal and is not claimed yet.

Native client message ids may add deduplication, but the universal contract
cannot rely on them.

## 9. Agent driver contract

The common contract makes native resume from retained local state mandatory;
a live process is an optional transport optimization. Fork is intentionally
absent: the platform clones the container, then uses this same `resume`
operation in the clone.

```python
class NativeAgentSession:
    async def start_new(self, context: SessionContext) -> SessionLocator: ...
    async def resume(self, context: SessionContext,
                     locator: SessionLocator) -> None: ...
    async def start_turn(self, input: TurnInput) -> AgentTurn: ...
    async def wait_settled(self, turn: AgentTurn) -> TurnResult: ...
    async def flush_for_stop(self) -> NativeSessionState: ...
    async def close(self) -> None: ...
```

The implementation direction for the currently pinned image is:

| Runtime | Current pin | Per-turn command | Native locator/state retained in container | Per-turn configuration |
|---|---:|---|---|---|
| Claude Code | `2.1.220` | headless [`stream-json`](https://code.claude.com/docs/en/cli-reference), then `--resume <session_id>` | `CLAUDE_CONFIG_DIR` plus announced session id | model/effort are launch flags; every turn already starts a fresh process |
| Codex | `0.145.0` | [`codex exec … resume <session_id> <prompt>`](https://github.com/openai/codex/blob/rust-v0.145.0/codex-rs/exec/src/cli.rs) | `CODEX_HOME` rollout/state plus `thread.started.thread_id` | model/effort stay on the resumed invocation |
| Pi | `0.73.1` | [`pi --mode json --session-dir … --session <id> -p <prompt>`](https://github.com/badlogic/pi-mono/blob/v0.73.1/packages/coding-agent/docs/rpc.md) | selected session directory plus JSON `session.id` | model/thinking stay on the resumed invocation |
| OpenCode | `1.18.9` | [`opencode run --format json --session <id> <prompt>`](https://github.com/anomalyco/opencode/blob/v1.18.9/packages/opencode/src/cli/cmd/run.ts) | relocated XDG roots plus event `sessionID` | model/reasoning config is regenerated in activation tmpfs |
| Kilo | `7.4.17` | [`kilo run --format json --session <id> <prompt>`](https://github.com/Kilo-Org/kilocode/blob/main/packages/kilo-docs/pages/code-with-ai/platforms/cli-reference.md) | relocated XDG roots plus event `sessionID` | model/reasoning config is regenerated in activation tmpfs |

The pinned-version storage audit gives a credible local-copy path for all five:

- the exact Claude binary names `CLAUDE_CONFIG_DIR/projects/<cwd>/*.jsonl` as
  its transcript store and exposes `--resume`;
- Codex stores rollout files and its SQLite state below `CODEX_HOME` and
  resumes by thread id;
- Pi stores each session as JSONL below its selected `--session-dir`;
- OpenCode stores session data below `Global.Path.data/storage`;
- Kilo stores local session data in its `Global.Path.data` database/storage
  paths. Optional Kilo cloud-session features are not an allowed authority for
  this mode and must be disabled or proven irrelevant.

All of those paths are inside the retained writable layer at stable absolute
locations, so Docker clone preserves their references. This static audit is
necessary but not sufficient: only the source/clone divergence test proves
that no runtime or provider mutates hidden external state keyed by the copied
opaque id.

No row depends on `--fork-session`, `thread/fork`, Pi `clone`/`fork`, or ACP
`session/fork`. Those upstream operations may exist, but they are not invoked
by the platform fork path and are not a prerequisite for it. In a cloned
container, the ordinary resume locator can contain the same opaque CLI id as
the source because the writable stores are isolated and the platform key is
`(sandbox_id, native_session_id)`.

An upstream command existing today is not proof that our pinned binary resumes
correctly. Each runtime is capability-gated. Its exact in-image contract suite
must prove live turns, clean stop, process restart, native state load,
per-turn model/effort behavior, continued tool use, and independent source/
clone divergence from the same copied state. A CLI whose session depends on
mutable external server state shared by the source and clone fails this gate;
the platform must not silently fall back to transcript replay or a CLI fork.
The final topology is universal only after every runtime offered in the UI
passes this suite; legacy one-shot mode is a rollout mechanism, not the end
state.

## 10. Stop protocol

### Clean-stop preconditions

1. Thread/container generation and supervisor ownership match.
2. No queued transition has reserved the session.
3. No turn is active; the latest turn has a terminal driver event.
4. Output has been drained and the attempt outcome is durable.
5. Foreground tool windows are closed.
6. The native session locator/state and `last_settled_job_id` are durable. The
   additional non-mutating context fingerprint remains a rollout gate, not a
   claim about the current preview.

The supervisor then:

1. reaches the host-monotonic idle deadline under the local ownership lock, or
   after supervisor reconstruction receives an exact generation-fenced stop
   command from the durable control-plane deadline;
2. verifies that no turn, terminal, or broker file/Git operation owns the
   container (the baseline Agent CLI already exited at settlement);
3. issues `docker stop` with a bounded grace period;
4. verifies the exact container is stopped and no Agent CLI PID remains;
5. reports Docker truth on the next heartbeat, which records STOPPED and
   clears the idle deadline.

An orphaned failed activation uses the same exact fence but an explicit
`orphaned_attempt` reason, allowing the supervisor to discard only that dead
activation's in-memory token before stopping it. A stop failure remains due
and is retried; it is not reported as STOPPED.

## 11. Start and native-resume protocol

1. Reserve active host capacity and compare-and-set STOPPED -> STARTING.
2. Verify container id, labels, stopped state, image/runtime compatibility,
   native session id, state path, and last settled job.
3. Use the fresh attempt-scoped inference and MCP credentials minted for this
   activation.
4. `docker start` the same container and re-inspect its inert init shim as
   running.
5. Send the launch command and activation-only credential payload through an
   exec connection. It launches exactly one Agent CLI process using its native
   resume mechanism and the retained state directory.
6. Require the CLI's explicit resume operation to succeed and verify that its
   reported native session locator exactly matches the durable locator before
   accepting a turn. A non-mutating context fingerprint and explicit
   `restored_through_job_id` proof remain pre-rollout gates.
7. Record the new process generation, mark RUNNING, open the attempt window,
   and send the new message once. A diagnostic PID is later observability work.

If native resume fails, stop the container again, revoke the new credentials,
and mark LOST or an explicit resume failure. Never inject the prompt into a
blank session as if it were a successful continuation.

## 12. Credentials and configuration

- The runner's worker token remains attempt-scoped and outside the sandbox.
- The landed baseline uses one fresh attempt-scoped inference grant and MCP
  token for each activation/CLI process. The runner heartbeat renews the
  attempt while it owns the turn, and terminal attempt completion revokes that
  authority; keeping the Docker container RUNNING does not keep credentials
  alive.
- A future multi-turn live process cannot reuse the current attempt-scoped MCP
  token across fences. It would need a distinct activation-scoped credential
  or stable sandbox-facing relay that validates current residency on every
  request and cannot authorize inference or worker endpoints on its own.
- No activation credential is part of the container creation request. The
  container starts only an unprivileged init shim; fresh secrets are supplied
  to the Agent CLI exec and its tmpfs-backed generated configuration. `docker
  inspect` of the base container and `docker history` of fork images must not
  reveal them.

Runtime, compatible CLI version, repository, MCP policy, sandbox
image/execution profile, permission policy, and stable workspace path are
fixed when the chat is created. Model and reasoning effort remain per-turn
attributes exactly as the current API exposes them. A driver either applies
them in-band or rotates only the Agent CLI process and resumes local context;
it never creates a second container or silently ignores the requested value.

## 13. Full isolated fork

Fork creates a new chat that starts as a complete, consistent platform-owned
copy of the source at its **latest settled turn**, then diverges independently.
It is semantically equivalent to a new chat whose conversation and sandbox
already reached that boundary. It does not literally resend historical prompts
to a model: replay could repeat tool calls, mutate files twice, spend inference
again, and still fail to reproduce the original context.

This phase does not promise a historical-turn fork: database history alone is
insufficient because the container holds only the latest filesystem. A request
whose anchor is not the source thread's latest settled job returns a conflict
until per-turn filesystem snapshots exist.

The fork inherits the source's repository, runtime/version, last-turn
model/reasoning effort, MCP policy, sandbox image/profile, setup result, and
permission policy. Later model/effort changes remain available as ordinary
per-turn operations. Changing runtime/image/MCP/permission identity during the
clone is a different operation and creates a blank chat instead.

The coordinated protocol is:

1. Lock the source thread and require no queued/admitted/active turn. Verify
   that the requested anchor is `last_settled_job_id`. The trusted publisher
   takes the same thread lock before changing a succeeded turn to PUBLISHING;
   it either wins before preparation (and the fork is refused as busy) or
   waits until the fork saga releases its fence, never mutating publication
   lineage during the Docker snapshot.
2. Allocate a fork operation id, new thread and sandbox ids, deterministic
   container name, and generations. Persist a `PREPARING` saga row keyed to the
   source anchor so a crash can resume or remove only the incomplete fork. The
   Docker object id is recorded only after creation. Nothing is user-visible as
   a usable chat yet; do not hold a database transaction open across Docker
   calls.
3. Require the settled boundary, where the Agent CLI process has exited, then
   issue a container-local filesystem sync. Preserve every native journal and
   WAL file as opaque state, and bind the source's container-local session
   locator to `last_settled_job_id`.
4. If the source is RUNNING, commit its complete non-mounted writable layer
   with Docker's default `pause=true` consistency barrier. Docker briefly
   pauses and then resumes the container. If the source is STOPPED, commit it
   directly. There is no timeout stop/start requirement and no correctness
   dependency on the source PID.
5. Create a new, independently labelled container from that immutable snapshot
   image. The new container has no chat-state mounts and no inherited
   activation secret. Its writable layer is independent copy-on-write state.
6. In the clone, rewrite only platform-owned identity/journal metadata to the
   new thread/container generations and create its new output branch at the
   copied HEAD while preserving staged, unstaged, and untracked state. Do not
   rewrite the Agent CLI's opaque session files and do not invoke any CLI fork
   command. The copied native id is stored under the new sandbox namespace;
   sync and stop the clone at this ready boundary.
7. In one database transaction, copy the source conversation through the same
   settled job into the reserved `agent_thread`, insert the independently
   fenced target sandbox as STOPPED/RESTORED, and mark the saga READY.
   Jobs/attempts, container/activation generations, sandbox id, and publication
   lineage are newly allocated; no source identity or mutable row is reused.
8. Record the immutable snapshot image content id as the source and clone's
   common base fingerprint, separately verify the clone's rewritten branch and
   platform metadata, mark the new residency STOPPED/READY, finalize the fork
   relationship, and release the source lock. The existing API behavior
   remains: no inference starts until the user sends the first new message.
   That activation mints fresh credentials and uses the driver's ordinary
   resume operation in the cloned local state. A failure deletes only
   incomplete clone artifacts; it never modifies or removes the source.

The snapshot may share immutable Docker image layers physically. It must never
share a writable layer, chat-state volume, process, credential, terminal,
residency row, or future mutation. The opaque session id may have the same
string in each isolated filesystem; only its sandbox-qualified locator is a
platform identity. Temporary snapshot images remain referenced until the clone
container exists and reconciliation can prove the transaction complete.

The clone capability gate is stricter than ordinary stop/start resume: the
source and clone must both continue independently from the copied boundary.
If a CLI relies on mutable external session state keyed only by its native id,
the runtime fails sandbox-clone eligibility until ordinary resume can be made
independent. The platform does not hide that failure by calling the CLI's fork
feature.

`POST /jobs/{job_id}/fork` now runs the coordinated database + Docker
transaction above for retained chats. Legacy chats keep their pre-existing
history-only behavior so the preview does not silently change already-created
resources; a retained chat never falls back to that weaker copy.

## 14. Failure behavior before persistent storage

| Situation | Required behavior |
|---|---|
| Follow-up while RUNNING | Launch one process and restore the native context in the same already-running container; report LIVE without implying PID reuse |
| Follow-up while STOPPED | Start the same container and restore native context before admitting the turn |
| First turn of a new chat | Create container and native session; NEW |
| Settled idle timeout | Cleanly stop; retain container/workspace/session files |
| Owner cancellation or turn timeout after admission | Stop the exact activation and report STOPPED_DIRTY/LOST; never reinterpret it as settled idle or auto-inject the next turn |
| Settled Agent CLI process has exited while the container remains idle | Normal baseline state; the next turn launches one fenced process and natively resumes in the same container |
| Native resume failure | Stop and report LOST/resume failure; no blank-session fallback |
| Exact container or Docker data-root is proven missing on its owning host | LOST; persistent recovery is not implemented |
| Owning host heartbeat is absent, offline, disabled, or temporarily unreachable | Keep the residency pinned and report it unavailable; host-row state alone is not proof that Docker state was deleted. Re-enrol/recover the same named host and reconcile the exact container before declaring LOST; never replace it on another host. |
| Disk pressure | Alert/reject new-chat admission; do not silently clean stopped chats |
| Stale runner/supervisor | Generation fencing rejects every operation |
| Fork at latest settled state | Full conversation + Docker filesystem snapshot, ordinary resume in an isolated new container; no CLI fork or prompt replay |
| Fork at an older turn | Reject until that turn has a matching filesystem snapshot |
| Fork snapshot/driver/database failure | Roll back only incomplete fork resources; source remains unchanged and usable |
| Generic Docker prune or Docker data-root loss | Unsupported destructive operation; reconcile as LOST, never claim resume |

## 15. Acceptance tests

Universal rollout is complete only when all applicable tests pass against
every advertised pinned CLI and the production sandbox runtime. Items that
name the explicitly deferred fingerprint, R6, Kata, or live-provider work are
rollout gates rather than claims about this preview branch:

1. Turn 1 records NEW, and it plus three sequential follow-ups use one
   container id across four distinct activation generations, with no more
   than one Agent CLI PID at any instant.
2. Turn 1 records a native session locator; each follow-up sends only its new
   prompt through that CLI's explicit resume command.
3. Concurrent follow-ups serialize and never create a second container or
   overlap turn windows.
4. A different runner replica dispatches to the same supervisor/container.
5. Settled idle timeout produces Docker `exited`/STOPPED, not PAUSED or
   removed; no Agent CLI PID remains.
6. The stopped container retains workspace files, uncommitted changes, native
   state files, and the same container id.
7. The next message starts that exact container and proves restoration through
   the latest settled job without transcript injection; PID equality or
   inequality does not affect the result.
8. Each fresh follow-up CLI process restores the same native context in the
   still-running container and never overlaps another CLI root; an optional
   future live transport must also pass forced process replacement.
9. Setup is not rerun after start, and setup egress never reappears.
10. Credentials work across two live turns, are revoked at each attempt/process
    boundary, and fresh credentials are used for every activation.
11. Runner/supervisor failure around stdin admission never injects a job twice.
    Restarting the supervisor between settled turns reconnects to the exact
    container and either reuses or restores one CLI without losing context.
12. An active ambiguous timeout never becomes clean STOPPED.
13. A resume locator, context fingerprint, or restored-through-job mismatch
    injects zero user input and reports failure.
14. `docker inspect` shows no bind/named/anonymous chat-state mount, and the
    supervisor/broker can browse, patch, upload, download, and open a terminal
    without resolving any per-chat host path.
15. Container `Config.Env`, retained home/config, snapshot image history, and
    forked filesystem contain no clone, inference, MCP, or worker credential.
16. A latest-state fork contains source messages, setup dependencies,
    uncommitted/staged/untracked files, native conversation state, and runtime
    state as of one consistent settled boundary without invoking a CLI fork or
    replaying any historical prompt.
17. Source and clone have different thread/container/writable-layer/credential
    identities; the sandbox-qualified native locators differ even if their
    opaque CLI session-id strings match. Edits and messages diverge without
    cross-write or external-session interference.
18. Forking a RUNNING-idle source leaves its context, container, and usability
    unchanged regardless of PID. A failed clone leaves the source unchanged.
19. A historical-anchor fork is rejected before any new resource exists.
20. A stopped chat survives beyond the old host-workspace TTL and an
    infrastructure `docker compose down/up`, without automatic removal.
21. A definitively missing container or Docker data-root on the owning host is
    shown as LOST, never as restored continuity. Heartbeat or administrative
    host-row absence alone leaves the chat unavailable and pinned until that
    same host can be reconciled; it does not manufacture evidence of data loss.
22. A follow-up can change model and reasoning effort. The driver either
    applies them in-band or replaces one process and resumes context; the
    sandbox id and prior conversation remain unchanged.
23. Create/list/read/follow-up/cancel/archive/pin/thread-events/stats APIs keep
    their response and ownership behavior under restartable mode.
24. File read/write, Git view, bounded command, interactive terminal
    create/list/stream/input/resize/delete, patch/workspace artifacts, MCP tool
    calls, and draft-PR publishing pass parity tests against legacy mode.
25. An active terminal prevents a mid-operation stop. When an inactive PTY is
    reaped, Docker stop is still measured from its last activity rather than a
    second full timeout; workspace and terminal metadata remain truthful.
26. Every advertised pinned CLI passes native multi-turn, forced process
    replacement, stop/start restore, model/effort, tool-use, and independent
    full-sandbox-clone divergence tests before universal rollout.

## 16. Implementation order

| Phase | Content |
|---|---|
| **R0 — contracts** | Define NEW/LIVE/RESTORED plus RUNNING/STOPPED/container, activation, and process generations; preserve per-turn model/effort; specify at-most-once admission, credentials, latest-settled full-sandbox clone, and Docker-only state ownership |
| **R1 — retained container** | Stable labels; create/start/stop/inspect/exec/archive/commit; inert credential-free PID 1, no `--rm`, no chat-state mounts, sanitized repository import, same-container setup/files/terminal, secret tmpfs |
| **R2 — supervisor** | Host singleton, serialized per-thread ownership (the current singleton's host-wide lifecycle lock is a conservative superset), replica RPC, capacity admission, fencing, stopped-container reconciliation |
| **R3 — Claude proof** | Native session from turn 1; local session-id/state flush; process rotation; stop/start same container; `--resume`; context-proof telemetry |
| **R4 — all CLIs** | Codex `exec resume`, Pi `--session`, OpenCode/Kilo `run --session`; native restart-resume/model-effort contract tests at exact pins; live transport remains optional; promote only after real-Docker/Kata and provider conformance pass |
| **R5 — full isolated fork** | Latest-settled source lock/flush; Docker commit with pause; new container + DB prefix + ordinary cloned-state resume; no CLI fork; rollback/reconcile transaction |
| **R6 — operations** | Docker-layer disk/admission metrics, admin state, clean drain, orphan reconciliation, prune prohibition, staged rollout and rollback |

## 17. Explicitly deferred phases

### Persistent storage

No native session or workspace is exported in this phase:

```text
same retained container exists -> docker start + native CLI resume
container does not exist        -> continuity is LOST
```

The local source of truth is the Docker container writable layer. There is no
second host-directory copy to discover or restore. A fork-time Docker commit
is a local copy operation, not the persistent checkpoint/export protocol.

A later design may export a settled native session/workspace checkpoint before
container removal and restore it on another host. It must separately specify
encryption, atomicity, version gates, cross-host placement, and garbage
collection.

### Sandbox cleaning

There is deliberately no automatic removal policy yet. Cleanup comes only
after persistent recovery is designed, because deleting the retained container
before export would delete the only copy of this phase's workspace and native
session state.
