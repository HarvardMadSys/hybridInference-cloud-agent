"""Administrator resource lifecycle endpoints, independent of Host commands.

Only identifiers and safe public projections cross this boundary. Provider
credentials enter through connection writes; request-validation errors must
never echo those inputs into a response or log.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.routing import APIRoute
from pydantic import ValidationError

from cloud_agent.api.deps import get_client_ip, get_storage, log_admin_action, verify_admin_access
from cloud_agent.application.compute_resources import (
    ComputeCommands,  # noqa: TC001 - FastAPI resolves annotations.
)
from cloud_agent.compute_secrets import ComputeSecretError
from cloud_agent.domain.compute import ComputeRefusal
from cloud_agent.ports.external.compute_provider import ProviderError
from cloud_agent.schemas_compute import (  # noqa: TC001 - FastAPI request models.
    AddComputeConnection,
    AddDeploymentProfile,
    AddServerRequest,
    AutomaticDeploymentSetup,
    CloudLabDefaults,
    ComputeOperationAction,
    ExtendComputeRequest,
    ImportComputeRequest,
    RemoveComputeRequest,
    ResolveComputeOperation,
    RetireComputeRequest,
    RotateComputeRequest,
    UpdateCloudLabAutoscaling,
    UpdateComputeConnection,
    UpdateComputePolicy,
)

if TYPE_CHECKING:
    from collections.abc import Callable, Coroutine


# These storage refusals have intentionally public meaning. Other ValueErrors
# remain server faults and never have their raw text copied into an HTTP body.
_STORAGE_ERRORS: dict[str, tuple[int, str]] = {
    "deployment_profile_in_use": (
        409,
        "The deployment profile is already used by another managed resource.",
    ),
    "deployment_profile_exists": (409, "A deployment profile with this ID already exists."),
    "resource_already_managed": (409, "This resource already belongs to a managed allocation."),
    "operation_not_resolvable": (409, "This operation can no longer adopt an external result."),
    "absence_not_confirmable": (
        409,
        "Only a manual acquisition that recorded no resource can be closed as never created.",
    ),
    "provider_node_registered": (
        409,
        "A node of this request registered, so a machine was created; adopt its experiment.",
    ),
    "record_not_found": (404, "The resource no longer exists."),
    "allocation_not_found": (404, "The allocation does not exist."),
    "operation_not_found": (404, "The operation does not exist."),
    "revision_conflict": (409, "The resource changed; review its current configuration."),
    "idempotency_conflict": (409, "This request ID belongs to different parameters."),
    "release_committed": (409, "Release is committed and can no longer be cancelled."),
    "resource_retiring": (409, "The resource is retiring and cannot accept new dependencies."),
    "external_operation_unresolved": (
        409,
        "The external operation is unresolved; reconcile its result before continuing.",
    ),
    "operation_in_flight": (409, "The operation is running; wait for its result."),
    "resource_release_required": (
        409,
        "Release cloud resources or disable attached hosts before removing this server's records.",
    ),
    "placement_pinned": (409, "Choose another default location for new sandboxes first."),
    "deployment_drain_active": (409, "Finish the server deployment drain before removal."),
    "retained_groups_present": (
        409,
        "Delete the server's retained sandboxes and wait for cleanup before removal.",
    ),
    "active_attempts_present": (409, "Wait for the server's active runs to finish before removal."),
    "autoscaling_member_active": (
        409,
        "Release the autoscaling server before removing its records.",
    ),
    "operation_not_cancellable": (409, "The operation cannot be cancelled in its current state."),
    "operation_not_retryable": (409, "The operation cannot be retried in its current state."),
    "operation_lease_lost": (409, "Another worker owns this operation."),
    "resource_identity_changed": (409, "The resource identity changed and requires review."),
    "host_identity_conflict": (409, "This Host identity is already bound to another resource."),
    "invalid_operation_state": (409, "The operation state no longer allows this action."),
    "rotation_in_progress": (409, "This server is already moving to a replacement node."),
    "rotation_switching": (
        409,
        "The old node is stopped or the Host already moved; wait for the move or its rollback.",
    ),
    "deployment_profile_reserved": (
        409,
        "The deployment profile is reserved for CloudLab autoscaling.",
    ),
    "autoscaling_profile_in_use": (
        409,
        "A selected deployment profile is already used by a manual resource.",
    ),
    "autoscaling_profile_unavailable": (
        409,
        "No reserved deployment profile is available for another host.",
    ),
    "autoscaling_connection_unverified": (
        409,
        "Both autoscaling connections must still be verified.",
    ),
    "autoscaling_connection_in_use": (
        409,
        "Pause autoscaling and resolve any running acquisition before changing this connection.",
    ),
    "autoscaling_placement_pinned": (
        409,
        "Set host placement to Automatic before enabling or running autoscaling.",
    ),
    "autoscaling_runtime_conflict": (
        409,
        "Autoscaling state changed; the controller will retry from a fresh snapshot.",
    ),
    "autoscaling_disabled": (409, "CloudLab autoscaling is paused."),
    "autoscaling_acquire_blocked": (
        409,
        "A previous autoscaling acquisition needs administrator attention.",
    ),
    "autoscaling_operation_in_flight": (
        409,
        "An autoscaling lifecycle operation is already in progress.",
    ),
    "autoscaling_at_capacity": (409, "Autoscaling is already at maximum capacity."),
    "autoscaling_scale_down_disabled": (409, "Automatic scale-down is disabled."),
    "autoscaling_member_not_found": (404, "The autoscaling member no longer exists."),
    "autoscaling_member_not_retirable": (
        409,
        "The autoscaling member is not in a safe state for retirement.",
    ),
    "autoscaling_member_not_idle": (
        409,
        "The autoscaling member became busy and cannot be retired.",
    ),
    "autoscaling_minimum_capacity": (
        409,
        "Retirement would reduce the pool below its configured minimum.",
    ),
}


def _error(status: int, code: str, message: str) -> JSONResponse:
    return JSONResponse(
        status_code=status,
        content={"detail": {"code": code, "message": message}},
        headers={"Cache-Control": "no-store"},
    )


class CredentialSafeRoute(APIRoute):
    """Redact validation inputs before FastAPI can render a write-only secret."""

    def get_route_handler(self) -> Callable[[Request], Coroutine[Any, Any, Response]]:
        """Wrap request validation and safe domain errors without exposing credentials."""
        handler = super().get_route_handler()

        async def safe_handler(request: Request) -> Response:
            try:
                response = await handler(request)
            except (RequestValidationError, ValidationError):
                return _error(
                    422,
                    "invalid_configuration",
                    "Check the required fields and configuration values.",
                )
            except ComputeRefusal as exc:
                return _error(exc.status, exc.code, str(exc))
            except ProviderError as exc:
                return _error(503 if exc.retryable else 422, exc.code, str(exc))
            except ComputeSecretError:
                return _error(
                    503,
                    "credential_unavailable",
                    "The control plane cannot access this credential. Check the secret store and connection configuration.",
                )
            except ValueError as exc:
                known = _STORAGE_ERRORS.get(str(exc))
                if known is None:
                    raise
                return _error(known[0], str(exc), known[1])
            except HTTPException as exc:
                # Preserve auth failures, including their challenge headers.
                exc.headers = {**(exc.headers or {}), "Cache-Control": "no-store"}
                raise
            response.headers["Cache-Control"] = "no-store"
            return response

        return safe_handler


# The guard is on the router, not only on each route: a route added to this
# surface inherits it, so the next compute action cannot ship without one by
# omission. Mutating routes keep their per-route ``actor`` parameter for the
# audit trail — FastAPI runs a shared dependency once per request, so the two
# do not double-ask the gateway.
router = APIRouter(
    prefix="/admin/agent",
    route_class=CredentialSafeRoute,
    dependencies=[Depends(verify_admin_access)],
)


def get_compute_commands(request: Request) -> ComputeCommands:
    """The stable control plane owns adapters, encrypted credentials and workers."""
    commands = getattr(request.app.state, "compute_commands", None)
    if commands is None:
        raise HTTPException(
            status_code=503, detail="Compute resource management is not configured."
        )
    return commands


async def _audit(request: Request, actor: str, action: str, **identifiers: str) -> None:
    # Never audit request bodies, provider parameters, secret material or user
    # supplied connection names. IDs are enough to correlate durable intent.
    # Only changes are audited: the Servers page polls the reads below every
    # 30 seconds, and recording each one buried every change on the Audit log.
    await log_admin_action(
        get_storage(request),
        get_client_ip(request),
        f"agent.compute.{action}",
        details=identifiers,
        actor_id=actor,
    )


@router.get("/compute-providers")
async def providers(
    commands: ComputeCommands = Depends(get_compute_commands),
) -> dict[str, Any]:
    """List enabled resource providers and public deployment choices."""
    return await commands.catalog()


@router.post("/compute-deployment-profiles", status_code=201)
async def create_deployment_profile(
    request: Request,
    payload: AddDeploymentProfile,
    actor: str = Depends(verify_admin_access),
    commands: ComputeCommands = Depends(get_compute_commands),
) -> dict[str, Any]:
    """Save a write-only deployment bundle and encrypted Host inputs."""
    profile = await commands.create_deployment_profile(payload)
    await _audit(request, actor, "profile.create", profile_id=profile["id"])
    return {"profile": profile}


@router.get("/compute-automatic-setup")
async def automatic_setup_status(
    commands: ComputeCommands = Depends(get_compute_commands),
) -> dict[str, Any]:
    """Expose deployed-release readiness without exposing generated setup inputs."""
    return await commands.automatic_setup_status()


@router.post("/compute-deployment-profiles/automatic", status_code=201)
async def create_automatic_setup(
    request: Request,
    payload: AutomaticDeploymentSetup,
    actor: str = Depends(verify_admin_access),
    commands: ComputeCommands = Depends(get_compute_commands),
) -> dict[str, Any]:
    """Generate recoverable encrypted setups through explicitly selected SSH access."""
    result = await commands.create_automatic_setup(payload)
    await _audit(
        request,
        actor,
        "profile.automatic_create",
        profile_id=",".join(profile["id"] for profile in result["profiles"]),
    )
    return result


@router.get("/compute-defaults/cloudlab")
async def cloudlab_defaults(
    _actor: str = Depends(verify_admin_access),
    commands: ComputeCommands = Depends(get_compute_commands),
) -> dict[str, Any]:
    """Read non-secret defaults for new CloudLab servers."""
    return {"defaults": await commands.cloudlab_defaults()}


@router.put("/compute-defaults/cloudlab")
async def set_cloudlab_defaults(
    request: Request,
    payload: CloudLabDefaults,
    actor: str = Depends(verify_admin_access),
    commands: ComputeCommands = Depends(get_compute_commands),
) -> dict[str, Any]:
    """Replace non-secret defaults for new CloudLab servers."""
    defaults = await commands.set_cloudlab_defaults(payload)
    await _audit(request, actor, "cloudlab_defaults.set")
    return {"defaults": defaults}


@router.get("/compute-autoscaling/cloudlab")
async def cloudlab_autoscaling(
    commands: ComputeCommands = Depends(get_compute_commands),
) -> dict[str, Any]:
    """Read the safe autoscaling policy, controller status and owned members."""
    return await commands.cloudlab_autoscaling()


@router.get("/compute-availability/cloudlab")
async def cloudlab_preferred_nodes(
    commands: ComputeCommands = Depends(get_compute_commands),
) -> dict[str, Any]:
    """List newer CloudLab node types with more than 20 total nodes, in order."""
    return await commands.cloudlab_preferred_nodes()


@router.put("/compute-autoscaling/cloudlab")
async def set_cloudlab_autoscaling(
    request: Request,
    payload: UpdateCloudLabAutoscaling,
    actor: str = Depends(verify_admin_access),
    commands: ComputeCommands = Depends(get_compute_commands),
) -> dict[str, Any]:
    """Compare and replace the singleton CloudLab autoscaling policy."""
    result = await commands.set_cloudlab_autoscaling(payload, actor=actor)
    await _audit(request, actor, "cloudlab_autoscaling.set")
    return result


@router.get("/compute-connections")
async def connections(
    commands: ComputeCommands = Depends(get_compute_commands),
) -> dict[str, Any]:
    """List connection metadata without plaintext credentials or ciphertext."""
    return await commands.connections()


@router.post("/compute-connections", status_code=201)
async def create_connection(
    request: Request,
    payload: AddComputeConnection,
    actor: str = Depends(verify_admin_access),
    commands: ComputeCommands = Depends(get_compute_commands),
) -> dict[str, Any]:
    """Save a write-only credential in the configured encrypted store."""
    connection = await commands.create_connection(payload)
    await _audit(request, actor, "connection.create", connection_id=connection["id"])
    return {"connection": connection}


@router.patch("/compute-connections/{connection_id}")
async def update_connection(
    connection_id: str,
    request: Request,
    payload: UpdateComputeConnection,
    actor: str = Depends(verify_admin_access),
    commands: ComputeCommands = Depends(get_compute_commands),
) -> dict[str, Any]:
    """Replace connection metadata or credentials against the observed revision."""
    connection = await commands.update_connection(connection_id, payload)
    await _audit(request, actor, "connection.update", connection_id=connection_id)
    return {"connection": connection}


@router.post("/compute-connections/{connection_id}/validate")
async def validate_connection(
    connection_id: str,
    request: Request,
    actor: str = Depends(verify_admin_access),
    commands: ComputeCommands = Depends(get_compute_commands),
) -> dict[str, Any]:
    """Validate a configured connection without allocating resources."""
    connection = await commands.validate_connection(connection_id)
    await _audit(request, actor, "connection.validate", connection_id=connection_id)
    return {"connection": connection}


@router.get("/servers")
async def servers(
    commands: ComputeCommands = Depends(get_compute_commands),
) -> dict[str, Any]:
    """Read current allocations, Host bindings, and global placement from storage."""
    return await commands.servers()


@router.post("/servers", status_code=202)
async def add_server(
    request: Request,
    payload: AddServerRequest,
    actor: str = Depends(verify_admin_access),
    commands: ComputeCommands = Depends(get_compute_commands),
) -> dict[str, Any]:
    """Queue an idempotent resource acquisition and shared Host deployment."""
    operation = await commands.add_server(payload, actor=actor)
    await _audit(request, actor, "server.add", operation_id=operation["id"])
    return {"operation": operation}


@router.delete("/servers/{allocation_id}")
async def remove_server(
    allocation_id: str,
    request: Request,
    payload: RemoveComputeRequest,
    actor: str = Depends(verify_admin_access),
    commands: ComputeCommands = Depends(get_compute_commands),
) -> dict[str, bool]:
    """Remove safe local inventory without orphaning provider resources or work."""
    await commands.remove_server(allocation_id, expected_revision=payload.expected_revision)
    await _audit(request, actor, "server.remove", allocation_id=allocation_id)
    return {"removed": True}


@router.post("/server-preflights", status_code=202)
async def preflight(
    request: Request,
    payload: AddServerRequest,
    actor: str = Depends(verify_admin_access),
    commands: ComputeCommands = Depends(get_compute_commands),
) -> dict[str, Any]:
    """Queue read-only connection and deployment prerequisite checks."""
    operation = await commands.add_server(payload, actor=actor, preflight=True)
    await _audit(request, actor, "server.preflight", operation_id=operation["id"])
    return {"operation": operation}


@router.post("/compute-allocations/import", status_code=202)
async def import_resource(
    request: Request,
    payload: ImportComputeRequest,
    actor: str = Depends(verify_admin_access),
    commands: ComputeCommands = Depends(get_compute_commands),
) -> dict[str, Any]:
    """Record an existing resource for observation without deploying a Host."""
    operation = await commands.import_resource(payload, actor=actor)
    await _audit(request, actor, "allocation.import", operation_id=operation["id"])
    return {"operation": operation}


@router.patch("/compute-allocations/{allocation_id}/policy")
async def policy(
    allocation_id: str,
    request: Request,
    payload: UpdateComputePolicy,
    actor: str = Depends(verify_admin_access),
    commands: ComputeCommands = Depends(get_compute_commands),
) -> dict[str, Any]:
    """Save a revisioned renewal policy independently of an extension request."""
    allocation = await commands.update_policy(allocation_id, payload)
    await _audit(request, actor, "allocation.policy", allocation_id=allocation_id)
    return {"allocation": allocation}


@router.post("/compute-allocations/{allocation_id}/extend", status_code=202)
async def extend(
    allocation_id: str,
    request: Request,
    payload: ExtendComputeRequest,
    actor: str = Depends(verify_admin_access),
    commands: ComputeCommands = Depends(get_compute_commands),
) -> dict[str, Any]:
    """Queue an extension to one fixed absolute expiration."""
    operation = await commands.extend(allocation_id, payload, actor=actor)
    await _audit(
        request,
        actor,
        "allocation.extend",
        allocation_id=allocation_id,
        operation_id=operation["id"],
    )
    return {"operation": operation}


@router.post("/compute-allocations/{allocation_id}/rotate", status_code=202)
async def rotate(
    allocation_id: str,
    request: Request,
    payload: RotateComputeRequest,
    actor: str = Depends(verify_admin_access),
    commands: ComputeCommands = Depends(get_compute_commands),
) -> dict[str, Any]:
    """Queue a move of the server's Host to a replacement node now."""
    operation = await commands.rotate(allocation_id, payload, actor=actor)
    await _audit(
        request,
        actor,
        "allocation.rotate",
        allocation_id=allocation_id,
        operation_id=operation["id"],
    )
    return {"operation": operation}


