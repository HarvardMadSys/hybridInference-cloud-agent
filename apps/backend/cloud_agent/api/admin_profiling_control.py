"""Starting and stopping operation measurement from the Admin page.

This is the write half of the Measurements surface. It sits in the same router
as the read half and behind the same guard, because it is the same feature: a
separate public route would be a second authentication path to the same state.

Four rules are visible in the shape of the handlers rather than left to the
service:

* **The revision the caller saw travels with the request.** Two administrators
  who both press Start from a page showing revision 12 must not both succeed,
  and the one who loses is told the number to re-read rather than being handed a
  generic conflict.
* **The request id is the caller's.** A Start whose response was lost is retried
  with the same id and answers with the same experiment and the same expiry. The
  browser generates it; the server never invents one, because a retry the server
  named would be a second experiment.
* **A refusal still returns the truth.** Every 409 carries the current control
  snapshot, so the page can recover without a second round trip and cannot show
  a stale status next to a fresh error.
* **Writes are audited, reads are not.** Starting an experiment and stopping one
  are the two actions an operator will later have to explain.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Protocol

from fastapi import APIRouter, Depends, HTTPException, Request, Response

from cloud_agent.api.deps import (
    get_client_ip,
    get_storage,
    log_admin_action,
    require_store,
    verify_admin_access,
)
from cloud_agent.application.profiling_control import (
    ProfilingControlService,
    Refusal,
)
from cloud_agent.domain.profiling_control import (
    CONTROL_REFUSED_ALREADY_ACTIVE,
    CONTROL_REFUSED_ALREADY_FINISHED,
    CONTROL_REFUSED_CONFLICTING_REQUEST,
    CONTROL_REFUSED_INVALID,
    CONTROL_REFUSED_NOT_ADMIN_MODE,
    CONTROL_REFUSED_STALE_REVISION,
    CONTROL_REFUSED_UNKNOWN_RUN,
    ControlCommandResult,
)
from cloud_agent.logging_utils import get_logger
from cloud_agent.profiling_control_config import (
    ENV_PROFILING_CONTROL_DEPLOYMENT,
    profiling_control_deployment,
)
from cloud_agent_shared.operation_profiling import (
    ENV_PROFILING_MODE,
    MODE_ADMIN,
    MODE_DISABLED,
    MODE_ENVIRONMENT,
    profile_mode,
)

logger = get_logger(__name__)

#: Status code for each refusal the service can produce. A refusal is a result
#: rather than a failure, and the distinction that matters to a caller is
#: "re-read and try again" (409) versus "this request was wrong" (422) versus
#: "this deployment cannot do that at all" (409 with an explanatory type).
_REFUSAL_STATUS: dict[str, int] = {
    CONTROL_REFUSED_INVALID: 422,
    CONTROL_REFUSED_STALE_REVISION: 409,
    CONTROL_REFUSED_ALREADY_ACTIVE: 409,
    CONTROL_REFUSED_UNKNOWN_RUN: 409,
    CONTROL_REFUSED_ALREADY_FINISHED: 409,
    CONTROL_REFUSED_CONFLICTING_REQUEST: 409,
    CONTROL_REFUSED_NOT_ADMIN_MODE: 409,
}


class _ControlStorage(Protocol):
    """The capabilities this module reaches the storage layer for.

    Narrow on purpose, and named here rather than at each call, so a handler
    that reaches for a fourth capability has to say so in one place -- which is
    also what makes the audit write below a type error rather than a silent
    skip if the assembly ever renames it.
    """

    profiling_control: Any
    host_pool: Any
    audit: Any


def _no_store(response: Response) -> None:
    """Control state is read at a time somebody chose, never from a cache."""
    response.headers["Cache-Control"] = "no-store"


router = APIRouter(
    prefix="/admin/agent",
    dependencies=[Depends(_no_store), Depends(verify_admin_access)],
)


def _require_control_store(store: _ControlStorage | None) -> _ControlStorage:
    """The storage layer, or 503."""
    return require_store(store)


def _service(store: _ControlStorage) -> ProfilingControlService:
    """Build the control service over the deployment's own namespace."""
    return ProfilingControlService(
        control=store.profiling_control,
        deployment=profiling_control_deployment(),
        host_pool=store.host_pool,
    )


