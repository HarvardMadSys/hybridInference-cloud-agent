# Session-resident harness threads

- **Status:** historical implementation record. The container/session
  lifecycle is superseded for new work by
  `2026-08-28-one-chat-one-restartable-sandbox-design.md`; the checkpoint
  sections remain input to the separately deferred persistent-storage phase.
- **Date:** 2026-08-17
- **Supersedes:** the "thread, not the harness session, is the unit of
  continuity" rationale in
  `docs/agents/specs/2026-07-30-agent-harness-adapter-audit.md` §8
  (hybridInference history, commit `9383cbd2`)
- **Replaces the behavior introduced by:** hybridInference #1113
  ("persistent Cursor-style task threads" — fresh sandbox + text replay per turn)

> **2026-08-31 supersession notice.** New capable chats now use the implemented
> shared-live design in
> `2026-08-31-native-session-fork-shared-workspace-design.md`: a supervisor owns
> one live CLI transport per member, ordinary chats remain one container each,
> and an immediate runtime-native Fork adds a second independently runnable chat
> and process to that same container/workspace. Group idle timeout closes the
> processes and uses `docker stop`; the next member interaction starts the same
> container and lazily resumes that native session. The 2026-08-28 Docker-clone
> Fork is only a superseded design record; its implementation was removed
> before merge and is not a `legacy_1to1` compatibility path.

> **2026-08-28 supersession notice.** The next phase retains exactly one
> container per `agent_thread`, starts a live Agent CLI on turn 1, stops (but
> does not remove) the container after settled idle timeout, and starts a new
> process with native session resume on the next message. Same-PID reuse is an
> optimization, not a correctness or topology requirement; LIVE/RESTORED
> context is the product contract. Chat state
> lives only in the container writable layer, without a per-chat host bind.
> Fork at the latest settled turn creates a new chat from a coordinated copy
> of database history and the complete Docker filesystem snapshot. The clone
> uses ordinary native resume against its copied local state; it does not call
> a driver-native fork or replay historical prompts, and shares no writable
> resource with its source. Per-turn model/reasoning changes remain supported
> through in-band control or a context-preserving CLI process rotation.
> The new design supersedes D3, D6, D8, the continuity/residency lifecycle in
> §§4/7/9, P3/P4 in §11, and the process behavior in §§13/14. Per-turn jobs,
> attempts, leases, events, billing windows, and generation fencing remain.
> Historical-turn fork, sandbox cleanup, and native checkpoint/cold storage
> are not part of this phase and must not be inferred from this historical
> document as approved implementation scope.

---

## 1. Problem

Today every follow-up turn cold-starts: a fresh sandbox, a fresh harness
process, and a prompt assembled by `conversation_prompt()` from a
runtime-neutral transcript (user prompts + assistant final messages, capped at
40k chars) plus the parent turn's unpublished patch.

Split a running conversation into four layers of state:

| # | Layer | Today |
|---|---|---|
| 1 | Conversation memory (messages, tool calls, results) | **Lost** — only final replies replayed |
| 2 | Tracked files (git) | Kept (patch restore) |
| 3 | Untracked environment (installed deps, generated files, build state) | Rebuilt by setup; agent's manual installs silently vanish |
| 4 | Process state (harness process memory, surviving background children)¹ | Lost |

¹ Layer 4 deliberately does **not** include shell `export`s/cwd across tool
calls: many harnesses spawn a fresh `bash -lc` per tool call, so that
sub-state is harness-dependent even under HOT. Per-driver behavior tests
decide what more each driver may promise.

Observable consequences: the agent re-explores the repository every turn
(tokens + latency), loses its own trial-and-error history, **confabulates**
answers to "why did you do X last turn" (it reconstructs from the diff), hits
memory-vs-reality mismatches ("I installed foo" — foo is gone), and long
threads silently fall off the 40k transcript cliff. The gap is smallest for
1–2 turn tasks (our dogfood pattern — which is why nobody noticed) and largest
for exactly the long iterative threads real users will run.

This behavior was never a reviewed decision: #1113 was merged 9 minutes after
opening, and the audit doc rationalized the implementation the following day.
This document is the reviewed decision.

## 2. Goal

