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
from tests.servers.test_owner_revalidation import _AccessGateway
from tests.support.storage import OneFake
from tests.unit.test_ledger_snapshots import END, START, ledger_row

from cloud_agent import identity
from cloud_agent.admin_observability import (
    AdminActivityPage,
    AdminRun,
    AdminRunList,
    cursor_scope,
)
from cloud_agent.api import admin_runs, usage
from cloud_agent.api._owner_auth import require_agent_owner
from cloud_agent.api.deps import get_gateway_client, get_storage
from cloud_agent.app import create_app, create_mcp_app
from cloud_agent.schemas import ResourceMetricBrief, RunResourceSummary
from cloud_agent_host.event_payloads import MAX_EVENT_PAYLOAD_BYTES

ADMIN = {"Authorization": "Bearer admin-observations-test"}
PREFIX = "/admin/agent/runs"
PATHS = [
    ("GET", PREFIX),
    ("GET", PREFIX + "/ajob_other"),
    ("GET", PREFIX + "/ajob_other/activity"),
    ("GET", PREFIX + "/ajob_other/attempts/10/resources"),
    ("POST", PREFIX + "/ajob_other/events/20/content"),
]


def _admin_run_row() -> dict:
    """One run row in the shape ``AdminRunQueries.list_runs`` returns."""
    return {
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


@pytest.fixture
def setup(monkeypatch):
    monkeypatch.setenv("ADMIN_TOKEN", "admin-observations-test")
    monkeypatch.setenv(identity.ENV_SESSION_SECRET, "admin-observations-session-test-long-secret")
    monkeypatch.setenv(identity.ENV_ISSUER, "https://gateway.example")
    run = _admin_run_row()
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
    store = OneFake(
        SimpleNamespace(
            get_job=AsyncMock(return_value=run),
            ledger_rows_for_job=AsyncMock(return_value=[ledger_row()]),
            record_admin_action=AsyncMock(return_value=1),
            queue_job_ledger_refresh=AsyncMock(),
            job_usage_inputs=AsyncMock(),
        )
    )
    gateway = _AccessGateway(["agent.use", "agent.admin"])
    app = FastAPI()
    app.include_router(admin_runs.router)
    app.include_router(usage.router)
    app.dependency_overrides[admin_runs.get_admin_run_queries] = lambda: queries
    app.dependency_overrides[get_storage] = lambda: store
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
    assert response.json()["session_statistics"] == {
        "model_seconds": None,
        "tool_seconds": None,
        "ttft_seconds": None,
        "output_tokens_per_second": None,
        "timing_source": "message_events",
    }


async def test_session_statistics_reuses_safe_usage_inputs_without_additional_reads(setup):
    app, queries, store, gateway = setup
    queries.usage_inputs.return_value = {
        "job_ids": ["ajob_other"],
        "attempts": [{"job_id": "ajob_other", "attempt_id": 10, "started_at": START}],
        "events": [
            {
                "job_id": "ajob_other",
                "attempt_id": 10,
                "event_type": kind,
                "seq": seq,
                "created_at": START + timedelta(seconds=seconds),
                "payload": payload,
            }
            for seq, (seconds, kind, payload) in enumerate(
                [
                    (0, "lifecycle", {}),
                    (2, "tool_use", {"id": "hashed-identity"}),
                    (8, "tool_result", {"tool_use_id": "hashed-identity"}),
                    (10, "message", {"usage": {"output_tokens": 80}}),
                ],
                start=1,
            )
        ],
    }
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get(PREFIX + "/ajob_other", headers=ADMIN)
    assert response.status_code == 200
    assert response.json()["session_statistics"] == {
        "model_seconds": 4,
        "tool_seconds": 6,
        "ttft_seconds": 2,
        "output_tokens_per_second": 20,
        "timing_source": "message_events",
    }
    assert "hashed-identity" not in response.text
    queries.usage_inputs.assert_awaited_once_with("ajob_other")
    queries.event_content.assert_not_awaited()
    queries.resources.assert_not_awaited()
    store.get_job.assert_not_awaited()
    store.job_usage_inputs.assert_not_awaited()
    store.record_admin_action.assert_not_awaited()
    assert gateway.asked == []


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


def _turn_summary_snapshot(*, state: str, observed_through_ms: int) -> dict[str, object]:
    """One stored snapshot in the canonical shape the route reduces."""
    return {
        "schema_version": 1,
        "measurement_id": "00000000-0000-4000-8000-000000000001",
        "report_seq": 9,
        "state": state,
        "scope": "member_execution_window",
        "source": "kata-guest-member-window-v1",
        "window": {
            "duration_ms": 130_000,
            "observed_through_ms": observed_through_ms,
            "open_scan_ms": 12,
            "close_scan_ms": 15,
        },
        "cpu": {
            "status": "available",
            "method": "live_and_exit_task_counters",
            "usec": 80_000_000,
            "reason_codes": [],
        },
        "memory": {
            "status": "available",
            "method": "sampled_member_pss",
            "peak_bytes": 1_288_490_189,
            "sample_interval_ms": 1000,
            "valid_samples": 131,
            "expected_samples": 131,
            "max_gap_ms": 1000,
            "reason_codes": [],
        },
        "storage_io": {
            "status": "unavailable",
            "method": "task_io_accounting",
            "read_bytes": None,
            "write_bytes": None,
            "reason_codes": ["filesystem_attribution_unverified"],
        },
    }


async def test_a_terminal_attempt_reports_its_prefix_as_interrupted_not_final(setup):
    """The derived state is the reason the reducer exists on this path."""
    app, queries, _, _ = setup
    queries.resources.return_value.update(
        {
            "attempt_terminal": True,
            "turn_summary_received_at": END,
            "turn_summary": _turn_summary_snapshot(state="collecting", observed_through_ms=12_000),
        }
    )

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get(PREFIX + "/ajob_other/attempts/10/resources", headers=ADMIN)

    assert response.status_code == 200
    turn = response.json()["resources"]["turn_summary"]
    assert turn["state"] == "interrupted"
    assert turn["window"]["observed_through_ms"] == 12_000
    assert turn["window"]["duration_ms"] == 130_000
    assert turn["cpu"]["usec"] == 80_000_000


async def test_the_stored_snapshot_is_not_rewritten_by_a_read(setup):
    """Reads never mutate: the stored state stays what the guest reported."""
    app, queries, _, _ = setup
    stored = _turn_summary_snapshot(state="collecting", observed_through_ms=12_000)
    queries.resources.return_value.update({"attempt_terminal": True, "turn_summary": stored})

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        await client.get(PREFIX + "/ajob_other/attempts/10/resources", headers=ADMIN)

    assert stored["state"] == "collecting"


async def test_an_unreadable_snapshot_does_not_break_the_detail_view(setup):
    """A damaged row must not fail an admin page; it reports no summary."""
    app, queries, _, _ = setup
    queries.resources.return_value["turn_summary"] = {"schema_version": 99}

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get(PREFIX + "/ajob_other/attempts/10/resources", headers=ADMIN)

    assert response.status_code == 200
    assert response.json()["resources"]["turn_summary"] is None


async def test_the_runs_list_carries_the_compact_resource_summary(setup):
    """The list projection is the model's, already folded by the shared reducer."""
    app, queries, _, _ = setup
    summary = RunResourceSummary(
        cpu_usec=80_000_000,
        memory_peak_bytes=1_288_490_189,
        state="final",
        observed_through_ms=130_000,
        cpu=ResourceMetricBrief(status="available"),
        memory=ResourceMetricBrief(status="available"),
        storage_io=ResourceMetricBrief(
            status="unavailable",
            reason_codes=["filesystem_attribution_unverified"],
        ),
        attempts_measured=2,
        attempts_total=3,
    )
    queries.list_runs.return_value = AdminRunList(
        runs=[
            AdminRun.model_validate({**_admin_run_row(), "resource_summary": summary}),
        ],
        since=START,
        until=END,
    )

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get(PREFIX, headers=ADMIN)

    assert response.status_code == 200
    listed = response.json()["runs"][0]["resource_summary"]
    assert listed["cpu_usec"] == 80_000_000
    assert listed["memory_peak_bytes"] == 1_288_490_189
    assert listed["storage_io"]["status"] == "unavailable"
    assert listed["cpu"]["status"] == "available"
    assert listed["attempts_measured"] == 2
    assert listed["attempts_total"] == 3


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


async def test_resource_read_preserves_the_measured_cpu_quota_and_nested_window(setup):
    """The frontend denominator is the recorded window and its captured quota."""
    app, queries, _, _ = setup
    stored = _turn_summary_snapshot(state="final", observed_through_ms=130_000)
    stored["cpu"]["capacity_cores"] = 2.0
    queries.resources.return_value.update({"attempt_terminal": True, "turn_summary": stored})
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get(PREFIX + "/ajob_other/attempts/10/resources", headers=ADMIN)
    assert response.status_code == 200
    summary = response.json()["resources"]["turn_summary"]
    assert summary["cpu"]["capacity_cores"] == 2.0
    assert summary["window"]["duration_ms"] == 130_000
    assert "duration_ms" not in summary
