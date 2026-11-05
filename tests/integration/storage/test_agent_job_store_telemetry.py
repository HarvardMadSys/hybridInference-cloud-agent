"""PostgreSQL operation telemetry, checkpoints, and per-call rows."""

from __future__ import annotations

import json

# Imported pytest fixtures are intentionally named by test parameters.
# ruff: noqa: F811
from tests.integration.storage.test_agent_job_store import (  # noqa: F401 - pytest fixtures
    Storage,
    _claim,
    _create_job,
    _expire_attempt,
    pool,
    pytest,
    store,
)

pytestmark = [pytest.mark.dbtest, pytest.mark.asyncio]


async def test_thread_stats_keep_output_presence_when_omitting_long_transcripts(
    pool,
    store: Storage,
):
    """Exercise the cached SQL rollup without retaining transcript text."""
    job = await _create_job(store)
    claim = await _claim(store, worker_id="w1", lease_ttl_seconds=60)
    assert claim is not None
    for text in [" \n\t", "long answer " * 1000]:
        assert await store.attempt_commands.append_event(
            attempt_id=claim["attempt_id"],
            lease_generation=claim["lease_generation"],
            event_type="message",
            payload={
                "text": text,
                "usage_id": "msg_answer",
                "usage": {"input_tokens": 100, "output_tokens": 0},
            },
        )

    result = await store.execution_queries.thread_stats(job_id=job["id"], user_id=job["user_id"])

    assert result is not None
    stats = result["stats"]
    assert stats.input_tokens == 100
    assert stats.output_tokens is None
    assert stats.output_tokens_per_second is None
    async with pool.acquire() as conn:
        encoded = await conn.fetchval(
            "SELECT rollup::text FROM agent_attempt_thread_stats WHERE attempt_id = $1",
            claim["attempt_id"],
        )
    cached = json.loads(encoded)
    assert cached["produced_text"] is True
    assert "long answer" not in encoded
    assert len(encoded) < 512


async def test_a_resource_rollup_upserts_rather_than_accumulating(store: Storage):
    """A retried post is the same window said twice, not a second window.

    Idempotence here is a statement about ``ON CONFLICT``, which is exactly
    what a fake keyed by attempt cannot test: any dict-backed stand-in is
    idempotent by construction, so the assertion passes without the SQL being
    right.
    """
    job = await _create_job(store)
    claim = await _claim(store, worker_id="w1", lease_ttl_seconds=60)
    assert claim is not None
    rollup = {
        "cpu_usec": 1_000,
        "mem_peak_bytes": 2_048,
        "net_rx_bytes": 4_096,
        "sample_count": 5,
        "coverage": 1.0,
        "sampler_version": "1",
    }

    assert await store.attempt_commands.record_attempt_resources(
        attempt_id=claim["attempt_id"], lease_generation=claim["lease_generation"], rollup=rollup
    )
    assert await store.attempt_commands.record_attempt_resources(
        attempt_id=claim["attempt_id"], lease_generation=claim["lease_generation"], rollup=rollup
    )

    telemetry = await store.execution_queries.job_turn_telemetry(job_id=job["id"])
    assert telemetry is not None
    assert telemetry["attempt_id"] == claim["attempt_id"]
    assert telemetry["cpu_usec"] == 1_000, "a retried post accumulated instead of replacing"
    assert telemetry["net_rx_bytes"] == 4_096


