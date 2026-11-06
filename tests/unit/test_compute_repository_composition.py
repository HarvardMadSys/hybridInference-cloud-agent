"""The compute repository split by aggregate still has the methods it had.

``storage/repositories/compute/`` holds one mixin per aggregate and composes
them into ``PostgresComputeResourceRepository``. The split moved methods; it
did not add, drop or shadow any. ``BEFORE_SPLIT`` is the method inventory of
the single class in ``storage/repositories/compute_resources.py`` at
``3e0400ef7``, the commit before the split.
"""

from __future__ import annotations

import inspect

from cloud_agent.storage.repositories.compute import PostgresComputeResourceRepository
from cloud_agent.storage.repositories.compute._common import ComputeRepositoryBase

BEFORE_SPLIT = frozenset(
    {
        "__init__",
        "_advance_autoscaling_runtime",
        "_allocation",
        "_autoscaling_policy_view",
        "_autoscaling_runtime_view",
        "_cancel_rotation",
        "_check_profile_available",
        "_cloudlab_autoscaling_snapshot",
        "_existing_request",
        "_fence",
        "_insert_operation",
        "_lose_reclaimed_group",
        "_move_out_possible",
        "_public_operation_state",
        "_release_terminal_autoscaling_reservation",
        "_retirement_impact",
        "_upsert_node",
        "abandon_candidate",
        "activate_automatic_profiles",
        "advance_host_release",
        "apply_cloudlab_autoscaling_slot_target",
        "assert_autoscale_acquire_connections_verified",
        "authorize_superseded_removals",
        "begin_retirement",
        "cancel_operation",
        "checkpoint_operation",
        "claim_operation",
        "clear_bootstrap_hold",
        "commit_candidate_release",
        "commit_release",
        "complete_node_registration",
        "confirm_acquisition_absent",
        "create_allocation_operation",
        "create_autoscale_allocation_operation",
        "create_node_registration",
        "create_operation",
        "create_rotation_candidate",
        "finish_operation",
        "get_allocation",
        "get_cloudlab_autoscaling",
        "get_connection",
        "get_deployment_profile",
        "get_operation",
        "list_allocations",
        "list_connections",
        "list_deployment_profiles",
        "list_operations",
        "list_reserved_deployment_profiles",
        "lose_reclaimed_groups",
        "move_out_report",
        "node_registration",
        "observe_allocation",
        "prepare_bootstrap_binding",
        "provider_defaults",
        "queue_autoscale_retire",
        "rebind_slot_host",
        "record_host_key",
        "record_inventory",
        "record_observation_error",
        "record_superseded_removal",
        "recorded_host_state",
        "remove_allocation",
        "renew_operation_lease",
        "reopen_node_registration",
        "resolve_acquisition",
        "retirement_impact",
        "retry_operation",
        "running_attempts",
        "save_connection",
        "save_deployment_profile",
        "scheduling_inventory",
        "server_inventory",
        "set_admission_hold",
        "set_move_out_hold",
        "set_provider_defaults",
        "set_rotation_hold",
        "superseded_removals",
        "unmoved_groups",
        "update_cloudlab_autoscaling",
        "update_cloudlab_autoscaling_runtime",
        "update_policy",
    }
)


def _defined_methods(klass: type) -> set[str]:
    """The functions ``klass`` defines itself, static methods included."""
    return {
        name
        for name, value in vars(klass).items()
        if inspect.isfunction(value) or isinstance(value, staticmethod)
    }


def _own_classes() -> list[type]:
    """The repository's classes, from the composition down to the shared base."""
    return [klass for klass in PostgresComputeResourceRepository.__mro__ if klass is not object]


def test_composed_repository_exposes_the_methods_it_had_before_the_split() -> None:
    """Public and private alike: no method was added, dropped or renamed."""
    found = set().union(*(_defined_methods(klass) for klass in _own_classes()))
    assert found == BEFORE_SPLIT


def test_each_method_is_defined_by_exactly_one_class() -> None:
    """No mixin shadows another, so the MRO order cannot change which body runs."""
    owners: dict[str, list[str]] = {}
    for klass in _own_classes():
        for name in _defined_methods(klass):
            owners.setdefault(name, []).append(klass.__name__)
    assert {name: where for name, where in owners.items() if len(where) > 1} == {}


def test_composition_adds_no_methods_and_every_mixin_shares_the_base() -> None:
    """The repository is only its mixins; each builds on the shared pool and steps."""
    assert _defined_methods(PostgresComputeResourceRepository) == set()
    for mixin in PostgresComputeResourceRepository.__bases__:
        assert issubclass(mixin, ComputeRepositoryBase), mixin
