# Concurrent live native-session forks in one shared sandbox workspace

- **Status:** implementation-ready proposal v5 — documentation only; not yet
  implemented
- **Date:** 2026-08-31
- **Depends on:**
  `2026-08-28-one-chat-one-restartable-sandbox-design.md`
- **Proposes replacing:** that document's runner-owned one-shot retained CLI
  process model and retained-chat fork behavior; ordinary
  one-chat/one-container ownership remains unchanged
- **Scope:** supervisor-owned live Agent CLI transports while Docker is
  running, latest-settled native Agent CLI forks, multiple concurrent chat
  sessions/processes in one container/workspace, lifecycle accounting, shared
  Git/publication, and removal of Docker-clone fork
- **Defers:** historical-turn forks, independent filesystems/branches for
  siblings, cross-host restore, cold storage, and sandbox deletion

This document is a proposed next design. The current branch still implements
runner-owned one-shot retained turns and the full Docker clone in section 13 of
the existing restartable-sandbox spec. No API, UI, or operational
documentation may claim this proposal is live until the schema, drivers, host
protocol, frontend, and concurrency tests land together.

---

## 1. Product decision

The required topology is:

1. A chat that has not been forked owns one sandbox group containing one
   Docker container, one workspace, and one native Agent CLI session.
2. Fork creates another chat in the **same** sandbox group. It branches the
   selected chat's native Agent CLI session but does not copy the container,
   workspace, or Git repository.
3. Every ready member chat owns its own native session and, while the sandbox
   group is running, normally one supervisor-owned live Agent CLI transport.
   After fork, two member processes remain available simultaneously in the
   same container against the same `/workspace`, including while both are
   executing turns.
4. There is no sandbox-group turn mutex. A running turn in A does not make B
   busy and does not prevent B from starting or continuing its own turn.
5. Each individual chat still admits at most one unresolved turn into its own
   native session at a time. This is a per-session correctness fence, not
   cross-chat avoidance.
6. A settled turn leaves that member's CLI transport alive and idle; the next
   message is sent directly to it. The idle process does not refresh activity
   or prevent the group timeout. PID identity is still not a correctness
   guarantee: a process crash, supervisor restart, incompatible model/MCP
   change, explicit capacity recovery, or `docker stop` may replace it by
   native resume without losing the logical session.
7. Repeated forks may create N member chats in one sandbox group; concurrent
   execution is bounded only by configured host/container resource admission,
   never by an artificial limit of one active member.
8. The retained Docker-clone fork path is removed. Fork always means native
   conversation fork into the same sandbox/workspace.

The ordinary case remains one-to-one:

```text
chat A -> native session A -> live process PA
                              -> sandbox group G -> container C -> /workspace
```

After fork, both chats can execute concurrently:

```text
chat A -> source session F -> process PA ─┐
                                         ├─> container C -> the same /workspace
chat B -> forked session B -> process PB ─┘
```

`PA` and `PB` are owned by the sandbox-group supervisor, not by whichever
short-lived runner happens to deliver a turn. A runner may disappear after a
turn without ending either idle transport. On the normal path, later messages
reuse the existing process; on a recovery path, the supervisor starts a new
process over the same native session id before accepting the next turn.

This deliberately matches opening two local Agent CLI terminals in the same
repository. The platform does not hide or serialize their workspace effects.

The minimum authoritative relationship is intentionally small:

```text
sandbox group G -> container C, /workspace, last_activity_at
chat A -> group G, native session A, resident process generation PA
chat B -> group G, native session B, resident process generation PB
```

Session continuity needs no per-chat volume, host bind mount, container clone,
or PID identity. `/workspace` and the runtime's durable `/agent-state` are the
shared Docker-owned data; each membership's native session id selects the
correct conversation inside that state. Native transcripts are same-owner
group data, not a hostile ACL boundary between sibling chats. The platform
still isolates credentials, process control, event routing, and authorization
per membership as described in section 11.

## 2. Meaning and consequences of one shared workspace

The native session transcripts diverge after fork. The filesystem does not.
Both agents immediately observe the same files, dependencies, processes,
repository instructions, Git metadata, and uncommitted changes.

Consequences are intentional product behavior:

- two agents may edit the same file at the same time; the resulting content is
  determined by the actual write order;
- one agent may observe a partially completed edit from the other;
- concurrent Git operations may contend on Git's own `.git/index.lock` or
  change the shared HEAD/index underneath the other process;
- a checkout, reset, stash, commit, package installation, dev server, or
  generated file from either chat affects every sibling;
- cancellation stops only the selected chat's Agent CLI process, but it does
  not roll back filesystem effects that process already made;
- a per-chat patch cannot be assumed to contain only that chat's edits.

These are the same hazards as two local coding agents pointed at one working
tree. If deterministic file attribution, an independent branch, or an
independent PR is required, it needs another workspace design and is outside
this Fork operation.

The shared CLI state root may also contain project-global memory, settings,
caches, tasks, or plugin state. For example, repository `AGENTS.md`/
`CLAUDE.md` files and runtime project memory can be observed by both sessions.
Fork isolates the native conversation transcript, not every ambient input.
Activation credentials and platform security policy remain isolated per
attempt and must never be stored in that shared state.

## 3. Native fork protocol

Fork remains restricted to the selected source chat's latest settled native
turn. The source chat itself must not have an admitted/active turn, but another
sibling chat may continue running and mutating the shared workspace while the
fork is created. There is no filesystem snapshot to coordinate with.

### 3.1 Fork is materialized by the Fork action

The Fork API must finish the runtime-native fork before it returns a usable
child chat. It must not defer the operation until either chat's next message.
It also must not invent a model prompt, start inference, replay the source
conversation through a provider, or implement a private transcript transform.

Use this protocol:

1. Compare-and-set only the source membership into a short `fork_preparing`
   transition. This prevents another message from appending to that exact
   native session while its settled prefix is copied; it does not block
   sibling memberships or their running turns.
2. Require the source's latest turn to be settled. Quiesce that member's idle
   resident transport and flush its native state. A runtime whose reviewed
   resident protocol can fork the idle session keeps the process alive; a
   runtime that requires an on-disk helper closes and later natively restarts
   only the source process. Other member processes remain running.
3. If the shared container is stopped, start that exact container. Reserve the
   hidden child membership's resident/host slot before mutating native state.
   Let `F` be the source membership's current native session id, and invoke the
   pinned runtime's reviewed **fork-only** surface inside the container.
4. The runtime returns a distinct, already-persisted target native session id
   `B`. Verify `B != F`, that `B` belongs to the expected runtime state root and
   workspace, and that no provider request or model turn occurred.
5. Redeem the reserved resident slot and start a supervisor-owned live
   transport over `B` without sending a model prompt. Verify it is idle and
   ready; also verify the source transport over `F` is still live or has been
   natively restarted. A launch failure exposes no child and releases the
   reservation during reconciliation.
6. In one database transaction, create the child chat and copied visible
   history prefix, store `B` on its membership, record the immutable native
   edge `F -> B`, mark the child `ready`, and clear the source transition.
7. Only then return the child chat. The parent continues to own live `F`; the
   child owns live `B`. Either can receive a normal message immediately through
   its existing transport, and both can run concurrently in `/workspace`.

Because `B` already exists before the source fence is released, later writes
to `F` cannot change the child's anchor. The parent therefore does **not** need
to fork itself:

```text
before Fork action:    A -> idle live session F

during Fork action:    A/F is briefly fenced
                       native fork-only F -> B

after API success:     A -> live process PA -> writable session F
                       B -> live process PB -> writable session B

concurrent messages:   A -> sends directly to PA/F
                       B -> sends directly to PB/B
```

A member with no settled native context cannot be forked. Repeated forks are
ordinary immediate operations: forking A again creates another child from A's
then-current settled `F`; forking B creates a child from B's then-current
settled native id. There is no pending source state or deferred first-message
protocol.

### 3.2 Fork saga and crash boundary

The operation crosses the control plane and owning host but never holds a
database transaction open across HTTP:

1. The client creates one request id and reuses it for every retry. A short
   transaction creates or returns the unique session-fork operation for
   `(user_id, client_fork_request_id)`, preallocates the hidden child ids, and
   CAS-fences only the source membership/generation.
2. The host starts the exact container if necessary, quiesces and flushes only
   that member's idle resident transport, then runs fork-only from the
   revalidated source locator. It preserves that process when the reviewed
   runtime protocol permits, otherwise it restarts only that member by native
   resume. The operation counts as group activity. Fork-helper
   mutations are serialized per sandbox group so one helper UID/private auth
   channel never serves two operations at once; this short mutex orders only
   Fork actions and does not block any Agent turn, terminal, or workspace
   operation in another membership.
3. Before invoking the CLI, a credential-free fork helper writes a durable
   PREPARING journal under a platform-owned directory in the Docker writable
   layer. It invokes the operation idempotently and atomically writes the
   runtime, exact runtime version, source id `F`, target id `B`, sandbox
   generation, and operation generation to the READY journal. A retry reads
   that receipt instead of forking again. The journal is not a host mount and
   contains no grant or provider credential.
4. The host returns that receipt. The control plane records the operation as
   MATERIALIZED and rejects a receipt for the wrong source, sandbox, runtime,
   version, or generation.
5. The host reserves the preallocated child's resident slot, starts and probes
   its live transport over `B`, and includes its process generation in the
   receipt. A final transaction creates the child membership/history and
   native edge, stores `B` as `ready`, marks the operation READY, and clears
   the source transition. The child is not externally visible before this
   transaction and is not returned before its transport is ready.
6. A failure before the native call changes no native or visible child. A
   failure after the runtime persisted `B` or started its hidden transport but
   before finalization may leave an unreachable native/process orphan; it never
   changes `F`. Reconciliation finalizes a recorded receipt when possible,
   otherwise it closes only the preallocated child process generation, scrubs
   its slot, and an unreachable-session collector removes `B` after proving
   that no membership or native edge owns it.
7. A reconciler retries or fails only the exact operation generation. It never
   stops sibling processes or changes their memberships. Retrying an ambiguous
   no-inference fork may create another unreachable native copy, but it must
   expose at most one finalized child.

The operation table has the states `preparing -> materialized -> ready`, plus
terminal `failed`. A unique client request id closes the "database committed
but HTTP response was lost" duplication window. The in-container receipt closes
the "host returned but the control plane restarted" window. The only remaining
ambiguous window is a runtime persisting `B` immediately before the helper is
killed; such a target stays unreachable and is handled by the verified orphan
collector, never guessed into a chat.

After the Fork API succeeds, parent and child turns use the existing ordinary
prompt-admission and at-most-once rules. A later crash is scoped to the member
whose turn was admitted; sibling native sessions remain valid. Partial
filesystem effects remain visible because the workspace is shared.

## 4. Agent CLI capability audit

The exact local sandbox image pins and resident surfaces are:

| Runtime | Current pin | Supervisor-owned live transport | Button-time fork-only path | Status |
|---|---:|---|---|---|
| Claude Code | CLI `2.1.220`; Agent SDK not yet installed | `--print --input-format stream-json --output-format stream-json`; the current driver already has `ClaudeCodeLiveSession` | Pin and call Anthropic's official [`fork_session(F, directory="/workspace")`](https://github.com/anthropics/claude-agent-sdk-python/blob/v0.2.148/src/claude_agent_sdk/_internal/session_mutations.py#L220) local-session API; briefly flush/restart the source if the live CLI cannot be forked safely in place | **Requires an exact reviewed Agent SDK pin** and moving the existing live handle from runner ownership to the supervisor. Plain headless `--fork-session` is not fork-only |
| Codex | `0.145.0` | `codex app-server --listen stdio://` with `thread/start`, `turn/start`, `thread/resume`, and `turn/interrupt` | Native app-server `thread/fork` | The exact installed `0.145.0` generated schema already contains all five methods. Select one exact reviewed pin (`0.145.0` or an intentionally upgraded version such as `0.148.0`) only after operational and event-parser conformance; an upgrade is not required merely to obtain the topology |
| Pi | `0.73.1` | `pi --mode rpc --session-dir ... --session F --offline` | Start RPC with `--fork F`; its session manager creates the target before waiting for input | Supported by the exact installed CLI and [session contract](https://github.com/earendil-works/pi/blob/main/packages/coding-agent/docs/sessions.md); needs live multi-turn, fork-only receipt, and concurrent JSONL/global-state conformance |
| OpenCode | `1.18.9` | One authenticated `opencode serve` process per membership | Resident service `POST /session/F/fork?directory=/workspace` | Supported by exact [`Session.fork`](https://github.com/anomalyco/opencode/blob/v1.18.9/packages/opencode/src/session/session.ts); exact-image offline probe produced a distinct session id without a prompt |
| Kilo | `7.4.17` | One authenticated `kilo serve` process per membership | Resident service `POST /session/F/fork?directory=/workspace` | Supported by the exact pinned image; offline probe produced a distinct session id without a prompt; needs source-level pin review and concurrent shared-database conformance |

The fork-only audit changes an important earlier assumption. OpenCode and Kilo
both expose a persistent native session service even though their `run --fork`
commands combine fork with a prompt. Pi's RPC mode remains alive for repeated
messages and performs `forkFrom` during initialization before it reads one.
The exact Codex `0.145.0` app-server schema already exposes native
`thread/fork`; `0.148.0` merely adds a convenient no-prompt `codex exec fork`
surface. Claude's headless `--fork-session` waits for input, but Anthropic ships
a separate official local `fork_session()` mutation API. These are native
runtime integrations, not platform-owned transcript transforms.

Local exact-image probes established that OpenCode `1.18.9` and Kilo `7.4.17`
create distinct persisted ids through their loopback APIs with networking
disabled. A Claude `2.1.220` probe established that a local slash command can
trigger `--fork-session` without inference, but it adds command records and is
therefore not the production design. The official Agent SDK helper must be
pinned in the sandbox image and tested against CLI `2.1.220` instead. A local
exact-image schema probe confirmed that Codex `0.145.0` contains
`thread/start`, `turn/start`, `thread/resume`, `thread/fork`, and
`turn/interrupt`; a real app-server/provider probe still gates the selected
production pin.

Each OpenCode/Kilo resident service must be authenticated even though it binds
`127.0.0.1`: sibling UIDs share the container network namespace and can reach
loopback. The exact OpenCode image supports
`OPENCODE_SERVER_USERNAME`/`OPENCODE_SERVER_PASSWORD`; the exact Kilo image
supports `KILO_SERVER_USERNAME`/`KILO_SERVER_PASSWORD`. A random local-control
password lives only in that membership slot's private tmpfs/environment and is
rotated whenever the resident process generation changes. The service runs in
pure mode on a random loopback port and terminates when the membership is
closed or the container stops. A separate recovery helper, if needed, uses an
ephemeral per-operation password. An exact-image Kilo probe returned HTTP 401
without credentials and 200 with the configured `kilo` Basic Auth identity.
The rollout suite repeats this check for both runtimes and proves a sibling
slot cannot read the password from `/proc` or private tmpfs.

The OpenCode/Kilo port is never published to the host or another Docker
network. The supervisor drives it through a slot-owned in-container control
client created with the Docker exec API; Basic Auth is read from a `0600`
private tmpfs file rather than placed in argv. Claude, Codex, and Pi keep their
framed stdin/stdout pipes directly in the supervisor. Thus every transport is
reachable through the existing Docker authority without a host bind mount or a
new sandbox egress destination.

The existence of a flag is insufficient. Every exact image must pass a real
provider test with two simultaneous processes that proves:

1. each resident transport accepts two or more sequential turns without
   replacing its normal-path process, while preserving per-turn event and
   usage boundaries;
2. each fork-only call copies all settled source transcript context through
   `F` without invoking a provider;
3. repeated calls announce distinct, already-persisted target native ids and
   start an idle target transport before API success;
4. parent and child transcripts do not acquire the other's chat messages;
5. both processes can write their session state concurrently without database
   corruption, truncated JSONL, deadlock, or one process selecting the other's
   session;
6. shared/global runtime database changes do not mutate the logical transcript
   of `F`;
7. both processes receive their own model, effort, MCP configuration, and
   correctly scoped inference credential (residency or attempt); any MCP token
   remains attempt-scoped;
8. source and every child are natively restartable after a process/supervisor
   crash and after Docker stop/start;
9. concurrent file and Git conflicts surface as ordinary tool results rather
   than corrupting platform state or being attributed to the wrong chat.

Codex remains disabled for shared-native fork until one exact version is
selected and its app-server framing, event parser, native-id capture, resume,
fork, interrupt, and all existing Codex tests pass together. Production images
must not use a floating dependency. There is no TUI/PTTY or transcript-replay
fallback for any runtime.

## 5. Driver and process model

The current `HarnessSession.fork()` raises `ForkUnsupported`, and retained
turns are still launched by a runner-owned one-shot session. Both ownership
boundaries change. A resident driver supplies a supervisor-owned transport and
a fork operation distinct from a model turn:

```python
class ResidentTransport:
    session_id: str
    process_generation: int

    def send_turn(self, turn: TurnInput, *, activation_generation: int) -> HarnessTurn: ...
    def flush(self) -> None: ...
    def interrupt(self, *, activation_generation: int) -> None: ...
    def close(self) -> None: ...

class HarnessDriver:
    def open_resident(
        self,
        context: LaunchContext,
        *,
        session_id: str | None,
        process_generation: int,
    ) -> ResidentTransport: ...

    def fork_session(
        self,
        context: LaunchContext,
        *,
        source_session_id: str,
        source_transport: ResidentTransport | None,
        operation_id: str,
    ) -> ForkReceipt: ...
```

`fork_session(...)` must call the exact runtime-native fork-only path, return
and verify the already-persisted target native id, emit no Agent turn events,
and refuse before changing state if the pinned runtime lacks support. The
supervisor then calls `open_resident(..., session_id=B)` and proves the child
transport ready before the Fork API returns. The first user message is sent to
that existing transport; it is not part of the fork operation.

Capabilities become:

```text
fork = none | latest_immediate
resident_transport = none | supervisor_live
concurrent_shared_workspace = false | true
```

The latter two capabilities are enabled only by live multi-turn and
simultaneous-process conformance. They describe our implemented
driver/image/supervisor combination, not an upstream CLI's advertised feature.

Every ready membership in a running group normally owns one process generation
and resident transport, including between turns. An active turn adds a nested
activation generation; at most one activation may write one membership at a
time. Process handles, input queues, stdout/stderr framing, native-id capture,
interrupts, and credentials are keyed by
`(sandbox_id, thread_id, process_generation)`, while events and cancellation
are additionally fenced by `activation_generation`. They are never keyed by
sandbox id alone.

The runner remains the claimed job/attempt/lease executor, but it no longer
owns the Docker process. It sends one fenced turn to the supervisor, relays
only that turn's framed events, and settles the attempt. The supervisor keeps
the idle transport afterwards. If a runner disappears, lease expiry interrupts
only that activation; it does not discard a settled resident process or a
sibling transport.

The live process uses that membership's independently fenced, short-TTL,
renewable residency grant. Active jobs remain the authorities for message
admission, events, cancellation, and usage attribution. A model, permission,
or MCP configuration that the pinned live protocol cannot safely change causes
a deliberate member-only flush/restart with a re-keyed grant and native resume;
the turn then proceeds on the replacement transport. Under the current
attempt-scoped MCP contract, an MCP configuration change is such a restart
unless a separately reviewed per-turn local relay lands. This is a recovery or
configuration transition, not the normal message path.

A forked membership receives a separate residency/process generation and
grant. It never inherits or shares the source membership's provider or MCP
credential. Idle resident processes do not make provider calls and do not count
as chat activity.

Generated configuration and tmpfs paths must use a process-isolated execution
slot bound to the membership for the life of that process generation, not the
current fixed per-runtime path. Two resident transports cannot overwrite or
read one shared config file containing different credentials, model settings,
local-control passwords, or MCP tokens. Section 11 makes that a hard security
gate.

## 6. Durable ownership model

The current `agent_thread_sandboxes` row cannot express this topology. It uses
`thread_id` as its primary key and combines one container's lifecycle with one
chat's native locator and one active attempt.

Split it into two authorities.

### 6.1 Sandbox group

`agent_sandboxes` owns only shared Docker/workspace lifecycle:

```text
sandbox_id primary key
host, container_id, container_name
container_generation, stop_generation
runtime_id, runtime_version, image_digest, execution_profile
resident_slot_capacity
state: provisioning | running | stopping | stopped | stopped_dirty | lost
last_activity_at, projected_idle_deadline
workspace_epoch, active_mutation_count
publication_lineage_id, output_branch, published_base_sha
snapshot_transition_id nullable
last_error
```

It does **not** have one `active_attempt_id`, `admitted_job_id`, or
`native_session_id`.

### 6.2 Chat membership

`agent_thread_sandbox_memberships` owns independent conversation execution:

```text
thread_id primary key -> agent_threads
sandbox_id -> agent_sandboxes
native_session_id nullable
native_state: new | ready | fork_preparing | lost
resident_state: stopped | starting | live | restarting | lost
resident_slot_id nullable
residency_generation, process_generation, activation_generation
active_attempt_id nullable
admitted_job_id nullable
admitted_at nullable
last_settled_job_id nullable
forked_from_thread_id nullable
fork_anchor_job_id nullable
fork_operation_id nullable
last_error nullable
```

Each membership enforces at most one unresolved prompt for its native session.
Different memberships may simultaneously have active attempts. A resident
slot is generation-fenced and bound only while its process is alive; it is
cleared on verified close/container stop and may be reassigned on native
restart.

The existing `agent_thread_residencies` authority remains per thread/chat and
links through the membership to the shared sandbox group. Shared-fork v1 uses
it for the supervisor-owned idle process and its renewable membership grant.
Residency generation, renewal, release, takeover, process restart,
model/MCP-switch re-keying, and WARM degradation remain member-scoped; none may
overwrite, signal, or revoke a sibling's residency.

An immutable native-session-edge/audit row records every finalized
`F -> target_session_id` branch. Memberships and finalized audit edges supply
the reference graph needed by later session-state cleanup; an unfinalized
native target is an unreachable orphan eligible for verified cleanup.

Within one sandbox/runtime state root, a non-null native session id is owned by
exactly one membership. A partial unique constraint on
`(sandbox_id, native_session_id)` prevents two chats from being
accidentally finalized onto the same writable conversation.

### 6.3 Fork operations and publication snapshots

`agent_session_forks` is the durable cross-service saga authority. It stores
the client request id, hidden preallocated child ids, source membership and
generation, anchor job, runtime/version, sandbox/container generation, source
and target native ids, host receipt, state, and error. It has a unique
`(user_id, client_fork_request_id)` constraint and at most one PREPARING fork
per source membership. `materialized` means the host receipt is durable but
the child is still invisible; only `ready` owns a visible child membership.

`agent_sandbox_snapshots` is a separate ordered publication ledger. Each row
stores its sandbox/lineage, monotonically increasing workspace epoch, immutable
patch artifact, patch base commit, publication state/result, and the completed
jobs included through that epoch. Fork and turn state do not depend on a
publisher finishing. Section 9 defines the ordering and retry rules.

Existing retained chats migrate one-to-one into a group plus one membership
without changing their container id, Docker name, state, native session id, or
behavior. Existing Docker-cloned chats remain independent historical groups,
but no new Docker clone is created after cutover. Old clone saga rows remain
auditable and their code path is retired only after in-flight operations are
reconciled.

Backfill does not pretend an old container acquired binaries, Linux identities,
tmpfs slots, or permissions from a newer image. A group is shared-fork capable
only when its recorded immutable image/runtime profile passes the exact
`shared-sandbox-v1` probe. Incapable existing groups keep normal 1:1
stop/start/native-resume behavior and do not expose Fork. The platform does not
silently recreate or clone them. If production later requires upgrading such a
group in place, that is a separately reviewed state migration; it is not part
of this rollout.

## 7. Scheduler, supervisor, and broker

### Scheduling and admission

- Scheduler affinity resolves `thread -> membership -> sandbox -> host`.
- Residency affinity and grants stay keyed by thread/membership, while their
  container locator resolves through the shared sandbox group.
- A member with an active/admitted attempt cannot claim another turn.
- A different member in the same group may claim and execute concurrently.
- Host capacity has separate accounting for retained containers, resident CLI
  processes, and currently executing turns. A running ordinary group consumes
  one resident slot; each finalized fork consumes another while that group is
  running.
- Every live membership reserves a credential-isolated execution slot for its
  process generation, including while idle. Fork reserves the target slot
  before mutating native state. Slot exhaustion queues/refuses new group/fork
  admission with no half-created child; it is not implemented by a single
  sandbox-group turn mutex.
- Fork-only helpers reserve a bounded host maintenance slot and a group
  operation refcount but no inference grant. Host-wide exhaustion returns a
  stable retryable response; it never bypasses admission by spawning
  unbounded local servers.
- Turn settlement leaves the member's transport and slot HOT. The normal path
  does not evict it before the group timeout; host admission leaves enough
  resident capacity before creating the chat/fork. A crash, rolling supervisor
  restart, incompatible configuration, or explicit emergency pressure action
  may flush and demote only that member to WARM, then natively restart it. An
  active turn is never capacity-evicted.
- The container must be provisioned with CPU/memory appropriate for the
  configured concurrency. Resource pressure may queue/refuse work through
  ordinary admission control, but the semantic concurrency limit must be at
  least two and must not silently collapse to one group-wide turn.
- A runner process currently executes one claimed job synchronously. Therefore
  two simultaneous A/B turns still require at least two runner executors (the
  production Compose default already has three replicas) and two active-turn
  slots, or an equivalent in-process executor pool. Local Compose currently has
  one runner/slot and must be raised for the A/B concurrency test. Idle CLI
  transports live in the shared broker/supervisor rather than pinning runner
  replicas; runners are parallel turn clients of that lifecycle authority.
- Shared-group jobs carry a `shared-sandbox-v1` host requirement. A host may
  advertise it only when its broker, image, runtime pin, slot pool, and runner
  concurrency all pass the feature probes; an old or partially upgraded host
  cannot claim those jobs.

### Host supervisor

The supervisor is keyed by sandbox id and contains a member map rather than a
single lease:

```text
sandbox group G
  container lifecycle / idle accounting
  member A -> activation/process/session handle
  member B -> activation/process/session handle
  member N -> ...
```

It owns the bidirectional stdin/RPC/HTTP transport, framed output reader, local
control credential, and process handle for every live membership. Runners send
fenced turns to this supervisor instead of spawning Docker execs themselves.
It can keep several member processes idle, drive several turns concurrently,
interrupt one exact activation, and report all generations independently.

A supervisor restart reconstructs Docker groups from immutable labels and the
authoritative membership manifest returned by the control plane. Existing
Docker exec processes whose pipes/control sockets cannot be safely reattached
are generation-fenced, flushed when possible, and replaced by native resume;
this is a temporary member-specific HOT-to-WARM-to-HOT recovery, not context
loss. The next acquire carries the native locator and generations, while the
heartbeat response reconciles durable group commands. The host never invents
a second container because its in-memory member map was lost.

Each resident exec runs under a generation-labelled wrapper/process group with
an exact slot-owned pidfile, and the wrapper acts as a child subreaper.
Process-group kill alone is insufficient because untrusted tools can call
`setsid` or double-fork. Before a turn, the supervisor records the reviewed
idle resident process tree. At settlement/cancellation it uses the native
interrupt when available, then reaps every process created by that activation,
including reparented descendants, while preserving the verified resident
transport. A credential-free cleanup exec under that exact slot UID performs
the final census. A process cannot change UID under the required
`no-new-privileges`/dropped-capability profile.

If turn cleanup cannot recover a verified idle transport, the supervisor
flushes/kills that member's resident root and every process under its UID,
scrubs the private activation path, and natively restarts the same session in a
new process generation. The slot is never rebound to another membership until
a zero-process/zero-secret proof succeeds. Because simultaneously resident
siblings have different UIDs, none of these actions targets them.

Agent-launched background processes may run during the turn but are closed at
its activation boundary even though the Agent CLI itself stays live. A
persistent dev server belongs in the existing
credential-free terminal/command facility, where it has an independently
tracked lifecycle and still uses the shared workspace. This preserves the
feature without letting a tool process that inherited the resident credential
survive beyond its turn fence.

The current retained-process kill backstop that stops the whole container
cannot be the normal member-failure path once siblings may still be running.
If the UID cleanup cannot prove the slot empty, the safe last resort is a
generation-fenced group stop and dirty-state reconciliation; the platform must
not release a possibly credential-bearing slot merely to preserve sibling
processes.

The immutable Docker `sandbox_id` label becomes authoritative. The existing
thread label is treated as the legacy/origin member for migration; sibling ids
are mutable database membership and are not encoded into Docker labels.

### Workspace broker

Terminal, file, Git, and command routes authorize a thread membership and then
resolve to the shared sandbox backend. They no longer set a group-wide
`workspace_suspended` flag merely because one Agent turn is running or
finalizing. Existing user terminals and sibling agents can therefore continue
concurrently.

Every broker operation still participates in container activity accounting so
the container cannot stop while an exec/PTY is alive. Cancellation and process
reaping target exact member/terminal tokens, not every process in the group.

File and Git views resolve to the shared group. Terminals are member-owned
processes for event visibility and cancellation: chat A lists/cancels A's
terminals and chat B lists/cancels B's terminals, while both terminal sets run
in the same `/workspace` and refresh the same group activity deadline. A
terminal carries no inference credential and cannot read a sibling Agent
slot's private activation state. Every writer-capable shell/PTY still uses a
tracked noncredential execution identity/wrapper so detached descendants stay
owned by that terminal operation. Bounded file/Git commands are similarly
refcounted and reaped before their identity is reused. This is process
lifecycle accounting, not a lock between chats.

### Heartbeat

One group observation reports Docker identity/state plus every membership's
resident state, slot, residency/process generation, active activation if any,
group activity timestamp, monotonic idle remaining time, and active
mutation/refcount values. The control-plane response carries the authoritative
membership manifest and renews or rejects each residency generation
independently. Reconciliation must neither collapse concurrent attempts into
one owner nor create duplicate containers from sibling observations.
Control-plane stop commands fence the sandbox/container generation; a stale
host observation cannot stop a newer activation.

### Resource accounting and failure scope

Inference usage remains exact because every resident process has its own
membership grant and every request is attributed to the fenced active turn.
Turn timeout and cancellation first invoke the exact transport's native
interrupt for that activation; an unhealthy transport takes the member-only
process restart path.
The current Docker sampler, however, reports cumulative container counters and
cannot truthfully charge the same CPU/memory/I/O to two overlapping jobs. The
shared mode must either add process-tree sampling rooted at each member wrapper
or mark unavailable per-member counters with zero coverage; it must never
duplicate group totals onto every job. Container totals remain group metrics.

Docker memory limits and OOM termination are container-scoped. Without a
separately reviewed delegated-cgroup design, an OOM/container exit is therefore
a group failure that may mark every membership LOST; it cannot be promised as
a member-only failure. Ordinary CLI exit, timeout, cancellation, parser error,
or credential failure remains member-scoped. Admission sizes the container and
queues work before pressure rather than weakening seccomp/capability isolation
to delegate cgroups to untrusted processes.

## 8. Lifecycle and timeout

Inactivity belongs to the sandbox group because Docker stop affects the one
shared container. The group stores one `last_activity_at`, updated to the
latest meaningful interaction from any member chat: message/turn activity,
terminal or file/Git activity, fork, or another user-visible workspace
operation.

The rule is:

```text
stop_due_at = sandbox_group.last_activity_at + configured_chat_timeout
```

The live host owns a monotonic countdown so wall-clock changes cannot stop a
container early. The control plane stores the durable wall-clock projection
and receives `idle_remaining_seconds` in generation-fenced heartbeats, matching
the existing restartable-sandbox pattern. After broker/host restart, durable
control-plane state plus a fresh Docker observation re-establish the countdown;
neither side treats a missing in-memory timer as permission for an immediate
stop.

1. An interaction from A or B refreshes the same group timestamp.
2. A long-running admitted turn or command prevents stop even if the timestamp
   passes its deadline; the supervisor never stops a container underneath an
   active operation.
3. A merely resident but idle Agent CLI process is not activity by itself and
   does not keep the container alive forever.
4. At the deadline, after confirming there is no active turn/command or
   lifecycle transition, the supervisor cleanly closes every idle member CLI,
   flushes their native session locators, revokes/releases every resident grant
   and local-control credential, and issues one `docker stop`.
5. Any later operation through any member first `docker start`s that exact
   container. The control plane supplies the durable group membership manifest
   and the supervisor natively starts one resident transport for every ready
   member before the group is fully HOT again. The requesting operation waits
   only for its member to become ready; sibling restores proceed independently
   and one member failure does not suppress the others.
6. Concurrent activity is reference-counted only to prevent a mid-operation
   stop. It does not serialize A and B and is not a second user-visible
   timeout.

Container lifecycle is shared; chat execution status is not. While A is
running, B may be idle, running, failed, or complete independently.

Attempt context remains:

- `new`: this member has no prior settled native context;
- `live`: valid member context was used while Docker was already running;
- `restored`: Docker had to start before this member resumed/forked context;
- `lost`: this member's native context cannot be trusted.

A failed member process does not automatically mark siblings LOST. Missing or
corrupt Docker writable state marks every membership LOST. Legacy
HOT/WARM/COLD/REPLAY telemetry stays diagnostic: HOT requires a matching live
resident transport, WARM means Docker is running but that member is being
natively restarted, COLD means its Docker-owned context is stopped, and REPLAY
remains an explicit recovery method rather than normal continuity. Docker
RUNNING alone never implies member HOT, and product `live` still does not
promise one immutable PID across failures.

## 9. Git, patch capture, and publication

All siblings share one `.git` directory, so the group owns one output branch
and one PR/publication lineage. `branch_name_for(thread_id)` becomes
`branch_name_for(publication_lineage_id)`. No member-specific checkout is
performed when users switch chats.

Normal Agent turns are never serialized for patch attribution. Therefore:

- a turn's displayed final response belongs to that chat;
- a workspace diff observed when it settles is a view of shared state and may
  contain concurrent sibling changes;
- the platform must label it as a shared-workspace snapshot, not "changes made
  only by this chat";
- two automatic publishers cannot independently push different thread
  branches from the same working tree.

The existing per-job patch/publisher row is therefore not reused as the shared
mode's authority. A group publication coordinator owns ordered immutable
`agent_sandbox_snapshots`. The group's `published_base_sha` starts at the
prepared repository commit. Once snapshot N publishes, its commit becomes the
patch base for N+1. The existing trusted publisher, post-apply path/mode gate,
secret scan, workflow approval rule, short-lived source-control credential,
and no-sandbox-push boundary all remain in force.

A consistent automatic patch/PR still needs a **short quiescent snapshot
barrier**:

1. Agent responses settle and return to their chats without waiting for other
   running agents or for publication. A same-group follow-up is claimable from
   the retained native/workspace state immediately; it no longer waits for its
   parent's per-job patch to publish.
2. Settling a member records its group workspace epoch and marks publication
   pending. The coordinator waits until the active mutation count reaches zero
   and any previous snapshot has published, then CASes the group from
   QUIESCENT to SNAPSHOTTING. Active Agent turns, writer-capable terminals, and
   file/Git mutations count; read-only views do not. The host also compares the
   process census with its generation-labelled operation ledger. An unknown
   writer process keeps publication pending/dirty instead of being ignored as
   quiescent. Idle resident Agent CLI roots are explicitly non-mutating: they
   stay alive and do not delay the snapshot.
3. New mutating admission waits only for the bounded in-sandbox Git diff
   capture; a message arriving in that brief window remains queued on its
   membership rather than being sent to the idle CLI. No resident process is
   closed and no already-running turn is interrupted. The diff is generated
   inside the untrusted container against `published_base_sha`, with a
   disposable index exactly as today, then stored as one immutable snapshot
   through epoch N. Read-only broker traffic may continue.
4. The barrier releases immediately after artifact capture. Network fetch,
   patch validation, external checkout, commit, push, and PR update happen in
   the trusted publisher after release. All completed jobs included through
   epoch N reference that same snapshot/result.
5. After publication, the trusted base-commit bundle path imports the new
   commit object into the retained repository without checking it out or
   changing the shared worktree/index. The next snapshot can then diff current
   workspace state against that exact published commit. Snapshots publish
   strictly in epoch order and never force-push over an externally moved
   branch.
6. If quiescence never occurs, capture remains pending. If validation, branch
   movement, or publication fails, the immutable snapshot is retained for
   explicit retry/release and later snapshots stay ordered behind it. The
   Agent turns remain settled; a publication failure is not relabelled as an
   Agent-turn failure.

This is not a turn mutex: A and B remain concurrently interactive. It is the
minimum consistency boundary required by the existing patch/PR feature. If
the product requires literally zero group-wide barriers, correct automatic
patch capture and publication must be disabled in this mode and replaced by a
manual user-triggered snapshot.

## 10. API and frontend contract

After rollout, retained `POST /jobs/{job_id}/fork` means shared native fork.
The frontend sends a client-generated `Idempotency-Key` and reuses it across
transport retries. Before the route returns success, it invokes fork-only and
creates a ready new chat/thread and membership, but no Docker container/image,
volume, branch, or model call. If the existing container was stopped, the
action starts it so the Docker-owned native state is available. The
Docker-clone mode and UI action are removed.

The response exposes at least:

```json
{
  "thread_id": "new-child",
  "sandbox_id": "same-as-parent",
  "workspace_shared": true,
  "fork_anchor_job_id": "source-latest-settled",
  "native_state": "ready",
  "resident_state": "live"
}
```

The runtime-native id and state locator remain internal control-plane/host
data. The public response proves readiness without exposing an implementation
identifier. Retrying a committed request returns the same child and success;
it never creates a second chat.

Frontend behavior:

- A and B can both send messages and show Running simultaneously.
- When the group is running, the normal message path sends directly to that
  chat's existing resident CLI. `resident_state` may briefly show Starting or
  Restarting after Docker/supervisor/process recovery; it is not inferred from
  container state alone.
- A running does not place a sandbox-busy banner or disabled composer on B.
- Each chat streams only its own Agent CLI events and owns its own cancel
  action.
- Container starting/stopped/lost state and idle timing are shared.
- File explorer, diff, branch, and PR are explicitly labelled shared workspace
  state. Terminal processes are listed/cancelled under their owning chat, but
  the UI states that their filesystem is shared.
- The Fork action warns that concurrent edits can overwrite or conflict and
  that it does not create an independent branch/container.
- While the short source-only fork operation is running, that source's Fork
  action/composer shows Forking; siblings remain interactive. Success means the
  child native session is durable, its resident CLI is live, and its composer
  can send a normal first message immediately without launching/forking again.
- Only the latest settled turn exposes the retained native Fork action. A
  request naming an older settled turn receives a stable capability error and
  creates no chat, membership, process, or provider request; it must never
  silently fork the latest context instead.
- A shared snapshot waiting for quiescence is shown as publication pending,
  not as a failed Agent turn.
- Non-forked retained chats and legacy disposable jobs keep their existing
  behavior.
- Fork capability is returned by the API from the exact runtime/image and host
  capability gate. The UI does not infer support from a runtime name or expose
  the action while the responsible host is on an old protocol.

## 11. Security and isolation invariants

This section is not additional session topology. Fork/session continuity only
needs the group/member/session-id mapping above. `/run/agent-activation` is the
already existing ephemeral secret/config area, not shared conversation state,
not persistent storage, and not a host mount. The extra isolation below exists
only because the current platform deliberately gives simultaneous CLI
processes separately fenced credentials.

### 11.1 Current implementation is not safe for concurrent grants

Today the retained container and every `docker exec` run as the same numeric
user/group (`10001:10001`). The container has one `0700`
`/run/agent-activation` tmpfs, and the Pi/OpenCode/Kilo adapters use fixed
subdirectories below it. That is safe only because the supervisor currently
admits one Agent CLI activation at a time.

If two current execs run concurrently, they can overwrite the fixed provider
configuration, and same-UID untrusted code can inspect another process's
environment/config or signal it. That exposes the sibling process's attempt or
residency grant and breaks usage attribution, revocation, cancellation, and
event ownership. The new topology must not be enabled on that implementation.

### 11.2 Required member-execution isolation

The first implementation uses a bounded pool of resident execution slots
prepared in the sandbox image/container. Each live member receives a
different numeric Linux UID and private credential tmpfs/config directory,
while all slots share a non-privileged workspace group:

```text
slot A: uid 11001, shared gid 10001, private activation path A
slot B: uid 11002, shared gid 10001, private activation path B
workspace/native store: setgid shared gid 10001, no credential files
fork helper: dedicated uid, shared gid 10001, private durable receipt directory
```

The image predefines slot identities/HOMEs, and container creation supplies
per-slot tmpfs paths with the matching owner. Runtime code never needs root or
`CAP_CHOWN`. The supervisor launches the exact member exec with
`docker exec --user <slot_uid>:10001`, sets a slot-specific HOME/config path,
and keeps that slot bound through idle turns. It clears/revokes the slot only
after the resident process generation is closed and its process/secret census
passes.

Different UIDs plus dropped capabilities must be proven to deny
`/proc/<sibling>/environ`, ptrace, and signals across slots. The test is the
contract; there is no fallback to the shared `10001` UID if the host/runtime
cannot enforce it. The pool has at least two Agent slots. Shared-fork v1 keeps
one slot per ready membership while the container is running; a settled turn
keeps the resident root but reaps turn descendants. Active processes are never
evicted for another member. Exhaustion is ordinary chat/fork admission and
cannot silently demote the design to one shared UID.

To keep the requested one shared workspace, checkout/setup applies a reviewed
setgid/shared-group mode, umask, Git `safe.directory`/shared-repository
configuration, and native-store permissions. All five CLIs must prove that a
different slot can resume the membership id selected by the platform and
concurrently use the shared SQLite/JSONL store without corruption. A wrapper
uses group-preserving umask/modes and normalizes only credential-free native
state at reviewed flush/close boundaries so a restarted slot can resume it.
Exact tests cover upstream files that default to `0600` as well as SQLite
DB/WAL/SHM creation while several resident services remain open.

The runtime's native store is deliberately same-owner shared state. The
platform does not claim that Unix permissions prevent a malicious sibling chat
from reading or modifying another transcript inside a shared upstream
database; neither the upstream SQLite schemas nor JSONL stores provide such an
ACL. Normal drivers are fenced to their membership's native id and the
conformance suite prevents accidental cross-selection. A stronger hostile
transcript boundary would require independent state namespaces or containers
and is a different product topology. Credentials and process control remain
hard boundaries.

The fork helper is a predeclared, unprivileged image identity with no provider
grant. It may read the credential-free shared native store and write only its
platform receipt directory plus runtime-created target state. Its directory is
Docker-owned and inaccessible to Agent slots; it is not `/run` tmpfs, a host
mount, or a new persistence service. OpenCode/Kilo resident servers
additionally use the per-process loopback authentication described in section
4; a recovery-only helper uses a separate operation credential.

Files that an agent deliberately chmods, deletes, or overwrites remain
ordinary shared-workspace/native-state interference inside the same authorized
user domain, not a credential-isolation escape. Such interference may make an
affected membership LOST but never grants access to a sibling activation
secret.

Unprivileged nested user/PID namespaces are not the baseline fallback: the
current Docker profile rejects `unshare`, and relaxing seccomp/capabilities to
make it work would widen the sandbox security boundary. A future equivalently
strong mechanism needs separate security review and the same conformance
tests.

A local Docker proof with the exact sandbox image and its intended
`cap-drop=ALL`/`no-new-privileges` profile successfully ran UID `11001` and
`11002` concurrently with separately owned tmpfs paths. The second UID was
denied access to the first process's `/proc/.../environ`, signals, and private
tmpfs while retaining write access to its own slot. This proves the Docker
primitive, not the full feature: every CLI, shared native store, workspace
permission, slot-reuse, Kata/gVisor, and adversarial test above remains a
rollout gate.

### 11.3 Invariants after isolation lands

- Forked memberships belong to the same authorized user/security domain and
  inherit repository, runtime, image, execution profile, permission policy,
  and MCP allowlist.
- Every resident process receives exactly one member-scoped, short-TTL
  residency grant through its own environment/config path; heartbeat renewal
  and revocation are fenced by that membership/process generation. Active
  attempts still delimit prompt admission, events, cancellation, and usage. An
  attempt-scoped MCP token or incompatible configuration triggers the
  documented member-only flush/restart unless a reviewed per-turn relay can
  rotate it without exposing it to the idle CLI.
- Residency renewal stops at the shared group deadline or on lost supervisor
  ownership. Docker stop/restart always creates new process generations and
  fresh grants; no credential is recovered from the writable layer.
- Sibling memberships never share a residency grant, attempt grant, MCP token,
  activation directory, process generation, or renewal/revocation fence.
- Concurrent processes cannot read a sibling process's credential from argv,
  disk, shared generated config, `/proc`, or a reusable environment file.
- No long-lived or cross-task credential is written into the Docker layer or
  native session state.
- Docker continues to own `/workspace` and Agent CLI state; no host bind mount
  or per-chat host directory is introduced.
- Setup-phase and agent-phase egress boundaries remain unchanged for every
  concurrent exec.
- A member may see sibling workspace/native project-global state by product
  design, but it must not receive another member's event stream, grant,
  cancellation authority, or control-plane authorization.

## 12. Current-code impact matrix

| Component today | Required concurrent-shared change |
|---|---|
| `agent_thread_sandboxes` | Split container group from per-chat session/attempt memberships |
| Claim/store fencing | Replace one group `active_attempt_id` with one independently fenced attempt per membership |
| Thread residency/grants | Keep one independently fenced live-process lease and grant per membership; resolve all of them to the shared sandbox |
| `ChatSessionSupervisor` | Replace one thread lease/process with a member map that owns simultaneous long-lived transports, input queues, output framing, and process recovery |
| Runner | Stop spawning/owning the retained CLI; deliver one fenced turn to the supervisor without suspending the shared workspace |
| Sandbox process identity | Replace same-UID/fixed activation config with credential-isolated UID/tmpfs execution slots and shared-group workspace permissions |
| Workspace broker | Resolve thread authorization to the same backend without a group turn lock; keep activity/refcount tracking |
| Heartbeat/reconciliation | Report one container plus every resident/turn generation and reconcile the authoritative membership manifest |
| Idle stop | Require zero active turns/commands, then flush/close idle resident CLIs before stopping the shared container |
| Driver | Add exact-version supervisor-live transport, native interrupt/resume, native fork-only receipts, and concurrent shared-state gates |
| Retained fork route | Remove Docker commit/create/fingerprint clone flow and synchronously materialize the member-only native fork before returning success |
| Fork recovery | Add client idempotency, PREPARING/MATERIALIZED/READY saga states, and a credential-free in-container receipt journal |
| Migrations | Preserve old clone audit rows, retire new clone creation, add group/membership/native-edge/fork-operation records |
| Patch/publisher | Replace per-job authority with ordered group snapshots, one lineage branch, imported published bases, and a bounded quiescent capture coordinator |
| Frontend | Allow simultaneous chat Running states and display shared/racing workspace semantics |
| Runner topology | Keep idle transports in the supervisor and require two or more turn executors/slots for simultaneous A/B turns |
| Resource accounting | Count resident processes separately from active turns; add truthful process-tree coverage or omit unavailable per-member counters |

No change needs host filesystem mapping. The existing Docker writable layer is
already the shared workspace/state location.

## 13. Acceptance and regression gates

The feature is not complete until tests prove:

1. A normal running chat gets exactly one container, one membership, and one
   supervisor-owned live CLI process; two sequential compatible turns reuse
   that process on the normal path.
2. Fork materializes and verifies a distinct native child id before returning,
   starts and probes the child's idle resident CLI, then adds one ready
   membership/chat; it creates no Docker container, image, volume, mount, host
   directory, provider request, or fake model prompt.
3. Parent and child have two distinct idle Agent CLI roots in the one container
   immediately after Fork, and can both start turns at the same time.
4. Neither member receives sandbox-busy merely because the sibling is active.
5. Each member admits at most one unresolved message and streams/interrupts
   only its own activation while leaving a healthy resident transport alive.
6. The parent remains on its source native id, the child is already on its
   distinct forked id, and parent-first, child-first, and simultaneous next
   messages all go directly to the already-live transports without another
   launch or fork.
7. Repeated forks support N memberships and at least two concurrent turns.
8. A retained fork request for an older settled turn fails without side
   effects instead of silently using the latest native context.
9. The exact Claude CLI + official Agent SDK, selected exact Codex app-server,
   Pi RPC, OpenCode serve, and Kilo serve images pass live multi-turn,
   prompt-free fork-only, simultaneous transport, and native-restart tests with
   real provider traffic for the turns.
10. Concurrent slots cannot read each other's environment/config, ptrace or
   signal each other, overwrite credential files, or spend against the wrong
   attempt; slot reuse removes stale secrets.
11. Shared UID fallback and a seccomp/capability relaxation both fail the
    rollout gate.
12. Two agents editing the same file produce real shared-workspace outcomes
    without event/session misrouting; conflicts are visible rather than hidden.
13. Concurrent Git contention is surfaced as ordinary tool errors and does not
    corrupt the control-plane ledger.
14. Model, effort, MCP configuration, renewable HOT residency grants, active
    attempt generations, usage, and cancellation remain correctly attributed
    per member under concurrency; restarting/re-keying A cannot revoke B.
15. An ordinary member process/restart failure does not kill or relabel
    siblings. Only the documented uncontainable credential-cleanup fallback or
    a container failure may stop the group; actual container-state loss still
    marks the whole group LOST.
16. Group `last_activity_at` is the maximum activity across all members; idle
    stop never occurs while any member operation is active, and an operation
    through either member restarts the same container and reconstructs live
    transports for every ready native session.
17. Publication waits for a bounded quiescent snapshot, coalesces one shared
    workspace epoch, keeps idle CLI roots alive, and never blocks an
    already-running Agent turn.
18. Timeout/cancel targets the exact member. Per-member resource counters come
    from its process tree or are explicitly unavailable; container totals are
    not duplicated. A container OOM/exit is tested as a group failure.
19. Existing non-forked retained, follow-up, terminal, file/Git, MCP,
    reasoning-effort, cancellation, publication, legacy disposable, auth,
    egress, and frontend tests remain green.
20. The selected exact Codex pin passes all previous model/event/patch tests
    plus app-server initialize/start/turn/interrupt/resume/fork and process
    recovery tests before its capability is enabled; no floating pin is used.
21. Real Docker inspection proves there are no chat-state mounts or persisted
    activation credentials.
22. Killing the host/control-plane call before and after native target creation
    never mutates the source, never exposes a half-created child, and leaves at
    most an unreachable native orphan that reconciliation can identify safely.
23. Repeating one client fork request before materialization, after host
    receipt, after database commit, and after a lost HTTP response always
    returns at most the same one child; a different request id creates a new
    child.
24. OpenCode/Kilo resident services reject unauthenticated sibling requests;
    their random process-generation control credential is absent from sibling
    `/proc`, tmpfs, native state, Docker layer, logs, and host files.
25. A broker restart reaps or fences orphan member wrappers, preserves every
    settled native locator, natively rebuilds each live transport from WARM,
    and returns it to HOT without cloning a container or treating a lost
    in-memory timer as an immediate timeout.
26. An additive migration backfills every existing retained chat into one
    group/member pair, historical Docker clones remain independent groups, and
    old hosts cannot claim `shared-sandbox-v1` work during rolling rollout.
27. A turn that forks, double-forks, calls `setsid`, and ignores TERM has all
    activation descendants reaped while its reviewed idle CLI root survives;
    full member restart clears the entire UID without signalling a sibling.
    The slot cannot be rebound until the zero-process proof succeeds; cleanup
    ambiguity takes the documented group-stop fallback.
28. Writer-capable terminals/commands retain ownership of their detached
    descendants, and the snapshot coordinator refuses quiescence when the
    Docker process census contains an untracked writer process.
29. A backfilled container with the old image keeps 1:1 resume behavior and
    exposes no shared Fork capability; the rollout never assumes a host update
    changed binaries, users, or tmpfs inside an existing container.
30. Two simultaneous Fork actions in one group are ordered by the helper-only
    mutex and produce independent receipts, while an already-running sibling
    Agent turn continues uninterrupted.
31. An idle resident CLI neither refreshes `last_activity_at` nor blocks the
    timeout: the supervisor flushes/closes it at the deadline, `docker stop`
    removes its PID, and `docker start` recreates it by native resume.
32. A runner replica can finish/disappear after a turn while the supervisor
    retains the member process; a later turn claimed by another runner uses
    the same transport and receives only its own framed events.
33. An incompatible model/MCP/permission change restarts only the affected
    member and resumes the same native session before admitting the turn.

Required validation layers are unit, server, database, migration, real Docker,
multi-process stress, crash/reconciliation, real-provider conformance, and
frontend end-to-end tests. A sequential fake driver is not sufficient evidence
for this design.

## 14. Rollout plan after approval

1. Record the approved product behavior: supervisor-owned live CLI per member,
   local-two-CLIs workspace races, one shared branch/PR, shared ambient project
   state, and the bounded publication snapshot barrier.
2. Select and pin one exact Codex app-server version (the current `0.145.0`
   already exposes the required schema, while an upgrade remains allowed) and
   one exact Claude Agent SDK version compatible with CLI `2.1.220`. First
   prove no regressions in every existing model/effort/MCP/event/patch test.
3. Add persistent multi-turn, native interrupt/restart, immediate fork-only,
   and simultaneous-transport conformance for all five exact runtime images,
   including authenticated resident loopback service probes.
4. Land credential-isolated execution slots, shared-workspace/native-store
   permissions, the credential-free fork helper/journal, and adversarial
   `/proc`/config/signal plus detached-child UID-reaping tests before allowing
   a second simultaneous grant into any container.
5. Add group/membership/native-edge/fork-operation/snapshot tables in an
   additive migration. Dual-read/backfill every current retained row 1:1
   without changing its container, keep old clone rows readable, and do not
   drop the old table during the rolling deployment.
6. Move retained process ownership from runner to supervisor, then make
   scheduler, runner turn delivery, activation config, broker, heartbeat,
   interruption, timeout, recovery, and resource reporting member-concurrent.
   Upgrade local/prod resident and turn-executor capacity and advertise
   `shared-sandbox-v1` only after all pieces are present.
7. Land shared workspace epochs and the bounded quiescent publication
   coordinator, then switch shared retained follow-ups away from the old
   per-job publish wait.
8. Replace retained Docker-clone fork with the member-only native fork saga;
   stop admitting old clone operations, reconcile every in-flight clone record,
   and only then remove new clone creation. Historical cloned containers remain
   ordinary independent groups.
9. Update API/frontend semantics, add the idempotency contract, and run the
   entire regression matrix. Enable the feature flag per exact runtime only on
   capable hosts; retain an operational rollback that disables new shared
   forks without invalidating already-created memberships.

Existing containers whose immutable image predates the slot/helper/runtime
changes remain capability-off after backfill. New groups use the new pinned
image. This is expected during rolling rollout and is surfaced as unavailable,
not as an attempted Docker clone or an eventual runtime failure.

## 15. Feasibility conclusion

The requested effect is implementable: Docker can keep multiple Agent CLI
processes alive in one container, the exact five runtimes expose persistent
stdin/RPC/app-server/HTTP transports, and the existing Docker-owned writable
layer is already one shared workspace. Ordinary non-fork chats can remain 1:1.
The current control-plane fork saga, host-affinity claim, fencing generations,
Docker broker, retained stop/start path, runner replicas, residency ledger,
capability matching, trusted patch publisher, and frontend immediate Fork
request are reusable building blocks. The material ownership change is moving
the resident transport from a runner-local handle into the supervisor member
map.

It is not implementable as "literally no coordination anywhere." Per-session
prompt fencing, process/cancellation identity, Docker stop refcounts, and a
short quiescent patch/publication snapshot are necessary for correctness. None
of those serializes normal turns across different chats; A and B can execute
and edit the workspace simultaneously.

The main engineering gates are:

- pin the official Claude Agent SDK fork helper alongside CLI `2.1.220`, and
  select one exact reviewed Codex app-server pin without model/event
  regressions;
- implement and stress the supervisor-owned live transport for Claude
  stream-json, Codex app-server, Pi RPC, OpenCode serve, and Kilo serve;
- replace the current same-UID/fixed-config execution path with proven
  credential-isolated member slots without weakening container security;
- prove every runtime's local session store tolerates immediate fork-only plus
  simultaneous live processes, and that a forked child's resident transport is
  ready before any new user message;
- move active-attempt/process state from the group row to memberships;
- remove global terminal/workspace suspension from normal Agent turns;
- add enough runner/host-slot concurrency and truthful process-vs-container
  resource reporting;
- make fork retries durable/idempotent and authenticate in-container resident
  loopback services;
- redefine patches and publication as ordered shared workspace snapshots;
- expose inherent file/Git/project-memory races honestly in the UI.

These are additive implementation changes with explicit rolling-rollout and
fallback gates, not missing platform primitives. Subject to the exact-runtime
conformance tests, there is no fundamental blocker in the current
infrastructure and the implementation can begin from this specification.
