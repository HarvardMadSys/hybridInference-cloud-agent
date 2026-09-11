"""The request-detail client refuses anything it cannot vouch for.

Every case here is a way a gateway response could be read as more than it is: a
totals-only answer taken for an empty page, a page from another grant, rows
outside the window that was asked for, a second page that is not a
continuation, or a body nobody bounded. None of them may reach a browser.
"""

from __future__ import annotations

import asyncio
import copy
import json
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import pytest

from cloud_agent import grants as grants_module
from cloud_agent.gateway import GatewayError
from cloud_agent.grant_usage import MAX_REQUEST_PAGE_BYTES, GrantUsageRequestPage
from cloud_agent.grants import GrantClient

SINCE = datetime(2026, 9, 11, 3, 20, tzinfo=UTC)
UNTIL = SINCE + timedelta(minutes=1)
TOKEN = "service-credential-never-persisted"
GRANT = "agr_requests_1"
JOB = "thread:athr_requests_1"
TOTALS = {
    "calls": 1,
    "tokens_in": 40086,
    "tokens_out": 413,
    "cache_read": 19712,
    "cache_write": None,
    "reasoning": None,
    "spent_usd": "0.00978569",
}
UNKNOWN_ROWS = {
    "tokens_in": 0,
    "tokens_out": 0,
    "cache_read": 0,
    "cache_write": 1,
    "reasoning": 1,
    "spent_usd": 0,
}


def request_row(**overrides: Any) -> dict[str, Any]:
    """One safe gateway request row, with unreported cache-write and reasoning."""
    row: dict[str, Any] = {
        "request_id": "req_example",
        "request_started_at": SINCE.isoformat(),
        "logged_at": (SINCE + timedelta(seconds=1)).isoformat(),
        "model": "model-alias",
        "served_model_id": "model-version",
        "tokens_in": 40086,
        "tokens_out": 413,
        "cache_read": 19712,
        "cache_write": None,
        "reasoning": None,
        "spent_usd": "0.00978569",
        "ttft_ms": 240,
        "latency_ms": 850,
        "status_code": 200,
        "usage_estimated": False,
    }
    row.update(overrides)
    return row


def page_payload(rows: list[dict[str, Any]] | None = None, **overrides: Any) -> dict[str, Any]:
    """One windowed report carrying request rows."""
    payload: dict[str, Any] = {
        "schema_version": 1,
        "source": "gateway_api_logs",
        "grant_id": GRANT,
        "external_job_id": JOB,
        "attribution": "grant_time_window",
        "window": {
            "since": SINCE.isoformat(),
            "until": UNTIL.isoformat(),
            "basis": "request_started_at",
        },
        "as_of": (UNTIL + timedelta(seconds=1)).isoformat(),
        "finalized": False,
        "totals": copy.deepcopy(TOTALS),
        "unknown_rows": copy.deepcopy(UNKNOWN_ROWS),
        "estimated_calls": 0,
        "requests": [request_row()] if rows is None else rows,
        "next_cursor": None,
    }
    payload.update(overrides)
    return payload


def _client(handler: Any) -> GrantClient:
    return GrantClient(
        "https://gateway.example",
        TOKEN,
        client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )


async def _read(payload: Any, **kwargs: Any) -> GrantUsageRequestPage:
    client = _client(lambda _: httpx.Response(200, json=payload))
    options: dict[str, Any] = {
        "since": SINCE,
        "until": UNTIL,
        "limit": 50,
        "external_job_id": JOB,
    }
    options.update(kwargs)
    return await client.usage_requests(GRANT, **options)


