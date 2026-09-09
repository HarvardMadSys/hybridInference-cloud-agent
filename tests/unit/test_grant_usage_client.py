"""The HTTP ledger client never mistakes unavailable or partial usage for zero."""

from __future__ import annotations

import copy
import json
from datetime import UTC, datetime, timedelta, timezone
from typing import TYPE_CHECKING, Any

import httpx
import pytest

from cloud_agent.gateway import GatewayError
from cloud_agent.grants import GrantClient

if TYPE_CHECKING:
    from cloud_agent.grant_usage import GrantUsageReport

SINCE = datetime(2026, 9, 9, 9, 0, tzinfo=UTC)
UNTIL = SINCE + timedelta(minutes=1)
TOKEN = "service-credential-never-persisted"
REPORT: dict[str, Any] = {
    "schema_version": 1,
    "source": "gateway_api_logs",
    "grant_id": "agr_ledger_1",
    "external_job_id": "thread:athr_ledger_1",
    "attribution": "grant_time_window",
    "window": {
        "since": SINCE.isoformat(),
        "until": UNTIL.isoformat(),
        "basis": "request_started_at",
    },
    "as_of": (UNTIL + timedelta(seconds=3)).isoformat(),
    "finalized": False,
    "totals": {
        "calls": 3,
        "tokens_in": 12500,
        "tokens_out": 320,
        "cache_read": 12000,
        "cache_write": 0,
        "reasoning": None,
        "spent_usd": "0.00123456",
    },
    "unknown_rows": {
        "tokens_in": 1,
        "tokens_out": 0,
        "cache_read": 1,
        "cache_write": 0,
        "reasoning": 3,
        "spent_usd": 1,
    },
    "estimated_calls": 1,
}


def _client(handler, **kwargs: Any) -> GrantClient:
    return GrantClient(
        "https://gateway.example",
        TOKEN,
        client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
        **kwargs,
    )


async def _read(payload: Any) -> GrantUsageReport:
    client = _client(lambda _: httpx.Response(200, json=payload))
    return await client.usage("agr_ledger_1", since=SINCE, until=UNTIL)


async def test_sends_explicit_normalized_window_and_service_credential() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json=REPORT)

    utc_plus_eight = timezone(timedelta(hours=8))
    report = await _client(handler).usage(
        "agr_ledger_1",
        since=SINCE.astimezone(utc_plus_eight),
        until=UNTIL.astimezone(utc_plus_eight),
    )
    assert len(seen) == 1
    assert seen[0].method == "GET"
    assert seen[0].url.path == "/internal/agent-grants/agr_ledger_1/usage"
    assert dict(seen[0].url.params) == {
        "since": SINCE.isoformat(),
        "until": UNTIL.isoformat(),
        "detail": "totals",
    }
    assert seen[0].headers["Authorization"] == f"Bearer {TOKEN}"
    assert not seen[0].content
    assert report.window.since == SINCE
    assert report.window.until == UNTIL


async def test_preserves_partial_totals_unknowns_and_decimal_precision() -> None:
    report = await _read(REPORT)
    assert report.totals.tokens_in == 12500
    assert report.unknown_rows.tokens_in == 1
    assert report.totals.reasoning is None
    assert report.unknown_rows.reasoning == 3
    assert report.totals.cache_write == 0
    assert report.unknown_rows.cache_write == 0
    assert report.totals.spent_usd == "0.00123456"
    assert report.estimated_calls == 1
    assert report.finalized is False


async def test_zero_calls_keep_unreported_metrics_null() -> None:
    payload = copy.deepcopy(REPORT)
    payload["totals"]["calls"] = payload["estimated_calls"] = 0
    for name in payload["unknown_rows"]:
        payload["totals"][name] = None
        payload["unknown_rows"][name] = 0
    report = await _read(payload)
    assert report.totals.calls == 0
    assert report.totals.spent_usd is None
    assert report.totals.tokens_in is None


async def test_snapshot_allowlist_drops_credentials_and_raw_gateway_metadata() -> None:
    payload = copy.deepcopy(REPORT)
    payload["token"] = TOKEN
    payload["metadata"] = {"Authorization": TOKEN, "prompt": "private input"}
    payload["requests"] = [{"prompt": "private input", "token": TOKEN}]
    payload["totals"]["token"] = TOKEN
    payload["window"]["metadata"] = {"token": TOKEN}
    report = await _read(payload)
    snapshot = report.model_dump(mode="json")
    encoded = json.dumps(snapshot)
    assert TOKEN not in encoded
    assert "private input" not in encoded
    assert set(snapshot) == set(REPORT)
    assert set(snapshot["totals"]) == set(REPORT["totals"])
    assert set(snapshot["window"]) == set(REPORT["window"])


