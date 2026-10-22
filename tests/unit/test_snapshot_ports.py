"""Focused contracts for the durable snapshot-state facets."""

from __future__ import annotations

import inspect
from itertools import combinations
from typing import Protocol

import pytest

from cloud_agent.ports.snapshot import (
    SnapshotBaseImportRepository,
    SnapshotCaptureRepository,
    SnapshotPublicationRepository,
    SnapshotPublishWorkerRepository,
    SnapshotReapingRepository,
    SnapshotRepository,
    SnapshotRequestRepository,
)

_FACETS: tuple[tuple[type[Protocol], set[str]], ...] = (
    (
        SnapshotRequestRepository,
        {"request_snapshot_publication"},
    ),
    (
        SnapshotCaptureRepository,
        {
            "claim_due_sandbox_snapshot",
            "defer_sandbox_snapshot_capture",
            "next_snapshot_retry_due",
            "record_sandbox_snapshot_capture",
            "reimport_snapshot_base",
        },
    ),
    (
        SnapshotPublicationRepository,
        {
            "claim_for_publish",
            "defer_snapshot_publish",
            "fail_snapshot_publish",
            "next_snapshot_publish_retry_due",
            "record_snapshot_publish",
            "record_snapshot_push",
            "renew_snapshot_publish_claim",
        },
    ),
    (
        SnapshotBaseImportRepository,
        {
            "complete_snapshot_base_import",
            "fence_base_import_start",
            "next_base_import_retry_due",
            "pending_snapshot_base_import",
            "record_snapshot_base_import_failure",
        },
    ),
    (
        SnapshotReapingRepository,
        {
            "reap_stalled_sandbox_snapshot_captures",
            "reap_stalled_snapshot_publishes",
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
    ids=("request", "capture", "publication", "base-import", "reaping"),
)
def test_each_snapshot_facet_owns_only_its_operations(
    protocol: type[Protocol], expected: set[str]
) -> None:
    """A consumer can request one snapshot phase without the whole state machine."""
    assert _declared_methods(protocol) == expected


def test_snapshot_facets_are_disjoint_and_compose_the_aggregate() -> None:
    """Every snapshot operation belongs to one facet and the facade loses none."""
    method_sets = [expected for _, expected in _FACETS]
    assert all(left.isdisjoint(right) for left, right in combinations(method_sets, 2))
    assert _all_methods(SnapshotRepository) == set().union(*method_sets)


def test_publish_worker_composite_contains_only_its_two_phases() -> None:
    """The publisher receives publication and base-import operations, not capture/reaping."""
    expected = _FACETS[2][1] | _FACETS[3][1]
    assert _all_methods(SnapshotPublishWorkerRepository) == expected
