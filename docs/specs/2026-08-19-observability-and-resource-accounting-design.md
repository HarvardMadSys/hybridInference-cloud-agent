# Observability and per-tool resource accounting

- **Status:** implemented per-chat subset; the shared-retained amendment in
  §0 is authoritative for the current topology
- **Scope cut:** per-chat timing and Kata process observations ship first. The
  admin layer, aggregate API, alert checker, export seam, egress byte ledger,
  inference-cost join, and portable persistence telemetry are **deferred**.
- **Date:** 2026-08-19
- **Companion:** `2026-08-17-session-resident-harness-threads-design.md`
  (historical continuity ladder),
  `2026-08-28-one-chat-one-restartable-sandbox-design.md`
  (RUNNING/STOPPED lifecycle and host-level Session Supervisor ownership);
  `2026-08-31-native-session-fork-shared-workspace-design.md`
  (implemented member-concurrent live processes and shared group accounting);
  issue #57 (residency observability read path)
- **Model:** the DeepSeek harness session-telemetry seam
  (`docs/subsystems/session-telemetry.md`,
  `packages/session/session-telemetry*`, and the Agent Notes
  `2026-07-23-session-telemetry-otel-revival`,
  `2026-08-05-feedback-gated-session-telemetry`,
  `2026-08-10-telemetry-default-off` in that repository). §2 states exactly
  what is copied and what is deliberately not.

---

## 0. Shared-retained implementation amendment (2026-09-03)

The executable topology is defined by
`2026-08-31-native-session-fork-shared-workspace-design.md`: one retained
container per sandbox group, one live Agent CLI session per member, and
concurrent fork siblings in the same workspace. That changes the accounting
unit. Docker statistics describe the **whole group container** and cannot be
assigned to either concurrent chat turn without double counting. Therefore the
current turn path never binds the whole-container `ResourceSampler` to a member
attempt, including while a group happens to have only one member.

Every current attempt instead stores this honest shape:

- aggregate CPU, memory, I/O, and network counters are null;
- `sample_count = 0`, `coverage = 0`, and
  `sampler_version = shared-member-v1` mean “group totals intentionally not
  attributed”, not “the turn used zero resources”;
- the sandbox manager records bounded message-level tool windows for all normalized
  `tool_use` / `tool_result` pairs;
- Claude Code can narrow those windows to `exact` with the passive, pinned
  marker helper;
- under Kata, a guest-root eBPF observer records fork/exec/exit for the addressed
  member UID. Exit-time CPU/RSS/I/O may be charged to a tool only when one
  complete, non-overlapping window owns that process;
- under trusted non-Kata Docker, the exact marker file may improve timing but
  process accounting remains unavailable. It is not fabricated from the group
  container.

The sandbox manager owns the observation cursor and tool-window tracker. It
closes observation before the member reaper and permission normalizer run, so
platform cleanup is never billed to the user call. The sandbox manager includes resource
telemetry in both result and boundary-error frames; after cancellation or turn
timeout, the runner interrupts the member and drains the terminal frame before
reporting the attempt. A disconnected or lease-lost runner still aborts and
cleans the member, but may only persist the explicit zero-coverage fallback if
the terminal frame could not cross the wire.

`ResourceSampler` and its daemon-backed tests remain as a lower-level
whole-container/group primitive and for reading historical rows. It is not a
current per-member accounting source. Any later group/residency dashboard must
keep those totals group-scoped.

Under the implemented shared-live group lifecycle, the first turn records
`new` because there is no prior session to continue.
`continuity_mode = hot` may still record that the existing Agent CLI process
handled a follow-up, and `warm_native` may record a replacement process, but
these are transport diagnostics rather than product correctness. The required
facts are `context_status = new | live | restored | lost`, the sandbox-qualified
session locator, and the member's `last_settled_job_id`. A process replacement
after model/effort change or `docker stop` / `docker start` is successful only
after the driver proves it loaded the retained native context. RUNNING-idle
and STOPPED are residency states. A missing native locator or lost container
must be recorded as `LOST`; replay remains historical telemetry and is never a
recovery path.

Checkpoint export metrics and alerts below belong to the explicitly deferred
persistent-storage phase. The remaining sections preserve the motivation and
roadmap; where older sampler language conflicts with this amendment, this
section and the 2026-08-31 topology are the implementation contract.

## 1. Problem

The platform is nearly blind. Inventory of everything that observes an agent
run today:

| Surface | What it holds | What it lacks |
|---|---|---|
| `agent_job_events` | Normalized + raw driver events per attempt, idempotent on `(attempt_id, client_seq)` | Written for UI replay; no aggregation, no export, no alerting; payloads unindexed |
| `agent_attempts` columns | `continuity_mode`, runtime/image version stamps, lease state | Facts exist per row; §9's promised aggregates (HOT hit-rate, export-failure rate, degradation reasons) were never built |
| Gateway `api_logs` | Tokens/cost per inference call, attempt- or thread-attributed (billing authority) | Lives in the gateway; no joined read path on our side (#57) |
| Squid egress proxy | **Already logs one line per CONNECT** — `ts`, `src`, allow/deny verdict, target host, `bytes=%<st` — to stdout | Nobody collects it; denied CONNECTs (a security signal) fall on the floor |
| `docker stats`, `journalctl`, ssh | Ad-hoc | Not recorded; runner prints are block-buffered and unsearchable (a real incident-response cost, already bitten during staging acceptance) |

There are zero metrics: no counter, no histogram, no dashboard, no alert on
the agent side. Consequences, concretely:

- Residency knobs ship blind. The idle deadline, the per-host RUNNING/STOPPED
  residency budget, and the #61 replica-dilution question are tuned by guess;
  the design's own open questions 4–5 ask for data nobody records.
- Incidents are diagnosed by ssh archaeology. "Is the claim loop alive",
  "why is this member LOST", "which turn OOM-killed the host"
  all require shelling into two machines.
- Resource abuse is invisible. A tool call that pins 32 cores for ten
  minutes, or exfiltrates via an allowlisted domain, costs and risks real
  money and shows up nowhere.
- The ops-paper dataset (with Juncheng, EOY 2026) has no resource dimension
  and no continuity-outcome time series.

## 2. What we copy from the DeepSeek harness — and what we don't

Their telemetry subsystem was read in full, not skimmed; the principles below
each cite where they are pinned.

**Adopted:**

- **A1 — Projection, not a second truth.** Telemetry records are projections
  of the canonical stream; the canonical store is never rewritten, and the
  export may lose or duplicate records because consumers dedupe on the
  identity that rides along (`session-telemetry.md` §"The logical record").
  Our canonical stream per attempt is `agent_job_events` with
  `(attempt_id, client_seq)` — the dedupe key already exists.
- **A2 — Two channels: `ledger` and `ops`.** Ledger records mirror canonical
  events one-to-one and are summable after dedupe; ops records are signals
  with no log home (errors, lifecycle transitions), deliberately carry no
  sequence identity, and are alerted on, never summed.
- **A3 — Severity pre-mapped at capture** from the event's *own* outcome
  flags (`turn_failed`, a tool result's error flag), so a receiver alerts
  with zero configuration.
- **A4 — The boundary axiom.** Our capture ends at `emit()`. Batching,
  retry, queueing, and loss policy belong to the OpenTelemetry SDK and
  collector, configured — in their words — verbatim. We never hand-roll a
  delivery pipeline (their revival note records why: every self-built
  alternative was rejected).
- **A5 — A redaction stage between projection and export**, fail-closed per
  record, applying to the exported copy only. (Default flipped for us — see
  "not copied".)
- **A6 — Attribute minimalism.** Identity attributes only; the body carries
  the payload; nothing recoverable from the body is duplicated into
  attributes.
- **A7 — Stream-start projection.** They ship only the first
  `assistant/chunk` per (turn, step); we ship only the first `output_delta`
  per (attempt, turn) plus the terminal event. Sequence gaps on the wire are
  routine and never a loss signal.

**Not copied:**

- **Their consent surface.** DS telemetry is a *client-side* product
  exporting a user's own sessions to a third party, hence default-off,
  feedback-gated, anonymous-id machinery. We are the deployment observing
  our own hosted infrastructure — the events already land in our Postgres.
  What transfers is not consent but *export hygiene*: their waterfall ships
  no rules and exports full bodies by default; **ours defaults to
  deny-body** (O9), because our export sink is shared operational tooling,
  not the user's own chosen backend.
- **The Cordis seam machinery.** One in-process service registry does not
  map onto our three processes (control plane, runner, sandbox). The seam
  reduces to: capture points in CP and runner, each ending at an `emit()`
  whose default backend is a no-op (O1).
- **Per-chunk ledger mirroring** — see A7; we already batch deltas.

## 3. Goal

> Long-term target: every attempt tells its whole story in queryable form: **what happened**
> (ledger), **what it consumed** (CPU, memory, io, network — per attempt,
> per residency, and per tool call), and **what the platform did around it**
> (ops). Postgres keeps every fact; our own console renders the operational
> view; a Slack-webhook checker watches it. A turn that we failed to
> measure says so instead of reporting zeros.

Non-goals (v1): distributed tracing (correlation ids suffice; spans are an
OBS-4 candidate), log-aggregation overhaul, harness-native OTel passthrough
(claude-code can export its own metrics; OBS-4 candidate, verify against the
pinned CLI), any third-party dashboard or metrics stack (no Grafana, no
Prometheus, no collector — O1), and any attribution precision beyond the
honest window model of §6.

## 4. Decision record

- **O1 — Deferred admin/export direction: Postgres is the only deployed backend; export is a seam, not a
  stack.** We deploy no Grafana, Prometheus, or collector — the gateway
  already removed Prometheus once, and this deployment gets no second
  metrics estate to operate. Every fact lives in Postgres; the admin
  surface is our own console reading a CP aggregate API; alerting is a
  CP-side checker posting to a Slack webhook. The DS boundary axiom is kept
  as a *seam*: capture ends at `emit()`, and the default emit backend is a
  no-op. Attaching an OTLP exporter later (if ad-hoc exploration ever
  justifies a standard stack) is configuration, not surgery (OBS-4).
- **O2 — Three signal classes.** DS's `ledger` and `ops`, plus one they
  don't have: **resources**. Resource facts can come from periodic
  whole-container samples or event-driven guest process accounting. They are
  measurements, not ledger events.
- **O3 — The measurement unit must match the ownership unit.** Docker stats
  are sandbox-group totals. The daemon-backed 1 Hz sampler is therefore never
  attached to one current shared member attempt. Current member rows carry the
  explicit `shared-member-v1` unavailable aggregate described in §0. Kata adds
  an independent, UID-filtered guest-kernel lifecycle census; trusted non-Kata
  mode degrades without process totals. A future residency view may sample the
  whole container only if it stores and renders the result as group-scoped.
- **O4 — Per-tool attribution is windowed, and honest about it.** Tool
  windows come from the normalized driver event stream. Claude Code upgrades a
  pair to `exact` only when its passive PreToolUse/PostToolUse marks are both
  valid and fall inside the message bracket; otherwise it, Codex, Pi,
  OpenCode, and Kilo remain `message`-level wherever they emit paired tool
  events. A Kata process is charged only when its start belongs to one window
  and its accounting is settled. Overlap, a still-running process, truncation,
  or journal loss makes the affected result partial and never invents an
  exclusive number.
- **O5 — Deferred: network is classified at choke points we own.** Inference
  billing remains in gateway `api_logs`, MCP uses an attempt-fenced relay
  token, and Squid logs setup egress. The proposed read path would parse flows
  and expose a residual without assigning a shared container's veth total to a
  member. No joined byte ledger, denied counter, or network panel ships in the
  current implementation.
- **O6 — Deferred: running-idle and stopped residency are separate first-class series.**
  Docker writable-layer bytes (sampled through Engine inspect with size at
  establish, stop/start, and daily),
  running-idle sandbox memory, stopped duration, and each state's share of
  host disk/container budgets price the idle deadline and new-chat admission.
  STOPPED consumes no workload CPU, memory, or pids but retains disk and the
  only local copy of workspace/native state in this phase.
- **O7 — Deferred metrics cardinality budget.** Metric labels would be bounded enums only: host,
  runtime, continuity mode, floor event kind, egress verdict, tool *name*
  (bounded per runtime registry). `thread_id`/`attempt_id`/domain names
  never label a metric; they live in Postgres rows (and later exemplars).
  A future lint-style test must enforce the label allowlist.
- **O8 — Failure honesty.** Measurement failure never fails a turn. Every
  attempt telemetry row carries `coverage` and `sampler_version`; current
  shared members use the explicit unavailable semantic in §0, while a
  sampler-backed historical/group row uses coverage for its observed span. A gap is recorded as a gap. This
  is DS's "seq gaps are routine, never a loss signal" applied to
  measurement.
- **O9 — Deferred export redaction defaults to deny-body.** The ledger projection
  exports event type, severity, sizes, ids, and a bounded allowlist of
  scalar fields; tool arguments/results and prompt text never leave Postgres
  via telemetry. The redaction stage is the DS waterfall shape — orderable
  rules, fail-closed per record, exported copy only — with the default rule
  set present rather than empty.
- **O10 — Deferred retention target.** Squid raw log 7 d after parsing; Postgres rollups
  and parsed flow rows indefinite (paper dataset); raw 1 Hz samples are a
  runner-side ring buffer only — rollups are computed at window close and
  the samples discarded. One retention tier, no second estate (O1).

### Historical O3 amendment — daemon, not cgroupfs (2026-08-19)

Recorded per this project's rule that a divergence is a decision and a
decision gets written down. This still defines the retained
`ResourceSampler` primitive, but §0 supersedes its former attempt binding.

O3 as written above says the runner reads cgroup v2 and the container's
namespace counters directly, resolved from a pid. That assumed the runner is
a host process. It is not: `deploy/host/docker-compose.sandbox.yml` runs the
runner as a container (three replicas) beside the sandboxes it supervises,
with only the Docker socket and three data directories mounted — no
`pid: host`, no host `/proc`, no cgroupfs. The daemon reports a host pid
correctly; that pid then does not exist in the runner's own pid namespace,
every read fails, and the feature reports `coverage = 0` on every turn of
every real deployment. It shipped that way and was caught in the UI, not by
a test — the fixtures supplied a procfs, and the one host without cgroupfs
(macOS) looked like expected behaviour.

**Amended mechanism:** counters come from the Docker daemon's own accounting
(`GET /containers/{id}/stats?stream=false&one-shot=true`) over the socket the
runner already holds.

Granting the runner `pid: host` + `cgroup: host` + a cgroupfs mount would
also work, and was rejected: it *manages* the topology assumption with three
deployment knobs instead of removing it, and each knob fails exactly the way
the original bug did — silently, and identically. `/proc/<pid>/cgroup` is
relativized to the reader's cgroup namespace, so with the default private
namespace the path read back is not even resolvable against a mounted host
cgroupfs. The daemon route has no such assumption: it answers the same
whether the runner is on the host, in a container, or on a machine that
joins the pool later.

**The invariant is unchanged**, which is why this is an amendment and not a
new decision: nothing is asked of the sandbox, nothing runs inside it, no
agent-visible process participates, and a turn can neither see, refuse, nor
forge its own measurement. What changed is which process outside the sandbox
does the reading.

For the later restartable-sandbox target, the same daemon-only rule also
applies to storage and hook journals: the supervisor has no per-chat host
filesystem mapping and never opens Docker's data root. It reads container
size via Engine inspect and streams any container-local journal via Engine
exec. `/workspace`, native CLI state, and the journal remain in the chat
container's writable layer.

Consequences:

- `sampler_version` becomes `2`. Stored rows keep the semantics they were
  written under, which is what that column is for.
- The pid-reuse guard is deleted rather than fixed: identity is now a
  container id, so a released pid handed to a host process — whose interface
  counters would have arrived as this turn's network — cannot arise.
- Block io is reported as **unmeasured** when the daemon returns no rows,
  not as zero. Reading `io.stat` directly, an empty file is written by the
  kernel and means no io; through the daemon the same emptiness is ambiguous
  (some Docker versions never populate it on cgroup v2). Ambiguity resolves
  to "not measured", never to a fabricated zero.
- Memory subtracts `inactive_file` as the `docker stats` CLI does, so an
  operator comparing the two sees one answer rather than two.
- External tests exercise the sampler against the daemon directly. The current
  runner does not emit a per-turn sampler warning because shared-member totals
  are intentionally unavailable, while ordinary backend preflight already
  requires the Docker authority used for lifecycle operations.

## 5. Architecture

Current per-turn path:

```text
retained group container                 sandbox manager                 control plane
────────────────────────                 ────────────────                 ─────────────
member A live CLI (UID A) ─┐
                            ├─ shared /workspace
member B live CLI (UID B) ─┘
  │                                         per-turn ToolWindowTracker
  ├─ Claude passive marks ──────────────► member terminal frame ──────► attempt/tool rows
  └─ fork/exec/exit ─► guest-root eBPF ──► UID-filtered cursor + attribution

runner: claim/lease/cancel + terminal-frame drain; never charges group Docker totals to a member
```

The following diagram is the original roadmap. Its sampler, egress, aggregate,
and alert branches are retained as deferred direction, not the current member
execution path:

```text
sandbox (docker, per residency)      runner (a container beside it)     control plane host
─────────────────────────────       ───────────────────────────        ──────────────────
harness + tool children              ResourceSampler (1 Hz, via the      worker API (exists):
  │                                  daemon's /containers/{id}/stats)     events, continuity,
  ├─ claude marker ─┐                │                                    + NEW resource rollups
  └─ fork/exec/exit ┴─► guest-root   ├─ process/mark journal reader
                       eBPF observer ├─► window rollup at turn/idle close ─► Postgres rows
squid egress                         │                                    agent_job_events (exists)
  access log (bytes/verdict) ──►     egress log parser ─────────────────► net flow rows
                                     │
                                     emit() seam (default no-op;          aggregate API ─► console admin pages
                                     OTLP-attachable later, O1)           alert checker ─► Slack webhook
gateway api_logs (exists, billing authority) ───────── joined by grant attribution / attempt window ──► #57 read path
```

The current sandbox manager→runner→CP path uses the existing worker-report pattern
(`worker_report_continuity` is the precedent): a small authenticated POST at
window close, idempotent per `(attempt_id, kind)`. The emit() seam may be
lossy by contract; Postgres rows are the facts (O1).

## 6. The resource model (the new part)

**Aggregate sampling.** Current shared-member attempts do not sample Docker
totals. They store the explicit unavailable shape in §0. The daemon-backed
sampler remains available only for a future group/residency window and for
historical-row compatibility; such a caller must never label its output as one
member's consumption.

**Windows.**

| Window | Boundaries | Rollup destination |
|---|---|---|
| Attempt | sandbox manager turn admission → terminal boundary | `attempt_resources` metadata; aggregate counters unavailable |
| Tool call | hook markers (`exact`) or sandbox manager event arrival (`message`) | `tool_call_resources` timing + optional process accounting |
| Guest process | member cursor → terminal boundary, filtered by fixed UID | `agent_process_observations` |
| Running-idle / stopped residency | group lifecycle boundaries | deferred group-scoped residency series |

**Per-tool mechanics.** For `message`-level drivers, a call's window is
[`tool_use` arrival, matching `tool_result` arrival] joined on
`tool_use_id`. The model has already produced the call by the opening event;
the wider window is labelled `message`, not `exact`, because it also contains
the harness/platform delay around the actual execution. For claude-code
`exact`, each
PreToolUse/PostToolUse hook `exec`s one immutable, image-bundled helper. The
helper timestamps before reading stdin and sends one bounded mark to a Unix
datagram socket owned by the Kata guest-root observer. The observer requires
kernel-supplied peer credentials, verifies `/proc/<pid>/exe` names the fixed
helper inode, and appends the mark to the same root-only, loss-accounted journal
as eBPF fork/exec/exit events. Its lifecycle remains visible in the complete
census, but that exact executable identity excludes it from per-tool workload
counts. Trusted
non-Kata development falls back to the old bounded state-file reader; Kata
never treats that agent-writable file as an attribution authority.

Hooks remain **passive by contract**: one bounded helper, a 25 ms non-blocking
stdin deadline plus a one-second outer CLI timeout, and exit 0 for every
invalid input or output failure. A hook that cannot record a mark leaves the
window message-level and must not block the tool. The root-only
journal prevents repository code from editing, truncating, or directly
injecting records. Exact tool attribution is still diagnostic rather than a
security or billing boundary: code running as the Agent CLI's UID can invoke
the marker helper itself. That limitation cannot be removed while hooks and
tools intentionally share one UID; the independent eBPF process census remains
the authoritative record of which processes existed.

**Historical sample attribution.** For stored sampler-backed rows and the
lower-level primitive, a delta between two samples is assigned to the tool windows
open during that interval. One open window: exclusive. `n` overlapping
windows: each records the shared delta with `overlap_n = n`; sums over
`overlap_n > 1` are upper bounds by construction and are stored under
`shared_*` fields, never `exclusive_*`. In-harness compute between tool
calls (the model streaming, the CLI's own work) accrues to the attempt, not
to any tool — visible as `attempt − Σ exclusive`, which is itself a useful
series (harness overhead).

**Exit-time accounting (Kata).** The sampler's grid is coarser than 97 % of
calls, so under Kata the per-call numbers come from the guest kernel instead,
from two records per task. `sched_process_exit` carries the group's settled
`maxrss`, the task's `task_io_accounting` bytes, and whether this exit
emptied the thread group (`signal->live` reached zero, which `do_exit`
decrements before the tracepoint). CPU is deliberately *not* read there:
`utime`/`stime` are tick accounted, and a 400 µs process that never received
a tick reads zero — exactly the case this exists for. The scheduler's own
runtime (`se.sum_exec_runtime`, the number `cputime_adjust` scales the tick
split to, so the total `/proc` and `wait4()` report) is final only once the
task has been dequeued for the last time, which is where `sched_switch` sees
it in `TASK_DEAD`; a fourth probe emits a `done` record with it there, and the
predicate is the only work on every other context switch. Each task reports
only its own share; the host sums a group's threads into its leader and never
adds `signal->utime` on top, which would count dead threads twice. A group's
numbers are settled only when the group is dead *and* every task whose exit
was seen has also reported `done` — Linux lets a leader exit while its
threads live on, and a leader's exit must not be mistaken for the process's.
Every counter is guarded by its presence in the guest's BTF and arrives as
absent, never as zero, on a kernel built without that accounting; the
preflight self-test requires a sub-millisecond child's `done` to carry
non-zero CPU. A process is charged to a call only when its start fell inside
that call's window alone; one that overlapping windows make plausible for
several calls, one whose group is not settled, or any process at all in a
census that lost events, is charged to none of them and marks every call it
may belong to as `process_resources_partial`, so a per-call sum presented as
exact never contains another call's work or hides a gap. CPU and I/O are sums
over a call's processes, the peak is the largest single process (their peaks
need not have coincided), and work the agent did inside its own resident
process belongs to no call. This answers the "fine-grained tool call resource
usage" question for a call of any length, including the 35 ms median.

**Network per tool is deferred.** The intended model remains windowed parsed
egress flows (connection window = [log time − duration, log time]), while
gateway/MCP usage joins by attempt. No current shared-member turn reports a
container network total or renders the planned network panel.

**Stored rows** (Postgres, implemented unless marked deferred):

```text
attempt_resources: attempt_id PK, cpu_usec, mem_peak_bytes, mem_avg_bytes,
  io_read_bytes, io_write_bytes, net_rx_bytes, net_tx_bytes,
  sample_count, coverage, sampler_version, tool_time_ms,
  tool_calls_truncated, process_observer, process_observation_complete,
  process_events_lost, processes_truncated, created_at
  — current shared-member writers leave aggregate counters null and use the
  shared-member-v1 shape from §0

tool_call_resources: attempt_id + call_seq PK, tool_use_id, tool_name,
  timing_source(exact|message), duration_ms, sample_count, overlap_n,
  measured_span_ms, historical sampled CPU/memory fields, process_count,
  process CPU/memory/I/O floors, process_resources_partial
  — capped at 500 rows per attempt; the parent row says when rows were dropped

agent_process_observations: attempt_id + process_seq PK, member uid,
  pid/tgid/ppid, comm/executable, relative start + duration, lifecycle flags,
  exit status, plausible tool call ordinals, and available exit-time counters

egress_flows: deferred; no current table or per-domain UI is implied
```

**Costs, honestly.** The current path writes one attempt row, at most 500 tool
rows, and at most 4096 bounded process rows; it makes no 1 Hz Docker request
per active member. The trusted non-Kata fallback mark read is bounded to 1 MiB
per turn. Deferred egress rows, checker, aggregate API, and console pages add
no standing cost today. The guest journal has a
64 MiB hard cap. Any reusable guest rotates it at 48 MiB only when no turn
cursor is active; a single process storm that exhausts the remaining reserve
is reported incomplete rather than silently losing processes.

## 7. Signal catalog (initial; the allowlist test pins it)

**Aggregates** (DEFERRED with the admin layer — served by the CP aggregate
API to the console, watched by the checker; bounded dimensions per O7):
turn duration histogram ×
(runtime, continuity_mode); turns total × (terminal state, degradation
reason); continuity mode counter (the §9 aggregate, finally); later persistent
phase: Docker data-root backup/restore outcome + size histogram; residency
establish/loss/takeover counters
× reason; stop/start/native-resume outcome and duration; running-idle/stopped
residency gauges; claim-loop lag gauge; event upload queue depth; attempt
cpu/mem/io/net totals as histograms × runtime; egress verdict counter × (allowed |
denied); unclassified-bytes gauge; sampler coverage histogram; tool duration
histogram × (tool_name, source).

**Ops records** (alert-on): attempt terminal failures with error class; later
persistent phase: retained storage backup/restore failure (projected, not
re-emitted);
`residency_unusable`; grant mint/link degradation; reaper actions; sampler
start/stop/gap.

**Ledger projection**: floor events + terminal events, deny-body per O9,
dedupe key `(attempt_id, client_seq)` carried in attributes.

**Alert seed set** (DEFERRED with the admin layer — a CP-side periodic
checker over Postgres/CP state, posting to its own Slack webhook — the
`alert_slack` pattern, deliberately never wired into the status-monitor
control plane): claim loop silent > 2 min; turn
failure rate; later persistent phase: backup/restore failure streak (a retained
container must never silently lose native context); denied-CONNECT burst;
unclassified-bytes growth; sampler coverage < 0.9 sustained; running-capacity
or stopped-container disk/admission saturation.

## 8. Degradation matrix

| Situation | Behavior |
|---|---|
| Current shared member | Aggregate counters null, `coverage = 0`, `sampler_version = shared-member-v1`; timing/process evidence remains independent |
| Historical/group sampler crashes | Work unaffected; rollup has partial coverage; never converted to a measured zero |
| Attached export backend down (if any) | emit() drops per SDK policy; Postgres facts, console, and checker unaffected (O1) |
| Marker unavailable / harness without hooks | `source = message` windows; no error |
| Hook cannot execute | Tool proceeds (passive contract); coverage of `exact` windows only |
| Parallel tool calls | timing remains valid; ambiguous guest processes are charged to no call and affected rows are partial |
| Guest observer unavailable | Tool timing remains; process observer is null/incomplete and no process use is fabricated |
| Guest journal loss/truncation | Observation and affected process-resource rows are explicitly partial |
| Cancel/timeout after admission | Member is interrupted and terminal telemetry is drained; context becomes LOST per the session design |
| Sandbox Manager stream disappears | Member is aborted/reaped; runner persists the explicit unavailable aggregate if no terminal frame arrived |
| RUNNING residency with no attempt | No member resource row is invented; future group accounting remains deferred |
| STOPPED residency | Docker stop preserves the writable layer; no workload measurement is attributed to a member |
| Clock skew runner↔CP | Windows are runner-local monotonic; only display joins use wall clock |

## 9. Contract tests

1. Current shared-member output never contains a whole-container CPU, memory,
   I/O, or network total; its semantic version and zero coverage are explicit.
2. Tool rows preserve issuance order, cap at 500, include unfinished calls,
   and never turn dropped/overlapping evidence into exclusive usage.
3. Claude markers upgrade a window only with one valid pre/post pair inside
   its message bracket; malformed, late, duplicate, or blocked hooks degrade.
4. The hook helper is passive, bounded, and exits successfully on bad input or
   output failure.
5. Concurrent member cursors are UID-filtered and one cursor closing does not
   rotate/restart the singleton observer underneath another.
6. Short-lived Kata children retain fork/exec/exit and scheduler CPU evidence;
   missing BTF/accounting fields remain null.
7. Observation closes before reaper/normalizer work and is returned on both
   success and activation-boundary failure.
8. Cancel/timeout drains a sandbox manager terminal resource frame, while lease loss
   interrupts and fences the member.
9. Re-posting one attempt rollup replaces its bounded child rows atomically and
   never doubles them.
10. The API/UI distinguish unavailable turn totals, partial historical totals,
    complete empty process observation, and a missing telemetry row.
11. Daemon-sampler unit/external tests continue to validate the lower-level
    group primitive without binding it to a shared member.
12. Kata deployment preflight proves the pinned observer and marker binaries
    against a real guest kernel before a runner advertises the capability.

## 10. Implementation status and remaining plan

The per-chat surface ships first; everything admin-facing remains deferred.

| Phase | Content | Note |
|---|---|---|
| **SHIPPED — chat context/timing** | `NEW/LIVE/RESTORED/LOST`, historical continuity fallback, turn duration, Trajectory, bounded per-tool durations and time split | Message-level is labelled as an outside bracket |
| **SHIPPED — Kata process observation** | UID-filtered lifecycle census, exact Claude markers, exit CPU/RSS/I/O floors, API/storage/UI, honest loss/truncation | Aggregate member totals remain unavailable by design |
| **RETAINED PRIMITIVE** | Daemon-backed `ResourceSampler` and attribution arithmetic | Tested for future group/residency use and historical rows; not bound to current members |
| **DEFERRED — CHAT-3** | Squid flow parser + `egress_flows` + network panel / denied count | No table or UI is currently promised |
| **DEFERRED — CHAT-5** | Per-turn inference cost through the gateway read path | Billing remains authoritative in the gateway |
| **DEFERRED — admin layer** | emit() seam + severity mapping + ops projection; CP aggregate API + console admin pages; alert checker + §7 seed set | Direction agreed in this doc; picked up when admin observability is prioritized |
| **Later candidates** | Standard metrics stack (OTLP → Prometheus/Grafana) behind the seam; claude-code native OTel passthrough (verify against pinned CLI); MCP relay byte counters; pi native `tool_execution_*` boundaries; spans; gateway-side paper events (other repo) | Not committed by this doc |

The next resource work must choose a truthful accounting unit first. A
group-level sampler can support capacity/residency views; member-level totals
need a separate kernel/cgroup design that isolates the member, not a division
or copy of one shared container delta.

## 11. Product surfaces

What a person actually sees. The DS harness's user-facing observability is
the transcript itself plus a sharing disclosure — verified: no user-visible
cost, resource, or diagnostics UI exists in its docs or packages. It is a
local tool; its user owns the machine and can run `htop` themselves. We are
hosted: the user cannot see the machine, so the surfaces below are new
product, not ports.

**End user (thread UI).** The implemented per-turn surface is:

- **Turn footer:** the authoritative context badge (`new`, `context live`,
  `restored`, or `context lost`) and duration. Historical rows without context
  status may fall back to the old continuity badge. Current retained turns
  never produce replay.
- **Aggregate honesty:** current rows say `turn totals unavailable`; they do not
  show CPU/memory/network zero or copy the shared container's totals. Historical
  sampler-backed rows remain readable and label partial coverage as a floor.
- **Expanded:** bounded per-tool timing and model+platform/tool time split. Under
  Kata, process starts and exit-time CPU/RSS/I/O floors appear for calls with
  unambiguous ownership, with partial/loss labels where appropriate.
- **Not yet present:** inference cost, per-domain/network totals, denied counts,
  egress panels, or an admin aggregate view. Those remain the deferred rows in
  §10.
- **The Trajectory view — the DS-parity surface.** A second tab beside the
  chat, mirroring the DS harness's own Trajectory page: the full event list
  (user / assistant / tool / context rows) with a per-call drill-down panel
  (payload, result, duration, and a timing-source label — DS's panel labels
  its source too), a session minimap strip, a bottom stats bar (turns,
  steps, model-time vs tool-time, TTFT, tok/s where usage events carry
  counts), search, and session-log export. This is a pure read of the
  stored event ledger. Honest gaps are labelled in the UI: non-Claude and
  unmarked call timing is message-level, and a schema
  tab can only cover tools we recognize (runtime catalogs plus what the MCP
  relay saw) — unknown tools show name and payload only. Tier-2 threads
  degrade to the raw tail, floor events only.
- **Honesty rules are product copy, not fine print:** unavailable totals stay
  unavailable; a historical turn with partial coverage is a floor; process
  loss/truncation stays partial; and `message` timing is an outside bracket.

**Admin (DEFERRED — 08-19 scope cut).** Console admin pages — the same chart components as the
user-facing turn views, pointed at the CP aggregate API; no third-party
dashboard (O1), and the same pages serve future org admins, who would never
be handed a Grafana anyway: health pulse (claim lag, queues), continuity
funnel (LIVE and RESTORED context rates per host, with same-PID HOT as optional
transport diagnostics — #61's data), capacity (RUNNING
slots plus STOPPED container/storage admission water-line), consumption
leaderboard (outlier turns), security
(denied-CONNECT bursts, unclassified-bytes growth). Slack alerts from the §7
checker. Per-attempt detail exposes coverage, sampler version, and
degradation reason — the support answer to why native context was lost.

**Alignment with DS, stated as product:** we adopt its three philosophies —
transcript fidelity (our floor contract guarantees a never-blank turn;
Tier-2 remains a raw tail, honestly below their bar), honest degradation
(they never degrade so never say; we degrade, so we badge it), and
say-what-you-record (their consent disclosure becomes our transparency
statement: events and the implemented resource evidence are recorded; the
deferred exporter and egress ledger do not exist yet).

**Doors opened, not committed:** group capacity accounting, member-isolated
resource limits, org usage reports, compute-based pricing, or quotas.

## 12. Deferred questions

1. (Deferred with the admin layer) Alert webhook destination and channel
   (its own webhook, `alert_slack` pattern; a third producer — never the
   status-monitor control plane).
2. Whether group-level Docker sampling is useful enough for residency/capacity
   views, and how those rows remain visibly separate from member attempts.
3. (Deferred with the admin layer) Which runner-local signals the checker
   needs pushed to the CP (claim heartbeat exists; does sampler liveness
   ride the rollup POSTs or its own beat?).
4. Tool-name label bound for Tier-2/raw runtimes (unknown tool vocabularies
   — likely `other` bucket to protect O7).
5. Whether non-Claude adapters expose a stable passive native boundary that can
   improve on their current message brackets.
6. Postgres partitioning for `egress_flows` if volume demands it (start
   unpartitioned, telemetry decides).

---

*Sign-off: Murphy, Juncheng. Implementation rides one PR per the house rule,
with this document cherry-picked in — not merged separately.*
