"""Owner scoping and cached ledger/runtime reads over the real API routes."""

from __future__ import annotations

from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from tests.support.storage import OneFake
from tests.unit.test_ledger_snapshots import END, START, ledger_row

from cloud_agent.api import usage
from cloud_agent.api._owner_auth import require_agent_owner
from cloud_agent.api.deps import get_storage
from cloud_agent.app import create_app, create_mcp_app


@pytest.fixture
def store():
    events = [
        {
            "job_id": "ajob_1",
            "attempt_id": attempt,
            "seq": 1,
            "event_type": "message",
            "created_at": END,
            "payload": {
                "has_text": True,
                "usage_id": "same-id",
                "usage": {"input_tokens": 100, "output_tokens": 10, "cache_read_input_tokens": 80},
            },
        }
        for attempt in (10, 11)
    ]
    return OneFake(
        SimpleNamespace(
            get_job=AsyncMock(return_value={"id": "ajob_1", "user_id": "owner"}),
            ledger_rows_for_job=AsyncMock(return_value=[ledger_row()]),
            ledger_rows_for_conversation=AsyncMock(return_value=[ledger_row(job_id="ajob_1")]),
            job_usage_inputs=AsyncMock(
                return_value={
                    "job_ids": ["ajob_1"],
                    "attempts": [{"job_id": "ajob_1", "started_at": START}],
                    "events": events,
                }
            ),
            queue_job_ledger_refresh=AsyncMock(),
        )
    )


@pytest.fixture
def app(store):
    app = FastAPI()
    app.include_router(usage.router)
    app.dependency_overrides[get_storage] = lambda: store
    app.dependency_overrides[require_agent_owner] = lambda: {"user_id": "owner"}
    return app


@pytest.mark.asyncio
async def test_usage_keeps_runtime_and_gateway_counts_in_separate_same_job_fields(app, store):
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get("/v1/agent/jobs/ajob_1/usage")
    assert response.status_code == 200
    body = response.json()
    assert body["ledger"]["totals"]["tokens_in"] == 120
    assert body["ledger"]["totals"]["calls"] == 1
    assert body["ledger"]["finalized"] is False
    assert body["runtime"] == {
        "source": "runtime",
        "input_tokens": 200,
        "output_tokens": 20,
        "cache_read_tokens": 160,
        "normalized_input_tokens": None,
    }
    store.job_usage_inputs.assert_awaited_once_with(job_id="ajob_1")
    store.ledger_rows_for_job.assert_awaited_once_with(job_id="ajob_1")
    store.ledger_rows_for_conversation.assert_awaited_once_with(job_id="ajob_1", user_id="owner")
    store.queue_job_ledger_refresh.assert_not_awaited()
    assert "grant_id" not in response.text
    assert "binding_id" not in response.text


@pytest.mark.asyncio
async def test_refresh_returns_existing_cache_and_only_enqueues_work(app, store):
    store.ledger_rows_for_job.return_value = [
        ledger_row(refreshing=True, queried_at=END - timedelta(seconds=30))
    ]
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post("/v1/agent/jobs/ajob_1/usage/refresh")
    assert response.status_code == 200
    assert response.json()["ledger"]["refreshing"] is True
    assert response.json()["ledger"]["totals"]["tokens_in"] == 120
    store.queue_job_ledger_refresh.assert_awaited_once_with(job_id="ajob_1")


@pytest.mark.asyncio
@pytest.mark.parametrize("method,path", [("get", "/usage"), ("post", "/usage/refresh")])
async def test_another_owner_cannot_read_or_queue_usage(app, store, method, path):
    store.get_job.return_value = {"id": "ajob_other", "user_id": "other"}
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await getattr(client, method)("/v1/agent/jobs/ajob_other" + path)
    assert response.status_code == 404
    store.ledger_rows_for_job.assert_not_awaited()
    store.ledger_rows_for_conversation.assert_not_awaited()
    store.job_usage_inputs.assert_not_awaited()
    store.queue_job_ledger_refresh.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("method,path", [("get", "/usage"), ("post", "/usage/refresh")])
@pytest.mark.parametrize(
    "state,expected",
    [
        ("cancelled", "unavailable"),
        ("failed", "unavailable"),
        ("succeeded", "unavailable"),
        ("queued", "pending"),
    ],
)
async def test_jobs_without_attempts_only_wait_for_usage_while_still_runnable(
    app, store, method, path, state, expected
):
    """An unexecuted terminal job cannot wait forever for a nonexistent collector row."""
    store.get_job.return_value = {"id": "ajob_1", "user_id": "owner", "state": state}
    store.ledger_rows_for_job.return_value = []
    store.job_usage_inputs.return_value = {"job_ids": ["ajob_1"], "attempts": [], "events": []}
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await getattr(client, method)("/v1/agent/jobs/ajob_1" + path)
    assert response.status_code == 200
    body = response.json()
    assert body["ledger"]["status"] == expected
    assert body["ledger"]["attempts"] == []
    assert body["ledger"]["totals"] is None
    assert body["ledger"]["refreshing"] is False
    assert body["ledger"]["next_refresh_at"] is None
    assert body["runtime"] == {
        "source": "runtime",
        "input_tokens": None,
        "output_tokens": None,
        "cache_read_tokens": None,
        "normalized_input_tokens": None,
    }
    store.get_job.assert_awaited_once_with("ajob_1")
    store.ledger_rows_for_job.assert_awaited_once_with(job_id="ajob_1")
    store.job_usage_inputs.assert_awaited_once_with(job_id="ajob_1")


