"""Sending frozen measurements to the control plane, off the turn's own path.

:mod:`cloud_agent_host.measurement_delivery` says what to retry and what to
drop; this module applies those bounds, in one place, to the deliveries this
process has in flight.
"""

from __future__ import annotations

import json
import os
import threading
import time
from typing import TYPE_CHECKING, Any

from cloud_agent_host.claimed_job import ClaimedJob, claim_secret_values as _claim_secret_values
from cloud_agent_host.control_plane_client import ControlPlane
from cloud_agent_host.logging_utils import get_logger
from cloud_agent_host.measurement_delivery import (
    DELIVERY_ATTEMPTS_MAX,
    DELIVERY_BYTES_MAX,
    DELIVERY_QUEUE_MAX,
    attempts_exhausted,
    classify_exception,
    classify_response,
)
from cloud_agent_shared import operation_profiling
from cloud_agent_shared.secret_redaction import SecretRedactor

if TYPE_CHECKING:
    from collections.abc import Iterable, Mapping

# Log records keep the name they had while all of this was runner.py.
logger = get_logger("cloud_agent_host.runner")


def _delivery_payload_size(measurement: operation_profiling.Measurement) -> int:
    """Return the encoded record and manifest bytes held by one delivery."""
    outcome = measurement.frozen_outcome or "incomplete"
    record = operation_profiling.frozen_record(measurement, outcome)
    manifest = operation_profiling.manifest_for_measurement(measurement)
    if record is None or manifest is None:
        return 0
    try:
        return len(
            json.dumps(
                {"record": record, "run": manifest},
                separators=(",", ":"),
                default=str,
            ).encode()
        )
    except (TypeError, ValueError):
        return 0


def _enqueue_delivery(
    measurement: operation_profiling.Measurement | None,
    *,
    job: ClaimedJob,
    base_url: str,
    drain_s: float,
) -> None:
    """Start a frozen measurement's send, or wait for the one already running.

    A record is frozen once, so only the first caller starts a thread. *Every*
    caller then waits its own bound -- including the one that started it. The
    first version returned as soon as the thread was running, which meant a
    turn that failed before dispatch (so the exit's sweep was the first
    enqueue) got no wait at all, and the process could exit with the send still
    in flight. That is exactly the case the sweep exists for.
    """
    if measurement is None:
        return
    global _DELIVERY_DROPPED_COUNT, _DELIVERY_DRAIN_TIMEOUTS, _DELIVERY_PENDING_BYTES
    measurement_id = measurement.measurement_id
    payload_size = _delivery_payload_size(measurement)
    start = False
    dropped = False
    with _DELIVERY_STATE_LOCK:
        if measurement_id in _DELIVERED_MEASUREMENT_IDS:
            return
        thread = _IN_FLIGHT_DELIVERIES.get(measurement_id)
        if thread is None and measurement_id not in _STARTING_DELIVERIES:
            if (
                len(_IN_FLIGHT_DELIVERIES) + len(_STARTING_DELIVERIES) >= DELIVERY_QUEUE_MAX
                or _DELIVERY_PENDING_BYTES + payload_size > DELIVERY_BYTES_MAX
            ):
                _DELIVERED_MEASUREMENT_IDS.add(measurement_id)
                _DELIVERY_DROPPED_COUNT += 1
                dropped = True
            else:
                _STARTING_DELIVERIES.add(measurement_id)
                _DELIVERY_SIZES[measurement_id] = payload_size
                _DELIVERY_PENDING_BYTES += payload_size
                start = True
    if dropped:
        logger.warning(
            "agent_measurement_delivery_queue_full",
            extra={
                "event": "agent_measurement_delivery_queue_full",
                "measurement_id": measurement_id,
                "dropped": _DELIVERY_DROPPED_COUNT,
            },
        )
        return
    if start:
        thread = _deliver_in_background(job=job, base_url=base_url, measurement=measurement)
        with _DELIVERY_STATE_LOCK:
            _STARTING_DELIVERIES.discard(measurement_id)
            # Recorded only once it is actually running. A thread that could
            # not start leaves nothing behind, so the next caller retries.
            if thread is not None and measurement_id not in _DELIVERED_MEASUREMENT_IDS:
                _IN_FLIGHT_DELIVERIES[measurement_id] = thread
            elif thread is None:
                _DELIVERY_PENDING_BYTES = max(
                    0,
                    _DELIVERY_PENDING_BYTES - _DELIVERY_SIZES.pop(measurement_id, 0),
                )
    # Joined whether it was just started or was already on its way: the bound
    # belongs to the caller that is waiting, not to whoever happened to be
    # first.
    if thread is not None and drain_s > 0:
        thread.join(timeout=drain_s)
        if thread.is_alive():
            with _DELIVERY_STATE_LOCK:
                _DELIVERY_DRAIN_TIMEOUTS += 1
                timed_out = _DELIVERY_DRAIN_TIMEOUTS
            logger.warning(
                "agent_measurement_delivery_outlived_drain",
                extra={
                    "event": "agent_measurement_delivery_outlived_drain",
                    "measurement_id": measurement_id,
                    "drain_s": drain_s,
                    "count": timed_out,
                },
            )


