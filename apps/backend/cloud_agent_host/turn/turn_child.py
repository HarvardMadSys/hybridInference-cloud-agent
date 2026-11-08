"""One claimed turn, in the child process the host service started for it.

Started as ``python -m cloud_agent_host.runner --turn``. It reads its turn on
standard input, runs the same preflight a turn needs, answers the handover,
and only then runs the turn under its lease
(:mod:`cloud_agent_host.turn.shared_live_attempt`).
"""

from __future__ import annotations

import contextlib
from typing import TYPE_CHECKING, Any

from cloud_agent_host.claimed_job import (
    ClaimedJob,
    LeaseLost,
    claim_secret_values as _claim_secret_values,
)
from cloud_agent_host.container.sandbox import (
    ContainerBackend,
    SandboxBackend,
    build_backend_from_env,
)
from cloud_agent_host.control_plane_client import ControlPlane, _log_control_failure
from cloud_agent_host.logging_utils import get_logger
from cloud_agent_host.measurement_delivery import DELIVERY_DRAIN_S
from cloud_agent_host.runtime_qualification import VERDICT_KEY, adopt_verdict
from cloud_agent_host.runtimes import get_runtime
from cloud_agent_host.service.attempt_lease import HEARTBEAT_INTERVAL_S, Heartbeater
from cloud_agent_host.service.host_control import release_turn
from cloud_agent_host.service.host_runtime import (
    HANDOVER_ACCEPTED,
    HANDOVER_DECLINED,
    decode_turn,
    write_handover,
)
from cloud_agent_host.service.local_settings import (
    DEFAULT_AGENT_TIMEOUT_S,
    DEFAULT_LEASE_TTL_S,
    _new_sandbox_storage_error,
)
from cloud_agent_host.service.measurement_sender import _enqueue_delivery
from cloud_agent_host.service.runner_stop import drain_on_sigterm as _drain_on_sigterm
from cloud_agent_host.service.turn_launch import handed_worker_id, read_turn_input
from cloud_agent_host.turn.shared_live_attempt import _run_shared_live_attempt
from cloud_agent_shared import operation_profiling
from cloud_agent_shared.host_settings import (
    RUNNER_SETTING_KEYS,
    reported_values,
)
from cloud_agent_shared.secret_redaction import SecretRedactor

if TYPE_CHECKING:
    from collections.abc import Mapping

# Log records keep the name they had while all of this was runner.py.
logger = get_logger("cloud_agent_host.runner")


def run_turn(
    job: ClaimedJob,
    *,
    base_url: str,
    worker_id: str,
    lease_ttl: float = DEFAULT_LEASE_TTL_S,
    agent_timeout_s: float = DEFAULT_AGENT_TIMEOUT_S,
    backend: SandboxBackend | None = None,
    host: str | None = None,
    measurement: operation_profiling.Measurement | None = None,
    host_settings: Mapping[str, float] | None = None,
) -> int:
    """Deliver one already-claimed turn, then report what it measured.

    The delivery is outside the turn on purpose: the record was frozen while
    the startup ran, and sending it -- with its retries and its timeouts --
    must not land inside the thing being measured or delay the turn's own
    answer.
    """
    try:
        return _run_turn(
            job,
            base_url=base_url,
            worker_id=worker_id,
            lease_ttl=lease_ttl,
            agent_timeout_s=agent_timeout_s,
            backend=backend,
            host=host,
            measurement=measurement,
            host_settings=host_settings,
        )
    finally:
        # A bounded sweep, not the delivery itself for the common case: the
        # record was queued when it was frozen, and this only catches one the
        # turn ended before. Waited for briefly, because a control plane that
        # is slow or unreachable must not hold this process open -- the turn is
        # over and its answer given, and whatever the thread has not finished
        # by the drain bound is simply not delivered.
        _enqueue_delivery(measurement, job=job, base_url=base_url, drain_s=DELIVERY_DRAIN_S)


