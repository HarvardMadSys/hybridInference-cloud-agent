"""Evidence must retain its meaning when recovered or projected for an operator."""

from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from cloud_agent.application.compute_release import ComputeRelease, recorded_evidence
from cloud_agent.domain.compute import public_release_evidence
from cloud_agent.ports.external.compute_provider import (
    ProviderReceipt,
    ResourceObservation,
    ResourceRef,
)

NOW = datetime(2026, 1, 1, tzinfo=UTC)


@pytest.mark.parametrize("source", [[], {}, 1, None])
def test_malformed_evidence_source_does_not_break_public_operation(source):
    assert public_release_evidence({"source": source}) is None


@pytest.mark.asyncio
@pytest.mark.parametrize("raw_state", ["expired_not_found", "terminated"])
async def test_terminal_observation_does_not_invent_a_delete_acknowledgement(raw_state):
    reconciler = SimpleNamespace(_checkpoint=AsyncMock())
    ref = ResourceRef(external_id="resource-1", config={})
    receipt = ProviderReceipt(
        outcome="confirmed",
        resource=ref,
        observation=ResourceObservation(
            ref=ref, state="terminated", raw_state=raw_state, observed_at=NOW
        ),
    )
    await ComputeRelease(reconciler).keep_receipt({"id": "release-1"}, ref, receipt)
    reconciler._checkpoint.assert_not_awaited()


def test_recovered_inventory_timestamp_is_not_a_provider_observation_timestamp():
    evidence = recorded_evidence(
        {"id": "release-1"},
        {
            "external_id": "resource-1",
            "observed_state": "terminated",
            "provider_state": "terminated",
            "observed_at": NOW,
        },
    )
    assert evidence is not None
    assert evidence["source"] == "provider_terminal"
    assert "observed_at" not in evidence
    assert evidence["recorded_at"] == NOW.isoformat()
