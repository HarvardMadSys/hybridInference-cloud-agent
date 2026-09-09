"""Pin the safe usage consumer to the gateway's mirrored HTTP contract."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
import yaml
from jsonschema import Draft202012Validator, FormatChecker

from cloud_agent.grant_usage import GrantUsageReport
from cloud_agent.grants import GrantClient

CONTRACT = Path(__file__).resolve().parents[2] / "contracts" / "inference-grants.openapi.yaml"


def _empty_report() -> dict[str, Any]:
    return {
        "schema_version": 1,
        "source": "gateway_api_logs",
        "grant_id": "agr_contract",
        "external_job_id": "thread:athr_contract",
        "attribution": "grant_time_window",
        "window": {
            "since": "2026-09-09T09:00:00+00:00",
            "until": "2026-09-09T09:01:00+00:00",
            "basis": "request_started_at",
        },
        "as_of": "2026-09-09T09:01:02+00:00",
        "finalized": False,
        "totals": {
            "calls": 0,
            "tokens_in": None,
            "tokens_out": None,
            "cache_read": None,
            "cache_write": None,
            "reasoning": None,
            "spent_usd": None,
        },
        "unknown_rows": {
            "tokens_in": 0,
            "tokens_out": 0,
            "cache_read": 0,
            "cache_write": 0,
            "reasoning": 0,
            "spent_usd": 0,
        },
        "estimated_calls": 0,
    }


async def test_every_default_grant_operation_and_usage_parameter_is_declared() -> None:
    document = yaml.safe_load(CONTRACT.read_text())
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json=_empty_report() if request.method == "GET" else {})

    client = GrantClient(
        "https://gateway.example",
        "service-secret",
        base_path="/internal/agent-grants",
        client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )
    await client.mint(user_id="user_1", job_id="job_1", attempt_id="1")
    await client.renew("agr_contract")
    await client.revoke("agr_contract")
    since = datetime(2026, 9, 9, 9, 0, tzinfo=UTC)
    report = await client.usage("agr_contract", since=since, until=since + timedelta(minutes=1))

    for request in seen:
        path = request.url.path.replace("agr_contract", "{grant_id}")
        assert request.method.lower() in document["paths"][path]
    usage_operation = document["paths"]["/internal/agent-grants/{grant_id}/usage"]["get"]
    query_schemas = {
        parameter["name"]: parameter["schema"]
        for parameter in usage_operation["parameters"]
        if parameter.get("in") == "query"
    }
    sent = dict(seen[-1].url.params)
    assert set(sent) <= set(query_schemas)
    assert set(sent) == {"since", "until", "detail"}
    for name, value in sent.items():
        Draft202012Validator(query_schemas[name], format_checker=FormatChecker()).validate(value)
    response_schema = usage_operation["responses"]["200"]["content"]["application/json"]["schema"]
    Draft202012Validator({**response_schema, "components": document["components"]}).validate(
        report.model_dump(mode="json")
    )


def test_snapshot_model_requires_the_contracts_coverage_and_window_fields() -> None:
    document = yaml.safe_load(CONTRACT.read_text())
    report = GrantUsageReport.model_validate(_empty_report())
    for model, schema_name in (
        (report, "WindowedUsage"),
        (report.totals, "UsageTotals"),
        (report.unknown_rows, "UsageUnknownRows"),
    ):
        assert set(type(model).model_fields) == set(
            document["components"]["schemas"][schema_name]["required"]
        )
    window_schema = document["components"]["schemas"]["WindowedUsage"]["properties"]["window"]
    assert set(type(report.window).model_fields) == set(window_schema["required"])
