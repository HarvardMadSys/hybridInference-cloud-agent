"""Admin authorization, pagination and content boundaries for retained sandboxes."""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from tests.servers.test_admin_runs import ADMIN, setup as run_setup  # noqa: F401
from tests.unit.test_ledger_snapshots import END, START

from cloud_agent import identity
from cloud_agent.admin_observability import encode_cursor
from cloud_agent.admin_sandboxes import SANDBOX_CURSOR_SCOPE, SandboxCursor
from cloud_agent.api import admin_sandboxes
from cloud_agent.api.deps import get_storage
from cloud_agent.app import create_app, create_mcp_app

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
        )
    )
    app.include_router(admin_sandboxes.router)
    app.dependency_overrides[admin_sandboxes.get_admin_sandbox_queries] = lambda: queries
    return app, queries, gateway


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


@pytest.mark.parametrize("path", ["/asbx_other", "/asbx_other/stop"])
async def test_detail_and_control_require_current_admin_access(setup, path):
    app, _, gateway = setup
    gateway.permissions = ["agent.use"]
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        client.cookies.set(identity.SESSION_COOKIE, identity.mint_session(user_id="admin"))
        response = await client.request(
            "POST" if path.endswith("stop") else "GET",
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
    assert PREFIX + "/{sandbox_id}/stop" not in create_mcp_app().openapi()["paths"]
