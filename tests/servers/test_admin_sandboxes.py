"""Admin authorization, pagination and content boundaries for retained sandboxes."""

from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, call

import pytest
from fastapi import FastAPI, HTTPException, Request as FastAPIRequest
from httpx import ASGITransport, AsyncClient, ConnectError, ReadTimeout, Request
from tests.servers.test_admin_runs import ADMIN, setup as run_setup  # noqa: F401
from tests.unit.test_ledger_snapshots import END, START

from cloud_agent import identity, sandbox_manager_routing
from cloud_agent.admin_observability import encode_cursor
from cloud_agent.admin_sandboxes import (
    SANDBOX_CURSOR_SCOPE,
    SandboxCursor,
    SandboxTrajectoryCursor,
    sandbox_cursor_scope,
    sandbox_trajectory_cursor_scope,
)
from cloud_agent.api import admin_sandboxes
from cloud_agent.api.deps import get_storage
from cloud_agent.app import create_app, create_mcp_app
from cloud_agent.sandbox_manager_client import SandboxManagerClient, SandboxManagerError
from cloud_agent.sandbox_manager_routing import SandboxManagerUrlRefused

PREFIX = "/admin/agent/sandboxes"


def sandbox():
    """Safe runtime metadata plus canaries a response must exclude."""
    return {
        "id": "asbx_other",
        "origin_thread_id": "athr_other",
        "title": "A generated conversation title",
        "repo": "example/repository",
        "user_id": "other-owner",
        "host": "runner-one",
        "container_id": "container-one",
        "container_name": "agent-sandbox-other",
        "runtime_id": "claude-code",
        "runtime_version": "2.1.220",
        "execution_profile": "untrusted-kata-v1",
        "state": "running",
        "memory_limit": "4g",
        "cpu_limit": "2",
        "member_count": 2,
        "active_attempt_count": 0,
        "resident_slot_capacity": 8,
        "active_operation_count": 1,
        "snapshot_state": "pending",
        "terminal_count": 2,
        "terminal_count_observed_at": END,
        "resource_usage": {
            "sample_id": "resource-sample-1",
            "source": "docker-stats",
            "scope": "container",
            "cpu_cores": 1.25,
            "cpu_window_seconds": 2.0,
            "memory_bytes": 512 * 1024**2,
            "memory_limit_bytes": 4 * 1024**3,
            "pids": 12,
            "io_read_bytes": 1000,
            "io_write_bytes": 2000,
            "net_rx_bytes": 3000,
            "net_tx_bytes": 4000,
            "io_read_bytes_per_second": 10.0,
            "io_write_bytes_per_second": 20.0,
            "net_rx_bytes_per_second": 30.0,
            "net_tx_bytes_per_second": 40.0,
            "sampled_at": END,
            "received_at": END,
            "age_seconds": 0.0,
            "stale": False,
        },
        "created_at": START,
        "last_activity_at": END,
        "idle_deadline": None,
        "updated_at": END,
        "host_last_heartbeat_at": None,
        "task_prompt": "PRIVATE prompt",
        "last_error": "PRIVATE container diagnostic",
        "metadata": {"credential": "PRIVATE token"},
        "native_session_id": "PRIVATE session",
    }


@pytest.fixture
def setup(run_setup):  # noqa: F811 — imported pytest fixture
    """Keep the real admin authentication dependency."""
    app, _, _, gateway = run_setup
    queries = SimpleNamespace(
        list_running=AsyncMock(
            return_value={
                "sandboxes": [sandbox()],
                "next_cursor": None,
                "observed_at": END,
                "total_count": 1,
            }
        ),
        trajectory=AsyncMock(
            return_value={
                "sandbox": sandbox()
                | {
                    "state": "lost",
                    "archived_at": END,
                    "deletion_state": "deleted",
                    "deleted_at": END,
                },
                "members": [
                    {
                        "thread_id": "athr_other",
                        "user_id": "other-owner",
                        "resident_slot_id": None,
                        "resident_state": "lost",
                        "context_status": "lost",
                        "native_state": "lost",
                        "created_at": START,
                        "updated_at": END,
                        "native_session_id": "PRIVATE session",
                    }
                ],
                "member_history_count": 1,
                "member_history_truncated": False,
                "events": [
                    {
                        "id": 2,
                        "generation": 1,
                        "event_type": "deletion_state_changed",
                        "from_state": "archived",
                        "to_state": "deleted",
                        "source": "system",
                        "details": {},
                        "created_at": END,
                    }
                ],
                "observed_at": END,
                "next_cursor": None,
            }
        ),
    )
    app.include_router(admin_sandboxes.router)
    app.dependency_overrides[admin_sandboxes.get_admin_sandbox_queries] = lambda: queries
    return app, queries, gateway


def sandbox_detail(**overrides):
    """A running, otherwise idle container with an unavailable terminal census."""
    return {
        "sandbox": sandbox()
        | {
            "active_operation_count": 0,
            "snapshot_state": "none",
            "terminal_count": None,
            "terminal_count_observed_at": None,
            "resource_usage": None,
        }
        | overrides,
        "members": [],
        "observed_at": END,
    }


@pytest.fixture
def terminal_probe(setup, monkeypatch):
    """Expose the owning host sandbox manager and the fenced observation command."""
    app, queries, _ = setup
    queries.detail = AsyncMock(return_value=sandbox_detail())
    commands = SimpleNamespace(
        record_terminal_observation=AsyncMock(return_value=True),
        request_admin_stop=AsyncMock(return_value="accepted"),
        request_admin_delete=AsyncMock(return_value="accepted"),
    )
    host_pool = SimpleNamespace(get_host=AsyncMock(return_value={"host": "runner-one"}))
    sandbox_manager = SimpleNamespace(
        list_sandboxes=AsyncMock(
            return_value=[
                {
                    "sandbox_id": "asbx_other",
                    "origin_thread_id": "athr_other",
                    "container_id": "container-one",
                    "generation": 1,
                    "running": True,
                    "shared_live": True,
                    "docker_status": "running",
                    "terminal_count": 0,
                    "group_busy": False,
                    "operation_count": 0,
                    "snapshotting": False,
                }
            ]
        ),
        shared_chat_sandbox_status=AsyncMock(
            return_value={
                "thread_id": "athr_other",
                "origin_thread_id": "athr_other",
                "sandbox_id": "asbx_other",
                "container_id": "container-one",
                "generation": 1,
                "running": True,
                "docker_status": "running",
                "busy": False,
                "group_busy": False,
                "terminal_count": 0,
                "operation_count": 0,
                "snapshotting": False,
            }
        ),
    )
    sandbox_manager_factory = Mock(return_value=sandbox_manager)
    monkeypatch.setattr(admin_sandboxes, "sandbox_manager_for_host", sandbox_manager_factory)
    app.dependency_overrides[get_storage] = lambda: SimpleNamespace(
        sandbox=commands, admin_sandboxes=queries, host_pool=host_pool
    )
    return app, queries, commands, host_pool, sandbox_manager, sandbox_manager_factory


