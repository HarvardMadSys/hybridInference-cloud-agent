"""Focused host heartbeat batching and policy-read regressions from #748."""

from __future__ import annotations

from typing import Any

import pytest

pytest_plugins = ("tests.unit.test_host_pool_api",)

from tests.unit.test_host_pool_api import (
    _CHECKPOINT,
    _TARGET,
    FakeHostPoolStore,
    RecordingGrants,
    _enabled_archive,
    _enroll,
    _retained_with_checkpoint,
)

from cloud_agent.api import hosts


async def test_a_heartbeat_answers_from_the_policy_its_own_write_read(client, monkeypatch):
    """The policy the beat's write share-locks is the one the answer is made of.

    Nothing reads it again, and with no agent update waiting, nothing tries
    to switch one.
    """
    async_client, store = client
    credential = await _enroll(async_client, store, capabilities=["sandbox-checkpoints-v1"])
    _enabled_archive(store, checkpoints_enabled=False)
    store.global_settings = {"agent_timeout_s": 5400}
    store.runtime_upgrades["codex"] = {**_TARGET, "version": "0.162.1", "activated_at": "x"}

    async def reread() -> Any:
        raise AssertionError("the heartbeat read the policy again")

    for name in (
        "host_settings",
        "sandbox_archive_settings",
        "runtime_download_targets",
        "activate_ready_runtime_upgrades",
    ):
        monkeypatch.setattr(store, name, reread)
    answer = (
        await async_client.post(
            "/v1/agent/host/heartbeat", json={}, headers={"Authorization": f"Bearer {credential}"}
        )
    ).json()

    assert answer["settings"]["agent_timeout_s"] == 5400
    assert answer["sandbox_archive_settings"]["revision"] == 5
    assert answer["sandbox_archive_settings"]["checkpoints_enabled"] is False
    assert [target["version"] for target in answer["runtime_downloads"]] == ["0.162.1"]


async def test_the_beat_that_switches_an_update_answers_with_what_it_switched_to(
    client, monkeypatch
):
    async_client, store = client
    credential = await _enroll(async_client, store)
    store.runtime_upgrades["claude-code"] = dict(_TARGET)
    switched = [{"runtime": "claude-code", "version": "2.1.300", "before": "b", "nonce": "n"}]

    async def after_the_switch() -> list[dict[str, str]]:
        return switched

    monkeypatch.setattr(store, "runtime_download_targets", after_the_switch)
    answer = (
        await async_client.post(
            "/v1/agent/host/heartbeat",
            json={"runtime_versions_available": {"claude-code": ["2.1.220", "2.1.300"]}},
            headers={"Authorization": f"Bearer {credential}"},
        )
    ).json()

    assert store.runtime_versions == {"claude-code": "2.1.300"}
    assert answer["runtime_downloads"] == switched


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
    client, store, grants = residency_client
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
    assert grants.revoked == ["agr-open"]


async def test_every_live_member_of_a_beat_is_extended_in_one_batch(residency_client):
    client, store, grants = residency_client
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
    assert grants.revoked == []


async def test_a_failed_residency_beat_is_never_answered_as_lost():
    """The host tears a sandbox down on a lost answer; a database failure is not one."""
    from fastapi import BackgroundTasks

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
            background_tasks=BackgroundTasks(),
            host={"host": "runner-a", "credential_hash": "credential-hash"},
            store=store,
            grants=RecordingGrants(),
        )
    assert store.beat_batches == [[("athr_a", 1)]]


async def test_a_recorded_checkpoint_receipt_is_not_offered_again(client):
    """Hosts repeat their latest receipt on every beat; only a new one is recorded."""
    async_client, store = client
    credential = await _enroll(async_client, store)
    later = {**_CHECKPOINT, "checkpoint_id": "f" * 32}

    for checkpoint in (_CHECKPOINT, _CHECKPOINT, later, later):
        response = await async_client.post(
            "/v1/agent/host/heartbeat",
            json={"retained_sandboxes": [_retained_with_checkpoint(checkpoint)]},
            headers={"Authorization": f"Bearer {credential}"},
        )
        assert response.status_code == 200

    assert [recorded["checkpoint"]["checkpoint_id"] for recorded in store.checkpoints] == [
        "c" * 32,
        "f" * 32,
    ]
    assert len(store.sandbox_observations) == 4


async def test_a_failed_checkpoint_lookup_still_offers_the_receipt(client, monkeypatch):
    async_client, store = client
    credential = await _enroll(async_client, store)

    async def unavailable(*, checkpoint_ids: list[str]) -> set[str]:
        raise RuntimeError("database unavailable")

    monkeypatch.setattr(store, "recorded_sandbox_checkpoints", unavailable)
    response = await async_client.post(
        "/v1/agent/host/heartbeat",
        json={"retained_sandboxes": [_retained_with_checkpoint(_CHECKPOINT)]},
        headers={"Authorization": f"Bearer {credential}"},
    )

    assert response.status_code == 200
    assert [recorded["checkpoint"]["checkpoint_id"] for recorded in store.checkpoints] == ["c" * 32]


async def test_a_due_stop_is_fenced_to_the_activation_the_beat_reported(residency_client):
    """The database answers a placeholder; the stop must name the live activation.

    The sandbox manager refuses a stop whose activation differs from its
    member's, so a placeholder made every control-plane stop of a used chat
    stale (staging: activation 0 sent, 3 current).
    """
    client, store, _grants = residency_client
    credential = await _enroll(client, store)
    store.sandbox_stop_due = {
        "thread_id": "athr_1",
        "generation": 3,
        "activation_generation": 0,
        "reason": "idle",
        "member_activation_generations": {"athr_1": 3},
    }

    response = await client.post(
        "/v1/agent/host/heartbeat",
        json={
            "retained_sandboxes": [
                {
                    "thread_id": "athr_1",
                    "sandbox_id": "asbx_1",
                    "container_id": "0123456789abcdef",
                    "generation": 3,
                    "activation_generation": 3,
                    "running": True,
                }
            ]
        },
        headers={"Authorization": f"Bearer {credential}"},
    )

    assert response.status_code == 200, response.text
    assert response.json()["retained_sandboxes_stop"] == [
        {**store.sandbox_stop_due, "activation_generation": 3, "shared_live": True}
    ]


async def test_stopped_shared_group_releases_and_revokes_each_member_residency(residency_client):
    client, store, grants = residency_client
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
    assert grants.renewed == []
    assert grants.revoked == ["agr-stopped", "agr-stopped"]
    assert response.json()["residencies_lost"] == []
