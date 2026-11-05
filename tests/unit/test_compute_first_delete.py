"""A committed first DELETE does not depend on another provider read.

Uncertain DELETE outcomes still require observation before replay. These
tests drive the reconciler through the durable submission marker and its
error handling, rather than bypassing them with a release helper stub.
"""

import pytest
from tests.unit.test_compute_reconcile import NOW, REF, harness, observed

from cloud_agent.ports.external.compute_provider import ProviderError, ProviderReceipt


def acknowledged():
    return ProviderReceipt(
        outcome="confirmed",
        resource=REF,
        observation=observed(state="terminated").model_copy(
            update={"raw_state": "delete_acknowledged", "provider_http_status": 204}
        ),
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("requested", [None, False])
async def test_first_committed_delete_survives_an_unavailable_read(monkeypatch, requested):
    h = harness(monkeypatch, kind="retire", identified=True)
    h.allocation.update(release_committed_at=NOW, desired_state="absent")
    if requested is not None:
        h.operation["result"]["release_requested"] = requested
    h.provider.inspect.side_effect = ProviderError(
        "temporarily_unavailable", "Read unavailable", retryable=True
    )
    h.provider.release.return_value = acknowledged()

    await h.reconciler.execute(h.operation)

    assert h.operation["state"] == "succeeded"
    assert h.allocation["observed_state"] == "terminated"
    h.provider.inspect.assert_not_awaited()
    h.provider.release.assert_awaited_once()
    assert h.provider.release.await_args.args[0] == REF
    proof = h.operation["result"]["release_evidence"]
    assert proof["source"] == "delete_acknowledged"
    assert proof["http_status"] == 204
    assert proof["external_id"] == REF.external_id
    assert proof["operation_id"] == h.operation["id"]


@pytest.mark.asyncio
@pytest.mark.parametrize("outcome", ["confirmed", "pending", "unknown"])
async def test_post_delete_read_failure_keeps_uncertainty_until_observed(monkeypatch, outcome):
    h = harness(monkeypatch, kind="retire", identified=True)
    h.allocation.update(release_committed_at=NOW, desired_state="absent")
    h.provider.release.return_value = ProviderReceipt(outcome=outcome, resource=REF)
    h.provider.inspect.side_effect = ProviderError(
        "temporarily_unavailable", "Read unavailable", retryable=True
    )

    await h.reconciler.execute(h.operation)

    h.provider.release.assert_awaited_once()
    assert h.operation["state"] == "waiting"
    assert h.operation["result"]["release_requested"] is True
    assert "release_evidence" not in h.operation["result"]
    h.provider.inspect.side_effect = None
    h.provider.inspect.return_value = observed(state="pending")
    await h.reconciler.execute(h.operation)
    assert h.operation["state"] == "waiting"
    assert h.operation["step"] == "confirming_release"
    assert h.provider.release.await_count == 1


@pytest.mark.asyncio
async def test_known_delete_rejection_allows_first_submission_without_read(monkeypatch):
    h = harness(monkeypatch, kind="retire", identified=True)
    h.allocation.update(release_committed_at=NOW, desired_state="absent")
    h.provider.inspect.side_effect = ProviderError(
        "temporarily_unavailable", "Read unavailable", retryable=True
    )
    h.provider.release.side_effect = ProviderError(
        "temporarily_unavailable", "Not submitted", retryable=True
    )

    await h.reconciler.execute(h.operation)

    h.provider.release.assert_awaited_once()
    assert h.operation["result"]["release_requested"] is False
    h.provider.release.side_effect = None
    h.provider.release.return_value = acknowledged()
    await h.reconciler.execute(h.operation)
    assert h.operation["state"] == "succeeded"
    assert h.provider.release.await_count == 2
    h.provider.inspect.assert_not_awaited()


@pytest.mark.asyncio
async def test_unknown_delete_exception_requires_observation_before_replay(monkeypatch):
    h = harness(monkeypatch, kind="retire", identified=True)
    h.allocation.update(release_committed_at=NOW, desired_state="absent")
    h.provider.release.side_effect = ProviderError("result_unknown", "Response lost")
    h.provider.inspect.return_value = observed(state="pending")

    await h.reconciler.execute(h.operation)

    h.provider.release.assert_awaited_once()
    assert h.operation["result"]["release_requested"] is True
    h.provider.inspect.assert_not_awaited()
    await h.reconciler.execute(h.operation)
    h.provider.inspect.assert_awaited_once()
    assert h.provider.release.await_count == 1
    assert h.operation["state"] == "waiting"
