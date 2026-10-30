"""Runtime accounting, sessions, and replay behavior for the job router.

These cases reuse the route suite's in-memory store while keeping operational
telemetry and session lifecycle independently schedulable.
"""

from __future__ import annotations

import hashlib
from types import SimpleNamespace
from unittest.mock import call

# Imported pytest fixtures are intentionally named by test parameters.
# ruff: noqa: F811
from tests.servers.test_agent_jobs_api import (  # noqa: F401 - shared pytest fixtures
    _OTHER,
    _OWNER,
    UTC,
    Any,
    ASGITransport,
    AsyncClient,
    AsyncMock,
    FakeStorage,
    _api_key_secret,
    _attach_shared_workspace,
    _build_app,
    _create_job,
    _next_turn,
    _send,
    _thread_of,
    asyncio,
    client,
    datetime,
    json,
    pytest,
    sandbox_manager_routing,
    store,
    timedelta,
    worker,
)
from tests.servers.test_agent_jobs_mcp_api import _binding, _job_body
from tests.support.owners import connection_claim, on_connection

from cloud_agent.application.follow_ups import PreparedFollowUp

pytestmark = pytest.mark.asyncio


async def test_the_owner_sees_what_their_turn_consumed(client: AsyncClient, store: FakeStorage):
    """The footer's numbers, end to end: runner posts, owner reads."""
    job_id = await _create_job(client)
    claim = (
        await client.post("/v1/agent/worker/claim", json=connection_claim({"worker_id": "w1"}))
    ).json()
    auth = {"Authorization": f"Bearer {claim['worker_token']}"}

    ack = await client.post(
        f"/v1/agent/worker/jobs/{job_id}/resources",
        json={
            "cpu_usec": 18_200_000,
            "mem_peak_bytes": 1_288_490_188,
            "net_rx_bytes": 35_651_584,
            "net_tx_bytes": 2_097_152,
            "sample_count": 42,
            "coverage": 1.0,
            "sampler_version": "1",
        },
        headers=auth,
    )
    assert ack.status_code == 200

    job = (await client.get(f"/v1/agent/jobs/{job_id}")).json()
    assert job["resources"]["cpu_usec"] == 18_200_000
    assert job["resources"]["mem_peak_bytes"] == 1_288_490_188
    assert job["resources"]["coverage"] == 1.0


async def test_shared_member_accounting_refuses_container_attribution(
    client: AsyncClient,
):
    """The semantic version is a server-enforced promise, not runner trust.

    Fork siblings can overlap in one container.  Accepting either an aggregate
    delta or a periodic tool sample under ``shared-member-v1`` would silently
    charge one chat for the other's work.
    """
    job_id = await _create_job(client)
    claim = (
        await client.post("/v1/agent/worker/claim", json=connection_claim({"worker_id": "w1"}))
    ).json()
    auth = {"Authorization": f"Bearer {claim['worker_token']}"}
    endpoint = f"/v1/agent/worker/jobs/{job_id}/resources"
    base = {
        "sample_count": 0,
        "coverage": 0.0,
        "sampler_version": "shared-member-v1",
    }

    invalid = [
        {**base, "cpu_usec": 1},
        {**base, "sample_count": 1},
        {
            **base,
            "tool_calls": [
                {
                    "call_seq": 1,
                    "tool_use_id": "toolu_1",
                    "tool_name": "Bash",
                    "sample_count": 2,
                    "measured_span_ms": 1,
                    "cpu_usec_exclusive": 1,
                }
            ],
        },
    ]
    for payload in invalid:
        refused = await client.post(endpoint, json=payload, headers=auth)
        assert refused.status_code == 422, refused.text

    accepted = await client.post(
        endpoint,
        json={
            **base,
            "tool_time_ms": 25,
            "tool_calls": [
                {
                    "call_seq": 1,
                    "tool_use_id": "toolu_1",
                    "tool_name": "Bash",
                    "duration_ms": 25,
                    "sample_count": 0,
                    "overlap_n": 1,
                }
            ],
        },
        headers=auth,
    )
    assert accepted.status_code == 200, accepted.text


async def test_resource_sequences_and_process_references_are_consistent(
    client: AsyncClient,
):
    """Malformed worker evidence is a 422, never a database primary-key 500."""
    job_id = await _create_job(client)
    claim = (
        await client.post("/v1/agent/worker/claim", json=connection_claim({"worker_id": "w1"}))
    ).json()
    auth = {"Authorization": f"Bearer {claim['worker_token']}"}
    endpoint = f"/v1/agent/worker/jobs/{job_id}/resources"
    call = {
        "call_seq": 1,
        "tool_use_id": "toolu_1",
        "tool_name": "Bash",
    }
    process = {
        "process_seq": 1,
        "uid": 11003,
        "pid": 42,
        "tgid": 42,
        "comm": "tiny",
        "started_offset_ms": 1,
        "tool_call_seqs": [1],
    }
    base = {
        "sampler_version": "shared-member-v1",
        "process_observer": "kata-guest-ebpf-v1",
    }
    invalid = [
        {**base, "tool_calls": [call, {**call, "tool_use_id": "toolu_2"}]},
        {
            **base,
            "tool_calls": [{**call, "process_count": 2}],
            "processes": [process, {**process, "pid": 43, "tgid": 43}],
        },
        {
            **base,
            "tool_calls": [{**call, "process_count": 1}],
            "processes": [{**process, "tool_call_seqs": [1, 1]}],
        },
        {**base, "tool_calls": [call], "processes": [{**process, "tool_call_seqs": [2]}]},
        {**base, "tool_calls": [{**call, "process_count": 1}]},
    ]

    for payload in invalid:
        refused = await client.post(endpoint, json=payload, headers=auth)
        assert refused.status_code == 422, refused.text


