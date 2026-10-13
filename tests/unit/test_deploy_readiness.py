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
    def __init__(
        self,
        rows: list[dict[str, Any]],
        *,
        active_host: str | None = None,
        retained_hosts: set[str] | None = None,
    ) -> None:
        self.rows = rows
        self.active_host = active_host
        self.retained_hosts = retained_hosts or set()

    async def deployment_runner_pool(
        self,
    ) -> tuple[list[dict[str, Any]], str | None, dict[str, str], set[str]]:
        return self.rows, self.active_host, {}, self.retained_hosts


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
        "sandbox_manager_url": "http://sandbox-manager-a:8092",
    }
    row.update(overrides)
    return row


@pytest.mark.asyncio
async def test_ready_requires_a_post_start_heartbeat_and_authoritative_sandbox_manager_probe() -> (
    None
):
    started = datetime.now(UTC)
    calls: list[tuple[str, str, bool]] = []

    async def probe(host: str, url: str, *, force: bool = False) -> None:
        calls.append((host, url, force))

    verdict = await shared_live_readiness(
        _Store([_host(started + timedelta(seconds=1))]),
        not_before=started,
        sandbox_manager_token="sandbox-manager-token",
        expected_deployment_sha=DEPLOYMENT_SHA,
        sandbox_manager_probe=probe,
    )

    assert verdict.ready is True
    assert verdict.host == "runner-a"
    assert calls == [("runner-a", "http://sandbox-manager-a:8092", True)]


@pytest.mark.asyncio
async def test_marker_change_during_sandbox_manager_probe_keeps_the_barrier_closed() -> None:
    started = datetime.now(UTC)
    ready = _host(started + timedelta(seconds=1))
    pending = _host(
        started + timedelta(seconds=2),
        capabilities=[SHARED_SANDBOX_CAPABILITY, NATIVE_FORK_AT_ANCHOR_CAPABILITY],
    )

    class ChangingStore(_Store):
        def __init__(self) -> None:
            super().__init__([ready])
            self.calls = 0

        async def deployment_runner_pool(self):
            self.calls += 1
            rows = [ready] if self.calls == 1 else [pending]
            return rows, None, {}, set()

    verdict = await shared_live_readiness(
        ChangingStore(),
        not_before=started,
        sandbox_manager_token="sandbox-manager-token",
        expected_deployment_sha=DEPLOYMENT_SHA,
        sandbox_manager_probe=lambda *_args, **_kwargs: _async_none(),
    )

    assert verdict.ready is False
    assert "changed while its sandbox manager was probed" in verdict.detail


async def _async_none() -> None:
    return None


@pytest.mark.asyncio
async def test_an_explicit_idle_draining_host_can_satisfy_the_deploy_barrier() -> None:
    started = datetime.now(UTC)
    calls: list[tuple[str, str, bool]] = []

    async def probe(host: str, url: str, *, force: bool = False) -> None:
        calls.append((host, url, force))

    verdict = await shared_live_readiness(
        _Store(
            [
                _host(
                    started + timedelta(seconds=1),
                    status="draining",
                    slots_used=0,
                )
            ]
        ),
        not_before=started,
        sandbox_manager_token="sandbox-manager-token",
        expected_deployment_sha=DEPLOYMENT_SHA,
        draining_host="runner-a",
        sandbox_manager_probe=probe,
    )

    assert verdict.ready is True
    assert calls == [("runner-a", "http://sandbox-manager-a:8092", True)]


@pytest.mark.asyncio
async def test_draining_host_is_still_rejected_without_the_explicit_mode() -> None:
    started = datetime.now(UTC)

    verdict = await shared_live_readiness(
        _Store(
            [
                _host(
                    started + timedelta(seconds=1),
                    status="draining",
                    slots_used=0,
                )
            ]
        ),
        not_before=started,
        sandbox_manager_token="sandbox-manager-token",
        expected_deployment_sha=DEPLOYMENT_SHA,
    )

    assert verdict.ready is False
    assert "status is draining" in verdict.detail


@pytest.mark.asyncio
async def test_explicit_draining_host_must_be_idle() -> None:
    started = datetime.now(UTC)

    verdict = await shared_live_readiness(
        _Store(
            [
                _host(
                    started + timedelta(seconds=1),
                    status="draining",
                    slots_used=1,
                )
            ]
        ),
        not_before=started,
        sandbox_manager_token="sandbox-manager-token",
        expected_deployment_sha=DEPLOYMENT_SHA,
        draining_host="runner-a",
    )

    assert verdict.ready is False
    assert "slots_used is 1" in verdict.detail


