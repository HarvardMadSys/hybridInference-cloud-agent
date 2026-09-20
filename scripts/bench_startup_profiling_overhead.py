#!/usr/bin/env python3
"""Measure what the opt-in operation profiler costs one sandbox startup.

``apps/backend/cloud_agent_profiling/operation_profiling.py`` is default-off timing for
platform operations, and the whole design rests on one claim: leaving the switch
in the tree is cheap enough that nobody ever notices it.  This script measures
that claim against the call pattern the runner actually uses, instead of leaving
it as an assertion in a docstring.

This is a **collector** benchmark, and it is deliberately not the whole
overhead story the design requires. It measures the instrumentation's own cost
on a synthetic attempt sequence -- see the limits section below -- and the
business-overhead experiment that drives the real routes, the real database and
the four real startup paths is a separate script,
``scripts/bench_operation_overhead.py``.

What is measured
----------------

One *simulated* startup attempt, shaped after ``run_once`` and
``_run_shared_live_attempt`` in ``apps/backend/cloud_agent_host/runner.py``:

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

``onlog``
    Same as ``on``, plus a deliberately slow ``INFO`` handler attached to the
    ``cloud_agent_profiling.operation_profiling`` logger for the duration of the
    measurement (``--log-delay-ms``, default 1.0).  One record per attempt at
    that delay would cost a millisecond *on whichever thread formats it* --
    roughly 40x the ~25 us attempt being measured -- so if any part of the log
    path still ran on the attempt thread the effect would be impossible to miss.
    The comparison that matters is ``onlog`` vs ``on``.

What this does and does not measure
-----------------------------------

It measures the profiler's *own* cost on a synthetic attempt sequence: the
guards, the clock reads, the record building, the JSON serialization and the
sink enqueue.  It is **not** an end-to-end sandbox startup benchmark.  There is
no claim, no container, no supervisor, no agent CLI and no network here, so the
numbers say what fraction of a startup the instrumentation adds, never how long
a startup takes.

The ``onlog`` cohort is why ``_Exporter`` exists.  Both destinations -- the log
line and the JSONL sink write -- go through the one bounded queue drained by the
exporter thread, so a handler that blocks on a socket, a full disk or a
synchronous shipper is paid there and not on the path a user waits on.  Attaching
such a handler and measuring ``onlog - on`` is the direct test of that claim: a
synchronous handler would show up as ``--log-delay-ms`` per attempt, one that
runs on the exporter thread should show up as nothing at all.

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
* ``bare``, ``off``, ``on`` and ``onsink`` run with **no** handler attached, so
  they measure the record building and the enqueue, not the formatting.  Only
  ``onlog`` formats a line, and it does so through the queue.
* ``onlog`` proves the log path runs and that the attempt thread does not pay
  for it; it is not a logging-throughput benchmark.  At a synthetic ~25 us per
  attempt the exporter queue overflows and drops most records, so the handler
  receives a sample -- enough to show the line was really produced, not a count
  of every attempt.  Drops are counted and never block the caller.
* Each measured sample includes two ``perf_counter_ns`` calls, present in every
  cohort.
* Cohorts run in sequence in one process, so CPU drift is not separated from
  the cohort effect.  Rerun with a different ``--iterations`` to sanity-check.

Examples::

    python scripts/bench_startup_profiling_overhead.py --iterations 20000
    python scripts/bench_startup_profiling_overhead.py --mode nosink
    python scripts/bench_startup_profiling_overhead.py --mode log --log-delay-ms 5
    python scripts/bench_startup_profiling_overhead.py --json /tmp/profiling.json
"""

from __future__ import annotations

import argparse
import json
import logging
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

