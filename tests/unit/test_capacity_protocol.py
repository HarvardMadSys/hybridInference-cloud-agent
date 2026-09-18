"""The capacity block's wire rules, and the read side built on top of them.

The block rides a liveness message, so both directions are load-bearing. Outward
it has to be a closed vocabulary — a monitoring field that can be made
arbitrarily large or arbitrarily shaped is a way to attack the heartbeat
endpoint. Inward it has to be *optional*: a control plane that answers 422 to a
malformed monitoring block has turned a gap in a graph into a host that reads
as dead, which is a strictly worse trade than not drawing the graph.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from uuid import UUID

import pytest
from pydantic import ValidationError

from cloud_agent.capacity_protocol import (
    FRESH_AFTER_SECONDS,
    HISTORY_RANGES,
    MAX_CAPACITY_BYTES,
    MAX_HISTORY_POINTS,
    AgentHostCapacityHistoryResponse,
    HostCapacityReport,
    admin_capacity_view,
    bucket_for,
    history_point_grid,
    history_point_write_from_block,
    history_window,
    parse_capacity_block,
    resolve_history_range,
)
from cloud_agent.schemas import HostHeartbeatRequest
from cloud_agent_host.host_capacity import OTHER_STATES

SAMPLE_ID = "00000000-0000-4000-8000-000000000001"
BOOT_ID = "00000000-0000-4000-8000-000000000002"


def capacity_block(**overrides: object) -> dict:
    """A complete, healthy v1 block, with named fields replaceable."""
    block: dict = {
        "schema_version": 1,
        "sample_id": SAMPLE_ID,
        "sample_age_ms": 120,
        "linux_boot_id": BOOT_ID,
        "source": "linux_host_proc_and_docker_list",
        "cpu_percent": 37.2,
        "cpu_logical_count": 32,
        "cpu_window_seconds": 30.01,
        "memory_total_bytes": 137_438_953_472,
        "memory_available_bytes": 92_341_796_864,
        "inventory": {
            "namespace": "staging-agent",
            "status": "available",
            "running": 3,
            "stopped": 7,
            "other_states": dict.fromkeys(OTHER_STATES, 0),
            "reason": None,
        },
        "missing": {"cpu": None, "memory": None, "storage": None},
    }
    block.update(overrides)
    return block


def heartbeat_body(block: object) -> dict:
    """A heartbeat carrying ``block`` under the optional capacity field."""
    return {"slots_total": 3, "capacity": block}


# ── Parsing the optional block ────────────────────────────────────────


def test_a_complete_block_parses() -> None:
    report = parse_capacity_block(capacity_block())
    assert report is not None
    assert report.cpu_percent == pytest.approx(37.2)
    assert report.inventory.running == 3
    assert report.missing.cpu is None


def test_an_absent_block_is_not_an_error() -> None:
    assert parse_capacity_block(None) is None


@pytest.mark.parametrize(
    "block",
    [
        "not-an-object",
        [],
        {"schema_version": 2, "sample_id": SAMPLE_ID},
        capacity_block(schema_version=99),
        capacity_block(source="something_else"),
        capacity_block(sample_id="not-a-uuid"),
        capacity_block(linux_boot_id="not-a-uuid"),
        capacity_block(sample_age_ms=600_000),
        capacity_block(cpu_percent=101),
        capacity_block(cpu_percent=float("nan")),
        capacity_block(cpu_logical_count=0),
        capacity_block(cpu_window_seconds=0),
        capacity_block(memory_total_bytes=0),
        capacity_block(memory_available_bytes=200_000_000_000),
        capacity_block(missing={"cpu": None, "memory": None}, cpu_percent=None),
        capacity_block(missing={"cpu": "warming_up", "memory": None}),
        capacity_block(memory_total_bytes=None, missing={"cpu": None, "memory": None}),
        capacity_block(cpu_window_seconds=None),
        capacity_block(linux_boot_id=None),
    ],
)
def test_an_unusable_block_is_dropped(block: object) -> None:
    assert parse_capacity_block(block) is None


def test_an_oversized_block_is_refused_before_parsing() -> None:
    """The ceiling is what stops a hostile runner making every beat expensive."""
    padded = capacity_block()
    padded["inventory"] = {
        **padded["inventory"],
        "reason": "x" * (MAX_CAPACITY_BYTES + 1),
    }
    assert parse_capacity_block(padded) is None


def test_a_namespace_jsonb_cannot_store_is_refused() -> None:
    """U+0000 is not representable in JSONB, and the database says so loudly.

    Left to the database, the rejection arrives as an exception inside the
    heartbeat's own transaction — which is a monitoring field taking the
    liveness write down with it. It is refused here instead, where the cost is
    the block.
    """
    block = capacity_block()
    block["inventory"] = {**block["inventory"], "namespace": "staging\u0000agent"}
    assert parse_capacity_block(block) is None

    for control in ("\n", "\t", "\x7f"):
        poisoned = capacity_block()
        poisoned["inventory"] = {**poisoned["inventory"], "namespace": f"staging{control}agent"}
        assert parse_capacity_block(poisoned) is None


def test_an_unknown_state_key_is_refused() -> None:
    block = capacity_block()
    block["inventory"]["other_states"] = {**dict.fromkeys(OTHER_STATES, 0), "hibernating": 1}
    assert parse_capacity_block(block) is None


def test_partial_states_are_normalized_to_the_fixed_map() -> None:
    block = capacity_block()
    block["inventory"] = {
        "namespace": "staging-agent",
        "status": "partial",
        "running": 1,
        "stopped": None,
        "other_states": {"paused": 1},
        "reason": "unknown_state",
    }
    report = parse_capacity_block(block)
    assert report is not None
    assert report.inventory.other_states == {**dict.fromkeys(OTHER_STATES, None), "paused": 1}
    assert report.inventory.reason == "unknown_state"


def test_an_unavailable_inventory_must_not_report_counts() -> None:
    block = capacity_block()
    block["inventory"] = {
        "namespace": "staging-agent",
        "status": "unavailable",
        "running": 0,
        "stopped": 0,
        "other_states": dict.fromkeys(OTHER_STATES, 0),
        "reason": "docker_timeout",
    }
    assert parse_capacity_block(block) is None


def test_a_partial_inventory_without_a_reason_is_refused() -> None:
    """A qualified count with no qualification reads as a total."""
    block = capacity_block()
    block["inventory"] = {
        "namespace": "staging-agent",
        "status": "partial",
        "running": 1,
        "stopped": 0,
        "other_states": dict.fromkeys(OTHER_STATES, 0),
        "reason": None,
    }
    assert parse_capacity_block(block) is None


def test_identifiers_are_normalized_to_canonical_uuids() -> None:
    report = parse_capacity_block(capacity_block(sample_id=SAMPLE_ID.upper()))
    assert report is not None
    assert report.sample_id == SAMPLE_ID


# ── The heartbeat never refuses over the block ────────────────────────


def test_a_malformed_block_does_not_refuse_the_heartbeat() -> None:
    request = HostHeartbeatRequest.model_validate(
        heartbeat_body({"schema_version": 3, "sample_id": "nope"})
    )
    assert request.capacity is None
    assert request.slots_total == 3


def test_a_good_block_reaches_the_request_intact() -> None:
    request = HostHeartbeatRequest.model_validate(heartbeat_body(capacity_block()))
    assert request.capacity is not None
    assert request.capacity.inventory.stopped == 7
    assert request.capacity.model_dump(mode="json")["sample_id"] == SAMPLE_ID


def test_the_liveness_fields_stay_exactly_as_strict() -> None:
    """Optional monitoring must not become an optional heartbeat."""
    with pytest.raises(ValidationError):
        HostHeartbeatRequest.model_validate({"slots_total": 0})
    with pytest.raises(ValidationError):
        HostHeartbeatRequest.model_validate({"slots_total": 3, "physical_host": ""})


def test_the_stored_dump_has_only_the_declared_fields() -> None:
    """What is persisted is the normalized shape, not what the runner sent."""
    block = capacity_block()
    block["evil"] = {"nested": "payload"}
    report = parse_capacity_block(block)
    assert report is not None
    dumped = report.model_dump(mode="json")
    assert "evil" not in dumped
    assert sorted(json.loads(json.dumps(dumped))) == sorted(dumped)


# ── The admin view ────────────────────────────────────────────────────


def test_the_admin_view_derives_memory_and_freshness() -> None:
    now = datetime(2026, 9, 11, 12, 0, 0, tzinfo=UTC)
    view = admin_capacity_view(
        snapshot=capacity_block(),
        received_at=now,
        sampled_at=now - timedelta(seconds=12),
        now=now,
    )
    assert view is not None
    assert view.freshness == "fresh"
    assert view.sample_age_seconds == pytest.approx(12.0)
    assert view.memory_used_bytes == 137_438_953_472 - 92_341_796_864
    assert view.memory_used_percent == pytest.approx(
        100 * (137_438_953_472 - 92_341_796_864) / 137_438_953_472
    )


def test_an_old_observation_is_stale_not_current() -> None:
    now = datetime(2026, 9, 11, 12, 0, 0, tzinfo=UTC)
    view = admin_capacity_view(
        snapshot=capacity_block(),
        received_at=now,
        sampled_at=now - timedelta(seconds=FRESH_AFTER_SECONDS + 1),
        now=now,
    )
    assert view is not None
    assert view.freshness == "stale"
    # The values survive: a stale reading is still the last thing measured.
    assert view.cpu_percent == pytest.approx(37.2)


def test_a_corrupt_snapshot_costs_one_host_its_numbers_only() -> None:
    now = datetime(2026, 9, 11, 12, 0, 0, tzinfo=UTC)
    assert (
        admin_capacity_view(
            snapshot={"schema_version": 1, "cpu_percent": "thirty-seven"},
            received_at=now,
            sampled_at=now,
            now=now,
        )
        is None
    )
    assert admin_capacity_view(snapshot=None, received_at=None, sampled_at=None, now=now) is None
    # A row written before the columns existed has timestamps but no block.
    assert admin_capacity_view(snapshot=None, received_at=now, sampled_at=now, now=now) is None


def test_a_future_sampled_at_never_produces_a_negative_age() -> None:
    now = datetime(2026, 9, 11, 12, 0, 0, tzinfo=UTC)
    view = admin_capacity_view(
        snapshot=capacity_block(),
        received_at=now,
        sampled_at=now + timedelta(seconds=30),
        now=now,
    )
    assert view is not None
    assert view.sample_age_seconds == 0.0


# ── History: writing ──────────────────────────────────────────────────


SAMPLED_AT = datetime(2026, 9, 11, 12, 34, 56, tzinfo=UTC)


def point_from(block: dict, *, sampled_at: datetime | None = None):
    """The history point an accepted block produces, at a fixed instant."""
    return history_point_write_from_block(
        block, sampled_at=sampled_at or SAMPLED_AT, sample_id=SAMPLE_ID
    )


def test_an_accepted_block_becomes_one_point_in_the_minute_it_measured() -> None:
    point = point_from(capacity_block())
    assert point is not None
    assert point.bucket_at == datetime(2026, 9, 11, 12, 34, 0, tzinfo=UTC)
    assert point.sampled_at == SAMPLED_AT
    assert point.sample_id == SAMPLE_ID
    assert point.cpu_percent == pytest.approx(37.2)
    assert point.memory_total_bytes == 137_438_953_472


def test_the_instant_stamped_is_the_accepting_writes_own() -> None:
    """The point and the snapshot must not disagree about when this was taken.

    The block carries an age, not an instant, and the row is stamped from the
    server's clock; re-deriving the instant here would let a point sit in a
    different minute from the reading it was taken from.
    """
    point = point_from(capacity_block(), sampled_at=datetime(2026, 9, 11, 13, 0, 5, tzinfo=UTC))
    assert point is not None
    assert point.bucket_at == datetime(2026, 9, 11, 13, 0, 0, tzinfo=UTC)


def test_an_unusable_identity_is_skipped_rather_than_stored() -> None:
    assert (
        history_point_write_from_block(capacity_block(), sampled_at=SAMPLED_AT, sample_id="nope")
        is None
    )
    assert (
        history_point_write_from_block(capacity_block(), sampled_at=None, sample_id=SAMPLE_ID)
        is None
    )  # type: ignore[arg-type]


def test_a_corrupt_scalar_costs_only_its_own_field() -> None:
    """One bad number must not cost the other half of the same observation."""
    point = point_from(
        capacity_block(
            cpu_percent="not-a-number", memory_total_bytes=512, memory_available_bytes=256
        )
    )
    assert point is not None
    assert point.cpu_percent is None
    assert point.memory_total_bytes == 512


def test_impossible_values_are_dropped_rather_than_stored() -> None:
    """The table's own CHECK constraints are the reason these never reach it.

    The model refuses these before they are ever stored; this is the second
    line, and it is the one that matters, because it runs inside the statement
    that is keeping a live machine's reading.
    """
    point = point_from(
        capacity_block(cpu_percent=250, memory_total_bytes=100, memory_available_bytes=200)
    )
    assert point is not None
    assert point.cpu_percent is None
    assert point.memory_total_bytes is None
    assert point.memory_available_bytes is None


def test_half_a_memory_pair_is_no_memory_reading_at_all() -> None:
    """The table stores both figures or neither, so neither is what this is."""
    point = point_from(capacity_block(memory_available_bytes=None))
    assert point is not None
    assert point.memory_total_bytes is None
    assert point.memory_available_bytes is None
    assert point.cpu_percent == pytest.approx(37.2)


def test_a_cpu_gap_keeps_its_memory_half() -> None:
    point = point_from(
        capacity_block(
            cpu_percent=None,
            cpu_window_seconds=None,
            missing={"cpu": "warming_up", "memory": None, "storage": None},
        )
    )
    assert point is not None
    assert point.cpu_percent is None
    assert point.cpu_missing_reason == "warming_up"
    assert point.memory_total_bytes == 137_438_953_472


def test_a_non_uuid_boot_id_is_stored_as_no_boot_id() -> None:
    point = point_from(capacity_block(linux_boot_id="hand-edited"))
    assert point is not None
    assert point.linux_boot_id is None


def test_a_uuid_column_value_is_normalized_before_the_response_model() -> None:
    """asyncpg returns UUID columns as UUID objects, not strings.

    The write path converts on the way in; this is the same conversion on the
    way out. Without it the response model refuses the row and the whole
    history window becomes a 500 — but only once real history exists, which is
    why the unit fixtures (all strings) never saw it.
    """
    since = datetime(2026, 9, 11, 12, 0, 0, tzinfo=UTC)
    row = history_row(since)
    row["linux_boot_id"] = UUID("00000000-0000-4000-8000-000000000002")
    (point,) = history_point_grid(rows=[row], since=since, until=since)
    assert point.linux_boot_id == "00000000-0000-4000-8000-000000000002"

    # And the response the endpoint returns is built from exactly those points.
    response = AgentHostCapacityHistoryResponse(
        host="gpu-box",
        capacity_epoch="00000000-0000-4000-8000-0000000000ff",
        since=since,
        until=since,
        observed_at=since,
        points=[point],
    )
    assert response.points[0].linux_boot_id == "00000000-0000-4000-8000-000000000002"


def test_a_boot_id_that_cannot_be_read_is_a_gap_not_a_failure() -> None:
    """A hand-edited row must cost one field, not the whole window."""
    since = datetime(2026, 9, 11, 12, 0, 0, tzinfo=UTC)
    row = history_row(since)
    row["linux_boot_id"] = "not-a-uuid"
    (point,) = history_point_grid(rows=[row], since=since, until=since)
    assert point.linux_boot_id is None
    assert point.cpu_percent == pytest.approx(10.0)


def test_bucketing_floors_to_the_minute_in_utc() -> None:
    moment = datetime(2026, 9, 11, 12, 34, 59, 999_999, tzinfo=UTC)
    assert bucket_for(moment) == datetime(2026, 9, 11, 12, 34, 0, tzinfo=UTC)


# ── History: reading ──────────────────────────────────────────────────


def history_row(bucket_at: datetime, *, cpu: float | None = 10.0) -> dict:
    """A stored history row as the read path returns it."""
    return {
        "bucket_at": bucket_at,
        "sampled_at": bucket_at + timedelta(seconds=20),
        "linux_boot_id": BOOT_ID,
        "cpu_percent": cpu,
        "cpu_logical_count": 32,
        "cpu_window_seconds": 30.0,
        "cpu_missing_reason": None if cpu is not None else "warming_up",
        "memory_total_bytes": 1024,
        "memory_available_bytes": 512,
        "memory_missing_reason": None,
    }


def test_a_missing_minute_is_an_explicit_null_point() -> None:
    """A gap is a statement about measurement, and zero would erase it."""
    since = datetime(2026, 9, 11, 12, 0, 0, tzinfo=UTC)
    until = since + timedelta(minutes=3)
    points = history_point_grid(
        rows=[history_row(since), history_row(since + timedelta(minutes=2))],
        since=since,
        until=until,
    )
    assert [point.sampled_at is None for point in points] == [False, True, False, True]
    assert points[1].cpu_percent is None
    assert points[1].bucket_at == since + timedelta(minutes=1)


def test_an_empty_window_is_still_a_full_grid_of_gaps() -> None:
    since = datetime(2026, 9, 11, 12, 0, 0, tzinfo=UTC)
    points = history_point_grid(rows=[], since=since, until=since + timedelta(minutes=1))
    assert len(points) == 2
    assert all(point.sampled_at is None for point in points)


def test_the_grid_carries_the_memory_derivation() -> None:
    since = datetime(2026, 9, 11, 12, 0, 0, tzinfo=UTC)
    points = history_point_grid(rows=[history_row(since)], since=since, until=since)
    assert points[0].memory_used_bytes == 512
    assert points[0].memory_used_percent == pytest.approx(50.0)


def test_rows_outside_the_window_are_ignored() -> None:
    since = datetime(2026, 9, 11, 12, 0, 0, tzinfo=UTC)
    points = history_point_grid(
        rows=[history_row(since - timedelta(hours=5)), history_row(since + timedelta(hours=5))],
        since=since,
        until=since + timedelta(minutes=1),
    )
    assert all(point.sampled_at is None for point in points)


def test_the_grid_is_bounded() -> None:
    since = datetime(2026, 9, 11, 0, 0, 0, tzinfo=UTC)
    points = history_point_grid(rows=[], since=since, until=since + timedelta(days=365))
    assert len(points) == MAX_HISTORY_POINTS


def test_a_cpu_gap_does_not_hide_the_memory_point() -> None:
    since = datetime(2026, 9, 11, 12, 0, 0, tzinfo=UTC)
    points = history_point_grid(rows=[history_row(since, cpu=None)], since=since, until=since)
    assert points[0].cpu_percent is None
    assert points[0].cpu_missing_reason == "warming_up"
    assert points[0].memory_used_percent == pytest.approx(50.0)


def test_the_window_is_floored_to_the_minute_by_the_servers_clock() -> None:
    now = datetime(2026, 9, 11, 12, 34, 56, tzinfo=UTC)
    since, until = history_window(now=now, seconds=HISTORY_RANGES["1h"])
    assert until == datetime(2026, 9, 11, 12, 34, 0, tzinfo=UTC)
    assert since == until - timedelta(hours=1)


def test_only_three_ranges_are_served() -> None:
    assert resolve_history_range("1h") == 3600
    assert resolve_history_range("6h") == 21600
    assert resolve_history_range("24h") == 86400
    assert resolve_history_range("7d") is None
    assert resolve_history_range("") is None


def test_the_history_response_names_what_it_is() -> None:
    now = datetime(2026, 9, 11, 12, 34, 0, tzinfo=UTC)
    response = AgentHostCapacityHistoryResponse(
        host="gpu-box",
        capacity_epoch="00000000-0000-4000-8000-0000000000ff",
        since=now - timedelta(hours=1),
        until=now,
        observed_at=now,
        points=[],
    )
    assert response.resolution_seconds == 60
    assert response.semantics == "sampled_observation"


def test_a_report_round_trips_through_the_stored_shape() -> None:
    """The stored JSONB must re-validate: the read path does exactly that."""
    report = HostCapacityReport.model_validate(capacity_block())
    stored = json.loads(json.dumps(report.model_dump(mode="json")))
    assert HostCapacityReport.model_validate(stored).inventory.running == 3


# ── Storage: a sub-block that can fail without taking the host with it ──

GIB = 1024**3


def storage_block(**overrides: object) -> dict:
    """A well-formed storage sub-block, with named fields replaceable."""
    block: dict = {
        "source": "linux_overlay_root_statvfs",
        "target": "/mnt/agent-docker",
        "total_bytes": 100 * GIB,
        "free_bytes": 40 * GIB,
        "available_bytes": 35 * GIB,
    }
    block.update(overrides)
    return block


def stored_view(block: dict, *, now: datetime | None = None):
    """Run one block through the parse/store/read path the pool actually uses."""
    read_at = datetime(2026, 9, 16, 8, 0, 0, tzinfo=UTC) if now is None else now
    report = parse_capacity_block(block)
    assert report is not None
    stored = json.loads(json.dumps(report.model_dump(mode="json")))
    return admin_capacity_view(
        snapshot=stored,
        received_at=read_at,
        sampled_at=read_at - timedelta(seconds=5),
        now=read_at,
    )


def test_a_storage_sub_block_is_stored_and_read_back_with_its_derivation() -> None:
    view = stored_view(capacity_block(storage=storage_block()))

    assert view is not None
    assert view.storage_source == "linux_overlay_root_statvfs"
    assert view.storage_target == "/mnt/agent-docker"
    assert view.storage_total_bytes == 100 * GIB
    assert view.storage_free_bytes == 40 * GIB
    assert view.storage_available_bytes == 35 * GIB
    assert view.storage_used_bytes == 60 * GIB
    # df's ratio: used over used + available, not over total.
    assert view.storage_used_percent == pytest.approx(100 * 60 / 95)
    assert view.storage_missing_reason is None


def test_a_runner_that_reports_a_gap_keeps_its_own_reason() -> None:
    for reason in ("disabled", "unsupported", "unverified", "source_unavailable"):
        view = stored_view(
            capacity_block(storage=None, missing={"cpu": None, "memory": None, "storage": reason})
        )
        assert view is not None
        assert view.storage_total_bytes is None
        assert view.storage_missing_reason == reason


def test_a_runner_with_no_storage_capability_reads_as_not_reported() -> None:
    """An older runner, and every snapshot written before the field existed."""
    view = stored_view(capacity_block(missing={"cpu": None, "memory": None}))

    assert view is not None
    assert view.storage_missing_reason == "not_reported"
    assert view.storage_used_percent is None
    # The rest of the observation is untouched by the older runner's silence.
    assert view.memory_used_percent is not None
    assert view.inventory is not None


def test_an_older_snapshot_without_the_missing_map_member_reads_as_not_reported() -> None:
    """The stored document predates the key, not just the sub-block."""
    block = capacity_block()
    del block["missing"]["storage"]
    view = stored_view(block)

    assert view is not None
    assert view.storage_missing_reason == "not_reported"
    assert view.storage_source is None


@pytest.mark.parametrize(
    "storage",
    [
        storage_block(total_bytes=True),
        storage_block(total_bytes="107374182400"),
        storage_block(total_bytes=-1),
        storage_block(total_bytes=2**53),
        storage_block(available_bytes=50 * GIB),  # above free
        storage_block(free_bytes=200 * GIB),  # above total
        storage_block(source="Overlay Root"),
        storage_block(source=""),
        storage_block(source="x" * 65),
        storage_block(target="bad\x00target"),
        storage_block(target="t" * 257),
        {"source": "linux_overlay_root_statvfs", "total_bytes": 100 * GIB},
        "not-an-object",
    ],
)
def test_a_malformed_storage_sub_block_costs_only_the_storage_field(storage: object) -> None:
    """CPU, memory and the inventory are independent facts about a live host."""
    report = parse_capacity_block(capacity_block(storage=storage))

    assert report is not None
    assert report.storage is None
    assert report.missing.storage == "invalid_sample"
    assert report.cpu_percent == pytest.approx(37.2)
    assert report.memory_total_bytes == 137_438_953_472
    assert report.inventory.running == 3


def block_without_the_missing_map(**overrides: object) -> dict:
    """A block that carries no ``missing`` map at all, not merely an empty one."""
    block = capacity_block(**overrides)
    del block["missing"]
    return block


@pytest.mark.parametrize(
    "block",
    [
        block_without_the_missing_map(storage=storage_block(total_bytes="lots")),
        capacity_block(storage=storage_block(total_bytes="lots"), missing="garbage"),
    ],
)
def test_a_storage_fault_does_not_rescue_an_unusable_missing_map(block: object) -> None:
    """Degrading the storage field must not make an unrelated block acceptable.

    ``missing`` is required, and a block whose map is absent or is not an
    object is dropped whatever its storage says. A map this control plane
    invented in its place would buy the report an acceptance that the runner's
    own CPU and memory reasons never paid for.
    """
    assert parse_capacity_block(block) is None


def test_a_malformed_storage_sub_block_does_not_refuse_the_heartbeat() -> None:
    request = HostHeartbeatRequest.model_validate(
        heartbeat_body(capacity_block(storage=storage_block(total_bytes="lots")))
    )

    assert request.capacity is not None
    assert request.capacity.storage is None
    assert request.capacity.missing.storage == "invalid_sample"
    assert request.slots_total == 3


def test_a_sample_and_a_missing_reason_together_cost_only_the_storage() -> None:
    """A contradiction in the storage half is not a malformed heartbeat.

    The two statements cannot both be true, so neither is published — but the
    CPU, the memory and the inventory beside them are facts about a live host,
    and a monitoring field does not get to take them down.
    """
    block = capacity_block(
        storage=storage_block(), missing={"cpu": None, "memory": None, "storage": "unverified"}
    )

    report = parse_capacity_block(block)

    assert report is not None
    assert report.storage is None
    assert report.missing.storage == "invalid_sample"
    assert report.cpu_percent == pytest.approx(37.2)
    assert report.inventory.stopped == 7


@pytest.mark.parametrize("reason", ["teleported", "DISABLED", 42, ["unverified"], {"a": 1}])
def test_a_reason_outside_the_vocabulary_costs_only_the_storage(reason: object) -> None:
    """A newer runner's new reason must not cost an older control plane the CPU.

    The page would render an unknown string as "Not measured" anyway, so the
    closed vocabulary is enforced by degrading the field rather than by failing
    the model — which would take three unrelated measurements down with it.
    """
    report = parse_capacity_block(
        capacity_block(storage=None, missing={"cpu": None, "memory": None, "storage": reason})
    )

    assert report is not None
    assert report.storage is None
    assert report.missing.storage == "invalid_sample"
    assert report.memory_total_bytes == 137_438_953_472


def test_a_storage_fault_never_refuses_the_heartbeat() -> None:
    request = HostHeartbeatRequest.model_validate(
        heartbeat_body(capacity_block(storage=storage_block(), missing={"storage": "warped"}))
    )

    assert request.capacity is not None
    assert request.capacity.missing.storage == "invalid_sample"
    assert request.slots_total == 3


def test_an_unknown_source_is_accepted_as_long_as_it_fits_the_format() -> None:
    """A new probe must not need a control-plane release to be readable."""
    view = stored_view(capacity_block(storage=storage_block(source="windows_volume_statvfs")))

    assert view is not None
    assert view.storage_source == "windows_volume_statvfs"


def test_a_filesystem_with_no_denominator_is_an_invalid_sample() -> None:
    """Every block reserved and none written: no ratio exists, so none is drawn."""
    view = stored_view(
        capacity_block(storage=storage_block(free_bytes=100 * GIB, available_bytes=0))
    )

    assert view is not None
    assert view.storage_missing_reason == "invalid_sample"
    assert view.storage_used_bytes is None
    assert view.storage_used_percent is None
    assert view.storage_total_bytes is None
    assert view.storage_source is None


def test_the_storage_sub_block_stays_inside_the_eight_kib_ceiling() -> None:
    block = capacity_block(storage=storage_block(target="/" + "m" * 254))
    encoded = json.dumps(block, ensure_ascii=False).encode("utf-8")

    assert len(encoded) < MAX_CAPACITY_BYTES
    assert parse_capacity_block(block) is not None


def test_history_ignores_the_storage_sub_block() -> None:
    """No migration and no new columns: the point carries fixed fields."""
    block = capacity_block(storage=storage_block())
    assert parse_capacity_block(block) is not None

    point = history_point_write_from_block(block, sampled_at=SAMPLED_AT, sample_id=SAMPLE_ID)

    assert point is not None
    assert point.memory_total_bytes == 137_438_953_472
    assert not hasattr(point, "storage_total_bytes")
