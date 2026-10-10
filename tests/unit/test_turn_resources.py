"""The whole-turn resource reader: what it may claim, and what it must refuse.

These tests are about honesty, not arithmetic. The reader decides how a stored
snapshot is presented, so a wrong choice here becomes a dashboard that reports
unmeasured work as zero, a prefix as a finished turn, or one capability's
numbers added to another's. Each case below pins one of those failures shut.
"""

from __future__ import annotations

from typing import Any

import pytest
from pydantic import ValidationError

from cloud_agent.schemas import WorkerTurnResourcesRequest
from cloud_agent_shared.turn_resources import (
    parse_cpu_capacity,
    summarize_run_resources,
    summarize_turn_resources,
)

MEASUREMENT_ID = "00000000-0000-4000-8000-000000000001"


def snapshot(
    *,
    state: str = "final",
    source: str = "kata-guest-member-window-v1",
    schema_version: int = 1,
    scope: str = "member_execution_window",
    cpu_usec: int | None = 80_000_000,
    cpu_status: str = "available",
    peak_bytes: int | None = 1_288_490_189,
    memory_status: str = "available",
    read_bytes: int | None = 180 * 1024 * 1024,
    write_bytes: int | None = 40 * 1024 * 1024,
    io_status: str = "available",
    duration_ms: int = 130_000,
    observed_through_ms: int = 130_000,
    cpu_reason_codes: list[str] | None = None,
    memory_reason_codes: list[str] | None = None,
    io_reason_codes: list[str] | None = None,
) -> dict[str, Any]:
    """One stored snapshot, with only the field under test varied."""
    return {
        "schema_version": schema_version,
        "measurement_id": MEASUREMENT_ID,
        "report_seq": 9,
        "state": state,
        "scope": scope,
        "source": source,
        "window": {
            "duration_ms": duration_ms,
            "observed_through_ms": observed_through_ms,
            "open_scan_ms": 12,
            "close_scan_ms": 15,
        },
        "cpu": {
            "status": cpu_status,
            "method": "live_and_exit_task_counters",
            "usec": cpu_usec,
            "reason_codes": cpu_reason_codes or [],
        },
        "memory": {
            "status": memory_status,
            "method": "sampled_member_pss",
            "peak_bytes": peak_bytes,
            "sample_interval_ms": 1000,
            "valid_samples": 131,
            "expected_samples": 131,
            "max_gap_ms": 1000,
            "reason_codes": memory_reason_codes or [],
        },
        "storage_io": {
            "status": io_status,
            "method": "task_io_accounting",
            "read_bytes": read_bytes,
            "write_bytes": write_bytes,
            "reason_codes": io_reason_codes or [],
        },
    }


class TestTurnSummaryRefusals:
    """A snapshot this reader cannot vouch for is absent, never a number."""

    def test_no_snapshot_is_no_summary(self) -> None:
        assert summarize_turn_resources(None, attempt_terminal=True) is None

    @pytest.mark.parametrize(
        "mutate",
        [
            pytest.param(lambda body: body.update(schema_version=2), id="future-schema"),
            pytest.param(lambda body: body.update(scope="container_window"), id="other-scope"),
            pytest.param(lambda body: body.update(measurement_id=7), id="unidentified"),
            pytest.param(lambda body: body.update(source=None), id="unattributed"),
            pytest.param(lambda body: body.update(state="finished"), id="unknown-state"),
            pytest.param(lambda body: body.pop("window"), id="no-window"),
            pytest.param(lambda body: body.pop("cpu"), id="no-cpu-metric"),
            pytest.param(lambda body: body.pop("memory"), id="no-memory-metric"),
            pytest.param(lambda body: body.pop("storage_io"), id="no-storage-metric"),
            pytest.param(
                lambda body: body["window"].update(observed_through_ms=130_001),
                id="observation-past-the-window",
            ),
            pytest.param(
                lambda body: body["window"].update(observed_through_ms=-1),
                id="negative-observation",
            ),
        ],
    )
    def test_a_snapshot_this_reader_cannot_read_is_absent(self, mutate: Any) -> None:
        body = snapshot()
        mutate(body)

        assert summarize_turn_resources(body, attempt_terminal=True) is None

    def test_a_non_mapping_snapshot_is_absent(self) -> None:
        assert summarize_turn_resources("not json", attempt_terminal=True) is None  # type: ignore[arg-type]


