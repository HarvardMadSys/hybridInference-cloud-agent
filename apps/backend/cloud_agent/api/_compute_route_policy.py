"""Sensitive request policy shared by compute-management API routes."""

from __future__ import annotations

from typing import ClassVar

from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import ValidationError

from cloud_agent.api._sensitive_routes import SensitiveRoute
from cloud_agent.domain.compute import ComputeRefusal
from cloud_agent.ports.errors import ConflictCode, StorageConflict
from cloud_agent.ports.external.compute_provider import ProviderError
from cloud_agent.providers.compute.compute_secrets import ComputeSecretError

# These storage refusals have intentionally public meaning. Other conflicts and
# every other ValueError remain server faults and never have their raw text
# copied into an HTTP body.
_STORAGE_ERRORS: dict[ConflictCode, tuple[int, str]] = {
    ConflictCode.DEPLOYMENT_PROFILE_IN_USE: (
        409,
        "The deployment profile is already used by another managed resource.",
    ),
    ConflictCode.DEPLOYMENT_PROFILE_EXISTS: (
        409,
        "A deployment profile with this ID already exists.",
    ),
    ConflictCode.RESOURCE_ALREADY_MANAGED: (
        409,
        "This resource already belongs to a managed allocation.",
    ),
    ConflictCode.OPERATION_NOT_RESOLVABLE: (
        409,
        "This operation can no longer adopt an external result.",
    ),
    ConflictCode.ABSENCE_NOT_CONFIRMABLE: (
        409,
        "Only a manual acquisition that recorded no resource can be closed as never created.",
    ),
    ConflictCode.PROVIDER_NODE_REGISTERED: (
        409,
        "A node of this request registered, so a machine was created; adopt its experiment.",
    ),
    ConflictCode.RECORD_NOT_FOUND: (404, "The resource no longer exists."),
    ConflictCode.ALLOCATION_NOT_FOUND: (404, "The allocation does not exist."),
    ConflictCode.OPERATION_NOT_FOUND: (404, "The operation does not exist."),
    ConflictCode.REVISION_CONFLICT: (
        409,
        "The resource changed; review its current configuration.",
    ),
    ConflictCode.IDEMPOTENCY_CONFLICT: (409, "This request ID belongs to different parameters."),
    ConflictCode.RELEASE_COMMITTED: (409, "Release is committed and can no longer be cancelled."),
    ConflictCode.RESOURCE_RETIRING: (
        409,
        "The resource is retiring and cannot accept new dependencies.",
    ),
    ConflictCode.EXTERNAL_OPERATION_UNRESOLVED: (
        409,
        "The external operation is unresolved; reconcile its result before continuing.",
    ),
    ConflictCode.OPERATION_IN_FLIGHT: (409, "The operation is running; wait for its result."),
    ConflictCode.RESOURCE_RELEASE_REQUIRED: (
        409,
        "Release cloud resources or disable attached hosts before removing this server's records.",
    ),
    ConflictCode.PLACEMENT_PINNED: (
        409,
        "Choose another default location for new sandboxes first.",
    ),
    ConflictCode.DEPLOYMENT_DRAIN_ACTIVE: (
        409,
        "Finish the server deployment drain before removal.",
    ),
    ConflictCode.RETAINED_GROUPS_PRESENT: (
        409,
        "Delete the server's retained sandboxes and wait for cleanup before removal.",
    ),
    ConflictCode.ACTIVE_ATTEMPTS_PRESENT: (
        409,
        "Wait for the server's active runs to finish before removal.",
    ),
    ConflictCode.AUTOSCALING_MEMBER_ACTIVE: (
        409,
        "Release the autoscaling server before removing its records.",
    ),
    ConflictCode.OPERATION_NOT_CANCELLABLE: (
        409,
        "The operation cannot be cancelled in its current state.",
    ),
    ConflictCode.OPERATION_NOT_RETRYABLE: (
        409,
        "The operation cannot be retried in its current state.",
    ),
    ConflictCode.OPERATION_LEASE_LOST: (409, "Another worker owns this operation."),
    ConflictCode.RESOURCE_IDENTITY_CHANGED: (
        409,
        "The resource identity changed and requires review.",
    ),
    ConflictCode.HOST_IDENTITY_CONFLICT: (
        409,
        "This Host identity is already bound to another resource.",
    ),
    ConflictCode.INVALID_OPERATION_STATE: (
        409,
        "The operation state no longer allows this action.",
    ),
    ConflictCode.ROTATION_IN_PROGRESS: (
        409,
        "This server is already moving to a replacement node.",
    ),
    ConflictCode.ROTATION_SWITCHING: (
        409,
        "The old node is stopped or the Host already moved; wait for the move or its rollback.",
    ),
    ConflictCode.DEPLOYMENT_PROFILE_RESERVED: (
        409,
        "The deployment profile is reserved for CloudLab autoscaling.",
    ),
    ConflictCode.AUTOSCALING_PROFILE_IN_USE: (
        409,
        "A selected deployment profile is already used by a manual resource.",
    ),
    ConflictCode.AUTOSCALING_PROFILE_UNAVAILABLE: (
        409,
        "No reserved deployment profile is available for another host.",
    ),
    ConflictCode.AUTOSCALING_CONNECTION_UNVERIFIED: (
        409,
        "Both autoscaling connections must still be verified.",
    ),
    ConflictCode.AUTOSCALING_CONNECTION_IN_USE: (
        409,
        "Pause autoscaling and resolve any running acquisition before changing this connection.",
    ),
    ConflictCode.AUTOSCALING_PLACEMENT_PINNED: (
        409,
        "Set host placement to Automatic before enabling or running autoscaling.",
    ),
    ConflictCode.AUTOSCALING_RUNTIME_CONFLICT: (
        409,
        "Autoscaling state changed; the controller will retry from a fresh snapshot.",
    ),
    ConflictCode.AUTOSCALING_DISABLED: (409, "CloudLab autoscaling is paused."),
    ConflictCode.AUTOSCALING_ACQUIRE_BLOCKED: (
        409,
        "A previous autoscaling acquisition needs administrator attention.",
    ),
    ConflictCode.AUTOSCALING_OPERATION_IN_FLIGHT: (
        409,
        "An autoscaling lifecycle operation is already in progress.",
    ),
    ConflictCode.AUTOSCALING_AT_CAPACITY: (409, "Autoscaling is already at maximum capacity."),
    ConflictCode.AUTOSCALING_SCALE_DOWN_DISABLED: (409, "Automatic scale-down is disabled."),
    ConflictCode.AUTOSCALING_MEMBER_NOT_FOUND: (404, "The autoscaling member no longer exists."),
    ConflictCode.AUTOSCALING_MEMBER_NOT_RETIRABLE: (
        409,
        "The autoscaling member is not in a safe state for retirement.",
    ),
    ConflictCode.AUTOSCALING_MEMBER_NOT_IDLE: (
        409,
        "The autoscaling member became busy and cannot be retired.",
    ),
    ConflictCode.AUTOSCALING_MINIMUM_CAPACITY: (
        409,
        "Retirement would reduce the pool below its configured minimum.",
    ),
}


def _error(status: int, code: str, message: str) -> JSONResponse:
    return JSONResponse(
        status_code=status,
        content={"detail": {"code": code, "message": message}},
    )


def compute_storage_error_message(code: ConflictCode) -> str:
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
        if isinstance(exc, StorageConflict):
            known = _STORAGE_ERRORS.get(exc.code)
            if known is not None:
                return _error(known[0], exc.code.value, known[1])
        return None


__all__ = ["ComputeSensitiveRoute", "compute_storage_error_message"]
