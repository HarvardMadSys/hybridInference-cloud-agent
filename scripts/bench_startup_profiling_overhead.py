#!/usr/bin/env python3
"""Measure what the opt-in operation profiler costs one sandbox startup.

``cloud_agent_host/operation_profiling.py`` is default-off timing for platform
operations, and the whole design rests on one claim: leaving the switch in the
tree is cheap enough that nobody ever notices it.  This script measures that
claim against the call pattern the runner actually uses, instead of leaving it
as an assertion in a docstring.

What is measured
----------------

One *simulated* startup attempt, shaped after ``run_once`` and
``_run_shared_live_attempt`` in ``cloud_agent_host/runner.py``:

* one ``start_operation(operation="attempt.startup", host=..., worker_id=...)``,
* one ``span`` around a stand-in for the attestation and claim call,
* twelve ``mark`` calls naming the real stage boundaries, in runner order,
* eight attributes in three ``set_attributes`` calls (runner makes one
  six-attribute call; the two extra single-fact calls carry ``sandbox_id`` and
  ``runtime``, the facts it supplies through ``bind`` once the attempt is
  claimed), and
* one ``finish(outcome="dispatched")``.

Every cohort runs that same sequence, and each attempt is timed end to end with
``time.perf_counter_ns()``.  Only the profiler's configuration differs:

``bare``
    The sequence written inline with plain locals -- a list of stage names and a
    facts dict -- and no profiler calls at all.  This is the
    no-instrumentation floor.  It reads no clock, so it is a genuine lower
    bound rather than a hand-rolled profiler.

``off``
    The real calls with profiling disabled: ``start_operation`` returns ``None``
    and every other call is a no-op.

``on``
    Profiling enabled through the environment, with a freshly generated
    ``expires_at`` fifteen minutes out and ``max_operations`` sized for the run,
    and **no** ``AGENT_OPERATION_PROFILING_SINK``.

``onsink``
    Same as ``on``, plus ``AGENT_OPERATION_PROFILING_SINK`` pointing at a
    temporary JSONL file.  Records are enqueued to the exporter thread, never
    written on the attempt thread.

What this does and does not measure
-----------------------------------

It measures the profiler's *own* cost on a synthetic attempt sequence: the
guards, the clock reads, the record building, the JSON serialization and the
sink enqueue.  It is **not** an end-to-end sandbox startup benchmark.  There is
no claim, no container, no supervisor, no agent CLI and no network here, so the
numbers say what fraction of a startup the instrumentation adds, never how long
a startup takes.

Deliberate limits, all of which make ``on``/``onsink`` mildly conservative:

* The real attempt is milliseconds-to-seconds long; this one is microseconds.
  A fixed per-attempt overhead is a much larger share of a synthetic attempt
  than of a real one, which is exactly why the per-attempt cost is the
  headline number and the ratio is not.
* ``runner.py`` guards its one six-attribute call behind
  ``if measurement is not None:``, so the shipped disabled path does *less*
  than the ``off`` cohort here, which calls every entry point unconditionally.
* ``bind`` -- one further profiler call that only the enabled path makes -- is
  not modeled, so ``on`` and ``onsink`` are a slight undercount.
* Logging is left at the interpreter default.  ``_emit`` builds and serializes
  the record either way, but with no handler configured at ``INFO`` the log line
  is never formatted or written, so a deployment that logs at ``INFO`` adds the
  cost of one line per attempt on top of these numbers.
* Each measured sample includes two ``perf_counter_ns`` calls, present in every
  cohort.
* Cohorts run in sequence in one process, so CPU drift is not separated from
  the cohort effect.  Rerun with a different ``--iterations`` to sanity-check.

Examples::

    python scripts/bench_startup_profiling_overhead.py --iterations 20000
    python scripts/bench_startup_profiling_overhead.py --mode nosink
    python scripts/bench_startup_profiling_overhead.py --json /tmp/profiling.json
"""

from __future__ import annotations