@pytest.mark.parametrize("auth", ["missing", "invalid", "revoked"])
async def test_listing_requires_current_admin_access(setup, auth):
    app, queries, gateway = setup
    gateway.permissions = ["agent.use"]
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        headers = {"Authorization": "Bearer wrong"} if auth == "invalid" else {}
        if auth == "revoked":
            client.cookies.set(identity.SESSION_COOKIE, identity.mint_session(user_id="admin"))
        response = await client.get(PREFIX, headers=headers)
    assert response.status_code == (403 if auth == "revoked" else 401)
    queries.list_running.assert_not_awaited()


@pytest.mark.parametrize("browser", [False, True])
async def test_listing_is_cross_owner_content_free_and_not_cached(setup, browser):
    app, queries, _ = setup
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        if browser:
            client.cookies.set(identity.SESSION_COOKIE, identity.mint_session(user_id="admin"))
        response = await client.get(PREFIX, headers={} if browser else ADMIN)
    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    body = response.json()
    assert body["total_count"] == 1
    assert body["next_cursor"] is None
    assert body["observed_at"] == END.isoformat().replace("+00:00", "Z")
    assert body["sandboxes"][0]["user_id"] == "other-owner"
    assert body["sandboxes"][0]["active_attempt_count"] == 0
    row = body["sandboxes"][0]
    assert row["resident_slot_capacity"] == 8
    assert row["active_operation_count"] == 1
    assert row["snapshot_state"] == "pending"
    assert row["terminal_count"] == 2
    assert row["terminal_count_observed_at"] == END.isoformat().replace("+00:00", "Z")
    assert row["resource_usage"]["sample_id"] == "resource-sample-1"
    assert row["resource_usage"]["source"] == "docker-stats"
    assert row["resource_usage"]["scope"] == "container"
    assert row["resource_usage"]["cpu_cores"] == 1.25
    assert row["resource_usage"]["memory_bytes"] == 512 * 1024**2
    assert row["resource_usage"]["sampled_at"] == END.isoformat().replace("+00:00", "Z")
    assert row["resource_usage"]["stale"] is False
    assert "private" not in response.text.lower()
    for field in ("task_prompt", "last_error", "metadata", "native_session_id"):
        assert field not in response.text
    queries.list_running.assert_awaited_once_with(cursor=None, limit=100)


async def test_valid_cursor_and_page_limit_are_forwarded(setup):
    app, queries, _ = setup
    parsed = SandboxCursor(scope=SANDBOX_CURSOR_SCOPE, after_id="asbx_previous")
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get(
            PREFIX, headers=ADMIN, params={"cursor": encode_cursor(parsed), "limit": 200}
        )
    assert response.status_code == 200
    queries.list_running.assert_awaited_once_with(cursor=parsed, limit=200)


async def test_trajectory_is_scoped_paginated_content_free_and_retains_tombstone(setup):
    app, queries, _ = setup
    parsed = SandboxTrajectoryCursor(
        scope=sandbox_trajectory_cursor_scope("asbx_other"),
        snapshot_id=12,
        before_id=9,
    )
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get(
            f"{PREFIX}/asbx_other/trajectory",
            headers=ADMIN,
            params={"cursor": encode_cursor(parsed), "limit": 25},
        )
    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    body = response.json()
    assert body["sandbox"]["id"] == "asbx_other"
    assert body["sandbox"]["archived_at"] is not None
    assert body["sandbox"]["deletion_state"] == "deleted"
    assert body["sandbox"]["deleted_at"] is not None
    assert body["members"][0]["resident_slot_id"] is None
    assert body["member_history_count"] == 1
    assert body["member_history_truncated"] is False
    assert body["events"][0]["event_type"] == "deletion_state_changed"
    assert "PRIVATE" not in response.text
    queries.trajectory.assert_awaited_once_with("asbx_other", cursor=parsed, limit=25)

    queries.trajectory.return_value = None
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        missing = await client.get(f"{PREFIX}/missing/trajectory", headers=ADMIN)
    assert missing.status_code == 404


async def test_trajectory_rejects_a_cursor_for_another_sandbox(setup):
    app, queries, _ = setup
    parsed = SandboxTrajectoryCursor(
        scope=sandbox_trajectory_cursor_scope("different"), snapshot_id=2, before_id=1
    )
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get(
            f"{PREFIX}/asbx_other/trajectory",
            headers=ADMIN,
            params={"cursor": encode_cursor(parsed)},
        )
    assert response.status_code == 422
    queries.trajectory.assert_not_awaited()


async def test_all_lifecycle_filter_and_cursor_are_scoped(setup):
    app, queries, _ = setup
    queries.list_sandboxes = AsyncMock(return_value=queries.list_running.return_value)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get(PREFIX, headers=ADMIN, params={"state": "all"})
        mismatch = await client.get(
            PREFIX,
            headers=ADMIN,
            params={
                "state": "all",
                "cursor": encode_cursor(SandboxCursor(scope=SANDBOX_CURSOR_SCOPE, after_id="one")),
            },
        )
        invalid = await client.get(PREFIX, headers=ADMIN, params={"state": "deleted"})
    assert response.status_code == 200
    assert mismatch.status_code == invalid.status_code == 422
    queries.list_sandboxes.assert_awaited_once_with(cursor=None, limit=100, state="all")


async def test_removed_inventory_flag_is_forwarded_and_cursor_scoped(setup):
    app, queries, _ = setup
    queries.list_sandboxes = AsyncMock(return_value=queries.list_running.return_value)
    parsed = SandboxCursor(scope=sandbox_cursor_scope("all", True), after_id="one")
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get(
            PREFIX,
            headers=ADMIN,
            params={"state": "all", "include_archived": "true", "cursor": encode_cursor(parsed)},
        )
        mismatch = await client.get(
            PREFIX, headers=ADMIN, params={"state": "all", "cursor": encode_cursor(parsed)}
        )
    assert response.status_code == 200
    assert mismatch.status_code == 422
    queries.list_sandboxes.assert_awaited_once_with(
        cursor=parsed, limit=100, state="all", include_archived=True
    )


async def test_detail_sanitizes_members_and_preserves_stopped_state(setup):
    app, queries, _ = setup
    row = sandbox() | {"state": "stopped", "generation": 3}
    queries.detail = AsyncMock(
        return_value={
            "sandbox": row,
            "members": [
                {
                    "thread_id": "athr_other",
                    "user_id": "other-owner",
                    "resident_slot_id": 0,
                    "resident_state": "stopped",
                    "context_status": "restored",
                    "native_state": "ready",
                    "created_at": START,
                    "updated_at": END,
                    "native_session_id": "PRIVATE session",
                    "last_error": "PRIVATE diagnostic",
                }
            ],
            "observed_at": END,
        }
    )
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get(f"{PREFIX}/asbx_other", headers=ADMIN)
    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    assert response.json()["sandbox"]["state"] == "stopped"
    assert response.json()["sandbox"]["generation"] == 3
    assert "PRIVATE" not in response.text
    queries.detail.return_value = None
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        missing = await client.get(f"{PREFIX}/missing", headers=ADMIN)
    assert missing.status_code == 404