async def test_sends_the_fixed_window_detail_and_limit() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json=page_payload())

    page = await _client(handler).usage_requests(
        GRANT, since=SINCE, until=UNTIL, limit=50, external_job_id=JOB
    )
    assert len(seen) == 1
    assert seen[0].method == "GET"
    assert seen[0].url.path == f"/internal/agent-grants/{GRANT}/usage"
    assert dict(seen[0].url.params) == {
        "since": SINCE.isoformat(),
        "until": UNTIL.isoformat(),
        "detail": "requests",
        "limit": "50",
    }
    assert seen[0].headers["Authorization"] == f"Bearer {TOKEN}"
    assert page.next_cursor is None
    assert len(page.requests) == 1


async def test_continuation_sends_the_gateway_cursor_unchanged() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(
            200,
            json=page_payload(
                [
                    request_row(
                        request_id="req_2",
                        request_started_at=(SINCE + timedelta(seconds=5)).isoformat(),
                    )
                ]
            ),
        )

    await _client(handler).usage_requests(
        GRANT,
        since=SINCE,
        until=UNTIL,
        limit=50,
        external_job_id=JOB,
        cursor="opaque-gateway-cursor",
        after=(SINCE + timedelta(seconds=3), "req_1"),
    )
    assert dict(seen[0].url.params)["cursor"] == "opaque-gateway-cursor"


async def test_reported_zeros_stay_zero_and_unreported_values_stay_null() -> None:
    page = await _read(page_payload([request_row(cache_write=0, ttft_ms=None, status_code=429)]))
    row = page.requests[0]
    assert row.cache_write == 0
    assert row.reasoning is None
    assert row.ttft_ms is None
    assert row.status_code == 429
    assert row.spent_usd == "0.00978569"
    assert row.usage_estimated is False


async def test_allowlist_drops_credentials_prompts_and_raw_metadata() -> None:
    payload = page_payload()
    payload["token"] = TOKEN
    payload["requests"][0]["prompt"] = "private input"
    payload["requests"][0]["metadata"] = {"Authorization": TOKEN}
    payload["requests"][0]["error"] = "private upstream failure"
    page = await _read(payload)
    encoded = page.model_dump_json()
    assert TOKEN not in encoded
    assert "private input" not in encoded
    assert "private upstream failure" not in encoded
    assert set(page.requests[0].model_dump()) == set(request_row())


async def test_a_page_with_more_rows_keeps_the_opaque_gateway_cursor() -> None:
    page = await _read(page_payload([request_row()], next_cursor="opaque-next"))
    assert page.next_cursor == "opaque-next"


async def test_totals_only_v1_is_not_read_as_an_empty_page() -> None:
    payload = page_payload()
    del payload["requests"]
    del payload["next_cursor"]
    with pytest.raises(GatewayError) as raised:
        await _read(payload)
    assert raised.value.error_type == "request_details_unsupported"


async def test_legacy_flat_response_is_not_a_page() -> None:
    payload = {
        "grant_id": GRANT,
        "external_job_id": JOB,
        "spent_usd": 0.2,
        "request_count": 20,
        "tokens_in": 5000,
        "tokens_out": 1000,
    }
    with pytest.raises(GatewayError) as raised:
        await _read(payload)
    assert raised.value.error_type == "usage_unsupported_schema"


@pytest.mark.parametrize(
    ("path", "value"),
    [
        (("requests",), {}),
        (("requests",), None),
        (("next_cursor",), 12),
        (("next_cursor",), "c" * 2049),
        (("next_cursor",), ""),
        (("requests", 0, "usage_estimated"), 0),
        (("requests", 0, "tokens_in"), -1),
        (("requests", 0, "tokens_out"), "413"),
        (("requests", 0, "spent_usd"), 0.00978569),
        (("requests", 0, "spent_usd"), "-0.01"),
        (("requests", 0, "request_started_at"), "2026-09-11T03:20:04"),
        (("requests", 0, "logged_at"), 1789000000),
        (("requests", 0, "request_id"), ""),
        (("requests", 0, "request_id"), "r" * 257),
        (("requests", 0, "model"), "m" * 1025),
        (("requests", 0, "served_model_id"), "m" * 1025),
    ],
)
async def test_malformed_rows_refuse_the_whole_page(path: tuple[Any, ...], value: Any) -> None:
    payload = page_payload()
    target: Any = payload
    for key in path[:-1]:
        target = target[key]
    target[path[-1]] = value
    with pytest.raises(GatewayError) as raised:
        await _read(payload)
    assert raised.value.error_type == "usage_invalid_response"


