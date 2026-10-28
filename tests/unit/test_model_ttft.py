"""Model TTFT is the gateway's per-request first-token time, and nothing else.

The incident behind these tests: a turn waited 85 s for its sandbox, workspace
and CLI before the first model request left, the gateway measured 10.4 s to the
first token, and the chat bar said "TTFT 96 s" because it subtracted the
attempt start from the first completed message event. Each case pins one way
the replacement could drift back into that, or invent a number it never read.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import pytest
from tests.unit.test_grant_usage_requests import GRANT, page_payload, request_row

from cloud_agent import model_ttft
from cloud_agent.gateway import GatewayError
from cloud_agent.grant_usage import GrantUsageRequestPage
from cloud_agent.model_ttft import (
    MODEL_TTFT_ROWS_PER_SOURCE,
    read_model_ttft,
    summarize_request_pages,
)
from cloud_agent.schemas import AgentThreadStatsResponse
from cloud_agent.thread_stats import SessionStatistics, summarize_thread

BINDING = "gw_binding_one"
ATTEMPT_START = datetime(2026, 10, 28, 9, 0, tzinfo=UTC)
WINDOW_UNTIL = ATTEMPT_START + timedelta(minutes=5)
THREAD = "athr_requests_1"


def source(**overrides: Any) -> dict[str, Any]:
    """One started execution with its stored gateway reference."""
    row: dict[str, Any] = {
        "attempt_id": 10,
        "attempt_no": 1,
        "attempt_status": "finished",
        "started_at": ATTEMPT_START,
        "finished_at": WINDOW_UNTIL,
        "thread_id": THREAD,
        "ledger_id": 42,
        "grant_id": GRANT,
        "binding_id": BINDING,
        "residency_generation": 1,
        "window_since": ATTEMPT_START,
        "window_until": WINDOW_UNTIL,
        "window_open": False,
    }
    row.update(overrides)
    return row


def row(request_id: str, *, offset_s: float = 85, **overrides: Any) -> dict[str, Any]:
    started = ATTEMPT_START + timedelta(seconds=offset_s)
    return request_row(
        request_id=request_id,
        request_started_at=started.isoformat(),
        logged_at=(started + timedelta(seconds=20)).isoformat(),
        **overrides,
    )


def page(rows: list[dict[str, Any]], **overrides: Any) -> GrantUsageRequestPage:
    payload = page_payload(
        rows,
        window={
            "since": ATTEMPT_START.isoformat(),
            "until": WINDOW_UNTIL.isoformat(),
            "basis": "request_started_at",
        },
        **overrides,
    )
    return GrantUsageRequestPage.model_validate(payload)


def grants(*answers: Any, binding_id: str = BINDING) -> SimpleNamespace:
    return SimpleNamespace(binding_id=binding_id, usage_requests=AsyncMock(side_effect=answers))


async def test_85_seconds_of_startup_never_reaches_model_ttft() -> None:
    """The reported incident: 85 s startup, 10.4 s gateway TTFT, block done at 96 s."""
    events = [
        {
            "job_id": "job-1",
            "attempt_id": 10,
            "seq": 1,
            "event_type": "lifecycle",
            "payload": {"phase": "start"},
            "created_at": ATTEMPT_START + timedelta(seconds=2),
        },
        {
            "job_id": "job-1",
            "attempt_id": 10,
            "seq": 2,
            "event_type": "message",
            "payload": {"text": "done"},
            "created_at": ATTEMPT_START + timedelta(seconds=96),
        },
    ]
    stats = summarize_thread(
        job_ids=["job-1"],
        attempts=[{"job_id": "job-1", "started_at": ATTEMPT_START}],
        events=events,
    )
    client = grants(page([row("req_first", offset_s=85, ttft_ms=10_400)]))

    summary = await read_model_ttft([source()], more_sources=False, grants=client, scope="job-1")

    # The event-derived interval keeps its startup-inclusive meaning under its
    # own name, and the thread summary has no field that calls it TTFT.
    assert stats.first_output_seconds == 96.0
    assert not hasattr(stats, "ttft_seconds")
    assert summary.mean_ttft_ms == 10_400
    assert summary.status == "available"
    assert summary.samples == 1
    assert summary.complete is True
    call = client.usage_requests.await_args
    assert call.args == (GRANT,)
    assert call.kwargs == {
        "since": ATTEMPT_START,
        "until": WINDOW_UNTIL,
        "limit": MODEL_TTFT_ROWS_PER_SOURCE,
        "external_job_id": f"thread:{THREAD}",
    }


async def test_unmeasured_and_failed_requests_never_become_samples() -> None:
    client = grants(
        page(
            [
                row("req_ok", offset_s=1, ttft_ms=900),
                row("req_unstreamed", offset_s=2, ttft_ms=None),
                row("req_zero", offset_s=3, ttft_ms=0),
                row("req_429", offset_s=4, ttft_ms=50, status_code=429),
                row("req_500", offset_s=5, ttft_ms=70, status_code=500),
                row("req_no_status", offset_s=6, ttft_ms=60, status_code=None),
                row("req_ok_2", offset_s=7, ttft_ms=1_100),
            ]
        )
    )

    summary = await read_model_ttft([source()], more_sources=False, grants=client, scope="job")

    assert summary.mean_ttft_ms == 1_000
    assert summary.samples == 2
    assert summary.requests_read == 7
    assert summary.complete is True


async def test_an_old_gateway_that_records_no_ttft_manufactures_none() -> None:
    client = grants(
        page([row("req_a", offset_s=1, ttft_ms=None), row("req_b", offset_s=2, ttft_ms=None)])
    )

    summary = await read_model_ttft([source()], more_sources=False, grants=client, scope="job")

    assert summary.status == "not_recorded"
    assert summary.mean_ttft_ms is None
    assert summary.samples == 0
    assert summary.requests_read == 2


@pytest.mark.parametrize("error_type", ["usage_unsupported_schema", "request_details_unsupported"])
async def test_a_gateway_without_request_detail_is_unavailable_not_zero(error_type: str) -> None:
    client = grants(GatewayError("no detail", error_type=error_type))

    summary = await read_model_ttft([source()], more_sources=False, grants=client, scope="job")

    assert summary.status == "unavailable"
    assert summary.mean_ttft_ms is None
    assert summary.complete is False
    assert summary.reason == "request_details_unsupported"


async def test_another_gateway_installations_reference_is_never_queried() -> None:
    client = grants(page([row("req", ttft_ms=100)]))

    summary = await read_model_ttft(
        [source(binding_id="gw_old")], more_sources=False, grants=client, scope="job"
    )

    client.usage_requests.assert_not_awaited()
    assert summary.status == "unavailable"
    assert summary.mean_ttft_ms is None
    assert summary.reason == "binding_changed"


@pytest.mark.parametrize(
    "change,reason",
    [
        ({"ledger_id": None, "grant_id": None, "binding_id": None}, "attribution_unavailable"),
        ({"thread_id": None}, "attribution_unavailable"),
        ({"window_until": ATTEMPT_START}, "invalid_window"),
    ],
)
async def test_unattributed_executions_are_gaps_not_silence(change: dict, reason: str) -> None:
    client = grants(page([row("req", ttft_ms=500)], grant_id=GRANT))

    summary = await read_model_ttft(
        [source(attempt_id=11, attempt_no=2, ledger_id=43), source(**change)],
        more_sources=False,
        grants=client,
        scope="job",
    )

    assert client.usage_requests.await_count == 1
    assert summary.mean_ttft_ms == 500
    assert summary.sources_read == 1
    assert summary.sources_total == 2
    assert summary.complete is False
    assert summary.reason == reason


async def test_attempts_and_jobs_weigh_each_request_once() -> None:
    """Request-weighted across executions; a row seen twice counts once."""
    first = page([row("req_shared", offset_s=1, ttft_ms=1_000)])
    second = page(
        [
            row("req_shared", offset_s=1, ttft_ms=1_000),
            row("req_b", offset_s=2, ttft_ms=2_000),
            row("req_c", offset_s=3, ttft_ms=2_000),
            row("req_d", offset_s=4, ttft_ms=2_000),
        ]
    )
    client = grants(first, second)

    summary = await read_model_ttft(
        [source(), source(attempt_id=20, attempt_no=1, ledger_id=44)],
        more_sources=False,
        grants=client,
        scope="thread",
    )

    # (1000 + 3 * 2000) / 4, not the mean of the two executions' means (1500).
    assert summary.mean_ttft_ms == 1_750
    assert summary.samples == 4
    assert summary.sources_read == 2


async def test_aborted_attempts_never_started_an_agent() -> None:
    client = grants()

    summary = await read_model_ttft(
        [source(attempt_status="aborted", ledger_id=None, grant_id=None)],
        more_sources=False,
        grants=client,
        scope="job",
    )

    client.usage_requests.assert_not_awaited()
    assert summary.sources_total == 0
    assert summary.status == "not_recorded"
    assert summary.complete is True


async def test_a_truncated_read_says_so() -> None:
    truncated = await read_model_ttft(
        [source()],
        more_sources=False,
        grants=grants(page([row("req", ttft_ms=300)], next_cursor="gw-next")),
        scope="job",
    )
    older = await read_model_ttft(
        [source()],
        more_sources=True,
        grants=grants(page([row("req", ttft_ms=300)])),
        scope="thread",
    )

    assert (truncated.mean_ttft_ms, truncated.complete, truncated.reason) == (
        300,
        False,
        "request_page_truncated",
    )
    assert (older.mean_ttft_ms, older.complete, older.reason) == (300, False, "sources_truncated")


async def test_one_failed_read_keeps_the_others_but_marks_the_gap() -> None:
    client = grants(
        page([row("req", ttft_ms=400)]),
        GatewayError("down", error_type="usage_unavailable"),
    )

    summary = await read_model_ttft(
        [source(), source(attempt_id=11, attempt_no=2, ledger_id=43)],
        more_sources=False,
        grants=client,
        scope="job",
    )

    assert summary.status == "available"
    assert summary.mean_ttft_ms == 400
    assert summary.complete is False
    assert summary.reason == "gateway_unavailable"


async def test_a_slow_gateway_yields_no_partial_guess(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(model_ttft, "MODEL_TTFT_DEADLINE_S", 0.01)

    async def hang(*_: Any, **__: Any) -> GrantUsageRequestPage:
        await asyncio.sleep(10)
        raise AssertionError("unreachable")

    client = SimpleNamespace(binding_id=BINDING, usage_requests=AsyncMock(side_effect=hang))

    summary = await read_model_ttft([source()], more_sources=False, grants=client, scope="job")

    assert summary.status == "unavailable"
    assert summary.mean_ttft_ms is None
    assert summary.reason == "gateway_unavailable"


def test_nothing_started_is_not_recorded_and_complete() -> None:
    summary = summarize_request_pages([], sources_total=0, gap=None)

    assert summary.status == "not_recorded"
    assert summary.mean_ttft_ms is None
    assert summary.complete is True


def test_startup_inclusive_first_output_is_never_published_as_ttft() -> None:
    """Neither stats response carries the old event-derived ``ttft_seconds``."""
    for model in (AgentThreadStatsResponse, SessionStatistics):
        assert "ttft_seconds" not in model.model_fields
        assert "first_output_seconds" in model.model_fields