@pytest.mark.parametrize(
    "overrides",
    [
        {"terminal_count": None},
        {"terminal_count": None, "terminal_count_observed_at": END},
        {"terminal_count": 0, "terminal_count_observed_at": None},
        {"terminal_count": 0, "terminal_count_observed_at": END - timedelta(seconds=91)},
        {"terminal_count": 2, "terminal_count_observed_at": END - timedelta(seconds=91)},
        {"terminal_count": 0, "terminal_count_observed_at": END},
        {"terminal_count": 0, "terminal_count_observed_at": END - timedelta(seconds=90)},
    ],
)
@pytest.mark.parametrize("count", [0, 2])
async def test_detail_refreshes_unavailable_or_zero_terminal_activity_before_rereading(
    terminal_probe, overrides, count
):
    app, queries, commands, host_pool, sandbox_manager, sandbox_manager_factory = terminal_probe
    fresh = sandbox_detail(terminal_count=count, terminal_count_observed_at=END)
    queries.detail.side_effect = [sandbox_detail(**overrides), fresh]
    sandbox_manager.list_sandboxes.return_value[0]["terminal_count"] = count
    sequence = Mock()
    sequence.attach_mock(queries.detail, "detail")
    sequence.attach_mock(sandbox_manager.list_sandboxes, "list_sandboxes")
    sequence.attach_mock(commands.record_terminal_observation, "record_terminal_observation")

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get(f"{PREFIX}/asbx_other", headers=ADMIN)

    assert response.status_code == 200
    body = response.json()
    assert body["sandbox"]["terminal_count"] == count
    assert body["sandbox"]["terminal_count_observed_at"] == END.isoformat().replace("+00:00", "Z")
    assert body["terminal_activity_error"] is None
    assert body["sandbox"]["resource_usage"] is None
    host_pool.get_host.assert_awaited_once_with(host="runner-one")
    sandbox_manager_factory.assert_called_once_with(host_pool.get_host.return_value)
    assert sequence.mock_calls == [
        call.detail("asbx_other"),
        call.list_sandboxes(thread_id="athr_other", timeout=5),
        call.record_terminal_observation(
            sandbox_id="asbx_other",
            host="runner-one",
            generation=1,
            container_id="container-one",
            terminal_count=count,
        ),
        call.detail("asbx_other"),
    ]
    commands.request_admin_stop.assert_not_awaited()


async def test_detail_refreshes_own_group_when_full_host_inventory_fails(terminal_probe):
    app, queries, commands, _, sandbox_manager, sandbox_manager_factory = terminal_probe
    sandbox_manager_app = FastAPI()
    selections: list[str | None] = []
    row = sandbox_manager.list_sandboxes.return_value[0]

    @sandbox_manager_app.get("/sandboxes")
    async def inventory(request: FastAPIRequest):
        assert request.headers["X-Agent-Workspace-Token"] == "secret"
        thread_id = request.query_params.get("thread_id")
        selections.append(thread_id)
        if thread_id != "athr_other":
            raise HTTPException(503, "An unrelated retained container cannot be inspected.")
        return {"sandboxes": [row]}

    queries.detail.side_effect = [
        sandbox_detail(),
        sandbox_detail(terminal_count=0, terminal_count_observed_at=END),
    ]
    async with AsyncClient(
        transport=ASGITransport(app=sandbox_manager_app), base_url="http://sandbox_manager"
    ) as sandbox_manager_http:
        real_sandbox_manager = SandboxManagerClient(
            "http://sandbox_manager", "secret", client=sandbox_manager_http
        )
        sandbox_manager_factory.return_value = real_sandbox_manager
        with pytest.raises(SandboxManagerError):
            await real_sandbox_manager.list_sandboxes()
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            response = await client.get(f"{PREFIX}/asbx_other", headers=ADMIN)

    assert response.status_code == 200
    assert response.json()["terminal_activity_error"] is None
    assert response.json()["sandbox"]["terminal_count"] == 0
    assert selections == [None, "athr_other"]
    commands.record_terminal_observation.assert_awaited_once_with(
        sandbox_id="asbx_other",
        host="runner-one",
        generation=1,
        container_id="container-one",
        terminal_count=0,
    )


@pytest.mark.parametrize(
    "overrides",
    [
        {"terminal_count": 2, "terminal_count_observed_at": END},
        {"terminal_count": 2, "terminal_count_observed_at": END - timedelta(seconds=90)},
        {"state": "stopped"},
    ],
)
async def test_detail_does_not_probe_current_positive_census_or_stopped_container(
    terminal_probe, overrides
):
    app, queries, commands, host_pool, sandbox_manager, _ = terminal_probe
    queries.detail.return_value = sandbox_detail(**overrides)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get(f"{PREFIX}/asbx_other", headers=ADMIN)
    assert response.status_code == 200
    assert response.json()["terminal_activity_error"] is None
    queries.detail.assert_awaited_once_with("asbx_other")
    host_pool.get_host.assert_not_awaited()
    sandbox_manager.list_sandboxes.assert_not_awaited()
    commands.record_terminal_observation.assert_not_awaited()


@pytest.mark.parametrize(
    "failure", ["missing_host", "missing_sandbox_manager", "refused_url", "sandbox_manager_error"]
)
async def test_detail_explains_unavailable_sandbox_manager_without_recording_an_idle_census(
    terminal_probe, failure
):
    app, queries, commands, host_pool, sandbox_manager, sandbox_manager_factory = terminal_probe
    if failure == "missing_host":
        host_pool.get_host.return_value = None
    elif failure == "missing_sandbox_manager":
        sandbox_manager_factory.return_value = None
    elif failure == "refused_url":
        sandbox_manager_factory.side_effect = SandboxManagerUrlRefused("not allowed")
    else:
        sandbox_manager.list_sandboxes.side_effect = SandboxManagerError(503, "PRIVATE diagnostic")
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get(f"{PREFIX}/asbx_other", headers=ADMIN)
    assert response.status_code == 200
    body = response.json()
    assert body["sandbox"]["terminal_count"] is None
    assert "sandbox manager" in body["terminal_activity_error"]
    assert "connection" in body["terminal_activity_error"]
    assert "PRIVATE" not in response.text
    queries.detail.assert_awaited_once_with("asbx_other")
    commands.record_terminal_observation.assert_not_awaited()
    commands.request_admin_stop.assert_not_awaited()
    if failure == "sandbox_manager_error":
        sandbox_manager.list_sandboxes.assert_awaited_once_with(thread_id="athr_other", timeout=5)
        assert "refresh details" in body["terminal_activity_error"]
    else:
        sandbox_manager.list_sandboxes.assert_not_awaited()


@pytest.mark.parametrize(
    "overrides",
    [
        {"sandbox_id": "asbx_replaced"},
        {"origin_thread_id": "athr_replaced"},
        {"container_id": "container-replaced"},
        {"generation": 2},
        {"running": False},
        {"shared_live": False},
    ],
)
async def test_detail_rejects_sandbox_manager_census_for_a_different_or_inactive_container(
    terminal_probe, overrides
):
    app, queries, commands, _, sandbox_manager, _ = terminal_probe
    sandbox_manager.list_sandboxes.return_value[0].update(overrides)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get(f"{PREFIX}/asbx_other", headers=ADMIN)
    assert response.status_code == 200
    body = response.json()
    assert body["sandbox"]["terminal_count"] is None
    assert "container changed" in body["terminal_activity_error"]
    queries.detail.assert_awaited_once_with("asbx_other")
    commands.record_terminal_observation.assert_not_awaited()