def _run_turn(
    job: ClaimedJob,
    *,
    base_url: str,
    worker_id: str,
    lease_ttl: float = DEFAULT_LEASE_TTL_S,
    agent_timeout_s: float = DEFAULT_AGENT_TIMEOUT_S,
    backend: SandboxBackend | None = None,
    host: str | None = None,
    measurement: operation_profiling.Measurement | None = None,
    host_settings: Mapping[str, float] | None = None,
) -> int:
    """Deliver one already-claimed turn, and report its outcome.

    Everything after the claim: the lease renewal, the retained session, the
    agent, and the terminal transition. This is what the host service starts a
    child process for, and the boundary is the claim precisely because it is
    the last point at which a turn can be handed back to the queue unspent.
    """
    if backend is None:
        backend = build_backend_from_env()
        backend.preflight()

    storage_error = _new_sandbox_storage_error(job, host_settings)
    if storage_error:
        logger.warning(
            "agent_storage_admission_declined",
            extra={
                "event": "agent_storage_admission_declined",
                "job_id": job.job_id,
                "detail": storage_error,
            },
        )
        operation_profiling.discard(measurement)
        return (
            0
            if release_turn(
                base_url=base_url,
                worker_token=job.worker_token,
                job_id=job.job_id,
                reason="storage_pressure",
            )
            else 2
        )

    print(f"claimed {job.job_id} attempt {job.attempt_no} runtime={job.runtime}")
    logger.info(
        "agent_attempt_claimed job_id=%s attempt_id=%s worker_id=%s host=%s",
        job.job_id,
        job.attempt_id,
        worker_id,
        host,
    )
    control = ControlPlane(base_url, job.worker_token)
    control._secret_values = _claim_secret_values(job)
    control._redactor = SecretRedactor(control._secret_values)
    control.job_id = job.job_id
    heart = Heartbeater(control, ttl=lease_ttl, interval=HEARTBEAT_INTERVAL_S)

    try:
        runtime = get_runtime(job.runtime)
    except KeyError as exc:
        operation_profiling.finish(measurement, outcome="failed", failure_stage="runtime_lookup")
        control.finish("failed", str(exc))
        control.close()
        return 2

    heart.start()
    try:
        return _run_shared_live_attempt(
            runtime=runtime,
            job=job,
            control=control,
            heart=heart,
            backend=backend,
            host=host,
            agent_timeout_s=agent_timeout_s,
            finish_attempt=control.finish,
            measurement=measurement,
            host_settings=host_settings,
        )
    except LeaseLost as exc:
        _log_control_failure("attempt_dispatch_fenced", job.job_id, exc)
        return 3
    except Exception as exc:
        # Preparation before the shared handler (image inspection, manifest
        # validation, etc.) can fail after claim. Report a terminal outcome
        # before finally stops renewal, instead of leaving a running attempt
        # that will later be misreported as a lease failure.
        _log_control_failure("attempt_dispatch", job.job_id, exc)
        try:
            control.finish("failed", f"attempt dispatch failed ({type(exc).__name__})")
        except Exception as finish_exc:
            _log_control_failure("finish", job.job_id, finish_exc)
        return 2
    finally:
        # A claimed attempt whose validation rejected it before the shared
        # handler started still closes its measurement, marked incomplete.
        operation_profiling.finish(measurement, outcome="incomplete", failure_stage="preflight")
        heart.stop()
        control.close()


def _turn_host_settings(raw: Any) -> dict[str, float]:
    """The runner's share of the runtime settings a turn was handed, checked again.

    The parent already resolved them, so this only drops what no turn should
    act on: a key the runner does not apply, or a value that is not a usable
    finite number. Zero is valid for the optional disk quota. A setting
    dropped here is read from the environment instead.
    """
    return {
        key: float(value)
        for key, value in (reported_values(raw) or {}).items()
        if key in RUNNER_SETTING_KEYS and (value > 0 or key == "sandbox_disk_mib")
    }