async def test_per_call_rows_survive_the_wire_in_both_directions(
    client: AsyncClient, store: FakeStorage
):
    """Posted by the runner, read by the owner, over the real request models.

    Written against the wire rather than the store because that is where this
    kind of field is lost: a model missing the attribute accepts the post,
    answers 200, and drops it silently — which no test that hands a dict to
    the store can see.
    """
    job_id = await _create_job(client)
    claim = (
        await client.post("/v1/agent/worker/claim", json=connection_claim({"worker_id": "w1"}))
    ).json()
    auth = {"Authorization": f"Bearer {claim['worker_token']}"}

    ack = await client.post(
        f"/v1/agent/worker/jobs/{job_id}/resources",
        json={
            "cpu_usec": 5_000_000,
            "sample_count": 12,
            "coverage": 1.0,
            "sampler_version": "2",
            "tool_time_ms": 9_543,
            "tool_calls": [
                {
                    "call_seq": 1,
                    "tool_use_id": "toolu_long",
                    "tool_name": "Bash",
                    "timing_source": "exact",
                    "duration_ms": 9_523,
                    "sample_count": 9,
                    "overlap_n": 1,
                    "measured_span_ms": 8_000,
                    "cpu_usec_exclusive": 4_400_000,
                    "mem_peak_exclusive_bytes": 270_000_000,
                    "process_count": 1,
                    "process_cpu_usec": 15_000,
                    "process_mem_peak_bytes": 2_228_224,
                    "process_io_read_bytes": 0,
                    "process_io_write_bytes": 8_388_608,
                    "process_resources_partial": True,
                },
                {
                    "call_seq": 2,
                    "tool_use_id": "toolu_short",
                    "tool_name": "Read",
                    "duration_ms": 20,
                    "sample_count": 0,
                    "overlap_n": 1,
                },
            ],
            "tool_calls_truncated": False,
            "process_observer": "kata-guest-ebpf-v1",
            "process_observation_complete": True,
            "process_events_lost": 0,
            "processes": [
                {
                    "process_seq": 1,
                    "uid": 11003,
                    "pid": 42,
                    "tgid": 42,
                    "ppid": 7,
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
        headers=auth,
    )
    assert ack.status_code == 200, ack.text

    job = (await client.get(f"/v1/agent/jobs/{job_id}")).json()
    assert job["telemetry_attempt_id"] == claim["attempt_id"]
    resources = job["resources"]
    assert resources["tool_calls_truncated"] is False
    assert resources["tool_time_ms"] == 9_543
    long_call, short_call = resources["tool_calls"]
    assert long_call["tool_name"] == "Bash"
    assert long_call["timing_source"] == "exact"
    assert short_call["timing_source"] == "message", "an unmarked call claimed precision"
    assert long_call["cpu_usec_exclusive"] == 4_400_000
    assert long_call["measured_span_ms"] == 8_000
    assert long_call["process_count"] == 1
    assert long_call["process_cpu_usec"] == 15_000
    assert long_call["process_mem_peak_bytes"] == 2_228_224
    assert long_call["process_io_write_bytes"] == 8_388_608
    assert long_call["process_resources_partial"] is True
    assert short_call["process_cpu_usec"] is None
    assert short_call["process_resources_partial"] is False
    assert resources["process_observer"] == "kata-guest-ebpf-v1"
    assert resources["process_observation_complete"] is True
    assert resources["process_events_lost"] == 0
    (process,) = resources["processes"]
    assert process["executable"] == "/bin/tiny"
    assert process["duration_ms"] == 1
    assert process["tool_call_seqs"] == [1]
    assert process["cpu_usec"] == 15_000
    assert process["mem_peak_bytes"] == 2_228_224
    assert process["io_write_bytes"] == 8_388_608
    assert process["io_rchar_bytes"] == 8_390_683
    # The 97 % case, all the way to the reader: a row, a duration, and no
    # counters at all. A zero here would be a number nobody measured.
    assert short_call["sample_count"] == 0
    assert short_call["cpu_usec_exclusive"] is None
    assert short_call["cpu_usec_shared"] is None
    assert short_call["mem_peak_exclusive_bytes"] is None


async def test_a_turn_with_no_per_call_rows_reads_as_an_empty_list(
    client: AsyncClient, store: FakeStorage
):
    """Every turn recorded before this shipped, and every unmeasured one."""
    job_id = await _create_job(client)
    claim = (
        await client.post("/v1/agent/worker/claim", json=connection_claim({"worker_id": "w1"}))
    ).json()
    auth = {"Authorization": f"Bearer {claim['worker_token']}"}
    await client.post(
        f"/v1/agent/worker/jobs/{job_id}/resources",
        json={"sample_count": 0, "coverage": 0.0, "sampler_version": "2"},
        headers=auth,
    )

    resources = (await client.get(f"/v1/agent/jobs/{job_id}")).json()["resources"]
    assert resources["tool_calls"] == []
    assert resources["tool_calls_truncated"] is False
    # Null, not zero: a turn nobody measured did not spend no time in tools.
    assert resources["tool_time_ms"] is None


async def test_more_calls_than_the_cap_are_refused_rather_than_truncated_server_side(
    client: AsyncClient, store: FakeStorage
):
    """The runner caps and flags; the server must not quietly cap a second time."""
    job_id = await _create_job(client)
    claim = (
        await client.post("/v1/agent/worker/claim", json=connection_claim({"worker_id": "w1"}))
    ).json()
    auth = {"Authorization": f"Bearer {claim['worker_token']}"}

    response = await client.post(
        f"/v1/agent/worker/jobs/{job_id}/resources",
        json={
            "sample_count": 1,
            "coverage": 1.0,
            "sampler_version": "2",
            "tool_calls": [
                {"call_seq": i + 1, "tool_use_id": f"t{i}", "tool_name": "Read"} for i in range(501)
            ],
        },
        headers=auth,
    )
    assert response.status_code == 422


async def test_a_rollup_is_the_same_window_said_twice_not_two_windows(
    client: AsyncClient, store: FakeStorage
):
    """Retrying the post must not double a turn's consumption."""
    job_id = await _create_job(client)
    claim = (
        await client.post("/v1/agent/worker/claim", json=connection_claim({"worker_id": "w1"}))
    ).json()
    auth = {"Authorization": f"Bearer {claim['worker_token']}"}
    body = {"cpu_usec": 1_000, "sample_count": 5, "coverage": 1.0, "sampler_version": "1"}

    await client.post(f"/v1/agent/worker/jobs/{job_id}/resources", json=body, headers=auth)
    await client.post(f"/v1/agent/worker/jobs/{job_id}/resources", json=body, headers=auth)

    job = (await client.get(f"/v1/agent/jobs/{job_id}")).json()
    assert job["resources"]["cpu_usec"] == 1_000, "a retried post doubled the turn's CPU"


async def test_a_superseded_attempt_cannot_relabel_what_the_live_one_used(
    client: AsyncClient, store: FakeStorage
):
    """Fenced like every other worker write."""
    job_id = await _create_job(client)
    claim = (
        await client.post("/v1/agent/worker/claim", json=connection_claim({"worker_id": "w1"}))
    ).json()
    auth = {"Authorization": f"Bearer {claim['worker_token']}"}
    # A takeover moves the fence to a new attempt generation.
    store.live_fence = (101, 2)

    refused = await client.post(
        f"/v1/agent/worker/jobs/{job_id}/resources",
        json={"cpu_usec": 999, "sample_count": 1, "coverage": 1.0, "sampler_version": "1"},
        headers=auth,
    )
    assert refused.status_code == 409

    job = (await client.get(f"/v1/agent/jobs/{job_id}")).json()
    assert job["resources"] is None


def _turn_resources_body(
    *,
    report_seq: int = 1,
    state: str = "final",
    measurement_id: str = "00000000-0000-4000-8000-000000000001",
    cpu_usec: int = 80_000_000,
    observed_through_ms: int = 130_000,
) -> dict[str, Any]:
    """One canonical ``member_execution_window`` body, as the guest posts it."""
    return {
        "schema_version": 1,
        "measurement_id": measurement_id,
        "report_seq": report_seq,
        "state": state,
        "scope": "member_execution_window",
        "source": "kata-guest-member-window-v1",
        "window": {
            "duration_ms": 130_000,
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
            "peak_bytes": 1_288_490_189,
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


async def test_a_turn_resource_snapshot_is_acknowledged_and_its_retry_too(
    client: AsyncClient, store: FakeStorage
):
    """An acknowledged checkpoint whose response was lost must be safe to resend."""
    job_id = await _create_job(client)
    claim = (
        await client.post("/v1/agent/worker/claim", json=connection_claim({"worker_id": "w1"}))
    ).json()
    auth = {"Authorization": f"Bearer {claim['worker_token']}"}
    endpoint = f"/v1/agent/worker/jobs/{job_id}/turn-resources"
    body = _turn_resources_body(report_seq=2)

    first = await client.post(endpoint, json=body, headers=auth)
    retry = await client.post(endpoint, json=body, headers=auth)

    assert first.status_code == 200
    assert first.json()["ok"] is True
    assert retry.status_code == 200
    assert retry.json()["ok"] is True
    assert store.turn_summaries[claim["attempt_id"]]["seq"] == 2


async def test_a_stale_or_disagreeing_snapshot_is_refused_with_409(
    client: AsyncClient, store: FakeStorage
):
    """``conflict`` is not ``lease_lost``: the worker keeps going, the post does not."""
    job_id = await _create_job(client)
    claim = (
        await client.post("/v1/agent/worker/claim", json=connection_claim({"worker_id": "w1"}))
    ).json()
    auth = {"Authorization": f"Bearer {claim['worker_token']}"}
    endpoint = f"/v1/agent/worker/jobs/{job_id}/turn-resources"
    assert (
        await client.post(endpoint, json=_turn_resources_body(report_seq=5), headers=auth)
    ).status_code == 200

    stale = await client.post(endpoint, json=_turn_resources_body(report_seq=4), headers=auth)
    disagreeing = await client.post(
        endpoint, json=_turn_resources_body(report_seq=5, cpu_usec=1), headers=auth
    )

    assert stale.status_code == 409
    assert stale.json()["detail"]["error"]["type"] == "turn_summary_conflict"
    assert disagreeing.status_code == 409
    assert disagreeing.json()["detail"]["error"]["type"] == "turn_summary_conflict"
    assert store.turn_summaries[claim["attempt_id"]]["seq"] == 5


async def test_a_superseded_attempt_cannot_file_a_turn_resource_snapshot(
    client: AsyncClient, store: FakeStorage
):
    """The fence is the same one every other worker write uses."""
    job_id = await _create_job(client)
    claim = (
        await client.post("/v1/agent/worker/claim", json=connection_claim({"worker_id": "w1"}))
    ).json()
    auth = {"Authorization": f"Bearer {claim['worker_token']}"}
    store.live_fence = (101, 2)

    refused = await client.post(
        f"/v1/agent/worker/jobs/{job_id}/turn-resources",
        json=_turn_resources_body(),
        headers=auth,
    )

    assert refused.status_code == 409
    assert refused.json()["detail"]["error"]["type"] == "lease_lost"
    assert store.turn_summaries == {}


async def test_an_oversized_turn_resource_snapshot_is_refused_before_parsing(
    client: AsyncClient, store: FakeStorage
):
    """The 16 KiB cap is on the bytes that crossed the wire, not on the model.

    The body carries a valid snapshot plus one long unknown field, so a server
    that validated first and bounded second would answer 422 or 200 here. The
    only correct answer is 413: the contract bounds the payload itself.
    """
    job_id = await _create_job(client)
    claim = (
        await client.post("/v1/agent/worker/claim", json=connection_claim({"worker_id": "w1"}))
    ).json()
    auth = {"Authorization": f"Bearer {claim['worker_token']}"}
    body = _turn_resources_body()
    body["padding"] = "x" * (16 * 1024)

    refused = await client.post(
        f"/v1/agent/worker/jobs/{job_id}/turn-resources", json=body, headers=auth
    )

    assert refused.status_code == 413
    assert refused.json()["detail"]["error"]["type"] == "payload_too_large"
    assert store.turn_summaries == {}


async def test_a_turn_resource_response_never_echoes_body_identity(
    client: AsyncClient, store: FakeStorage
):
    """Identity comes from the signed claims, so the body cannot name anything.

    A body that tries to select another attempt must be stored against the
    caller's own fenced attempt and answered with no identity at all.
    """
    job_id = await _create_job(client)
    claim = (
        await client.post("/v1/agent/worker/claim", json=connection_claim({"worker_id": "w1"}))
    ).json()
    auth = {"Authorization": f"Bearer {claim['worker_token']}"}
    body = _turn_resources_body()
    body.update({"attempt_id": 999, "lease_generation": 7, "job_id": "ajob_someone_else"})

    response = await client.post(
        f"/v1/agent/worker/jobs/{job_id}/turn-resources", json=body, headers=auth
    )

    assert response.status_code == 200
    assert response.json()["ok"] is True
    assert set(store.turn_summaries) == {claim["attempt_id"]}
    assert "job_id" not in response.text
    assert "attempt_id" not in response.text


async def test_an_unmeasured_turn_reports_nothing_rather_than_zero(
    client: AsyncClient, store: FakeStorage
):
    """The distinction the whole feature rests on.

    A turn nobody measured has no resources object at all. Reading zeros here
    would be indistinguishable from a turn that genuinely consumed nothing —
    and the reader would have no way to tell which they were looking at.
    """
    job_id = await _create_job(client)

    job = (await client.get(f"/v1/agent/jobs/{job_id}")).json()

    assert job["telemetry_attempt_id"] is None
    assert job["resources"] is None
    assert job["continuity_mode"] is None
    assert job["started_at"] is None
    assert job["finished_at"] is None
    assert job["duration_ms"] is None


@pytest.mark.parametrize("state", ["running", "failed"])
async def test_job_timing_does_not_invent_a_missing_attempt_finish(
    client: AsyncClient, store: FakeStorage, state: str
):
    """A job update or terminal state cannot supply an unrecorded end time."""
    job_id = await _create_job(client)
    started = datetime(2026, 9, 3, 10, 0, tzinfo=UTC)
    store.jobs[job_id].update(
        state=state,
        created_at=started - timedelta(minutes=1),
        updated_at=started + timedelta(minutes=5),
    )
    store.attempt_telemetry[job_id] = [
        {
            "attempt_id": 100,
            "attempt_no": 1,
            "started_at": started,
            "finished_at": None,
        }
    ]

    for query in ("", "?include_resources=false"):
        response = await client.get(f"/v1/agent/jobs/{job_id}{query}")
        assert response.status_code == 200
        job = response.json()
        assert job["started_at"] == started.isoformat()
        assert job["finished_at"] is None
        assert job["duration_ms"] is None


async def test_job_list_and_thread_do_not_substitute_job_dates_for_execution_timing(
    client: AsyncClient, store: FakeStorage
):
    """Paths without a telemetry read leave the execution window unknown."""
    job_id = await _create_job(client)
    started = datetime(2026, 9, 3, 10, 0, tzinfo=UTC)
    store.jobs[job_id].update(
        state="succeeded",
        created_at=started - timedelta(minutes=1),
        updated_at=started + timedelta(minutes=5),
    )
    store.attempt_telemetry[job_id] = [
        {
            "attempt_id": 100,
            "attempt_no": 1,
            "started_at": started,
            "finished_at": started + timedelta(seconds=9),
        }
    ]

    for path in ("/v1/agent/jobs", f"/v1/agent/jobs/{job_id}/thread"):
        response = await client.get(path)
        assert response.status_code == 200
        job = response.json()["jobs"][0]
        assert job["started_at"] is None
        assert job["finished_at"] is None
        assert job["duration_ms"] is None


async def test_job_detail_keeps_each_retry_s_telemetry_on_its_attempt(
    client: AsyncClient, store: FakeStorage
):
    """Selecting an old transcript must never show the retry's measurements."""
    job_id = await _create_job(client)
    started = datetime(2026, 9, 3, 10, 0, tzinfo=UTC)
    newest_started = started + timedelta(seconds=5)
    store.attempt_telemetry = {
        job_id: [
            {
                "attempt_id": 100,
                "attempt_no": 1,
                "continuity_mode": "replay",
                "context_status": "lost",
                "started_at": started,
                "finished_at": started + timedelta(seconds=3),
                "sampler_version": "2",
                "cpu_usec": 1_000_000,
                "sample_count": 3,
                "coverage": 1.0,
            },
            {
                "attempt_id": 101,
                "attempt_no": 2,
                "continuity_mode": "warm_native",
                "context_status": "restored",
                "started_at": newest_started,
                "finished_at": newest_started + timedelta(seconds=9),
                "sampler_version": "2",
                "cpu_usec": 7_000_000,
                "sample_count": 9,
                "coverage": 1.0,
            },
        ]
    }

    job = (await client.get(f"/v1/agent/jobs/{job_id}")).json()

    first, second = job["attempt_telemetry"]
    assert (first["attempt_id"], first["attempt_no"]) == (100, 1)
    assert first["duration_ms"] == 3_000
    assert second["duration_ms"] == 9_000
    assert "resources" not in first and "resources" not in second
    # Existing clients still receive the newest attempt at the top level.
    assert job["telemetry_attempt_id"] == 101
    assert job["started_at"] == newest_started.isoformat()
    assert job["finished_at"] == (newest_started + timedelta(seconds=9)).isoformat()
    assert job["duration_ms"] == 9_000
    assert job["resources"]["cpu_usec"] == 7_000_000

    lightweight = (await client.get(f"/v1/agent/jobs/{job_id}?include_resources=false")).json()
    assert lightweight["started_at"] == job["started_at"]
    assert lightweight["finished_at"] == job["finished_at"]
    assert lightweight["duration_ms"] == job["duration_ms"]

    old = (await client.get(f"/v1/agent/jobs/{job_id}/attempts/100/telemetry")).json()
    assert old["attempt_id"] == 100
    assert old["duration_ms"] == 3_000
    assert old["resources"]["cpu_usec"] == 1_000_000


async def test_retry_picker_polls_only_summaries_and_fetches_one_detail(
    client: AsyncClient, store: FakeStorage, monkeypatch
):
    """Adding retries must not multiply the polled tool/process payload."""
    job_id = await _create_job(client)
    store.attempt_telemetry[job_id] = [
        {
            "attempt_id": attempt_id,
            "attempt_no": attempt_id,
            "sampler_version": "2",
            "sample_count": 1,
            "coverage": 1.0,
            "cpu_usec": attempt_id,
            "tool_calls": [
                {"call_seq": seq + 1, "tool_use_id": f"t{seq}", "tool_name": "Read"}
                for seq in range(500)
            ],
        }
        for attempt_id in range(1, 13)
    ]
    read_detail = AsyncMock(wraps=store.job_turn_telemetry)
    monkeypatch.setattr(store, "job_turn_telemetry", read_detail)
    # The real store already returns scalar summaries, independently of the
    # detail reader. Keep the fake faithful to that separate query.
    summaries = [
        {key: row[key] for key in ("attempt_id", "attempt_no")}
        for row in store.attempt_telemetry[job_id]
    ]
    monkeypatch.setattr(store, "job_attempts_telemetry", AsyncMock(return_value=summaries))
    for _ in range(2):
        response = await client.get(f"/v1/agent/jobs/{job_id}?include_resources=false")
        assert response.status_code == 200
        data = response.json()
        assert len(data["attempt_telemetry"]) == 12
        assert data["resources"] is None
        assert b'"tool_calls"' not in response.content
        assert b'"processes"' not in response.content
        assert len(response.content) < 5_000
    read_detail.assert_not_awaited()
    # Supply the full row only when the singular reader is called.
    read_detail.side_effect = lambda **kwargs: store.attempt_telemetry[job_id][
        kwargs["attempt_id"] - 1
    ]
    detail = await client.get(f"/v1/agent/jobs/{job_id}/attempts/3/telemetry")
    assert detail.status_code == 200
    assert len(detail.json()["resources"]["tool_calls"]) == 500
    read_detail.assert_awaited_once_with(job_id=job_id, attempt_id=3)


async def test_attempt_telemetry_is_fenced_to_the_owned_job(
    client: AsyncClient, store: FakeStorage
):
    job_id = await _create_job(client)
    another = await _create_job(client)
    store.attempt_telemetry[another] = [{"attempt_id": 999, "attempt_no": 1}]
    assert (await client.get(f"/v1/agent/jobs/{job_id}/attempts/999/telemetry")).status_code == 404
    store.jobs[another]["user_id"] = "someone-else"
    assert (await client.get(f"/v1/agent/jobs/{another}/attempts/999/telemetry")).status_code == 404


# ── Reasoning effort ───────────────────────────────────────────────────


async def test_a_turn_without_an_effort_is_auto(client: AsyncClient):
    """Auto is null, and null is what every turn before this control reports."""
    job_id = await _create_job(client)

    job = (await client.get(f"/v1/agent/jobs/{job_id}")).json()

    assert job["reasoning_effort"] is None


async def test_every_runtime_still_takes_ordinary_turns(client: AsyncClient):
    """And nothing else about them changed: Auto is every turn they ever ran."""
    for runtime in ("pi", "opencode", "kilo", "codex"):
        response = await client.post(
            "/v1/agent/jobs",
            json=on_connection(
                {
                    "repo": "owner/name",
                    "task_prompt": "fix it",
                    "runtime": runtime,
                    "model": "glm-5.1",
                }
            ),
        )
        assert response.status_code == 201, f"{runtime}: {response.text}"
        assert response.json()["reasoning_effort"] is None


async def test_a_thread_read_returns_what_earlier_turns_did(
    client: AsyncClient, store: FakeStorage
):
    """A follow-up must not make the previous turn's tool activity unreachable."""
    first_id = await _create_job(client)
    claim = (
        await client.post("/v1/agent/worker/claim", json=connection_claim({"worker_id": "w1"}))
    ).json()
    auth = {"Authorization": f"Bearer {claim['worker_token']}"}
    await client.post(
        f"/v1/agent/worker/jobs/{first_id}/events",
        json={"event_type": "tool_use", "payload": {"id": "t1", "name": "Bash"}},
        headers=auth,
    )
    await client.post(
        f"/v1/agent/worker/jobs/{first_id}/events",
        json={"event_type": "message", "payload": {"text": "done"}},
        headers=auth,
    )
    # With the worker's credential: the first turn has to have finished for
    # the message below to become the next turn rather than wait for it.
    finished = await client.post(
        f"/v1/agent/worker/jobs/{first_id}/finish", json={"state": "succeeded"}, headers=auth
    )
    assert finished.status_code == 200
    second = await _send(client, store, first_id, {"prompt": "and now the docs"})
    second_id = second.json()["job"]["id"]

    per_job = (await client.get(f"/v1/agent/jobs/{second_id}/events")).json()
    assert per_job["events"] == []

    thread = (await client.get(f"/v1/agent/jobs/{second_id}/thread/events")).json()
    assert [event["event_type"] for event in thread["events"]] == ["tool_use", "message"]
    assert {event["turn_no"] for event in thread["events"]} == {1}
    assert thread["events"][0]["payload"]["name"] == "Bash"


async def test_a_thread_event_read_refuses_a_foreign_job(client: AsyncClient, store: FakeStorage):
    job_id = await _create_job(client)
    store.jobs[job_id]["user_id"] = "someone-else"

    response = await client.get(f"/v1/agent/jobs/{job_id}/thread/events")

    assert response.status_code == 404


@pytest.mark.parametrize("session_state", ["queued", "running", "idle", "failed"])
async def test_session_listing_exposes_stable_titles_facets_and_archive_state(
    client: AsyncClient, store: FakeStorage, monkeypatch, session_state: str
):
    job_id = await _create_job(client)
    await store.conversation_commands.set_thread_archived(
        job_id=job_id, user_id=_OWNER, archived=True
    )
    job = await store.conversation_queries.get_job(job_id)
    assert job is not None
    listing = AsyncMock(
        return_value={
            "sessions": [
                {
                    "thread_id": job["thread_id"],
                    "title": "Stable conversation title",
                    "archived_at": job["archived_at"],
                    "job": job,
                    "can_delete": False,
                    "delete_block_reason": "fork_in_progress",
                }
            ],
            "total": 51,
            "next_cursor": "opaque-next-page",
            "projects": [{"repo": "owner/name", "count": 51}],
        }
    )
    monkeypatch.setattr(store, "list_sessions", listing, raising=False)

    response = await client.get(
        "/v1/agent/sessions",
        params={
            "archived": "true",
            "search": "  Stable  ",
            "repo": "owner/name",
            "state": session_state,
            "limit": 20,
            "cursor": "previous-page",
        },
    )
    assert response.status_code == 200
    body = response.json()
    assert body["sessions"][0]["title"] == "Stable conversation title"
    assert body["sessions"][0]["archived_at"] == body["sessions"][0]["job"]["archived_at"]
    assert body["sessions"][0]["delete_block_reason"] == "fork_in_progress"
    assert body["total"] == 51 and body["next_cursor"] == "opaque-next-page"
    assert body["projects"] == [{"repo": "owner/name", "count": 51}]
    listing.assert_awaited_once_with(
        user_id=_OWNER,
        archived=True,
        search="Stable",
        repo="owner/name",
        state=session_state,
        limit=20,
        cursor="previous-page",
        include_summary=True,
        temporary=False,
    )


async def test_job_response_preserves_turn_result_beside_conversation_activity(
    client: AsyncClient, store: FakeStorage
):
    job_id = await _create_job(client)
    store.jobs[job_id].update(state="cancelled", conversation_state="running")
    response = await client.get(f"/v1/agent/jobs/{job_id}")
    assert response.status_code == 200
    assert response.json()["state"] == "cancelled"
    assert response.json()["conversation_state"] == "running"


async def test_session_list_can_omit_summary(client: AsyncClient, store: FakeStorage, monkeypatch):
    listing = AsyncMock(
        return_value={"sessions": [], "total": None, "projects": None, "next_cursor": None}
    )
    monkeypatch.setattr(store, "list_sessions", listing, raising=False)
    response = await client.get("/v1/agent/sessions?include_summary=false&repo=owner/name")
    assert response.status_code == 200
    assert response.json() == {"sessions": [], "total": None, "projects": None, "next_cursor": None}
    assert listing.call_args.kwargs["include_summary"] is False
    assert listing.call_args.kwargs["repo"] == "owner/name"


async def test_session_list_rejects_bad_cursor_and_display_filters(
    client: AsyncClient, store: FakeStorage, monkeypatch
):
    listing = AsyncMock(side_effect=ValueError("Invalid session cursor for these filters"))
    monkeypatch.setattr(store, "list_sessions", listing, raising=False)
    assert (await client.get("/v1/agent/sessions?cursor=bad")).status_code == 400
    for params in ({"state": "succeeded"}, {"limit": 201}, {"search": "bad\x00search"}):
        assert (await client.get("/v1/agent/sessions", params=params)).status_code == 422
    assert listing.await_count == 1


async def test_session_mutations_use_explicit_owner_snapshot(
    client: AsyncClient, store: FakeStorage, monkeypatch
):
    thread_ids = ["athr_1", "athr_2"]
    archive = AsyncMock(
        return_value={
            "sessions": [{"thread_id": value, "archived_at": None} for value in thread_ids]
        }
    )
    delete = AsyncMock(return_value={"thread_ids": thread_ids})
    monkeypatch.setattr(store, "set_sessions_archived", archive, raising=False)
    monkeypatch.setattr(store, "delete_sessions", delete, raising=False)
    response = await client.post(
        "/v1/agent/sessions/archive", json={"thread_ids": thread_ids, "archived": False}
    )
    assert response.status_code == 200
    assert all(row["archived"] is False for row in response.json()["sessions"])
    archive.assert_awaited_once_with(thread_ids=thread_ids, user_id=_OWNER, archived=False)
    response = await client.post("/v1/agent/sessions/delete", json={"thread_ids": thread_ids})
    assert response.status_code == 200 and response.json() == {"thread_ids": thread_ids}
    delete.assert_awaited_once_with(thread_ids=thread_ids, user_id=_OWNER)


@pytest.mark.parametrize(
    ("error", "status"),
    [
        ("not_found", 404),
        ("fork_in_progress", 409),
        ("publication_pending", 409),
    ],
)
async def test_session_delete_conflicts_have_actionable_types(
    client: AsyncClient, store: FakeStorage, monkeypatch, error: str, status: int
):
    delete = AsyncMock(return_value={"error": error})
    monkeypatch.setattr(store, "delete_sessions", delete, raising=False)
    response = await client.post("/v1/agent/sessions/delete", json={"thread_ids": ["athr_1"]})
    assert response.status_code == status
    assert response.json()["detail"]["error"]["type"] == error


@pytest.mark.parametrize("manager_fails", [False, True])
async def test_session_delete_revokes_and_closes_released_residencies(
    store: FakeStorage, monkeypatch, manager_fails: bool
):
    """A deleted chat's credential ends now; its host is told to close the process."""
    released = [
        {"thread_id": "athr_1", "generation": 4, "host": "runner-a", "grant_id": "agr_1"},
        {"thread_id": "athr_2", "generation": 2, "host": "runner-a", "grant_id": ""},
    ]
    delete = AsyncMock(
        return_value={"thread_ids": ["athr_1", "athr_2"], "released_residencies": released}
    )
    monkeypatch.setattr(store, "delete_sessions", delete, raising=False)
    monkeypatch.setattr(
        store, "get_host", AsyncMock(return_value={"host": "runner-a"}), raising=False
    )
    manager = SimpleNamespace(
        shared_residency_lost=AsyncMock(
            side_effect=RuntimeError("unreachable") if manager_fails else None
        )
    )
    monkeypatch.setattr(sandbox_manager_routing, "sandbox_manager_for_host", lambda host: manager)

    app = _build_app(store)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post(
            "/v1/agent/sessions/delete", json={"thread_ids": ["athr_1", "athr_2"]}
        )
    # The delete has committed; a host that cannot be reached now closes the
    # process on its next residency heartbeat instead.
    assert response.status_code == 200
    assert response.json() == {"thread_ids": ["athr_1", "athr_2"]}
    # Rows are closed concurrently, so only the set of calls is fixed.
    assert sorted(manager.shared_residency_lost.await_args_list, key=lambda made: made.args[0]) == [
        call("athr_1", generation=4),
        call("athr_2", generation=2),
    ]


async def test_session_delete_does_not_wait_on_a_host_that_never_answers(
    store: FakeStorage, monkeypatch
):
    """A silent host cannot hold the owner's delete open; its heartbeat finishes the close."""
    from cloud_agent.application import residency

    monkeypatch.setattr(residency, "RELEASED_RESIDENCY_CLOSE_TIMEOUT_S", 0.2)
    released = [
        {
            "thread_id": f"athr_{index}",
            "generation": 1,
            "host": "runner-a",
            "grant_id": f"agr_{index}",
        }
        for index in range(20)
    ]
    thread_ids = [row["thread_id"] for row in released]
    monkeypatch.setattr(
        store,
        "delete_sessions",
        AsyncMock(return_value={"thread_ids": thread_ids, "released_residencies": released}),
        raising=False,
    )
    monkeypatch.setattr(
        store, "get_host", AsyncMock(return_value={"host": "runner-a"}), raising=False
    )
    never = asyncio.Event()

    async def silent(thread_id: str, *, generation: int) -> dict:
        await never.wait()
        return {}

    manager = SimpleNamespace(shared_residency_lost=AsyncMock(side_effect=silent))
    monkeypatch.setattr(sandbox_manager_routing, "sandbox_manager_for_host", lambda host: manager)

    app = _build_app(store)
    transport = ASGITransport(app=app)
    loop = asyncio.get_running_loop()
    started = loop.time()
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await asyncio.wait_for(
            client.post("/v1/agent/sessions/delete", json={"thread_ids": thread_ids}),
            timeout=5,
        )
    assert response.status_code == 200
    assert response.json() == {"thread_ids": thread_ids}
    # One deadline for the whole batch, not one per silent host.
    assert loop.time() - started < 2
    # At most the bounded number of hosts was being waited on.
    assert (
        manager.shared_residency_lost.await_count == residency.RELEASED_RESIDENCY_CLOSE_CONCURRENCY
    )


@pytest.mark.parametrize("thread_ids", [[], [""], ["a", "a"], ["bad\x00id"], ["x" * 129]])
async def test_session_mutations_refuse_ambiguous_selections(client: AsyncClient, thread_ids):
    for path, body in (
        ("archive", {"thread_ids": thread_ids, "archived": False}),
        ("delete", {"thread_ids": thread_ids}),
    ):
        assert (await client.post(f"/v1/agent/sessions/{path}", json=body)).status_code == 422
    too_many = {"thread_ids": [f"athr_{index}" for index in range(1001)]}
    assert (await client.post("/v1/agent/sessions/delete", json=too_many)).status_code == 422


async def test_archived_history_is_readable_but_follow_up_requires_unarchive(
    client: AsyncClient, store: FakeStorage
):
    job_id = await _create_job(client)
    assert (await client.post(f"/v1/agent/jobs/{job_id}/archive")).status_code == 200
    detail = await client.get(f"/v1/agent/jobs/{job_id}")
    assert detail.status_code == 200 and detail.json()["archived_at"] is not None
    assert (await client.get(f"/v1/agent/jobs/{job_id}/thread")).status_code == 200
    blocked = await _send(client, store, job_id, {"prompt": "continue"})
    assert blocked.status_code == 409
    assert blocked.json()["detail"]["error"]["type"] == "session_archived"
    assert len(store.jobs) == 1
    assert store.pending == {}
    assert (await client.delete(f"/v1/agent/jobs/{job_id}/archive")).status_code == 200
    assert (await _send(client, store, job_id, {"prompt": "continue"})).status_code == 201


async def test_follow_up_rechecks_archive_at_the_atomic_store_boundary(
    client: AsyncClient, store: FakeStorage, monkeypatch
):
    """The store's locked check wins over the route's advisory read."""
    job_id = await _create_job(client)
    monkeypatch.setattr(
        store, "submit_message", AsyncMock(return_value={"error": "session_archived"})
    )
    response = await _send(client, store, job_id, {"prompt": "continue"})
    assert response.status_code == 409
    assert response.json()["detail"]["error"]["type"] == "session_archived"


async def test_an_idle_only_message_passes_its_guard_to_the_atomic_store(
    client: AsyncClient, store: FakeStorage, monkeypatch
):
    """A stale action is refused before any queue settlement can send owner work."""
    job_id = await _create_job(client)
    store.jobs[job_id]["state"] = "succeeded"
    submitted = AsyncMock(return_value={"error": "conversation_changed"})
    settlement = AsyncMock()
    monkeypatch.setattr(store, "submit_message", submitted)
    monkeypatch.setattr(store, "settle_queue", settlement)

    response = await _send(
        client,
        store,
        job_id,
        {"prompt": "watch the PR", "require_idle": True, "expected_latest_job_id": job_id},
    )

    assert response.status_code == 409, response.text
    assert response.json()["detail"]["error"]["type"] == "conversation_changed"
    submitted.assert_awaited_once()
    assert submitted.await_args.kwargs["require_idle"] is True
    assert submitted.await_args.kwargs["expected_latest_job_id"] == job_id
    settlement.assert_not_awaited()
    assert store.pending == {}
    assert len(store.jobs) == 1


@pytest.mark.parametrize(
    "guard",
    [
        {"require_idle": True},
        {"require_idle": True, "expected_latest_job_id": ""},
        {"expected_latest_job_id": "ajob_0001"},
        {"require_idle": "true", "expected_latest_job_id": "ajob_0001"},
    ],
)
async def test_an_idle_only_message_requires_a_complete_strict_guard(
    client: AsyncClient, store: FakeStorage, monkeypatch, guard: dict[str, Any]
):
    job_id = await _create_job(client)
    submitted = AsyncMock()
    monkeypatch.setattr(store, "submit_message", submitted)

    response = await _send(client, store, job_id, {"prompt": "watch the PR", **guard})

    assert response.status_code == 422, response.text
    submitted.assert_not_awaited()
    assert store.pending == {}
    assert len(store.jobs) == 1


async def test_an_idle_only_message_replays_after_its_turn_has_started(
    client: AsyncClient, store: FakeStorage, monkeypatch
):
    job_id = await _create_job(client)
    store.jobs[job_id]["state"] = "succeeded"
    _attach_shared_workspace(store, job_id)
    store.jobs[job_id]["thread_sandbox"]["workspace_publication_state"] = "published"
    settlement = AsyncMock()
    monkeypatch.setattr(store, "settle_queue", settlement)
    body = {"prompt": "watch the PR", "require_idle": True, "expected_latest_job_id": job_id}
    headers = {"Idempotency-Key": "watch-pr-once"}

    first = await _send(client, store, job_id, body, headers=headers)
    assert first.status_code == 201, first.text
    watch_id = first.json()["job"]["id"]
    store.jobs[watch_id]["state"] = "running"
    again = await _send(client, store, job_id, body, headers=headers)

    assert again.status_code == 201, again.text
    assert again.json()["job"]["id"] == watch_id
    assert len(store.jobs) == 2
    assert store.pending == {}
    changed_guard = await _send(
        client,
        store,
        job_id,
        {**body, "expected_latest_job_id": watch_id},
        headers=headers,
    )
    assert changed_guard.status_code == 409, changed_guard.text
    assert changed_guard.json()["detail"]["error"]["type"] == "idempotency_conflict"
    assert len(store.jobs) == 2
    assert store.pending == {}
    settlement.assert_not_awaited()


async def test_an_idle_only_message_creates_its_turn_with_prepared_choices_without_settlement(
    client: AsyncClient, store: FakeStorage, monkeypatch
):
    job_id = await _create_job(client)
    store.jobs[job_id]["state"] = "succeeded"
    store.jobs[job_id]["reasoning_effort"] = "high"
    thread_id = _attach_shared_workspace(store, job_id)
    store.jobs[job_id]["thread_sandbox"]["workspace_publication_state"] = "published"
    store.queue_paused[thread_id] = "failed"
    prepared = PreparedFollowUp(
        model="glm-5.1",
        reasoning_effort=None,
        reset_reasoning_effort=True,
        skills=[
            {
                "id": 5,
                "name": "Review",
                "description": "Review changes",
                "revision": 2,
                "instructions": "Inspect tests and edge cases.",
            }
        ],
        attachments=[
            {"id": 42, "path": "notes.txt", "content_type": "text/plain", "size_bytes": 11}
        ],
    )
    monkeypatch.setattr("cloud_agent.api.turns.prepare_follow_up", AsyncMock(return_value=prepared))
    created = AsyncMock(wraps=store.insert_follow_up)
    settlement = AsyncMock()
    monkeypatch.setattr(store, "insert_follow_up", created)
    monkeypatch.setattr(store, "settle_queue", settlement)

    response = await _send(
        client,
        store,
        job_id,
        {
            "prompt": "watch the PR",
            "require_idle": True,
            "expected_latest_job_id": job_id,
            "reasoning_effort": "auto",
            "skill_ids": [5],
            "attachment_ids": [42],
        },
    )

    assert response.status_code == 201, response.text
    watch = response.json()["job"]
    assert watch["parent_job_id"] == job_id
    assert watch["reasoning_effort"] is None
    assert store.jobs[watch["id"]]["skills"] == prepared.skills
    created.assert_awaited_once()
    assert created.await_args.kwargs["model"] == prepared.model
    assert created.await_args.kwargs["reasoning_effort"] is None
    assert created.await_args.kwargs["reset_reasoning_effort"] is True
    assert created.await_args.kwargs["skills"] == prepared.skills
    assert created.await_args.kwargs["attachments"] == prepared.attachments
    assert created.await_args.kwargs["yield_to_queued"] is True
    assert response.json()["pending"] is None
    assert store.pending == {}
    assert thread_id not in store.queue_paused
    assert store.queue_settled[thread_id] == job_id
    settlement.assert_not_awaited()


async def test_deleted_history_cannot_be_read_resumed_forked_or_used_as_workspace(
    client: AsyncClient, store: FakeStorage
):
    job_id = await _create_job(client)
    thread_id = _thread_of(store, job_id)
    store.jobs[job_id]["deleted_at"] = datetime.now(UTC)
    for suffix in (
        "",
        "/thread",
        "/thread/stats",
        "/thread/events",
        "/events",
        "/stream",
        "/files",
        "/git",
    ):
        response = await client.get(f"/v1/agent/jobs/{job_id}{suffix}")
        assert response.status_code == 404, (suffix, response.text)
    for suffix, body in (("/fork", {}), ("/cancel", {})):
        response = await client.post(f"/v1/agent/jobs/{job_id}{suffix}", json=body)
        assert response.status_code == 404, (suffix, response.text)
    # The conversation's own routes answer the same way: a message, the queue,
    # and continuing it.
    for method, path, body in (
        ("POST", f"/v1/agent/threads/{thread_id}/messages", {"prompt": "continue"}),
        ("GET", f"/v1/agent/threads/{thread_id}/messages", None),
        ("POST", f"/v1/agent/threads/{thread_id}/queue/resume", None),
    ):
        response = await client.request(method, path, json=body)
        assert response.status_code == 404, (path, response.text)
        assert response.json()["detail"]["error"]["type"] == "not_found"


async def test_a_submission_key_reaches_the_store_with_a_fingerprint(
    client: AsyncClient, store: FakeStorage
):
    """The ``Idempotency-Key`` header becomes the job's submission key."""
    response = await client.post(
        "/v1/agent/jobs",
        json=on_connection({"repo": "owner/name", "task_prompt": "fix it", "model": "glm-5.1"}),
        headers={"Idempotency-Key": "submit-1"},
    )
    assert response.status_code == 201, response.text
    created = store.jobs[response.json()["id"]]
    assert created["client_request_id"] == "submit-1"
    assert isinstance(created["client_request_fingerprint"], str)
    assert len(created["client_request_fingerprint"]) == 64

    parent_id = response.json()["id"]
    follow = await _send(
        client, store, parent_id, {"prompt": "and then"}, headers={"Idempotency-Key": "turn-2"}
    )
    assert follow.status_code == 201, follow.text
    waiting = store.pending[follow.json()["pending"]["id"]]
    key, fingerprint = waiting["client_request_id"], waiting["client_request_fingerprint"]
    assert key == "turn-2" and isinstance(fingerprint, str) and len(fingerprint) == 64


async def test_a_submission_without_a_key_carries_none(client: AsyncClient, store: FakeStorage):
    job_id = await _create_job(client)
    assert store.jobs[job_id]["client_request_id"] is None
    assert store.jobs[job_id]["client_request_fingerprint"] is None


async def test_idle_guards_preserve_ordinary_message_fingerprints_and_bind_the_newest_turn(
    client: AsyncClient, store: FakeStorage, monkeypatch
):
    job_id = await _create_job(client)
    store.jobs[job_id]["state"] = "succeeded"
    submitted = AsyncMock(return_value={"error": "conversation_changed"})
    monkeypatch.setattr(store, "submit_message", submitted)
    bodies = [
        {"prompt": "watch the PR"},
        {"prompt": "watch the PR", "require_idle": False},
        {"prompt": "watch the PR", "require_idle": True, "expected_latest_job_id": job_id},
        {"prompt": "watch the PR", "require_idle": True, "expected_latest_job_id": "ajob_newer"},
    ]
    for index, body in enumerate(bodies):
        response = await _send(
            client, store, job_id, body, headers={"Idempotency-Key": f"fingerprint-{index}"}
        )
        assert response.status_code == 409, response.text
    fingerprints = [call.kwargs["client_request_fingerprint"] for call in submitted.await_args_list]
    legacy_payload = {
        "kind": "thread_message",
        "thread_id": _thread_of(store, job_id),
        "prompt": "watch the PR",
        "attachment_ids": None,
        "model": None,
        "reasoning_effort": None,
    }
    legacy_hash = hashlib.sha256(
        json.dumps(legacy_payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()

    assert fingerprints[0] == fingerprints[1] == legacy_hash
    assert fingerprints[2] != fingerprints[0]
    assert fingerprints[3] != fingerprints[2]
    assert store.pending == {}
    assert len(store.jobs) == 1


async def test_a_reused_key_under_different_content_is_a_409(
    client: AsyncClient, store: FakeStorage, monkeypatch
):
    """The store's conflict answer is the API's ``idempotency_conflict``."""

    async def conflict(**_kwargs: Any) -> dict[str, Any]:
        return {"error": "client_request_conflict"}

    monkeypatch.setattr(store, "create_job", conflict)
    response = await client.post(
        "/v1/agent/jobs",
        json=on_connection({"repo": "owner/name", "task_prompt": "fix it", "model": "glm-5.1"}),
        headers={"Idempotency-Key": "submit-1"},
    )
    assert response.status_code == 409
    assert response.json()["detail"]["error"]["type"] == "idempotency_conflict"


async def test_a_follow_up_resend_after_archiving_returns_the_first_turn(
    client: AsyncClient, store: FakeStorage
):
    """The replay wins over a check that changed after the first send.

    The first send succeeded and its response was lost; the session was then
    archived. A retry under the same key and content is that same submission,
    so it must return the turn the first send made rather than the 409 the
    archive would give a genuinely new turn.
    """
    parent_id = await _create_job(client)
    store.jobs[parent_id]["state"] = "succeeded"
    body = {"prompt": "and then"}
    headers = {"Idempotency-Key": "turn-2"}

    first = await _send(client, store, parent_id, body, headers=headers)
    assert first.status_code == 201, first.text

    await store.conversation_commands.set_thread_archived(
        job_id=parent_id, user_id=_OWNER, archived=True
    )

    again = await _send(client, store, parent_id, body, headers=headers)
    assert again.status_code == 201, again.text
    assert again.json()["job"]["id"] == first.json()["job"]["id"]
    assert len(store.jobs) == 2


async def test_a_follow_up_resend_after_the_sandbox_is_lost_returns_the_first_turn(
    client: AsyncClient, store: FakeStorage
):
    """Same rule for the other advisory check the handler runs before the store."""
    parent_id = await _create_job(client)
    store.jobs[parent_id]["state"] = "succeeded"
    body = {"prompt": "and then"}
    headers = {"Idempotency-Key": "turn-2"}

    first = await _send(client, store, parent_id, body, headers=headers)
    assert first.status_code == 201, first.text

    store.jobs[parent_id]["thread_sandbox"] = {"shared_live": True, "context_status": "lost"}

    again = await _send(client, store, parent_id, body, headers=headers)
    assert again.status_code == 201, again.text
    assert again.json()["job"]["id"] == first.json()["job"]["id"]


async def test_a_create_resend_after_a_connection_disconnects_returns_the_first_job(
    client: AsyncClient, store: FakeStorage
):
    """A tool the first send was allowed to use is not re-authorised on a resend."""
    row = store.connect_mcp(user_id=_OWNER)
    body = _job_body(mcp_bindings=[_binding(row["id"])])
    headers = {"Idempotency-Key": "submit-1"}

    first = await client.post("/v1/agent/jobs", json=body, headers=headers)
    assert first.status_code == 201, first.text

    assert (
        await store.mcp.disconnect_mcp_connection(connection_id=row["id"], user_id=_OWNER) is True
    )

    again = await client.post("/v1/agent/jobs", json=body, headers=headers)
    assert again.status_code == 201, again.text
    assert again.json()["id"] == first.json()["id"]
    assert len(store.jobs) == 1


async def test_a_new_key_still_runs_the_checks_that_can_refuse_it(
    client: AsyncClient, store: FakeStorage
):
    """Answering the resend first must not turn the mutable checks off.

    A different key is a different submission: the disconnected connection
    that a replay may ignore still refuses a job that has not been created.
    """
    row = store.connect_mcp(user_id=_OWNER)
    body = _job_body(mcp_bindings=[_binding(row["id"])])

    first = await client.post("/v1/agent/jobs", json=body, headers={"Idempotency-Key": "submit-1"})
    assert first.status_code == 201, first.text

    assert (
        await store.mcp.disconnect_mcp_connection(connection_id=row["id"], user_id=_OWNER) is True
    )

    fresh = await client.post("/v1/agent/jobs", json=body, headers={"Idempotency-Key": "submit-2"})
    assert fresh.status_code == 400, fresh.text
    assert len(store.jobs) == 1


async def test_selected_skills_are_snapshotted_and_delivered_only_to_worker(
    client, store, monkeypatch
):
    rows = [
        {
            "id": 5,
            "name": "Review",
            "description": "Review changes",
            "revision": 1,
            "instructions": "Inspect tests and edge cases.",
            "default_enabled": True,
        }
    ]
    lookup = AsyncMock(return_value=rows)
    monkeypatch.setattr(store, "select_skills", lookup)
    created = await client.post(
        "/v1/agent/jobs",
        json=on_connection({"repo": "owner/name", "model": "glm-5.1", "task_prompt": "fix it"}),
        headers={"Idempotency-Key": "skill-submit"},
    )
    assert created.status_code == 201, created.text
    job = created.json()
    assert job["skills"] == [
        {"id": 5, "name": "Review", "description": "Review changes", "revision": 1}
    ]
    assert job["task_prompt"] == "fix it"
    lookup.assert_awaited_once_with(user_id=_OWNER, skill_ids=None)
    rows[0]["instructions"] = "A later edit."
    rows[0]["revision"] = 2
    rows[0]["default_enabled"] = False
    # A resend must retain the first accepted content even if defaults changed.
    lookup.reset_mock()
    replay = await client.post(
        "/v1/agent/jobs",
        json=on_connection({"repo": "owner/name", "model": "glm-5.1", "task_prompt": "fix it"}),
        headers={"Idempotency-Key": "skill-submit"},
    )
    assert replay.json()["id"] == job["id"]
    lookup.assert_not_awaited()
    claim = await client.post("/v1/agent/worker/claim", json=connection_claim({"worker_id": "w1"}))
    assert claim.status_code == 200, claim.text
    assert "Inspect tests and edge cases." in claim.json()["task_prompt"]
    assert "A later edit." not in claim.json()["task_prompt"]
    assert claim.json()["task_prompt"].endswith("User task:\nfix it")
    assert store.messages[0]["content"] == "fix it"
    follow = await _next_turn(client, store, job["id"], {"prompt": "Continue"})
    assert follow["skills"][0]["revision"] == 1
    lookup.assert_not_awaited()
    updated = await _next_turn(client, store, follow["id"], {"prompt": "Update", "skill_ids": [5]})
    assert updated["skills"][0]["revision"] == 2
    cleared = await _next_turn(client, store, updated["id"], {"prompt": "Clear", "skill_ids": []})
    assert cleared["skills"] == []
    assert store.jobs[cleared["id"]]["skills"] == []


@pytest.mark.parametrize(
    "selection,status", [([999], 400), ([5, 5], 400), ([0], 422), ([True], 422), (["5"], 422)]
)
async def test_invalid_skill_selections_never_create_jobs(client, store, selection, status):
    result = await client.post(
        "/v1/agent/jobs",
        json=on_connection(
            {
                "repo": "owner/name",
                "model": "glm-5.1",
                "task_prompt": "fix it",
                "skill_ids": selection,
            }
        ),
    )
    assert result.status_code == status
    assert not store.jobs


# ── Returning a turn that never started ─────────────────────────────────


@pytest.mark.asyncio
async def test_a_turn_that_never_started_goes_back_to_the_queue(store: FakeStorage):
    """The host could not start the process; nobody should wait out its lease."""
    app = _build_app(store)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        job_id = await _create_job(client)
        claim = await client.post(
            "/v1/agent/worker/claim", json=connection_claim({"worker_id": "worker-1"})
        )
        auth = {"Authorization": f"Bearer {claim.json()['worker_token']}"}

        released = await client.post(
            f"/v1/agent/worker/jobs/{job_id}/release",
            json={"reason": "launch_failed"},
            headers=auth,
        )

    assert released.status_code == 200
    assert released.json() == {"ok": True, "state": "queued"}
    assert store.jobs[job_id]["state"] == "queued"
    assert store.jobs[job_id]["claim_retry_delay"] == 30
    # And the reason travels, because "this host cannot start turns" is what an
    # operator reads the trail for.
    assert store.released[-1][2] == "launch_failed"


@pytest.mark.asyncio
async def test_a_turn_that_started_cannot_be_returned(store: FakeStorage):
    """The refusal is the store's, not the caller's account of what happened.

    Replaying an admitted prompt executes a user's instruction a second time.
    A host that is wrong about what its child did must not be able to cause
    that, so the check lives where the evidence is.
    """
    app = _build_app(store)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        job_id = await _create_job(client)
        claim = await client.post(
            "/v1/agent/worker/claim", json=connection_claim({"worker_id": "worker-1"})
        )
        auth = {"Authorization": f"Bearer {claim.json()['worker_token']}"}
        store.jobs[job_id]["admitted"] = True

        refused = await client.post(
            f"/v1/agent/worker/jobs/{job_id}/release",
            json={"reason": "launch_failed"},
            headers=auth,
        )

    assert refused.status_code == 409
    assert refused.json()["detail"]["error"]["type"] == "turn_not_releasable"
    assert store.jobs[job_id]["state"] == "running", "nothing was changed"


@pytest.mark.asyncio
async def test_returning_a_turn_needs_that_turn_own_worker_token(store: FakeStorage):
    app = _build_app(store)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        job_id = await _create_job(client)
        await client.post(
            "/v1/agent/worker/claim", json=connection_claim({"worker_id": "worker-1"})
        )

        unauthenticated = await client.post(
            f"/v1/agent/worker/jobs/{job_id}/release", json={"reason": "launch_failed"}
        )

    assert unauthenticated.status_code == 401
    assert store.jobs[job_id]["state"] == "running"


@pytest.mark.asyncio
@pytest.mark.parametrize("reason", ["", "Launch Failed", "launch failed", "x" * 65])
async def test_a_release_reason_outside_the_audit_vocabulary_is_refused(
    store: FakeStorage, reason: str
):
    """The reason lands in an append-only trail; it is a token, not free text."""
    app = _build_app(store)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        job_id = await _create_job(client)
        claim = await client.post(
            "/v1/agent/worker/claim", json=connection_claim({"worker_id": "worker-1"})
        )
        auth = {"Authorization": f"Bearer {claim.json()['worker_token']}"}

        refused = await client.post(
            f"/v1/agent/worker/jobs/{job_id}/release", json={"reason": reason}, headers=auth
        )

    assert refused.status_code == 422
    assert store.jobs[job_id]["state"] == "running"


async def test_follow_up_receipts_include_retained_membership_on_create_and_replay(
    client: AsyncClient, store: FakeStorage, monkeypatch
):
    """Commands return bare SQL job rows; the HTTP receipt must use the owner read."""
    parent_id = await _create_job(client)
    store.jobs[parent_id]["state"] = "succeeded"
    sandbox = {
        "sandbox_id": "asbx_side_contract",
        "shared_live": True,
        "fork_anchor_job_id": "ajob_main_anchor",
        "native_state": "ready",
        "origin_thread_id": "athread_main",
    }
    store.jobs[parent_id]["thread_sandbox"] = sandbox
    insert = store.insert_follow_up
    replay = store.replayed_submission

    async def bare_insert(**kwargs):
        job = await insert(**kwargs)
        store.jobs[job["id"]]["thread_sandbox"] = sandbox
        return {**job, "thread_sandbox": None}

    async def bare_replay(**kwargs):
        job = await replay(**kwargs)
        return {**job, "thread_sandbox": None} if job else None

    monkeypatch.setattr(store, "insert_follow_up", bare_insert)
    monkeypatch.setattr(store, "replayed_submission", bare_replay)
    first = await _send(
        client, store, parent_id, {"prompt": "side reply"}, headers={"Idempotency-Key": "side-send"}
    )
    again = await _send(
        client, store, parent_id, {"prompt": "side reply"}, headers={"Idempotency-Key": "side-send"}
    )
    for response in (first, again):
        assert response.status_code == 201, response.text
        receipt = response.json()["job"]
        assert receipt["sandbox_id"] == sandbox["sandbox_id"]
        assert receipt["fork_anchor_job_id"] == sandbox["fork_anchor_job_id"]
        assert receipt["workspace_shared"] is True
    assert first.json()["job"]["id"] == again.json()["job"]["id"]
    assert len(store.jobs) == 2


@pytest.mark.parametrize("runtime", ["codex", "kilo", "claude-code", "opencode", "pi"])
async def test_claim_loads_current_default_servers_and_skips_unavailable_ones(
    client: AsyncClient, store: FakeStorage, runtime
):
    """Every CLI receives the live, usable set without a composer selection."""
    working = store.connect_mcp(user_id=_OWNER, label="Wiki")
    expired = store.connect_mcp(
        user_id=_OWNER,
        label="Expired",
        expires_at=datetime.now(UTC) - timedelta(minutes=1),
    )
    paused = store.connect_mcp(user_id=_OWNER, label="Paused")
    store.mcp_connections[paused["id"]]["enabled"] = False
    created = await client.post("/v1/agent/jobs", json=_job_body(runtime=runtime))
    assert created.status_code == 201
    thread = created.json()["thread_id"]
    # The real repository creates these defaults on list; storage tests exercise
    # that owner-scoped insert against Postgres. Keep the route fake explicit.
    store.mcp_bindings[thread] = [
        store._thread_binding(
            thread,
            {
                "connection_id": row["id"],
                "alias": f"mcp{row['id']}",
                "enabled": True,
                "selection_pending": True,
                "rules": {},
                "config_revision": row["config_revision"],
                "authorization_epoch": row["authorization_epoch"],
                "capability_revision": row["capability_revision"],
            },
        )
        for row in (working, expired, paused)
    ]
    app = _build_app(store, claim_auth=True)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as runner:
        claimed = await runner.post(
            "/v1/agent/worker/claim",
            json=connection_claim({"worker_id": "w-1", "lease_ttl_seconds": 60}),
        )
    assert claimed.status_code == 200, claimed.text
    assert claimed.json()["mcp_bindings"] == [
        f"mcp{working['id']}",
        worker.memory_tool.RUNTIME_SERVER_ID,
    ]
    assert claimed.json()["mcp_token"]


async def test_claim_puts_durable_memories_with_their_ids_before_the_task(
    client, store, monkeypatch
):
    read = AsyncMock(
        return_value={
            "repo": "owner/name",
            "project": [{"id": "m-project", "content": "Use pnpm, not npm."}],
            "user": [{"id": "m-user", "content": "Reply in Chinese."}],
        }
    )
    mark = AsyncMock()
    monkeypatch.setattr(store, "task_memories", read, raising=False)
    monkeypatch.setattr(store, "mark_memories_used", mark, raising=False)
    created = await client.post(
        "/v1/agent/jobs",
        json=on_connection({"repo": "owner/name", "model": "glm-5.1", "task_prompt": "fix it"}),
    )
    assert created.status_code == 201, created.text
    claim = await client.post("/v1/agent/worker/claim", json=connection_claim({"worker_id": "w1"}))
    assert claim.status_code == 200, claim.text
    prompt = claim.json()["task_prompt"]
    read.assert_awaited_once_with(_OWNER, "owner/name")
    mark.assert_awaited_once_with(_OWNER, ["m-project", "m-user"])
    assert '{"id": "m-project", "content": "Use pnpm, not npm."}' in prompt
    assert '{"id": "m-user", "content": "Reply in Chinese."}' in prompt
    assert "memory_update" in prompt
    assert prompt.endswith("Current task:\nfix it")


async def test_unavailable_durable_memory_never_blocks_a_claim(client, store, monkeypatch):
    monkeypatch.setattr(
        store, "task_memories", AsyncMock(side_effect=RuntimeError("down")), raising=False
    )
    created = await client.post(
        "/v1/agent/jobs",
        json=on_connection({"repo": "owner/name", "model": "glm-5.1", "task_prompt": "fix it"}),
    )
    assert created.status_code == 201, created.text
    claim = await client.post("/v1/agent/worker/claim", json=connection_claim({"worker_id": "w1"}))
    assert claim.status_code == 200, claim.text
    assert claim.json()["task_prompt"] == "fix it"
