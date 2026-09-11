"""Real admin authorization and bounded, audited observation API responses."""

from __future__ import annotations

import base64
import json
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from cloud_agent import identity
from cloud_agent.admin_observability import AdminActivityPage, AdminRunList, cursor_scope
from cloud_agent.api import admin_runs, usage
from cloud_agent.api.deps import get_agent_job_store, get_gateway_client
from cloud_agent.api.routes import require_agent_owner
from cloud_agent.app import create_app, create_mcp_app
from cloud_agent_host.event_payloads import MAX_EVENT_PAYLOAD_BYTES
from tests.servers.test_owner_revalidation import _AccessGateway
from tests.unit.test_ledger_snapshots import END, START, ledger_row

ADMIN = {"Authorization": "Bearer admin-observations-test"}
PREFIX = "/admin/agent/runs"
PATHS = [
    ("GET", PREFIX),
    ("GET", PREFIX + "/ajob_other"),
    ("GET", PREFIX + "/ajob_other/activity"),
    ("GET", PREFIX + "/ajob_other/attempts/10/resources"),
    ("POST", PREFIX + "/ajob_other/events/20/content"),
]


@pytest.fixture
def setup(monkeypatch):
    monkeypatch.setenv("ADMIN_TOKEN", "admin-observations-test")
    monkeypatch.setenv(identity.ENV_SESSION_SECRET, "admin-observations-session-test-long-secret")
    monkeypatch.setenv(identity.ENV_ISSUER, "https://gateway.example")
    run = {
        "id": "ajob_other",
        "thread_id": "athr_other",
        "title": "A task title",
        "turn_no": 2,
        "user_id": "other-owner",
        "repo": "example/repository",
        "runtime": "claude-code",
        "model": "example-model",
        "execution_profile": "untrusted-kata-v1",
        "state": "succeeded",
        "created_at": START,
        "updated_at": END,
        "hosts": ["example-runner"],
        "tool_calls": 1,
        # A response model is a second boundary even if a future query grows.
        "task_prompt": "private prompt",
        "detail": "private exception output",
        "metadata": {"token": "private credential"},
    }
    event = {
        "id": 20,
        "attempt_id": 10,
        "seq": 1,
        "event_type": "tool_use",
        "created_at": START,
        "payload": {"input": {"command": "private command"}},
    }
    events = [
        {
            "job_id": run["id"],
            "attempt_id": attempt,
            "event_type": kind,
            "seq": seq,
            "created_at": END,
            "payload": {
                "usage_id": "same-response",
                "usage": {
                    "input_tokens": 100,
                    "output_tokens": 10,
                    "cache_read_input_tokens": 80,
                    "cache_creation_input_tokens": 20,
                },
            },
        }
        for attempt in (10, 11)
        for seq, kind in enumerate(("thinking", "tool_use", "message"), start=1)
    ]
    queries = SimpleNamespace(
        list_runs=AsyncMock(return_value=AdminRunList(runs=[], since=START, until=END)),
        get_run=AsyncMock(return_value=run),
        attempts=AsyncMock(return_value=[]),
        usage_inputs=AsyncMock(
            return_value={"job_ids": [run["id"]], "attempts": [], "events": events}
        ),
        activity=AsyncMock(return_value=AdminActivityPage(events=[])),
        resources=AsyncMock(
            return_value={
                "attempt_id": 10,
                "attempt_no": 1,
                "started_at": START,
                "finished_at": END,
                "sampler_version": "shared-member-v1",
                "coverage": 0,
                "tool_calls": [
                    {
                        "call_seq": 1,
                        "tool_use_id": "t1",
                        "tool_name": "Bash",
                        "timing_source": "message",
                        "duration_ms": 25,
                    }
                ],
            }
        ),
        event_content=AsyncMock(return_value=event),
    )
    store = SimpleNamespace(
        get_job=AsyncMock(return_value=run),
        ledger_rows_for_job=AsyncMock(return_value=[ledger_row()]),
        record_admin_action=AsyncMock(return_value=1),
        queue_job_ledger_refresh=AsyncMock(),
        job_usage_inputs=AsyncMock(),
    )
    gateway = _AccessGateway(["agent.use", "agent.admin"])
    app = FastAPI()
    app.include_router(admin_runs.router)
    app.include_router(usage.router)
    app.dependency_overrides[admin_runs.get_admin_run_queries] = lambda: queries
    app.dependency_overrides[get_agent_job_store] = lambda: store
    app.dependency_overrides[get_gateway_client] = lambda: gateway
    app.dependency_overrides[require_agent_owner] = lambda: {"user_id": "admin-person"}
    return app, queries, store, gateway


