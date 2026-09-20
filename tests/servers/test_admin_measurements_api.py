"""Admin operation measurements: authorization, honesty, and cursor mechanics.

The interesting claims of this surface are all about what it refuses to say.
An empty page is not a page of zero latency; a percentile is not computed over
whatever rows the caller loaded; a grouping the caller names does not quietly
lose a dimension that decides comparability. Those are the properties pinned
here, alongside the guard that keeps the surface administrative.
"""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi import FastAPI  # noqa: TC002 — used in a runtime annotation.
from httpx import ASGITransport, AsyncClient

from cloud_agent import identity
from cloud_agent.api.deps import get_storage
from cloud_agent.app import create_app

ADMIN = {"Authorization": "Bearer admin-measurements-test"}
RUNS = "/admin/agent/profiling-runs"
MEASUREMENTS = "/admin/agent/measurements"


def _row(measurement_id: str = "msm_1", **overrides: object) -> dict[str, object]:
    """One stored measurement summary, as the repository returns it."""
    row: dict[str, object] = {
        "measurement_id": measurement_id,
        "interaction_id": "msm_1",
        "profiling_run_id": "run-1",
        "schema_version": 3,
        "owner_id": "user-1",
        "job_id": "ajob_1",
        "thread_id": "athr_1",
        "sandbox_id": "asbx_1",
        "host_id": "host-a",
        "attempt_id": None,
        "lease_generation": None,
        "operation": "files.list",
        "request_kind": "files.list",
        "boundary_version": 1,
        "coverage_scope": "server_request",
        "producer": "control-plane",
        "producer_instance_id": "cp-1",
        "duration_us": 1234,
        "outcome": "completed",
        "collection_complete": True,
        "end_to_end_complete": False,
        "startup_path": None,
        "observed_at": datetime.now(UTC),
        "received_at": datetime.now(UTC),
    }
    row.update(overrides)
    return row


@pytest.fixture
def capability():
    """A stub measurement capability that records what the route asked for."""
    return SimpleNamespace(
        list_profiling_runs=AsyncMock(return_value=[{"run_id": "run-1"}]),
        summarize_operation_measurements=AsyncMock(
            return_value=[{"sample_count": 3, "eligible_count": 2, "p50_us": 100}]
        ),
        list_operation_measurements=AsyncMock(return_value=[_row()]),
        get_operation_measurement=AsyncMock(return_value={**_row(), "record": {"a": 1}}),
    )


@pytest.fixture
def setup(monkeypatch, capability):
    monkeypatch.setenv("ADMIN_TOKEN", "admin-measurements-test")
    monkeypatch.setenv(identity.ENV_SESSION_SECRET, "admin-measurements-session-secret")
    monkeypatch.setenv(identity.ENV_ISSUER, "https://gateway.example")
    app = create_app()
    app.dependency_overrides[get_storage] = lambda: SimpleNamespace(
        operation_measurements=capability
    )
    return app, capability


async def get(app: FastAPI, path: str, headers: dict | None = None):
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        return await client.get(path, headers=ADMIN if headers is None else headers)


# ---------------------------------------------------------------------------
# The guard
# ---------------------------------------------------------------------------


async def test_the_surface_requires_an_administrator(setup):
    """It answers for every user's experiments, so it is not an owner surface."""
    app, _ = setup

    missing = await get(app, MEASUREMENTS, headers={})
    invalid = await get(app, MEASUREMENTS, headers={"Authorization": "Bearer nope"})
    assert missing.status_code == 401
    assert invalid.status_code == 401
    assert missing.headers["cache-control"] == "no-store"
    assert invalid.headers["cache-control"] == "no-store"


async def test_reads_are_never_cached(setup):
    """A cached measurement was read at a time nobody chose."""
    app, _ = setup

    response = await get(app, MEASUREMENTS)

    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"


async def test_an_unexpected_admin_read_failure_is_not_cacheable(setup):
    """A generated 500 needs the same cache boundary as a successful read."""
    app, capability = setup
    capability.list_operation_measurements.side_effect = RuntimeError("database unavailable")

    async with AsyncClient(
        transport=ASGITransport(app=app, raise_app_exceptions=False),
        base_url="http://test",
    ) as client:
        response = await client.get(MEASUREMENTS, headers=ADMIN)

    assert response.status_code == 500
    assert response.headers["cache-control"] == "no-store"