import argparse
import json
import os
import platform
import statistics
import sys
import tempfile
import time
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from cloud_agent_host import operation_profiling
from cloud_agent_host.operation_profiling import (
    ENV_PROFILING_ENABLED,
    ENV_PROFILING_RUN,
    ENV_PROFILING_SINK,
)

#: Mirrors ``cloud_agent_host.runner.STARTUP_OPERATION``.  The runner module is
#: not imported on purpose: it pulls in Docker, the runtimes and the control
#: plane, none of which this measurement needs.
STARTUP_OPERATION = "attempt.startup"

HOST = "bench-host"
WORKER_ID = "bench-worker"
SANDBOX_ID = "asbx_00000000000000000000000000000000"
RUNTIME_ID = "claude-code"
CONTAINER_ID = "0" * 64

#: The stage boundaries ``_run_shared_live_attempt`` marks, in runner order.
#: The suspend/resume branches it takes per startup are collapsed to the ones a
#: warm retained container hits.
STAGE_MARKS: tuple[str, ...] = (
    "validate",
    "reserve",
    "supervisor_acquire",
    "sandbox_running",
    "workspace_reset",
    "checkout",
    "setup",
    "runtime_preflight",
    "agent_network",
    "resident_start",
    "prompt_admission",
    "prompt_dispatch",
)

#: Marks recorded before the attribute block, matching where runner.py attaches
#: its post-resident facts (line 1929, between ``resident_start`` and
#: ``prompt_admission``).
MARKS_BEFORE_ATTRIBUTES = 10

#: Kept identical between the ``bare`` and instrumented attempts so the
#: comparison isolates the profiler and not the payload.
ATTRIBUTE_VALUES: dict[str, object] = {
    "context_status": "restored",
    "resident_action": "reuse",
    "startup_path": "reuse_live",
    "process_generation": 3,
    "container_id": CONTAINER_ID,
    "workspace_action": "reuse",
}

#: The two facts the runner supplies through ``bind`` after the claim.
POST_CLAIM_ATTRIBUTES: tuple[tuple[str, object], ...] = (
    ("sandbox_id", SANDBOX_ID),
    ("runtime", RUNTIME_ID),
)

#: Which cohorts each ``--mode`` selects.  ``bare`` is in every mode because
#: every delta in the report is relative to it.
MODE_COHORTS: dict[str, tuple[str, ...]] = {
    "all": ("bare", "off", "on", "onsink"),
    "off": ("bare", "off"),
    "on": ("bare", "on", "onsink"),
    "nosink": ("bare", "on"),
}

COHORT_DESCRIPTIONS: dict[str, str] = {
    "bare": "same sequence inline, no profiler calls (floor)",
    "off": "real calls, profiling disabled",
    "on": "profiling enabled, no sink",
    "onsink": "profiling enabled, JSONL sink configured",
}

#: Headroom over ``warmup + iterations`` so a run can never quietly run out of
#: budget mid-cohort and degrade into the ``off`` cost.  ``_verify_selected``
#: fails loudly if it ever does.
BUDGET_SLACK = 1000

#: How far ahead the generated run descriptor expires.
RUN_LIFETIME = timedelta(minutes=15)


def _claim_like_work() -> dict[str, object]:
    """Stand in for the attestation plus claim call inside the claim span.

    A real claim is a network round trip; this has to stay negligible so the
    measurement is the profiler and not the stand-in.  It is a function call so
    no interpreter can fold it away.
    """
    return {"attested": True, "queued": 0}


def bare_attempt() -> None:
    """One startup-shaped pass with plain locals and no profiler calls.

    No clock is read anywhere in here, which makes this a floor rather than a
    hand-rolled profiler: the deltas reported against it therefore include the
    profiler's own clock reads.
    """
    marks: list[str] = []
    facts: dict[str, object] = {}
    _claim_like_work()
    for name in STAGE_MARKS[:MARKS_BEFORE_ATTRIBUTES]:
        marks.append(name)
    facts.update(ATTRIBUTE_VALUES)
    for key, value in POST_CLAIM_ATTRIBUTES:
        facts[key] = value
    for name in STAGE_MARKS[MARKS_BEFORE_ATTRIBUTES:]:
        marks.append(name)