@pytest.mark.parametrize("field", ["request_id", "request_started_at", "usage_estimated", "model"])
async def test_a_row_missing_a_required_field_refuses_the_page(field: str) -> None:
    payload = page_payload()
    del payload["requests"][0][field]
    with pytest.raises(GatewayError) as raised:
        await _read(payload)
    assert raised.value.error_type == "usage_invalid_response"


async def test_wrong_grant_window_or_job_is_refused_even_with_valid_rows() -> None:
    for mutate in (
        lambda payload: payload.update(grant_id="agr_other"),
        lambda payload: payload["window"].update(until=(UNTIL + timedelta(seconds=1)).isoformat()),
        lambda payload: payload.update(external_job_id="thread:athr_other"),
    ):
        payload = page_payload()
        mutate(payload)
        with pytest.raises(GatewayError) as raised:
            await _read(payload)
        assert raised.value.error_type == "usage_response_mismatch"


@pytest.mark.parametrize(
    ("rows", "kwargs"),
    [
        # Duplicate identity inside one page.
        ([request_row(), request_row()], {}),
        # Rows must be ordered by (start time, request id).
        (
            [
                request_row(
                    request_id="req_b",
                    request_started_at=(SINCE + timedelta(seconds=2)).isoformat(),
                ),
                request_row(
                    request_id="req_a",
                    request_started_at=(SINCE + timedelta(seconds=1)).isoformat(),
                ),
            ],
            {},
        ),
        # The same instant is ordered by request id, and must still ascend.
        (
            [
                request_row(request_id="req_b", request_started_at=SINCE.isoformat()),
                request_row(request_id="req_a", request_started_at=SINCE.isoformat()),
            ],
            {},
        ),
        # A row at the inclusive lower bound is inside the window; before it is not.
        ([request_row(request_started_at=(SINCE - timedelta(microseconds=1)).isoformat())], {}),
        # The exclusive upper bound is outside the window.
        ([request_row(request_started_at=UNTIL.isoformat())], {}),
        # A continuation must be strictly later than the previous page's last row.
        (
            [request_row(request_id="req_1", request_started_at=SINCE.isoformat())],
            {"after": (SINCE, "req_1")},
        ),
        # More rows than the page size that was asked for.
        (
            [
                request_row(request_id="req_1", request_started_at=SINCE.isoformat()),
                request_row(
                    request_id="req_2",
                    request_started_at=(SINCE + timedelta(seconds=1)).isoformat(),
                ),
            ],
            {"limit": 1},
        ),
    ],
)
async def test_inconsistent_pages_are_refused_whole(
    rows: list[dict[str, Any]], kwargs: dict[str, Any]
) -> None:
    with pytest.raises(GatewayError) as raised:
        await _read(page_payload(rows), **kwargs)
    assert raised.value.error_type == "usage_invalid_response"


async def test_an_empty_page_cannot_claim_another_page() -> None:
    with pytest.raises(GatewayError) as raised:
        await _read(page_payload([], next_cursor="more"))
    assert raised.value.error_type == "usage_invalid_response"


async def test_a_cursor_cannot_be_echoed_back_as_progress() -> None:
    with pytest.raises(GatewayError) as raised:
        await _read(
            page_payload([request_row()], next_cursor="same-cursor"),
            cursor="same-cursor",
        )
    assert raised.value.error_type == "usage_invalid_response"


