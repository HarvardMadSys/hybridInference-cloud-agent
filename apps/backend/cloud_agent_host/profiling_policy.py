"""Getting the deployment's profiling policy onto the machine that collects.

The runner parent process is where a turn is *selected*, so it is where the
policy has to arrive. This module owns that: it asks the control plane what to
collect, applies the answer to its own process's policy store, and hands the
same answer to the workspace broker, which is a separate container that shares
no memory with the runner.

Four things are deliberate.

**The broker's confirmation is not assumed.** The runner pushes the policy over
the broker's existing authenticated channel and records *what the broker said*,
not what it sent. A broker that never answered is reported as unconfirmed, so
the Admin page can say "the runner is collecting, the broker is not" instead of
rounding the pair up to "collecting".

**The lease is a local deadline.** Every answer carries a reduced lease; the
deadline is computed once, against this process's monotonic clock, and never
extended by re-reading a reply. That is what makes a control plane that goes
away stop this host rather than leaving it collecting until the experiment's
original end.

**Nothing here decides what to collect.** The policy store is the collector's
own seam, and this module only publishes into it. A second decision point --
"the runner thinks profiling should be on" -- is how two processes end up
disagreeing about which experiment is in force.

**Failure is not fatal and not silent.** A control plane that cannot be reached
leaves the last policy in place, which then expires on its own. The runner keeps
claiming work throughout: profiling observes the product, it never gates it.
"""

from __future__ import annotations

import json
import logging
import os
import socket
import threading
import time
import uuid
from collections.abc import Mapping
from dataclasses import replace
from datetime import datetime, timezone
from typing import Any

import httpx

from cloud_agent_profiling.operation_profiling import (
    MODE_ADMIN,
    ControlState,
    ProfilingRun,
    budget_state,
    install_policy_provider,
    parse_run,
    policy_store,
    profile_mode,
)
from cloud_agent_shared.shared_protocol import (
    PROFILING_CONTROL_CAPABILITY,
    PROFILING_POLICY_HEADER,
)

#: How long a policy answer may be acted on without a fresh one, reduced by the
#: request that carried it. The control plane states the same ceiling; this is
#: the host-side default for an answer that does not, and is deliberately short:
#: a lease is the maximum time an isolated node may keep collecting, so guessing
#: high guesses in the unsafe direction.
DEFAULT_LEASE_SECONDS = 15.0

logger = logging.getLogger(__name__)

#: How often to ask while a run is in force. The design's contract is that a
#: Stop stops adding samples within twenty seconds, and this is a quarter of it.
ACTIVE_INTERVAL_S = 5.0

#: How often to ask when nothing is running. The idle cost of having runtime
#: control at all, stated rather than denied.
IDLE_INTERVAL_S = 30.0

#: How often to ask after a failed request. The last policy still applies and
#: still expires; this only bounds how long a recovered control plane takes to
#: be noticed.
ERROR_INTERVAL_S = 10.0

#: Request timeout. Shorter than the active interval so one slow answer cannot
#: make the loop run late, and the design's stated network timeout.
REQUEST_TIMEOUT_S = 2.0

#: Jitter added to each wait, so a fleet of hosts that started together does not
#: ask in lockstep. A tenth of the interval: enough to spread, small enough not
#: to eat into the Stop deadline.
JITTER_FRACTION = 0.1

#: Bounded retries for the broker push. The policy is one small message and the
#: broker is on the same machine; more than a couple of tries means it is down,
#: and a down broker has its own path back.
BROKER_PUSH_ATTEMPTS = 2


def _instance_id() -> str:
    """Return a stable identity for this runner process."""
    try:
        host = socket.gethostname()
    except OSError:  # pragma: no cover - hostname lookup is effectively local
        host = "unknown-host"
    return f"runner:{host}:{os.getpid()}:{uuid.uuid4().hex[:8]}"


class PolicyProvider:
    """Serves the collector from this process's policy store.

    Installed once, at startup. The store is what actually holds the state; this
    exists so the collector has one seam it reads through rather than importing
    the host agent's transport.
    """

    def snapshot(self) -> ControlState:
        """Return the control state this process last applied."""
        published = policy_store().published()
        if published is None:
            return ControlState(mode=MODE_ADMIN)
        return published.policy


