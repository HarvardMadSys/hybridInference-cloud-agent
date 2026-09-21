"""Handing one host the profiling policy it is allowed to act on.

The control plane has exactly one way to tell a host agent what to collect, and
this is it: the policy rides the authenticated host heartbeat, and a dedicated
read answers the same question between beats. Two entry points, one builder, so
a host cannot get two different answers to the same question.

Four properties are why the answer looks like this.

**The lease is reduced before it is sent.** The host must never extend a lease
by re-reading it, so the number in the answer is already ``ttl`` minus the time
spent building it. The host anchors it to its own monotonic clock and stops
there whether or not another answer ever arrives. That is what makes an isolated
node stop collecting by itself, and why a Stop does not depend on reaching it.

**A host is told its own policy.** The route authenticates the caller, and the
answer is computed for *that* host -- its own broker, its own reported revision.
A host cannot ask about another machine, because it cannot name one.

**A revision is a command, including the command to stop.** A policy whose run
is null is not "no news": it means the control plane is at this revision and is
not collecting for it, which is a Stop for a host that applied an earlier one.

**Nothing here is authoritative for a process that cannot read it.** A
deployment not under Admin control states its mode and leaves the host on its
own configuration; the host applies no run, and nothing is raised, because
"this deployment is configured statically" is a normal state rather than a
failure.
"""

from __future__ import annotations

import time
from collections.abc import Mapping
from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter, Depends, Request, Response

from cloud_agent.api.hosts import authenticate_host
from cloud_agent.logging_utils import get_logger
from cloud_agent.profiling_control_config import profiling_control_deployment
from cloud_agent_profiling.operation_profiling import (
    MODE_ADMIN,
    profile_mode,
)
from cloud_agent_shared.shared_protocol import PROFILING_CONTROL_CAPABILITY

logger = get_logger(__name__)

#: How long an authorization handed to a host may be acted on without renewal.
#: The design's ceiling for a node that loses contact with the control plane.
POLICY_LEASE_SECONDS = 15.0

#: The participant kind a runner reports as. Its broker is a separate process on
#: the same machine and reports under its own kind.
KIND_RUNNER = "runner"
KIND_BROKER = "broker"

#: How long a participant row survives without a fresh confirmation. Mirrors
#: the application service's own TTL; stated here so a control plane with no
#: control row can still answer without importing the service.
PARTICIPANT_TTL_SECONDS = 300

router = APIRouter(prefix="/v1/agent/hosts", tags=["host-profiling"])


@router.post("/profiling-policy")
async def read_host_profiling_policy(
    request: Request,
    response: Response,
    payload: dict[str, Any] | None = None,
    host: dict[str, Any] = Depends(authenticate_host),
) -> dict[str, Any]:
    """Return the profiling policy this host must apply.

    A dedicated read so a Start or a Stop reaches a host that is between beats.
    It takes the same credential the beat does, so the host needs no second
    secret, and the sandbox -- which never holds the credential -- can reach
    nothing here.

    The host is resolved by the credential and never named by the request, which
    is what makes it impossible to ask about another machine: an answer is
    computed for whoever authenticated.
    """
    response.headers["Cache-Control"] = "no-store"
    # The body is the same envelope the beat carries: ``{"profiling": {...}}``.
    # One shape for one report, so the two channels a host can use cannot drift
    # into two spellings of the same facts.
    reported = payload or {}
    inner = reported.get("profiling")
    return await build_policy_for_host(
        host=host,
        reported=dict(inner) if isinstance(inner, Mapping) else reported,
        store=getattr(request.app.state, "storage", None),
    )


