"""The residencies a heartbeat extends and releases, and the grants it renews.

A beat extends its live members' residencies in one batch, offers for release
only those still open, and renews their grants after its reply, and only once
a grant runs low. These cases reuse the pool suite's fake store.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any

import pytest
from httpx import ASGITransport, AsyncClient

# Imported pytest fixtures are intentionally named by test parameters.
# ruff: noqa: F811
from tests.unit.test_host_pool_api import (  # noqa: F401 - shared pytest fixtures
    FakeHostPoolStore,
    RecordingGrants,
    _build_app,
    _enroll,
    _live_group,
    residency_client,
)

from cloud_agent.api import admin_hosts, hosts
from cloud_agent.api.deps import get_grant_client
from cloud_agent.grants import GATEWAY_RENEWAL_STEP_S, RENEWAL_DUE_WITHIN_S, RenewalSchedule
from cloud_agent.host_pool import HEARTBEAT_INTERVAL_S


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
            "retained_sandboxes": [
                _live_group(("th-linked", 1), ("th-grantless", 1), ("th-lost", 3))
            ]
        },
        headers={"Authorization": f"Bearer {credential}"},
    )

    assert response.status_code == 200, response.text
    assert grants.renewed == ["agr-1"]
    assert response.json()["residencies_lost"] == [{"thread_id": "th-lost", "generation": 3}]


async def test_a_slow_gateway_does_not_hold_the_heartbeat_reply():
    """Renewals run after the reply, and together.

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
        {"retained_sandboxes": [_live_group(("th-1", 1), ("th-2", 1))]}
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


class SteppedGrants(RecordingGrants):
    """Renewals that buy the gateway's step from the moment they are asked for."""

    def __init__(self, wall_clock: Any) -> None:
        super().__init__()
        self._wall_clock = wall_clock

    async def renew(self, grant_id: str) -> dict[str, Any]:
        await super().renew(grant_id)
        expires_at = self._wall_clock() + timedelta(seconds=GATEWAY_RENEWAL_STEP_S)
        return {"grant_id": grant_id, "expires_at": expires_at.isoformat()}


async def test_a_beat_renews_only_the_residency_grants_running_low():
    """Every beat extends its residencies; their grants need renewing far less often.

    Renewed on every thirty-second beat, each resident member's grant cost the
    gateway ten renewals for every step it granted. A grant whose last renewal
    has plenty left now waits for a later beat; one that has run low is
    renewed, still after the reply, and what it bought is recorded.
    """
    from fastapi import BackgroundTasks

    from cloud_agent.schemas import HostHeartbeatRequest

    clock = SimpleNamespace(elapsed=0.0)
    epoch = datetime(2026, 10, 21, 12, 0, tzinfo=UTC)

    def wall() -> datetime:
        return epoch + timedelta(seconds=clock.elapsed)

    schedule = RenewalSchedule(monotonic=lambda: clock.elapsed, wall_clock=wall)
    grants = SteppedGrants(wall)
    store = FakeHostPoolStore()
    store.seed("runner-a", credential_hash="credential-hash", enrolled=True)
    store.beat_answers = {("th-fresh", 1): "agr-fresh", ("th-low", 1): "agr-low"}
    body = HostHeartbeatRequest.model_validate(
        {"retained_sandboxes": [_live_group(("th-fresh", 1), ("th-low", 1))]}
    )

    async def beat_at(elapsed: float) -> list[str]:
        clock.elapsed = elapsed
        background = BackgroundTasks()
        before = len(grants.renewed)
        await hosts.heartbeat_host(
            body,
            background_tasks=background,
            host={"host": "runner-a", "credential_hash": "credential-hash"},
            store=store,
            grants=grants,
            schedule=schedule,
        )
        assert len(grants.renewed) == before, "the reply waited on the gateway"
        await background()
        return grants.renewed[before:]

    # agr-fresh was renewed a beat ago; agr-low has no more than the threshold left.
    fresh_left = GATEWAY_RENEWAL_STEP_S - HEARTBEAT_INTERVAL_S
    schedule.record(
        "agr-fresh", {"expires_at": (wall() + timedelta(seconds=fresh_left)).isoformat()}
    )
    schedule.record(
        "agr-low", {"expires_at": (wall() + timedelta(seconds=RENEWAL_DUE_WITHIN_S)).isoformat()}
    )

    assert await beat_at(0) == ["agr-low"]
    assert await beat_at(1) == [], "a grant renewed on the last beat was renewed again"
    assert await beat_at(fresh_left - RENEWAL_DUE_WITHIN_S) == ["agr-fresh"]


async def test_heartbeats_share_the_process_renewal_schedule(monkeypatch):
    """The schedule a beat consults is the process's, which the lifespan sets.

    A record built per request would know nothing, and renew every grant on
    every beat as before.
    """
    monkeypatch.setenv("AGENT_CONTROL_TOKEN_SECRET", "unit-test-control-secret")
    monkeypatch.setenv("AGENT_SANDBOX_MANAGER_TOKEN", "unit-test-sandbox-manager-secret")

    async def _no_audit(*_args, **_kwargs):
        return None

    monkeypatch.setattr(admin_hosts, "log_admin_action", _no_audit)
    monkeypatch.setattr(admin_hosts, "get_client_ip", lambda _request: "127.0.0.1")
    store = FakeHostPoolStore()
    store.beat_answers = {("th-1", 1): "agr-1"}
    grants = SteppedGrants(lambda: datetime.now(UTC))
    app = _build_app(store)
    app.dependency_overrides[get_grant_client] = lambda: grants
    app.state.renewal_schedule = RenewalSchedule()

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        credential = await _enroll(client, store)
        for _ in range(2):
            response = await client.post(
                "/v1/agent/host/heartbeat",
                json={"retained_sandboxes": [_live_group(("th-1", 1))]},
                headers={"Authorization": f"Bearer {credential}"},
            )
            assert response.status_code == 200, response.text

    assert grants.renewed == ["agr-1"]
    assert "agr-1" in app.state.renewal_schedule


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
