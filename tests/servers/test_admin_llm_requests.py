"""Admin per-request LLM detail: authorization, attribution and refusals.

The gateway is a second service with its own availability, so these tests hold
two lines at once: nothing reaches it without a current admin and a stored
reference that still matches, and every way it can fail becomes a stable
reason a browser can act on instead of an empty page.
"""

from __future__ import annotations

import base64
import json
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from cloud_agent import identity
from cloud_agent.admin_observability import (
    MAX_ADMIN_RESPONSE_BYTES,
    MAX_REQUEST_CURSOR_BYTES,
    REQUEST_CURSOR_TTL,
    AdminLlmRequest,
    AdminLlmRequestPage,
    AdminLlmRequestWindow,
    LlmRequestCursor,
    cursor_scope,
    encode_llm_request_cursor,
    request_source_fingerprint,
    response_body_bytes,
)
from cloud_agent.api import admin_llm_requests
from cloud_agent.api.deps import get_gateway_client, get_grant_client
from cloud_agent.app import create_app, install_admin_no_store
from cloud_agent.gateway import GatewayError
from cloud_agent.grant_usage import GrantUsageRequestPage
from tests.servers.test_owner_revalidation import _AccessGateway
from tests.unit.test_grant_usage_requests import (
    GRANT,
    SINCE,
    UNTIL,
    page_payload,
    request_row,
)

ADMIN = {"Authorization": "Bearer admin-llm-requests-test"}
PREFIX = "/admin/agent/runs/ajob_one"
SOURCES = PREFIX + "/llm-request-sources"
REQUESTS = PREFIX + "/llm-requests"
BINDING = "gw_binding_one"
JOB_KEY = "thread:athr_one"
SESSION_SECRET = "admin-llm-requests-session-test-secret-value"


def source_row(**overrides: Any) -> dict[str, Any]:
    """One stored reference as the admin projection returns it."""
    row: dict[str, Any] = {
        "attempt_id": 10,
        "attempt_no": 1,
        "attempt_status": "finished",
        "started_at": SINCE,
        "finished_at": UNTIL,
        "thread_id": "athr_one",
        "ledger_id": 42,
        "grant_id": GRANT,
        "binding_id": BINDING,
        "residency_generation": 1,
        "window_since": SINCE,
        "window_until": UNTIL,
        "window_open": False,
        "ledger_key": 42,
    }
    row.update(overrides)
    return row


def gateway_page(
    rows: list[dict[str, Any]] | None = None, **overrides: Any
) -> GrantUsageRequestPage:
    return GrantUsageRequestPage.model_validate(page_payload(rows, **overrides))


class _FakeGrants:
    """Stands in for the gateway client: identity plus one recorded call."""

    def __init__(self, binding_id: str = BINDING) -> None:
        self.binding_id = binding_id
        self.usage_requests = AsyncMock(return_value=gateway_page())


@pytest.fixture
def setup(monkeypatch):
    monkeypatch.setenv("ADMIN_TOKEN", "admin-llm-requests-test")
    monkeypatch.setenv(identity.ENV_SESSION_SECRET, SESSION_SECRET)
    monkeypatch.setenv(identity.ENV_ISSUER, "https://gateway.example")
    queries = SimpleNamespace(
        get_run=AsyncMock(return_value={"id": "ajob_one", "thread_id": "athr_one"}),
        llm_request_sources=AsyncMock(return_value=([source_row()], False)),
        llm_request_source=AsyncMock(return_value=source_row()),
    )
    grants = _FakeGrants()
    gateway = _AccessGateway(["agent.use", "agent.admin"])
    app = FastAPI()
    app.include_router(admin_llm_requests.router)
    install_admin_no_store(app)
    app.dependency_overrides[admin_llm_requests.get_admin_run_queries] = lambda: queries
    app.dependency_overrides[get_grant_client] = lambda: grants
    app.dependency_overrides[get_gateway_client] = lambda: gateway
    return app, queries, grants, gateway


async def _get(app: FastAPI, path: str, **kwargs: Any):
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        return await client.get(path, headers=ADMIN, **kwargs)