@pytest.mark.parametrize("count", [None, True, False, "0", 0.0, -1, 4097])
async def test_detail_rejects_missing_or_invalid_sandbox_manager_terminal_counts(
    terminal_probe, count
):
    app, queries, commands, _, sandbox_manager, _ = terminal_probe
    sandbox_manager.list_sandboxes.return_value[0]["terminal_count"] = count
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get(f"{PREFIX}/asbx_other", headers=ADMIN)
    assert response.status_code == 200
    body = response.json()
    assert body["sandbox"]["terminal_count"] is None
    assert "Update its sandbox manager" in body["terminal_activity_error"]
    queries.detail.assert_awaited_once_with("asbx_other")
    commands.record_terminal_observation.assert_not_awaited()


@pytest.mark.parametrize("terminal_count", [None, 0])
@pytest.mark.parametrize("group_busy", [True, None, "missing"])
async def test_detail_does_not_treat_legacy_origin_terminal_count_as_group_idle(
    terminal_probe, group_busy, terminal_count
):
    app, queries, commands, _, sandbox_manager, _ = terminal_probe
    queries.detail.return_value = sandbox_detail(
        terminal_count=terminal_count, terminal_count_observed_at=END
    )
    row = sandbox_manager.list_sandboxes.return_value[0]
    row["thread_id"] = row.pop("origin_thread_id")
    if group_busy == "missing":
        row.pop("group_busy")
    else:
        row["group_busy"] = group_busy
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get(f"{PREFIX}/asbx_other", headers=ADMIN)
    assert response.status_code == 200
    body = response.json()
    assert body["sandbox"]["terminal_count"] == terminal_count
    assert "Wait until it is idle" in body["terminal_activity_error"]
    queries.detail.assert_awaited_once_with("asbx_other")
    commands.record_terminal_observation.assert_not_awaited()


async def test_detail_keeps_activity_unavailable_if_container_changes_before_recording(
    terminal_probe,
):
    app, queries, commands, _, _, _ = terminal_probe
    commands.record_terminal_observation.return_value = False
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get(f"{PREFIX}/asbx_other", headers=ADMIN)
    assert response.status_code == 200
    body = response.json()
    assert body["sandbox"]["terminal_count"] is None
    assert "container changed" in body["terminal_activity_error"]
    commands.record_terminal_observation.assert_awaited_once()
    queries.detail.assert_awaited_once_with("asbx_other")


@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("GET", "/asbx_other"),
        ("GET", "/asbx_other/trajectory"),
        ("POST", "/asbx_other/stop"),
        ("POST", "/asbx_other/archive"),
        ("POST", "/asbx_other/restore"),
        ("POST", "/asbx_other/delete"),
    ],
)
async def test_detail_and_control_require_current_admin_access(setup, method, path):
    app, _, gateway = setup
    gateway.permissions = ["agent.use"]
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        client.cookies.set(identity.SESSION_COOKIE, identity.mint_session(user_id="admin"))
        response = await client.request(
            method,
            PREFIX + path,
            json={
                "generation": 1,
                "container_id": "container-one",
                "last_activity_at": END.isoformat(),
            },
        )
    assert response.status_code == 403


@pytest.mark.parametrize(
    ("result", "status"),
    [
        ("accepted", 202),
        ("not_found", 404),
        ("stale", 409),
        ("busy", 409),
        ("not_running", 409),
        ("terminals_open", 409),
        ("activity_unknown", 409),
    ],
)
async def test_reviewed_stop_passes_authenticated_actor_to_atomic_command(setup, result, status):
    from cloud_agent.admin_sandboxes import AdminSandboxDetail

    app, queries, _ = setup
    queries.detail = AsyncMock(
        return_value=AdminSandboxDetail(
            sandbox=sandbox() | {"state": "stopping", "stop_requested_at": END},
            members=[],
            observed_at=END,
        )
    )
    if result == "activity_unknown":
        # A missing current detail cannot supply a safe sandbox manager probe target.
        queries.detail.return_value = None
    commands = SimpleNamespace(request_admin_stop=AsyncMock(return_value=result))
    app.dependency_overrides[get_storage] = lambda: SimpleNamespace(
        sandbox=commands, admin_sandboxes=queries
    )
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post(
            f"{PREFIX}/asbx_other/stop",
            headers=ADMIN,
            json={
                "generation": 1,
                "container_id": "container-one",
                "last_activity_at": END.isoformat(),
            },
        )
    assert response.status_code == status
    commands.request_admin_stop.assert_awaited_once_with(
        sandbox_id="asbx_other",
        generation=1,
        container_id="container-one",
        last_activity_at=END,
        actor_id="admin-token",
        admin_ip="127.0.0.1",
    )
    if result == "accepted":
        assert response.json()["sandbox"]["state"] == "stopping"
        assert queries.detail.await_args_list == [call("asbx_other"), call("asbx_other")]
    elif result == "activity_unknown":
        assert "terminal activity is unavailable" in response.json()["detail"]
        assert queries.detail.await_args_list == [call("asbx_other"), call("asbx_other")]
    else:
        queries.detail.assert_awaited_once_with("asbx_other")


@pytest.mark.parametrize(
    ("retry_result", "status", "message"),
    [
        ("accepted", 202, None),
        ("stale", 409, "Sandbox activity or container changed"),
        ("busy", 409, "active turn, operation, or snapshot"),
        ("terminals_open", 409, "open terminals"),
        ("activity_unknown", 409, "terminal activity is unavailable"),
    ],
)
@pytest.mark.parametrize("review_current", [False, True])
async def test_stop_probes_unknown_activity_then_retries_all_original_review_fences(
    terminal_probe, retry_result, status, message, review_current
):
    app, queries, commands, _, sandbox_manager, _ = terminal_probe
    # Activity can move while the sandbox manager probe is in flight. The retry must
    # still pass the administrator's older reviewed timestamp to the command.
    current = sandbox_detail(last_activity_at=END if review_current else END + timedelta(seconds=1))
    stopped = sandbox_detail(state="stopping", stop_requested_at=END)
    queries.detail.side_effect = [current, current, stopped]
    commands.request_admin_stop.side_effect = ["activity_unknown", retry_result]
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post(
            f"{PREFIX}/asbx_other/stop",
            headers=ADMIN,
            json={
                "generation": 1,
                "container_id": "container-one",
                "last_activity_at": END.isoformat(),
            },
        )
    assert response.status_code == status
    reviewed_fences = call(
        sandbox_id="asbx_other",
        generation=1,
        container_id="container-one",
        last_activity_at=END,
        actor_id="admin-token",
        admin_ip="127.0.0.1",
    )
    assert commands.request_admin_stop.await_args_list == [reviewed_fences, reviewed_fences]
    probe_count = 2 if review_current else 1
    assert (
        sandbox_manager.list_sandboxes.await_args_list
        == [call(thread_id="athr_other", timeout=5)] * probe_count
    )
    observation = call(
        sandbox_id="asbx_other",
        host="runner-one",
        generation=1,
        container_id="container-one",
        terminal_count=0,
    )
    assert commands.record_terminal_observation.await_args_list == [observation] * probe_count
    if retry_result == "accepted":
        assert response.json()["sandbox"]["state"] == "stopping"
        assert queries.detail.await_count == 3
    else:
        assert message in response.json()["detail"]
        assert queries.detail.await_args_list == [call("asbx_other"), call("asbx_other")]


