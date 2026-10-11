# Retained native sessions and shared-workspace Fork

- **Status:** implemented; this is the only executable chat-sandbox topology
- **Date:** 2026-08-31
- **Scope:** retained Docker containers, live Agent CLI sessions, immediate
  native-session Fork, concurrent sibling chats, lifecycle accounting,
  workspace access, and publication
- **Defers:** sandbox deletion, portable/cold storage, cross-host restore, and
  recovery after Docker loses a retained container or its data root

This document is the source of truth for chat execution. There is no rollout
flag and no disposable or one-to-one compatibility executor. Every newly
claimed chat must use this topology. Historical database rows that do not have
a shared retained sandbox are non-runnable and fail closed; they are not
silently upgraded, replayed, or routed through an older worker.

Host capability attestation remains mandatory. It is a safety proof that a
runner, broker, image, runtime pin, and protocol version match this design; it
does not select between old and new behavior.

## 1. Product topology

The normal case is a sandbox group with one member:

```text
chat A -> native session A -> live Agent CLI process PA
                              -> sandbox group G
                              -> Docker container C
                              -> /workspace
```

Therefore “one chat, one sandbox container” remains true until Fork. A normal
chat is still represented as a one-member group so lifecycle rules do not
need a second special case.

Fork adds a member to the same group:

```text
chat A -> native session A -> process PA --+
                                         +-> container C -> /workspace
chat B -> native session B -> process PB --+
```

The required invariants are:

1. A sandbox group owns exactly one retained Docker container and one
   `/workspace`.
2. A chat belongs to exactly one group and owns exactly one native Agent CLI
   session inside it.
3. A healthy, active member has its own supervisor-owned Agent CLI process.
   A settled turn leaves that process alive for the next message.
4. Fork creates another chat, native session, resident slot, and process in
   the same group. It does not copy the container or repository.
5. Parent and child may run turns simultaneously against the same workspace.
   There is no group-wide turn lock.
6. Each member admits at most one unresolved turn into its own session.
7. PID identity is an optimization, not the continuity contract. After
   `docker stop`, a process crash, or an incompatible process-fixed setting,
   the supervisor starts a replacement process over the same native session.
8. Native session state is authoritative. The platform never reconstructs a
   chat by copying a transcript into a prompt or replaying old messages through
   a model provider.

This deliberately behaves like opening two local Agent CLI terminals in one
repository. The workspace is shared; the conversations and process control
are separate.

## 2. Storage ownership

Chat data lives only in the retained container's Docker writable layer:

- `/workspace`, including Git metadata, dirty files, dependencies, and build
  outputs;
- `/agent-state`, including runtime-native sessions;
- `/home/agent/.freeinference-platform`, containing platform-owned native-fork
  receipts outside provider state roots that some CLIs enumerate;
- member-private provider homes where a CLI cannot safely share one home
  between concurrent processes.

Claude's launch settings and process registry live under each execution slot's
private `/home/agent-member-N/.claude-members/<thread-hash>` directory. Only
`projects/` links to the group's `/agent-state/claude/projects` native history, preserving Fork and resume
without loading the old shared settings. Claude loads only private user settings;
project and local settings in the shared repository cannot configure a sibling's
credential-bearing process. Private homes and settings are never widened by the
shared-workspace permission-normalization pass.
The thread key prevents a reassigned execution slot from inheriting a previous
member's settings; old slot processes are reaped before launch-state preparation.

No chat workspace, Agent CLI state directory, snapshot directory, named
volume, or per-chat host path is mounted into the sandbox. The host services
mount the Docker socket so they can address the container through Docker's
authority. The enrolled-host credential directory is infrastructure state,
not chat state.

`docker stop` preserves the writable layer. `docker start` wakes the exact
same container. The host's Docker data root must therefore be backed by the
durability expected from this phase. If Docker loses the container object or
its data root, every member in that group becomes `LOST`; there is no fake
recovery from database event text.