def test_usage_routes_are_available_only_on_the_control_plane():
    public_paths = create_app().openapi()["paths"]
    relay_paths = create_mcp_app().openapi()["paths"]
    for path in ("/v1/agent/jobs/{job_id}/usage", "/v1/agent/jobs/{job_id}/usage/refresh"):
        assert path in public_paths
        assert path not in relay_paths


@pytest.mark.asyncio
async def test_conversation_totals_are_separate_from_the_selected_turn(app, store):
    store.ledger_rows_for_conversation.return_value = [
        ledger_row(job_id="ajob_1"),
        ledger_row(job_id="ajob_2", attempt_id=11, id=2),
        ledger_row(job_id="ajob_old", attempt_id=12, id=None, snapshot=None, queried_at=None),
    ]
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get("/v1/agent/jobs/ajob_1/usage")
    assert response.status_code == 200
    body = response.json()
    assert body["ledger"]["totals"]["calls"] == 1
    conversation = body["conversation"]
    assert conversation["totals"]["calls"] == 2
    assert conversation["totals"]["spent_usd"] == "0.00024690"
    assert conversation["turn_count"] == 3
    assert conversation["incomplete"] is True
    assert "attempts" not in conversation
    assert "grant_id" not in response.text


@pytest.mark.asyncio
async def test_claude_fallback_normalizes_cache_instead_of_changing_input_definition(app, store):
    store.get_job.return_value = {"id": "ajob_1", "user_id": "owner", "runtime": "claude-code"}
    for event in store.job_usage_inputs.return_value["events"]:
        event["payload"]["usage"]["cache_creation_input_tokens"] = 20
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get("/v1/agent/jobs/ajob_1/usage")
    runtime = response.json()["runtime"]
    assert runtime["input_tokens"] == 200
    assert runtime["normalized_input_tokens"] == 400


@pytest.fixture
def request_grants(app, store):
    from tests.unit.test_grant_usage_requests import GRANT, SINCE, UNTIL, page_payload, request_row

    from cloud_agent.api.deps import get_grant_client
    from cloud_agent.grant_usage import GrantUsageRequestPage

    store.get_job.return_value = {
        "id": "ajob_1",
        "user_id": "owner",
        "thread_id": "athr_requests_1",
    }
    store.ledger_rows_for_job.return_value = [
        ledger_row(grant_id=GRANT, window_since=SINCE, window_until=UNTIL)
    ]
    client = SimpleNamespace(
        binding_id="gw_one",
        usage_requests=AsyncMock(
            return_value=GrantUsageRequestPage.model_validate(
                page_payload(rows=[request_row(response_id="msg_1")], response_id="msg_1")
            )
        ),
    )
    app.dependency_overrides[get_grant_client] = lambda: client
    return client


MESSAGE_PATH = "/v1/agent/jobs/ajob_1/attempts/10/messages/msg_1/request"


@pytest.mark.asyncio
async def test_message_request_is_owner_scoped_and_reads_exact_identity(app, store, request_grants):
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get(MESSAGE_PATH)
    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    assert response.json()["status"] == "available"
    assert response.json()["request"]["ttft_ms"] == 240
    assert response.json()["request"]["tokens_out"] == 413
    assert response.json()["request"]["reasoning"] is None
    assert "grant_id" not in response.text
    assert "binding_id" not in response.text
    assert request_grants.usage_requests.await_args.kwargs["response_id"] == "msg_1"
    assert (
        request_grants.usage_requests.await_args.kwargs["external_job_id"]
        == "thread:athr_requests_1"
    )
    store.queue_job_ledger_refresh.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("other_owner", [True, False])
async def test_message_request_refuses_other_owner_or_attempt(
    app, store, request_grants, other_owner
):
    if other_owner:
        store.get_job.return_value["user_id"] = "other"
    else:
        store.ledger_rows_for_job.return_value[0]["attempt_id"] = 11
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get(MESSAGE_PATH)
    assert response.status_code == 404
    request_grants.usage_requests.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "change", [{"binding_id": "old-gateway"}, {"id": None}, {"window_since": None}]
)
async def test_message_request_never_guesses_missing_attribution(
    app, store, request_grants, change
):
    store.ledger_rows_for_job.return_value[0].update(change)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get(MESSAGE_PATH)
    assert response.status_code == 200
    assert response.json()["request"] is None
    request_grants.usage_requests.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "case,expected",
    [("duplicate", "ambiguous"), ("missing", "not_recorded"), ("failed", "unavailable")],
)
async def test_message_request_does_not_select_arbitrary_or_unavailable_rows(
    app, request_grants, case, expected
):
    from cloud_agent.gateway import GatewayError

    page = request_grants.usage_requests.return_value
    if case == "duplicate":
        request_grants.usage_requests.return_value = page.model_copy(
            update={
                "requests": [
                    page.requests[0],
                    page.requests[0].model_copy(update={"request_id": "req_other"}),
                ]
            }
        )
    elif case == "missing":
        request_grants.usage_requests.return_value = page.model_copy(update={"requests": []})
    else:
        request_grants.usage_requests.side_effect = GatewayError(
            "private diagnostics", error_type="usage_unavailable"
        )
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get(MESSAGE_PATH)
    assert response.json()["status"] == expected
    assert response.json()["request"] is None
    assert "private diagnostics" not in response.text