class ProfilingPolicySync:
    """Keeps this host's profiling policy in step with the control plane.

    One instance per runner process. :meth:`start` spawns one daemon thread; the
    thread is the only thing that ever writes the policy store, so a reader
    never sees a half-applied answer.
    """

    def __init__(
        self,
        *,
        base_url: str,
        credential: str,
        host: str,
        broker: Any = None,
        capability: str = PROFILING_CONTROL_CAPABILITY,
    ) -> None:
        """Bind the sync to one host's credential and one broker client.

        ``broker`` is the existing authenticated supervisor client, or ``None``
        when this host has none. A host without a broker still syncs its own
        policy: it just reports the broker as unconfirmed, which is the truth.
        """
        self._base_url = base_url.rstrip("/")
        self._credential = credential
        self._host = host
        self._broker = broker
        self._capability = capability
        self._participant_id = _instance_id()
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._wake = threading.Event()
        self._thread: threading.Thread | None = None
        #: What this process has applied. Read by the reporter below and by the
        #: child handoff, which carries the revision the child must match.
        self._applied_revision = 0
        self._applied_run_id: str | None = None
        #: What the broker last confirmed, and why not when it did not.
        self._broker_revision: int | None = None
        self._broker_detail = ""
        self._interval = IDLE_INTERVAL_S
        self._client = httpx.Client(timeout=REQUEST_TIMEOUT_S)

    # -- lifecycle ---------------------------------------------------------

    def install(self) -> None:
        """Make this process's collector read through the policy store.

        Called before the loop starts, so there is no window in which the
        collector is live but ungoverned. Until the first answer arrives the
        store holds nothing, which reads as "not collecting" -- the safe
        direction, and the one that makes a restart a Stop rather than a
        continuation.
        """
        install_policy_provider(PolicyProvider())

    def start(self) -> None:
        """Start the background loop."""
        if self._thread is not None:
            return
        self._thread = threading.Thread(
            target=self._run, name="agent-profiling-policy", daemon=True
        )
        self._thread.start()

    def stop(self, timeout: float = 2.0) -> None:
        """Stop the loop and close its connection pool."""
        self._stop.set()
        self._wake.set()
        thread, self._thread = self._thread, None
        if thread is not None:
            thread.join(timeout)
        self._client.close()

    def wake(self) -> None:
        """Ask the loop to sync now rather than at the next interval."""
        self._wake.set()

    # -- reporting ---------------------------------------------------------

    def report(self) -> dict[str, Any]:
        """Return what this host has applied, for the heartbeat to carry.

        Sent on every beat so the Admin page can distinguish "the host has not
        heard about this revision" from "the host is not there". Deliberately
        reports the *applied* revision rather than the one it was sent: a host
        that failed to apply a policy must not look like one that did.
        """
        with self._lock:
            revision = self._applied_revision
            run_id = self._applied_run_id
            broker_revision = self._broker_revision
            broker_detail = self._broker_detail
        return {
            "participant_id": self._participant_id,
            "applied_revision": revision,
            "run_id": run_id,
            "capability": self._capability,
            "detail": "",
            "budget": budget_state(run_id) if run_id else {},
            "broker_revision": broker_revision,
            "broker_capability": self._capability if broker_revision is not None else None,
            "broker_detail": broker_detail,
        }

    @property
    def interval_s(self) -> float:
        """Return the interval the last pass settled on."""
        return self._interval

    # -- the loop ----------------------------------------------------------

    def _run(self) -> None:
        """Sync until stopped, never letting an exception end the thread."""
        while not self._stop.is_set():
            try:
                interval = self._sync_once()
            except Exception:
                # A thread that dies is never restarted, and the cost would be a
                # host that silently stops hearing about Stops for the life of
                # the process. Keep the last policy -- it expires on its own --
                # and come back.
                logger.warning("agent_profiling_policy_sync_crashed", exc_info=True)
                interval = ERROR_INTERVAL_S
            self._interval = interval
            self._wait(interval)

    def _wait(self, interval: float) -> None:
        """Sleep, cut short by :meth:`wake` or :meth:`stop`."""
        jitter = interval * JITTER_FRACTION * _jitter_fraction()
        self._wake.wait(interval + jitter)
        self._wake.clear()

    def _sync_once(self) -> float:
        """Ask once, apply the answer, and say when to ask again."""
        if profile_mode() != MODE_ADMIN:
            # Not under Admin control: the deployment's own configuration is
            # the policy, and this loop must not overwrite it. Sleep long, but
            # keep the thread so a mode change on a later restart is honoured
            # without a new code path.
            return IDLE_INTERVAL_S
        try:
            answer = self._fetch()
        except Exception:
            # The last policy still applies and still expires. Retrying soon
            # bounds how long a recovered control plane takes to be noticed,
            # without treating an outage as a reason to stop the thread.
            logger.debug("agent_profiling_policy_unreachable", exc_info=True)
            return ERROR_INTERVAL_S
        if answer is None:  # pragma: no cover - _fetch raises rather than returning None
            return ERROR_INTERVAL_S
        # The broker hears every answer, including the one that says "no run".
        # A broker that kept its own copy would keep contributing fragments for
        # an experiment the runner has already left.
        self._push_to_broker(answer)
        return ACTIVE_INTERVAL_S if answer["run_id"] else IDLE_INTERVAL_S

    def _fetch(self) -> dict[str, Any] | None:
        """Ask the control plane for this host's policy, and apply the answer.

        Returns ``None`` when nothing could be read, which the caller turns into
        a short retry. The answer is reduced to a flat, already-validated form
        here -- the lease becomes a local monotonic deadline exactly once -- so
        no later step re-interprets a wall clock or a round trip.
        """
        body = {"profiling": self.report()}
        response = self._client.post(
            f"{self._base_url}/v1/agent/hosts/profiling-policy",
            json=body,
            headers={"Authorization": f"Bearer {self._credential}"},
        )
        if response.is_error:
            raise RuntimeError(f"policy exchange refused with {response.status_code}")
        payload = response.json()
        if not isinstance(payload, dict):
            raise RuntimeError("policy exchange returned invalid data")
        mode = str(payload.get("mode") or MODE_ADMIN)
        revision = int(payload.get("revision") or 0)
        deadline = lease_deadline_mono(payload)
        run_payload = payload.get("run")
        parsed: ProfilingRun | None = None
        if mode == MODE_ADMIN and isinstance(run_payload, Mapping):
            parsed = _parse_policy_run(run_payload)
        self._publish(mode=mode, revision=revision, run=parsed, deadline=deadline)
        with self._lock:
            self._applied_revision = revision
            self._applied_run_id = parsed.run_id if parsed is not None else None
        return {
            "mode": mode,
            "revision": revision,
            "run_id": parsed.run_id if parsed is not None else None,
            "deadline_mono": deadline,
            "run": run_payload,
        }

    def _publish(
        self, *, mode: str, revision: int, run: ProfilingRun | None, deadline: float
    ) -> None:
        """Replace this process's whole policy snapshot, in one swap.

        The store is replaced rather than mutated, so a reader never sees the
        new run id beside the previous run's operations or deadline -- the torn
        snapshot that would let a Stop be half-applied.
        """
        policy_store().publish(
            ControlState(
                mode=mode,
                control_revision=revision,
                run=run,
                authority_epoch=datetime.now(timezone.utc).timestamp(),
                stop_reason="" if run is not None else "stopped",
                authorization_ttl_s=max(0.0, deadline - time.monotonic()),
            ),
            run=run,
        )

    def _push_to_broker(self, answer: Mapping[str, Any]) -> None:
        """Hand the broker the same policy, and record what it confirmed."""
        if self._broker is None:
            with self._lock:
                self._broker_revision = None
                self._broker_detail = "this host has no workspace broker client"
            return
        message = _broker_policy_message(answer)
        if message is None:
            return
        for attempt in range(1, BROKER_PUSH_ATTEMPTS + 1):
            try:
                confirmed = self._broker.apply_profiling_policy(message)
            except Exception as exc:
                if attempt == BROKER_PUSH_ATTEMPTS:
                    with self._lock:
                        self._broker_revision = None
                        self._broker_detail = f"broker did not confirm: {type(exc).__name__}"
                    logger.warning(
                        "agent_profiling_broker_policy_failed",
                        extra={"event": "agent_profiling_broker_policy_failed"},
                        exc_info=True,
                    )
                continue
            revision = confirmed.get("revision") if isinstance(confirmed, Mapping) else None
            with self._lock:
                self._broker_revision = int(revision) if isinstance(revision, int) else None
                self._broker_detail = ""
            return