async def test_the_local_writer_exposes_its_bounds_and_loss_counts(setup):
    """Queue loss is visible and explicitly scoped to this replica."""
    from cloud_agent import measurement_writer
    from cloud_agent.measurement_writer import MeasurementWriter

    app, _ = setup
    local = MeasurementWriter()
    local.counts.dropped_full = 3
    measurement_writer.reset_writer(local)
    try:
        response = await get(app, "/admin/agent/measurement-writer")
    finally:
        measurement_writer.reset_writer(None)

    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    assert response.json() == {
        "scope": "this_control_plane_process",
        "running": False,
        "pending": 0,
        "in_flight": 0,
        "queued_bytes": 0,
        "counts": {
            "stored": 0,
            "duplicate": 0,
            "rejected": 0,
            "conflict": 0,
            "dropped_full": 3,
            "dropped_timeout": 0,
            "dropped_shutdown": 0,
            "failed": 0,
            "reasons": {},
        },
    }


# ---------------------------------------------------------------------------
# An empty result is reported as empty
# ---------------------------------------------------------------------------


async def test_an_empty_page_says_why_it_cannot_say_why(setup):
    """No samples is not zero samples, and the page does not pretend otherwise."""
    app, capability = setup
    capability.list_operation_measurements.return_value = []

    response = await get(app, f"{MEASUREMENTS}?run_id=run-1")

    body = response.json()
    assert body["measurements"] == []
    assert body["count"] == 0
    assert body["next_cursor"] is None
    # The four causes are named, because the page genuinely cannot distinguish
    # them -- and a reader that assumed "nothing was slow" would be wrong.
    assert "off" in body["empty_reason"]
    assert "unselected" in body["empty_reason"]
    assert "undelivered" in body["empty_reason"]


async def test_a_page_offers_a_cursor_only_when_lookahead_finds_more(setup):
    """A page exactly at the limit must not invent a row after it."""
    app, capability = setup
    capability.list_operation_measurements.return_value = [_row("msm_1"), _row("msm_2")]

    exact = await get(app, f"{MEASUREMENTS}?limit=2")
    assert exact.json()["next_cursor"] is None

    capability.list_operation_measurements.return_value = [
        _row("msm_1"),
        _row("msm_2"),
        _row("msm_3"),
    ]
    full = await get(app, f"{MEASUREMENTS}?limit=2")
    assert [row["measurement_id"] for row in full.json()["measurements"]] == [
        "msm_1",
        "msm_2",
    ]
    assert full.json()["next_cursor"] == "msm_2"

    capability.list_operation_measurements.return_value = [_row("msm_1")]
    short = await get(app, f"{MEASUREMENTS}?limit=2")
    assert short.json()["next_cursor"] is None
    assert capability.list_operation_measurements.await_args.kwargs["limit"] == 3


async def test_a_cursor_is_resolved_rather_than_trusted(setup):
    """A caller cannot forge an arrival time by handing back a bare id."""
    app, capability = setup
    anchor = _row("msm_9")
    capability.get_operation_measurement.return_value = anchor

    response = await get(app, f"{MEASUREMENTS}?after=msm_9")

    assert response.status_code == 200
    assert capability.get_operation_measurement.await_args.args == ("msm_9",)
    keyset = capability.list_operation_measurements.await_args.kwargs["after"]
    assert keyset == (anchor["received_at"], "msm_9")


async def test_an_unknown_cursor_is_refused(setup):
    """A cursor pointing at nothing cannot silently restart the page."""
    app, capability = setup
    capability.get_operation_measurement.return_value = None

    response = await get(app, f"{MEASUREMENTS}?after=msm_missing")

    assert response.status_code == 404
    assert response.json()["detail"]["error"]["type"] == "unknown_cursor"
    assert response.headers["cache-control"] == "no-store"


# ---------------------------------------------------------------------------
# Filters and aggregation
# ---------------------------------------------------------------------------