@pytest.mark.parametrize("terminal_count", [None, 0])
@pytest.mark.parametrize(
    "failure", ["sandbox_manager_error", "container_changed", "record_refused"]
)
async def test_stop_does_not_issue_another_command_if_terminal_probe_cannot_record_safe_activity(
    terminal_probe, failure, terminal_count
):
    app, queries, commands, _, sandbox_manager, _ = terminal_probe
    queries.detail.return_value = sandbox_detail(
        terminal_count=terminal_count,
        terminal_count_observed_at=END,
        # An older reviewed timestamp skips the preflight. The old unknown
        # result can still trigger its safe sandbox manager recovery and fenced retry.
        last_activity_at=END + timedelta(seconds=1) if terminal_count is None else END,
    )
    commands.request_admin_stop.return_value = "activity_unknown"
    if failure == "sandbox_manager_error":
        sandbox_manager.list_sandboxes.side_effect = SandboxManagerError(503, "unreachable")
    elif failure == "container_changed":
        sandbox_manager.list_sandboxes.return_value[0]["generation"] = 2
    else:
        commands.record_terminal_observation.return_value = False
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post(
            f"{PREFIX}/asbx_other/stop",
            headers=ADMIN,
            json={
                "generation": 1,
                "container_id": "container-one",
                "last_activity_at": END.isoformat(),
            },
        )
    assert response.status_code == 409
    expected = "Restore its connection" if failure == "sandbox_manager_error" else "Refresh details"
    assert expected in response.json()["detail"]
    if terminal_count is None:
        commands.request_admin_stop.assert_awaited_once()
        assert queries.detail.await_args_list == [call("asbx_other"), call("asbx_other")]
    else:
        commands.request_admin_stop.assert_not_awaited()
        queries.detail.assert_awaited_once_with("asbx_other")
    if failure == "record_refused":
        commands.record_terminal_observation.assert_awaited_once()
    else:
        commands.record_terminal_observation.assert_not_awaited()


@pytest.mark.parametrize("group_busy", [True, "missing"])
@pytest.mark.parametrize("terminal_count", [None, 0, 2])
async def test_stop_rejects_busy_legacy_census_before_atomic_stop(
    terminal_probe, group_busy, terminal_count
):
    app, queries, commands, _, sandbox_manager, _ = terminal_probe
    queries.detail.return_value = sandbox_detail(
        terminal_count=terminal_count, terminal_count_observed_at=END
    )
    row = sandbox_manager.list_sandboxes.return_value[0]
    row["thread_id"] = row.pop("origin_thread_id")
    if group_busy == "missing":
        row.pop("group_busy")
    else:
        row["group_busy"] = group_busy
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post(
            f"{PREFIX}/asbx_other/stop",
            headers=ADMIN,
            json={
                "generation": 1,
                "container_id": "container-one",
                "last_activity_at": END.isoformat(),
            },
        )
    assert response.status_code == 409
    assert "Wait until it is idle" in response.json()["detail"]
    sandbox_manager.list_sandboxes.assert_awaited_once_with(thread_id="athr_other", timeout=5)
    commands.record_terminal_observation.assert_not_awaited()
    commands.request_admin_stop.assert_not_awaited()
    queries.detail.assert_awaited_once_with("asbx_other")


@pytest.mark.parametrize("terminal_count", [None, 0, 2])
async def test_stop_verifies_current_group_census_before_atomic_stop(
    terminal_probe, terminal_count
):
    app, queries, commands, _, sandbox_manager, _ = terminal_probe
    queries.detail.side_effect = [
        sandbox_detail(terminal_count=terminal_count, terminal_count_observed_at=END),
        sandbox_detail(state="stopping", stop_requested_at=END),
    ]
    sequence = Mock()
    sequence.attach_mock(queries.detail, "detail")
    sequence.attach_mock(sandbox_manager.list_sandboxes, "list_sandboxes")
    sequence.attach_mock(commands.record_terminal_observation, "record_terminal_observation")
    sequence.attach_mock(commands.request_admin_stop, "request_admin_stop")
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post(
            f"{PREFIX}/asbx_other/stop",
            headers=ADMIN,
            json={
                "generation": 1,
                "container_id": "container-one",
                "last_activity_at": END.isoformat(),
            },
        )
    assert response.status_code == 202
    assert response.json()["sandbox"]["state"] == "stopping"
    assert sequence.mock_calls == [
        call.detail("asbx_other"),
        call.list_sandboxes(thread_id="athr_other", timeout=5),
        call.record_terminal_observation(
            sandbox_id="asbx_other",
            host="runner-one",
            generation=1,
            container_id="container-one",
            terminal_count=0,
        ),
        call.request_admin_stop(
            sandbox_id="asbx_other",
            generation=1,
            container_id="container-one",
            last_activity_at=END,
            actor_id="admin-token",
            admin_ip="127.0.0.1",
        ),
        call.detail("asbx_other"),
    ]


@pytest.mark.parametrize(
    "overrides",
    [
        {"generation": 2},
        {"container_id": "container-replaced"},
        {"last_activity_at": END + timedelta(seconds=1)},
    ],
)
async def test_stop_keeps_atomic_stale_refusal_when_review_fences_do_not_match(
    terminal_probe, overrides
):
    app, queries, commands, host_pool, sandbox_manager, _ = terminal_probe
    queries.detail.return_value = sandbox_detail(
        terminal_count=0, terminal_count_observed_at=END, **overrides
    )
    commands.request_admin_stop.return_value = "stale"
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post(
            f"{PREFIX}/asbx_other/stop",
            headers=ADMIN,
            json={
                "generation": 1,
                "container_id": "container-one",
                "last_activity_at": END.isoformat(),
            },
        )
    assert response.status_code == 409
    assert "Sandbox activity or container changed" in response.json()["detail"]
    commands.request_admin_stop.assert_awaited_once_with(
        sandbox_id="asbx_other",
        generation=1,
        container_id="container-one",
        last_activity_at=END,
        actor_id="admin-token",
        admin_ip="127.0.0.1",
    )
    host_pool.get_host.assert_not_awaited()
    sandbox_manager.list_sandboxes.assert_not_awaited()
    commands.record_terminal_observation.assert_not_awaited()
    queries.detail.assert_awaited_once_with("asbx_other")