@router.post("/compute-allocations/{allocation_id}/retire", status_code=202)
async def retire(
    allocation_id: str,
    request: Request,
    payload: RetireComputeRequest,
    actor: str = Depends(verify_admin_access),
    commands: ComputeCommands = Depends(get_compute_commands),
) -> dict[str, Any]:
    """Queue retirement of a complete allocation with retained-state guards."""
    operation = await commands.retire(allocation_id, payload, actor=actor)
    await _audit(
        request,
        actor,
        "allocation.retire",
        allocation_id=allocation_id,
        operation_id=operation["id"],
    )
    return {"operation": operation}


@router.get("/compute-allocations/{allocation_id}/retirement-impact")
async def retirement_impact(
    allocation_id: str,
    commands: ComputeCommands = Depends(get_compute_commands),
) -> dict[str, Any]:
    """Read the full resource scope and blockers before requesting retirement."""
    return await commands.retirement_impact(allocation_id)


@router.get("/compute-operations")
async def operations(
    request_id: str | None = Query(default=None, max_length=128),
    commands: ComputeCommands = Depends(get_compute_commands),
) -> dict[str, Any]:
    """Recover recent operations or one request whose response was lost."""
    return await commands.operations(request_id=request_id)


async def _operation(commands: ComputeCommands, operation_id: str) -> dict[str, Any]:
    row = await commands.repository.get_operation(operation_id)
    if row is None:
        raise ComputeRefusal("operation_not_found", "The operation does not exist.", status=404)
    return await commands.operation_view(row)


