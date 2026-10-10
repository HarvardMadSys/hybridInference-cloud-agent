"""Read side of the whole-turn member execution window.

One turn's ``member_execution_window`` is collected inside the Kata guest and
reported once through its own endpoint.  Everything after that point is a pure
transformation of one small stored snapshot, and this module is the only place
that performs it: the admin runs list, the run detail view, and any later
aggregation all call :func:`summarize_turn_resources`, so two views cannot
disagree about the same turn.

**Why a tolerant reader.** A stored snapshot is not re-derived here.  The
snapshot was validated when it was accepted, and this module re-validates it
only to protect a reader: a row written by an older schema version, or damaged
some other way, must degrade to "no summary" instead of failing an admin page.
That is also why a malformed snapshot never raises.

**Why the status vocabulary is not a single number.**  Each metric carries its
own completeness because the ways it can be missing are independent: a kernel
without task I/O accounting says nothing about whether the CPU counters were
complete.  A folded "coverage: 80%" would let one missing metric silently
speak for the others, so it is deliberately absent from this shape.

**Why a partial is not always a lower bound.**  CPU and same-method storage
I/O are additive over disjoint windows, so a measurement missing a piece is a
floor.  A PSS peak is a maximum, not a sum: a peak over fewer complete cycles
may be below the true peak, but it is not "at least" anything, and a peak over
parallel activations is not a simultaneous platform peak at all.  The response
shape keeps the reason codes rather than a single ``>=`` flag so each renderer
can make that distinction from the metric's own kind.
"""

from __future__ import annotations

import math
from collections.abc import Mapping
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Sequence

#: Version of the stored snapshot shape this module reads.  A snapshot written
#: under a different version is reported as absent rather than guessed at: the
#: numbers in it would carry a meaning this reader cannot vouch for.
TURN_SUMMARY_SCHEMA_VERSION = 1

#: The only scope this reader projects.
TURN_SUMMARY_SCOPE = "member_execution_window"

#: What a run's compact summary calls a metric whose inputs are incomplete but
#: still a floor, and one whose inputs cannot be combined at all.
_STATUS_AVAILABLE = "available"
_STATUS_PARTIAL = "partial"
_STATUS_UNAVAILABLE = "unavailable"

#: Used when an attempt of a run contributed no summary at all.  The run's
#: numbers are then a floor over the attempts that did.
_REASON_NOT_MEASURED = "observer_unavailable"

#: Used when a run's attempts were measured by different capabilities.  Their
#: numbers do not describe one method, so they are never added into one total.
_REASON_MIXED_SOURCE = "source_mismatch"

_METRIC_KEYS = ("cpu", "memory", "storage_io")


def parse_cpu_capacity(value: object) -> float | None:
    """Read a positive CPU quota in cores, preserving unknown/unlimited as absent."""
    if isinstance(value, bool) or not isinstance(value, (int, float, str)):
        return None
    try:
        cores = float(value)
    except (ValueError, OverflowError):
        return None
    return cores if math.isfinite(cores) and cores > 0 else None


def _as_dict(value: Any) -> dict[str, Any] | None:
    """Return a plain mapping, or None for anything else."""
    if isinstance(value, dict):
        return value
    return None


def _int_or_none(value: Any) -> int | None:
    """Coerce a stored counter to a non-negative int, or None."""
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value if value >= 0 else None


def _reason_codes(metric: Mapping[str, Any]) -> list[str]:
    """Copy a metric's bounded reason codes, ignoring anything else."""
    raw = metric.get("reason_codes")
    if not isinstance(raw, list):
        return []
    return [code for code in raw if isinstance(code, str)]


def _metric_status(metric: Mapping[str, Any]) -> str:
    """Read one metric's status, refusing an unknown vocabulary."""
    status = metric.get("status")
    if status in (_STATUS_AVAILABLE, _STATUS_PARTIAL, _STATUS_UNAVAILABLE):
        return status
    return _STATUS_UNAVAILABLE


def _project_metric(metric: Mapping[str, Any], *value_keys: str) -> dict[str, Any]:
    """Project the shared metric envelope, with only the named counters."""
    projected: dict[str, Any] = {
        "status": _metric_status(metric),
        "method": metric.get("method"),
        "reason_codes": _reason_codes(metric),
    }
    for key in value_keys:
        projected[key] = _int_or_none(metric.get(key))
    return projected


