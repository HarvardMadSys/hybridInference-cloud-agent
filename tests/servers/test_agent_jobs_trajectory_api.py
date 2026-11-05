"""Owner-fenced batch trajectory telemetry behavior for the job router."""

from __future__ import annotations

from datetime import timedelta

# Imported pytest fixtures are intentionally named by test parameters.
# ruff: noqa: F811
from tests.servers.test_agent_jobs_api import (  # noqa: F401 - shared pytest fixtures
    _OTHER,
    _OWNER,
    UTC,
    AsyncClient,
    AsyncMock,
    FakeStorage,
    _api_key_secret,
    _create_job,
    client,
    datetime,
    pytest,
    store,
)

pytestmark = pytest.mark.asyncio


async def test_trajectory_telemetry_batches_attempt_timing_without_detail(
    client: AsyncClient, store: FakeStorage, monkeypatch: pytest.MonkeyPatch
):
    """One owner-fenced read keeps requested turn and attempt order plus nulls."""
    first_job = await _create_job(client)
    second_job = await _create_job(client)
    started = datetime(2026, 10, 20, 12, 0, tzinfo=UTC)
    store.attempt_telemetry[first_job] = [
        {
            "attempt_id": 101,
            "attempt_no": 1,
            "continuity_mode": "new",
            "context_status": "new",
            "started_at": started,
            "finished_at": started + timedelta(milliseconds=1_234),
            "tool_time_ms": 400,
            "tool_calls": [
                {"call_seq": 1, "timing_source": "exact"},
                {"call_seq": 2, "timing_source": "exact"},
            ],
        },
        {
            "attempt_id": 102,
            "attempt_no": 2,
            "continuity_mode": "warm_native",
            "context_status": "restored",
            "started_at": started + timedelta(seconds=2),
            "finished_at": None,
            "tool_time_ms": None,
            "tool_calls_truncated": True,
            "tool_calls": [{"call_seq": 1, "timing_source": "message"}],
        },
    ]
    store.attempt_telemetry[second_job] = []
    read_batch = AsyncMock(wraps=store.trajectory_timings)
    monkeypatch.setattr(store, "trajectory_timings", read_batch)

    response = await client.post(
        "/v1/agent/jobs/trajectory-telemetry",
        json={"job_ids": [second_job, first_job]},
    )

    assert response.status_code == 200, response.text
    second, first = response.json()["jobs"]
    assert second == {"job_id": second_job, "attempts": []}
    assert first["job_id"] == first_job
    attempt_one, attempt_two = first["attempts"]
    assert attempt_one == {
        "attempt_id": 101,
        "attempt_no": 1,
        "continuity_mode": "new",
        "context_status": "new",
        "duration_ms": 1_234,
        "tool_time_ms": 400,
        "tool_calls_truncated": False,
        "tool_timing_exact": True,
    }
    assert attempt_two["duration_ms"] is None
    assert attempt_two["tool_time_ms"] is None
    assert attempt_two["tool_calls_truncated"] is True
    assert attempt_two["tool_timing_exact"] is False
    read_batch.assert_awaited_once_with(
        job_ids=[second_job, first_job], user_id=_OWNER, max_attempts=2_000
    )


@pytest.mark.parametrize("unavailable", ["foreign", "deleted", "missing"])
async def test_trajectory_telemetry_does_not_disclose_unavailable_jobs(
    client: AsyncClient, store: FakeStorage, unavailable: str
):
    job_id = await _create_job(client)
    if unavailable == "foreign":
        store.jobs[job_id]["user_id"] = _OTHER
    elif unavailable == "deleted":
        store.jobs[job_id]["deleted_at"] = datetime.now(UTC)
    else:
        job_id = "ajob_missing"

    response = await client.post("/v1/agent/jobs/trajectory-telemetry", json={"job_ids": [job_id]})

    assert response.status_code == 404
    assert response.json() == {"detail": "Job not found"}


@pytest.mark.parametrize(
    "job_ids",
    [
        ["ajob_same", "ajob_same"],
        [f"ajob_{index}" for index in range(101)],
        ["ajob_bad\x00id"],
    ],
)
async def test_trajectory_telemetry_rejects_invalid_or_unbounded_jobs(
    client: AsyncClient,
    store: FakeStorage,
    monkeypatch: pytest.MonkeyPatch,
    job_ids: list[str],
):
    read_batch = AsyncMock()
    monkeypatch.setattr(store, "trajectory_timings", read_batch)

    response = await client.post("/v1/agent/jobs/trajectory-telemetry", json={"job_ids": job_ids})

    assert response.status_code == 422
    read_batch.assert_not_awaited()


async def test_trajectory_telemetry_rejects_an_oversized_attempt_result(
    client: AsyncClient, store: FakeStorage, monkeypatch: pytest.MonkeyPatch
):
    job_id = await _create_job(client)
    read_batch = AsyncMock(return_value={"owned_count": 1, "attempt_count": 2_001, "attempts": []})
    monkeypatch.setattr(store, "trajectory_timings", read_batch)

    response = await client.post("/v1/agent/jobs/trajectory-telemetry", json={"job_ids": [job_id]})

    assert response.status_code == 413
    assert response.json() == {"detail": "Too many attempts in trajectory batch"}