async def test_an_oversized_body_is_refused_before_it_is_parsed() -> None:
    oversize = b'{"pad": "' + b"a" * MAX_REQUEST_PAGE_BYTES + b'"}'

    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=oversize, headers={"Content-Type": "application/json"})

    with pytest.raises(GatewayError) as raised:
        await _client(handler).usage_requests(
            GRANT, since=SINCE, until=UNTIL, limit=50, external_job_id=JOB
        )
    assert raised.value.error_type == "usage_response_too_large"


class _SlowBody(httpx.AsyncByteStream):
    """A body that keeps trickling: every operation succeeds, the call does not."""

    def __init__(self, chunks: list[bytes], delay: float) -> None:
        self._chunks = chunks
        self._delay = delay

    async def __aiter__(self):  # type: ignore[override]
        for chunk in self._chunks:
            yield chunk
            await asyncio.sleep(self._delay)


async def test_a_slow_drip_cannot_outlive_the_whole_deadline(monkeypatch) -> None:
    """A per-operation timeout never fires on a body that always makes progress."""
    monkeypatch.setattr(grants_module, "_REQUEST_PAGE_DEADLINE_S", 0.05)
    body = json.dumps(page_payload()).encode()
    half = len(body) // 2

    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(200, stream=_SlowBody([body[:half], body[half:]], 0.2))

    with pytest.raises(GatewayError) as raised:
        await _client(handler).usage_requests(
            GRANT, since=SINCE, until=UNTIL, limit=50, external_job_id=JOB
        )
    assert raised.value.error_type == "usage_unavailable"


async def test_the_deadline_covers_waiting_for_a_slot(monkeypatch) -> None:
    """Queueing behind other administrators is time this call also promised."""
    in_flight = 0
    release = asyncio.Event()

    async def handler(_: httpx.Request) -> httpx.Response:
        nonlocal in_flight
        in_flight += 1
        await release.wait()
        in_flight -= 1
        return httpx.Response(200, json=page_payload([]))

    client = _client(handler)
    held = [
        asyncio.create_task(
            client.usage_requests(GRANT, since=SINCE, until=UNTIL, limit=50, external_job_id=JOB)
        )
        for _ in range(4)
    ]
    for _ in range(200):
        if in_flight == 4:
            break
        await asyncio.sleep(0.01)
    assert in_flight == 4

    monkeypatch.setattr(grants_module, "_REQUEST_PAGE_DEADLINE_S", 0.05)
    with pytest.raises(GatewayError) as raised:
        await client.usage_requests(GRANT, since=SINCE, until=UNTIL, limit=50, external_job_id=JOB)
    assert raised.value.error_type == "usage_unavailable"

    release.set()
    await asyncio.gather(*held)


@pytest.mark.parametrize(
    ("status", "error_type"),
    [
        (401, "invalid_dispatch_token"),
        (404, "unknown_grant"),
        (422, "invalid_cursor"),
        (503, "ledger_unavailable"),
    ],
)
async def test_refusals_keep_the_status_and_machine_reason_only(
    status: int, error_type: str
) -> None:
    client = _client(
        lambda _: httpx.Response(
            status, json={"error": {"type": error_type, "message": f"secret {TOKEN}"}}
        )
    )
    with pytest.raises(GatewayError) as raised:
        await client.usage_requests(GRANT, since=SINCE, until=UNTIL, limit=50, external_job_id=JOB)
    assert raised.value.status_code == status
    assert raised.value.error_type == error_type
    assert TOKEN not in str(raised.value)


async def test_unreadable_refusal_body_keeps_a_stable_reason() -> None:
    client = _client(lambda _: httpx.Response(503, content=b"<html>maintenance</html>"))
    with pytest.raises(GatewayError) as raised:
        await client.usage_requests(GRANT, since=SINCE, until=UNTIL, limit=50, external_job_id=JOB)
    assert raised.value.error_type == "usage_http_error"


