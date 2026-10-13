"""Getting the deployment's collection configuration onto the machine that collects.

The runner parent process is where a turn is *selected*, so it is where the
configuration has to arrive. This module owns that: it reads the deployment's
current configuration from the control plane, publishes it into this process's
policy store, and hands the same answer to the sandbox manager -- a separate
container that shares no memory with the runner.

Four things are deliberate.

**There is no lease.** A configuration is what it is until the control plane
says otherwise. Timed experiments additionally stop at ``expires_at``; continuous
ones retain the last applied configuration until synchronization returns.

**A version never goes backwards.** Every answer carries the deployment's
configuration version, and an answer older than the one already applied is
ignored by the store. That is the whole anti-rollback protocol: a delayed or
reordered reply cannot put a stopped experiment back into force.

**The sandbox manager is told on every pass, and its answer is not assumed.** The runner
pushes the configuration over the sandbox manager's existing authenticated channel and
records only whether the push was accepted. It does not wait for, invent or
report a confirmation the sandbox manager did not give. It re-sends even when the
configuration has not changed, because the two ways a sandbox manager can be missing one
-- a failed first push, and the sandbox manager's own restart -- are both invisible from
here, and the receiver is idempotent by revision.

**Failure is neither fatal nor silent.** A control plane that cannot be reached
leaves the last configuration in place, which then ends at the experiment's own
deadline. The runner keeps claiming work throughout: profiling observes the
product, it never gates it.
"""

from __future__ import annotations

import json
import logging
import os
import socket
import threading
import uuid
from collections.abc import Mapping
from dataclasses import replace
from datetime import datetime
from typing import Any

import httpx

from cloud_agent_shared.operation_profiling import (
    MODE_ADMIN,
    ControlState,
    ProfilingRun,
    budget_state,
    install_policy_provider,
    parse_run,
    policy_store,
    profile_mode,
)
from cloud_agent_shared.shared_protocol import PROFILING_POLICY_HEADER

logger = logging.getLogger(__name__)

#: How often to read the configuration while an experiment is in force. The
#: page tells an operator to allow up to 30 seconds for a change to land
#: everywhere, and this is what makes that number true.
ACTIVE_INTERVAL_S = 5.0

#: How often to read it when nothing is running. The idle cost of having
#: runtime control at all, stated rather than denied.
IDLE_INTERVAL_S = 30.0

#: How often to try again after a failed read. The last configuration still
#: applies and still expires; this only bounds how long a recovered control
#: plane takes to be noticed.
ERROR_INTERVAL_S = 10.0

#: Request timeout. Shorter than the active interval, so one slow answer cannot
#: make the loop run late.
REQUEST_TIMEOUT_S = 2.0

#: Jitter added to each wait, so a fleet of hosts that started together does not
#: ask in lockstep. A tenth of the interval: enough to spread, small enough not
#: to matter against a 30-second promise.
JITTER_FRACTION = 0.1

#: Bounded retries for the sandbox manager push. The configuration is one small message
#: and the sandbox manager is on the same machine; more than a couple of tries means it
#: is down, and a down sandbox manager has its own path back.
SANDBOX_MANAGER_PUSH_ATTEMPTS = 2


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
        """Return the configuration this process last applied."""
        published = policy_store().published()
        if published is None:
            return ControlState(mode=MODE_ADMIN)
        return published.state