def _serve_turn(
    *,
    handover_fd: int,
    base_url: str,
    worker_id: str,
    lease_ttl: float,
    agent_timeout_s: float,
    host: str | None,
    spare: bool = False,
) -> int:
    """Run the one turn delivered on standard input, or decline having none.

    The answer is sent **after everything that can still be declined and
    before anything that cannot**. That ordering is the whole contract in both
    directions: only a child that explicitly declines is requeued unspent, so
    everything this process could fail at without consequence belongs above
    the answer, and everything a user or an operator could notice belongs
    below it. Exiting without a trustworthy answer remains ambiguous.

    Preflight is above it for exactly that reason. It checks Docker, the
    image and the network without running the turn, so failing it means the
    turn has not begun — and answering first would instead strand that turn behind
    a lease it will never use, having spent one of its attempts.

    ``spare`` is a child started before its turn was claimed (turn_launch).
    One retired without a turn exits at once, without the probe.
    """
    # Install before reading the payload or probing the runtime. The parent
    # does not signal children during a graceful drain, but this also protects
    # a child from an out-of-band process-group signal while its handover is
    # still ambiguous. Once a child exists, deployment waits for its natural
    # exit rather than guessing whether it accepted the turn.
    _drain_on_sigterm()

    # Drained first, and before anything slow, so the host is never left
    # blocked writing a long prompt into a pipe this process has not read yet.
    raw = read_turn_input(spare=spare)
    if raw is None:
        return 0
    # Recover only the parent's optional timing selection before preflight.
    # This does not accept the turn or reserve a slot: declined children
    # discard their timing without emitting, and the host retains its budget.
    # Malformed measurement metadata must never prevent the actual probe or
    # the turn; it merely leaves this child unmeasured.
    payload = None
    measurement = None
    with contextlib.suppress(Exception):
        payload = decode_turn(raw)
        if payload is not None:
            measurement = operation_profiling.attach_selection(payload.get("_profiling"))

    # Empty input runs the full deployment canary; a real turn adopts what its host proved.
    operation_profiling.mark(measurement, "preflight")
    try:
        with operation_profiling.span(measurement, "preflight"):
            backend = build_backend_from_env()
            if payload is not None:
                backend.adopt_rules_verification(payload)
            verdict = payload.get(VERDICT_KEY) if payload is not None else None
            if isinstance(backend, ContainerBackend) and adopt_verdict(backend, verdict):
                backend.check_network()
            else:
                backend.preflight()
    except Exception as exc:
        # Non-zero: the deployment check reads this exit status, and an image
        # that cannot run turns has to fail a rollout rather than pass it. For
        # a real turn the host puts it straight back and, after a run of these,
        # stops claiming and says why — so a machine in this state costs the
        # queue nothing while somebody fixes it.
        operation_profiling.discard(measurement)
        write_handover(handover_fd, f"{HANDOVER_DECLINED} {exc}")
        return 2

    operation_profiling.mark(measurement, "handover")
    if payload is None:
        operation_profiling.discard(measurement)
        write_handover(handover_fd, f"{HANDOVER_DECLINED} no turn was delivered")
        return 0
    try:
        job = ClaimedJob.from_response(payload)
    except (KeyError, TypeError, ValueError) as exc:
        operation_profiling.discard(measurement)
        write_handover(handover_fd, f"{HANDOVER_DECLINED} unusable turn ({type(exc).__name__})")
        return 0

    host_settings = _turn_host_settings(payload.get("_host_settings"))
    storage_error = _new_sandbox_storage_error(job, host_settings)
    if storage_error:
        operation_profiling.discard(measurement)
        write_handover(handover_fd, f"{HANDOVER_DECLINED} {storage_error}")
        return 2
    write_handover(handover_fd, f"{HANDOVER_ACCEPTED} {job.job_id}")
    # Only an accepted turn can finish and deliver the inherited measurement,
    # now including the preflight the user waited through before this answer.
    # The settings the parent resolved when it handed this turn over: an
    # admin's values over the host's own. Fixed for the turn from here, so a
    # change on the admin page applies to the next turn, never halfway
    # through this one.
    return run_turn(
        job,
        base_url=base_url,
        worker_id=handed_worker_id(payload, worker_id),
        # The lease the parent claimed this turn with, so its renewals ask for
        # the same length the claim did.
        lease_ttl=host_settings.get("lease_ttl_s", lease_ttl),
        agent_timeout_s=host_settings.get("agent_timeout_s", agent_timeout_s),
        backend=backend,
        host=host,
        measurement=measurement,
        host_settings=host_settings,
    )
