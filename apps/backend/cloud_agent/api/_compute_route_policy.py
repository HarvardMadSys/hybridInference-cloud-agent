"""Sensitive request policy shared by compute-management API routes."""

from __future__ import annotations

from typing import ClassVar

from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import ValidationError

from cloud_agent.api._sensitive_routes import SensitiveRoute
from cloud_agent.compute_secrets import ComputeSecretError
from cloud_agent.domain.compute import ComputeRefusal
from cloud_agent.ports.external.compute_provider import ProviderError

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
    )


def compute_storage_error_message(code: str) -> str:
    """Return the stable public explanation for a known storage refusal."""
    return _STORAGE_ERRORS[code][1]


class ComputeSensitiveRoute(SensitiveRoute):
    """Redact credential validation and map safe compute-domain failures."""

    validation_exceptions: ClassVar[tuple[type[Exception], ...]] = (
        RequestValidationError,
        ValidationError,
    )

    def validation_response(self, exc: Exception) -> JSONResponse:
        return _error(
            422,
            "invalid_configuration",
            "Check the required fields and configuration values.",
        )

    def domain_response(self, exc: Exception) -> JSONResponse | None:
        if isinstance(exc, ComputeRefusal):
            return _error(exc.status, exc.code, str(exc))
        if isinstance(exc, ProviderError):
            return _error(503 if exc.retryable else 422, exc.code, str(exc))
        if isinstance(exc, ComputeSecretError):
            return _error(
                503,
                "credential_unavailable",
                "The control plane cannot access this credential. Check the secret store and connection configuration.",
            )
        if isinstance(exc, ValueError):
            known = _STORAGE_ERRORS.get(str(exc))
            if known is not None:
                return _error(known[0], str(exc), known[1])
        return None


__all__ = ["ComputeSensitiveRoute", "compute_storage_error_message"]