An optional deployment-wide archive policy is stored in the Agent database and
configured in **Admin → Settings → Sandbox**, not in the host environment. Its
destination is fixed at `s3://freeinference/cloud-agent/sandbox`; brokers use
their AWS workload identity, and no AWS credential enters the database,
runner-delivered policy, or sandbox. When enabled, the broker stores one
immutable initial `/workspace` archive after source setup and before the first
prompt, then a final `/workspace` plus native `/agent-state` archive after each
broker-managed clean stop. It does not archive individual turns. Versioned S3
objects are an export/forensics boundary only and are not a recovery path in
this design; the retained container remains authoritative. Policy transitions
are forward-only: enabling cannot manufacture a pre-prompt baseline for an
already-used sandbox, and disabling does not force a final capture. A baseline
or clean stop already durably prepared under an enabled revision still resolves
under that original revision after a policy change; disabling only prevents new
preparations. Operators who require paired initial/final objects enable and
confirm the policy before admitting those sandboxes, then stop or drain them
before disabling it.

## 3. Live Agent CLI sessions

The workspace broker owns one group supervisor. The supervisor owns one
resident transport per active member, rather than a short-lived runner owning
the process for one turn.

For a compatible follow-up while the container and process are running, the
runner sends only the new message to the existing transport. When the process
is absent but the container-native session is intact, the supervisor invokes
the runtime's native resume operation and then sends only the new message.

The five supported adapters are pinned and attested as a unit:

| Runtime | Resident/native mechanism |
|---|---|
| Claude Code | stream-json live process, native session id, reviewed `claude-session-fork` helper |
| Codex | app-server live thread, resume by rollout id, native app-server fork |
| Pi | RPC process, `--session` resume, RPC `fork`/`clone` |
| OpenCode | authenticated serve bridge and native session fork |
| Kilo | authenticated serve bridge and native session fork |

A runtime is claimable only when the exact sandbox image labels, runtime
versions, helper versions, live driver capability, broker configuration, and
host slot capacity match the protocol attestation. Version mismatch or missing
native state fails closed.

Model, reasoning effort, MCP configuration, and credentials may be fixed at
process start by an upstream CLI. A change that cannot be applied safely
restarts only the affected member over its native session. It does not restart
or invalidate sibling sessions.

## 4. Fork semantics

Fork is immediate. Clicking Fork invokes the selected Agent CLI's native fork
operation before the API returns a usable child chat. It is not deferred until
the child's first message.

The selected anchor may be any successfully settled native turn in the source
chat, including a historical one, once it has a positive persisted
`native_turn_no`, the owner host advertises at-anchor support, and the group's
own image can take the anchor (§12). Failed/cancelled turns and
successful rows whose ordinal has not settled are neither advertised nor
accepted as anchors. A sibling may continue running because the workspace is
intentionally shared; only the source session is quiesced while its immutable
prefix is forked.

The platform assigns `native_turn_no` when a CLI turn settles, before final job
state is written. It passes that stable ordinal to the host, which resolves the
current provider-specific identifier at mutation time. Provider ids are not
stored as platform history: Claude Code and OpenCode/Kilo can rewrite message
ids in a fork, whereas the ordinal remains stable within each copied prefix.
Codex maps it to inclusive `lastTurnId`; Claude maps it to the inclusive
top-level assistant UUID; Pi forks before the next user entry (or clones the
leaf); OpenCode/Kilo pass the next user `messageID` as an exclusive boundary.

Those adapters count **platform prompts**, not every provider record whose role
is `user`. Claude tool results are user-role JSONL entries and the pinned SDK
does not preserve their `parent_tool_use_id`; the adapter therefore reads the
raw current chain, excludes `tool_result`, meta/sidechain, and compact-summary
records, and follows a compact boundary's `logicalParentUuid` to retain
pre-compaction ordinals. OpenCode/Kilo exclude user messages containing a
`compaction` part and all-synthetic auto-continue parts. This keeps one
platform turn mapped to one ordinal even when the provider performs tools or
compaction internally; an unrecognized history shape fails closed.

The durable sequence is:

1. Create or recover an idempotent fork operation and mark only the source
   membership `fork_preparing`.
2. Ensure the exact retained container is running and reserve a child member
   slot.
3. Resolve the selected settled native-turn ordinal inside source session `S`,
   flush runtime state as required, and invoke the reviewed prompt-free fork
   surface at that boundary.
4. Receive and validate a distinct native session id `T`.
5. Materialize member-private provider state when that runtime needs it, while
   keeping `/workspace` shared.
6. Start and probe a child resident transport over `T`; resume the source over
   `S` if its fork implementation required a process restart.
7. In one fenced database transition, create the child chat/membership,
   persist `S -> T`, copy the visible settled history prefix for UI display,
   and mark the child ready.