class TestStateDerivation:
    """State says what actually happened to the collection, not what we wish."""

    def test_a_collecting_snapshot_of_a_running_attempt_is_collecting(self) -> None:
        summary = summarize_turn_resources(snapshot(state="collecting"), attempt_terminal=False)

        assert summary is not None
        assert summary["state"] == "collecting"

    def test_a_collecting_snapshot_of_a_terminal_attempt_is_interrupted(self) -> None:
        summary = summarize_turn_resources(snapshot(state="collecting"), attempt_terminal=True)

        assert summary is not None
        assert summary["state"] == "interrupted"

    def test_an_interrupted_snapshot_keeps_how_far_it_actually_reached(self) -> None:
        """The whole point of the state: a prefix must not be rounded up."""
        summary = summarize_turn_resources(
            snapshot(state="collecting", observed_through_ms=12_000),
            attempt_terminal=True,
        )

        assert summary is not None
        assert summary["window"]["observed_through_ms"] == 12_000
        assert summary["window"]["duration_ms"] == 130_000

    def test_a_final_snapshot_stays_final(self) -> None:
        summary = summarize_turn_resources(snapshot(), attempt_terminal=True)

        assert summary is not None
        assert summary["state"] == "final"

    def test_the_reader_does_not_rewrite_the_stored_state(self) -> None:
        body = snapshot(state="collecting")

        summarize_turn_resources(body, attempt_terminal=True)

        assert body["state"] == "collecting"


class TestMetricProjection:
    """An unmeasured counter is never rendered as a measured zero."""

    def test_unavailable_counters_stay_null_rather_than_zero(self) -> None:
        summary = summarize_turn_resources(
            snapshot(
                cpu_usec=None,
                cpu_status="unavailable",
                peak_bytes=None,
                memory_status="unavailable",
                read_bytes=None,
                write_bytes=None,
                io_status="unavailable",
            ),
            attempt_terminal=True,
        )

        assert summary is not None
        assert summary["cpu"]["usec"] is None
        assert summary["memory"]["peak_bytes"] is None
        assert summary["storage_io"]["read_bytes"] is None

    def test_a_measured_zero_survives_as_zero(self) -> None:
        summary = summarize_turn_resources(snapshot(cpu_usec=0), attempt_terminal=True)

        assert summary is not None
        assert summary["cpu"]["usec"] == 0

    def test_a_partial_metric_keeps_its_reason_codes(self) -> None:
        summary = summarize_turn_resources(
            snapshot(cpu_status="partial", cpu_reason_codes=["ring_loss"]),
            attempt_terminal=True,
        )

        assert summary is not None
        assert summary["cpu"]["status"] == "partial"
        assert summary["cpu"]["reason_codes"] == ["ring_loss"]

    def test_memory_carries_its_own_sampling_evidence(self) -> None:
        summary = summarize_turn_resources(snapshot(), attempt_terminal=True)

        assert summary is not None
        assert summary["memory"]["sample_interval_ms"] == 1000
        assert summary["memory"]["valid_samples"] == 131
        assert summary["memory"]["expected_samples"] == 131
        assert summary["memory"]["max_gap_ms"] == 1000
        assert summary["memory"]["method"] == "sampled_member_pss"