def _valid_snapshot(stored: Mapping[str, Any]) -> bool:
    """Whether this snapshot is the declared shape this reader understands.

    A cheap structural check, not a re-validation of every bound.  The goal is
    to distinguish "a summary written by this schema" from "something else in
    that column" so the latter cannot be rendered as a measurement.
    """
    if stored.get("schema_version") != TURN_SUMMARY_SCHEMA_VERSION:
        return False
    if stored.get("scope") != TURN_SUMMARY_SCOPE:
        return False
    if not isinstance(stored.get("measurement_id"), str):
        return False
    if not isinstance(stored.get("source"), str):
        return False
    if stored.get("state") not in ("collecting", "final"):
        return False
    window = _as_dict(stored.get("window"))
    if window is None:
        return False
    duration = _int_or_none(window.get("duration_ms"))
    observed = _int_or_none(window.get("observed_through_ms"))
    if duration is None or observed is None or observed > duration:
        return False
    return all(_as_dict(stored.get(key)) is not None for key in _METRIC_KEYS)


def summarize_turn_resources(
    stored: Mapping[str, Any] | None,
    *,
    attempt_terminal: bool,
    received_at: Any = None,
) -> dict[str, Any] | None:
    """Project one stored snapshot into the response shape, or None.

    ``attempt_terminal`` is what turns a stored ``collecting`` snapshot into
    ``interrupted``.  That distinction is the point: a snapshot whose
    collection stopped when the process died still holds everything measured
    up to that moment, and calling it finalized would claim the tail it never
    saw.  ``observed_through_ms`` is carried through unchanged so a reader can
    say exactly how far it got.

    A non-terminal attempt with a ``collecting`` snapshot stays ``collecting``.
    Nothing here reads a clock: ``received_at`` is supplied by the caller
    because only the caller knows when the row was written.
    """
    if not isinstance(stored, Mapping):
        return None
    if not _valid_snapshot(stored):
        return None

    window = _as_dict(stored["window"]) or {}
    state = stored["state"]
    if state == "collecting" and attempt_terminal:
        state = "interrupted"

    summary: dict[str, Any] = {
        "measurement_id": stored["measurement_id"],
        "state": state,
        "source": stored["source"],
        "received_at": received_at,
        "window": {
            "duration_ms": _int_or_none(window.get("duration_ms")) or 0,
            "observed_through_ms": _int_or_none(window.get("observed_through_ms")) or 0,
            "open_scan_ms": _int_or_none(window.get("open_scan_ms")) or 0,
            "close_scan_ms": _int_or_none(window.get("close_scan_ms")) or 0,
        },
        "cpu": _project_metric(_as_dict(stored["cpu"]) or {}, "usec"),
        "storage_io": _project_metric(
            _as_dict(stored["storage_io"]) or {},
            "read_bytes",
            "write_bytes",
        ),
    }
    summary["cpu"]["capacity_cores"] = parse_cpu_capacity(
        (_as_dict(stored["cpu"]) or {}).get("capacity_cores")
    )
    memory = _as_dict(stored["memory"]) or {}
    projected_memory = _project_metric(memory, "peak_bytes")
    projected_memory.update(
        {
            "sample_interval_ms": _int_or_none(memory.get("sample_interval_ms")) or 1,
            "valid_samples": _int_or_none(memory.get("valid_samples")) or 0,
            "expected_samples": _int_or_none(memory.get("expected_samples")) or 0,
            "max_gap_ms": _int_or_none(memory.get("max_gap_ms")) or 0,
        }
    )
    summary["memory"] = projected_memory
    return summary


def _brief(metric: Mapping[str, Any]) -> dict[str, Any]:
    """The compact status-only projection the runs list carries."""
    return {"status": _metric_status(metric), "reason_codes": _reason_codes(metric)}


def _fold_status(
    statuses: Sequence[str],
    *,
    has_values: bool,
    mixed_source: bool,
    short: bool = False,
) -> str:
    """Combine per-attempt statuses into one metric status.

    The folding rule is deliberately asymmetric.  Any attempt that could not
    contribute makes the metric a floor, never a complete measurement, whether
    it contributed a partial measurement or nothing at all -- a run that
    measured three of its four executions has a CPU *floor*, and calling that
    complete is exactly the overclaim this shape exists to prevent.  A metric
    with no contributing value is unavailable rather than zero.  Mixed sources
    make the combination meaningless regardless of the parts.
    """
    if mixed_source or not has_values:
        return _STATUS_UNAVAILABLE
    if short or any(status != _STATUS_AVAILABLE for status in statuses):
        return _STATUS_PARTIAL
    return _STATUS_AVAILABLE


