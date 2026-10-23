"""A heartbeat's address under its former name, a residency two groups report,
and a stored policy that does not decode."""

from __future__ import annotations

import pytest
from tests.unit.test_host_pool_api import (  # noqa: F401 - shared pytest fixtures
    _TARGET,
    _enroll,
    _live_group,
    client,
    residency_client,
)

# Imported pytest fixtures are intentionally named by test parameters.
# ruff: noqa: F811
from cloud_agent_shared.sandbox_archive_settings import SandboxArchivePolicyError


async def test_a_heartbeat_still_takes_the_address_as_broker_url(client):
    """Hosts of the previous release send that name; the alias is dated."""
    async_client, store = client
    credential = await _enroll(async_client, store)

    response = await async_client.post(
        "/v1/agent/host/heartbeat",
        json={"broker_url": "http://sandbox-manager-a:8092"},
        headers={"Authorization": f"Bearer {credential}"},
    )

    assert response.status_code == 200, response.text
    assert store.rows["runner-a"]["sandbox_manager_url"] == "http://sandbox-manager-a:8092"


async def test_a_residency_two_groups_report_is_renewed_once_and_lost_once(residency_client):
    """A member that two retained groups both report is still one residency.

    The beat is the host's to shape, so the answer is per residency, not per
    report: the grant of an extended residency is renewed once, and a residency
    that was not extended comes back in ``residencies_lost`` once.
    """
    client, store, grants = residency_client
    credential = await _enroll(client, store)
    store.beat_answers = {("th-1", 1): "agr-1"}  # th-gone absent → not extended → lost
    group = _live_group(("th-1", 1), ("th-gone", 4))
    second = {**group, "sandbox_id": "asbx_2", "container_id": "fedcba9876543210"}

    response = await client.post(
        "/v1/agent/host/heartbeat",
        json={"retained_sandboxes": [group, second]},
        headers={"Authorization": f"Bearer {credential}"},
    )

    assert response.status_code == 200, response.text
    assert grants.renewed == ["agr-1"]
    assert response.json()["residencies_lost"] == [{"thread_id": "th-gone", "generation": 4}]


async def test_host_heartbeat_forwards_optional_container_telemetry(residency_client):
    from tests.unit.test_sandbox_resource_protocol import beat, sample

    client, store, _grants = residency_client
    credential = await _enroll(client, store)
    measured = sample()
    response = await client.post(
        "/v1/agent/host/heartbeat",
        json={"retained_sandboxes": [beat(resource_usage=measured, terminal_count=3)]},
        headers={"Authorization": f"Bearer {credential}"},
    )
    assert response.status_code == 200, response.text
    assert store.sandbox_observations[0]["resource_usage"] == measured
    assert store.sandbox_observations[0]["terminal_count"] == 3
    assert store.sandbox_observations[0]["host"] == "runner-a"


async def test_host_heartbeat_drops_bad_telemetry_and_still_reports_lifecycle(residency_client):
    from tests.unit.test_sandbox_resource_protocol import beat, sample

    client, store, _grants = residency_client
    credential = await _enroll(client, store)
    response = await client.post(
        "/v1/agent/host/heartbeat",
        json={"retained_sandboxes": [beat(resource_usage=sample(pids=-1), terminal_count=-1)]},
        headers={"Authorization": f"Bearer {credential}"},
    )
    assert response.status_code == 200, response.text
    observation = store.sandbox_observations[0]
    assert observation["running"] is True
    assert observation.get("resource_usage") is None
    assert observation.get("terminal_count") is None


@pytest.mark.parametrize(
    ("part", "error", "message"),
    [
        ("archive_settings", SandboxArchivePolicyError, "region is required"),
        ("host_settings", TypeError, "host settings"),
        ("runtime_policy", TypeError, "agent versions"),
    ],
)
async def test_a_policy_that_does_not_decode_fails_only_the_answer(
    residency_client, part: str, error: type[Exception], message: str
):
    """The beat's own work is done before a policy part that does not decode fails it.

    The heartbeat reads the policy in its own write. A stored archive policy,
    set of global settings or record of agent versions that does not decode
    must fail only the answer, with the error of its own read, once the beat
    has extended its residencies and reconciled its observations. A beat that
    cannot read whether an agent update waits still tries to switch one, which
    reads the updates itself.
    """
    client, store, _grants = residency_client
    credential = await _enroll(client, store)
    setattr(store, f"{part}_undecodable", True)
    store.beat_answers = {("th-1", 1): ""}

    with pytest.raises(error, match=message):
        await client.post(
            "/v1/agent/host/heartbeat",
            # The batched beat extends the live members its retained groups
            # report, so that is where the residency work is observable.
            json={"retained_sandboxes": [_live_group(("th-1", 1))]},
            headers={"Authorization": f"Bearer {credential}"},
        )

    assert store.beat_batches[0] == [("th-1", 1)]
    assert [observation["sandbox_id"] for observation in store.sandbox_observations] == ["asbx_1"]
    # No update waits in the others, and this beat's read cannot say.
    assert store.activation_attempts == (1 if part == "runtime_policy" else 0)


@pytest.mark.parametrize(
    ("part", "error"),
    [("archive_settings", SandboxArchivePolicyError), ("host_settings", TypeError)],
)
async def test_a_policy_that_does_not_decode_still_lets_the_beat_switch_an_update(
    client, part: str, error: type[Exception]
):
    """The agent update a beat completes switches before the policy fails the answer.

    Reading a part that did not decode again, before the switch, would keep
    every waiting update from switching for as long as it stayed broken.
    """
    async_client, store = client
    credential = await _enroll(async_client, store)
    store.runtime_upgrades["claude-code"] = dict(_TARGET)
    setattr(store, f"{part}_undecodable", True)

    with pytest.raises(error):
        await async_client.post(
            "/v1/agent/host/heartbeat",
            json={"runtime_versions_available": {"claude-code": ["2.1.220", "2.1.300"]}},
            headers={"Authorization": f"Bearer {credential}"},
        )

    assert store.runtime_versions == {"claude-code": "2.1.300"}
    assert store.runtime_upgrades["claude-code"]["activated_at"]
