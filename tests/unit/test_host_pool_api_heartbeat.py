"""A heartbeat's address under its former name, and a residency two groups report."""

from __future__ import annotations

# Imported pytest fixtures are intentionally named by test parameters.
# ruff: noqa: F811
from tests.unit.test_host_pool_api import (  # noqa: F401 - shared pytest fixtures
    _enroll,
    _live_group,
    client,
    residency_client,
)


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
    report: a residency that was not extended comes back in ``residencies_lost``
    once.
    """
    client, store = residency_client
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
    assert response.json()["residencies_lost"] == [{"thread_id": "th-gone", "generation": 4}]