@pytest.mark.parametrize(
    "payload",
    [
        {
            "grant_id": "agr_ledger_1",
            "external_job_id": "thread:athr_ledger_1",
            "request_count": 20,
            "tokens_in": 5000,
            "tokens_out": 1000,
            "spent_usd": 0.2,
        },
        {**REPORT, "schema_version": 2},
    ],
)
async def test_legacy_or_future_schema_is_not_accepted_as_per_turn_usage(payload: Any) -> None:
    with pytest.raises(GatewayError) as raised:
        await _read(payload)
    assert raised.value.error_type == "usage_unsupported_schema"


@pytest.mark.parametrize(
    ("path", "value"),
    [
        (("schema_version",), True),
        (("schema_version",), "1"),
        (("source",), "runtime"),
        (("attribution",), "job_lifetime"),
        (("window", "basis"), "logged_at"),
        (("window", "since"), "2026-09-09T09:00:00"),
        (("window", "until"), SINCE.isoformat()),
        (("as_of",), 1788944463),
        (("as_of",), "2026-09-09T09:01:03"),
        (("finalized",), True),
        (("finalized",), 0),
        (("totals", "calls"), True),
        (("totals", "calls"), 3.0),
        (("totals", "tokens_in"), -1),
        (("totals", "tokens_out"), "320"),
        (("totals", "cache_read"), False),
        (("totals", "spent_usd"), 0.00123456),
        (("totals", "spent_usd"), "NaN"),
        (("totals", "spent_usd"), "Infinity"),
        (("totals", "spent_usd"), "1e3"),
        (("totals", "spent_usd"), "-0.10"),
        (("totals", "spent_usd"), " 0.10"),
        (("totals", "spent_usd"), "0.10\n"),
        (("unknown_rows", "tokens_in"), True),
        (("unknown_rows", "tokens_out"), 4),
        (("unknown_rows", "tokens_in"), 3),
        (("unknown_rows", "reasoning"), 0),
        (("estimated_calls",), -1),
        (("estimated_calls",), True),
        (("estimated_calls",), 4),
    ],
)
async def test_invalid_metrics_or_envelope_fail_closed(path: tuple[str, ...], value: Any) -> None:
    payload = copy.deepcopy(REPORT)
    target = payload
    for key in path[:-1]:
        target = target[key]
    target[path[-1]] = value
    with pytest.raises(GatewayError) as raised:
        await _read(payload)
    assert raised.value.error_type == "usage_invalid_response"


@pytest.mark.parametrize(
    "field", ["unknown_rows", "estimated_calls", "source", "finalized", "as_of"]
)
async def test_required_coverage_fields_cannot_be_defaulted(field: str) -> None:
    payload = copy.deepcopy(REPORT)
    del payload[field]
    with pytest.raises(GatewayError) as raised:
        await _read(payload)
    assert raised.value.error_type == "usage_invalid_response"


@pytest.mark.parametrize("field", ["grant_id", "since", "until"])
async def test_refuses_another_grant_or_window_even_with_valid_metrics(field: str) -> None:
    payload = copy.deepcopy(REPORT)
    if field == "grant_id":
        payload[field] = "agr_another"
    else:
        payload["window"][field] = (
            datetime.fromisoformat(payload["window"][field]) + timedelta(seconds=1)
        ).isoformat()
    with pytest.raises(GatewayError) as raised:
        await _read(payload)
    assert raised.value.error_type == "usage_response_mismatch"


@pytest.mark.parametrize(
    ("since", "until"),
    [
        (SINCE.replace(tzinfo=None), UNTIL),
        (SINCE, UNTIL.replace(tzinfo=None)),
        (UNTIL, SINCE),
        (SINCE, SINCE),
        (SINCE.isoformat(), UNTIL),
    ],
)
async def test_invalid_local_window_does_not_issue_http(since: Any, until: Any) -> None:
    def handler(_: httpx.Request) -> httpx.Response:
        pytest.fail("invalid usage query reached the gateway")

    with pytest.raises(GatewayError) as raised:
        await _client(handler).usage("agr_ledger_1", since=since, until=until)
    assert raised.value.error_type == "usage_query_invalid"


