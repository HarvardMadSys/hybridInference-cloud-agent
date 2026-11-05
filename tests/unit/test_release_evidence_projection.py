"""A release's proof is explicit about its source and shows only safe fields."""

from __future__ import annotations

from datetime import UTC, datetime

import httpx
import pytest
from tests.unit.test_compute_providers import CONFIG, EXPERIMENT, intent, provider

from cloud_agent.application.compute_release import (
    acknowledged_release,
    recorded_evidence,
    recorded_receipt,
    release_evidence,
)
from cloud_agent.domain.compute import public_operation, public_release_evidence
from cloud_agent.ports.external.compute_provider import ResourceObservation, ResourceRef

REF = ResourceRef(external_id=EXPERIMENT, config=CONFIG)
SEEN = datetime(2026, 10, 25, 12, tzinfo=UTC)


def terminal(raw_state: str, status: int | None = None) -> ResourceObservation:
    return ResourceObservation(
        ref=REF,
        state="terminated",
        raw_state=raw_state,
        observed_at=SEEN,
        provider_http_status=status,
        lease_known=True,
        scope_complete=True,
    )


def receipt(operation_id: str = "op-1", external_id: str = EXPERIMENT) -> dict:
    return {
        "source": "delete_acknowledged",
        "external_id": external_id,
        "observed_at": SEEN.isoformat(),
        "http_status": 204,
        "operation_id": operation_id,
    }


@pytest.mark.asyncio
async def test_cloudlab_delete_acknowledgement_keeps_the_status_it_returned():
    receipt = await provider(lambda request: httpx.Response(204)).release(REF, intent())
    assert receipt.observation is not None
    assert receipt.observation.provider_http_status == 204


def test_a_read_is_provider_terminal_and_a_lease_past_absence_says_so():
    operation = {"id": "op-1", "result": {}}
    assert release_evidence(operation, terminal("terminated", 200)) == {
        "source": "provider_terminal",
        "external_id": EXPERIMENT,
        "observed_at": SEEN.isoformat(),
        "http_status": 200,
    }
    # The fallback made no provider call that returned a status.
    assert release_evidence(operation, terminal("expired_not_found")) == {
        "source": "expired_not_found",
        "external_id": EXPERIMENT,
        "observed_at": SEEN.isoformat(),
    }


def test_a_saved_delete_receipt_counts_only_for_its_own_operation_and_identity():
    operation = {"id": "op-1", "result": {"release_evidence": receipt()}}
    assert release_evidence(operation, terminal("terminated")) == receipt()
    replayed = acknowledged_release(operation, REF)
    assert replayed is not None
    assert (replayed.state, replayed.observed_at, replayed.provider_http_status) == (
        "terminated",
        SEEN,
        204,
    )
    for foreign in (
        {"id": "op-2", "result": {"release_evidence": receipt()}},
        {"id": "op-1", "result": {"release_evidence": receipt(external_id="other")}},
        {"id": "op-1", "result": {"release_evidence": {**receipt(), "source": "not_found"}}},
    ):
        assert recorded_receipt(foreign, EXPERIMENT) is None
        assert acknowledged_release(foreign, REF) is None


def test_recorded_terminal_states_name_their_source_and_local_removal_none():
    operation = {"id": "op-1", "result": {}}
    base = {"external_id": EXPERIMENT, "observed_at": SEEN}
    assert recorded_evidence(operation, {"observed_state": "absent", "external_id": None}) == {
        "source": "never_created"
    }
    assert recorded_evidence(
        operation, {**base, "observed_state": "terminated", "provider_state": "expired_not_found"}
    ) == {"source": "expired_not_found", "external_id": EXPERIMENT, "recorded_at": SEEN.isoformat()}
    assert (
        recorded_evidence(
            operation, {**base, "observed_state": "terminated", "provider_state": "destroyed"}
        )["source"]
        == "provider_terminal"
    )
    # Deregistering an attached machine is local inventory, not provider proof.
    assert (
        recorded_evidence(
            operation, {**base, "observed_state": "absent", "provider_state": "deregistered"}
        )
        is None
    )


def test_public_projection_allowlists_each_release_evidence_field():
    internal = {
        **receipt(),
        "body": "<html>private</html>",
        "configuration": {"credential_ref": "account-1"},
        "address": "192.0.2.10",
        "requested_project": "research",
    }
    shown = public_operation(
        {"id": "op-1", "state": "succeeded", "result": {"release_evidence": internal}}
    )
    assert shown["result"]["release_evidence"] == {
        "source": "delete_acknowledged",
        "observed_at": SEEN.isoformat(),
        "http_status": 204,
        "external_id": EXPERIMENT,
    }
    assert public_release_evidence({"source": "not_found", "http_status": 404}) is None
    assert public_release_evidence("delete_acknowledged") is None
    assert public_release_evidence(
        {
            "source": "provider_terminal",
            "http_status": True,
            "observed_at": "yesterday",
            "external_id": "x" * 200,
        }
    ) == {"source": "provider_terminal"}
    assert public_release_evidence({"source": "never_created"}) == {"source": "never_created"}
    assert "release_evidence" not in public_operation({"id": "op-1", "result": {}})["result"]
