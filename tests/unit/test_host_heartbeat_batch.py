"""Focused host heartbeat batching and policy-read regressions from #748."""

from __future__ import annotations

import asyncio
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


async def test_the_beat_that_extends_a_residency_renews_its_grant(residency_client):
    client, store, grants = residency_client
    credential = await _enroll(client, store)
    store.beat_answers = {
        ("th-linked", 1): "agr-1",  # extended, grant linked → renew
        ("th-grantless", 1): "",  # extended, workspace-only → nothing to renew
        # th-lost absent → not extended → reported lost
    }

    response = await client.post(
        "/v1/agent/host/heartbeat",
        json={
            "residencies": [
                {"thread_id": "th-linked", "generation": 1},
                {"thread_id": "th-grantless", "generation": 1},
                {"thread_id": "th-lost", "generation": 3},
            ]
        },
        headers={"Authorization": f"Bearer {credential}"},
    )

    assert response.status_code == 200, response.text
    assert grants.renewed == ["agr-1"]
    assert response.json()["residencies_lost"] == [{"thread_id": "th-lost", "generation": 3}]


async def test_a_slow_gateway_does_not_hold_the_heartbeat_reply():
    """Renewals run after the reply, together, and once per grant.

    A host's heartbeat times out at 15 s, and each renewal may take the
    gateway's full request timeout. Awaited one by one before the reply, two
    slow renewals were enough to time the beat out — and the host then never
    received the stops and lost residencies the reply carries. Here every
    renewal blocks until all of them have started: awaited inline the reply
    never comes, and awaited one at a time the renewals never finish.
    """
    from fastapi import BackgroundTasks

    from cloud_agent.schemas import HostHeartbeatRequest

    class GatedGrants(RecordingGrants):
        def __init__(self, expected: int) -> None:
            super().__init__()
            self.started: list[str] = []
            self._expected = expected
            self._all_started = asyncio.Event()

        async def renew(self, grant_id: str) -> dict[str, Any]:
            self.started.append(grant_id)
            if len(self.started) >= self._expected:
                self._all_started.set()
            await self._all_started.wait()
            return await super().renew(grant_id)

    store = FakeHostPoolStore()
    store.seed("runner-a", credential_hash="credential-hash", enrolled=True)
    store.beat_answers = {("th-1", 1): "agr-1", ("th-2", 1): "agr-2"}
    grants = GatedGrants(expected=2)
    body = HostHeartbeatRequest.model_validate(
        {
            "residencies": [
                {"thread_id": "th-1", "generation": 1},
                {"thread_id": "th-2", "generation": 1},
            ],
            # The same member again, as a live member of its retained group:
            # one extended residency is one renewal, however it was reported.
            "retained_sandboxes": [
                {
                    "thread_id": "th-1",
                    "sandbox_id": "asbx_1",
                    "container_id": "0123456789abcdef",
                    "generation": 3,
                    "activation_generation": 7,
                    "running": True,
                    "activity_active": True,
                    "idle_remaining_seconds": 1799.5,
                    "shared_live": True,
                    "members": [
                        {
                            "thread_id": "th-1",
                            "native_session_id": "native-1",
                            "process_generation": 2,
                            "residency_generation": 1,
                            "resident_state": "live",
                            "resident_slot_id": 0,
                        }
                    ],
                }
            ],
        }
    )
    background = BackgroundTasks()

    reply = await asyncio.wait_for(
        hosts.heartbeat_host(
            body,
            background_tasks=background,
            host={"host": "runner-a", "credential_hash": "credential-hash"},
            store=store,
            grants=grants,
        ),
        timeout=5,
    )

    assert reply.residencies_lost == []
    assert grants.started == [], "the reply waited on the gateway"
    await asyncio.wait_for(background(), timeout=5)
    assert sorted(grants.renewed) == ["agr-1", "agr-2"]