async def build_policy_for_host(
    *,
    host: Mapping[str, Any],
    reported: Mapping[str, Any],
    store: Any,
) -> dict[str, Any]:
    """Build the policy answer for one host, and record what it reported.

    Recording happens here rather than in the caller so that every path which
    answers a host also records it: a participant table that only filled in on
    the dedicated read would leave the page blind for the deployment's normal
    steady state, which is the beat.
    """
    started = time.monotonic()
    now = datetime.now(timezone.utc)
    mode = profile_mode()
    name = str(host.get("host") or "")
    if store is None:
        # A control plane without a database cannot state a control revision,
        # so it states its mode and nothing else. Reporting an error here would
        # make a host retry a question that has no answer yet.
        return _empty_policy(mode=mode, revision=0, now=now)
    from cloud_agent.application.profiling_control import ProfilingControlService
    from cloud_agent.workers.profiling_control import AUTHORIZATION_TTL_S

    service = ProfilingControlService(
        control=store.profiling_control,
        deployment=profiling_control_deployment(),
        host_pool=store.host_pool,
    )
    snapshot = await service.control_snapshot(now=now)
    revision = int(snapshot.get("revision") or 0)
    run = snapshot.get("run")
    broker_revision = _as_int(reported.get("broker_revision"))
    applied = _as_int(reported.get("applied_revision"))
    ttl = int(snapshot.get("participant_ttl_seconds") or PARTICIPANT_TTL_SECONDS)
    if mode == MODE_ADMIN:
        # Only Admin mode has a control row to report against. The other modes
        # still answer, so a host can tell "this deployment is configured
        # statically" from "this deployment is unreachable".
        await store.profiling_control.record_participant(
            deployment=service.deployment,
            participant_id=str(reported.get("participant_id") or f"runner:{name}"),
            kind=KIND_RUNNER,
            host=name or None,
            run_id=str(run["run_id"]) if run else None,
            applied_revision=applied if applied is not None else -1,
            capability=str(reported.get("capability") or ""),
            state="applied" if _supports_control(reported) else "unsupported",
            detail=str(reported.get("detail") or ""),
            budget=_as_mapping(reported.get("budget")),
            ttl_seconds=ttl,
            now=now,
        )
        if broker_revision is not None:
            await store.profiling_control.record_participant(
                deployment=service.deployment,
                participant_id=f"broker:{name}",
                kind=KIND_BROKER,
                host=name or None,
                run_id=str(run["run_id"]) if run else None,
                applied_revision=broker_revision,
                capability=str(reported.get("broker_capability") or ""),
                state="applied",
                detail=str(reported.get("broker_detail") or ""),
                budget={},
                ttl_seconds=ttl,
                now=now,
            )
    policy_run = None
    if mode == MODE_ADMIN and run is not None:
        policy_run = {
            "run_id": str(run["run_id"]),
            "operations": [str(item) for item in run.get("operations") or ()],
            "target": {str(key): str(value) for key, value in (run.get("target") or {}).items()},
            "expires_at": run["expires_at"],
            "config_version": 1,
            "max_operations": int(snapshot.get("bounds", {}).get("default_max_operations") or 100),
            "admission_instances": [],
        }
    elapsed = max(0.0, time.monotonic() - started)
    return {
        "mode": mode,
        "revision": revision,
        "run": policy_run,
        "lease": {
            # Reduced by the round trip this request already took. The host
            # anchors it locally and never extends it, so this number is the
            # whole of what the control plane promises.
            "seconds": max(0.0, AUTHORIZATION_TTL_S - elapsed),
            "issued_at": now,
        },
        "server_time": now,
        "broker_confirmed_revision": broker_revision,
    }


def lease_deadline_mono(reply: Mapping[str, Any], *, now: float | None = None) -> float:
    """Return a local monotonic deadline for one policy answer.

    Every term is stated relative to an instant the control plane chose, so this
    process never has to trust its own wall clock against the server's -- and it
    never compares two machines' monotonic values, which mean nothing to each
    other.

    Two ceilings apply and the earlier one wins: the lease (how long the control
    plane stands behind this answer) and the run's own remaining life (how long
    the experiment was asked to last). Both are re-anchored to this process's
    clock at receipt, so a reply delayed in flight cannot buy time it never had.
    """
    moment = time.monotonic() if now is None else now
    lease = reply.get("lease")
    seconds = 0.0
    issued = None
    if isinstance(lease, Mapping):
        seconds = _as_float(lease.get("seconds")) or 0.0
        issued = _parse_instant(lease.get("issued_at"))
    if issued is None or seconds <= 0:
        return moment
    server_now = _parse_instant(reply.get("server_time")) or issued
    # Everything is measured against the server's own frame. ``server_now`` is
    # the instant the answer was assembled and ``issued`` the instant the lease
    # started, so their difference is time the lease already spent -- measured
    # without either machine's wall clock having to agree with the other's.
    already_spent = max(0.0, (server_now - issued).total_seconds())
    deadline = moment + max(0.0, seconds - already_spent)
    run = reply.get("run")
    if isinstance(run, Mapping):
        expires_at = _parse_instant(run.get("expires_at"))
        if expires_at is not None:
            remaining = (expires_at - server_now).total_seconds()
            deadline = min(deadline, moment + max(0.0, remaining))
    return deadline


def _empty_policy(*, mode: str, revision: int, now: datetime) -> dict[str, Any]:
    """Return an answer that commands nothing, for a control plane with no store."""
    return {
        "mode": mode,
        "revision": revision,
        "run": None,
        "lease": {"seconds": 0.0, "issued_at": now},
        "server_time": now,
        "broker_confirmed_revision": None,
    }


def _supports_control(reported: Mapping[str, Any]) -> bool:
    """Return whether the host advertised the runtime-control capability."""
    return str(reported.get("capability") or "") == PROFILING_CONTROL_CAPABILITY


def _as_int(value: Any) -> int | None:
    """Return an integer, or ``None`` when the caller did not state one."""
    if value is None or isinstance(value, bool):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _as_float(value: Any) -> float | None:
    """Return a float, or ``None`` when the caller did not state one."""
    if value is None or isinstance(value, bool):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _as_mapping(value: Any) -> dict[str, Any]:
    """Return a plain mapping from an untrusted body field."""
    return dict(value) if isinstance(value, Mapping) else {}


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


__all__ = [
    "KIND_BROKER",
    "KIND_RUNNER",
    "PARTICIPANT_TTL_SECONDS",
    "POLICY_LEASE_SECONDS",
    "PROFILING_CONTROL_CAPABILITY",
    "build_policy_for_host",
    "lease_deadline_mono",
    "read_host_profiling_policy",
    "router",
]