def _deliver_in_background(
    *,
    job: ClaimedJob,
    base_url: str,
    measurement: operation_profiling.Measurement,
) -> threading.Thread | None:
    """Start a send for this measurement, or ``None`` if no thread would run."""

    def _send() -> None:
        global _DELIVERY_PENDING_BYTES
        control: ControlPlane | None = None
        try:
            control = ControlPlane(base_url, job.worker_token)
            control._secret_values = _claim_secret_values(job)
            control._redactor = SecretRedactor(control._secret_values)
            outcome = measurement.frozen_outcome or "incomplete"
            record = operation_profiling.frozen_record(measurement, outcome)
            manifest = operation_profiling.manifest_for_measurement(measurement)
            if record is None or manifest is None:
                logger.warning(
                    "agent_measurement_delivery_missing_record",
                    extra={
                        "event": "agent_measurement_delivery_missing_record",
                        "job_id": job.job_id,
                        "measurement_id": measurement.measurement_id,
                    },
                )
                return
            counts = deliver_measurement_record(
                control=control,
                job_id=job.job_id,
                record=record,
                manifest=manifest,
            )
            if counts["dropped"] or counts["retried"]:
                logger.info(
                    "agent_measurement_delivery_summary",
                    extra={
                        "event": "agent_measurement_delivery_summary",
                        "job_id": job.job_id,
                        "measurement_id": measurement.measurement_id,
                        **counts,
                    },
                )
        except Exception:
            logger.warning(
                "agent_measurement_delivery_failed",
                exc_info=True,
                extra={"event": "agent_measurement_delivery_failed"},
            )
        finally:
            if control is not None:
                control.close()
            with _DELIVERY_STATE_LOCK:
                _DELIVERED_MEASUREMENT_IDS.add(measurement.measurement_id)
                _IN_FLIGHT_DELIVERIES.pop(measurement.measurement_id, None)
                _DELIVERY_PENDING_BYTES = max(
                    0,
                    _DELIVERY_PENDING_BYTES - _DELIVERY_SIZES.pop(measurement.measurement_id, 0),
                )

    thread = threading.Thread(target=_send, name="agent-measurement-delivery", daemon=True)
    try:
        thread.start()
    except RuntimeError as exc:  # pragma: no cover - thread limit reached
        logger.warning(
            "agent_measurement_delivery_unstarted",
            extra={"event": "agent_measurement_delivery_unstarted", "error": str(exc)},
        )
        return None
    return thread


#: The fields of a record a run manifest is built from. The manifest is
#: derived from the record rather than re-read from the environment so the two
def deliver_measurement_record(
    *,
    control: ControlPlane,
    job_id: str,
    record: Mapping[str, Any],
    manifest: Mapping[str, Any],
) -> dict[str, int]:
    """Deliver one frozen record with bounded retries and explicit accounting."""
    counts = {"delivered": 0, "duplicate": 0, "dropped": 0, "retried": 0}
    measurement_id = str(record.get("measurement_id") or "")
    body: dict[str, Any] = {"record": dict(record), "run": dict(manifest)}
    for attempt in range(1, DELIVERY_ATTEMPTS_MAX + 1):
        try:
            status, answer = control.record_measurement(job_id, body)
        except Exception as exc:
            outcome = classify_exception(exc)
        else:
            outcome = classify_response(status, answer)
        if outcome.delivered:
            counts["duplicate" if outcome.result == "duplicate" else "delivered"] += 1
            break
        if not outcome.retryable:
            counts["dropped"] += 1
            logger.warning(
                "agent_measurement_refused",
                extra={
                    "event": "agent_measurement_refused",
                    "job_id": job_id,
                    "measurement_id": measurement_id,
                    "reason": outcome.reason,
                },
            )
            break
        counts["retried"] += 1
        if attempts_exhausted(attempt):
            counts["dropped"] += 1
            logger.warning(
                "agent_measurement_undeliverable",
                extra={
                    "event": "agent_measurement_undeliverable",
                    "job_id": job_id,
                    "measurement_id": measurement_id,
                    "reason": outcome.reason,
                },
            )
            break
        time.sleep(min(2.0, 0.25 * attempt))
    return counts


