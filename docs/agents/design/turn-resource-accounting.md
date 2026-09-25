# Whole-turn resource accounting: `member_execution_window`

Status: implementation contract. This file is the single source of truth for
the interfaces used by the collector, the control plane, and the admin
dashboard. The product rationale, the rejected alternatives, and the full
acceptance matrix live in the design document
`2026-09-11-admin-turn-resource-accounting-design.zh.md`; this file fixes only
what code must agree on.

## 1. Scope

One user input is one `job`/`turn`. The stored unit is one **member
activation** — one attempt executing one resident member inside a shared Kata
sandbox.

The window is named `member_execution_window`:

| In scope | Out of scope |
| --- | --- |
| The resident agent's own CPU, memory and measurable I/O while the prompt runs | Queueing, image pull, VM start, repository preparation |
| Work threads and tool children under the member's UID | `supervisor`, guest observer, proxy, VM kernel, host QEMU/virtiofsd |
| Same-UID detached/double-forked children that exit before the window closes | Web terminal, platform git/snapshot/publish, post-turn cleanup |
| The increment of an already-resident same-member process during this window | Idle resource of a resident session between turns, remote model-serving GPU/CPU |

This is a member-execution resource bill. It must never be presented as the
platform cost of the task.

## 2. Wire payload: `POST /v1/agent/worker/jobs/{job_id}/turn-resources`

One independent endpoint. It never writes, replaces, or deletes the existing
tool-call and process rows, and it is not constrained by the guard that keeps
`shared-member-v1` free of top-level container totals.

The body is a **cumulative snapshot** of one measurement, not a batch of
deltas. Re-sending the same or an older `report_seq` must not accumulate.

```json
{
  "schema_version": 1,
  "measurement_id": "00000000-0000-4000-8000-000000000001",
  "report_seq": 9,
  "state": "final",
  "scope": "member_execution_window",
  "source": "kata-guest-member-window-v1",
  "window": {
    "duration_ms": 130000,
    "observed_through_ms": 130000,
    "open_scan_ms": 12,
    "close_scan_ms": 15
  },
  "cpu": {
    "status": "available",
    "method": "live_and_exit_task_counters",
    "usec": 80000000,
    "capacity_cores": 2,
    "reason_codes": []
  },
  "memory": {
    "status": "available",
    "method": "sampled_member_pss",
    "peak_bytes": 1288490189,
    "sample_interval_ms": 1000,
    "valid_samples": 131,
    "expected_samples": 131,
    "max_gap_ms": 1000,
    "reason_codes": []
  },
  "storage_io": {
    "status": "unavailable",
    "method": "task_io_accounting",
    "read_bytes": null,
    "write_bytes": null,
    "reason_codes": ["filesystem_attribution_unverified"]
  }
}
```

### 2.1 Field contract

| Field | Type | Rule |
| --- | --- | --- |
| `schema_version` | `1` | Exactly `1` in this revision |
| `measurement_id` | UUID | Fixed at the first accepted report for an attempt |
| `report_seq` | int >= 1 | Strictly monotonic per `measurement_id`; same seq with a different payload is a protocol conflict |
| `state` | `collecting` \| `final` | `final` can never be overwritten by `collecting` |
| `scope` | `member_execution_window` | Constant |
| `source` | `kata-guest-member-window-v1` | Constant; the capability string |
| `window.duration_ms` | int >= 0 | Guest-monotonic open-to-close span |
| `window.observed_through_ms` | int >= 0 | How far data actually reached; `<= duration_ms` |
| `window.open_scan_ms` / `close_scan_ms` | int >= 0 | Cost of the boundary scans |
| `report_seq` | int >= 1 | Cumulative-snapshot counter, assigned by the collector and stored with the snapshot. Strictly increasing per `measurement_id` |
| `cpu.usec` | int >= 0 or null | Threads' own scheduler time. Per thread instance, the difference between consecutive trusted readings of `se.sum_exec_runtime`, closed by that instance's settled exit. May exceed wall-clock duration |
| `cpu.capacity_cores` | positive finite number or absent | Sandbox CPU quota in cores captured at window open. Shared by the sandbox members; older snapshots omit it. Never filled from current host configuration |
| `memory.peak_bytes` | int >= 0 or null | `max` over complete PSS census cycles of the per-member address-space sum |
| `memory.sample_interval_ms` | int > 0 | Planned period |
| `memory.valid_samples` | int >= 0 | Cycles where every member address space read succeeded |
| `memory.expected_samples` | int >= 0 | Cycles the schedule intended, given `duration_ms` |
| `memory.max_gap_ms` | int >= 0 | Longest span between two valid cycles |
| `storage_io.read_bytes` / `write_bytes` | int >= 0 or null | Kernel task I/O accounting delta |

