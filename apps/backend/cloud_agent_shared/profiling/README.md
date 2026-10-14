# Operation profiling

Application producers use `cloud_agent_shared.operation_profiling`. That module
is the stable import surface; its `finish` function closes the measurement and
queues its frozen record. The implementation lives in this package.

| Module | Responsibility |
| --- | --- |
| `schema.py` | Environment names, wire headers, limits, operation boundaries and process identity |
| `models.py` | Run/control descriptors, measurement state and span payloads |
| `runs.py` | JSON run validation and immutable catalogue manifests |
| `policy.py` | Runtime control snapshots, environment caches and per-run admission budgets |
| `measurement.py` | Root selection, attribution, timing, fragment accounting and record freezing |
| `stages.py` | Local stage observers and duration-only recorders |
| `context.py` | Validated selection handoffs, continuations and response headers |
| `export.py` | Bounded background queue, structured logs and optional JSONL delivery |
| `clock.py` | Shared clock access through the public API's replaceable `time` object |

The dependency direction is explicit: schema and models underpin run parsing;
policy uses parsed runs; measurement uses policy; observers and process contexts
use measurements. The exporter consumes serialized records and has no dependency
on policy or measurement state. Clock resolution happens only when collection or
export needs a timestamp, preserving deterministic clock replacement across all
modules.

## Behavior to preserve

- Collection defaults to off. An unselected root operation allocates no
  measurement and reads no clock.
- Control policy is published as one versioned snapshot; an older answer cannot
  restore a stopped experiment. Environment descriptors are parsed once per
  process.
- A measurement retains the run and target that selected it. Stopping a run ends
  new selection; its original deadline truncates in-flight collection.
- Root selection reserves one per-process budget slot. Discarding unselected
  work returns the slot, and continuing a parent's selection consumes no second
  slot. Continuous Admin runs have no cumulative sample cap.
- `bind` proves all configured target facts. A scoped measurement that was never
  bound produces no record.
- Only durations cross independent monotonic clock domains. Fragment call IDs
  distinguish retries, missing answers and truncated answers.
- Finalization freezes the end boundary and completeness once. Response
  metadata, persistence and export reuse the same frozen payload.
- Both log and JSONL writes run on the background thread. A full queue drops
  records and counts the drops; it never blocks the measured operation.

The existing collector, runtime-control and overhead tests exercise these
contracts, including fake-clock timing and real child-process continuations:

```bash
uv run pytest -q tests/unit/test_agent_operation_profiling.py \
  tests/unit/test_operation_profiling_runtime_control.py \
  tests/unit/test_operation_overhead_benchmark.py
```

Field definitions and operation boundaries remain in
`docs/agents/specs/2026-09-14-sandbox-startup-measurement-design.zh-CN.md` and
`docs/agents/specs/2026-09-14-platform-operation-measurement-inventory.zh-CN.md`.