@pytest.mark.parametrize("path", [SOURCES, REQUESTS + "?source_id=42"])
@pytest.mark.parametrize("auth", ["missing", "invalid", "revoked"])
async def test_both_reads_require_a_current_admin(setup, path, auth):
    app, queries, grants, gateway = setup
    gateway.permissions = ["agent.use"]
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        headers = {}
        if auth == "invalid":
            headers = {"Authorization": "Bearer not-an-admin-token"}
        elif auth == "revoked":
            client.cookies.set(
                identity.SESSION_COOKIE, identity.mint_session(user_id="admin-person")
            )
        response = await client.get(path, headers=headers)
    assert response.status_code == (403 if auth == "revoked" else 401)
    queries.get_run.assert_not_awaited()
    queries.llm_request_sources.assert_not_awaited()
    queries.llm_request_source.assert_not_awaited()
    grants.usage_requests.assert_not_awaited()


@pytest.mark.parametrize(
    "path",
    [
        SOURCES + "?since=2026-09-11T03:20:00Z",
        SOURCES + "?grant_id=agr_other",
        REQUESTS + "?source_id=42&until=2026-09-11T03:21:00Z",
        REQUESTS + "?source_id=42&binding_id=gw_other",
        REQUESTS + "?source_id=42&sort=spent_usd",
        REQUESTS + "?source_id=42&model=other",
        REQUESTS + "?source_id=42&status_code=429",
    ],
)
async def test_selectors_this_service_owns_are_refused_not_ignored(setup, path):
    app, queries, grants, _ = setup
    response = await _get(app, path)
    assert response.status_code == 422
    assert response.json()["detail"]["error"]["type"] == "unexpected_query_parameter"
    queries.llm_request_source.assert_not_awaited()
    queries.llm_request_sources.assert_not_awaited()
    grants.usage_requests.assert_not_awaited()


async def test_the_source_list_keeps_unattributed_executions_and_says_why(setup):
    app, queries, grants, _ = setup
    queries.llm_request_sources.return_value = (
        [
            source_row(),
            source_row(
                attempt_id=11,
                attempt_no=2,
                ledger_id=None,
                grant_id=None,
                binding_id=None,
                window_since=None,
                window_until=None,
                ledger_key=2**63 - 1,
            ),
            source_row(attempt_id=12, attempt_no=3, binding_id="gw_other", ledger_id=43),
        ],
        False,
    )
    response = await _get(app, SOURCES)
    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    body = response.json()
    assert body["job_id"] == "ajob_one"
    assert body["next_cursor"] is None
    assert body["reason"] is None
    first, missing, foreign = body["sources"]
    assert first["source_id"] == "42"
    assert first["availability"] == "queryable"
    assert first["window_state"] == "closed"
    assert datetime.fromisoformat(first["window_since"]) == SINCE
    assert missing == {
        "source_id": None,
        "attempt_id": 11,
        "attempt_no": 2,
        "window_since": None,
        "window_until": None,
        "window_state": "unknown",
        "availability": "unavailable",
        "reason": "attribution_unavailable",
    }
    assert foreign["availability"] == "unavailable"
    assert foreign["reason"] == "binding_changed"
    grants.usage_requests.assert_not_awaited()


async def test_a_run_without_executions_says_not_started(setup):
    app, queries, _, _ = setup
    queries.llm_request_sources.return_value = ([], False)
    body = (await _get(app, SOURCES)).json()
    assert body["sources"] == []
    assert body["reason"] == "not_started"
    assert body["next_cursor"] is None


async def test_a_missing_execution_filter_is_not_an_empty_page(setup):
    app, queries, _, _ = setup
    queries.llm_request_sources.return_value = ([], False)
    response = await _get(app, SOURCES + "?attempt_id=99")
    assert response.status_code == 404


async def test_the_source_list_pages_on_its_own_keyset(setup):
    app, queries, _, _ = setup
    queries.llm_request_sources.return_value = ([source_row()], True)
    first = (await _get(app, SOURCES)).json()
    assert first["next_cursor"]

    queries.llm_request_sources.return_value = (
        [source_row(attempt_id=11, attempt_no=2, ledger_id=43, ledger_key=43)],
        False,
    )
    response = await _get(app, SOURCES, params={"cursor": first["next_cursor"]})
    assert response.status_code == 200
    args = queries.llm_request_sources.await_args.kwargs
    assert args["cursor"].attempt_no == 1
    assert args["cursor"].attempt_id == 10
    assert args["cursor"].ledger_key == 42


