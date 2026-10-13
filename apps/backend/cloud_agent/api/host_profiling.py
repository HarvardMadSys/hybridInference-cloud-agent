"""Handing one host the collection configuration it should act on.

The control plane has exactly one way to tell a host agent what to collect, and
this is it: a dedicated read behind the host's own credential, answered from the
deployment's stored configuration. The runner polls it in the background, which
is the whole propagation mechanism -- there is no push, no lease and no
acknowledgement.

Three properties, and each replaces something the earlier design got wrong.

**The answer is the configuration, not a permission.** It says which experiment
is in force, until when, and which operations it selects. The host keeps that
until it hears something else. Timed runs also stop at their absolute expiry;
continuous runs have no expiry, so a disconnected node needs synchronization
to resume before Disable reaches it.

**A host is told its own configuration.** The route authenticates the caller, so
the answer is computed for whoever asked and a host cannot ask about another
machine.

**A lower version is refused by the reader, not re-sent by the writer.** The
host stores the version it applied and ignores anything older, so a delayed or
reordered answer cannot put a stopped experiment back into force. The control
plane does not need to know which version a host holds to make that work.

**Nothing here claims to have been applied.** The control plane reports what the
deployment is configured to do; whether a particular process has caught up is
not something it asserts, because sync is allowed to lag and the page says so.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter, Depends, Request, Response

from cloud_agent.api._body_limits import HostBodyLimitRoute
from cloud_agent.api.hosts import authenticate_host
from cloud_agent.logging_utils import get_logger
from cloud_agent.profiling_control_config import profiling_control_deployment
from cloud_agent_shared.operation_profiling import MODE_ADMIN, profile_mode

logger = get_logger(__name__)

#: How often a host agent should re-read this configuration when nothing is
#: running, and how often while an experiment is in force. Stated here because
#: the answer is what a host schedules itself by, and a number two modules
#: disagreed about would be a lag nobody could explain.
IDLE_SYNC_SECONDS = 30.0
ACTIVE_SYNC_SECONDS = 5.0

router = APIRouter(
    prefix="/v1/agent/hosts", tags=["host-profiling"], route_class=HostBodyLimitRoute
)


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
    """Build the configuration answer for one host.

    This is the whole of the host-side control channel: read what the deployment
    is currently configured to collect and say so. Nothing is recorded about
    the host and nothing is asserted about whether it applied the answer -- the
    runner applies it locally, and the page reports the configuration rather
    than a consensus. ``reported`` is accepted and ignored so a host written
    against the earlier shape keeps working; it is not trusted for anything.
    """
    now = datetime.now(timezone.utc)
    mode = profile_mode()
    if store is None:
        # A control plane without a database cannot state a configuration, so
        # it states its mode and nothing else. Reporting an error here would
        # make a host retry a question that has no answer yet.
        return _empty_policy(mode=mode, revision=0, now=now)
    from cloud_agent.application.profiling_control import ProfilingControlService

    service = ProfilingControlService(
        control=store.profiling_control,
        deployment=profiling_control_deployment(),
        host_pool=store.host_pool,
    )
    snapshot = await service.control_snapshot(now=now)
    run = snapshot.get("run") if snapshot.get("run_active") else None
    policy_run = None
    if mode == MODE_ADMIN and run is not None:
        policy_run = {
            "run_id": str(run["run_id"]),
            "operations": [str(item) for item in run.get("operations") or ()],
            "target": {str(key): str(value) for key, value in (run.get("target") or {}).items()},
            "expires_at": run["expires_at"],
            "continuous": run.get("continuous") is True,
            "config_version": 1,
            "max_operations": 0
            if run.get("continuous") is True
            else int(snapshot.get("bounds", {}).get("default_max_operations") or 100),
            "admission_instances": [],
        }
    return {
        "mode": mode,
        # The deployment's configuration version. A host refuses a lower one, so
        # a delayed or reordered answer cannot put a stopped experiment back
        # into force. This is the whole anti-rollback protocol.
        "revision": int(snapshot.get("revision") or 0),
        "run": policy_run,
        "server_time": now,
    }


def _empty_policy(*, mode: str, revision: int, now: datetime) -> dict[str, Any]:
    """Return an answer that commands nothing, for a control plane with no store."""
    return {
        "mode": mode,
        "revision": revision,
        "run": None,
        "server_time": now,
    }


__all__ = [
    "ACTIVE_SYNC_SECONDS",
    "IDLE_SYNC_SECONDS",
    "build_policy_for_host",
    "read_host_profiling_policy",
    "router",
]