def instrumented_attempt() -> None:
    """One startup-shaped pass through the real profiler entry points.

    The same function serves the ``off``, ``on`` and ``onsink`` cohorts: the
    calls are identical, and only the environment decides whether
    ``start_operation`` returns a measurement or ``None``.
    """
    measurement = operation_profiling.start_operation(
        operation=STARTUP_OPERATION,
        host=HOST,
        worker_id=WORKER_ID,
    )
    with operation_profiling.span(measurement, "claim"):
        _claim_like_work()
    operation_profiling.mark(measurement, "validate")
    operation_profiling.mark(measurement, "reserve")
    operation_profiling.mark(measurement, "supervisor_acquire")
    operation_profiling.mark(measurement, "sandbox_running")
    operation_profiling.mark(measurement, "workspace_reset")
    operation_profiling.mark(measurement, "checkout")
    operation_profiling.mark(measurement, "setup")
    operation_profiling.mark(measurement, "runtime_preflight")
    operation_profiling.mark(measurement, "agent_network")
    operation_profiling.mark(measurement, "resident_start")
    operation_profiling.set_attributes(
        measurement,
        context_status="restored",
        resident_action="reuse",
        startup_path="reuse_live",
        process_generation=3,
        container_id=CONTAINER_ID,
        workspace_action="reuse",
    )
    operation_profiling.set_attributes(measurement, sandbox_id=SANDBOX_ID)
    operation_profiling.set_attributes(measurement, runtime=RUNTIME_ID)
    operation_profiling.mark(measurement, "prompt_admission")
    operation_profiling.mark(measurement, "prompt_dispatch")
    operation_profiling.finish(measurement, outcome="dispatched")


#: Every cohort runs the same body; only the environment differs.
ATTEMPTS = {
    "bare": bare_attempt,
    "off": instrumented_attempt,
    "on": instrumented_attempt,
    "onsink": instrumented_attempt,
}


class Sample:
    """The per-attempt timings of one cohort, in nanoseconds."""

    def __init__(self, name: str, description: str, values_ns: list[int]) -> None:
        self.name = name
        self.description = description
        self.values_ns = sorted(values_ns)

    def percentile_us(self, fraction: float) -> float:
        """Return a nearest-rank percentile in microseconds."""
        if not self.values_ns:
            raise ValueError("no samples")
        index = min(len(self.values_ns) - 1, max(0, round(fraction * (len(self.values_ns) - 1))))
        return self.values_ns[index] / 1000.0

    def mean_us(self) -> float:
        """Return the arithmetic mean in microseconds."""
        return statistics.fmean(self.values_ns) / 1000.0

    def as_dict(self) -> dict[str, float | int | str]:
        """Return the summary row for this cohort."""
        return {
            "cohort": self.name,
            "description": self.description,
            "n": len(self.values_ns),
            "p50_us": round(self.percentile_us(0.50), 3),
            "p90_us": round(self.percentile_us(0.90), 3),
            "p95_us": round(self.percentile_us(0.95), 3),
            "mean_us": round(self.mean_us(), 3),
            "min_us": round(self.values_ns[0] / 1000.0, 3),
            "max_us": round(self.values_ns[-1] / 1000.0, 3),
        }


def run_descriptor(max_operations: int) -> str:
    """Build a live run descriptor that expires fifteen minutes from now."""
    return json.dumps(
        {
            "run_id": f"bench-{uuid.uuid4().hex[:12]}",
            "operations": [STARTUP_OPERATION],
            "expires_at": (datetime.now(timezone.utc) + RUN_LIFETIME).isoformat(),
            "max_operations": max_operations,
        },
        separators=(",", ":"),
    )