@pytest.mark.asyncio
async def test_explicit_draining_mode_never_accepts_a_different_host() -> None:
    started = datetime.now(UTC)

    verdict = await shared_live_readiness(
        _Store(
            [
                _host(
                    started + timedelta(seconds=1),
                    host="runner-b",
                    status="draining",
                    slots_used=0,
                )
            ]
        ),
        not_before=started,
        sandbox_manager_token="sandbox-manager-token",
        expected_deployment_sha=DEPLOYMENT_SHA,
        draining_host="runner-a",
    )

    assert verdict.ready is False
    assert "draining host 'runner-a' is not registered" in verdict.detail


@pytest.mark.asyncio
async def test_a_pinned_host_and_a_different_idle_draining_host_both_pass() -> None:
    started = datetime.now(UTC)
    calls: list[tuple[str, str, bool]] = []

    async def probe(host: str, url: str, *, force: bool = False) -> None:
        calls.append((host, url, force))

    verdict = await shared_live_readiness(
        _Store(
            [
                _host(started + timedelta(seconds=1), host="pinned-host"),
                _host(
                    started + timedelta(seconds=1),
                    host="rolled-host",
                    status="draining",
                    slots_used=0,
                    sandbox_manager_url="http://sandbox-manager-b:8092",
                ),
            ],
            active_host="pinned-host",
        ),
        not_before=started,
        sandbox_manager_token="sandbox-manager-token",
        expected_deployment_sha=DEPLOYMENT_SHA,
        draining_host="rolled-host",
        sandbox_manager_probe=probe,
    )

    assert verdict.ready is True
    assert "pinned-host" in verdict.detail
    assert "rolled-host" in verdict.detail
    assert calls == [
        ("pinned-host", "http://sandbox-manager-a:8092", True),
        ("rolled-host", "http://sandbox-manager-b:8092", True),
    ]


@pytest.mark.asyncio
async def test_a_different_draining_host_does_not_excuse_an_unready_pin() -> None:
    started = datetime.now(UTC)
    pin = _host(started + timedelta(seconds=1), host="pinned-host")
    pin["capabilities"] = [SHARED_SANDBOX_CAPABILITY, NATIVE_FORK_AT_ANCHOR_CAPABILITY]

    verdict = await shared_live_readiness(
        _Store(
            [
                pin,
                _host(
                    started + timedelta(seconds=1),
                    host="rolled-host",
                    status="draining",
                    slots_used=0,
                ),
            ],
            active_host="pinned-host",
        ),
        not_before=started,
        sandbox_manager_token="sandbox-manager-token",
        expected_deployment_sha=DEPLOYMENT_SHA,
        draining_host="rolled-host",
    )

    assert verdict.ready is False
    assert "pinned-host" in verdict.detail
    assert "has not completed target deployment" in verdict.detail


@pytest.mark.asyncio
async def test_the_pinned_host_can_itself_be_the_idle_draining_host() -> None:
    started = datetime.now(UTC)

    verdict = await shared_live_readiness(
        _Store(
            [
                _host(
                    started + timedelta(seconds=1),
                    status="draining",
                    slots_used=0,
                )
            ],
            active_host="runner-a",
        ),
        not_before=started,
        sandbox_manager_token="sandbox-manager-token",
        expected_deployment_sha=DEPLOYMENT_SHA,
        draining_host="runner-a",
        sandbox_manager_probe=lambda *_args, **_kwargs: _async_none(),
    )

    assert verdict.ready is True
    assert "runner-a" in verdict.detail


