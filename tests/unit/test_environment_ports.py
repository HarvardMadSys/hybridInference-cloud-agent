"""Focused contracts for the environment persistence facets."""

from __future__ import annotations

import inspect
from itertools import combinations
from typing import Protocol

import pytest

from cloud_agent.ports.environments import (
    EnvironmentBindingRepository,
    EnvironmentConfigurationRepository,
    EnvironmentExecutionRepository,
    EnvironmentRepository,
    EnvironmentRunRepository,
    EnvironmentWorkspaceRepository,
)

_FACETS: tuple[tuple[type[Protocol], set[str]], ...] = (
    (
        EnvironmentConfigurationRepository,
        {
            "archive_environment",
            "copy_environment",
            "current_setup_fingerprints",
            "get_environment",
            "get_global_environment",
            "list_environment_versions",
            "list_environments",
            "restore_environment",
            "save_environment",
            "set_environment_default",
        },
    ),
    (
        EnvironmentBindingRepository,
        {
            "bind_current_environment",
            "environment_for_claim",
            "get_environment_binding",
            "get_job_environment",
            "redact_environment_steps",
            "refresh_runtime_environment",
            "request_skip_environment_start",
            "workspace_environment_view",
        },
    ),
    (
        EnvironmentRunRepository,
        {
            "cancel_environment_run",
            "claim_environment_run",
            "create_environment_run",
            "fail_environment_preflight",
            "find_environment_run",
            "get_environment_run",
            "list_environment_orphans",
            "maintain_environments",
            "update_environment_run",
        },
    ),
    (
        EnvironmentWorkspaceRepository,
        {
            "create_environment_action_run",
            "get_environment_action_run",
            "record_environment_preparation",
            "record_worker_environment_preparation",
            "update_environment_action_run",
        },
    ),
)


def _declared_methods(protocol: type[Protocol]) -> set[str]:
    """Return only methods owned by this facet, excluding inherited machinery."""
    return {
        name
        for name, value in vars(protocol).items()
        if not name.startswith("_") and inspect.isfunction(value)
    }


def _all_methods(protocol: type[Protocol]) -> set[str]:
    """Return the public method surface including inherited facets."""
    return {
        name
        for name, value in inspect.getmembers(protocol, inspect.isfunction)
        if not name.startswith("_")
    }


@pytest.mark.parametrize(
    ("protocol", "expected"),
    _FACETS,
    ids=("configuration", "bindings", "runs", "workspace"),
)
def test_each_environment_facet_owns_only_its_operations(
    protocol: type[Protocol], expected: set[str]
) -> None:
    """A consumer can request one cohesive capability without unrelated methods."""
    assert _declared_methods(protocol) == expected


def test_environment_facets_are_disjoint_and_compose_the_aggregate() -> None:
    """Every environment operation belongs to one facet and the facade loses none."""
    method_sets = [expected for _, expected in _FACETS]
    assert all(left.isdisjoint(right) for left, right in combinations(method_sets, 2))
    assert _all_methods(EnvironmentRepository) == set().union(*method_sets)


def test_execution_composite_contains_only_binding_and_workspace_operations() -> None:
    """Turn execution receives its two required facets, not config or test scheduling."""
    expected = _FACETS[1][1] | _FACETS[3][1]
    assert _all_methods(EnvironmentExecutionRepository) == expected