@router.get("/profiling-control")
async def get_profiling_control(
    store: _ControlStorage | None = Depends(get_storage),
) -> dict[str, Any]:
    """Return the control mode, the current experiment and what has confirmed it.

    ``mode`` is the deployment's answer to "who may control this", and it is
    what stops the page from offering a switch that would do nothing. The three
    answers are deliberately different:

    * ``disabled`` -- no control at all, and no collection either.
    * ``environment`` -- a static descriptor decides; Admin reports that the
      deployment is configuration-managed and offers no switch, because there is
      nothing for this API to turn off.
    * ``admin`` -- the persisted configuration decides, and everything below
      applies.

    A deployment whose mode is not ``admin`` still gets an answer rather than a
    404: "you cannot control this here, and here is why" is the useful reply.
    """
    mode = profile_mode()
    now = datetime.now(timezone.utc)
    if mode != MODE_ADMIN:
        # Not under Admin control, so there is no configuration to read. The
        # answer still has the same *shape* as a real one -- every key present,
        # the vocabulary empty -- so the page can render it without a special
        # case, and it says why in the mode it reports rather than in a field a
        # reader would have to interpret.
        return {
            "deployment": profiling_control_deployment(),
            "mode": mode,
            "configured": False,
            "revision": 0,
            "server_time": now,
            "status": "unmanaged" if mode == MODE_ENVIRONMENT else "unavailable",
            "run": None,
            "run_active": False,
            "last_stopped_run": None,
            "stored_samples": 0,
            "budget_scope": "producer_instance",
            "budget_note": "",
            "sync_note": "",
            "operations": {"selectable": {}, "all": []},
            "bounds": {},
        }
    snapshot = await _service(_require_control_store(store)).control_snapshot(now=now)
    snapshot["mode"] = mode
    return snapshot


@router.post("/profiling-runs", status_code=201)
async def start_profiling_run(
    request: Request,
    payload: dict[str, Any],
    response: Response,
    store: _ControlStorage | None = Depends(get_storage),
    _admin_id: str = Depends(verify_admin_access),
) -> dict[str, Any]:
    """Create and start one measurement experiment.

    The body is read as a plain mapping rather than a generated model so that a
    refusal can name the offending field in the same envelope every other
    refusal uses. Unknown keys are refused rather than ignored: a caller that
    misspelled ``operations`` would otherwise start an experiment measuring
    nothing.
    """
    moment = datetime.now(timezone.utc)
    if profile_mode() != MODE_ADMIN:
        raise HTTPException(
            status_code=409,
            detail={
                "error": {
                    "type": CONTROL_REFUSED_NOT_ADMIN_MODE,
                    "message": _mode_explanation(profile_mode()),
                    "revision": 0,
                }
            },
        )
    _reject_unknown_keys(
        payload,
        {
            "request_id",
            "expected_revision",
            "operations",
            "target",
            "duration_seconds",
        },
    )
    capability = _require_control_store(store)
    service = _service(capability)
    host_list = await _known_hosts(capability)
    validated = service.validate_start(
        request_id=str(payload.get("request_id") or ""),
        expected_revision=_as_int(payload.get("expected_revision")),
        operations=_as_operations(payload.get("operations")),
        host=_as_host(payload.get("target")),
        duration_seconds=None
        if payload.get("duration_seconds") is None
        else _as_int(payload["duration_seconds"]),
        actor_id=_admin_id,
        known_hosts=host_list,
    )
    if isinstance(validated, Refusal):
        await log_admin_action(
            capability,
            get_client_ip(request),
            "agent.profiling.start",
            None,
            {"outcome": "refused", "reason": validated.reason},
            success=False,
            actor_id=_admin_id,
        )
        raise _refusal(validated, revision=await _revision(capability))
    result = await service.start(validated, now=moment)
    await log_admin_action(
        capability,
        get_client_ip(request),
        "agent.profiling.start",
        None,
        {
            "outcome": result.kind,
            "run_id": result.run_id,
            "revision": result.revision,
            "operations": list(validated.operations),
            "host": validated.host,
            "duration_seconds": validated.duration_seconds,
            **_refusal_reason(result),
        },
        # A refused Start changed nothing; the Audit log must not read it as
        # measurements having been turned on.
        success=result.accepted,
        actor_id=_admin_id,
    )
    if not result.accepted:
        raise _refusal(
            Refusal(result.reason, result.detail),
            revision=result.revision,
            run_id=result.run_id,
        )
    snapshot = await service.control_snapshot(now=datetime.now(timezone.utc))
    snapshot["mode"] = MODE_ADMIN
    response.headers["Cache-Control"] = "no-store"
    return {"control": snapshot, "run_id": result.run_id, "revision": result.revision}