async def test_a_ledger_id_sentinel_keeps_unattributed_rows_reachable(setup):
    """The sentinel is what makes a missing reference sortable, not skippable."""
    app, queries, _, _ = setup
    queries.llm_request_sources.return_value = (
        [source_row(ledger_id=None, ledger_key=2**63 - 1)],
        True,
    )
    body = (await _get(app, SOURCES)).json()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        await client.get(SOURCES, headers=ADMIN, params={"cursor": body["next_cursor"]})
    assert queries.llm_request_sources.await_args.kwargs["cursor"].ledger_key == 2**63 - 1


async def test_a_source_list_cursor_is_bound_to_the_job_and_filter(setup):
    app, queries, _, _ = setup
    other_scope = cursor_scope(["ajob_other", None])
    forged = base64.urlsafe_b64encode(
        json.dumps(
            {
                "version": 1,
                "scope": other_scope,
                "attempt_no": 1,
                "attempt_id": 10,
                "ledger_key": 42,
            }
        ).encode()
    ).decode()
    response = await _get(app, SOURCES, params={"cursor": forged})
    assert response.status_code == 422
    queries.get_run.assert_not_awaited()


async def test_a_source_from_another_run_is_a_404_before_any_gateway_call(setup):
    app, queries, grants, _ = setup
    queries.llm_request_source.return_value = None
    response = await _get(app, REQUESTS + "?source_id=99")
    assert response.status_code == 404
    assert response.headers["cache-control"] == "no-store"
    assert response.json()["detail"]["error"]["type"] == "request_source_not_found"
    grants.usage_requests.assert_not_awaited()


async def test_history_from_another_installation_is_refused_locally(setup):
    app, queries, grants, _ = setup
    queries.llm_request_source.return_value = source_row(binding_id="gw_other")
    response = await _get(app, REQUESTS + "?source_id=42")
    assert response.status_code == 409
    assert response.json()["detail"]["error"]["type"] == "binding_changed"
    grants.usage_requests.assert_not_awaited()


async def test_an_empty_window_is_refused_before_the_gateway(setup):
    app, queries, grants, _ = setup
    queries.llm_request_source.return_value = source_row(window_until=SINCE)
    response = await _get(app, REQUESTS + "?source_id=42")
    assert response.status_code == 422
    assert response.json()["detail"]["error"]["type"] == "request_window_unavailable"
    grants.usage_requests.assert_not_awaited()


async def test_an_unattributable_reference_is_not_queried_on_a_guess(setup):
    app, queries, grants, _ = setup
    queries.llm_request_source.return_value = source_row(thread_id=None)
    response = await _get(app, REQUESTS + "?source_id=42")
    assert response.status_code == 422
    assert response.json()["detail"]["error"]["type"] == "request_source_unavailable"
    grants.usage_requests.assert_not_awaited()


async def test_a_page_uses_the_stored_reference_and_adds_nothing(setup):
    app, _queries, grants, _ = setup
    grants.usage_requests.return_value = gateway_page(next_cursor="gw-next")
    response = await _get(app, REQUESTS + "?source_id=42")
    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    args = grants.usage_requests.await_args
    assert args.args == (GRANT,)
    assert args.kwargs["since"] == SINCE
    assert args.kwargs["until"] == UNTIL
    assert args.kwargs["limit"] == 50
    assert args.kwargs["external_job_id"] == JOB_KEY
    assert args.kwargs["cursor"] is None
    assert args.kwargs["after"] is None
    body = response.json()
    assert body["job_id"] == "ajob_one"
    assert body["source_id"] == "42"
    assert body["attempt_no"] == 1
    assert body["window"]["basis"] == "request_started_at"
    assert datetime.fromisoformat(body["window"]["since"]) == SINCE
    assert datetime.fromisoformat(body["window"]["until"]) == UNTIL
    assert body["source"] == "gateway_api_logs"
    assert body["pagination_consistency"] == "live_keyset"
    assert body["finalized"] is False
    assert body["next_cursor"]
    assert body["requests"][0]["spent_usd"] == "0.00978569"
    # The gateway's window totals are not a page subtotal and must not appear.
    assert "totals" not in body and "unknown_rows" not in body and "estimated_calls" not in body
    assert GRANT not in response.text
    assert BINDING not in response.text
    assert "Bearer" not in response.text