@pytest.mark.parametrize(
    ("since", "until", "limit"),
    [
        (SINCE.replace(tzinfo=None), UNTIL, 50),
        (UNTIL, SINCE, 50),
        (SINCE, UNTIL, 0),
        (SINCE, UNTIL, 501),
        (SINCE, UNTIL, True),
    ],
)
async def test_an_invalid_local_query_never_reaches_the_gateway(
    since: Any, until: Any, limit: Any
) -> None:
    def handler(_: httpx.Request) -> httpx.Response:
        pytest.fail("an invalid request-page query reached the gateway")

    with pytest.raises(GatewayError) as raised:
        await _client(handler).usage_requests(
            GRANT, since=since, until=until, limit=limit, external_job_id=JOB
        )
    assert raised.value.error_type == "usage_query_invalid"


async def test_network_failure_and_unconfigured_deployment_have_distinct_reasons(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def handler(_: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError(f"debug header: {TOKEN}")

    with pytest.raises(GatewayError) as unreachable:
        await _client(handler).usage_requests(
            GRANT, since=SINCE, until=UNTIL, limit=50, external_job_id=JOB
        )
    assert unreachable.value.error_type == "usage_unavailable"
    assert TOKEN not in str(unreachable.value)

    monkeypatch.delenv("GATEWAY_BASE_URL", raising=False)
    monkeypatch.delenv("GATEWAY_GRANT_DISPATCH_TOKEN", raising=False)
    with pytest.raises(GatewayError) as unconfigured:
        await GrantClient().usage_requests(
            GRANT, since=SINCE, until=UNTIL, limit=50, external_job_id=JOB
        )
    assert unconfigured.value.error_type == "usage_unconfigured"


async def test_request_pages_are_capped_per_process() -> None:
    """A panel refresh cannot crowd out grant renewal on a shared gateway."""
    in_flight = 0
    peak = 0
    release = asyncio.Event()

    async def handler(_: httpx.Request) -> httpx.Response:
        nonlocal in_flight, peak
        in_flight += 1
        peak = max(peak, in_flight)
        await release.wait()
        in_flight -= 1
        return httpx.Response(200, json=page_payload())

    client = _client(handler)
    tasks = [
        asyncio.create_task(
            client.usage_requests(GRANT, since=SINCE, until=UNTIL, limit=50, external_job_id=JOB)
        )
        for _ in range(5)
    ]
    for _ in range(200):
        if in_flight == 4:
            break
        await asyncio.sleep(0.01)
    assert peak == 4, "the fifth request must wait for a slot"
    assert not tasks[4].done()
    release.set()
    await asyncio.gather(*tasks)
    assert peak == 4
    assert all(task.result().requests for task in tasks)


async def test_the_page_never_asks_for_more_than_the_service_will_show() -> None:
    """The gateway accepts 500; this service asks for what a browser can read."""
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json=page_payload([]))

    page = await _client(handler).usage_requests(
        GRANT, since=SINCE, until=UNTIL, limit=100, external_job_id=JOB
    )
    assert dict(seen[0].url.params)["limit"] == "100"
    assert page.requests == []
    assert json.loads(page.model_dump_json())["next_cursor"] is None


async def test_identity_text_is_preserved_exactly_including_non_ascii() -> None:
    page = await _read(
        page_payload(
            [
                request_row(
                    request_id="req_😀_ident",
                    model="模型-别名",
                    served_model_id="模型-版本",
                )
            ]
        )
    )
    assert page.requests[0].request_id == "req_😀_ident"
    assert page.requests[0].model == "模型-别名"
    assert page.requests[0].served_model_id == "模型-版本"


async def test_counts_a_browser_cannot_hold_are_refused_rather_than_rounded() -> None:
    """The gateway's columns are 32-bit today; a widening must fail loudly."""
    with pytest.raises(GatewayError) as raised:
        await _read(page_payload([request_row(tokens_in=2**53)]))
    assert raised.value.error_type == "usage_invalid_response"