> **A thread is a resident harness session.** Conversing in a thread feels
> like a local terminal: within the residency TTL the harness process itself
> is alive (layers 1–4 continuous, with the layer-4 caveat above). Beyond the
> TTL it degrades to what a local `--resume` gives you:
> **native-session-equivalent continuity** — the harness restores its own
> session (subject to its own compaction), environment rebuilt. Replay is
> always *available* as the final fallback path; a thread is never lost to an
> infrastructure event.

Non-goals (v1): mid-turn interject/steering (enabled by this architecture,
separate project), cross-harness threads, driver plugin frameworks,
unifying every harness's raw event vocabulary. (True HOT sat here while it
waited on residency-scoped credentials; the §14 amendment un-deferred it and
it is implemented — the live process rides the residency's grant.)

## 3. Decision record

- **D1 — Continuity unit is the harness session.** Supersedes audit §8's
  "fusion substrate" argument. The transcript remains as durable record, UI
  source, and last-resort fallback — it is no longer what the agent is fed.
- **D2 — Harness is fixed per thread.** The follow-up `runtime` override
  returns 400 when it differs from the thread's; `registered_runtimes()`
  membership is validated at create and follow-up (closes the current
  unvalidated dark path).
- **D3 — HOT means process residency. There is one HOT.** In-sandbox
  re-invoke via native resume is (a) HOT's internal crash-recovery path and
  (b) a *compat mode* for harnesses without live input, visibly marked
  below-bar. Degradation across tiers is **automatic and observable**: the
  user never configures it, but every turn's continuity mode
  (`hot | cold_native | replay`) is recorded on the attempt, shown in the UI,
  and aggregated in admin.
- **D4 — Cross-turn contamination is accepted.** A poisoned turn (e.g.
  prompt injection read from repo content) contaminates subsequent turns.
  Inherent to session continuity; recorded, not mitigated away.
- **D5 — Credentials: per-residency-generation handles, two audiences,
  per-call resolution.** No residency-bound renewal, and no thread-lived
  handles (both break fencing or attribution). See §8.
- **D6 — Per-turn model switch stays.** On harnesses with
  `model_switch: restart` it costs an in-sandbox process restart
  (`launch(state=...)` with the new model); layer 3 survives, layer 4 doesn't.
- **D7 — Sandbox-spawn boundary; transport is driver-owned.** The harness
  process **must run inside the sandbox**; runner-side driver objects operate
  it only through a driver-owned, controlled transport. No runner-side
  dependency may spawn or execute the harness outside the sandbox: pure
  protocol/type or client libraries are fine runner-side, but any vendor SDK
  that creates the harness process, executes tools, or reads the workspace
  runs inside the sandbox or is replaced by speaking the wire protocol
  directly. v1 drivers use container stdio (Claude stream-json, Codex
  app-server JSON-RPC, Pi JSONL RPC); an HTTP/SSE transport (e.g. OpenCode)
  is a valid future driver choice.
