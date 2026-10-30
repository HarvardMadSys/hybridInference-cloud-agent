"""The residencies a heartbeat extends and releases.

A beat extends its live members' residencies in one batch and offers for
release only those still open. These cases reuse the pool suite's fake store.
"""

from __future__ import annotations

from typing import Any

import pytest

# Imported pytest fixtures are intentionally named by test parameters.
# ruff: noqa: F811
from tests.unit.test_host_pool_api import (  # noqa: F401 - shared pytest fixtures
    FakeHostPoolStore,
    _build_app,
    _enroll,
    _live_group,
    residency_client,
)

from cloud_agent.api import hosts


async def test_the_beat_reports_a_residency_it_did_not_extend_as_lost(residency_client):
    client, store = residency_client
    credential = await _enroll(client, store)
    store.beat_answers = {
        ("th-linked", 1): "agr-1",  # extended, grant linked → renew
        ("th-grantless", 1): "",  # extended, workspace-only → nothing to renew
        # th-lost absent → not extended → reported lost
    }

    response = await client.post(
        "/v1/agent/host/heartbeat",
        json={
            "retained_sandboxes": [
                _live_group(("th-linked", 1), ("th-grantless", 1), ("th-lost", 3))
            ]
        },
        headers={"Authorization": f"Bearer {credential}"},
    )

    assert response.status_code == 200, response.text
    assert response.json()["residencies_lost"] == [{"thread_id": "th-lost", "generation": 3}]


async def test_stopped_shared_group_releases_each_member_residency(residency_client):
    client, store = residency_client
    credential = await _enroll(client, store)
    store.release_answer = "agr-stopped"

    response = await client.post(
        "/v1/agent/host/heartbeat",
        json={
            "retained_sandboxes": [
                {
                    "thread_id": "athr_source",
                    "sandbox_id": "asbx_shared",
                    "container_id": "0123456789abcdef",
                    "generation": 1,
                    "activation_generation": 2,
                    "running": False,
                    "shared_live": True,
                    "members": [
                        {
                            "thread_id": "athr_source",
                            "native_session_id": "native-source",
                            "process_generation": 11,
                            "residency_generation": 7,
                            "resident_state": "stopped",
                            "resident_slot_id": 0,
                        },
                        {
                            "thread_id": "athr_child",
                            "native_session_id": "native-child",
                            "process_generation": 12,
                            "residency_generation": 8,
                            "resident_state": "stopped",
                            "resident_slot_id": 1,
                        },
                    ],
                }
            ]
        },
        headers={"Authorization": f"Bearer {credential}"},
    )

    assert response.status_code == 200, response.text
    assert store.release_calls == [
        ("athr_source", 7, "runner-a", 11),
        ("athr_child", 8, "runner-a", 12),
    ]
    assert response.json()["residencies_lost"] == []


def _shared_member(thread_id: str, generation: int, resident_state: str) -> dict[str, Any]:
    return {
        "thread_id": thread_id,
        "process_generation": 3,
        "residency_generation": generation,
        "resident_state": resident_state,
    }


def _shared_group(sandbox_id: str, *, running: bool, members: list[dict[str, Any]]) -> dict:
    return {
        "thread_id": members[0]["thread_id"],
        "sandbox_id": sandbox_id,
        "container_id": f"container-{sandbox_id}",
        "generation": 1,
        "activation_generation": 1,
        "running": running,
        "members": members,
    }


async def test_a_beat_offers_for_release_only_the_residencies_still_open(residency_client):
    """A stopped group stays listed until its chats are deleted; its release happens once."""
    client, store = residency_client
    credential = await _enroll(client, store)
    store.release_answer = "agr-open"
    store.closed_residencies = {("asbx_stopped", "athr_released", 4)}

    response = await client.post(
        "/v1/agent/host/heartbeat",
        json={
            "retained_sandboxes": [
                _shared_group(
                    "asbx_stopped",
                    running=False,
                    members=[
                        _shared_member("athr_released", 4, "stopped"),
                        _shared_member("athr_open", 5, "stopped"),
                    ],
                )
            ]
        },
        headers={"Authorization": f"Bearer {credential}"},
    )

    assert response.status_code == 200, response.text
    assert store.release_calls == [("athr_open", 5, "runner-a", 3)]


async def test_every_live_member_of_a_beat_is_extended_in_one_batch(residency_client):
    client, store = residency_client
    credential = await _enroll(client, store)
    store.beat_answers = {("athr_a", 1): "agr-a", ("athr_b", 2): ""}

    response = await client.post(
        "/v1/agent/host/heartbeat",
        json={
            "retained_sandboxes": [
                _shared_group(
                    "asbx_one",
                    running=True,
                    members=[
                        _shared_member("athr_a", 1, "live"),
                        # Still launching, or released: neither is beaten.
                        _shared_member("athr_starting", 1, "starting"),
                        _shared_member("athr_never", 0, "live"),
                    ],
                ),
                _shared_group(
                    "asbx_two",
                    running=True,
                    members=[
                        _shared_member("athr_b", 2, "live"),
                        _shared_member("athr_gone", 3, "live"),
                    ],
                ),
            ]
        },
        headers={"Authorization": f"Bearer {credential}"},
    )

    assert response.status_code == 200, response.text
    assert store.beat_batches == [[("athr_a", 1), ("athr_b", 2), ("athr_gone", 3)]]
    assert response.json()["residencies_lost"] == [{"thread_id": "athr_gone", "generation": 3}]
    assert store.release_calls == []


async def test_a_failed_residency_beat_is_never_answered_as_lost():
    """The host tears a sandbox down on a lost answer; a database failure is not one."""

    from cloud_agent.schemas import HostHeartbeatRequest

    store = FakeHostPoolStore()
    store.seed("runner-a", credential_hash="credential-hash", enrolled=True)
    store.fail_beats = True
    body = HostHeartbeatRequest.model_validate(
        {
            "retained_sandboxes": [
                _shared_group(
                    "asbx_one", running=True, members=[_shared_member("athr_a", 1, "live")]
                )
            ]
        }
    )

    with pytest.raises(RuntimeError, match="database unavailable"):
        await hosts.heartbeat_host(
            body,
            host={"host": "runner-a", "credential_hash": "credential-hash"},
            store=store,
        )
    assert store.beat_batches == [[("athr_a", 1)]]