8. Return the child chat only after both native identities are durable.

The fork journal in the Docker layer makes a retry idempotent when the native
mutation completed but the HTTP response was lost. An ambiguous native result
fails closed instead of issuing another fork or inventing a child id.

Fork never:

- calls `docker commit`, `docker create` from a snapshot, or clones a sandbox;
- creates a second workspace or Git branch;
- sends a synthetic prompt to a model;
- copies an event transcript into model context;
- waits for the child's first user message to materialize the native fork.

The UI exposes Fork once per turn, on its final settled assistant/tool event,
never on an intermediate streaming or tool boundary. “Edit & rewind” is this
same operation from the user-message side: Fork is executed immediately at the
nearest preceding forkable assistant boundary, skipping failed or unsettled
turns; the child opens with only that prefix, and the selected user prompt is
placed in the child composer as a one-shot draft. Submitting the edit is a new
native turn; the original prompt is never replayed automatically.

## 5. Shared workspace consequences

Conversation histories diverge after Fork; files do not. Both agents see all
workspace effects immediately. Consequently:

- simultaneous writes follow real filesystem ordering;
- a member may observe a sibling's partial edit;
- Git operations may contend on `.git/index.lock` or change shared HEAD/index;
- cancellation stops only the selected Agent CLI turn and does not roll back
  filesystem changes already made. After prompt admission the selected
  member's native context fails closed as `LOST`, because the five CLIs do not
  expose one portable, provable cancelled-turn boundary; sibling members remain
  usable;
- a publication snapshot can contain changes produced by multiple members.

These are product semantics, not a race the platform hides with a lock. A
future feature that needs independent branches or isolated filesystems must
create a different workspace operation; it is not this Fork.

## 6. Concurrency and credential isolation

No group-wide prompt mutex exists. Parent and child can both be `RUNNING`.
Correctness fences are narrower:

- one unresolved turn per member;
- one process generation and residency generation per member;
- one activation/event/cancellation stream per attempt;
- group operation references around Docker start/stop;
- one terminal-suspension token per protected turn, so PTYs resume only after
  every concurrent sibling has left its preparation/finalization phase;
- a short source-member fork transition;
- a bounded quiescent publication capture.

Resident processes use member execution slots with separate UIDs and private
tmpfs credential paths. Siblings do not share residency grants, attempt grants,
MCP tokens, activation directories, process generations, event streams, or
cancellation authority. A member may see the shared workspace and eligible
runtime-native project data by design, but it must not read another process's
credential from argv, disk, `/proc`, or reusable environment files.

Resource attribution follows the same ownership boundary. Whole-container
Docker CPU, memory, I/O, and network counters belong to the group and are never
copied onto a member attempt. Every current turn records an explicit
zero-coverage `shared-member-v1` aggregate plus tool timing; Kata additionally
records fork/exec/exit and settled resource counters through a guest-root
observer filtered by the member's fixed UID. Trusted non-Kata mode degrades to
timing without process totals. Observation closes before member reaping and
permission normalization, and cancellation/timeout drains the broker's terminal
telemetry frame when the stream remains reachable.

Turn cleanup uses the root-owned, mode-0555 `member-process-reaper` embedded in
the sandbox image. It captures a member UID's `(pid, /proc starttime)` baseline
in helper memory and accepts the baseline for cleanup over stdin, rather than
trusting a member-writable pidfile. Before each TERM/KILL it rechecks UID and
starttime to fence PID reuse; after escalation it rescans and fails closed if
any non-baseline process survives. A new trusted runner also carries the same
reviewed helper and installs it root-owned/mode-0555 before activating a
retained pre-upgrade container that lacks it, so an in-place host rollout does
not strand ordinary follow-ups on the old writable layer.