@pytest.mark.parametrize("method,path", PATHS)
@pytest.mark.parametrize("auth", ["missing", "invalid", "revoked"])
async def test_every_read_and_reveal_requires_current_admin_permission(setup, method, path, auth):
    app, queries, store, gateway = setup
    gateway.permissions = ["agent.use"]
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        headers = {}
        if auth == "invalid":
            headers = {"Authorization": "Bearer not-an-admin-token"}
        elif auth == "revoked":
            client.cookies.set(
                identity.SESSION_COOKIE,
                identity.mint_session(user_id="admin-person"),
            )
        response = await client.request(method, path, headers=headers)
    assert response.status_code == (403 if auth == "revoked" else 401)
    for function in vars(queries).values():
        function.assert_not_awaited()
    store.ledger_rows_for_job.assert_not_awaited()
    store.record_admin_action.assert_not_awaited()


async def test_admin_reads_another_owner_but_owner_routes_are_unchanged(setup):
    app, queries, store, _ = setup
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        admin = await client.get(PREFIX + "/ajob_other", headers=ADMIN)
        owner = await client.get("/v1/agent/jobs/ajob_other/usage")
    assert admin.status_code == 200
    assert admin.headers["cache-control"] == "no-store"
    assert owner.status_code == 404
    body = admin.json()
    assert body["run"]["user_id"] == "other-owner"
    assert body["usage"]["ledger"]["totals"]["calls"] == 1
    assert body["usage"]["runtime"]["input_tokens"] == 200  # two attempts, not six blocks
    assert body["usage"]["runtime"]["normalized_input_tokens"] == 400
    assert body["usage"]["conversation"] is None
    assert body["capabilities"] == {"llm_requests": True}
    assert body["usage"]["ledger"]["finalized"] is False
    assert body["usage"]["ledger"]["unknown_rows"]["reasoning"] == 1
    assert "private" not in admin.text
    assert "grant_id" not in admin.text
    assert "binding_id" not in admin.text
    store.job_usage_inputs.assert_not_awaited()
    queries.event_content.assert_not_awaited()
    store.record_admin_action.assert_not_awaited()
    store.queue_job_ledger_refresh.assert_not_awaited()


async def test_empty_terminal_turn_has_no_promised_future_usage(setup):
    app, queries, store, _ = setup
    store.ledger_rows_for_job.return_value = []
    queries.usage_inputs.return_value = {"job_ids": ["ajob_other"], "attempts": [], "events": []}
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get(PREFIX + "/ajob_other", headers=ADMIN)
    assert response.json()["usage"]["ledger"]["status"] == "unavailable"
    assert response.json()["usage"]["runtime"]["input_tokens"] is None


async def test_resource_read_keeps_message_timing_and_unavailable_whole_turn(setup):
    app, queries, store, _ = setup
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get(PREFIX + "/ajob_other/attempts/10/resources", headers=ADMIN)
    assert response.status_code == 200
    resource = response.json()["resources"]
    assert resource["coverage"] == 0
    assert resource["cpu_usec"] is None
    assert resource["tool_calls"][0]["timing_source"] == "message"
    assert resource["tool_calls"][0]["duration_ms"] == 25
    queries.resources.assert_awaited_once_with("ajob_other", 10)
    queries.event_content.assert_not_awaited()
    store.get_job.assert_not_awaited()


@pytest.mark.parametrize("browser", [False, True])
async def test_explicit_reveal_records_actor_owner_and_event_before_return(setup, browser):
    app, queries, store, gateway = setup
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        if browser:
            client.cookies.set(
                identity.SESSION_COOKIE,
                identity.mint_session(user_id="admin-person"),
            )
        response = await client.post(
            PREFIX + "/ajob_other/events/20/content", headers={} if browser else ADMIN
        )
    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    assert response.json()["payload"]["input"]["command"] == "private command"
    record = store.record_admin_action.await_args.kwargs
    assert record["target_user_id"] == "other-owner"
    assert record["details"] == {
        "actor": "admin:admin-person" if browser else "admin-token",
        "job_id": "ajob_other",
        "event_id": 20,
        "attempt_id": 10,
        "seq": 1,
    }
    assert record["action"] == "agent.run.event_content.read"
    assert "private command" not in str(record)
    assert gateway.asked == (["admin-person"] if browser else [])
    queries.event_content.assert_awaited_once_with("ajob_other", 20)