async def test_retry_telemetry_is_read_back_per_attempt(pool, store: Storage):
    """The owner UI can pair each attempt's events with the same window."""
    job = await _create_job(store)
    first = await _claim(store, worker_id="w1", lease_ttl_seconds=60)
    assert first is not None
    assert await store.attempt_commands.record_attempt_resources(
        attempt_id=first["attempt_id"],
        lease_generation=first["lease_generation"],
        rollup={
            "cpu_usec": 1_000,
            "sample_count": 2,
            "coverage": 1.0,
            "sampler_version": "2",
            "tool_calls": [_tool_call(1, "Read")],
            "processes": [_retry_process("first")],
        },
    )
    await _expire_attempt(pool, store, first["attempt_id"])
    await store.attempt_commands.reap_expired(max_attempts=3)

    second = await _claim(store, worker_id="w2", lease_ttl_seconds=60)
    assert second is not None
    assert await store.attempt_commands.record_attempt_resources(
        attempt_id=second["attempt_id"],
        lease_generation=second["lease_generation"],
        rollup={
            "cpu_usec": 7_000,
            "sample_count": 4,
            "coverage": 1.0,
            "sampler_version": "2",
            "tool_calls": [_tool_call(1, "Bash")],
            "processes": [_retry_process("second")],
        },
    )

    attempts = await store.execution_queries.job_attempts_telemetry(job_id=job["id"])

    assert [attempt["attempt_id"] for attempt in attempts] == [
        first["attempt_id"],
        second["attempt_id"],
    ]
    assert [attempt["attempt_no"] for attempt in attempts] == [1, 2]
    assert all("tool_calls" not in attempt and "processes" not in attempt for attempt in attempts)
    details = [
        await store.execution_queries.job_turn_telemetry(
            job_id=job["id"], attempt_id=attempt["attempt_id"]
        )
        for attempt in attempts
    ]
    assert [attempt["cpu_usec"] for attempt in details] == [1_000, 7_000]
    assert [attempt["tool_calls"][0]["tool_name"] for attempt in details] == ["Read", "Bash"]
    assert [attempt["processes"][0]["comm"] for attempt in details] == ["first", "second"]
    other_job = await _create_job(store)
    assert (
        await store.execution_queries.job_turn_telemetry(
            job_id=other_job["id"], attempt_id=first["attempt_id"]
        )
        is None
    )


def _retry_process(comm: str) -> dict[str, object]:
    return {
        "process_seq": 1,
        "uid": 10001,
        "pid": 42,
        "tgid": 42,
        "ppid": 1,
        "comm": comm,
        "executable": "/bin/test",
        "started_offset_ms": 0,
        "duration_ms": 1,
        "exit_code": 0,
        "observed_fork": True,
        "observed_exec": True,
        "observed_exit": True,
        "tool_call_seqs": [1],
    }


async def test_a_superseded_attempt_cannot_overwrite_the_live_one_s_numbers(
    store: Storage,
):
    """Fenced by the same predicate as every other worker write."""
    job = await _create_job(store)
    claim = await _claim(store, worker_id="w1", lease_ttl_seconds=60)
    assert claim is not None
    await store.attempt_commands.record_attempt_resources(
        attempt_id=claim["attempt_id"],
        lease_generation=claim["lease_generation"],
        rollup={"cpu_usec": 1_000, "sample_count": 5, "coverage": 1.0, "sampler_version": "1"},
    )

    stale = await store.attempt_commands.record_attempt_resources(
        attempt_id=claim["attempt_id"],
        lease_generation=claim["lease_generation"] + 1,
        rollup={"cpu_usec": 999_999, "sample_count": 1, "coverage": 1.0, "sampler_version": "1"},
    )

    assert stale is False
    telemetry = await store.execution_queries.job_turn_telemetry(job_id=job["id"])
    assert telemetry is not None
    assert telemetry["cpu_usec"] == 1_000


