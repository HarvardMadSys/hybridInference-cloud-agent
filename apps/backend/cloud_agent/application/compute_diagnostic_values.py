"""Provider diagnostics the compute reconciler may keep on an operation.

Split from :mod:`cloud_agent.application.compute_reconcile`. A provider error
or receipt can carry diagnostics; only allowlisted field names with validated
values are persisted, together with the local operation's correlation fields.
Provider text, credentials and opaque tokens never are.
"""

from __future__ import annotations

import re
from contextlib import suppress
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any
from uuid import UUID

if TYPE_CHECKING:
    from cloud_agent.ports.external.compute_provider import ProviderReceipt, ResourceObservation


def _now() -> datetime:
    return datetime.now(UTC)


def _safe_uuid_fields(source: dict[str, Any]) -> dict[str, str]:
    """The provider identifiers in ``source`` that are UUIDs, normalized."""
    details: dict[str, str] = {}
    for key in ("provider_request_id", "external_id", "submission_request_id"):
        value = source.get(key)
        if isinstance(value, str):
            try:
                details[key] = str(UUID(value))
            except ValueError:
                continue
    return details


def _safe_observed_at(observed_at: Any) -> str | None:
    """A timezone-aware ISO timestamp, bounded, or None."""
    if isinstance(observed_at, str):
        try:
            parsed = datetime.fromisoformat(observed_at.replace("Z", "+00:00"))
        except ValueError:
            pass
        else:
            if parsed.tzinfo is not None:
                return observed_at[:64]
    return None


_SAFE_DIAGNOSTIC_LABEL = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")
_SENSITIVE_DIAGNOSTIC_LABEL = re.compile(
    r"token|secret|password|credential|bearer|api[_-]?key|jwt", re.IGNORECASE
)
_SAFE_PROVIDER_OPERATIONS = {
    "experiments.create",
    "experiments.delete",
    "experiments.list",
    "experiments.manifests.read",
    "experiments.read",
    "experiments.update",
    "profiles.read",
    "profiles.request",
    "provider.request",
}
_SAFE_PROVIDER_REASONS = {
    "authorization_denied",
    "capacity_unavailable",
    "connect_failed",
    "experiment_failed_no_provider_reason",
    "experiment_ready",
    "http_rejected",
    "invalid_create_response",
    "invalid_expiry_timestamp",
    "invalid_json_response",
    "invalid_manifest",
    "invalid_parameter",
    "invalid_profile_binding",
    "invalid_request",
    "invalid_response_shape",
    "invalid_ssh_destination",
    "invalid_ssh_manifest",
    "incomplete_resource_scope",
    "missing_or_invalid_experiment_id",
    "permission_denied",
    "profile_not_found",
    "profile_rejected",
    "project_access_denied",
    "provider_server_error",
    "rate_limited",
    "rate_limited_or_capacity",
    "request_accepted",
    "request_failed",
    "request_outcome_unknown",
    "reservation_conflict",
    "resource_identity_mismatch",
    "resource_not_found",
    "response_too_large",
    "tls_verification_failed",
    "unsupported_node_count",
}


def _safe_provider_fields(source: dict[str, Any]) -> dict[str, str | int | bool]:
    """Validate provider diagnostic values as well as their field names."""
    details: dict[str, str | int | bool] = {}
    operation = source.get("provider_operation")
    if isinstance(operation, str) and operation in _SAFE_PROVIDER_OPERATIONS:
        details["provider_operation"] = operation
    status = source.get("http_status")
    if isinstance(status, int) and not isinstance(status, bool) and 100 <= status <= 599:
        details["http_status"] = status
    reason = source.get("provider_reason_code")
    if isinstance(reason, str) and reason in _SAFE_PROVIDER_REASONS:
        details["provider_reason_code"] = reason
    details.update(_safe_uuid_fields(source))
    status = source.get("submission_http_status")
    if isinstance(status, int) and not isinstance(status, bool) and 100 <= status <= 599:
        details["submission_http_status"] = status
    for key in (
        "requested_project",
        "requested_profile",
        "requested_profile_project",
        "requested_site",
        "requested_hardware",
        "provider_state",
    ):
        value = source.get(key)
        if isinstance(value, str) and _safe_diagnostic_label(value):
            details[key] = value
    observed_at = _safe_observed_at(source.get("provider_observed_at"))
    if observed_at is not None:
        details["provider_observed_at"] = observed_at
    return details


