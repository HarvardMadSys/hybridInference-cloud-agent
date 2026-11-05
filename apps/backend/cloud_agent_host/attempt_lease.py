"""An attempt's lease while its turn runs: renewal, and the receipts retried while it holds.

Heartbeats are how cancellation arrives: a background thread renews the lease
and watches the reply for ``cancel_requested``. Losing the lease (409) is
fatal by design -- an attempt that was superseded must stop rather than race
its replacement.
"""

from __future__ import annotations

import threading
import time
from typing import TYPE_CHECKING, Any

import httpx

from cloud_agent_host.claimed_job import LeaseLost
from cloud_agent_host.control_plane_client import (
    _EVENT_RETRY_MAX_DELAY_S,
    _HEARTBEAT_TIMEOUT_S,
    ControlPlane,
    _log_control_failure,
)
from cloud_agent_host.logging_utils import get_logger
from cloud_agent_host.sandbox_manager_client import SandboxManagerError

if TYPE_CHECKING:
    from collections.abc import Callable

# Log records keep the name they had while all of this was runner.py.
logger = get_logger("cloud_agent_host.runner")


HEARTBEAT_INTERVAL_S = 30.0


# Heartbeats use a short per-request timeout and retry promptly, so a slow
# control request cannot consume the remaining lease in normal cadence sleeps.
# Idempotent lifecycle receipts reuse the same short retry interval, except the
# control plane's admission and settlement, which wait out a restart instead.
_CONTROL_RETRY_INTERVAL_S = 5.0


#: Answers a repeated receipt can change: the answer was lost on the way, or
#: the server was briefly unavailable. Anything else is the receipt refused.
_RECEIPT_RETRY_STATUSES = frozenset({408, 502, 503, 504})


#: Heartbeat answers no retry can change. The worker token and the job it names
#: are fixed for the attempt, so a renewal refused for either is refused every
#: time, and the lease runs out regardless.
_HEARTBEAT_REFUSALS = frozenset({401, 403, 404})


#: The longest lease that still cannot be renewed: each renewal after the first
#: is sent one heartbeat interval after the last and may take a whole request
#: timeout to land, so a lease no longer than that expires first and every
#: attempt is lost by its second beat. The control plane itself only requires a
#: positive number.
_UNRENEWABLE_LEASE_S = HEARTBEAT_INTERVAL_S + _HEARTBEAT_TIMEOUT_S


def _retry_lifecycle_receipt(operation: str, job_id: str, call: Callable[[], Any]) -> Any:
    """Retry only idempotent lifecycle receipts, never prompt delivery."""
    for attempt in range(3):
        try:
            return call()
        except (httpx.TransportError, httpx.HTTPStatusError, SandboxManagerError) as exc:
            status = (
                exc.response.status_code
                if isinstance(exc, httpx.HTTPStatusError)
                else exc.status_code
                if isinstance(exc, SandboxManagerError)
                else None
            )
            if attempt == 2 or (status is not None and status not in _RECEIPT_RETRY_STATUSES):
                raise
            _log_control_failure(operation, job_id, exc)
            time.sleep(_CONTROL_RETRY_INTERVAL_S)
    raise AssertionError("lifecycle retry exhausted without a result")


def _retry_control_receipt(
    operation: str, job_id: str, call: Callable[[], Any], *, heart: Heartbeater
) -> Any:
    """Retry the control plane's admission or settlement receipt while the lease holds.

    A deploy that recreates the control-plane container refuses connections,
    or its proxy answers 502, for longer than three quick tries cover, and
    giving up on either receipt failed the turn over a restart. A settlement
    costs more: however the attempt then ends, by a failed finish or by the
    lease reaper, the control plane marks an admitted turn it never saw settle
    lost, though the sandbox holds it settled. So these back off as event
    uploads do for as long as the lease holds; once the heartbeat has lost it,
    no receipt can land. A control plane that renews the lease all along but
    fails the receipt for a whole lease is not restarting, and is given up on.
    """
    give_up_at: float | None = None
    delay = 1.0
    while True:
        try:
            return call()
        except (httpx.TransportError, httpx.HTTPStatusError) as exc:
            if (
                isinstance(exc, httpx.HTTPStatusError)
                and exc.response.status_code not in _RECEIPT_RETRY_STATUSES
            ):
                raise
            if heart.lease_lost:
                raise LeaseLost(f"lease lost while retrying {operation}") from exc
            if give_up_at is None:
                give_up_at = time.monotonic() + heart.ttl
            if time.monotonic() + delay > give_up_at:
                raise
            _log_control_failure(operation, job_id, exc)
        time.sleep(delay)
        delay = min(delay * 2, _EVENT_RETRY_MAX_DELAY_S)