@pytest.mark.parametrize(
    ("status", "error_type"),
    [
        (401, "invalid_dispatch_token"),
        (404, "unknown_grant"),
        (404, "internal_api_not_configured"),
        (422, "invalid_usage_query"),
        (503, "ledger_unavailable"),
    ],
)
async def test_refusals_keep_status_and_machine_reason_without_server_message(
    status: int, error_type: str
) -> None:
    client = _client(
        lambda _: httpx.Response(
            status,
            json={
                "error": {"type": error_type, "message": f"secret debug context {TOKEN}"},
            },
        )
    )
    with pytest.raises(GatewayError) as raised:
        await client.usage("agr_ledger_1", since=SINCE, until=UNTIL)
    assert raised.value.status_code == status
    assert raised.value.error_type == error_type
    assert TOKEN not in str(raised.value)


async def test_network_failure_does_not_leak_request_or_secret() -> None:
    def handler(_: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError(f"debug header: {TOKEN}")

    with pytest.raises(GatewayError) as raised:
        await _client(handler).usage("agr_ledger_1", since=SINCE, until=UNTIL)
    assert raised.value.error_type == "usage_unavailable"
    assert raised.value.status_code is None
    assert TOKEN not in str(raised.value)


@pytest.mark.parametrize(
    "response",
    [
        httpx.Response(200, content=b"not json"),
        httpx.Response(200, json=[]),
        httpx.Response(200, json=None),
    ],
)
async def test_success_status_with_unreadable_body_is_not_zero(response: httpx.Response) -> None:
    with pytest.raises(GatewayError) as raised:
        await _client(lambda _: response).usage("agr_ledger_1", since=SINCE, until=UNTIL)
    assert raised.value.error_type == "usage_invalid_response"


async def test_unconfigured_usage_has_a_distinct_reason(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("GATEWAY_BASE_URL", raising=False)
    monkeypatch.delenv("GATEWAY_GRANT_DISPATCH_TOKEN", raising=False)
    with pytest.raises(GatewayError) as raised:
        await GrantClient().usage("agr_ledger_1", since=SINCE, until=UNTIL)
    assert raised.value.error_type == "usage_unconfigured"


async def test_configured_prefix_applies_to_all_grant_operations(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("GATEWAY_GRANT_BASE_PATH", "/internal/cloud-agent/v1/grants/")
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.url.path)
        return httpx.Response(200, json=REPORT if request.method == "GET" else {"revoked": True})

    client = _client(handler)
    await client.mint(user_id="user_1", job_id="job_1", attempt_id="1")
    await client.renew("agr_ledger_1")
    await client.revoke("agr_ledger_1")
    await client.usage("agr_ledger_1", since=SINCE, until=UNTIL)
    assert seen == [
        "/internal/cloud-agent/v1/grants",
        "/internal/cloud-agent/v1/grants/agr_ledger_1/renew",
        "/internal/cloud-agent/v1/grants/agr_ledger_1/revoke",
        "/internal/cloud-agent/v1/grants/agr_ledger_1/usage",
    ]


def test_logical_binding_survives_endpoint_migration_and_credential_rotation() -> None:
    first = GrantClient("https://gateway.example/", "old", binding_id="installation-1")
    rotated = GrantClient("https://gateway.example", "new", binding_id="installation-1")
    assert first.binding_id == rotated.binding_id
    assert (
        first.binding_id
        == GrantClient("https://different.example", "new", binding_id="installation-1").binding_id
    )
    assert (
        first.binding_id
        == GrantClient(
            "https://gateway.example", "new", base_path="/v2/grants", binding_id="installation-1"
        ).binding_id
    )
    assert (
        first.binding_id
        != GrantClient("https://gateway.example", "new", binding_id="installation-2").binding_id
    )
    assert "installation-1" not in first.binding_id
    assert "gateway.example" not in first.binding_id


def test_unconfigured_binding_falls_back_to_destination(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("GATEWAY_GRANT_BINDING_ID", raising=False)
    first = GrantClient("https://gateway.example/", "old")
    rotated = GrantClient("https://gateway.example", "new")
    assert first.binding_id == rotated.binding_id
    assert first.binding_id != GrantClient("https://different.example", "new").binding_id
    assert (
        first.binding_id
        != GrantClient("https://gateway.example", "new", base_path="/v2/grants").binding_id
    )


def test_configured_installation_identity_affects_binding(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GATEWAY_GRANT_BINDING_ID", "installation-1")
    first = GrantClient("https://gateway.example", TOKEN)
    monkeypatch.setenv("GATEWAY_GRANT_BINDING_ID", "installation-2")
    assert first.binding_id != GrantClient("https://gateway.example", TOKEN).binding_id