from cloud_agent_profiling import operation_profiling
from cloud_agent_profiling.operation_profiling import (
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
#: every delta in the report is relative to it.  ``log`` also carries ``on``,
#: because the number that answers the claim is ``onlog - on``.
MODE_COHORTS: dict[str, tuple[str, ...]] = {
    "all": ("bare", "off", "on", "onsink", "onlog"),
    "off": ("bare", "off"),
    "on": ("bare", "on", "onsink"),
    "nosink": ("bare", "on"),
    "log": ("bare", "on", "onlog"),
}

COHORT_DESCRIPTIONS: dict[str, str] = {
    "bare": "same sequence inline, no profiler calls (floor)",
    "off": "real calls, profiling disabled",
    "on": "profiling enabled, no sink",
    "onsink": "profiling enabled, JSONL sink configured",
    "onlog": "profiling enabled, slow logging handler attached",
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
    "onlog": instrumented_attempt,
}

#: Mirrors the prefix ``_Exporter._publish`` logs each record with.  The line
#: arrives here already formatted, which is what a real handler would pay for.
PROFILE_LOG_PREFIX = "agent_operation_profile "


class SlowCountingHandler(logging.Handler):
    """A deliberately slow ``INFO`` handler that counts what it was given.

    The per-record delay stands in for the handler this design protects
    against: a socket that blocks, a disk that is full, a synchronous shipper.
    It is charged inside :meth:`emit`, which is the work the logging framework
    would otherwise be doing on the calling thread.

    ``logging.Handler.handle`` holds this handler's lock across ``emit``, so the
    counters need no lock of their own.
    """

    def __init__(self, logger_name: str, delay_seconds: float) -> None:
        super().__init__(level=logging.INFO)
        self._logger_name = logger_name
        self._delay_seconds = max(0.0, delay_seconds)
        #: Profile lines that actually reached this handler.
        self.profile_records = 0
        #: Anything else the logger emitted (warnings).  Expected to stay 0.
        self.other_records = 0
        #: Bytes of formatted profile text, so "produced" is not just a count.
        self.formatted_bytes = 0

    def emit(self, record: logging.LogRecord) -> None:
        """Format the record, charge the synthetic delay, and count it."""
        message = self.format(record)
        # Counted before the delay: the record is in hand either way, and the
        # delay is the cost, not a filter.
        if record.name == self._logger_name and message.startswith(PROFILE_LOG_PREFIX):
            self.profile_records += 1
            self.formatted_bytes += len(message.encode("utf-8"))
        else:
            self.other_records += 1
        if self._delay_seconds:
            time.sleep(self._delay_seconds)


class LogProbe:
    """Attaches :class:`SlowCountingHandler` to the profiler's logger.

    A handler alone would not be enough.  The profiler's logger inherits its
    effective level from the root logger, which is ``WARNING`` by default, so
    ``logger.info`` would be dropped before any handler saw it and ``onlog``
    would silently measure an empty log path -- fast for the wrong reason.  The
    level is therefore raised for the cohort and restored on detach.
    """

    def __init__(self, delay_ms: float) -> None:
        self.delay_ms = delay_ms
        self._logger = logging.getLogger(operation_profiling.__name__)
        self._previous_level = self._logger.level
        self.handler = SlowCountingHandler(self._logger.name, delay_ms / 1000.0)
        self._logger.addHandler(self.handler)
        self._logger.setLevel(logging.INFO)

    def detach(self, expected: int) -> dict[str, object]:
        """Remove the handler, restore the level, and report what it received.

        Call :func:`operation_profiling.flush_sink` first, so the exporter has
        drained and the count is what the handler really received rather than
        what was merely queued.
        """
        self._logger.removeHandler(self.handler)
        self._logger.setLevel(self._previous_level)
        received = self.handler.profile_records
        return {
            "logger": self._logger.name,
            "handler": "SlowCountingHandler",
            "handler_level": "INFO",
            "per_record_delay_ms": self.delay_ms,
            "synchronous_cost_us_per_attempt": round(self.delay_ms * 1000.0, 3),
            "records_expected": expected,
            "records_received": received,
            "records_dropped": max(0, expected - received),
            "other_records_received": self.handler.other_records,
            "formatted_bytes": self.handler.formatted_bytes,
            "bytes_per_received_record": (
                round(self.handler.formatted_bytes / received, 1) if received else 0.0
            ),
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
    if cohort in {"on", "onsink", "onlog"}:
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
    if cohort not in {"on", "onsink", "onlog"}:
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


def cohort_delta(sample: Sample, base: Sample) -> dict[str, float]:
    """Return one cohort's per-attempt cost over another, in microseconds."""
    base_mean = base.mean_us()
    return {
        "p50_us": round(sample.percentile_us(0.50) - base.percentile_us(0.50), 3),
        "p90_us": round(sample.percentile_us(0.90) - base.percentile_us(0.90), 3),
        "p95_us": round(sample.percentile_us(0.95) - base.percentile_us(0.95), 3),
        "mean_us": round(sample.mean_us() - base_mean, 3),
        "mean_ratio": round(sample.mean_us() / base_mean, 3) if base_mean else 0.0,
    }


def deltas(samples: dict[str, Sample], baseline: str = "bare") -> dict[str, dict[str, float]]:
    """Return each cohort's cost over the baseline, in microseconds."""
    base = samples.get(baseline)
    if base is None:
        return {}
    return {
        name: cohort_delta(sample, base) for name, sample in samples.items() if name != baseline
    }


#: Tolerance for calling ``onlog`` and ``on`` the same measurement.  A handler
#: running synchronously at the configured delay would add
#: ``delay_ms * 1000`` us per attempt; 10% of the enabled attempt (or 1 us,
#: whichever is larger) sits far below that, so a separated verdict here means
#: something real and not scheduler noise.
VERDICT_TOLERANCE_FRACTION = 0.10
VERDICT_TOLERANCE_MIN_US = 1.0


def onlog_comparison(samples: dict[str, Sample], delay_ms: float) -> dict[str, object] | None:
    """Test the claim that a blocking log handler does not delay the attempt thread."""
    on = samples.get("on")
    onlog = samples.get("onlog")
    if on is None or onlog is None:
        return None
    delta = cohort_delta(onlog, on)
    tolerance_us = max(VERDICT_TOLERANCE_MIN_US, VERDICT_TOLERANCE_FRACTION * on.mean_us())
    indistinguishable = (
        abs(delta["mean_us"]) <= tolerance_us and abs(delta["p50_us"]) <= tolerance_us
    )
    synchronous_us = delay_ms * 1000.0
    if indistinguishable:
        detail = (
            "`onlog - on` is inside the tolerance: the attempt thread did not pay for the "
            "handler, because the record is serialized on the attempt thread but formatted "
            "and logged on the exporter thread"
        )
    else:
        detail = (
            "`onlog - on` is outside the tolerance. The further it sits from zero and the "
            "closer to the configured delay, the more it looks like the log line is being "
            "formatted on the attempt thread again -- which is the claim this cohort tests"
        )
    return {
        "claim": (
            "a blocking log handler no longer delays the attempt thread: the log line and "
            "the sink write are both published by the _Exporter thread"
        ),
        "test": (
            "compare `onlog` against `on`; the cohorts are identical except that `onlog` "
            "has a slow INFO handler attached to cloud_agent_profiling.operation_profiling"
        ),
        "handler_per_record_delay_ms": delay_ms,
        "synchronous_cost_per_attempt_us": round(synchronous_us, 3),
        "synchronous_cost_per_10k_operations_s": round(synchronous_us * 10_000 / 1e6, 3),
        "delta_onlog_minus_on_us": delta,
        "tolerance_us": round(tolerance_us, 3),
        "verdict": "indistinguishable" if indistinguishable else "separated",
        "verdict_detail": detail,
    }


def build_payload(
    *,
    mode: str,
    samples: dict[str, Sample],
    iterations: int,
    warmup: int,
    budget: int,
    sink: dict[str, object] | None,
    log_handler: dict[str, object] | None,
    log_delay_ms: float,
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
            "logging is left unconfigured for `bare`, `off`, `on` and `onsink`: the "
            "record is built and serialized either way, but no INFO handler formats or "
            "writes the log line in those cohorts",
            "`onlog` is the only cohort with a handler attached, and it is deliberately "
            "slow (see log_handler): it exists to show that the log line is published "
            "on the exporter thread, and the comparison that carries the claim is "
            "`onlog` vs `on`, not `onlog` vs `bare`",
            "at this synthetic ~25 us per attempt the bounded exporter queue drops most "
            "records, so the slow handler receives a sample rather than every line. "
            "Drops never block the attempt thread, so the `onlog` timings stand",
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
        "log_delay_ms": log_delay_ms,
        "log_handler": log_handler,
        "onlog_vs_on": onlog_comparison(samples, log_delay_ms),
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
        for label in ("on", "onsink", "onlog"):
            if label in rows:
                print(
                    f"  (`{label}` adds {rows[label]['mean_us']:.1f} us of attempt-thread CPU"
                    f" per attempt: {rows[label]['mean_us'] * 10_000 / 1e6:.2f} s across a"
                    " 10,000-operation run)"
                )
                break

    log = payload["log_handler"]
    comparison = payload["onlog_vs_on"]
    if isinstance(log, dict) and isinstance(comparison, dict):
        delta = comparison["delta_onlog_minus_on_us"]
        print(
            f"\nslow log handler ({log['per_record_delay_ms']:.3f} ms/record attached to "
            f"{log['logger']}): received {log['records_received']} of "
            f"{log['records_expected']} records, {log['records_dropped']} dropped, "
            f"{log['formatted_bytes'] / 1024:.0f} KiB formatted"
        )
        print(
            "  a handler formatted synchronously on the attempt thread would add "
            f"at least ~{comparison['synchronous_cost_per_attempt_us']:.0f} us per attempt "
            f"({comparison['synchronous_cost_per_10k_operations_s']:.1f} s across a "
            "10,000-operation run)"
        )
        print("  `onlog` vs `on` -- the cohort that carries the claim:")
        print(
            f"    onlog - on: p50 {delta['p50_us']:+.3f} us   p90 {delta['p90_us']:+.3f} us   "
            f"p95 {delta['p95_us']:+.3f} us   mean {delta['mean_us']:+.3f} us   "
            f"({delta['mean_ratio']:.2f}x)"
        )
        print(
            f"    tolerance {comparison['tolerance_us']:.2f} us -> "
            f"{comparison['verdict']}: {comparison['verdict_detail']}"
        )
        if log["records_dropped"]:
            print(
                "    (the dropped records are bounded-queue drops: at ~40,000 attempts/s the\n"
                "    exporter drains one record per handler delay and the rest are discarded.\n"
                "    Drops never block the attempt thread, which is exactly why `onlog` is\n"
                "    indistinguishable from `on`.)"
            )
        if log["other_records_received"]:
            print(
                f"    note: the handler also saw {log['other_records_received']} non-profile "
                "record(s) from the same logger"
            )

    sink = payload["sink"]
    if isinstance(sink, dict):
        print(
            f"\nsink: {sink['records_written']} of {sink['records_expected']} records written"
            f" ({sink['records_dropped']} dropped), {sink['bytes'] / 1024:.0f} KiB, "
            f"{sink['bytes_per_record']:.0f} bytes/record"
        )
        if sink["records_dropped"]:
            print(
                "  most records are dropped here because a synthetic attempt finishes about\n"
                "  every 30 us; a real startup produces one record, which the\n"
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
        "bind() is not modeled, which slightly undercounts `on` and `onsink`.\n"
        "`onlog` is the cohort that tests the logging claim: both destinations, the log\n"
        "line and the JSONL sink write, are published by the one bounded queue drained\n"
        "by the `_Exporter` thread, so a blocking handler is paid there and `onlog - on`\n"
        "should be indistinguishable from zero.  At this synthetic rate the queue drops\n"
        "most records -- the handler receives a sample, which is enough to prove the line\n"
        "was really produced, not a count of every attempt.  Drops are counted, and they\n"
        "never block the attempt thread."
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
        choices=("all", "off", "on", "nosink", "log"),
        default="all",
        help=(
            "cohort selection: 'all' is bare+off+on+onsink+onlog, 'off' is bare+off, "
            "'on' is bare+on+onsink, 'nosink' is bare+on (profiling enabled, no "
            "sink configured), 'log' is bare+on+onlog (the slow-handler comparison). "
            "'bare' is always measured because every delta is relative to it "
            "(default: all)"
        ),
    )
    parser.add_argument(
        "--log-delay-ms",
        type=float,
        default=1.0,
        help=(
            "per-record delay of the deliberately slow INFO handler the onlog cohort "
            "attaches to the profiler's logger; it is charged inside the handler's "
            "emit(), i.e. on whichever thread does the logging (default: 1.0)"
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
    if args.log_delay_ms < 0:
        print("--log-delay-ms must not be negative", file=sys.stderr)
        return 2

    cohorts = MODE_COHORTS[args.mode]
    budget = args.warmup + args.iterations + BUDGET_SLACK
    samples: dict[str, Sample] = {}
    sink_meta: dict[str, object] | None = None
    log_meta: dict[str, object] | None = None

    with tempfile.TemporaryDirectory(prefix="startup-profiling-bench-") as directory:
        sink_path = Path(directory) / "operation-profiling.jsonl"
        try:
            for cohort in cohorts:
                # The handler is attached after install_environment(), which
                # stops the previous cohort's exporter: the shutdown warning
                # that logs is not a profile record and should not be counted.
                install_environment(cohort, sink_path, budget)
                probe = LogProbe(args.log_delay_ms) if cohort == "onlog" else None
                try:
                    values = timed(ATTEMPTS[cohort], args.iterations, args.warmup)
                    verify_selected(cohort)
                    if probe is not None:
                        # Drain before counting, or the handler would be
                        # credited only with what was published mid-run and the
                        # "records received" number would understate the log
                        # path for a reason that has nothing to do with logging.
                        operation_profiling.flush_sink()
                finally:
                    if probe is not None:
                        log_meta = probe.detach(args.warmup + args.iterations)
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
        log_handler=log_meta,
        log_delay_ms=args.log_delay_ms,
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