def install_environment(cohort: str, sink: Path | None, max_operations: int) -> None:
    """Point the profiler at this cohort's configuration and drop its cache."""
    for name in (ENV_PROFILING_ENABLED, ENV_PROFILING_RUN, ENV_PROFILING_SINK):
        os.environ.pop(name, None)
    if cohort in {"on", "onsink"}:
        os.environ[ENV_PROFILING_ENABLED] = "1"
        os.environ[ENV_PROFILING_RUN] = run_descriptor(max_operations)
    if cohort == "onsink":
        if sink is None:
            raise SystemExit("the onsink cohort needs a sink path")
        os.environ[ENV_PROFILING_SINK] = str(sink)
    # The switch state is cached per process, so every cohort has to clear it.
    operation_profiling.reset_cache()


def clear_environment() -> None:
    """Leave the process as it was found."""
    for name in (ENV_PROFILING_ENABLED, ENV_PROFILING_RUN, ENV_PROFILING_SINK):
        os.environ.pop(name, None)
    operation_profiling.reset_cache()


def verify_selected(cohort: str) -> None:
    """Fail loudly if the run stopped selecting attempts part-way through.

    Without this, a descriptor that expired or ran out of budget would silently
    turn the ``on`` and ``onsink`` cohorts into a second measurement of ``off``
    and the table would look perfectly reasonable.
    """
    if cohort not in {"on", "onsink"}:
        return
    probe = operation_profiling.start_operation(
        operation=STARTUP_OPERATION,
        host=HOST,
        worker_id=WORKER_ID,
    )
    if probe is None:
        raise SystemExit(
            f"{cohort}: the profiling run stopped selecting attempts "
            "(expired, or max_operations was exhausted), so this cohort "
            "measured the disabled path"
        )
    operation_profiling.discard(probe)


def timed(attempt, iterations: int, warmup: int) -> list[int]:
    """Time ``iterations`` whole attempts after ``warmup`` untimed ones."""
    for _ in range(warmup):
        attempt()
    samples: list[int] = []
    for _ in range(iterations):
        started = time.perf_counter_ns()
        attempt()
        samples.append(time.perf_counter_ns() - started)
    return samples


def drain_sink(path: Path, expected: int) -> dict[str, object]:
    """Stop the exporter and report what actually reached the JSONL file.

    A shortfall is not a benchmarking error: the sink queue is bounded on
    purpose and drops when it cannot keep up.  ``offer`` never blocks, so drops
    do not touch the per-attempt timings -- they are reported because a sink
    that cannot keep up is itself a result.
    """
    operation_profiling.flush_sink()
    operation_profiling.close_sink()
    written = 0
    if path.exists():
        with path.open(encoding="utf-8") as handle:
            for line in handle:
                if line.strip():
                    written += 1
    size = path.stat().st_size if path.exists() else 0
    return {
        "path": str(path),
        "records_expected": expected,
        "records_written": written,
        "records_dropped": max(0, expected - written),
        "bytes": size,
        "bytes_per_record": round(size / written, 1) if written else 0.0,
    }


def deltas(samples: dict[str, Sample], baseline: str = "bare") -> dict[str, dict[str, float]]:
    """Return each cohort's cost over the baseline, in microseconds."""
    if baseline not in samples:
        return {}
    base = samples[baseline]
    rows: dict[str, dict[str, float]] = {}
    for name, sample in samples.items():
        if name == baseline:
            continue
        base_mean = base.mean_us()
        rows[name] = {
            "p50_us": round(sample.percentile_us(0.50) - base.percentile_us(0.50), 3),
            "p90_us": round(sample.percentile_us(0.90) - base.percentile_us(0.90), 3),
            "p95_us": round(sample.percentile_us(0.95) - base.percentile_us(0.95), 3),
            "mean_us": round(sample.mean_us() - base_mean, 3),
            "mean_ratio": round(sample.mean_us() / base_mean, 3) if base_mean else 0.0,
        }
    return rows