def _safe_diagnostic_label(value: str) -> bool:
    """Reject label-shaped values that look like credentials or opaque tokens."""
    return bool(
        _SAFE_DIAGNOSTIC_LABEL.fullmatch(value)
        and not _SENSITIVE_DIAGNOSTIC_LABEL.search(value)
        and not (len(value) >= 32 and re.fullmatch(r"[A-Za-z0-9_-]+", value))
    )


def cloudlab_observation_diagnostics(
    observation: ResourceObservation, *, reason_code: str
) -> dict[str, str | int | bool]:
    """Summarize a successful CloudLab read without retaining provider text."""
    config = observation.ref.config
    details: dict[str, str | int | bool] = {
        "provider_operation": "experiments.read",
        "provider_reason_code": reason_code,
        "external_id": observation.ref.external_id,
        "provider_state": observation.state,
        "provider_observed_at": observation.observed_at.isoformat(),
    }
    project = config.get("project")
    if isinstance(project, str) and _safe_diagnostic_label(project):
        details["requested_project"] = project
    if observation.provider_http_status is not None:
        details["http_status"] = observation.provider_http_status
    if observation.provider_request_id is not None:
        details["provider_request_id"] = observation.provider_request_id
    for source, target in (
        ("profile_name", "requested_profile"),
        ("profile_project", "requested_profile_project"),
    ):
        value = config.get(source)
        if isinstance(value, str) and _safe_diagnostic_label(value):
            details[target] = value
    bindings = config.get("bindings")
    if isinstance(bindings, dict):
        for keys, target in (
            (("site", "cluster"), "requested_site"),
            (("phystype", "hardware", "node_type"), "requested_hardware"),
        ):
            value = next(
                (bindings[key] for key in keys if isinstance(bindings.get(key), str)), None
            )
            if isinstance(value, str) and _safe_diagnostic_label(value):
                details[target] = value
    return details


def provider_diagnostic_result(
    error: Exception,
    operation: dict[str, Any],
    *,
    outcome: str,
) -> dict[str, Any]:
    """Persist only allowlisted provider facts plus local operation correlation."""
    source = getattr(error, "diagnostics", None)
    if not isinstance(source, dict) or not source:
        return {}
    stage = source.get("_failure_stage")
    if not isinstance(stage, str):
        stage = str(operation.get("step") or "unknown")
    details = _safe_provider_fields(source)
    previous = (operation.get("result") or {}).get("provider_diagnostic")
    if (
        isinstance(previous, dict)
        and previous.get("provider_operation") == "experiments.create"
        and previous.get("operation_outcome") == "accepted"
    ):
        request_id = previous.get("provider_request_id")
        if isinstance(request_id, str):
            with suppress(ValueError):
                details["submission_request_id"] = str(UUID(request_id))
        status = previous.get("http_status")
        if isinstance(status, int) and not isinstance(status, bool) and 100 <= status <= 599:
            details["submission_http_status"] = status
    resource_ref = (operation.get("result") or {}).get("resource_ref") or {}
    external_id = details.get("external_id") or (
        resource_ref.get("external_id") if isinstance(resource_ref, dict) else None
    )
    if isinstance(external_id, str):
        try:
            details["external_id"] = str(UUID(external_id))
        except ValueError:
            details.pop("external_id", None)
    details.update(
        {
            "stage": stage[:64],
            "operation_id": str(operation.get("id") or "")[:128],
            "allocation_id": str(operation.get("allocation_id") or "")[:128],
            "operation_outcome": outcome,
            "retryable": bool(getattr(error, "retryable", False)),
            "recorded_at": _now().isoformat(),
        }
    )
    return {"provider_diagnostic": details}


def provider_receipt_diagnostic_result(
    receipt: ProviderReceipt, operation: dict[str, Any]
) -> dict[str, Any]:
    """Checkpoint allowlisted receipt metadata before the next provider call."""
    source = receipt.diagnostics
    if not source:
        return {}
    details = _safe_provider_fields(source)
    if receipt.resource is not None:
        try:
            details["external_id"] = str(UUID(receipt.resource.external_id))
        except ValueError:
            details.pop("external_id", None)
    details.update(
        {
            "stage": str(operation.get("step") or "unknown")[:64],
            "operation_id": str(operation.get("id") or "")[:128],
            "allocation_id": str(operation.get("allocation_id") or "")[:128],
            "operation_outcome": (
                "accepted"
                if details.get("provider_reason_code") == "request_accepted"
                else receipt.outcome
            ),
            "retryable": False,
            "recorded_at": _now().isoformat(),
        }
    )
    return {"provider_diagnostic": details}