class ProfilingPolicySync:
    """Keeps this host's collection configuration in step with the control plane.

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
        sandbox_manager: Any = None,
    ) -> None:
        """Bind the sync to one host's credential and one sandbox manager client.

        ``sandbox_manager`` is the host's authenticated supervisor client, or ``None``
        when this host has none configured. A host without a sandbox manager still syncs
        its own configuration; it just cannot hand it on.
        """
        self._base_url = base_url.rstrip("/")
        self._credential = credential
        self._host = host
        self._sandbox_manager = sandbox_manager
        self._instance_id = _instance_id()
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._wake = threading.Event()
        self._thread: threading.Thread | None = None
        #: The configuration version this process has applied, and the run it
        #: names. Read by :meth:`state` for diagnostics; nothing upstream
        #: branches on them.
        self._applied_revision = 0
        self._applied_run_id: str | None = None
        #: Whether the last sandbox manager push was accepted, and why not when it was
        #: not. Local diagnostics only.
        self._sandbox_manager_ok: bool | None = None
        self._sandbox_manager_detail = ""
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
        """Stop the loop and close what this object owns."""
        self._stop.set()
        self._wake.set()
        thread, self._thread = self._thread, None
        if thread is not None:
            thread.join(timeout)
        self._client.close()
        # The sandbox manager client was handed over for this loop's exclusive use, so
        # closing it here is the same ownership statement the constructor makes.
        close = getattr(self._sandbox_manager, "close", None)
        if callable(close):
            close()

    def wake(self) -> None:
        """Ask the loop to sync now rather than at the next interval."""
        self._wake.set()

    # -- diagnostics -------------------------------------------------------

    def state(self) -> dict[str, Any]:
        """Return what this host has applied, for logs and local diagnostics."""
        with self._lock:
            return {
                "instance": self._instance_id,
                "applied_revision": self._applied_revision,
                "run_id": self._applied_run_id,
                "sandbox_manager_ok": self._sandbox_manager_ok,
                "sandbox_manager_detail": self._sandbox_manager_detail,
                "budget": budget_state(self._applied_run_id) if self._applied_run_id else {},
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
                # host that silently stops hearing about changes for the life of
                # the process. Keep the last configuration -- it expires on its
                # own -- and come back.
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
        """Read once, apply the answer, hand it on, and say when to read again."""
        if profile_mode() != MODE_ADMIN:
            # Not under Admin control: the deployment's own configuration is the
            # configuration, and this loop must not overwrite it. Sleep long,
            # but keep the thread so a mode change on a later restart is honoured
            # without a new code path.
            return IDLE_INTERVAL_S
        try:
            answer = self._fetch()
        except Exception:
            logger.debug("agent_profiling_policy_unreachable", exc_info=True)
            return ERROR_INTERVAL_S
        # The sandbox manager hears every pass, not only every *change*.
        #
        # A change-only push looks cheaper and is wrong twice over. The first
        # push of a configuration can fail -- the sandbox manager is still starting, the
        # connection was reset -- and if the runner then records the revision as
        # applied, no later pass ever tries again: the experiment runs with the
        # sandbox manager contributing nothing, which shows up as a root record whose
        # container stages are simply missing. And the sandbox manager holds its
        # configuration in memory, so its own restart silently empties it; the
        # runner has no way to learn that except by offering it again.
        #
        # Re-sending is safe because the sandbox manager's route is idempotent by
        # revision: an equal revision answers with what it already holds and
        # writes nothing. This is not an acknowledgement protocol -- the runner
        # does not wait for one, and nothing upstream depends on it -- it is a
        # periodic re-offer of a value the receiver already knows how to ignore.
        self._push_to_sandbox_manager(answer)
        return ACTIVE_INTERVAL_S if answer["run_id"] else IDLE_INTERVAL_S

    def _fetch(self) -> dict[str, Any]:
        """Read this host's configuration and publish it.

        The answer is reduced to a flat, already-validated form here, so no
        later step re-interprets it.
        """
        response = self._client.post(
            f"{self._base_url}/v1/agent/hosts/profiling-policy",
            json={"host": self._host},
            headers={"Authorization": f"Bearer {self._credential}"},
        )
        if response.is_error:
            raise RuntimeError(f"configuration exchange refused with {response.status_code}")
        payload = response.json()
        if not isinstance(payload, dict):
            raise RuntimeError("configuration exchange returned invalid data")
        mode = str(payload.get("mode") or MODE_ADMIN)
        revision = max(0, int(payload.get("revision") or 0))
        run_payload = payload.get("run")
        parsed: ProfilingRun | None = None
        if mode == MODE_ADMIN and isinstance(run_payload, Mapping):
            parsed = _parse_policy_run(run_payload)
        with self._lock:
            previous_run = self._applied_run_id
            previous_revision = self._applied_revision
        published = policy_store().publish(
            ControlState(
                mode=mode,
                config_version=revision,
                run=parsed,
                stop_reason="" if parsed is not None else "stopped",
            ),
            run=parsed,
        )
        applied = published.state
        with self._lock:
            self._applied_revision = applied.config_version
            self._applied_run_id = parsed.run_id if parsed is not None else None
        return {
            "mode": applied.mode,
            "revision": applied.config_version,
            "run_id": parsed.run_id if parsed is not None else None,
            "run": run_payload if parsed is not None else None,
            "changed": applied.config_version != previous_revision
            or (parsed.run_id if parsed is not None else None) != previous_run,
        }

    def _push_to_sandbox_manager(self, answer: Mapping[str, Any]) -> None:
        """Hand the sandbox manager the same configuration, and record what it answered."""
        if self._sandbox_manager is None:
            with self._lock:
                self._sandbox_manager_ok = None
                self._sandbox_manager_detail = "this host has no sandbox manager client"
            return
        message = {
            "mode": answer["mode"],
            "revision": answer["revision"],
            "run": answer["run"],
        }
        for attempt in range(1, SANDBOX_MANAGER_PUSH_ATTEMPTS + 1):
            try:
                accepted = self._sandbox_manager.apply_profiling_policy(message)
            except Exception as exc:
                if attempt == SANDBOX_MANAGER_PUSH_ATTEMPTS:
                    with self._lock:
                        self._sandbox_manager_ok = False
                        self._sandbox_manager_detail = f"{type(exc).__name__}: {exc}"
                    logger.warning(
                        "agent_profiling_sandbox_manager_policy_failed",
                        extra={"event": "agent_profiling_sandbox_manager_policy_failed"},
                        exc_info=True,
                    )
                continue
            with self._lock:
                self._sandbox_manager_ok = bool(accepted.get("accepted", True))
                self._sandbox_manager_detail = ""
            return


def _parse_policy_run(run: Mapping[str, Any]) -> ProfilingRun | None:
    """Build a run descriptor from the configuration a control plane sent.

    Rebuilt through the collector's own parser, so a host validates a pushed
    configuration with exactly the code that validates a deployment's static
    descriptor. A message the parser refuses is a configuration no producer
    could collect for, and it is reported rather than half-applied.
    """
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
    if run.get("continuous") is True:
        if "expires_at" not in run:
            return None
        descriptor.update(continuous=True, expires_at=run.get("expires_at"), max_operations=0)
    try:
        parsed = parse_run(json.dumps(descriptor), allow_continuous=True)
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
    """Return a per-wait fraction in ``[0, 1)`` for the jitter."""
    return (uuid.uuid4().int % 1000) / 1000.0


__all__ = [
    "ACTIVE_INTERVAL_S",
    "ERROR_INTERVAL_S",
    "IDLE_INTERVAL_S",
    "JITTER_FRACTION",
    "PROFILING_POLICY_HEADER",
    "REQUEST_TIMEOUT_S",
    "SANDBOX_MANAGER_PUSH_ATTEMPTS",
    "PolicyProvider",
    "ProfilingPolicySync",
]