@pytest.mark.parametrize("archived", [False, True])
@pytest.mark.parametrize(
    ("result", "status"),
    [("accepted", 200), ("not_found", 404), ("stale", 409), ("busy", 409), ("not_stopped", 409)],
)
async def test_inventory_controls_pass_review_and_authenticated_actor(
    setup, archived, result, status
):
    from cloud_agent.admin_sandboxes import AdminSandboxDetail

    app, queries, _ = setup
    queries.detail = AsyncMock(
        return_value=AdminSandboxDetail(
            sandbox=sandbox()
            | {"state": "stopped", "container_id": None, "archived_at": END if archived else None},
            members=[],
            observed_at=END,
        )
    )
    commands = SimpleNamespace(set_admin_archived=AsyncMock(return_value=result))
    app.dependency_overrides[get_storage] = lambda: SimpleNamespace(
        sandbox=commands, admin_sandboxes=queries
    )
    action = "archive" if archived else "restore"
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post(
            f"{PREFIX}/asbx_other/{action}",
            headers=ADMIN,
            json={"generation": 1, "container_id": None, "last_activity_at": END.isoformat()},
        )
    assert response.status_code == status
    commands.set_admin_archived.assert_awaited_once_with(
        sandbox_id="asbx_other",
        generation=1,
        container_id=None,
        last_activity_at=END,
        archived=archived,
        actor_id="admin-token",
        admin_ip="127.0.0.1",
        allow_stale_host=False,
    )
    if result == "accepted":
        assert response.headers["cache-control"] == "no-store"
        assert (response.json()["sandbox"]["archived_at"] is not None) == archived
        assert "PRIVATE" not in response.text
    else:
        queries.detail.assert_not_awaited()


@pytest.mark.parametrize(
    ("result", "status"), [("accepted", 200), ("host_online", 409), ("busy", 409), ("stale", 409)]
)
async def test_stale_inventory_removal_passes_review_without_contacting_host(setup, result, status):
    app, queries, _ = setup
    queries.detail = AsyncMock(return_value=sandbox_detail(archived_at=END))
    commands = SimpleNamespace(set_admin_archived=AsyncMock(return_value=result))
    host_pool = SimpleNamespace(get_host=AsyncMock(side_effect=AssertionError("host probe")))
    app.dependency_overrides[get_storage] = lambda: SimpleNamespace(
        sandbox=commands, admin_sandboxes=queries, host_pool=host_pool
    )
    app.dependency_overrides[admin_sandboxes.admin_sandbox_deletion_available] = lambda: False
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post(
            f"{PREFIX}/asbx_other/archive?stale=true",
            headers=ADMIN,
            json={
                "generation": 1,
                "container_id": "container-one",
                "last_activity_at": END.isoformat(),
            },
        )
    assert response.status_code == status
    commands.set_admin_archived.assert_awaited_once_with(
        sandbox_id="asbx_other",
        generation=1,
        container_id="container-one",
        last_activity_at=END,
        archived=True,
        actor_id="admin-token",
        admin_ip="127.0.0.1",
        allow_stale_host=True,
    )
    host_pool.get_host.assert_not_awaited()
    if result == "accepted":
        assert response.headers["cache-control"] == "no-store"
        assert response.json()["sandbox"]["state"] == "running"
        assert response.json()["sandbox"]["archived_at"] is not None
    else:
        queries.detail.assert_not_awaited()


@pytest.mark.parametrize("action", ["archive", "restore"])
@pytest.mark.parametrize(
    "changes",
    [
        {"generation": 0},
        {"container_id": ""},
        {"last_activity_at": "invalid"},
        {"force": True},
    ],
)
async def test_inventory_controls_reject_unreviewed_payloads(setup, action, changes):
    app, queries, _ = setup
    commands = SimpleNamespace(set_admin_archived=AsyncMock())
    app.dependency_overrides[get_storage] = lambda: SimpleNamespace(
        sandbox=commands, admin_sandboxes=queries
    )
    payload = {"generation": 1, "container_id": None, "last_activity_at": END.isoformat()}
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post(
            f"{PREFIX}/asbx_other/{action}", headers=ADMIN, json=payload | changes
        )
    assert response.status_code == 422
    commands.set_admin_archived.assert_not_awaited()


@pytest.mark.parametrize(
    ("result", "status"),
    [("accepted", 202), ("not_found", 404), ("stale", 409), ("busy", 409), ("not_stopped", 409)],
)
async def test_reviewed_delete_queues_authenticated_atomic_request(setup, result, status):
    from cloud_agent.admin_sandboxes import AdminSandboxDetail

    app, queries, _ = setup
    queries.detail = AsyncMock(
        return_value=AdminSandboxDetail(
            sandbox=sandbox() | {"state": "stopped", "deletion_state": "pending"},
            members=[],
            observed_at=END,
        )
    )
    commands = SimpleNamespace(request_admin_delete=AsyncMock(return_value=result))
    app.dependency_overrides[get_storage] = lambda: SimpleNamespace(
        sandbox=commands, admin_sandboxes=queries
    )
    app.dependency_overrides[admin_sandboxes.admin_sandbox_deletion_available] = lambda: True
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post(
            f"{PREFIX}/asbx_other/delete",
            headers=ADMIN,
            json={
                "generation": 1,
                "container_id": "container-one",
                "last_activity_at": END.isoformat(),
            },
        )
    assert response.status_code == status
    commands.request_admin_delete.assert_awaited_once_with(
        sandbox_id="asbx_other",
        generation=1,
        container_id="container-one",
        last_activity_at=END,
        actor_id="admin-token",
        admin_ip="127.0.0.1",
        sandbox_manager_confirmed_stopped=False,
        confirmed_docker_status=None,
        discard=False,
    )
    if result == "accepted":
        assert response.headers["cache-control"] == "no-store"
        assert response.json()["sandbox"]["deletion_state"] == "pending"
    else:
        queries.detail.assert_not_awaited()


@pytest.mark.parametrize("docker_status", ["stopped", "dead"])
@pytest.mark.parametrize("discard", [False, True])
async def test_force_delete_repairs_only_an_exact_host_confirmed_stopped_container(
    terminal_probe, docker_status, discard
):
    app, queries, commands, host_pool, sandbox_manager, sandbox_manager_factory = terminal_probe
    sandbox_manager.shared_chat_sandbox_status.return_value.update(
        {
            "running": False,
            "docker_status": docker_status,
            "group_busy": False,
            "terminal_count": 0,
            "operation_count": 0,
            "snapshotting": False,
        }
    )
    queries.detail.side_effect = [
        sandbox_detail(),
        sandbox_detail(state="stopped", deletion_state="pending"),
    ]
    app.dependency_overrides[admin_sandboxes.admin_sandbox_deletion_available] = lambda: not discard
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post(
            f"{PREFIX}/asbx_other/delete?force=true&discard={str(discard).lower()}",
            headers=ADMIN,
            json={
                "generation": 1,
                "container_id": "container-one",
                "last_activity_at": END.isoformat(),
            },
        )

    assert response.status_code == 202
    assert response.json()["sandbox"]["state"] == "stopped"
    host_pool.get_host.assert_awaited_once_with(host="runner-one")
    sandbox_manager_factory.assert_called_once_with(host_pool.get_host.return_value)
    sandbox_manager.shared_chat_sandbox_status.assert_awaited_once_with("athr_other", timeout=5)
    sandbox_manager.list_sandboxes.assert_not_awaited()
    commands.request_admin_delete.assert_awaited_once_with(
        sandbox_id="asbx_other",
        generation=1,
        container_id="container-one",
        last_activity_at=END,
        actor_id="admin-token",
        admin_ip="127.0.0.1",
        sandbox_manager_confirmed_stopped=True,
        confirmed_docker_status=docker_status,
        discard=discard,
    )


