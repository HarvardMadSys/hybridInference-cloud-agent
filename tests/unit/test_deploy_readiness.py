"""A control-plane rollout cannot outrun its exact shared-live host path."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from cloud_agent.deploy_readiness import shared_live_readiness
from cloud_agent_host.shared_protocol import (
    NATIVE_FORK_AT_ANCHOR_CAPABILITY,
    SHARED_SANDBOX_CAPABILITY,
    shared_deployment_capability,
)

DEPLOYMENT_SHA = "abc123"


class _Store:
    def __init__(self, rows: list[dict[str, Any]], *, active_host: str | None = None) -> None:
        self.rows = rows
        self.active_host = active_host

    async def runner_pool(self) -> tuple[list[dict[str, Any]], str | None, dict[str, str]]:
        return self.rows, self.active_host, {}


def _host(now: datetime, **overrides: Any) -> dict[str, Any]:
    row: dict[str, Any] = {
        "host": "runner-a",
        "status": "online",
        "enrolled": True,
        "capabilities": [
            SHARED_SANDBOX_CAPABILITY,
            NATIVE_FORK_AT_ANCHOR_CAPABILITY,
            shared_deployment_capability(DEPLOYMENT_SHA),
        ],
        "slots_total": 3,
        "last_heartbeat_at": now,
        "broker_url": "http://broker-a:8092",
    }
    row.update(overrides)
    return row


@pytest.mark.asyncio
async def test_ready_requires_a_post_start_heartbeat_and_authoritative_broker_probe() -> None:
    started = datetime.now(UTC)
    calls: list[tuple[str, str, bool]] = []

    async def probe(host: str, url: str, *, force: bool = False) -> None:
        calls.append((host, url, force))

    verdict = await shared_live_readiness(
        _Store([_host(started + timedelta(seconds=1))]),
        not_before=started,
        broker_token="broker-token",
        expected_deployment_sha=DEPLOYMENT_SHA,
        broker_probe=probe,
    )

    assert verdict.ready is True
    assert verdict.host == "runner-a"
    assert calls == [("runner-a", "http://broker-a:8092", True)]


@pytest.mark.asyncio
async def test_a_stale_capability_row_cannot_release_the_deploy_barrier() -> None:
    started = datetime.now(UTC)
    probed = False

    async def probe(*_args: Any, **_kwargs: Any) -> None:
        nonlocal probed
        probed = True

    verdict = await shared_live_readiness(
        _Store([_host(started - timedelta(seconds=1))]),
        not_before=started,
        broker_token="broker-token",
        expected_deployment_sha=DEPLOYMENT_SHA,
        broker_probe=probe,
    )

    assert verdict.ready is False
    assert "since this deployment check began" in verdict.detail
    assert probed is False


@pytest.mark.asyncio
async def test_a_control_plane_that_cannot_use_the_broker_stays_closed() -> None:
    started = datetime.now(UTC)

    async def probe(*_args: Any, **_kwargs: Any) -> str:
        return "401 from broker health"

    verdict = await shared_live_readiness(
        _Store([_host(started + timedelta(seconds=1))]),
        not_before=started,
        broker_token="broker-token",
        expected_deployment_sha=DEPLOYMENT_SHA,
        broker_probe=probe,
    )

    assert verdict.ready is False
    assert "control plane cannot use its broker" in verdict.detail


@pytest.mark.asyncio
async def test_the_active_host_pin_cannot_be_bypassed_by_another_ready_host() -> None:
    started = datetime.now(UTC)
    rows = [
        _host(
            started + timedelta(seconds=1),
            host="pinned-host",
            capabilities=[],
        ),
        _host(started + timedelta(seconds=1), host="other-ready-host"),
    ]

    verdict = await shared_live_readiness(
        _Store(rows, active_host="pinned-host"),
        not_before=started,
        broker_token="broker-token",
        expected_deployment_sha=DEPLOYMENT_SHA,
    )

    assert verdict.ready is False
    assert "pinned host 'pinned-host'" in verdict.detail
    assert "has not advertised shared-sandbox-v1" in verdict.detail


@pytest.mark.asyncio
async def test_every_unpinned_schedulable_host_must_finish_the_target_deploy() -> None:
    started = datetime.now(UTC)
    probed = False

    async def probe(*_args: Any, **_kwargs: Any) -> None:
        nonlocal probed
        probed = True

    rows = [
        _host(started + timedelta(seconds=1), host="target-ready"),
        _host(
            started + timedelta(seconds=1),
            host="old-shared-host",
            capabilities=[SHARED_SANDBOX_CAPABILITY, NATIVE_FORK_AT_ANCHOR_CAPABILITY],
        ),
        _host(
            started - timedelta(days=1),
            host="offline-old-host",
            status="offline",
            capabilities=[SHARED_SANDBOX_CAPABILITY],
        ),
        _host(
            started + timedelta(seconds=1),
            host="online-legacy-host",
            capabilities=[],
        ),
    ]

    verdict = await shared_live_readiness(
        _Store(rows),
        not_before=started,
        broker_token="broker-token",
        expected_deployment_sha=DEPLOYMENT_SHA,
        broker_probe=probe,
    )

    assert verdict.ready is False
    assert "old-shared-host" in verdict.detail
    assert f"has not completed target deployment {DEPLOYMENT_SHA}" in verdict.detail
    assert "offline-old-host" not in verdict.detail
    assert "online-legacy-host" not in verdict.detail
    assert probed is False


@pytest.mark.asyncio
async def test_every_unpinned_target_broker_must_answer() -> None:
    started = datetime.now(UTC)

    async def probe(host: str, *_args: Any, **_kwargs: Any) -> str | None:
        return "connection refused" if host == "runner-b" else None

    verdict = await shared_live_readiness(
        _Store(
            [
                _host(started + timedelta(seconds=1), host="runner-a"),
                _host(started + timedelta(seconds=1), host="runner-b"),
            ]
        ),
        not_before=started,
        broker_token="broker-token",
        expected_deployment_sha=DEPLOYMENT_SHA,
        broker_probe=probe,
    )

    assert verdict.ready is False
    assert "runner-b: control plane cannot use its broker" in verdict.detail


@pytest.mark.asyncio
async def test_missing_control_plane_broker_credential_fails_closed() -> None:
    started = datetime.now(UTC)
    verdict = await shared_live_readiness(
        _Store([_host(started + timedelta(seconds=1))]),
        not_before=started,
        broker_token="",
        expected_deployment_sha=DEPLOYMENT_SHA,
    )

    assert verdict.ready is False
    assert "AGENT_WORKSPACE_BROKER_TOKEN" in verdict.detail


@pytest.mark.asyncio
async def test_missing_target_commit_fails_closed() -> None:
    started = datetime.now(UTC)
    verdict = await shared_live_readiness(
        _Store([_host(started + timedelta(seconds=1))]),
        not_before=started,
        broker_token="broker-token",
        expected_deployment_sha="  ",
    )

    assert verdict.ready is False
    assert "target deployment SHA is empty" in verdict.detail


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        ({"status": "offline"}, "status is offline"),
        ({"enrolled": False}, "not enrolled"),
        (
            {
                "capabilities": [
                    SHARED_SANDBOX_CAPABILITY,
                    shared_deployment_capability(DEPLOYMENT_SHA),
                ]
            },
            NATIVE_FORK_AT_ANCHOR_CAPABILITY,
        ),
        ({"slots_total": 1}, "fewer than two"),
        ({"broker_url": ""}, "no control-plane-reachable broker URL"),
    ],
)
async def test_incomplete_host_attestation_does_not_release_the_barrier(
    overrides: dict[str, Any],
    message: str,
) -> None:
    started = datetime.now(UTC)
    verdict = await shared_live_readiness(
        _Store([_host(started + timedelta(seconds=1), **overrides)]),
        not_before=started,
        broker_token="broker-token",
        expected_deployment_sha=DEPLOYMENT_SHA,
    )

    assert verdict.ready is False
    assert message in verdict.detail


@pytest.mark.asyncio
async def test_a_runner_that_started_but_did_not_finish_target_deploy_stays_closed() -> None:
    started = datetime.now(UTC)
    verdict = await shared_live_readiness(
        _Store(
            [
                _host(
                    started + timedelta(seconds=1),
                    capabilities=[
                        SHARED_SANDBOX_CAPABILITY,
                        NATIVE_FORK_AT_ANCHOR_CAPABILITY,
                    ],
                )
            ]
        ),
        not_before=started,
        broker_token="broker-token",
        expected_deployment_sha=DEPLOYMENT_SHA,
    )

    assert verdict.ready is False
    assert f"has not completed target deployment {DEPLOYMENT_SHA}" in verdict.detail