- **D8 — Fork scope (v1 product decision).** v1: memory-carrying fork from
  the **latest** settled turn (native); older anchors degrade to today's
  replay-fork. This is scope, not necessity: the latest harness state
  typically retains the full history, so historical **native** fork needs no
  per-turn checkpoints — only a small per-turn anchor mapping
  (`platform_turn_id → harness_anchor_id`) plus three conditions:
  the latest state still retains the anchor, the driver supports
  fork-at-anchor (Codex `thread/fork(lastTurnId)`, Pi `fork(entryId)`,
  OpenCode session fork by message id; Claude's CLI/wire equivalent of the
  SDK's `fork_session(up_to_message_id)` is unverified), and the target
  turn's workspace is recoverable — which for settled turns it already is
  (per-turn patch artifacts and published commits are retained today).
  The `fork(at=...)` signature is reserved now so v2 needs no interface
  change; v1 accepts only `None`/latest.
- **D9 — Mandatory event floor and result contract.** Every driver emits at
  least `turn_started / output_delta / turn_completed / turn_failed`;
  richer harness events flow as `raw`. The hard guarantee lives on
  `TurnResult`: `status == "completed"` ⇒ `final_output` non-empty — a driver
  whose harness produced only tool results must synthesize a displayable
  final output rather than yield an empty thread (contractually prevents the
  pi blank-thread class of bug, and feeds the replay fallback). The existing
  9-kind normalized vocabulary remains the Tier-1 quality bar; the floor
  replaces "Tier 2 = raw only".

## 4. Architecture

```text
Control plane                    Runner host                       Sandbox
─────────────                    ───────────                       ───────
thread residency registry   ◄──  residency heartbeat (gen-scoped)  harness process
affinity claim (preferred_host)  HarnessDriver / Session / Turn    (claude/codex/pi…)
credential resolution (gen)      driver-owned transport            workspace
ThreadCheckpoint storage         checkpoint export/restore
```

Continuity ladder (automatic and observable degradation):

```text
hot          process alive, next turn injected via live input   layers 1-4¹
             (on the residency's thread-attributed grant, §14)
warm_native  resident workspace + per-turn --resume restart     layers 1-3
             (the restart rung: grantless residencies, model
             switches, MCP turns, and the turn that spawns the
             live process — the process must predate a turn for
             that turn to be ``hot``)
cold_native  new sandbox + ThreadCheckpoint import              layers 1-2
replay       new sandbox + transcript prompt (today's path)     layer 2 only
```

`HOT → COLD-native → replay` — each step triggers only when the one above is
unavailable; replay is always available (it can itself still fail on clone/
setup/model errors, like any turn today). Every attempt records which mode it
ran in.

## 5. Driver contract

Three objects, mirroring the harnesses' own native models
(Codex `app-server → thread → turn`, Claude `process → session → query`,
Pi `RPC process → session → prompt`):

```python
class HarnessDriver:
    id: str
    async def launch(self, context: LaunchContext,
                     state: HarnessState | None = None) -> HarnessSession: ...

class HarnessSession:
    async def start_turn(self, input: TurnInput) -> HarnessTurn: ...
    async def export_state(self) -> HarnessState: ...   # §6 preconditions; atomic or fail
    async def fork(self, at: ForkPoint | None = None) -> ForkResult: ...  # v1: None only (D8)
    async def close(self) -> None: ...

class HarnessTurn:
    def events(self) -> AsyncIterator[HarnessEvent]: ...  # floor events mandatory (D9)
    async def steer(self, input: TurnInput) -> None: ...  # capability-gated
    async def cancel(self) -> None: ...
    async def wait(self) -> TurnResult: ...

class TurnResult:
    status: Literal["completed", "failed", "cancelled"]
    final_output: str          # non-empty when status == "completed" (D9)
    usage: Usage | None        # display/diagnostics/reconciliation only —
                               # the gateway ledger is the billing authority;
                               # disagreement never changes the bill
    raw: dict | None
```

Contract clauses:

- Objects live **runner-side**; the harness process lives in the sandbox;
  the transport between them is driver-owned (D7).
- `launch(state=None)` = fresh; `launch(state=…)` = restore. One signature
  covers cold start, in-sandbox crash recovery, and cross-host recovery.
- `export_state()` preconditions: no active turn, event stream terminal,
  harness flushed. It returns a complete, verifiable state **or fails** —
  never a best-effort partial.
- Event envelope carries `attempt_id` and a monotonic `event_seq`, so
  reconnects and re-uploads are idempotent (dedupe on
  `(attempt_id, event_seq)`).
- **Harness turn completion ≠ platform attempt success.** The turn-finish
  pipeline is: `turn_completed` → quiesce → `export_state` + workspace
  snapshot → validate → checkpoint commit (§6) → attempt outcome. The UI may
  render the final output as soon as `turn_completed` arrives, but the
  attempt's terminal state lands only after the pipeline resolves.
- Process liveness is layered, none of it in the public interface: the
  runner supervises PID/container liveness; the driver converts transport
  EOF/protocol errors into session/turn failures; residency heartbeat tells
  the control plane whether a session is still held.
- Cancellation backstop stays platform-owned: `turn.cancel()` is the polite
  path; the runner's kill of the sandbox is the enforcement path.

Capabilities are semantic enums, not booleans:

```python
class HarnessCapabilities:
    continuity:  Literal["hot", "resume", "replay"]
    steering:    Literal["mid_turn", "queue_only", "none"]
    fork:        Literal["at_latest", "at_anchor", "none"]   # v1 uses at_latest
    model_switch: Literal["per_turn", "restart", "none"]
```

Provisional matrix — **every value must be pinned by contract tests against
the version-stamped CLI before it is trusted**:

| | continuity | steering | fork | model_switch |
|---|---|---|---|---|
| claude-code | hot (stream-json multi-turn stdin) | queue_only (interrupt channel may upgrade to mid_turn) | at_latest (`--resume --fork-session`); at_anchor unverified on the wire | restart |
| codex | hot? (app-server JSON-RPC) | mid_turn? | at_anchor? (`thread/fork`) | per_turn? |
| pi | resume→hot? (RPC mode; sessions currently disabled by our own `--no-session`) | mid_turn? | at_anchor? (`fork(entryId)`) | per_turn? |
| kilo / opencode | replay (until drivers land; opencode transport likely HTTP/SSE) | none | none | none |

Static registry, no plugins:

```python
DRIVERS = {"claude-code": ClaudeCodeDriver(), "codex": CodexDriver(), "pi": PiDriver()}
```

## 6. ThreadCheckpoint

The **driver** exports only harness state; the **platform** assembles the
checkpoint:

```json
{
  "harness_state": {
    "driver": "claude-code",
    "driver_version": "x.y.z",
    "state_format_version": 1,
    "runtime_image_digest": "sha256:…",
    "sha256": "…", "size": 1234567,
    "blob": "<compressed native session file(s)>"
  },
  "workspace": { "base_sha": "…", "patch_artifact": "(existing)",
                  "workspace_state_hash": "…" },
  "consistency": {
    "through_turn_id": "…", "attempt_id": 42, "residency_generation": 7,
    "harness_state_hash": "…"
  },
  "setup_digest": "sha256 of setup script — invalidates env assumptions"
}
```

**Consistency (review P0-2).** A checkpoint is only usable when it describes
the thread's present:

- Restore precondition: `checkpoint.through_turn_id ==
  thread.latest_settled_turn_id`. On mismatch, native resume is **forbidden**
  and the turn runs replay — never "turn 10 memory + turn 11 code", which is
  the exact memory-vs-reality disease this design exists to cure.
- Commit ordering: quiesce → export harness state → snapshot workspace →
  validate both → write checkpoint → **atomically update
  `thread.latest_checkpoint`** → attempt outcome → GC the previous
  checkpoint.
- Export failure does **not** fail the attempt (the user's patch is safe;
  infra hiccups must not punish the turn). It sets
  `thread.cold_resume_available = false` (visible in admin): if residency is
  later lost, the next turn runs replay. A subsequent successful export
  restores native resume.

**Storage & ops** (v1 Postgres, honestly costed):

- `agent_job_artifacts` kind `checkpoint`, existing
  `(job_id, attempt_id, kind)` upsert. "Zero migration" is an MVP
  convenience, **not** a long-term data-model commitment — the access path
  goes through an `ArtifactStore` seam so blob storage can move out of
  Postgres without touching callers.
- Retention: latest per thread (logical); note Postgres reclaims physically
  only via vacuum — TOAST bloat, WAL amplification and backup growth are
  real. Telemetry: compressed/uncompressed size, write latency, over-cap
  rate, TOAST size.
- Size cap: **16 MB compressed** initially (over-cap ⇒ thread marked
  `cold_resume_available = false`, logged).
- Checkpoints are **encrypted at rest** (they can contain source, tool
  output, secrets — reuse the MCP-connection encryption machinery) and are
  deleted with their thread.
- Export runs at every turn end regardless of process survival (HOT's crash
  insurance and COLD's substrate).
- COLD import = three pieces: clone at pinned base + apply patch (existing,
  incl. safety gate), write harness_state back to its native path,
  `launch(state)`. **Hard prerequisite: workdir and HOME paths are stable
  across turns** (session-file paths derive from them) — verify, else pin
  `/workspace`.

## 7. Residency

New object: *thread T's hot sandbox, generation G, resides on host H, lease
until X.* Sandbox lifetime is decoupled from attempt lifetime (1 sandbox : N
attempts, with idle windows); attempts, events, billing, and fencing stay
strictly per-turn.

```text
Residency {
  thread_id, generation, host_id, sandbox_id,
  runtime_id, runtime_version,
  lease_expires_at, last_checkpoint_turn_id,
  status: none | hot | draining
}
```

- **Generation fencing (review P0-1).** Every residency operation carries
  the generation: `heartbeat(thread, gen)`, `evict(thread, gen)`,
  `start_turn(thread, gen)`, `credential_resolve(thread, gen)`. Takeover is
  atomic: `generation += 1, host_id = new, sandbox_id = new`. A late
  heartbeat from a previous generation's host can never re-promote the old
  residency. Attempt fencing guards event/result writes; residency
  generation guards handles, heartbeats and sandbox management — both are
  required for "double fencing" to be true.
- **States:** `none → hot → draining → none`. TTL (default 30–60 min,
  configurable) counts from each turn's end; any turn refreshes it. A COLD
  start establishes a new residency (generation+1) and the sandbox then
  stays hot.
- **Heartbeat:** piggybacked on host heartbeat, generation-scoped. The
  reaper watches residency leases in addition to attempt leases; expiry ⇒
  control plane unilaterally marks `none` ⇒ next turn COLD.
- **Eviction:** per-host hot-sandbox budget (memory), LRU. `draining`
  refuses affinity claims. Teardown ordering: quiesce → confirm last export →
  kill process → destroy sandbox → release. Host drain evicts all. A CD
  rebuild that kills hot sandboxes demotes threads to COLD (checkpoints
  already committed) — no longer thread-fatal.
- **Scheduling:** follow-up jobs carry `preferred_host`; held for it ~30 s,
  then opened to any host (⇒ COLD).
- Concurrency semantics unchanged: a follow-up during a running turn stays
  `waiting` (mid-turn injection = the future interject feature).

## 8. Credential model (review P0-1 incorporated)

**Rejected #1:** binding `agr`/`ajt(mcp)` renewal to the residency lease —
turn N's grant would authorize and be billed for turn N+1's calls.
**Rejected #2:** one stable handle per **thread** — a handle leaked from
residency generation 7 would still resolve after the thread re-established
generation 8 on another host, letting a stale sandbox borrow future attempts.

**Adopted:** per-residency-generation handles with split audiences, resolved
per call — the pattern `resolve_model_credential` already implements
(reject unless the fence is live), extended one level:

```text
handle = { thread_id, residency_generation, audience: inference | mcp }
minted at residency establishment (every launch); one per audience —
a leaked inference handle buys no tools, a leaked MCP handle buys no models.

resolution (gateway for inference, relay for mcp), atomic at request entry:
  handle.residency_generation == current_residency.generation
  AND a live attempt exists for the thread
    ⇒ allow; bill/attribute to THAT attempt — bound at entry, so a
      streaming call that outlives its turn still bills to the attempt
      resolved at entry (never re-resolved at completion)
  else ⇒ reject
```

Properties: per-turn billing intact (attribution at resolution, bound at
entry); fencing unchanged in kind and strengthened in scope (attempt
supersede ⇒ reject, residency takeover ⇒ old generation's handles dead);
**an idle hot sandbox holds zero live capability**; renewal machinery is
deleted entirely. `ajt(full)` unchanged: runner-side, strictly per-attempt.

Residual risk: a handle leaked **within a residency generation** is usable
during that generation's later active-turn windows (same user, thread, single
model, same bill), bounded by TTL and by eviction/takeover (both rotate the
generation). Optional hardening for claude-code: `apiKeyHelper` per-turn
rotation. Not a core dependency.

## 9. Degradation matrix

| Situation | Behavior |
|---|---|
| Follow-up within TTL | HOT: live-input injection |
| TTL expired | COLD-native (checkpoint import, new residency generation) |
| Preferred host busy/offline/draining | 30 s, then any host, COLD-native |
| CD rebuild killed hot sandbox | COLD-native (checkpoint pre-committed) |
| Process crashed mid-turn | Attempt fails/retries; in-sandbox `launch(state)`, else COLD from last committed checkpoint |
| Image/CLI version changed | Stamp mismatch ⇒ no injection ⇒ COLD relaunch |
| Checkpoint export failed last turn | Attempt succeeded, `cold_resume_available=false`; on residency loss ⇒ replay until a successful export |
| Checkpoint `through_turn_id` stale / missing / corrupt / over-cap | Replay |
| Harness has no session support | Replay (capability-gated, visible) |

Every attempt records its continuity mode; admin aggregates HOT hit-rate,
export failure rate, and degradation reasons.

## 10. Contract tests

Per driver, against the pinned image (extends the existing
`agent_runtime_streams` fixtures from parse-level to behavior-level):

1. **Memory:** turn 1 reads a value and is told not to repeat it; turn 2
   recalls it. Passes only with real continuity — the existence proof.
2. Floor events emitted; `TurnResult.final_output` non-empty on `completed`
   (incl. a tool-results-only run — driver must synthesize).
3. Degradation matrix, every branch, turn still succeeds.
4. Fencing: revived stale sandbox can neither write events nor resolve
   credentials.
5. `cancel()` effective (polite) and sandbox kill (backstop).
6. Fork-at-latest carries memory; older-anchor fork falls back to replay (v1).
7. Model switch per capability value (incl. restart path).
8. Capability matrix values pinned per (driver_version, image_digest).
9. A handle from residency generation G is rejected while generation G+1 has
   a live turn.
10. Two hosts racing to take over one thread: exactly one generation wins.
11. A streaming model call that spans its turn's end bills to the attempt
    resolved at entry.
12. After a failed export in turn N, no path combines turn N−1 memory with
    turn N workspace (replay is forced).
13. A late heartbeat from a previous generation's host does not revive the
    old residency.

## 11. Implementation plan

| Phase | Content | Note |
|---|---|---|
| **P0** | Version stamping: heartbeat reports `sandbox_runtime`, image digest, runtime versions; stamped onto attempts | Already ratified; prerequisite for compat checks and matrix pinning |
| **P1** | Driver object model: refactor `runtimes.py` adapters into `Driver/Session/Turn` (behavior-neutral), event floor + `TurnResult` contract, event idempotency keys, registry validation + follow-up runtime 400 (D2) | No user-visible change |
| **P2** | **COLD loop** for claude-code: checkpoint export/restore with consistency fields + atomic pointer, `launch(state)`, replay fallback, encryption, telemetry | Ships standalone value: threads stop forgetting. Uses today's per-attempt credentials — residency handles arrive only in P3 |
| **P3** | **HOT**: live-input session, residency object with generation fencing, affinity claim, TTL/eviction, per-generation credential handles + per-call resolution (§8), continuity-mode observability in UI/admin | The resident experience |
| **P4** | pi driver (drop `--no-session`, RPC mode), codex driver (app-server), anchor mapping + historical native fork (D8 v2), WARM go/no-go from telemetry | Ladder-climbing per harness |

## 12. Open questions (resolve during P1/P2)

1. Workdir/HOME stability across turns (pin `/workspace`?).
2. Claude stream-json multi-turn + interrupt behavior on the pinned version
   (may upgrade steering to `mid_turn`); wire-level equivalent of
   `fork_session(up_to_message_id)` for at-anchor fork.
3. Pi RPC / Codex app-server verification (their matrix rows); OpenCode
   transport choice (HTTP/SSE vs ACP stdio).
4. Checkpoint size distribution in practice (cap tuning; blob store beyond
   Postgres if TOAST/WAL telemetry demands it).
5. TTL default and per-host hot-sandbox memory budget.
6. Per-driver shell-state behavior tests (what layer-4 sub-state each driver
   may additionally promise).

## 13. Implementation notes (v1, PR #58)

Recorded per this document's own rule: divergences are decisions, and
decisions get written down.

1. **Hot turns restart the process per turn — measured, not assumed.**
   §8's residency handle and an earlier helper-file rotation idea were both
   struck by experiment (``tests/external/test_credential_rotation.py``,
   against the pinned CLI): the ``apiKeyHelper`` result is fetched once per
   process; ``CLAUDE_CODE_API_KEY_HELPER_TTL_MS`` unset, ``0`` and ``1``
   behave identically; and a 401 on a revoked key yields an error result
   with no refetch and no retry. There is **no in-process re-keying** in the
   pinned CLI's headless mode. What is resident is therefore the workspace,
   the state mount and the CP-fenced row — never a process — and each hot
   turn starts a fresh ``--resume`` process whose environment carries its
   own attempt's grant. Billing stays exactly per-attempt; an idle
   residency holds no live process and no live credential at all. The
   stdin multi-turn session machinery remains in-tree, unit-tested and
   unwired, for the day a residency-scoped credential (server-side
   per-call resolution) exists; the external test doubles as the tripwire
   that says when a CLI upgrade makes in-process rotation real.

2. **The MCP and model-switch exclusions are lifted — for the workspace.**
   Both were live-process-era constraints; under restart every resident turn
   carries a fresh environment, so MCP threads and model switches reuse the
   resident workspace like any other turn. With §14 the live process is
   back, and so are its gates, now scoped to the process alone: an MCP turn
   rides warm (the relay token is still per-attempt), and a model switch
   restarts — and re-keys the residency at promotion, because the grant is
   model-scoped. What still ends a residency outright is a harness switch
   (another CLI cannot read this session) and a base the tree cannot fetch.

3. **The first implemented resident rung was §2's WARM semantics, recorded
   as ``warm_native``, per turn.** Every resident
   turn skips clone, context patch and setup, resumes the native session in
   the preserved workspace, and pays one CLI start. Layer 4 (processes) was
   not continuous — the price of per-attempt credentials, paid until the
   §14 amendment made the residency itself the credential's subject. The
   restart rung remains, as the path for grantless residencies, model
   switches, MCP turns and live-process death. The idle cost of a residency
   is a workspace, a row — and with a grant, a live TTL-capped credential
   (§14); the budget's LRU pressure caps the workspaces.
4. **Contract-test coverage** (§10) lives in the unit and dbtest suites for
   every branch exercisable without a real CLI: fencing (4, 9, 10, 13),
   consistency (12), floor/TurnResult (2), fork refusal (6), model-switch
   restart (7), degradation branches (3, partial). The memory fixture (1)
   and cancel-against-a-real-process (5) need the pinned image and belong
   to the staging acceptance run before public exposure.

## 14. Amendment — D5 relaxed to thread-level attribution (2026-08-18, Murphy)

Per-attempt cost **attribution** was a declared invariant, not an accounting
necessity: every turn of a thread is the same user, the same account, the
same model, so total billing and quota enforcement are identical either way.
What per-attempt granularity bought — per-turn cost display, direct audit
lookup, a minutes-scale zombie cut-off — is worth less than what it blocked:
the live process, which the pinned CLI permits only under a
process-lifetime credential.

**Amended invariant:** inference attribution is per **thread** for resident
turns. Consequences, accepted explicitly:

- The residency grant lives for the residency: minted at establish
  (attributed to the thread), renewed on beats, revoked at release, lapse
  and takeover — generation rotation is credential rotation. An idle hot
  sandbox holds a live, TTL-capped credential (the original §8 v1
  concession, reinstated knowingly).
- Per-turn cost display, where wanted, is derived by bracketing
  ``api_logs`` timestamps with attempt start/end — a view, not a ledger
  change.
- The zombie cut-off becomes reaper-plus-revocation latency rather than
  renewal starvation; comparable magnitude, now explicit.

This unblocks true HOT with agent-side work only: §13's item 1 stands as
the record of *why* per-turn restart was built, and the live-input session
it kept in-tree is what this amendment finally wires in.

**Implemented (2026-08-18).** The shape that shipped, where it is stricter
than the text above:

- Establish mints the grant (attributed ``thread:<thread_id>``, scoped to
  the establishing turn's model) and returns it with the generation; the
  beat that extends a lease renews its grant; release, lapse and takeover
  revoke it. A mint or link failure degrades to a workspace-only residency
  — turns ride ``warm_native`` on their own per-attempt grants, never an
  outage.
- ``hot`` is recorded only when the process predates the turn. The turn
  that *spawns* the live process (over the resident workspace, on the
  residency credential) is ``warm_native``: same fresh-process semantics,
  honestly labelled. The next turn is hot.
- MCP turns do not ride the live process — the relay token in a resident
  environment would be the establishing attempt's, and stale mid-thread.
  They ride warm; lifting this needs a residency-lifetime relay token.
- A model switch restarts (D6) and re-keys: promotion drops the old
  generation and establishes a successor whose grant is scoped to the new
  model, so the thread is hot again from the following turn.
- A live session that fails its turn, loses its lease, or is evicted is
  closed at the same points the residency drops; a survivor without a
  residency to live in is closed rather than leaked.