@router.get("/compute-operations/{operation_id}")
async def operation(
    operation_id: str,
    commands: ComputeCommands = Depends(get_compute_commands),
) -> dict[str, Any]:
    """Read safe durable progress for a resource operation."""
    return {"operation": await _operation(commands, operation_id)}


@router.post("/compute-operations/{operation_id}/retry", status_code=202)
async def retry(
    operation_id: str,
    request: Request,
    payload: ComputeOperationAction,
    actor: str = Depends(verify_admin_access),
    commands: ComputeCommands = Depends(get_compute_commands),
) -> dict[str, Any]:
    """Resume recoverable work from its persisted checkpoint."""
    view = await _operation(commands, operation_id)
    if "retry" not in view["allowed_actions"]:
        raise ComputeRefusal(
            "operation_not_retryable", "The operation cannot be retried in its current state."
        )
    # The repository repeats this state check under its operation/allocation locks.
    result = await commands.operation_view(await commands.repository.retry_operation(operation_id))
    await _audit(request, actor, "operation.retry", operation_id=operation_id)
    return {"operation": result}


@router.post("/compute-operations/{operation_id}/cancel", status_code=202)
async def cancel(
    operation_id: str,
    request: Request,
    payload: ComputeOperationAction,
    actor: str = Depends(verify_admin_access),
    commands: ComputeCommands = Depends(get_compute_commands),
) -> dict[str, Any]:
    """Cancel a permitted retirement before the irreversible release boundary."""
    view = await _operation(commands, operation_id)
    if "cancel" not in view["allowed_actions"]:
        raise ComputeRefusal(
            "operation_not_cancellable", "The operation cannot be cancelled in its current state."
        )
    result = await commands.operation_view(await commands.repository.cancel_operation(operation_id))
    await _audit(request, actor, "operation.cancel", operation_id=operation_id)
    return {"operation": result}