@pytest.mark.parametrize(
    "changes",
    [
        {"sandbox_id": "asbx_replaced"},
        {"thread_id": "athr_replaced"},
        {"container_id": "replacement"},
        {"generation": 2},
        {"origin_thread_id": "athr_replaced"},
        {"running": True, "docker_status": "running"},
        {"running": True, "docker_status": "dead"},
        {"docker_status": "created"},
        {"docker_status": "paused"},
        {"busy": True},
        {"group_busy": True},
        {"terminal_count": 1},
        {"terminal_count": True},
        {"operation_count": 1},
        {"operation_count": None},
        {"snapshotting": True},
    ],
)
@pytest.mark.parametrize("discard", [False, True])
async def test_force_delete_rejects_changed_running_or_non_quiescent_host_proof(
    terminal_probe, changes, discard
):
    app, queries, commands, _, sandbox_manager, _ = terminal_probe
    sandbox_manager.shared_chat_sandbox_status.return_value.update(
        {
            "running": False,
            "docker_status": "stopped",
            "group_busy": False,
            "terminal_count": 0,
            "operation_count": 0,
            "snapshotting": False,
        }
        | changes
    )
    queries.detail.return_value = sandbox_detail()
    app.dependency_overrides[admin_sandboxes.admin_sandbox_deletion_available] = lambda: not discard
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post(
            f"{PREFIX}/asbx_other/delete?force=true&discard={str(discard).lower()}",
            headers=ADMIN,
            json={
                "generation": 1,
                "container_id": "container-one",
                "last_activity_at": END.isoformat(),
            },
        )

    assert response.status_code == 409
    commands.request_admin_delete.assert_not_awaited()


@pytest.mark.parametrize(
    "failure", ["missing_host", "refused_url", "sandbox_manager_error", "missing_row"]
)
async def test_force_delete_fails_closed_when_host_proof_is_unavailable(terminal_probe, failure):
    app, queries, commands, host_pool, sandbox_manager, sandbox_manager_factory = terminal_probe
    queries.detail.return_value = sandbox_detail()
    if failure == "missing_host":
        host_pool.get_host.return_value = None
    elif failure == "refused_url":
        sandbox_manager_factory.side_effect = SandboxManagerUrlRefused("not allowed")
    elif failure == "sandbox_manager_error":
        sandbox_manager.shared_chat_sandbox_status.side_effect = SandboxManagerError(
            503, "PRIVATE diagnostic"
        )
    else:
        sandbox_manager.shared_chat_sandbox_status.side_effect = SandboxManagerError(
            410, "PRIVATE lost"
        )
    app.dependency_overrides[admin_sandboxes.admin_sandbox_deletion_available] = lambda: True
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post(
            f"{PREFIX}/asbx_other/delete?force=true",
            headers=ADMIN,
            json={
                "generation": 1,
                "container_id": "container-one",
                "last_activity_at": END.isoformat(),
            },
        )

    assert response.status_code == (503 if failure != "missing_row" else 409)
    assert "PRIVATE" not in response.text
    commands.request_admin_delete.assert_not_awaited()


@pytest.mark.parametrize(
    ("failure", "cause_keywords"),
    [
        ("missing_host", ("host", "register")),
        ("blank_url", ("advertis", "sandbox manager")),
        ("missing_token", ("credential", "control plane")),
        ("malformed_url", ("url", "invalid")),
        ("refused_url", ("url", "allowlist")),
    ],
)
async def test_force_delete_explains_configuration_failure_without_contacting_sandbox_manager(
    terminal_probe, monkeypatch, caplog, failure, cause_keywords
):
    app, _, commands, host_pool, sandbox_manager, _ = terminal_probe
    host = {"host": "runner-one", "sandbox_manager_url": "http://sandbox-manager-one:8092"}
    settings = {sandbox_manager_routing.ENV_SANDBOX_MANAGER_TOKEN: "PRIVATE sandbox manager token"}
    if failure == "missing_host":
        host_pool.get_host.return_value = None
    else:
        if failure == "blank_url":
            host["sandbox_manager_url"] = "   "
        elif failure == "missing_token":
            settings[sandbox_manager_routing.ENV_SANDBOX_MANAGER_TOKEN] = "   "
        elif failure == "malformed_url":
            host["sandbox_manager_url"] = "http://[PRIVATE-internal"
        elif failure == "refused_url":
            host["sandbox_manager_url"] = "http://PRIVATE-denied.internal:8092"
            settings[sandbox_manager_routing.ENV_SANDBOX_MANAGER_HOSTS] = "sandbox-manager-one"
        host_pool.get_host.return_value = host
    monkeypatch.setattr(
        sandbox_manager_routing,
        "setting_env",
        lambda name, default=None: settings.get(name, default),
    )
    monkeypatch.setattr(
        admin_sandboxes,
        "sandbox_manager_for_host",
        sandbox_manager_routing.sandbox_manager_for_host,
    )
    app.dependency_overrides[admin_sandboxes.admin_sandbox_deletion_available] = lambda: True

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post(
            f"{PREFIX}/asbx_other/delete?force=true",
            headers=ADMIN,
            json={
                "generation": 1,
                "container_id": "container-one",
                "last_activity_at": END.isoformat(),
            },
        )

    assert response.status_code == 503
    detail = response.json()["detail"].lower()
    assert all(keyword in detail for keyword in cause_keywords)
    assert any(action in detail for action in ("configure", "settings", "register", "check"))
    assert "PRIVATE" not in response.text
    assert "http://" not in response.text
    sandbox_manager.shared_chat_sandbox_status.assert_not_awaited()
    commands.request_admin_delete.assert_not_awaited()
    records = [
        record
        for record in caplog.records
        if getattr(record, "event", None) == "agent_sandbox_delete_verification_failed"
    ]
    assert len(records) == 1
    record = records[0]
    assert record.sandbox_id == "asbx_other"
    assert record.host == "runner-one"
    assert record.sandbox_manager_status == 503
    assert record.failure_type == "configuration"
    assert record.exc_info is None
    assert "PRIVATE" not in str(record.__dict__)
    assert "http://" not in str(record.__dict__)


@pytest.mark.parametrize(
    ("failure", "cause_keywords"),
    [
        ("unauthorized", ("credential",)),
        ("forbidden", ("credential",)),
        ("timeout", ("time",)),
        ("unreachable", ("connect",)),
        ("sandbox_manager_response", ("sandbox manager", "status")),
    ],
)
async def test_force_delete_distinguishes_sandbox_manager_failures_without_exposing_private_diagnostics(
    terminal_probe, caplog, failure, cause_keywords
):
    app, _, commands, _, sandbox_manager, _ = terminal_probe
    status_code = {"unauthorized": 401, "forbidden": 403}.get(failure, 503)
    error = SandboxManagerError(status_code, "PRIVATE diagnostic http://PRIVATE-internal:8092")
    request = Request("GET", "http://PRIVATE-internal:8092/shared-sandboxes/athr_other")
    if failure == "timeout":
        error.__cause__ = ReadTimeout("PRIVATE timeout", request=request)
    elif failure == "unreachable":
        error.__cause__ = ConnectError("PRIVATE connection diagnostic", request=request)
    sandbox_manager.shared_chat_sandbox_status.side_effect = error
    app.dependency_overrides[admin_sandboxes.admin_sandbox_deletion_available] = lambda: True

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post(
            f"{PREFIX}/asbx_other/delete?force=true",
            headers=ADMIN,
            json={
                "generation": 1,
                "container_id": "container-one",
                "last_activity_at": END.isoformat(),
            },
        )

    assert response.status_code == 503
    detail = response.json()["detail"].lower()
    assert all(keyword in detail for keyword in cause_keywords)
    assert any(action in detail for action in ("configure", "check", "refresh", "retry", "match"))
    if failure == "sandbox_manager_response":
        assert "restore its connection" not in detail
    assert "PRIVATE" not in response.text
    assert "http://" not in response.text
    sandbox_manager.shared_chat_sandbox_status.assert_awaited_once_with("athr_other", timeout=5)
    commands.request_admin_delete.assert_not_awaited()
    records = [
        record
        for record in caplog.records
        if getattr(record, "event", None) == "agent_sandbox_delete_verification_failed"
    ]
    assert len(records) == 1
    record = records[0]
    assert record.sandbox_id == "asbx_other"
    assert record.host == "runner-one"
    assert record.sandbox_manager_status == status_code
    assert record.failure_type == type(error.__cause__ or error).__name__
    assert record.exc_info is None
    assert "PRIVATE" not in str(record.__dict__)
    assert "http://" not in str(record.__dict__)