Every metric carries `status` and `reason_codes`. `status` is one of:

- `available` — complete **within the declared method and scope**. This never
  means "no sampling error", "no boundary error", or "covers all platform
  cost".
- `partial` — a measured lower bound for an additive quantity. Rendered as
  "observed"; only CPU and same-method I/O may use the `>=` form.
- `unavailable` — no usable number. The value field is `null`.

`NULL` and `0` are different states everywhere: a metric is `0` only when a
complete window proved a true zero, and `unavailable` otherwise.

### Average CPU usage in the admin execution summary

The admin execution summary displays average CPU usage as a share of the
sandbox CPU quota: `cpu.usec / (window.duration_ms * 1000 * cpu.capacity_cores)
* 100`. A 2-core sandbox with a member averaging one core displays 50%. The
numerator covers only that member and its tools, not sibling members or VM
and platform overhead. Waiting for a model remains part of the elapsed window.

Show a percentage only for a final window whose CPU status is `available`,
whose observation spans its duration, and whose positive duration and captured
capacity are known. Partial, interrupted, collecting, legacy, and contradictory
readings show `—`, never a guessed percentage or a zero. Values above 100% are
unavailable rather than clamped. No peak utilisation can be inferred from a
cumulative CPU counter. Cumulative CPU accounting remains available to other
consumers and the compact Runs list.

The optional capacity field is omitted during serialization when unknown, so
retries of legacy reports keep the same stored payload. Existing collectors
and stored records require no backfill or database migration. Detail responses
keep the timing fields under `window`; only the run-level aggregate is flat.

### 2.2 Reason codes

Bounded enum. The UI maps each to one fixed human sentence and never shows
UIDs, lease identifiers, kernel field names, or raw exception text.

```
missing_thread_baseline        counter_regressed
thread_identity_conflict       observer_restarted
ring_loss                      filesystem_attribution_unverified
pss_unreadable                 address_space_share_unverified
sampling_over_budget           window_prefix_retained
observer_unavailable           not_a_kata_guest
source_mismatch                estimates_diverged
```

`source_mismatch` is a run-level code: attempts measured by different
capabilities are never added into one total. `estimates_diverged` says the two
window estimates disagree by more than the reconciliation tolerance, so the
reported figure is the larger **floor** and not an exact number.

## 3. Storage

### 3.1 Migration `0007_attempt_turn_resources`

`down_revision = "0006_host_capacity"`: the migration chain was rewritten into
a single baseline (`0001`) plus additive revisions, so the numbered chain the
original design assumed does not exist on this line. This file has already
been reparented once — it was written as `0030` against `0005`, and the host
capacity work landed `0006` in between, which would have produced two heads.
Anything else that lands on this chain must reparent this revision rather than
assume its number is free; two Alembic heads, or a revision whose parent is
absent, fail at deploy rather than in the tests.
`tests/unit/test_schema_baseline.py` asserts exactly one head and no revision
revised twice.

Three nullable columns on `agent_attempt_resources`; old rows stay valid and
are never backfilled. A partial index over the rows that actually carry a
snapshot keeps the runs list from fetching the ones that do not.

| Column | Type | Meaning |
| --- | --- | --- |
| `turn_summary` | `JSONB` | Latest cumulative snapshot, canonical snake_case keys |
| `turn_summary_seq` | `BIGINT` | Guard for the monotonic `report_seq` |
| `turn_summary_received_at` | `TIMESTAMPTZ` | When the accepted snapshot arrived |

Compatibility rules, in both directions:

- The legacy writer (`record_attempt_resources`) must not touch these three
  columns.
- The turn writer must not touch or delete the legacy columns or the
  `agent_tool_call_resources` / `agent_process_observations` child rows.
- When the turn writer runs first it creates the row with a legacy-compatible
  base: `sample_count = 0`, `coverage = 0`, `sampler_version =
  'shared-member-v1'`, all aggregate counters `NULL`. That base is the honest
  existing statement "no container totals were attributed to this member", not
  a fabricated measurement.

`turn_summary` is stored with canonical snake_case keys, and the worker's post
body uses the same spelling, so no renaming layer sits between the collector
and the stored row. Case conversion happens once, on the frontend boundary,
when the admin API's JSON is adapted for display.

### 3.2 Write rules

The insert/update runs in one transaction whose fence predicate is evaluated
against the **locked** attempt row:

```sql
SELECT id, job_id FROM agent_attempts
WHERE id = $1 AND lease_generation = $2 AND status = 'running'
  AND lease_expires_at > NOW()
FOR UPDATE
```

A missing or terminal row returns `False`; the API answers `409 lease_lost`
exactly as the legacy resource route does.

On conflict:

- accept when `turn_summary_seq IS NULL`, or the incoming `report_seq` is
  greater, or it is equal **and** the payload is byte-identical;
- reject with a conflict result when the incoming `report_seq` is smaller, or
  equal with a different payload;
- never accept `state = 'collecting'` over a stored `state = 'final'`.

After the first accepted report the attempt's `measurement_id` is fixed; a
different UUID cannot take over the stored summary.

### 3.3 Derived read state

Reads never mutate. A stored snapshot whose `state` is `collecting` while the
attempt is already terminal is presented as `interrupted`, with
`observed_through_ms` surfaced so the UI can say how far it actually got.

## 4. Read model

### 4.1 Two reducers, one per direction

They are deliberately separate, because they answer different questions and
merging them would force one to pretend the other's inputs exist.

| Module | Function | Reads | Answers |
| --- | --- | --- | --- |
| `cloud_agent_host/member_resources.py` | `reduce_window()` | the guest's live censuses and the kernel's settled exits | what did this member consume during this activation |
| `cloud_agent_host/turn_resources.py` | `summarize_turn_resources()` / `summarize_run_resources()` | one or more *stored* snapshots | what should a reader be shown, and how complete is it |

`summarize_turn_resources()` is a pure function shared by the list projection,
the detail view, and any later aggregation. It takes the stored summary (or
`None`) plus the attempt's terminal state and returns the response projection
below. It never reads a clock, never touches the database, and degrades a
malformed snapshot to "no summary" rather than raising, because a corrupt row
must not be able to fail an admin page.

### 4.2 Response models

`AttemptResources` gains one nested field, `turn_summary`, projected
explicitly (see the hazard in 4.4). Shape:

```
TurnResourceSummary
  measurement_id / state (collecting|final|interrupted) / observed_through_ms
  duration_ms / observed_through_ms
  cpu:        status, method, usec, reason_codes[]
  memory:     status, method, peak_bytes, sample_interval_ms,
              valid_samples, expected_samples, max_gap_ms, reason_codes[]
  storage_io: status, method, read_bytes, write_bytes, reason_codes[]
```

`AgentRunSummary` gains a flat scalar `resource_summary` for the Runs list:
CPU usec, memory peak bytes, a per-metric status triple, and the observation
cut-off. It holds no lists, so listing 50 runs stays one query over the
current page.

### 4.3 Multi-attempt runs

- CPU and same-method I/O: sum the measured parts; a missing attempt
  contributes `partial`.
- Memory: report the **largest single activation peak**. Peaks are never
  summed and never presented as a simultaneous platform peak.
- Summaries from different `source` values or schema versions are not mixed
  into one total.

Per-metric status stays independent: an unsupported I/O metric must not mark
a complete CPU measurement unavailable, and a valid CPU measurement must not
promote the whole turn to "complete".

### 4.4 SQL projection hazard

`cloud_agent/storage/admin_queries.py` builds its column list from
`AttemptResources.model_fields`. Adding a *scalar* field there without a
matching physical column breaks the endpoint at request time. `turn_summary`
is not a physical column: it must be projected from `r.turn_summary` as JSONB
and explicitly excluded from the generated scalar list.

## 5. Collection contract

### 5.1 Identity

A business window is bound to
`attempt_id + lease_generation + sandbox_id + sandbox_generation +
member_slot + activation_generation`. These produce one `measurement_id`.

A thread instance is bound to `guest_boot_id + start_boottime_ns + tid`, and
carries `tgid` and the member UID. A PID alone is never an identity, and guest
proc jiffies are never treated as BPF nanoseconds without conversion. UID and
slot reuse always open a new window.

### 5.2 Window lifecycle

```text
member turn lock held, resident ready
  -> open scan: register the member UID, take the live thread baseline, take
     the first PSS census
  -> prompt sent
  -> guest samples once per second; the broker posts a cumulative checkpoint
     at most once every 15 seconds
  -> normal end / error / cancel: close scan, freeze the summary
  -> existing member cleanup and settlement
  -> runner reports final while the lease is still live, then finishes
```

Open and close scans are bounded; a failure yields a missing metric, never a
failed turn and never a longer execution timeout. The prompt is sent only
after the open scan completes, and cleanup runs only after the close scan.