def lease_deadline_mono(reply: Mapping[str, Any], *, now: float | None = None) -> float:
    """Return a local monotonic deadline for one policy answer.

    Every term is measured against an instant the control plane chose, so this
    process never has to trust its own wall clock against the server's -- and it
    never compares two machines' monotonic values, which mean nothing to each
    other.

    Two ceilings apply and the earlier one wins: the lease (how long the control
    plane stands behind this answer) and the run's own remaining life (how long
    the experiment was asked to last). Both are re-anchored to this process's
    clock at receipt, so an answer delayed in flight cannot buy time it never
    had. That is the whole of the offline guarantee: a node that hears nothing
    more stops at this instant.
    """
    moment = time.monotonic() if now is None else now
    lease = reply.get("lease")
    seconds = 0.0
    issued = None
    if isinstance(lease, Mapping):
        try:
            seconds = float(lease.get("seconds") or 0.0)
        except (TypeError, ValueError):
            seconds = 0.0
        issued = _parse_instant(lease.get("issued_at"))
    if issued is None or seconds <= 0:
        seconds = 0.0
        issued = _parse_instant(reply.get("server_time"))
    if issued is None:
        return moment
    server_now = _parse_instant(reply.get("server_time")) or issued
    # Everything is stated in the server's own frame: ``issued`` is when the
    # lease started and ``server_now`` when the answer was assembled, so their
    # difference is lease time already spent -- measured without either
    # machine's wall clock having to agree with the other's.
    already_spent = max(0.0, (server_now - issued).total_seconds())
    deadline = moment + max(0.0, seconds - already_spent)
    run = reply.get("run")
    if isinstance(run, Mapping):
        expires_at = _parse_instant(run.get("expires_at"))
        if expires_at is not None:
            remaining = (expires_at - server_now).total_seconds()
            deadline = min(deadline, moment + max(0.0, remaining))
    return deadline