Public multi-tenant deployments still require the documented Kata/gVisor,
non-root, capability, seccomp, and resource boundaries. The shared workspace
does not change the sandbox network, which is one network with nothing to
select ([design](2026-09-23-agent-public-egress-design.zh-CN.md)): setup, the
agent and every Fork member share the group's one routable Docker bridge,
populated with only that group container and the two fixed inference-gateway
and MCP-only relays. This deployment's host nftables tables, installed and read
back before the bridge exists, let the group reach public IPv4 on TCP 80/443,
the one resolver in `AGENT_EGRESS_DNS_SERVER` and those two relays, and refuse
the host, private and special-purpose addresses, other groups, other ports and
IPv6. Docker's own network flags are not the boundary. The relay network the
two relays are discovered on is never joined by a sandbox. Docker atomically
allocates each sandbox generation's bridge as a `/29` from
`AGENT_EGRESS_CHAT_NETWORK_POOL`; stop removes it, wake recreates it, and broker
reconciliation prunes labelled orphans after a five-minute grace period so it
cannot race an in-flight bridge attachment. Setup and the agent never switch
networks, so there is no bridge swap and no Kata guest restart between them.

## 7. Lifecycle and inactivity

Every chat, including an unforked chat, belongs to a sandbox group. The idle
deadline is computed from the group's latest real activity, which is the
maximum across its members and broker operations.

Activity includes admitted Agent turns and owner file, terminal, Git, Fork,
and publication operations. An idle resident process alone is not activity and
does not keep a group alive forever.

At the inactivity deadline (1800 seconds by default):

1. the host supervisor verifies under the group admission fence that no member
   turn, terminal, or broker operation is active;
2. it flushes native state, closes all resident processes, suspends protected
   terminal activity, and performs `docker stop` on the exact fenced container;
3. when the database-backed archive policy is enabled, it synchronously stores
   the exact stopped container's final workspace and native history; an
   exhausted bounded upload fails the clean-stop operation rather than silently
   claiming archive success;
4. it disconnects the stopped container from its group bridge and removes
   that bridge, while retaining the same container writable layer;
5. the next authenticated host heartbeat records the group as stopped without
   deleting it;
6. after a broker restart, when the in-memory deadline is unavailable, the
   control plane applies the same durable activity/admission fences and a
   bounded recovery window before issuing the exact-generation stop command.

Any later interaction with any member recreates the group's one bridge, host
rules first, starts the same container, restores only the member transport(s)
needed for that interaction, and refreshes the common group deadline. A stopped
sibling need not be eagerly restored. Broker startup stops running containers
whose resident process ownership cannot be reconstructed before allowing a
fenced wake.

Container lifecycle is group-scoped: activity from either parent or child
keeps their shared container running. A fatal container loss is group-scoped;
a member process failure is member-scoped.

## 8. Context and continuity labels

Product context state is:

- `NEW`: the member has no previously settled native turn;
- `LIVE`: this turn used the compatible resident process already running;
- `RESTORED`: a replacement process resumed the same native session from the
  retained Docker layer;
- `LOST`: the required native state/container cannot be proved intact.

`LIVE` and `RESTORED` both have full native model context. Their difference is
whether the process was reused, not whether history was copied to the model.

HOT/WARM/COLD/REPLAY remains diagnostic telemetry for older metrics and UI
rows. It does not choose an executor:

- HOT corresponds to a compatible live process;
- WARM/native corresponds to native resume in a running or restarted
  container;
- COLD means the retained container is stopped;
- REPLAY is never an allowed recovery path for this topology.

## 9. Workspace APIs and runner responsibilities

The control plane owns job, attempt, lease, group, membership, residency, and
fork fencing. It routes a workspace request only to the broker registered by
the group's owning host.

The host runner claims one turn, prepares the first checkout or imports an
approved published base, obtains attempt/residency grants, and streams the
turn through the group supervisor. It does not own chat files on its own
filesystem and does not manufacture context.

The workspace broker is the authenticated Docker authority for:

- file browsing and writes;
- interactive terminals and bounded workspace commands;
- Git status/diff operations;
- group/member start, stop, resume, native Fork, and resident transport;
- quiescent workspace capture for publication.

Missing broker credentials or a broker address fail readiness/claim instead of
silently falling back to a host worktree or GitHub snapshot.

## 10. Publication snapshot

“Quiescent snapshot capture” is publication coordination, not session
persistence. The publisher needs a stable filesystem epoch from a workspace
that siblings can mutate concurrently.

Capture therefore waits for a bounded quiet point, takes a credential-free
workspace snapshot through the exact owning container, and releases the
barrier promptly. It does not stop normal idle resident transports permanently,
does not create a chat restore artifact, and is not used to rebuild model
context. If the group is stopped, capture may start that same fenced container
without replacing it.

## 11. Durable model and failure behavior

The executable records are:

- `agent_sandboxes`: group/container identity, owning host, generation,
  runtime/image/profile pins, state, activity, and idle deadline;
- `agent_thread_sandbox_memberships`: chat-to-group identity, native session id,
  member slot, admission/process/residency generations, and context state;
- `agent_session_forks` and `agent_native_session_edges`: idempotency and
  source/target session lineage;
- existing jobs, attempts, leases, events, and publication records.

All mutations are fenced by attempt, sandbox, activation, process, or
residency generation as appropriate. A late result from a superseded owner
cannot settle, stop, publish, or revoke the current owner.

Rows from the removed execution topology may remain in historical migration
tables for audit and schema compatibility. They are not joined into runnable
sandbox lookups. Follow-up, Fork, terminal, file, and claim operations on such
a chat return a context/workspace-unavailable error.

The admin active-host selection chooses where a chat without a sandbox may be
materialized. It does not move an existing group: follow-up turns remain
hard-pinned to the host that owns that group's Docker writable layer. Moving a
group requires the deferred portable-storage protocol, not a scheduler pin.

## 12. Deployment contract

The topology is unconditional in local, staging, and production code. A usable
host must provide:

- an enrolled host credential and fresh heartbeat;
- an authenticated workspace broker URL reachable by the control plane;
- the expected Docker data root and sandbox image digest;
- exact runtime/helper versions;
- `native-fork-at-anchor-v1` from the current host image, and for each
  retained group either that very image or the exact CLI version the group's
  container runs plus its recorded compatible fork helper (below);
- at least two host slots and enough runner replicas to execute fork siblings
  concurrently (staging currently defaults both to three);
- an immutable resident-slot capacity for each group;
- the required isolation configuration, and the sandbox network's host rules
  installed and read back.

The host and control-plane GitHub Actions jobs start in parallel during a
rolling deployment. The control job may build images, start/health-check its
database, and run a backwards-compatible migration while the old API remains
live. After the host's runners, broker, tunnel, isolation configuration, and
Docker data root pass local checks, it publishes a capability containing the
exact target commit. Before replacing any running API container, the
control-plane deploy requires a fresh heartbeat carrying that commit from every
host currently eligible to materialize a new chat. It also reverse-probes every
online host that still owns a non-lost retained group, even when new-chat
placement has moved elsewhere. Such an owner may remain on an older compatible
generation for ordinary follow-ups; it does not need to claim new chats or
advertise historical Fork. The scheduler admits such a replica only to an
existing group owned by that host, and does not ask its image for the fork
helper, which an ordinary turn never runs; creating a new group still
requires the pinned fork helper.
This runtime fence, not a serial Actions dependency, establishes
host-before-new-API compatibility. Failure stops the rollout; it never falls
back to an older sandbox path.

While that marker is pending, replacement runners still enroll and heartbeat
the base protocol capabilities needed by host-local checks, but do not poll for
jobs. The pause is runner-side because the previous API serving during a
parallel rollout does not understand the new exact-SHA claim field; relying
only on the target API's gate would leave a window in which the old API could
assign work to an as-yet-unverified replacement.

The schema baseline (`0001_schema_baseline`) carries `native_turn_no` with a
unique partial index per thread and a CHECK that a `succeeded` job has one.
The settlement transaction assigns the ordinal explicitly before the job's
terminal transition — for a turn that settled its native context and then
failed as much as for a successful one — so no trigger reconstructs it and no
rolling-deploy guard remains. Follow-ups are stored as `queued`; claiming
requires all earlier turns in the thread to be terminal. A successful parent
also requires a usable retained membership. A child whose parent succeeded
but whose membership is since lost fails instead of being materialized as a
fresh native session. An actual retained membership is the only proof that a
chat remains runnable after successful execution.

When either the old API or the MCP relay is absent, readiness cannot complete
(a true cold start has neither). The control script starts Postgres, migrates,
then independently bootstraps only each missing target service so a
simultaneously starting host can enroll, send the required fresh heartbeat, and
prove both fixed sandbox relay paths. Web remains stopped until the exact-SHA
gate succeeds. If the gate fails, cleanup stops only the bootstrap services
started by that invocation; an already-running API or relay is not rolled back.
Without this bounded bootstrap, a fresh install, recovery after `compose down`,
and either partial-service state deadlock: readiness waits for an HTTP
heartbeat and MCP probe while a process that must answer is kept stopped.

