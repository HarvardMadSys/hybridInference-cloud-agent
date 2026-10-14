"""The sandbox manager's own copy of the deployment's profiling policy.

The sandbox manager is a separate container from the runner. It shares no memory with it
and holds no host credential, so it cannot learn the deployment's control state
the way the runner does -- and it must not be given a credential to try, because
the design keeps the host credential out of the sandbox side entirely.

So the runner tells it. This module is the receiving half: it accepts a policy
message over the sandbox manager's existing authenticated channel, validates it,
and publishes it into the sandbox manager's own policy store. That store is what
``start_stage_recording`` reads for a *new* selection, so a call that was not
already selected upstream contributes a fragment only while this deployment is
collecting for its run.

Three rules. The first says who may tell the sandbox manager anything, and what
a Stop deliberately does not end; the other two close ways the sandbox manager
could keep measuring after one:

**A policy only ever comes from the runner, and a selection only from a token
holder.** The route sits behind the same shared-token guard as every other
sandbox manager route, and the message is bounded and re-parsed rather than
trusted. The policy does not gate every profiling *header*, though. A header
that names an operation without a manifest is a new selection, honoured only
when this sandbox manager's policy says that run is in force and selects it. A
header that carries the run's manifest is a continuation (see
``start_stage_recording``): under ``MODE=admin`` it is honoured whatever this
policy says -- after a Stop, or for a run this sandbox manager was never told
about -- bounded only by the manifest's own deadline, which a continuous run
does not have. That is on purpose: it is how the later stages of work selected
before a Stop still land. What keeps a forged continuation out is the channel,
not this policy: only the control plane and the runner hold the sandbox
manager token, each builds the header from a selection it made itself, and
neither a browser nor a sandbox holds that token.

**Revisions only move forwards.** A lower revision is refused rather than
applied, so a delayed or reordered push cannot resurrect a configuration the
runner has already moved past. An equal revision is idempotent and answers with
what is already held. This is the same anti-rollback rule the host applies to
the control plane, and it is the only ordering this channel needs.

**Everything is bounded.** The message crosses a process boundary and is not
trusted: a bounded number of operations, bounded strings, and a deadline that
can only ever be earlier than the one stated. A sandbox manager is not allowed to extend
an experiment on its own.
"""

from __future__ import annotations

import json
import logging
import threading
from collections.abc import Mapping
from datetime import datetime, timezone
from typing import Any

from fastapi import HTTPException

from cloud_agent_shared.operation_profiling import (
    MODE_ADMIN,
    MODE_DISABLED,
    MODE_ENVIRONMENT,
    RUN_ID_PATTERN,
    TARGET_KEYS,
    ControlState,
    ProfilingRun,
    parse_run,
    policy_store,
)
from cloud_agent_shared.sandbox_manager_compat import SANDBOX_MANAGER_PRODUCER

logger = logging.getLogger(__name__)

#: Producer name the sandbox manager reports under. Declared here because the policy
#: endpoint is where the sandbox manager learns which experiment it may collect for.
PRODUCER = SANDBOX_MANAGER_PRODUCER

#: Bounds on an incoming policy message. It crosses a container boundary.
MAX_OPERATIONS = 32
MAX_TARGET_FIELDS = 8
MAX_ID_CHARS = 128
MAX_DETAIL_CHARS = 200

#: Serializes policy application inside one sandbox manager process. Two pushes must not
#: race each other into two different answers about which revision is applied.
_POLICY_LOCK = threading.Lock()

#: What this sandbox manager has applied. Local state, read back by the runner that
#: pushed it; nothing upstream depends on it and it is not an acknowledgement
#: anybody waits for.
_APPLIED: dict[str, Any] = {
    "revision": 0,
    "run_id": None,
    "applied_at": None,
}


def applied_state() -> dict[str, Any]:
    """Return what this sandbox manager has applied, for the runner's own diagnostics."""
    return {
        "revision": int(_APPLIED["revision"]),
        "run_id": _APPLIED["run_id"],
        "applied_at": _APPLIED["applied_at"],
    }


def reset_applied_state() -> None:
    """Forget the applied policy. Tests call this."""
    _APPLIED.update({"revision": 0, "run_id": None, "applied_at": None})


def install_policy_provider() -> None:
    """Make this sandbox manager's collector read through the pushed policy store.

    Called once, when the sandbox manager application is built. Until a policy arrives
    the store holds nothing, which reads as "not collecting" -- the safe
    direction, and the reason a sandbox manager that starts before its runner does not
    briefly measure for an experiment it has not been told about.
    """
    from cloud_agent_shared.operation_profiling import install_policy_provider as install

    class _Pushed:
        """Serves the collector from the store the policy endpoint writes."""

        def snapshot(self) -> ControlState:
            """Return the last policy this sandbox manager accepted."""
            published = policy_store().published()
            if published is None:
                return ControlState(mode=MODE_ADMIN)
            return published.state

    install(_Pushed())