def summarize_run_resources(
    attempts: Sequence[Mapping[str, Any] | None],
    *,
    terminal_by_attempt: Sequence[bool] | None = None,
    received_at_by_attempt: Sequence[Any] | None = None,
) -> dict[str, Any] | None:
    """Fold a run's attempts into the compact list projection, or None.

    CPU and same-method storage I/O are summed over the measured parts, so a
    run missing one attempt reports a floor and says so through ``partial``.
    Memory reports the **largest single activation peak** — peaks describe
    concurrent or sequential demand, and adding them would invent a
    simultaneous platform peak that never happened.

    Attempts measured by different capabilities are not combined at all: their
    numbers do not share a method, so every metric degrades to unavailable with
    a reason.  ``None`` means no attempt of this run carries a summary, which
    is different from a run that used no resources.
    """
    projected: list[dict[str, Any]] = []
    for index, attempt in enumerate(attempts):
        terminal = bool(terminal_by_attempt[index]) if terminal_by_attempt else True
        received_at = received_at_by_attempt[index] if received_at_by_attempt else None
        summary = summarize_turn_resources(
            attempt,
            attempt_terminal=terminal,
            received_at=received_at,
        )
        if summary is not None:
            projected.append(summary)
    if not projected:
        return None

    sources = {summary["source"] for summary in projected}
    mixed_source = len(sources) > 1
    total = len(attempts)

    cpu_values = [s["cpu"]["usec"] for s in projected if s["cpu"]["usec"] is not None]
    io_read_values = [
        s["storage_io"]["read_bytes"]
        for s in projected
        if s["storage_io"]["read_bytes"] is not None
    ]
    io_write_values = [
        s["storage_io"]["write_bytes"]
        for s in projected
        if s["storage_io"]["write_bytes"] is not None
    ]
    memory_values = [
        s["memory"]["peak_bytes"] for s in projected if s["memory"]["peak_bytes"] is not None
    ]

    cpu_statuses = [s["cpu"]["status"] for s in projected]
    memory_statuses = [s["memory"]["status"] for s in projected]
    io_statuses = [s["storage_io"]["status"] for s in projected]

    cpu_reasons: list[str] = []
    memory_reasons: list[str] = []
    io_reasons: list[str] = []
    short = len(projected) < total
    if short:
        cpu_reasons.append(_REASON_NOT_MEASURED)
        memory_reasons.append(_REASON_NOT_MEASURED)
        io_reasons.append(_REASON_NOT_MEASURED)

    cpu_status = _fold_status(
        cpu_statuses,
        has_values=bool(cpu_values),
        mixed_source=mixed_source,
        short=short,
    )
    memory_status = _fold_status(
        memory_statuses,
        has_values=bool(memory_values),
        mixed_source=mixed_source,
        short=short,
    )
    io_status = _fold_status(
        io_statuses,
        has_values=bool(io_read_values or io_write_values),
        mixed_source=mixed_source,
        short=short,
    )

    # Every reason any attempt gave is carried up, including for a metric that
    # folded to unavailable: dropping them there would lose the one thing a
    # reader needs, which is *why* this run has no storage number. The fold's
    # own codes come first so the run-level cause reads before the detail.
    def _collect(metric_key: str, fold_reasons: list[str]) -> list[str]:
        ordered = list(fold_reasons)
        for summary in projected:
            for code in summary[metric_key]["reason_codes"]:
                if code not in ordered:
                    ordered.append(code)
        return ordered

    if mixed_source:
        cpu_reasons = _collect("cpu", [_REASON_MIXED_SOURCE])
        memory_reasons = _collect("memory", [_REASON_MIXED_SOURCE])
        io_reasons = _collect("storage_io", [_REASON_MIXED_SOURCE])
    else:
        cpu_reasons = _collect("cpu", cpu_reasons)
        memory_reasons = _collect("memory", memory_reasons)
        io_reasons = _collect("storage_io", io_reasons)

    states = {summary["state"] for summary in projected}
    if states == {"final"} and len(projected) == total:
        run_state = "final"
    elif "collecting" in states:
        run_state = "collecting"
    else:
        run_state = "interrupted"

    observed = [summary["window"]["observed_through_ms"] for summary in projected]
    return {
        # Peaks describe demand, not consumption, so the memory cell is a
        # maximum while the CPU and I/O cells are sums over the measured parts.
        "cpu_usec": sum(cpu_values) if (cpu_values and not mixed_source) else None,
        "memory_peak_bytes": (max(memory_values) if (memory_values and not mixed_source) else None),
        "state": run_state,
        "observed_through_ms": max(observed) if observed else None,
        "cpu": _brief({"status": cpu_status, "reason_codes": cpu_reasons}),
        "memory": _brief({"status": memory_status, "reason_codes": memory_reasons}),
        "storage_io": _brief({"status": io_status, "reason_codes": io_reasons}),
        "attempts_measured": len(projected),
        "attempts_total": total,
    }