The complete local stack follows a separate credential-safety preflight.
`deploy/local/.env.local` and its generated Postgres password must be retained
with the Compose project's database volume. If the file is lost while that
volume exists, `dev.sh` refuses to generate an unusable replacement or reset
the database. `up`/`start` bring up Postgres alone, authenticate through the
configured DSN, and run Alembic before starting the complete stack. `up` builds
the required images before that database-only preflight; `start` does not
rebuild them.

There are no `AGENT_RESTARTABLE_*`, `AGENT_SHARED_LIVE_*`, or dispatcher-token
feature switches. Names such as `shared-sandbox-v1` are protocol/capability
identifiers used for exact claim attestation, not behavior toggles. The
deployment-SHA marker is likewise a rolling-compatibility fence and cannot
select another executor. Historical fork has its own
`native-fork-at-anchor-v1` attestation, and the in-image helper that
interprets the anchor is pinned by its own version label
(`claude-session-fork`) beside the other helpers; there is no separate
shared-sandbox protocol number. The group records this helper's version when
the runner creates its container. The control plane exposes Fork only when
the host advertises that capability and either still runs the group's very
image, or the group's recorded helper matches the reviewed pin and the host
ships the exact CLI version the container runs. An unrecorded or unreviewed
helper stays on the exact-image rule; it is never assumed to understand a
historical anchor. Every release labels its image with its source commit, so
comparing only digests would disable Fork on each deployment even when the
helper and CLI remain compatible. Mid-turn input asks only for the host's
per-runtime capability and the same CLI version; it uses no fork helper.

## 13. Acceptance contract

The implementation is ready only when these properties hold:

1. A normal chat creates one group, container, workspace, membership, native
   session, and resident process.
2. Compatible sequential turns reuse the live process; stop/start resumes the
   same native session in the same container.
3. Every supported runtime passes real-binary new, live follow-up, stopped
   restore, and prompt-free native Fork tests at its exact pin.
4. Fork returns only after a distinct child native id and live child transport
   exist; it creates no Docker clone, mount, host directory, or model request.
   Forking a historical successful, ordinal-bearing turn copies exactly that
   native/display prefix; only its final assistant/tool boundary is actionable,
   and Edit & rewind opens the replacement prompt as an unsent draft.
5. Parent and child can run simultaneously and edit the same workspace without
   a group busy error or cross-routed events/cancellation.
6. Repeated forks support multiple members within configured slot/resource
   capacity.
7. Group latest activity prevents idle stop while any member/operation is
   active; either member restarts the same stopped container.
8. Missing container/native state, mismatched image/runtime pins, ambiguous
   fork recovery, stale generations, and historical non-shared rows fail
   closed.
9. File, terminal, Git, MCP, reasoning effort, cancellation, publication,
   auth, egress, and frontend flows continue through the owning broker and
   shared group.
10. Docker inspection shows no chat-state bind or volume mount and the
    configured Docker data root is the one the broker actually controls. Two
    unrelated sandbox groups do not share a Docker network; a group's one
    bridge, used by setup and the agent alike, contains only its container and
    the fixed inference/MCP relays. Stop releases the bridge, wake recreates
    one from the configured pool, and startup removes abandoned labelled
    allocations.
11. Staging deployment requires broker credentials, enrolled host identity,
    exact attestation, and matching executor/slot capacity before a claim.
12. `NEW`, `LIVE`, `RESTORED`, and `LOST` match native context truth; no replay
    is presented as recovery.
13. A member attempt never receives its shared container's aggregate resource
    totals. Unavailable, partial, and complete process observations remain
    distinguishable on success, cancellation, timeout, and boundary failure.

The repository suite and Docker-only local smoke cover the deterministic
control-plane, host, broker, all-five-runtime, stop/restore, historical
immediate Fork, and concurrent-turn paths. Real-provider traffic, production
isolation under Kata/gVisor, adversarial multi-process tests,
OOM/container-exit stress, and disk-pressure operations remain deployment
gates where applicable.

## 14. Deferred persistence

This phase intentionally does not export session history to a mounted drive or
object store. The host integrates durability by provisioning Docker's own data
root on the desired persistent storage. A later portable-storage design must
define an atomic, runtime-specific export/import contract and cross-host
identity fencing; it must not reintroduce transcript replay as a silent
fallback.