def apply_received_policy(payload: dict[str, Any]) -> dict[str, Any]:
    """Apply one profiling policy this host's runner pushed.

    Idempotent by revision: the same revision answers with what is already held,
    and a lower one is refused. That is what makes a retried push harmless and a
    delayed one unable to undo a Stop.

    Authentication happens at the route, which is the sandbox manager's own
    ``authenticate`` dependency -- the same shared-token check every other sandbox manager
    route uses. This function is deliberately about the message, so the two
    cannot drift into different ideas of who may send one.
    """
    revision = _bounded_int(payload.get("revision"))
    if revision is None or revision < 0:
        raise HTTPException(status_code=422, detail="revision must be a non-negative integer")
    mode = str(payload.get("mode") or MODE_ADMIN).strip().lower()
    if mode not in {MODE_ADMIN, MODE_DISABLED, MODE_ENVIRONMENT}:
        raise HTTPException(status_code=422, detail="unknown profiling mode")
    with _POLICY_LOCK:
        current = int(_APPLIED["revision"])
        if revision < current:
            # A push that is behind what this sandbox manager already applied. Refusing
            # rather than applying is the difference between a Stop that holds
            # and a Stop an out-of-order retry can undo.
            raise HTTPException(
                status_code=409,
                detail=f"policy revision {revision} is behind the applied {current}",
            )
        if revision == current and _APPLIED["applied_at"] is not None:
            return {**applied_state(), "accepted": True, "changed": False}
        run = _parse_run_payload(payload.get("run")) if mode == MODE_ADMIN else None
        if mode == MODE_ADMIN and run is None and payload.get("run") is not None:
            raise HTTPException(status_code=422, detail="the run descriptor is unusable")
        if mode == MODE_ADMIN:
            _publish(revision=revision, run=run)
        else:
            # A deployment not under Admin control is not this channel's to
            # configure: the sandbox manager keeps its own environment's answer, and a
            # policy that said otherwise would be the runner overruling a
            # deployment setting.
            policy_store().publish(ControlState(mode=mode, config_version=revision))
        _APPLIED.update(
            {
                "revision": revision,
                "run_id": run.run_id if run is not None else None,
                "applied_at": datetime.now(timezone.utc).isoformat(),
            }
        )
    return {**applied_state(), "accepted": True, "changed": True}


def _publish(*, revision: int, run: ProfilingRun | None) -> None:
    """Publish one accepted policy into the sandbox manager's store."""
    store = policy_store()
    if run is None:
        # A configuration with no run means "stop selecting". It carries the
        # revision that says so, so a fragment already in flight can be told
        # from one that arrived after -- and work already selected is not
        # touched by it.
        store.publish(
            ControlState(
                mode=MODE_ADMIN,
                config_version=revision,
                run=None,
                stop_reason="stopped",
            )
        )
        return
    store.publish(
        ControlState(
            mode=MODE_ADMIN,
            config_version=revision,
            run=run,
        ),
        run=run,
    )


def _parse_run_payload(value: Any) -> ProfilingRun | None:
    """Return the run descriptor a policy message states, or ``None``.

    Rebuilt through the collector's own parser so a sandbox manager validates a policy
    with exactly the code that validates a deployment's static descriptor. The
    message was already bounded here, but the parser is what decides whether the
    result is an experiment at all.
    """
    if not isinstance(value, Mapping):
        return None
    run_id = str(value.get("run_id") or "").strip()
    if not run_id or len(run_id) > MAX_ID_CHARS or RUN_ID_PATTERN.fullmatch(run_id) is None:
        return None
    raw_operations = value.get("operations")
    if not isinstance(raw_operations, list) or not raw_operations:
        return None
    operations = [str(item)[:MAX_DETAIL_CHARS] for item in raw_operations[:MAX_OPERATIONS]]
    target: dict[str, str] = {}
    raw_target = value.get("target")
    if isinstance(raw_target, Mapping):
        for key, item in list(raw_target.items())[:MAX_TARGET_FIELDS]:
            name = str(key)
            if name not in TARGET_KEYS:
                return None
            target[name] = str(item)[:MAX_DETAIL_CHARS]
    expires_at = value.get("expires_at")
    if isinstance(expires_at, datetime):
        expires_at_text = expires_at.astimezone(timezone.utc).isoformat()
    else:
        expires_at_text = str(expires_at or "")
    descriptor = {
        "run_id": run_id,
        "operations": operations,
        "expires_at": expires_at_text,
        "config_version": _bounded_int(value.get("config_version")) or 1,
        "max_operations": _bounded_int(value.get("max_operations")) or 100,
        "target": target,
        "admission_instances": [],
    }
    if value.get("continuous") is True:
        if "expires_at" not in value:
            return None
        descriptor.update(continuous=True, expires_at=value.get("expires_at"), max_operations=0)
    try:
        return parse_run(json.dumps(descriptor), allow_continuous=True)
    except ValueError:
        return None


def _bounded_int(value: Any) -> int | None:
    """Return an integer field from an untrusted message, or ``None``."""
    if value is None or isinstance(value, bool):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


__all__ = [
    "MAX_ID_CHARS",
    "MAX_OPERATIONS",
    "MAX_TARGET_FIELDS",
    "PRODUCER",
    "applied_state",
    "apply_received_policy",
    "install_policy_provider",
    "reset_applied_state",
]