class Heartbeater:
    """Renews the lease in the background and surfaces cancellation."""

    def __init__(self, control: ControlPlane, *, ttl: float, interval: float) -> None:
        self._control = control
        self._ttl = ttl
        self._interval = interval
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self.cancel_requested = False
        self.lease_lost = False

    @property
    def ttl(self) -> float:
        """How long each renewal extends the lease."""
        return self._ttl

    def start(self) -> None:
        """Begin heartbeating on a daemon thread."""
        self._thread = threading.Thread(target=self._run, daemon=True, name="agent-heartbeat")
        self._thread.start()

    def stop(self) -> None:
        """Stop heartbeating and join."""
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=5)

    def _run(self) -> None:
        # The first renewal goes out at once. The lease runs from the claim,
        # and the process running the turn has already spent part of it on its
        # handover and preflight; a whole interval more let a slow start reach
        # an expired lease, which the control plane refuses to renew.
        delay = 0.0
        last_success = time.monotonic()
        failures = 0
        while not self._stop.wait(delay):
            try:
                reply = self._control.heartbeat(self._ttl)
            except LeaseLost as exc:
                # The fence was lost; this does not imply a replacement exists.
                _log_control_failure("heartbeat_fenced", self._control.job_id, exc)
                self.lease_lost = True
                return
            except Exception as exc:
                refused = (
                    isinstance(exc, httpx.HTTPStatusError)
                    and exc.response.status_code in _HEARTBEAT_REFUSALS
                )
                if refused or time.monotonic() - last_success >= self._ttl:
                    # A refused renewal is refused on every retry, and once the
                    # last acknowledged renewal has run out no request can fit
                    # inside it. Either way the lease is gone, so the agent is
                    # stopped as for a 409 rather than left writing to a
                    # workspace the reaper has already taken back.
                    _log_control_failure(
                        "heartbeat_refused" if refused else "heartbeat_expired",
                        self._control.job_id,
                        exc,
                    )
                    self.lease_lost = True
                    return
                # A transient control-plane blip must not kill a healthy run;
                # the next request still has to fit inside the original lease.
                failures += 1
                delay = min(self._interval, _CONTROL_RETRY_INTERVAL_S)
                # The aggregate retry line below already carries the count and
                # timing, so log the exception detail once per outage instead of
                # once per retry.
                if failures == 1:
                    _log_control_failure("heartbeat", self._control.job_id, exc)
                logger.warning(
                    "agent_heartbeat_retry job_id=%s consecutive_failures=%s "
                    "seconds_since_ack=%.3f retry_in_seconds=%.3f",
                    self._control.job_id,
                    failures,
                    time.monotonic() - last_success,
                    delay,
                )
                continue
            if failures:
                logger.info(
                    "agent_heartbeat_recovered job_id=%s failed_requests=%s seconds_since_ack=%.3f",
                    self._control.job_id,
                    failures,
                    time.monotonic() - last_success,
                )
            failures = 0
            last_success = time.monotonic()
            delay = self._interval
            if reply.get("cancel_requested"):
                # Renewal goes on until the runner stops it: releasing the
                # activation, or draining an interrupted turn, still happens
                # under this lease, and one left to expire meanwhile makes the
                # reaper mark the chat's context lost.
                self.cancel_requested = True
