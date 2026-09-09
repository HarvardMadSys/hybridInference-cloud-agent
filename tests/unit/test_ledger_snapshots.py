"""Snapshot aggregation and failure isolation without a Gateway dependency."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from cloud_agent.gateway import GatewayError
from cloud_agent.grant_usage import GrantUsageReport
from cloud_agent.ledger import collect_usage_snapshots, refresh_ledger_entry, summarize_ledger

START = datetime(2026, 9, 9, 10, tzinfo=UTC)
END = START + timedelta(minutes=1)


def report_payload(**overrides):
    """A request with some known values and intentionally unreported reasoning."""
    report = {
        "schema_version": 1,
        "source": "gateway_api_logs",
        "grant_id": "agr_actual",
        "external_job_id": "thread:athr_1",
        "attribution": "grant_time_window",
        "window": {
            "since": START.isoformat(),
            "until": END.isoformat(),
            "basis": "request_started_at",
        },
        "as_of": END.isoformat(),
        "finalized": False,
        "totals": {
            "calls": 1,
            "tokens_in": 120,
            "tokens_out": 20,
            "cache_read": 100,
            "cache_write": 0,
            "reasoning": None,
            "spent_usd": "0.00012345",
        },
        "unknown_rows": {
            "tokens_in": 0,
            "tokens_out": 0,
            "cache_read": 0,
            "cache_write": 0,
            "reasoning": 1,
            "spent_usd": 0,
        },
        "estimated_calls": 0,
    }
    report.update(overrides)
    return report


def ledger_row(**overrides):
    """One persisted attempt reference as the store's job read returns it."""
    row = {
        "id": 1,
        "attempt_id": 10,
        "attempt_no": 1,
        "grant_id": "agr_actual",
        "binding_id": "gw_one",
        "residency_generation": 3,
        "snapshot": report_payload(),
        "queried_at": END,
        "last_error_code": None,
        "refreshing": False,
        "window_since": START,
        "window_until": END,
        "terminal": True,
        "refresh_generation": 5,
    }
    row.update(overrides)
    return row


def test_retries_are_summed_without_losing_partial_metrics_or_decimal_precision():
    first = ledger_row()
    second = ledger_row(
        id=2,
        attempt_id=11,
        attempt_no=2,
        snapshot=report_payload(estimated_calls=1),
        queried_at=END + timedelta(seconds=30),
    )
    summary = summarize_ledger([first, second])
    assert summary.totals.calls == 2
    assert summary.totals.tokens_in == 240
    assert summary.totals.reasoning is None
    assert summary.totals.spent_usd == "0.00024690"
    assert summary.unknown_rows.reasoning == 2
    assert summary.estimated_calls == 1
    # Unreported reasoning is informational: the core metrics are all known.
    assert summary.status == "available"
    assert [a.status for a in summary.attempts] == ["available", "available"]
    assert summary.queried_at == END
    assert summary.missing_attempts == 0
    assert [a.attempt_id for a in summary.attempts] == [10, 11]
    assert summary.finalized is False


def test_only_a_missing_core_metric_makes_a_snapshot_partial():
    payload = report_payload()
    payload["totals"] = {**payload["totals"], "spent_usd": None}
    payload["unknown_rows"] = {**payload["unknown_rows"], "spent_usd": 1}
    summary = summarize_ledger([ledger_row(snapshot=payload)])
    assert summary.status == "partial"
    assert summary.attempts[0].status == "partial"
    assert summary.totals.spent_usd is None
    assert summary.unknown_rows.spent_usd == 1
    # The reasoning gap is still reported, it just does not decide the status.
    assert summary.unknown_rows.reasoning == 1


def test_historical_attempts_stay_unknown_and_make_a_partial_job_explicit():
    old = ledger_row(id=None, attempt_id=9, attempt_no=1, snapshot=None, queried_at=None)
    current = ledger_row(attempt_no=2)
    summary = summarize_ledger([old, current])
    assert summary.totals.calls == 1
    assert summary.missing_attempts == 1
    assert summary.status == "partial"
    assert summary.attempts[0].status == "unavailable"
    assert summary.attempts[0].reason == "attribution_unavailable"
    assert summary.attempts[0].totals is None


def test_failed_refresh_keeps_cached_values_and_safe_reason():
    summary = summarize_ledger([ledger_row(last_error_code="usage_unavailable")])
    assert summary.status == "stale"
    assert summary.totals.tokens_in == 120
    assert summary.queried_at == END
    assert summary.attempts[0].reason == "usage_unavailable"
    assert summary.finalized is False


def test_scheduled_refresh_is_visible_without_claiming_it_is_in_flight():
    due = END + timedelta(seconds=30)
    summary = summarize_ledger([ledger_row(next_refresh_at=due)])
    assert summary.next_refresh_at == due
    assert summary.attempts[0].next_refresh_at == due
    assert summary.refreshing is False
    assert summary.totals.tokens_in == 120