async def test_an_unmeasured_window_is_stored_as_such_not_as_zero(store: Storage):
    """Null counters with coverage 0 — the row that says "not measured".

    Postgres is what makes this meaningful: the columns are nullable, so a
    turn nobody could measure reads back as NULL rather than as a zero the
    reader would have no way to question.
    """
    job = await _create_job(store)
    claim = await _claim(store, worker_id="w1", lease_ttl_seconds=60)
    assert claim is not None

    assert await store.attempt_commands.record_attempt_resources(
        attempt_id=claim["attempt_id"],
        lease_generation=claim["lease_generation"],
        rollup={"sample_count": 0, "coverage": 0.0, "sampler_version": "1"},
    )

    telemetry = await store.execution_queries.job_turn_telemetry(job_id=job["id"])
    assert telemetry is not None
    assert telemetry["cpu_usec"] is None
    assert telemetry["coverage"] == 0.0
    assert telemetry["sampler_version"] == "1"
    assert telemetry["tool_calls"] == []
    assert telemetry["tool_calls_truncated"] is False
    assert telemetry["tool_time_ms"] is None


MEASUREMENT_ID = "00000000-0000-4000-8000-000000000001"


def _turn_snapshot(
    *,
    state: str = "final",
    measurement_id: str = MEASUREMENT_ID,
    cpu_usec: int = 80_000_000,
    peak_bytes: int = 1_288_490_189,
    duration_ms: int = 130_000,
    observed_through_ms: int = 130_000,
) -> dict[str, object]:
    """One cumulative snapshot in the canonical snake_case stored shape."""
    return {
        "schema_version": 1,
        "measurement_id": measurement_id,
        "report_seq": 1,
        "state": state,
        "scope": "member_execution_window",
        "source": "kata-guest-member-window-v1",
        "window": {
            "duration_ms": duration_ms,
            "observed_through_ms": observed_through_ms,
            "open_scan_ms": 12,
            "close_scan_ms": 15,
        },
        "cpu": {
            "status": "available",
            "method": "live_and_exit_task_counters",
            "usec": cpu_usec,
            "reason_codes": [],
        },
        "memory": {
            "status": "available",
            "method": "sampled_member_pss",
            "peak_bytes": peak_bytes,
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


async def _record_turn_summary(
    store: Storage,
    claim: dict,
    *,
    report_seq: int,
    summary: dict[str, object],
    state: str | None = None,
    measurement_id: str | None = None,
    lease_generation: int | None = None,
) -> str:
    """Post one checkpoint through the arguments the route passes."""
    return await store.attempt_commands.record_attempt_turn_summary(
        attempt_id=claim["attempt_id"],
        lease_generation=(
            claim["lease_generation"] if lease_generation is None else lease_generation
        ),
        measurement_id=(
            str(summary["measurement_id"]) if measurement_id is None else measurement_id
        ),
        report_seq=report_seq,
        state=str(summary["state"]) if state is None else state,
        summary={**summary, "report_seq": report_seq},
    )


async def test_a_turn_summary_is_stored_and_its_retry_is_not_a_second_write(
    store: Storage,
):
    """A cumulative snapshot resent unchanged is the same snapshot, not a delta.

    This is the property a fake keyed by attempt cannot check: ``duplicate``
    has to come from the stored sequence and the stored payload matching, and
    the row must still hold the first value afterwards.
    """
    job = await _create_job(store)
    claim = await _claim(store, worker_id="w1", lease_ttl_seconds=60)
    assert claim is not None
    snapshot = _turn_snapshot()

    assert await _record_turn_summary(store, claim, report_seq=1, summary=snapshot) == "stored"
    assert await _record_turn_summary(store, claim, report_seq=1, summary=snapshot) == "duplicate"

    telemetry = await store.execution_queries.job_turn_telemetry(job_id=job["id"])
    assert telemetry is not None
    assert telemetry["turn_summary"]["cpu"]["usec"] == 80_000_000
    assert telemetry["turn_summary"]["state"] == "final"
    assert telemetry["turn_summary_received_at"] is not None


async def test_an_older_checkpoint_cannot_roll_a_stored_window_backwards(
    store: Storage,
):
    """Out-of-order delivery must not replace a newer cumulative snapshot."""
    job = await _create_job(store)
    claim = await _claim(store, worker_id="w1", lease_ttl_seconds=60)
    assert claim is not None

    assert (
        await _record_turn_summary(
            store, claim, report_seq=7, summary=_turn_snapshot(cpu_usec=50_000_000)
        )
        == "stored"
    )
    assert (
        await _record_turn_summary(
            store, claim, report_seq=3, summary=_turn_snapshot(cpu_usec=1_000)
        )
        == "conflict"
    )

    telemetry = await store.execution_queries.job_turn_telemetry(job_id=job["id"])
    assert telemetry is not None
    assert telemetry["turn_summary"]["cpu"]["usec"] == 50_000_000


async def test_two_payloads_at_one_sequence_are_a_protocol_conflict(
    store: Storage,
):
    """Last-write-wins would let whichever delivery arrived second decide."""
    job = await _create_job(store)
    claim = await _claim(store, worker_id="w1", lease_ttl_seconds=60)
    assert claim is not None

    assert (
        await _record_turn_summary(store, claim, report_seq=4, summary=_turn_snapshot()) == "stored"
    )
    different = _turn_snapshot(cpu_usec=1)
    assert await _record_turn_summary(store, claim, report_seq=4, summary=different) == "conflict"

    telemetry = await store.execution_queries.job_turn_telemetry(job_id=job["id"])
    assert telemetry is not None
    assert telemetry["turn_summary"]["cpu"]["usec"] == 80_000_000


async def test_a_final_snapshot_cannot_be_overwritten_by_a_collecting_one(
    store: Storage,
):
    """A newer sequence does not outrank ``final``. Nothing un-finishes a turn."""
    job = await _create_job(store)
    claim = await _claim(store, worker_id="w1", lease_ttl_seconds=60)
    assert claim is not None

    assert (
        await _record_turn_summary(
            store, claim, report_seq=9, summary=_turn_snapshot(state="final")
        )
        == "stored"
    )
    assert (
        await _record_turn_summary(
            store,
            claim,
            report_seq=10,
            summary=_turn_snapshot(state="collecting", observed_through_ms=4_000),
        )
        == "conflict"
    )

    telemetry = await store.execution_queries.job_turn_telemetry(job_id=job["id"])
    assert telemetry is not None
    assert telemetry["turn_summary"]["state"] == "final"
    assert telemetry["turn_summary"]["window"]["observed_through_ms"] == 130_000


async def test_a_second_measurement_cannot_take_over_a_fixed_window(
    store: Storage,
):
    """The first accepted report fixes the attempt's ``measurement_id``."""
    job = await _create_job(store)
    claim = await _claim(store, worker_id="w1", lease_ttl_seconds=60)
    assert claim is not None

    assert (
        await _record_turn_summary(store, claim, report_seq=1, summary=_turn_snapshot()) == "stored"
    )
    other = "00000000-0000-4000-8000-000000000002"
    assert (
        await _record_turn_summary(
            store,
            claim,
            report_seq=2,
            summary=_turn_snapshot(measurement_id=other, cpu_usec=7),
        )
        == "conflict"
    )

    telemetry = await store.execution_queries.job_turn_telemetry(job_id=job["id"])
    assert telemetry is not None
    assert telemetry["turn_summary"]["measurement_id"] == MEASUREMENT_ID


async def test_a_superseded_or_expired_lease_cannot_write_a_turn_summary(pool, store: Storage):
    """Fenced like every other worker write, against the database clock."""
    job = await _create_job(store)
    claim = await _claim(store, worker_id="w1", lease_ttl_seconds=60)
    assert claim is not None

    assert (
        await _record_turn_summary(
            store,
            claim,
            report_seq=1,
            summary=_turn_snapshot(),
            lease_generation=claim["lease_generation"] + 1,
        )
        == "lease_lost"
    )
    await _expire_attempt(pool, store, claim["attempt_id"])
    assert (
        await _record_turn_summary(store, claim, report_seq=1, summary=_turn_snapshot())
        == "lease_lost"
    )

    telemetry = await store.execution_queries.job_turn_telemetry(job_id=job["id"])
    assert telemetry is not None
    assert telemetry["turn_summary"] is None


async def test_the_legacy_writer_does_not_clobber_the_turn_summary(
    store: Storage,
):
    """Two independent reports about one attempt, in both write orders."""
    job = await _create_job(store)
    claim = await _claim(store, worker_id="w1", lease_ttl_seconds=60)
    assert claim is not None

    assert (
        await _record_turn_summary(store, claim, report_seq=1, summary=_turn_snapshot()) == "stored"
    )
    assert await store.attempt_commands.record_attempt_resources(
        attempt_id=claim["attempt_id"],
        lease_generation=claim["lease_generation"],
        rollup={"cpu_usec": 1_000, "sample_count": 5, "coverage": 1.0, "sampler_version": "1"},
    )

    telemetry = await store.execution_queries.job_turn_telemetry(job_id=job["id"])
    assert telemetry is not None
    assert telemetry["cpu_usec"] == 1_000, "the legacy rollup must have landed"
    assert telemetry["turn_summary"]["cpu"]["usec"] == 80_000_000, (
        "the legacy writer erased the turn summary"
    )


async def test_the_turn_writer_keeps_the_legacy_row_and_its_child_rows(
    store: Storage,
):
    """The other direction: a snapshot must not erase independent evidence."""
    job = await _create_job(store)
    claim = await _claim(store, worker_id="w1", lease_ttl_seconds=60)
    assert claim is not None

    assert await store.attempt_commands.record_attempt_resources(
        attempt_id=claim["attempt_id"],
        lease_generation=claim["lease_generation"],
        rollup={
            "cpu_usec": 9_000,
            "mem_peak_bytes": 4_096,
            "sample_count": 12,
            "coverage": 1.0,
            "sampler_version": "2",
            "tool_calls": [_tool_call(1, "Read")],
            "processes": [_retry_process("reader")],
        },
    )
    assert (
        await _record_turn_summary(store, claim, report_seq=1, summary=_turn_snapshot()) == "stored"
    )

    telemetry = await store.execution_queries.job_turn_telemetry(job_id=job["id"])
    assert telemetry is not None
    assert telemetry["cpu_usec"] == 9_000
    assert telemetry["mem_peak_bytes"] == 4_096
    assert telemetry["sampler_version"] == "2"
    assert [call["tool_name"] for call in telemetry["tool_calls"]] == ["Read"]
    assert [process["comm"] for process in telemetry["processes"]] == ["reader"]
    assert telemetry["turn_summary"]["measurement_id"] == MEASUREMENT_ID


async def test_a_turn_summary_alone_creates_a_legacy_compatible_base_row(
    store: Storage,
):
    """The base is the honest "no container totals were attributed", not a zero.

    ``sampler_version`` is NOT NULL with no default, so the row the snapshot
    writer inserts has to name the shared-member convention itself. A reader
    must see coverage 0 and NULL counters rather than a fabricated measurement.
    """
    job = await _create_job(store)
    claim = await _claim(store, worker_id="w1", lease_ttl_seconds=60)
    assert claim is not None

    assert (
        await _record_turn_summary(store, claim, report_seq=1, summary=_turn_snapshot()) == "stored"
    )

    telemetry = await store.execution_queries.job_turn_telemetry(job_id=job["id"])
    assert telemetry is not None
    assert telemetry["sampler_version"] == "shared-member-v1"
    assert telemetry["sample_count"] == 0
    assert telemetry["coverage"] == 0.0
    assert telemetry["cpu_usec"] is None
    assert telemetry["mem_peak_bytes"] is None
    assert telemetry["net_rx_bytes"] is None
    assert telemetry["tool_calls"] == []
    assert telemetry["processes"] == []
    assert telemetry["turn_summary"]["cpu"]["usec"] == 80_000_000


def _tool_call(seq: int, name: str, **overrides: object) -> dict[str, object]:
    """One per-call row in the shape the runner's attribution produces."""
    return {
        "call_seq": seq,
        "tool_use_id": f"toolu_{seq}",
        "tool_name": name,
        "timing_source": "message",
        "duration_ms": 20,
        "sample_count": 0,
        "overlap_n": 1,
        "measured_span_ms": None,
        "cpu_usec_exclusive": None,
        "cpu_usec_shared": None,
        "mem_peak_exclusive_bytes": None,
        "mem_peak_shared_bytes": None,
        "process_count": 0,
        **overrides,
    }


async def test_per_call_rows_are_stored_and_read_back_in_order(store: Storage):
    """The rows the footer's expanded table renders, over real SQL."""
    job = await _create_job(store)
    claim = await _claim(store, worker_id="w1", lease_ttl_seconds=60)
    assert claim is not None

    assert await store.attempt_commands.record_attempt_resources(
        attempt_id=claim["attempt_id"],
        lease_generation=claim["lease_generation"],
        rollup={
            "sample_count": 12,
            "coverage": 1.0,
            "sampler_version": "2",
            "tool_calls_truncated": False,
            "tool_time_ms": 9_543,
            "process_observer": "kata-guest-ebpf-v1",
            "process_observation_complete": True,
            "process_events_lost": 0,
            "processes_truncated": False,
            "tool_calls": [
                _tool_call(
                    1,
                    "Bash",
                    timing_source="exact",
                    duration_ms=9_523,
                    sample_count=9,
                    measured_span_ms=8_000,
                    cpu_usec_exclusive=4_400_000,
                    mem_peak_exclusive_bytes=270_000_000,
                    process_count=1,
                    process_cpu_usec=15_000,
                    process_mem_peak_bytes=2_228_224,
                    process_io_read_bytes=0,
                    process_io_write_bytes=8_388_608,
                    process_resources_partial=True,
                ),
                _tool_call(2, "Read"),
            ],
            "processes": [
                {
                    "process_seq": 1,
                    "uid": 11001,
                    "pid": 412,
                    "tgid": 412,
                    "ppid": 41,
                    "comm": "tiny",
                    "executable": "/bin/tiny",
                    "started_offset_ms": 12,
                    "duration_ms": 1,
                    "exit_code": 0,
                    "observed_fork": True,
                    "observed_exec": True,
                    "observed_exit": True,
                    "tool_call_seqs": [1],
                    "cpu_usec": 15_000,
                    "mem_peak_bytes": 2_228_224,
                    "io_read_bytes": 0,
                    "io_write_bytes": 8_388_608,
                    "io_rchar_bytes": 8_390_683,
                    "io_wchar_bytes": 8_388_701,
                }
            ],
        },
    )

    telemetry = await store.execution_queries.job_turn_telemetry(job_id=job["id"])
    assert telemetry is not None
    assert telemetry["tool_calls_truncated"] is False
    assert telemetry["tool_time_ms"] == 9_543
    first, second = telemetry["tool_calls"]
    assert [first["tool_name"], second["tool_name"]] == ["Bash", "Read"]
    assert [first["timing_source"], second["timing_source"]] == ["exact", "message"]
    assert first["cpu_usec_exclusive"] == 4_400_000
    assert first["process_count"] == 1
    assert first["process_cpu_usec"] == 15_000
    assert first["process_mem_peak_bytes"] == 2_228_224
    assert first["process_io_read_bytes"] == 0
    assert first["process_io_write_bytes"] == 8_388_608
    assert first["process_resources_partial"] is True
    assert second["process_cpu_usec"] is None
    assert second["process_resources_partial"] is False
    assert telemetry["process_observer"] == "kata-guest-ebpf-v1"
    assert telemetry["process_observation_complete"] is True
    assert telemetry["process_events_lost"] == 0
    assert telemetry["processes_truncated"] is False
    (process,) = telemetry["processes"]
    assert process["uid"] == 11001
    assert process["executable"] == "/bin/tiny"
    assert process["duration_ms"] == 1
    assert process["tool_call_seqs"] == [1]
    assert process["cpu_usec"] == 15_000
    assert process["mem_peak_bytes"] == 2_228_224
    assert process["io_write_bytes"] == 8_388_608
    assert process["io_rchar_bytes"] == 8_390_683
    assert process["io_wchar_bytes"] == 8_388_701
    # Null rather than zero, in the database itself: this is the row that says
    # the call was too short for the sampler to have seen anything.
    assert second["cpu_usec_exclusive"] is None
    assert second["mem_peak_exclusive_bytes"] is None
    assert second["sample_count"] == 0


async def test_a_retried_post_replaces_the_call_list_rather_than_appending(
    store: Storage,
):
    """The list is one statement about the attempt, not a set to union with."""
    job = await _create_job(store)
    claim = await _claim(store, worker_id="w1", lease_ttl_seconds=60)
    assert claim is not None
    base = {"sample_count": 5, "coverage": 1.0, "sampler_version": "2"}

    await store.attempt_commands.record_attempt_resources(
        attempt_id=claim["attempt_id"],
        lease_generation=claim["lease_generation"],
        rollup={**base, "tool_calls": [_tool_call(1, "Bash"), _tool_call(2, "Read")]},
    )
    await store.attempt_commands.record_attempt_resources(
        attempt_id=claim["attempt_id"],
        lease_generation=claim["lease_generation"],
        rollup={**base, "tool_calls": [_tool_call(1, "Bash")]},
    )

    telemetry = await store.execution_queries.job_turn_telemetry(job_id=job["id"])
    assert telemetry is not None
    assert len(telemetry["tool_calls"]) == 1, "a retried post left a row from the first"


async def test_a_superseded_attempt_cannot_write_per_call_rows_either(store: Storage):
    """The rows describe spans inside a window this attempt no longer owns."""
    job = await _create_job(store)
    claim = await _claim(store, worker_id="w1", lease_ttl_seconds=60)
    assert claim is not None
    await store.attempt_commands.record_attempt_resources(
        attempt_id=claim["attempt_id"],
        lease_generation=claim["lease_generation"],
        rollup={
            "sample_count": 5,
            "coverage": 1.0,
            "sampler_version": "2",
            "tool_calls": [_tool_call(1, "Bash")],
        },
    )

    stale = await store.attempt_commands.record_attempt_resources(
        attempt_id=claim["attempt_id"],
        lease_generation=claim["lease_generation"] + 1,
        rollup={
            "sample_count": 1,
            "coverage": 1.0,
            "sampler_version": "2",
            "tool_calls": [_tool_call(1, "Bash", cpu_usec_exclusive=999_999)],
        },
    )

    assert stale is False
    telemetry = await store.execution_queries.job_turn_telemetry(job_id=job["id"])
    assert telemetry is not None
    (row,) = telemetry["tool_calls"]
    assert row["cpu_usec_exclusive"] is None, "a fenced write reached the per-call rows"


async def test_a_row_cannot_claim_a_number_is_both_exclusive_and_shared(
    store: Storage,
):
    """The whole point of two columns is that a reader can tell them apart."""
    import asyncpg

    job = await _create_job(store)
    claim = await _claim(store, worker_id="w1", lease_ttl_seconds=60)
    assert claim is not None

    with pytest.raises(asyncpg.IntegrityConstraintViolationError):
        await store.attempt_commands.record_attempt_resources(
            attempt_id=claim["attempt_id"],
            lease_generation=claim["lease_generation"],
            rollup={
                "sample_count": 5,
                "coverage": 1.0,
                "sampler_version": "2",
                "tool_calls": [_tool_call(1, "Bash", cpu_usec_exclusive=10, cpu_usec_shared=10)],
            },
        )
    assert job["id"]