async def test_audit_failure_never_returns_content(setup):
    app, _, store, _ = setup
    store.record_admin_action.side_effect = RuntimeError("private database diagnostic")
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post(PREFIX + "/ajob_other/events/20/content", headers=ADMIN)
    assert response.status_code == 503
    assert "private" not in response.text


async def test_missing_audit_receipt_never_returns_content(setup):
    app, _, store, _ = setup
    store.record_admin_action.return_value = None
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post(PREFIX + "/ajob_other/events/20/content", headers=ADMIN)
    assert response.status_code == 503
    assert "private command" not in response.text


@pytest.mark.parametrize("text", ["a" * MAX_EVENT_PAYLOAD_BYTES, '😀\n"' * 100_000])
async def test_reveal_bounds_the_actual_response_including_envelope_and_escaping(setup, text):
    app, queries, _, _ = setup
    queries.event_content.return_value["payload"] = {"text": text}
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post(PREFIX + "/ajob_other/events/20/content", headers=ADMIN)
    assert response.status_code == 200
    assert len(response.content) <= MAX_EVENT_PAYLOAD_BYTES
    assert response.json()["truncated"] is True
    assert response.json()["payload"]["truncation_notice"]


async def test_event_and_attempt_ids_are_scoped_to_the_requested_job(setup):
    app, queries, store, _ = setup
    queries.event_content.return_value = None
    queries.resources.return_value = None
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        event = await client.post(PREFIX + "/ajob_other/events/21/content", headers=ADMIN)
        resources = await client.get(PREFIX + "/ajob_other/attempts/11/resources", headers=ADMIN)
    assert event.status_code == resources.status_code == 404
    store.record_admin_action.assert_not_awaited()


async def test_list_default_window_and_exact_filters_are_forwarded(setup):
    app, queries, _, _ = setup
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get(
            PREFIX, params={"repo": "example/repository", "user_id": "other-owner"}, headers=ADMIN
        )
    assert response.status_code == 200
    args = queries.list_runs.await_args.kwargs
    assert args["until"] - args["since"] == timedelta(days=7)
    assert args["filters"].repo == "example/repository"
    assert args["filters"].user_id == "other-owner"
    assert args["limit"] == 50


@pytest.mark.parametrize(
    "query",
    [
        {"cursor": "not-a-cursor"},
        {"limit": 201},
        {"since": "2026-01-01T00:00:00", "until": "2026-01-02T00:00:00Z"},
        {"since": "2026-01-02T00:00:00Z", "until": "2026-01-01T00:00:00Z"},
        {"since": "2024-01-01T00:00:00Z", "until": "2026-01-01T00:00:00Z"},
    ],
)
async def test_invalid_or_unbounded_queries_are_rejected_before_store_reads(setup, query):
    app, queries, _, _ = setup
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get(PREFIX, params=query, headers=ADMIN)
    assert response.status_code == 422
    queries.list_runs.assert_not_awaited()


@pytest.mark.parametrize("field", ["snapshot_id", "attempt_id", "attempt_no", "seq"])
async def test_cursor_integer_overflow_is_rejected_before_sql(setup, field):
    app, queries, _, _ = setup
    raw = {
        "version": 1,
        "scope": cursor_scope(["ajob_other", None]),
        "snapshot_id": 100,
        "attempt_id": 10,
        "attempt_no": 1,
        "seq": 1,
    }
    raw[field] = 10**100
    cursor = base64.urlsafe_b64encode(json.dumps(raw).encode()).decode()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get(
            PREFIX + "/ajob_other/activity", params={"cursor": cursor}, headers=ADMIN
        )
    assert response.status_code == 422
    queries.get_run.assert_not_awaited()
    queries.activity.assert_not_awaited()


@pytest.mark.parametrize(
    "method,path",
    [
        ("GET", f"/ajob_other/activity?attempt_id={2**63}"),
        ("GET", f"/ajob_other/attempts/{2**63}/resources"),
        ("POST", f"/ajob_other/events/{2**63}/content"),
    ],
)
async def test_path_and_query_identity_overflow_never_reaches_sql(setup, method, path):
    app, queries, _, _ = setup
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.request(method, PREFIX + path, headers=ADMIN)
    assert response.status_code == 422
    for function in vars(queries).values():
        function.assert_not_awaited()


def test_admin_observation_routes_are_registered_only_on_the_control_plane():
    control = create_app().openapi()["paths"]
    relay = create_mcp_app().openapi()["paths"]
    paths = {path for path in control if path.startswith(PREFIX)}
    assert len(paths) == 7
    assert not paths.intersection(relay)