def test_invalid_stored_report_is_unavailable_instead_of_mislabelled_usage():
    summary = summarize_ledger([ledger_row(snapshot=report_payload(grant_id="agr_other"))])
    assert summary.totals is None
    assert summary.status == "unavailable"
    assert summary.attempts[0].reason == "usage_invalid_response"


def test_multiple_actual_grants_for_one_attempt_remain_one_attempt_summary():
    summary = summarize_ledger(
        [
            ledger_row(),
            ledger_row(id=2, grant_id="agr_next", snapshot=report_payload(grant_id="agr_next")),
        ]
    )
    assert len(summary.attempts) == 1
    assert summary.totals.calls == 2
    assert len(summary.attempts[0].windows) == 2


def test_pending_and_empty_reports_do_not_fabricate_free_usage():
    pending = summarize_ledger([ledger_row(snapshot=None, queried_at=None, refreshing=True)])
    assert pending.status == "pending"
    assert pending.refreshing
    assert pending.totals is None
    empty = report_payload(
        totals={
            "calls": 0,
            "tokens_in": None,
            "tokens_out": None,
            "cache_read": None,
            "cache_write": None,
            "reasoning": None,
            "spent_usd": None,
        },
        unknown_rows=dict.fromkeys(report_payload()["unknown_rows"], 0),
    )
    summary = summarize_ledger([ledger_row(snapshot=empty)])
    assert summary.totals.calls == 0
    assert summary.totals.spent_usd is None
    assert summary.status == "available"


def test_public_shape_excludes_grants_bindings_and_arbitrary_gateway_keys():
    snapshot = report_payload(token="upstream-secret", metadata={"authorization": "secret"})
    public = summarize_ledger(
        [ledger_row(snapshot=snapshot, last_error_code="secret-error")]
    ).model_dump_json()
    for private in ("agr_actual", "gw_one", "upstream-secret", "authorization", "secret-error"):
        assert private not in public
    assert "refresh_failed" in public


@pytest.mark.asyncio
async def test_collector_uses_the_saved_grant_and_fixed_window():
    store = SimpleNamespace(complete_ledger_refresh=AsyncMock(return_value=True))
    grants = SimpleNamespace(
        binding_id="gw_one",
        usage=AsyncMock(return_value=GrantUsageReport.model_validate(report_payload())),
    )
    await refresh_ledger_entry(store, grants, ledger_row())
    grants.usage.assert_awaited_once_with("agr_actual", since=START, until=END)
    saved = store.complete_ledger_refresh.await_args.kwargs
    assert saved["refresh_generation"] == 5
    assert saved["terminal"] is True
    assert saved["error_code"] is None
    assert saved["report"]["grant_id"] == "agr_actual"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "failure",
    [GatewayError("Bearer SECRET", error_type="usage_unavailable"), RuntimeError("SECRET")],
)
async def test_collector_records_only_a_safe_failure_and_no_replacement_snapshot(failure):
    store = SimpleNamespace(complete_ledger_refresh=AsyncMock(return_value=True))
    grants = SimpleNamespace(binding_id="gw_one", usage=AsyncMock(side_effect=failure))
    await refresh_ledger_entry(store, grants, ledger_row())
    saved = store.complete_ledger_refresh.await_args.kwargs
    assert saved["report"] is None
    assert saved["error_code"] in {"usage_unavailable", "refresh_failed"}
    assert "SECRET" not in str(saved)


@pytest.mark.asyncio
async def test_rebinding_never_queries_the_new_gateway_with_an_old_reference():
    store = SimpleNamespace(complete_ledger_refresh=AsyncMock(return_value=True))
    grants = SimpleNamespace(binding_id="gw_new", usage=AsyncMock())
    await refresh_ledger_entry(store, grants, ledger_row())
    grants.usage.assert_not_awaited()
    assert store.complete_ledger_refresh.await_args.kwargs["error_code"] == "binding_changed"


@pytest.mark.asyncio
async def test_background_collector_limits_inflight_queries_and_propagates_cancellation():
    entered = 0
    started = asyncio.Event()
    blocker = asyncio.Event()

    async def fetch(*args, **kwargs):
        nonlocal entered
        entered += 1
        if entered == 4:
            started.set()
        await blocker.wait()

    store = SimpleNamespace(
        claim_due_ledger_refresh=AsyncMock(side_effect=lambda: ledger_row()),
        complete_ledger_refresh=AsyncMock(),
    )
    grants = SimpleNamespace(binding_id="gw_one", usage=fetch)
    task = asyncio.create_task(collect_usage_snapshots(store, grants))
    try:
        await asyncio.wait_for(started.wait(), timeout=1)
        assert entered == 4
        assert store.claim_due_ledger_refresh.await_count == 4
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    store.complete_ledger_refresh.assert_not_awaited()