def build_payload(
    *,
    mode: str,
    samples: dict[str, Sample],
    iterations: int,
    warmup: int,
    budget: int,
    sink: dict[str, object] | None,
) -> dict[str, object]:
    """Assemble the machine-readable report, caveats included."""
    return {
        "what": "the operation profiler's own cost on one synthetic attempt.startup sequence",
        "not_what": (
            "end-to-end sandbox startup: no claim, container, supervisor, agent CLI "
            "or network is involved, so these are per-attempt instrumentation costs "
            "and not startup latencies"
        ),
        "caveats": [
            "the six-attribute set_attributes call is made unconditionally here; "
            "runner.py guards its own behind `if measurement is not None`, so the "
            "shipped disabled path is cheaper than the `off` cohort",
            "bind() is not modeled, so `on` and `onsink` undercount by one call",
            "logging is left unconfigured: the record is built and serialized either "
            "way, but no INFO handler formats or writes the log line",
            "each sample includes two perf_counter_ns calls, in every cohort",
            "cohorts run in sequence in one process; CPU drift is not separated "
            "from the cohort effect",
            "the sink cohort drops most records because a synthetic attempt finishes "
            "every ~25 us (~40,000 records/s); a real startup produces one record, "
            "which the exporter keeps up with. Drops never block, so `onsink - on` is "
            "the enqueue path, not disk latency -- the write is on the exporter thread",
        ],
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "python": platform.python_version(),
        "platform": platform.platform(),
        "clock": "time.perf_counter_ns",
        "mode": mode,
        "iterations": iterations,
        "warmup": warmup,
        "max_operations": budget,
        "attempt_shape": {
            "start_operation": 1,
            "spans": 1,
            "marks": len(STAGE_MARKS),
            "set_attributes_calls": 3,
            "attributes": len(ATTRIBUTE_VALUES) + len(POST_CLAIM_ATTRIBUTES),
            "finish": 1,
        },
        "stage_marks": list(STAGE_MARKS),
        "baseline": "bare",
        "cohorts": {name: sample.as_dict() for name, sample in samples.items()},
        "deltas_vs_bare_us": deltas(samples),
        "sink": sink,
        "raw_samples_ns": {name: sample.values_ns for name, sample in samples.items()},
    }


