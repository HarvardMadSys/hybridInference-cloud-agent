"""Refusals a store raises when the state it holds forbids the requested change.

A caller tells them apart by ``exc.code``, never by the exception's text: an
unrelated ``ValueError`` whose message happens to match a code must not be
mistaken for one. ``str(exc)`` stays the code's value for logs and records.
"""

from __future__ import annotations

from enum import StrEnum


class ConflictCode(StrEnum):
    """Every refusal the compute and host-pool stores raise, by its stable public name."""

    ABSENCE_NOT_CONFIRMABLE = "absence_not_confirmable"
    ACTIVE_ATTEMPTS_PRESENT = "active_attempts_present"
    ALLOCATION_NOT_FOUND = "allocation_not_found"
    AUTOSCALING_ACQUIRE_BLOCKED = "autoscaling_acquire_blocked"
    AUTOSCALING_AT_CAPACITY = "autoscaling_at_capacity"
    AUTOSCALING_CONNECTION_IN_USE = "autoscaling_connection_in_use"
    AUTOSCALING_CONNECTION_UNVERIFIED = "autoscaling_connection_unverified"
    AUTOSCALING_DISABLED = "autoscaling_disabled"
    AUTOSCALING_MEMBER_ACTIVE = "autoscaling_member_active"
    AUTOSCALING_MEMBER_NOT_FOUND = "autoscaling_member_not_found"
    AUTOSCALING_MEMBER_NOT_IDLE = "autoscaling_member_not_idle"
    AUTOSCALING_MEMBER_NOT_RETIRABLE = "autoscaling_member_not_retirable"
    AUTOSCALING_MINIMUM_CAPACITY = "autoscaling_minimum_capacity"
    AUTOSCALING_OPERATION_IN_FLIGHT = "autoscaling_operation_in_flight"
    AUTOSCALING_PLACEMENT_PINNED = "autoscaling_placement_pinned"
    AUTOSCALING_PROFILE_IN_USE = "autoscaling_profile_in_use"
    AUTOSCALING_PROFILE_UNAVAILABLE = "autoscaling_profile_unavailable"
    AUTOSCALING_RUNTIME_CONFLICT = "autoscaling_runtime_conflict"
    AUTOSCALING_SCALE_DOWN_DISABLED = "autoscaling_scale_down_disabled"
    CANDIDATE_BOUND = "candidate_bound"
    CANDIDATE_MISMATCH = "candidate_mismatch"
    DEPLOYMENT_DRAIN_ACTIVE = "deployment_drain_active"
    DEPLOYMENT_PROFILE_EXISTS = "deployment_profile_exists"
    DEPLOYMENT_PROFILE_IN_USE = "deployment_profile_in_use"
    DEPLOYMENT_PROFILE_RESERVED = "deployment_profile_reserved"
    ENROLLMENT_TOKEN_REMOVED = "enrollment_token_removed"
    EXTERNAL_OPERATION_UNRESOLVED = "external_operation_unresolved"
    HOST_DRAIN_FENCED = "host_drain_fenced"
    HOST_IDENTITY_CONFLICT = "host_identity_conflict"
    HOST_NOT_FOUND = "host_not_found"
    HOST_RELEASE_NOT_RECORDED = "host_release_not_recorded"
    IDEMPOTENCY_CONFLICT = "idempotency_conflict"
    INVALID_OPERATION_STATE = "invalid_operation_state"
    NOT_A_CANDIDATE = "not_a_candidate"
    OPERATION_IN_FLIGHT = "operation_in_flight"
    OPERATION_LEASE_LOST = "operation_lease_lost"
    OPERATION_NOT_CANCELLABLE = "operation_not_cancellable"
    OPERATION_NOT_FOUND = "operation_not_found"
    OPERATION_NOT_RESOLVABLE = "operation_not_resolvable"
    OPERATION_NOT_RETRYABLE = "operation_not_retryable"
    PLACEMENT_PINNED = "placement_pinned"
    PROVIDER_NODE_REGISTERED = "provider_node_registered"
    RECORD_NOT_FOUND = "record_not_found"
    RELEASE_COMMITTED = "release_committed"
    RESOURCE_ALREADY_MANAGED = "resource_already_managed"
    RESOURCE_IDENTITY_CHANGED = "resource_identity_changed"
    RESOURCE_OBSERVATION_NOT_READY = "resource_observation_not_ready"
    RESOURCE_RELEASE_REQUIRED = "resource_release_required"
    RESOURCE_RETIRING = "resource_retiring"
    RETAINED_GROUPS_PRESENT = "retained_groups_present"
    REVISION_CONFLICT = "revision_conflict"
    ROTATION_IN_PROGRESS = "rotation_in_progress"
    ROTATION_SWITCHING = "rotation_switching"


class StorageConflict(ValueError):
    """The store refused a change; ``code`` says which refusal.

    It remains a ``ValueError`` so handlers that catch that keep working.
    """

    code: ConflictCode

    def __init__(self, code: ConflictCode) -> None:
        # Unpickling passes the plain string back in; normalise it.
        self.code = ConflictCode(code)
        super().__init__(self.code.value)


__all__ = ["ConflictCode", "StorageConflict"]