async def test_a_continuation_repeats_the_fixed_window_and_last_row(setup):
    app, _queries, grants, _ = setup
    grants.usage_requests.return_value = gateway_page(next_cursor="gw-next")
    first = (await _get(app, REQUESTS + "?source_id=42")).json()
    last = first["requests"][-1]
    grants.usage_requests.return_value = gateway_page(
        [
            request_row(
                request_id="req_2",
                request_started_at=(SINCE + timedelta(seconds=5)).isoformat(),
            )
        ]
    )
    response = await _get(app, REQUESTS, params={"source_id": 42, "cursor": first["next_cursor"]})
    assert response.status_code == 200
    args = grants.usage_requests.await_args.kwargs
    assert args["cursor"] == "gw-next"
    assert args["after"] == (datetime.fromisoformat(last["request_started_at"]), last["request_id"])
    assert args["since"] == SINCE and args["until"] == UNTIL
    assert response.json()["next_cursor"] is None


async def test_a_window_that_was_clamped_requires_a_restart(setup):
    app, queries, grants, _ = setup
    grants.usage_requests.return_value = gateway_page(next_cursor="gw-next")
    first = (await _get(app, REQUESTS + "?source_id=42")).json()
    grants.usage_requests.reset_mock()
    # A later execution began and shortened this attempt's effective window.
    queries.llm_request_source.return_value = source_row(window_until=UNTIL - timedelta(seconds=30))
    response = await _get(app, REQUESTS, params={"source_id": 42, "cursor": first["next_cursor"]})
    assert response.status_code == 409
    assert response.json()["detail"]["error"]["type"] == "request_window_changed"
    grants.usage_requests.assert_not_awaited()


async def test_a_replaced_reference_requires_a_restart(setup):
    app, queries, grants, _ = setup
    grants.usage_requests.return_value = gateway_page(next_cursor="gw-next")
    first = (await _get(app, REQUESTS + "?source_id=42")).json()
    grants.usage_requests.reset_mock()
    queries.llm_request_source.return_value = source_row(grant_id="agr_replaced")
    response = await _get(app, REQUESTS, params={"source_id": 42, "cursor": first["next_cursor"]})
    assert response.status_code == 409
    assert response.json()["detail"]["error"]["type"] == "request_window_changed"
    grants.usage_requests.assert_not_awaited()


async def test_an_expired_continuation_requires_a_restart(setup):
    app, _queries, grants, _ = setup
    row = source_row()
    expired = encode_llm_request_cursor(
        LlmRequestCursor(
            admin="admin-token",
            job_id="ajob_one",
            source_id="42",
            fingerprint=request_source_fingerprint(row),
            since=SINCE,
            until=UNTIL,
            limit=50,
            request_started_at=SINCE,
            request_id="req_example",
            gateway_cursor="gw-next",
        ),
        issued_at=datetime.now(UTC) - REQUEST_CURSOR_TTL - timedelta(minutes=1),
    )
    response = await _get(app, REQUESTS, params={"source_id": 42, "cursor": expired})
    assert response.status_code == 409
    assert response.json()["detail"]["error"]["type"] == "request_cursor_expired"
    grants.usage_requests.assert_not_awaited()


async def test_a_continuation_is_not_transferable_between_admins_or_runs(setup):
    app, queries, grants, _ = setup
    grants.usage_requests.return_value = gateway_page(next_cursor="gw-next")
    first = (await _get(app, REQUESTS + "?source_id=42")).json()
    grants.usage_requests.reset_mock()
    for params in (
        {"source_id": 43, "cursor": first["next_cursor"]},
        {"source_id": 42, "cursor": first["next_cursor"], "limit": 10},
    ):
        queries.llm_request_source.return_value = source_row(ledger_id=params["source_id"])
        response = await _get(app, REQUESTS, params=params)
        assert response.status_code == 422, params
    grants.usage_requests.assert_not_awaited()


async def test_a_cursor_minted_for_another_admin_is_refused(setup):
    app, _queries, grants, _ = setup
    row = source_row()
    token = encode_llm_request_cursor(
        LlmRequestCursor(
            admin="admin:someone-else",
            job_id="ajob_one",
            source_id="42",
            fingerprint=request_source_fingerprint(row),
            since=SINCE,
            until=UNTIL,
            limit=50,
            request_started_at=SINCE,
            request_id="req_example",
            gateway_cursor="gw-next",
        )
    )
    response = await _get(app, REQUESTS, params={"source_id": 42, "cursor": token})
    assert response.status_code == 422
    grants.usage_requests.assert_not_awaited()