@router.post("/compute-operations/{operation_id}/resolve", status_code=202)
async def resolve(
    operation_id: str,
    request: Request,
    payload: ResolveComputeOperation,
    actor: str = Depends(verify_admin_access),
    commands: ComputeCommands = Depends(get_compute_commands),
) -> dict[str, Any]:
    """Adopt an explicitly identified result of an unknown acquisition."""
    result = await commands.resolve_operation(operation_id, payload.external_id)
    await _audit(request, actor, "operation.resolve", operation_id=operation_id)
    return {"operation": result}


@router.post("/compute-operations/{operation_id}/confirm-absent", status_code=202)
async def confirm_absent(
    operation_id: str,
    request: Request,
    payload: ComputeOperationAction,
    actor: str = Depends(verify_admin_access),
    commands: ComputeCommands = Depends(get_compute_commands),
) -> dict[str, Any]:
    """Close an acquisition the administrator confirmed created nothing at its provider."""
    view = await _operation(commands, operation_id)
    if "confirm_absent" not in view["allowed_actions"]:
        raise ComputeRefusal(
            "absence_not_confirmable", _STORAGE_ERRORS["absence_not_confirmable"][1]
        )
    # The repository repeats this check under the operation and allocation locks.
    result = await commands.operation_view(
        await commands.repository.confirm_acquisition_absent(operation_id=operation_id)
    )
    await _audit(request, actor, "operation.confirm_absent", operation_id=operation_id)
    return {"operation": result}