Windows for one member are serialized. Fork siblings use their own UID and
their own window.

### 5.3 Counter de-duplication

Per stable thread instance the reducer keeps the last trusted cumulative
value and counts only differences. The census therefore carries one compact
entry per live task — identity plus counters — because a member total that
moves can be moved by a child arriving as easily as by work happening, and a
reducer given only totals cannot tell those apart.

- A thread alive before the window is baselined by the open scan.
- A thread born inside the window is zero-baselined only when the fork
  identity proves it started at zero; otherwise it has no baseline.
- An exit event after a live snapshot contributes only the delta from the last
  snapshot; the full lifetime is never added again.
- A thread that is born and dies between two samples is counted from its exit
  record, so a one-second period does not lose sub-second CPU work.
- A thread still alive at close is closed by the close-scan value; its future
  consumption belongs to no earlier window.
- A missing birth proof, a cross-window trailing event, an identity conflict,
  ring loss, or a counter regression marks the affected part partial. A
  missing baseline is never repaired with zero.

### 5.4 Memory

```text
member_pss(tick)      = sum of PSS over the member's distinct address spaces
memory_pss_peak_bytes = max(complete and valid member_pss(tick))
```

PSS is a share of mapped resident memory, not machine memory. It excludes the
VM kernel, unmapped page cache, host services, and special page classes. RSS
is never substituted. A cycle where one process read failed is not a complete
cycle: it is excluded from the peak and reported through `valid_samples`.
Because sampling cannot see between ticks, a complete plan is still labelled
`sampled`.

### 5.5 Budgets

| Limit | Value |
| --- | --- |
| Tracked work threads per guest | 4096 |
| Distinct thread instances per window | 100000 |
| One census cycle | 200 ms target; over budget skips and marks missing |
| Boundary scan addition to a turn | 500 ms maximum |
| New summary payload | UTF-8 <= 16 KiB |
| Running checkpoint | at most one per 15 s, latest cumulative value only |

A sampling failure never blocks `finish` and never changes a task's
success/failure semantics.

## 5.6 Where each piece lives

| Concern | Module | Notes |
| --- | --- | --- |
| One member window's numbers | `cloud_agent_host/member_resources.py` | `MemberWindowReducer`, `MemberWindowSampler`, `reduce_window()` |
| One census record | `guest-observer.c` `journal_window_sample()` | compact per-task entry plus the member totals |
| Asking for a census | `deploy/sandbox/member-window.c` | the fixed `register`/`sample`/`close`/`probe` verbs |
| Reading a stored snapshot | `cloud_agent_host/turn_resources.py` | the list, the detail view and any aggregation |
| Reporting it | `runner.ControlPlane.report_turn_resources()` | one endpoint, snapshot bodies |

## 5.7 What a real guest still has to prove

Nothing below can be established on a machine without a Kata guest. The
preflight `--self-test` answers all of it on the target image, and the
implementation ships the probe rather than assuming the answer:

- `/proc/<tid>/schedstat` field 0 is the same quantity `se.sum_exec_runtime`
  holds for a task that has just died. If it is not, live and exit counters
  are not comparable and the CPU metric must be reported unavailable.
- `/proc/<tid>/status` exposes `Tgid`, and every thread of one group shares
  one `smaps_rollup` entry, so a group is summed once.
- `/proc/<tid>/stat` field 22 is a clock-tick start time, and `USER_HZ` is
  100, so the tick-to-nanosecond conversion is exact.
- reading another UID's `/proc` entries succeeds for the guest-root helper
  with its reviewed capability set.
- `smaps_rollup` puts `Pss:` inside the first 16 KiB, which the bounded read
  depends on.
- the sandbox's workspace filesystem attributes task I/O to the member.

Until that last one is proven on the filesystem a deployment actually serves,
`storage_io` is reported `unavailable` with
`filesystem_attribution_unverified`, and the guest record does not carry
per-task read/write bytes at all: two zeroes cannot be told apart between "the
member wrote nothing" and "this filesystem never charged the member", so
shipping figures the platform refuses to act on would be payload without a
reader. The member totals still carry the cycle's I/O counters, so the metric
can be enabled without changing the record's shape.

## 6. What is not in this change

No per-attempt cgroup, no moving resident processes, no cgroupfs for tasks, no
host `/proc` reads from the runner container, no per-tool sampler, no
per-second database table, no network attribution metric, no resource billing,
no alerting, no cross-user aggregation, and no owner-facing UI for the new
summary. Low CPU is not evidence of waiting on the model, so no "model wait
time" is derived.