async def test_an_oversized_continuation_is_refused_before_it_is_parsed(setup):
    app, _, grants, _ = setup
    response = await _get(
        app, REQUESTS, params={"source_id": 42, "cursor": "c" * (MAX_REQUEST_CURSOR_BYTES + 1)}
    )
    assert response.status_code == 422
    grants.usage_requests.assert_not_awaited()


@pytest.mark.parametrize(
    ("error_type", "status", "expected"),
    [
        ("unknown_grant", 404, "request_details_unavailable"),
        ("ledger_unavailable", 503, "request_details_unavailable"),
        ("invalid_dispatch_token", 401, "request_details_unavailable"),
        ("usage_unsupported_schema", 200, "request_details_unsupported"),
        ("request_details_unsupported", 200, "request_details_unsupported"),
        ("usage_response_too_large", 200, "request_page_too_large"),
        ("usage_invalid_response", 200, "request_page_invalid"),
        ("usage_response_mismatch", 200, "request_page_invalid"),
        ("invalid_cursor", 422, "request_cursor_expired"),
    ],
)
async def test_gateway_failures_become_stable_reasons(setup, error_type, status, expected):
    app, _, grants, _ = setup
    grants.usage_requests.side_effect = GatewayError(
        f"upstream said secret {GRANT}", status_code=status, error_type=error_type
    )
    response = await _get(app, REQUESTS + "?source_id=42")
    assert response.json()["detail"]["error"]["type"] == expected
    assert (
        response.status_code
        == {
            "request_details_unavailable": 503,
            "request_details_unsupported": 503,
            "request_page_too_large": 502,
            "request_page_invalid": 502,
            "request_cursor_expired": 409,
        }[expected]
    )
    assert GRANT not in response.text
    assert "upstream said" not in response.text


async def test_reading_detail_touches_the_gateway_once_and_only_reads(setup):
    app, queries, grants, _ = setup
    await _get(app, SOURCES)
    grants.usage_requests.assert_not_awaited()
    await _get(app, REQUESTS + "?source_id=42")
    grants.usage_requests.assert_awaited_once()
    queries.get_run.assert_awaited_once_with("ajob_one")


async def test_a_browser_session_is_recorded_in_the_cursor_it_mints(monkeypatch):
    """Two administrators cannot hand continuations to each other."""
    monkeypatch.setenv("ADMIN_TOKEN", "")
    monkeypatch.setenv(identity.ENV_SESSION_SECRET, SESSION_SECRET)
    monkeypatch.setenv(identity.ENV_ISSUER, "https://gateway.example")
    queries = SimpleNamespace(
        get_run=AsyncMock(return_value={"id": "ajob_one"}),
        llm_request_sources=AsyncMock(return_value=([source_row()], False)),
        llm_request_source=AsyncMock(return_value=source_row()),
    )
    grants = _FakeGrants()
    grants.usage_requests.return_value = gateway_page(next_cursor="gw-next")
    gateway = _AccessGateway(["agent.use", "agent.admin"])
    app = FastAPI()
    app.include_router(admin_llm_requests.router)
    install_admin_no_store(app)
    app.dependency_overrides[admin_llm_requests.get_admin_run_queries] = lambda: queries
    app.dependency_overrides[get_grant_client] = lambda: grants
    app.dependency_overrides[get_gateway_client] = lambda: gateway
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        client.cookies.set(identity.SESSION_COOKIE, identity.mint_session(user_id="admin-person"))
        first = await client.get(REQUESTS, params={"source_id": 42})
        assert first.status_code == 200
        cursor = first.json()["next_cursor"]
        body = json.loads(
            base64.urlsafe_b64decode(cursor.split(".")[1] + "=" * (-len(cursor.split(".")[1]) % 4))
        )
        assert body["admin"] == "admin:admin-person"
        grants.usage_requests.reset_mock()
        ours = await client.get(REQUESTS, params={"source_id": 42, "cursor": cursor})
        assert ours.status_code == 200
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        client.cookies.set(identity.SESSION_COOKIE, identity.mint_session(user_id="other-admin"))
        theirs = await client.get(REQUESTS, params={"source_id": 42, "cursor": cursor})
    assert theirs.status_code == 422