def report(payload: dict[str, object]) -> None:
    """Print the table a human reads."""
    rows = list(payload["cohorts"].values())
    print(
        f"\nmode: {payload['mode']}   cohorts: {', '.join(row['cohort'] for row in rows)}"
        f"   iterations: {payload['iterations']}   warmup: {payload['warmup']}"
    )
    print(f"python {payload['python']} on {payload['platform']}")
    print()
    print(
        f"{'cohort':<8} {'n':>7} {'p50':>9} {'p90':>9} {'p95':>9} {'mean':>9} "
        f"{'min':>9} {'max':>9}  description"
    )
    for row in rows:
        print(
            f"{row['cohort']:<8} {row['n']:>7} {row['p50_us']:>9.3f} {row['p90_us']:>9.3f} "
            f"{row['p95_us']:>9.3f} {row['mean_us']:>9.3f} {row['min_us']:>9.3f} "
            f"{row['max_us']:>9.3f}  {row['description']}"
        )
    print("(all values in microseconds per simulated startup attempt)")
    print(
        f"\nattempt shape: {payload['attempt_shape']['start_operation']} start_operation, "
        f"{payload['attempt_shape']['spans']} span, {payload['attempt_shape']['marks']} marks, "
        f"{payload['attempt_shape']['attributes']} attributes in "
        f"{payload['attempt_shape']['set_attributes_calls']} calls, 1 finish"
    )

    rows = payload["deltas_vs_bare_us"]
    if rows:
        print("\ndelta vs `bare`, microseconds per attempt")
        print(f"{'cohort':<8} {'p50':>9} {'p90':>9} {'p95':>9} {'mean':>9} {'mean/bare':>10}")
        for name, row in rows.items():
            print(
                f"{name:<8} {row['p50_us']:>9.3f} {row['p90_us']:>9.3f} {row['p95_us']:>9.3f} "
                f"{row['mean_us']:>9.3f} {row['mean_ratio']:>9.2f}x"
            )
        for label in ("on", "onsink"):
            if label in rows:
                print(
                    f"  (`{label}` adds {rows[label]['mean_us']:.1f} us of attempt-thread CPU"
                    f" per attempt: {rows[label]['mean_us'] * 10_000 / 1e6:.2f} s across a"
                    " 10,000-operation run)"
                )
                break

    sink = payload["sink"]
    if isinstance(sink, dict):
        print(
            f"\nsink: {sink['records_written']} of {sink['records_expected']} records written"
            f" ({sink['records_dropped']} dropped), {sink['bytes'] / 1024:.0f} KiB, "
            f"{sink['bytes_per_record']:.0f} bytes/record"
        )
        if sink["records_dropped"]:
            print(
                "  every record is dropped here because a synthetic attempt finishes every\n"
                "  ~25 us (~40,000 records/s); a real startup produces one record, which the\n"
                "  exporter keeps up with trivially.  Drops never block the attempt thread,\n"
                "  so the `onsink` timings stand -- but read `onsink - on` as the enqueue\n"
                "  path (env lookup, lock, queue hand-off), not as disk latency: the write\n"
                "  itself happens on the exporter thread by design."
            )

    print(
        "\nWhat this measures: the profiler's own cost on a synthetic attempt.startup\n"
        "sequence -- guards, clock reads, record building, JSON serialization, sink\n"
        "enqueue.  It does NOT measure end-to-end sandbox startup: there is no claim,\n"
        "container, supervisor, agent CLI or network here, so treat the deltas as the\n"
        "instrumentation share of a real attempt, never as a startup latency.\n"
        "`bare` reads no clock and calls nothing, so it is a floor, not a profiler.\n"
        "runner.py guards its one six-attribute call behind `if measurement is not\n"
        "None:`, which makes the real disabled path cheaper than the `off` cohort;\n"
        "bind() is not modeled, which slightly undercounts `on` and `onsink`."
    )


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=__doc__ or "", formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument(
        "--iterations",
        type=int,
        default=20000,
        help="timed attempts per cohort (default: 20000)",
    )
    parser.add_argument(
        "--warmup",
        type=int,
        default=200,
        help=(
            "untimed attempts before each cohort, to warm the heap and the cached "
            "env state so the first cohort is not charged for the cold interpreter "
            "(default: 200)"
        ),
    )
    parser.add_argument(
        "--mode",
        choices=("all", "off", "on", "nosink"),
        default="all",
        help=(
            "cohort selection: 'all' is bare+off+on+onsink, 'off' is bare+off, "
            "'on' is bare+on+onsink, 'nosink' is bare+on (profiling enabled, no "
            "sink configured). 'bare' is always measured because every delta is "
            "relative to it (default: all)"
        ),
    )
    parser.add_argument(
        "--json",
        metavar="PATH",
        help="also write the summary and the raw per-attempt nanosecond samples as JSON",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    if args.iterations < 1:
        print("--iterations must be at least 1", file=sys.stderr)
        return 2
    if args.warmup < 0:
        print("--warmup must not be negative", file=sys.stderr)
        return 2

    cohorts = MODE_COHORTS[args.mode]
    budget = args.warmup + args.iterations + BUDGET_SLACK
    samples: dict[str, Sample] = {}
    sink_meta: dict[str, object] | None = None

    with tempfile.TemporaryDirectory(prefix="startup-profiling-bench-") as directory:
        sink_path = Path(directory) / "operation-profiling.jsonl"
        try:
            for cohort in cohorts:
                install_environment(cohort, sink_path, budget)
                values = timed(ATTEMPTS[cohort], args.iterations, args.warmup)
                verify_selected(cohort)
                samples[cohort] = Sample(cohort, COHORT_DESCRIPTIONS[cohort], values)
                if cohort == "onsink":
                    sink_meta = drain_sink(sink_path, args.warmup + args.iterations)
        finally:
            clear_environment()

    payload = build_payload(
        mode=args.mode,
        samples=samples,
        iterations=args.iterations,
        warmup=args.warmup,
        budget=budget,
        sink=sink_meta,
    )
    report(payload)
    if args.json:
        destination = Path(args.json)
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
        print(f"\nwrote {destination}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