async def test_filters_reach_the_query_as_they_were_asked(setup):
    """The route passes the selection through rather than narrowing it itself."""
    app, capability = setup

    await get(
        app,
        f"{MEASUREMENTS}?run_id=run-1&operation=files.list&operation=files.open"
        "&request_kind=terminal.create&coverage_scope=server_request"
        "&outcome=completed&collection_complete=true&job_id=ajob_1&thread_id=athr_1"
        "&attempt_id=42&interaction_id=aint_1",
    )

    kwargs = capability.list_operation_measurements.await_args.kwargs
    assert kwargs["run_id"] == "run-1"
    assert kwargs["operations"] == ["files.list", "files.open"]
    assert kwargs["request_kinds"] == ["terminal.create"]
    assert kwargs["coverage_scopes"] == ["server_request"]
    assert kwargs["outcome"] == "completed"
    assert kwargs["collection_complete"] is True
    assert kwargs["job_id"] == "ajob_1"
    assert kwargs["thread_id"] == "athr_1"
    # The Runs detail joins on the attempt; a terminal open has no attempt and
    # is read by its interaction. Both must arrive as the caller stated them.
    assert kwargs["attempt_id"] == 42
    assert kwargs["interaction_id"] == "aint_1"


async def test_the_precise_keys_are_absent_when_they_were_not_asked_for(setup):
    """An unfiltered read must not narrow to attempt ``0`` or an empty id."""
    app, capability = setup

    await get(app, MEASUREMENTS)

    kwargs = capability.list_operation_measurements.await_args.kwargs
    assert kwargs["attempt_id"] is None
    assert kwargs["interaction_id"] is None


async def test_a_negative_attempt_id_is_refused(setup):
    """An attempt id is a positive generation; 422 rather than an empty page."""
    app, _capability = setup

    response = await get(app, f"{MEASUREMENTS}?attempt_id=-1")

    assert response.status_code == 422


async def test_a_selector_this_surface_does_not_accept_is_refused(setup):
    """An ignored query parameter is a wider read than the caller asked for.

    FastAPI drops an unknown parameter silently, so a misspelt key would return
    the whole experiment's page and look exactly like a filter that matched
    everything. The Admin page refuses the same situation, and the API must not
    answer a different question than the one it was asked.
    """
    app, capability = setup

    response = await get(app, f"{MEASUREMENTS}?attempt=42")

    assert response.status_code == 422
    assert response.json()["detail"]["error"]["type"] == "unexpected_query_parameter"
    capability.list_operation_measurements.assert_not_awaited()


async def test_the_summary_is_computed_over_the_experiment(setup):
    """A percentile of one page is a percentile of the page."""
    app, capability = setup

    response = await get(app, f"{RUNS}/run-1/summary")

    assert response.status_code == 200
    assert capability.summarize_operation_measurements.await_args.kwargs["run_id"] == "run-1"
    body = response.json()
    assert body["run_id"] == "run-1"
    assert body["groups"][0]["eligible_count"] == 2


async def test_an_unknown_grouping_dimension_is_refused(setup):
    """A caller must not be handed a coarser grouping than it asked for."""
    app, capability = setup
    capability.summarize_operation_measurements.side_effect = ValueError(
        "unknown grouping dimension(s): nonsense"
    )

    response = await get(app, f"{RUNS}/run-1/summary?group_by=nonsense")

    assert response.status_code == 422
    assert response.json()["detail"]["error"]["type"] == "invalid_group_by"


async def test_a_missing_measurement_is_a_404(setup):
    """One id either exists or does not; there is no empty stand-in."""
    app, capability = setup
    capability.get_operation_measurement.return_value = None

    response = await get(app, f"{MEASUREMENTS}/msm_missing")

    assert response.status_code == 404
    assert response.headers["cache-control"] == "no-store"


async def test_the_catalogue_reports_how_many_runs_it_returned(setup):
    """A page does not imply it returned every experiment."""
    app, _capability = setup

    response = await get(app, f"{RUNS}?limit=10")

    body = response.json()
    assert body["count"] == 1
    assert body["limit"] == 10
    assert body["runs"] == [{"run_id": "run-1"}]


async def test_a_detail_read_returns_the_frozen_record(setup):
    """The list is summaries; only the detail read carries the record itself."""
    app, _ = setup

    response = await get(app, f"{MEASUREMENTS}/msm_1")

    assert response.status_code == 200
    body = response.json()
    assert body["record"] == {"a": 1}
    # The two completeness questions stay distinguishable in the payload, so a
    # page cannot present a server request as the user's whole wait.
    assert body["collection_complete"] is True
    assert body["end_to_end_complete"] is False
    assert body["coverage_scope"] == "server_request"


async def test_the_route_does_not_invent_a_window(setup):
    """No implicit time filter: an unqualified read is the newest page."""
    app, capability = setup

    await get(app, RUNS)

    assert capability.list_profiling_runs.await_args.kwargs == {"since": None, "limit": 50}