@router.post("/profiling-runs/{run_id}/stop")
async def stop_profiling_run(
    run_id: str,
    request: Request,
    payload: dict[str, Any],
    response: Response,
    store: _ControlStorage | None = Depends(get_storage),
    _admin_id: str = Depends(verify_admin_access),
) -> dict[str, Any]:
    """Stop the named experiment.

    Stopping does not interrupt anything a user is doing: it stops *collecting*.
    A file read that is already running finishes, a terminal stays attached, and
    every sample already frozen is delivered. What ends is the addition of new
    samples, at the latest when the bounded authorization the producers hold
    lapses.
    """
    moment = datetime.now(timezone.utc)
    if profile_mode() != MODE_ADMIN:
        raise HTTPException(
            status_code=409,
            detail={
                "error": {
                    "type": CONTROL_REFUSED_NOT_ADMIN_MODE,
                    "message": _mode_explanation(profile_mode()),
                    "revision": 0,
                }
            },
        )
    _reject_unknown_keys(payload, {"request_id", "reason"})
    capability = _require_control_store(store)
    service = _service(capability)
    validated = service.validate_stop(
        request_id=str(payload.get("request_id") or ""),
        run_id=run_id,
        actor_id=_admin_id,
        reason=str(payload.get("reason") or "stopped"),
    )
    if isinstance(validated, Refusal):
        await log_admin_action(
            capability,
            get_client_ip(request),
            "agent.profiling.stop",
            None,
            {"outcome": "refused", "reason": validated.reason, "run_id": run_id},
            success=False,
            actor_id=_admin_id,
        )
        raise _refusal(validated, revision=await _revision(capability))
    result = await service.stop(validated, now=moment)
    await log_admin_action(
        capability,
        get_client_ip(request),
        "agent.profiling.stop",
        None,
        {
            "outcome": result.kind,
            "run_id": run_id,
            "revision": result.revision,
            **_refusal_reason(result),
        },
        # Stopping an experiment that already ended leaves measurements as
        # they were; it is not a Stop that took effect.
        success=result.accepted,
        actor_id=_admin_id,
    )
    if not result.accepted:
        raise _refusal(
            Refusal(result.reason, result.detail),
            revision=result.revision,
            run_id=result.run_id,
        )
    snapshot = await service.control_snapshot(now=datetime.now(timezone.utc))
    snapshot["mode"] = MODE_ADMIN
    response.headers["Cache-Control"] = "no-store"
    return {"control": snapshot, "run_id": run_id, "revision": result.revision}


def _refusal_reason(result: ControlCommandResult) -> dict[str, str]:
    """Why a Start or Stop was refused, for its audit row; nothing when it was not."""
    return {} if result.accepted else {"reason": result.reason}


async def _revision(capability: _ControlStorage) -> int:
    """Return the deployment's current revision, for a refusal to state.

    Read from the control row rather than from the whole snapshot: a refusal
    only has to carry the number the caller must re-read, and the page needs no
    further detail to recover from "somebody else moved it".
    """
    control = await capability.profiling_control.get_control_state(
        deployment=profiling_control_deployment()
    )
    return int(control["revision"]) if control else 0


async def _known_hosts(capability: _ControlStorage) -> list[str] | None:
    """Return the enrolled host names, or ``None`` when they cannot be read.

    ``None`` means "do not validate the host here": a pool that cannot be read
    must not turn every Start into "unknown host". The database's own state is
    still authoritative for whether the target exists, and the page's dropdown
    was built from the same read.
    """
    try:
        hosts = await capability.host_pool.list_runner_hosts()
    except Exception:  # pragma: no cover - a read that must not block a Start
        logger.warning("agent_profiling_host_list_failed", exc_info=True)
        return None
    return [str(item.get("host")) for item in hosts if item.get("host")]


def _mode_explanation(mode: str) -> str:
    """Return the sentence the page shows for a deployment's control mode."""
    if mode == MODE_DISABLED:
        return (
            "operation profiling is disabled for this deployment; set "
            f"{ENV_PROFILING_MODE}=admin to enable Admin control"
        )
    if mode == MODE_ENVIRONMENT:
        return (
            "operation profiling is managed by deployment configuration, so there is "
            "nothing for the Admin API to start or stop"
        )
    return "operation profiling is under Admin control"


def _reject_unknown_keys(payload: Any, allowed: set[str]) -> None:
    """Refuse a body field this endpoint does not accept."""
    if not isinstance(payload, dict):
        raise HTTPException(
            status_code=422,
            detail={
                "error": {
                    "type": CONTROL_REFUSED_INVALID,
                    "message": "the request body must be a JSON object",
                }
            },
        )
    unknown = sorted(str(key) for key in payload if str(key) not in allowed)
    if unknown:
        raise HTTPException(
            status_code=422,
            detail={
                "error": {
                    "type": CONTROL_REFUSED_INVALID,
                    "message": f"Unsupported request field: {unknown[0]}.",
                }
            },
        )


def _as_int(value: Any) -> int:
    """Return a bounded integer, or a value the validator will refuse."""
    if isinstance(value, bool) or value is None:
        return -1
    try:
        return int(value)
    except (TypeError, ValueError):
        return -1


def _as_operations(value: Any) -> list[str]:
    """Return the operation names a body named, or an empty list."""
    if isinstance(value, str):
        return [value]
    if isinstance(value, (list, tuple)):
        return [str(item) for item in value]
    return []


def _as_host(target: Any) -> str | None:
    """Return the host a target names, or ``None`` for All hosts."""
    if not isinstance(target, dict):
        return None
    host = target.get("host")
    if host is None:
        return None
    text = str(host).strip()
    return text or None


def _refusal(refusal: Refusal, *, revision: int, run_id: str | None = None) -> HTTPException:
    """Return the HTTP error one refusal becomes."""
    status = _REFUSAL_STATUS.get(refusal.reason, 409)
    return HTTPException(
        status_code=status,
        detail={
            "error": {
                "type": refusal.reason,
                "message": refusal.detail,
                "revision": int(revision),
                **({"run_id": run_id} if run_id else {}),
            }
        },
    )


__all__ = [
    "ENV_PROFILING_CONTROL_DEPLOYMENT",
    "get_profiling_control",
    "router",
    "start_profiling_run",
    "stop_profiling_run",
]