def _parse_instant(value: Any) -> datetime | None:
    """Parse an ISO-8601 instant, tolerating a trailing ``Z``."""
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip()
    normalized = text[:-1] + "+00:00" if text.endswith("Z") else text
    try:
        moment = datetime.fromisoformat(normalized)
    except ValueError:
        return None
    return moment if moment.tzinfo else moment.replace(tzinfo=timezone.utc)


def _broker_policy_message(answer: Mapping[str, Any]) -> dict[str, Any] | None:
    """Return the message a broker needs to mirror this policy.

    The broker is told the *authorization*, not just the run: run id, operations,
    target, the absolute end, and the control revision. It needs all of them
    because it validates an incoming selection against its own copy -- a broker
    that only knew "a run is on" could not refuse a fragment for a different one.
    """
    run = answer.get("run")
    revision = int(answer.get("revision") or 0)
    if not isinstance(run, Mapping):
        return {
            "mode": MODE_ADMIN,
            "revision": revision,
            "run": None,
        }
    expires_at = run.get("expires_at")
    return {
        "mode": MODE_ADMIN,
        "revision": revision,
        "run": {
            "run_id": str(run.get("run_id") or ""),
            "operations": [str(item) for item in run.get("operations") or ()],
            "target": {str(key): str(value) for key, value in (run.get("target") or {}).items()},
            "expires_at": (
                expires_at.isoformat()
                if isinstance(expires_at, datetime)
                else str(expires_at or "")
            ),
            "config_version": int(run.get("config_version") or 1),
            "max_operations": int(run.get("max_operations") or 100),
        },
    }


def _parse_policy_run(run: Mapping[str, Any]) -> ProfilingRun | None:
    """Build a run descriptor from the policy a control plane sent."""
    expires_at = run.get("expires_at")
    descriptor = {
        "run_id": str(run.get("run_id") or ""),
        "operations": [str(item) for item in run.get("operations") or ()],
        "expires_at": (
            expires_at.isoformat() if isinstance(expires_at, datetime) else str(expires_at or "")
        ),
        "config_version": int(run.get("config_version") or 1),
        "max_operations": int(run.get("max_operations") or 100),
        "target": {str(key): str(value) for key, value in (run.get("target") or {}).items()},
        "admission_instances": [],
    }
    try:
        parsed = parse_run(json.dumps(descriptor))
    except ValueError:
        logger.warning(
            "agent_profiling_policy_descriptor_unusable",
            extra={"event": "agent_profiling_policy_descriptor_unusable"},
        )
        return None
    # The source travels into every measurement this host produces, so a reader
    # can tell an administrator's experiment from a static descriptor's.
    return replace(parsed, source="admin")


def _jitter_fraction() -> float:
    """Return a stable per-process fraction in ``[0, 1)`` for the jitter."""
    return (uuid.uuid4().int % 1000) / 1000.0


__all__ = [
    "ACTIVE_INTERVAL_S",
    "BROKER_PUSH_ATTEMPTS",
    "DEFAULT_LEASE_SECONDS",
    "ERROR_INTERVAL_S",
    "IDLE_INTERVAL_S",
    "JITTER_FRACTION",
    "PROFILING_POLICY_HEADER",
    "REQUEST_TIMEOUT_S",
    "PolicyProvider",
    "ProfilingPolicySync",
    "lease_deadline_mono",
]