@pytest.mark.asyncio
async def test_explicit_draining_host_must_not_transition_online() -> None:
    started = datetime.now(UTC)

    verdict = await shared_live_readiness(
        _Store(
            [
                _host(
                    started + timedelta(seconds=1),
                    status="online",
                    slots_used=0,
                )
            ]
        ),
        not_before=started,
        sandbox_manager_token="sandbox-manager-token",
        expected_deployment_sha=DEPLOYMENT_SHA,
        draining_host="runner-a",
    )

    assert verdict.ready is False
    assert "status is online" in verdict.detail


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("changed", "message"),
    [
        ({"status": "online"}, "status is online"),
        ({"slots_used": 1}, "slots_used is 1"),
    ],
)
async def test_explicit_draining_host_is_rechecked_after_the_sandbox_manager_probe(
    changed: dict[str, Any],
    message: str,
) -> None:
    started = datetime.now(UTC)
    draining = _host(
        started + timedelta(seconds=1),
        status="draining",
        slots_used=0,
    )
    changed_row = {**draining, **changed}

    class ChangingStore(_Store):
        def __init__(self) -> None:
            super().__init__([draining])
            self.calls = 0

        async def deployment_runner_pool(self):
            self.calls += 1
            rows = [draining] if self.calls == 1 else [changed_row]
            return rows, None, {}, set()

    verdict = await shared_live_readiness(
        ChangingStore(),
        not_before=started,
        sandbox_manager_token="sandbox-manager-token",
        expected_deployment_sha=DEPLOYMENT_SHA,
        draining_host="runner-a",
        sandbox_manager_probe=lambda *_args, **_kwargs: _async_none(),
    )

    assert verdict.ready is False
    assert "changed while its sandbox manager was probed" in verdict.detail
    assert message in verdict.detail


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
        sandbox_manager_token="sandbox-manager-token",
        expected_deployment_sha=DEPLOYMENT_SHA,
        sandbox_manager_probe=probe,
    )

    assert verdict.ready is False
    assert "since this deployment check began" in verdict.detail
    assert probed is False


@pytest.mark.asyncio
async def test_a_control_plane_that_cannot_use_the_sandbox_manager_stays_closed() -> None:
    started = datetime.now(UTC)

    async def probe(*_args: Any, **_kwargs: Any) -> str:
        return "401 from sandbox manager health"

    verdict = await shared_live_readiness(
        _Store([_host(started + timedelta(seconds=1))]),
        not_before=started,
        sandbox_manager_token="sandbox-manager-token",
        expected_deployment_sha=DEPLOYMENT_SHA,
        sandbox_manager_probe=probe,
    )

    assert verdict.ready is False
    assert "control plane cannot use its sandbox manager" in verdict.detail


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
        sandbox_manager_token="sandbox-manager-token",
        expected_deployment_sha=DEPLOYMENT_SHA,
    )

    assert verdict.ready is False
    assert "pinned host 'pinned-host'" in verdict.detail
    assert "has not advertised shared-sandbox-v1" in verdict.detail


@pytest.mark.asyncio
async def test_an_online_retained_owner_is_probed_after_new_chat_placement_moves() -> None:
    started = datetime.now(UTC)
    calls: list[tuple[str, str, bool]] = []

    async def probe(host: str, url: str, *, force: bool = False) -> None:
        calls.append((host, url, force))

    old_owner = _host(
        started + timedelta(seconds=1),
        host="old-owner",
        sandbox_manager_url="http://old-sandbox-manager:8092",
        # An older compatible generation may serve normal retained follow-ups
        # without this target SHA or historical-fork capability.
        capabilities=[SHARED_SANDBOX_CAPABILITY],
    )
    verdict = await shared_live_readiness(
        _Store(
            [
                _host(started + timedelta(seconds=1), host="new-placement"),
                old_owner,
            ],
            active_host="new-placement",
            retained_hosts={"old-owner"},
        ),
        not_before=started,
        sandbox_manager_token="sandbox-manager-token",
        expected_deployment_sha=DEPLOYMENT_SHA,
        sandbox_manager_probe=probe,
    )

    assert verdict.ready is True
    assert "new-chat placement hosts new-placement completed target deployment" in verdict.detail
    assert "compatible retained owners also answered: old-owner" in verdict.detail
    assert calls == [
        ("new-placement", "http://sandbox-manager-a:8092", True),
        ("old-owner", "http://old-sandbox-manager:8092", True),
    ]


@pytest.mark.asyncio
async def test_an_online_retained_owner_with_an_unreachable_sandbox_manager_blocks_rollout() -> (
    None
):
    started = datetime.now(UTC)

    async def probe(host: str, *_args: Any, **_kwargs: Any) -> str | None:
        return "connection refused" if host == "old-owner" else None

    verdict = await shared_live_readiness(
        _Store(
            [
                _host(started + timedelta(seconds=1), host="new-placement"),
                _host(
                    started + timedelta(seconds=1),
                    host="old-owner",
                    capabilities=[SHARED_SANDBOX_CAPABILITY],
                ),
            ],
            active_host="new-placement",
            retained_hosts={"old-owner"},
        ),
        not_before=started,
        sandbox_manager_token="sandbox-manager-token",
        expected_deployment_sha=DEPLOYMENT_SHA,
        sandbox_manager_probe=probe,
    )

    assert verdict.ready is False
    assert "old-owner" in verdict.detail
    assert "connection refused" in verdict.detail


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
        sandbox_manager_token="sandbox-manager-token",
        expected_deployment_sha=DEPLOYMENT_SHA,
        sandbox_manager_probe=probe,
    )

    assert verdict.ready is False
    assert "old-shared-host" in verdict.detail
    assert f"has not completed target deployment {DEPLOYMENT_SHA}" in verdict.detail
    assert "offline-old-host" not in verdict.detail
    assert "online-legacy-host" not in verdict.detail
    assert probed is False