async def test_refusals_the_framework_generates_are_not_storable(monkeypatch):
    """A validation or auth refusal is built by FastAPI, not by this module."""
    monkeypatch.setenv("ADMIN_TOKEN", "admin-llm-requests-test")
    app = create_app()
    app.dependency_overrides[admin_llm_requests.get_admin_run_queries] = lambda: SimpleNamespace()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        anonymous = await client.get(REQUESTS + "?source_id=42")
        malformed = await client.get(REQUESTS + "?source_id=not-a-number", headers=ADMIN)
        wrong_shape = await client.get(REQUESTS + "?source_id=42&limit=0", headers=ADMIN)
    assert anonymous.status_code == 401
    assert malformed.status_code == 422
    assert wrong_shape.status_code == 422
    for response in (anonymous, malformed, wrong_shape):
        assert response.headers["cache-control"] == "no-store"


def oversized_page(rows: int, cursor: str) -> GrantUsageRequestPage:
    """A page built from legal field widths, sized by how many rows it carries."""
    return gateway_page(
        [
            request_row(
                request_id=f"req_{index:04d}" + "x" * 247,
                request_started_at=(SINCE + timedelta(milliseconds=index)).isoformat(),
                model="m" * 1024,
                served_model_id="s" * 1024,
                spent_usd="1" * 128,
            )
            for index in range(rows)
        ],
        next_cursor=cursor,
    )


def forwarded(page: GrantUsageRequestPage, cursor: str | None) -> AdminLlmRequestPage:
    """The response this service would build for a gateway page."""
    return AdminLlmRequestPage(
        job_id="ajob_one",
        source_id="42",
        attempt_id=10,
        attempt_no=1,
        window=AdminLlmRequestWindow(since=SINCE, until=UNTIL),
        fetched_at=UNTIL,
        gateway_as_of=UNTIL,
        requests=[AdminLlmRequest.model_validate(row.model_dump()) for row in page.requests],
        next_cursor=cursor,
    )


def minted(page: GrantUsageRequestPage, *, limit: int) -> str:
    """The exact continuation the route would sign for this page."""
    last = page.requests[-1]
    return encode_llm_request_cursor(
        LlmRequestCursor(
            admin="admin-token",
            job_id="ajob_one",
            source_id="42",
            fingerprint=request_source_fingerprint(source_row()),
            since=SINCE,
            until=UNTIL,
            limit=limit,
            request_started_at=last.request_started_at,
            request_id=last.request_id,
            gateway_cursor=page.next_cursor,
        )
    )


async def test_the_final_response_is_bounded_after_the_cursor_is_attached(setup):
    """The ceiling is over what the browser receives, continuation included."""
    app, _queries, grants, _ = setup
    page = None
    for rows in range(100, 0, -1):
        candidate = oversized_page(rows, "c" * 2048)
        if (
            response_body_bytes(forwarded(candidate, None))
            < MAX_ADMIN_RESPONSE_BYTES
            <= response_body_bytes(forwarded(candidate, minted(candidate, limit=100)))
        ):
            page = candidate
            break
    assert page is not None, "no page straddles the response ceiling"
    grants.usage_requests.return_value = page
    response = await _get(app, REQUESTS + "?source_id=42&limit=100")
    assert response.status_code == 502
    assert response.json()["detail"]["error"]["type"] == "request_page_too_large"
    # The refusal itself is small: an over-limit page is never echoed back.
    assert len(response.content) < 4096


async def test_a_page_that_fits_is_returned_under_the_ceiling(setup):
    app, _queries, grants, _ = setup
    grants.usage_requests.return_value = oversized_page(40, "c" * 200)
    response = await _get(app, REQUESTS + "?source_id=42&limit=100")
    assert response.status_code == 200
    assert len(response.content) <= MAX_ADMIN_RESPONSE_BYTES
    assert response.json()["next_cursor"]


async def test_a_continuation_too_large_to_resume_is_not_signed(setup):
    app, _queries, grants, _ = setup
    grants.usage_requests.return_value = gateway_page(
        [request_row(request_id="😀" * 256)], next_cursor="c" * 2048
    )
    response = await _get(app, REQUESTS + "?source_id=42")
    assert response.status_code == 502
    assert response.json()["detail"]["error"]["type"] == "request_page_too_large"
    assert "next_cursor" not in response.text