class TestRunFolding:
    """A run's totals describe its executions, and admit what they omit."""

    def test_cpu_is_summed_across_executions(self) -> None:
        run = summarize_run_resources(
            [snapshot(cpu_usec=30_000_000), snapshot(cpu_usec=50_000_000)]
        )

        assert run is not None
        assert run["cpu_usec"] == 80_000_000
        assert run["cpu"]["status"] == "available"

    def test_memory_peaks_are_compared_and_never_added(self) -> None:
        """Two executions that each peaked at 1 GiB did not use 2 GiB at once."""
        run = summarize_run_resources(
            [snapshot(peak_bytes=1_000), snapshot(peak_bytes=1_500), snapshot(peak_bytes=900)]
        )

        assert run is not None
        assert run["memory_peak_bytes"] == 1_500

    def test_a_missing_execution_makes_every_metric_a_floor(self) -> None:
        run = summarize_run_resources([snapshot(cpu_usec=30_000_000), None])

        assert run is not None
        assert run["cpu_usec"] == 30_000_000
        assert run["cpu"]["status"] == "partial"
        assert run["memory"]["status"] == "partial"
        assert run["storage_io"]["status"] == "partial"
        assert run["attempts_measured"] == 1
        assert run["attempts_total"] == 2

    def test_a_partial_execution_makes_the_run_metric_partial(self) -> None:
        run = summarize_run_resources(
            [snapshot(cpu_status="partial", cpu_reason_codes=["ring_loss"])]
        )

        assert run is not None
        assert run["cpu"]["status"] == "partial"
        assert "ring_loss" in run["cpu"]["reason_codes"]

    def test_metrics_stay_independent(self) -> None:
        """An unsupported I/O metric says nothing about the CPU measurement."""
        run = summarize_run_resources(
            [
                snapshot(
                    io_status="unavailable",
                    read_bytes=None,
                    write_bytes=None,
                    io_reason_codes=["filesystem_attribution_unverified"],
                )
            ]
        )

        assert run is not None
        assert run["cpu"]["status"] == "available"
        assert run["memory"]["status"] == "available"
        assert run["storage_io"]["status"] == "unavailable"
        assert run["storage_io"]["reason_codes"] == ["filesystem_attribution_unverified"]

    def test_a_measured_cpu_does_not_promote_the_whole_run(self) -> None:
        """There is no whole-turn completeness field, by design."""
        run = summarize_run_resources([snapshot(memory_status="unavailable", peak_bytes=None)])

        assert run is not None
        assert run["cpu"]["status"] == "available"
        assert run["memory"]["status"] == "unavailable"

    def test_different_capabilities_are_never_added_together(self) -> None:
        run = summarize_run_resources(
            [
                snapshot(cpu_usec=30_000_000),
                snapshot(cpu_usec=50_000_000, source="some-other-window-v2"),
            ]
        )

        assert run is not None
        assert run["cpu_usec"] is None
        assert run["memory_peak_bytes"] is None
        assert run["cpu"]["status"] == "unavailable"
        assert run["cpu"]["reason_codes"] == ["source_mismatch"]

    def test_no_summaries_is_no_summary(self) -> None:
        assert summarize_run_resources([]) is None
        assert summarize_run_resources([None, None]) is None

    def test_a_run_with_an_unfinished_execution_is_not_final(self) -> None:
        run = summarize_run_resources([snapshot(state="collecting")], terminal_by_attempt=[False])

        assert run is not None
        assert run["state"] == "collecting"

    def test_a_run_whose_execution_lost_its_collector_is_interrupted(self) -> None:
        run = summarize_run_resources([snapshot(state="collecting")], terminal_by_attempt=[True])

        assert run is not None
        assert run["state"] == "interrupted"

    def test_a_received_at_timestamp_is_carried_not_invented(self) -> None:
        summary = summarize_turn_resources(
            snapshot(),
            attempt_terminal=True,
            received_at="2026-09-11T00:00:00+00:00",
        )

        assert summary is not None
        assert summary["received_at"] == "2026-09-11T00:00:00+00:00"

    def test_the_reader_never_reads_a_clock(self) -> None:
        """A summary with no supplied timestamp reports none."""
        summary = summarize_turn_resources(snapshot(), attempt_terminal=True)

        assert summary is not None
        assert summary["received_at"] is None


@pytest.mark.parametrize(
    "quota,expected",
    [
        ("2", 2.0),
        ("0.5", 0.5),
        (2, 2.0),
        (None, None),
        ("0", None),
        (0, None),
        (-1, None),
        (True, None),
        ("unlimited", None),
        ("nan", None),
        (float("inf"), None),
    ],
)
def test_cpu_capacity_is_positive_finite_and_never_assumed(quota, expected):
    """Unknown or unlimited quota cannot supply a utilisation denominator."""
    assert parse_cpu_capacity(quota) == expected


def test_cpu_capacity_survives_validation_and_projection():
    """The capacity belongs to the stored window, including fractional quotas."""
    payload = snapshot()
    payload["cpu"]["capacity_cores"] = 0.5
    stored = WorkerTurnResourcesRequest.model_validate(payload).model_dump()
    projected = summarize_turn_resources(stored, attempt_terminal=True)
    assert projected is not None
    assert projected["cpu"]["capacity_cores"] == 0.5
    assert projected["window"]["duration_ms"] == 130_000


def test_legacy_cpu_capacity_stays_unknown_and_does_not_change_retry_payloads():
    """An old snapshot gains no new key on a retry after a control-plane upgrade."""
    payload = snapshot()
    stored = WorkerTurnResourcesRequest.model_validate(payload).model_dump()
    assert stored == payload
    projected = summarize_turn_resources(stored, attempt_terminal=True)
    assert projected is not None
    assert projected["cpu"]["capacity_cores"] is None


@pytest.mark.parametrize("quota", [0, -1, True, float("inf"), float("nan"), "2"])
def test_invalid_wire_cpu_capacity_is_rejected(quota):
    """The wire carries a positive finite number, not a guessed CPU capacity."""
    payload = snapshot()
    payload["cpu"]["capacity_cores"] = quota
    with pytest.raises(ValidationError):
        WorkerTurnResourcesRequest.model_validate(payload)