@pytest.mark.asyncio
async def test_unpinned_v1_host_is_only_a_compatible_retained_owner() -> None:
    """A v1 heartbeat cannot materialize new state but may keep its own layer."""
    started = datetime.now(UTC)
    calls: list[tuple[str, str, bool]] = []

    async def probe(host: str, url: str, *, force: bool = False) -> None:
        calls.append((host, url, force))

    verdict = await shared_live_readiness(
        _Store(
            [
                _host(started + timedelta(seconds=1), host="v2-placement"),
                _host(
                    started + timedelta(seconds=1),
                    host="v1-owner",
                    sandbox_manager_url="http://v1-sandbox-manager:8092",
                    capabilities=[SHARED_SANDBOX_CAPABILITY],
                ),
            ],
            retained_hosts={"v1-owner"},
        ),
        not_before=started,
        sandbox_manager_token="sandbox-manager-token",
        expected_deployment_sha=DEPLOYMENT_SHA,
        sandbox_manager_probe=probe,
    )

    assert verdict.ready is True
    assert calls == [
        ("v2-placement", "http://sandbox-manager-a:8092", True),
        ("v1-owner", "http://v1-sandbox-manager:8092", True),
    ]


@pytest.mark.asyncio
async def test_unpinned_v1_host_without_retained_state_is_not_a_rollout_target() -> None:
    started = datetime.now(UTC)
    calls: list[tuple[str, str, bool]] = []

    async def probe(host: str, url: str, *, force: bool = False) -> None:
        calls.append((host, url, force))

    verdict = await shared_live_readiness(
        _Store(
            [
                _host(started + timedelta(seconds=1), host="v2-placement"),
                _host(
                    started + timedelta(seconds=1),
                    host="v1-idle",
                    capabilities=[SHARED_SANDBOX_CAPABILITY],
                ),
            ]
        ),
        not_before=started,
        sandbox_manager_token="sandbox-manager-token",
        expected_deployment_sha=DEPLOYMENT_SHA,
        sandbox_manager_probe=probe,
    )

    assert verdict.ready is True
    assert calls == [("v2-placement", "http://sandbox-manager-a:8092", True)]


@pytest.mark.asyncio
async def test_every_unpinned_target_sandbox_manager_must_answer() -> None:
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
        sandbox_manager_token="sandbox-manager-token",
        expected_deployment_sha=DEPLOYMENT_SHA,
        sandbox_manager_probe=probe,
    )

    assert verdict.ready is False
    assert "runner-b: control plane cannot use its sandbox manager" in verdict.detail


@pytest.mark.asyncio
async def test_missing_control_plane_sandbox_manager_credential_fails_closed() -> None:
    started = datetime.now(UTC)
    verdict = await shared_live_readiness(
        _Store([_host(started + timedelta(seconds=1))]),
        not_before=started,
        sandbox_manager_token="",
        expected_deployment_sha=DEPLOYMENT_SHA,
    )

    assert verdict.ready is False
    assert "AGENT_SANDBOX_MANAGER_TOKEN" in verdict.detail


@pytest.mark.asyncio
async def test_missing_target_commit_fails_closed() -> None:
    started = datetime.now(UTC)
    verdict = await shared_live_readiness(
        _Store([_host(started + timedelta(seconds=1))]),
        not_before=started,
        sandbox_manager_token="sandbox-manager-token",
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
        ({"sandbox_manager_url": ""}, "no control-plane-reachable sandbox manager URL"),
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
        sandbox_manager_token="sandbox-manager-token",
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
        sandbox_manager_token="sandbox-manager-token",
        expected_deployment_sha=DEPLOYMENT_SHA,
    )

    assert verdict.ready is False
    assert f"has not completed target deployment {DEPLOYMENT_SHA}" in verdict.detail