def deliver_measurements(
    *,
    control: ControlPlane,
    job_id: str,
    measurement_ids: Iterable[str] | None = None,
) -> dict[str, int]:
    """Drain selected records from the optional JSONL baseline sink.

    The live runtime delivers its frozen record directly and does not require
    this file. This reader remains for explicit sink drains and recovery tests;
    delivery, retry, and timeout still happen after the measurement boundary.

    Delivery is best effort by design. A refused or undeliverable record is
    counted and the turn ends normally -- the measurement is diagnostic, and
    losing one must never fail the work it described. What it must not do is
    *silently* lose one, which is what the counts are for.

    Returns:
        ``{"delivered", "duplicate", "dropped", "retried"}`` for this call.
    """
    sink = (os.environ.get(operation_profiling.ENV_PROFILING_SINK) or "").strip()
    counts = {"delivered": 0, "duplicate": 0, "dropped": 0, "retried": 0}
    if not sink:
        return counts
    wanted = set(measurement_ids or ())
    # Drained *before* the sink is looked for. The exporter writes on its own
    # thread, so a record can be frozen and queued in this process while the
    # file does not exist yet -- and treating that as "nothing to send" lost
    # the record for good, because the turn's exit only joins a send that has
    # already finished. An empty scan must not be read as a completed send.
    operation_profiling.flush_sink()
    if not os.path.exists(sink):
        return counts
    try:
        with open(sink, encoding="utf-8") as handle:
            lines = [line for line in handle if line.strip()]
    except OSError as exc:
        logger.warning(
            "agent_measurement_sink_unreadable",
            extra={"event": "agent_measurement_sink_unreadable", "error": str(exc)},
        )
        return counts

    delivered: set[str] = set()
    for line in lines:
        try:
            record = json.loads(line)
        except ValueError:
            # A truncated line is a record the exporter could not finish
            # writing. It is dropped from the sink and counted, not retried:
            # re-reading bytes that will never parse cannot help.
            counts["dropped"] += 1
            continue
        measurement_id = str(record.get("measurement_id") or "")
        if wanted and measurement_id not in wanted:
            continue
        if measurement_id in _DELIVERED_MEASUREMENT_IDS:
            continue
        manifest = operation_profiling.manifest_for_run(operation_profiling.active_run())
        if manifest is None:
            counts["dropped"] += 1
            delivered.add(measurement_id)
            continue
        outcome_counts = deliver_measurement_record(
            control=control,
            job_id=job_id,
            record=record,
            manifest=manifest,
        )
        for key, value in outcome_counts.items():
            counts[key] += value
        delivered.add(measurement_id)

    # Remembered in this process rather than erased from the file. The sink can
    # be shared -- several runners on one host pointed at one state directory --
    # and a read-modify-write of it would silently drop whatever another
    # process appended between this call's read and its write.
    with _DELIVERY_STATE_LOCK:
        _DELIVERED_MEASUREMENT_IDS.update(delivered)
    return counts


#: Measurements this process has already decided the fate of. Held in memory
#: because the sink is append-only and may be shared: rewriting it would race
#: another process's append, and losing a record that was never delivered is
#: exactly the failure the delivery counts exist to make visible.
_DELIVERED_MEASUREMENT_IDS: set[str] = set()


#: Measurements this process has already queued for delivery. A record is
#: frozen once and its fate is decided once, so a second enqueue would be the
#: same sample sent twice -- and one of the two would be reported as a
#: duplicate that never happened.
#: The delivery in flight for each measurement, so a later caller can wait
#: for the send already happening. A second enqueue cannot start one -- the
#: record is frozen once -- but "already queued" must not mean "do not wait",
#: or the handoff's short bound would silently become the only attempt.
_IN_FLIGHT_DELIVERIES: dict[str, threading.Thread] = {}


_STARTING_DELIVERIES: set[str] = set()


_DELIVERY_SIZES: dict[str, int] = {}


_DELIVERY_STATE_LOCK = threading.Lock()


_DELIVERY_DROPPED_COUNT = 0


_DELIVERY_DRAIN_TIMEOUTS = 0


_DELIVERY_PENDING_BYTES = 0


def reset_delivery_state() -> None:
    """Forget what this process delivered and queued. Tests call this."""
    global _DELIVERY_DROPPED_COUNT, _DELIVERY_DRAIN_TIMEOUTS, _DELIVERY_PENDING_BYTES
    with _DELIVERY_STATE_LOCK:
        _DELIVERED_MEASUREMENT_IDS.clear()
        _IN_FLIGHT_DELIVERIES.clear()
        _STARTING_DELIVERIES.clear()
        _DELIVERY_SIZES.clear()
        _DELIVERY_DROPPED_COUNT = 0
        _DELIVERY_DRAIN_TIMEOUTS = 0
        _DELIVERY_PENDING_BYTES = 0