async def test_force_delete_rejects_a_stale_review_before_contacting_host(terminal_probe):
    app, queries, commands, host_pool, sandbox_manager, _ = terminal_probe
    queries.detail.return_value = sandbox_detail()
    app.dependency_overrides[admin_sandboxes.admin_sandbox_deletion_available] = lambda: True
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post(
            f"{PREFIX}/asbx_other/delete?force=true",
            headers=ADMIN,
            json={
                "generation": 2,
                "container_id": "container-one",
                "last_activity_at": END.isoformat(),
            },
        )

    assert response.status_code == 409
    host_pool.get_host.assert_not_awaited()
    sandbox_manager.shared_chat_sandbox_status.assert_not_awaited()
    commands.request_admin_delete.assert_not_awaited()


async def test_delete_body_cannot_claim_host_confirmation(setup):
    app, queries, _ = setup
    commands = SimpleNamespace(request_admin_delete=AsyncMock())
    app.dependency_overrides[get_storage] = lambda: SimpleNamespace(
        sandbox=commands, admin_sandboxes=queries
    )
    app.dependency_overrides[admin_sandboxes.admin_sandbox_deletion_available] = lambda: True
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post(
            f"{PREFIX}/asbx_other/delete",
            headers=ADMIN,
            json={
                "generation": 1,
                "container_id": "container-one",
                "last_activity_at": END.isoformat(),
                "force": True,
            },
        )

    assert response.status_code == 422
    commands.request_admin_delete.assert_not_awaited()


@pytest.mark.parametrize("force", [False, True])
async def test_delete_without_archive_configuration_does_not_fence_sandbox(setup, force):
    app, queries, _ = setup
    commands = SimpleNamespace(request_admin_delete=AsyncMock())
    app.dependency_overrides[get_storage] = lambda: SimpleNamespace(
        sandbox=commands, admin_sandboxes=queries
    )
    app.dependency_overrides[admin_sandboxes.admin_sandbox_deletion_available] = lambda: False
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post(
            f"{PREFIX}/asbx_other/delete{'?force=true' if force else ''}",
            headers=ADMIN,
            json={
                "generation": 1,
                "container_id": "container-one",
                "last_activity_at": END.isoformat(),
            },
        )
    assert response.status_code == 503
    assert "S3 bucket" in response.json()["detail"]
    commands.request_admin_delete.assert_not_awaited()


async def test_discard_deletion_does_not_require_archive_configuration(setup):
    from cloud_agent.admin_sandboxes import AdminSandboxDetail

    app, queries, _ = setup
    queries.detail = AsyncMock(
        return_value=AdminSandboxDetail(
            sandbox=sandbox() | {"state": "stopped", "deletion_state": "pending"},
            members=[],
            observed_at=END,
        )
    )
    commands = SimpleNamespace(request_admin_delete=AsyncMock(return_value="accepted"))
    app.dependency_overrides[get_storage] = lambda: SimpleNamespace(
        sandbox=commands, admin_sandboxes=queries
    )
    app.dependency_overrides[admin_sandboxes.admin_sandbox_deletion_available] = lambda: False
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post(
            f"{PREFIX}/asbx_other/delete?discard=true",
            headers=ADMIN,
            json={
                "generation": 1,
                "container_id": "container-one",
                "last_activity_at": END.isoformat(),
            },
        )
    assert response.status_code == 202
    commands.request_admin_delete.assert_awaited_once_with(
        sandbox_id="asbx_other",
        generation=1,
        container_id="container-one",
        last_activity_at=END,
        actor_id="admin-token",
        admin_ip="127.0.0.1",
        sandbox_manager_confirmed_stopped=False,
        confirmed_docker_status=None,
        discard=True,
    )


@pytest.mark.parametrize(
    "params",
    [
        {"limit": 0},
        {"limit": -1},
        {"limit": 201},
        {"limit": "invalid"},
        {"cursor": "bad"},
        {"cursor": ""},
        {"cursor": "x" * 2049},
        {"cursor": encode_cursor(SandboxCursor(scope="another-surface", after_id="asbx_one"))},
        {
            "cursor": encode_cursor(
                SandboxCursor(scope=SANDBOX_CURSOR_SCOPE, after_id="asbx_one").model_copy(
                    update={"version": 2}
                )
            )
        },
    ],
)
async def test_invalid_limits_and_cursors_fail_before_queries(setup, params):
    app, queries, _ = setup
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get(PREFIX, headers=ADMIN, params=params)
    assert response.status_code == 422
    queries.list_running.assert_not_awaited()


async def test_unavailable_storage_returns_503(monkeypatch):
    monkeypatch.setenv("ADMIN_TOKEN", "admin-observations-test")
    app = FastAPI()
    app.include_router(admin_sandboxes.router)
    app.dependency_overrides[get_storage] = lambda: None
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get(PREFIX, headers=ADMIN)
    assert response.status_code == 503


def test_sandboxes_are_only_on_the_main_listener():
    """Cross-user runtime metadata is never available to sandbox MCP clients."""
    assert PREFIX in create_app().openapi()["paths"]
    assert PREFIX not in create_mcp_app().openapi()["paths"]
    assert PREFIX + "/{sandbox_id}" not in create_mcp_app().openapi()["paths"]
    assert PREFIX + "/{sandbox_id}/trajectory" in create_app().openapi()["paths"]
    assert PREFIX + "/{sandbox_id}/trajectory" not in create_mcp_app().openapi()["paths"]
    assert PREFIX + "/{sandbox_id}/stop" not in create_mcp_app().openapi()["paths"]
    assert PREFIX + "/{sandbox_id}/archive" in create_app().openapi()["paths"]
    assert PREFIX + "/{sandbox_id}/restore" in create_app().openapi()["paths"]
    assert PREFIX + "/{sandbox_id}/archive" not in create_mcp_app().openapi()["paths"]
    assert PREFIX + "/{sandbox_id}/restore" not in create_mcp_app().openapi()["paths"]
    assert PREFIX + "/{sandbox_id}/delete" in create_app().openapi()["paths"]
    assert PREFIX + "/{sandbox_id}/delete" not in create_mcp_app().openapi()["paths"]
