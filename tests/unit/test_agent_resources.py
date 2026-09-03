"""The resource sampler: what it measures, and what it refuses to invent.

The contract these pin is not "the numbers are right" — a unit test cannot
know a real cgroup's truth — but that every *shape* of failure produces an
honest row. A sampler that reported plausible zeros when it could read
nothing would be worse than one that did not exist, because a dashboard
cannot tell a measured zero from a fabricated one.
"""

from __future__ import annotations

import time

import httpx

from cloud_agent_host.resources import (
    SAMPLER_VERSION,
    SHARED_MEMBER_ACCOUNTING_VERSION,
    ContainerStats,
    ResourceSampler,
    Sample,
    daemon_reachable,
    rollup,
    sample_from_stats,
    shared_member_resource_rollup,
)


def test_shared_member_rollup_never_claims_group_container_totals():
    assert shared_member_resource_rollup() == {
        "sample_count": 0,
        "coverage": 0.0,
        "sampler_version": SHARED_MEMBER_ACCOUNTING_VERSION,
    }


def test_rollup_reports_deltas_so_a_resident_sandbox_is_not_double_charged():
    """Counters are cumulative; a window owns only its own difference."""
    samples = [
        Sample(mono=0.0, cpu_usec=1_000, net_rx_bytes=500),
        Sample(mono=5.0, cpu_usec=4_000, net_rx_bytes=900),
    ]
    result = rollup(samples, window_s=5.0)
    assert result["cpu_usec"] == 3_000
    assert result["net_rx_bytes"] == 400


def test_peak_memory_comes_from_our_own_samples():
    """The kernel's high-water mark would attribute an earlier turn's spike."""
    samples = [
        Sample(mono=0.0, mem_bytes=100),
        Sample(mono=1.0, mem_bytes=900),
        Sample(mono=2.0, mem_bytes=300),
    ]
    result = rollup(samples, window_s=2.0)
    assert result["mem_peak_bytes"] == 900
    assert result["mem_avg_bytes"] == 433


def test_a_counter_that_went_backwards_reports_nothing():
    """A cgroup replaced mid-window must not surface as a wild number."""
    samples = [Sample(mono=0.0, cpu_usec=9_000), Sample(mono=1.0, cpu_usec=5)]
    assert rollup(samples, window_s=1.0)["cpu_usec"] is None


def test_a_single_sample_cannot_describe_a_window():
    """One reading of a cumulative counter is a lifetime, not a window."""
    result = rollup([Sample(mono=0.0, cpu_usec=1_000)], window_s=5.0)
    assert result["coverage"] == 0.0
    assert "cpu_usec" not in result or result.get("cpu_usec") is None


def test_coverage_reports_the_share_of_the_window_observed():
    """A sampler that died early says so instead of implying a full read."""
    samples = [Sample(mono=0.0, cpu_usec=0), Sample(mono=2.0, cpu_usec=10)]
    assert rollup(samples, window_s=10.0)["coverage"] == 0.2
    assert rollup(samples, window_s=2.0)["coverage"] == 1.0


def test_rollup_always_stamps_the_sampler_that_took_it():
    """A stored row must be readable under the semantics it was written with."""
    assert rollup([], window_s=0.0)["sampler_version"] == SAMPLER_VERSION


def test_an_inert_sampler_starts_stops_and_reports_zero_coverage():
    """The normal outcome on an unmeasurable host, and never an error."""
    sampler = ResourceSampler(None)
    assert not sampler.active
    sampler.start()
    result = sampler.stop()
    assert result["coverage"] == 0.0
    assert result["sample_count"] == 0


def test_a_sampler_that_never_binds_reports_zero_coverage(tmp_path):
    """A host that can never be read says so, and stops asking."""
    sampler = ResourceSampler(resolver=lambda: None, interval_s=0.01, bind_timeout_s=0.05)
    sampler.start()
    time.sleep(0.12)
    result = sampler.stop()

    assert result["coverage"] == 0.0
    assert result["sample_count"] == 0


def test_coverage_counts_readings_not_heartbeats():
    """A sampler watching a dead container must not claim a measured turn.

    Ticking the clock is not measuring. Samples whose every counter is
    ``None`` are heartbeats, and counting them would let a sampler that read
    nothing for the last nine seconds of a ten-second turn report near-total
    coverage.
    """
    samples = [
        Sample(mono=0.0, cpu_usec=100),
        Sample(mono=1.0, cpu_usec=200),
        *[Sample(mono=float(t)) for t in range(2, 11)],
    ]

    result = rollup(samples, window_s=10.0)

    assert result["sample_count"] == 2
    assert result["coverage"] == 0.1


def _stats(*, cpu_ns=1_000_000, mem=1000, inactive=0, io=None, net=None) -> dict:
    """One daemon stats document, in the shape the API actually returns."""
    doc: dict = {
        "cpu_stats": {"cpu_usage": {"total_usage": cpu_ns}},
        "memory_stats": {"usage": mem, "stats": {"inactive_file": inactive}},
    }
    if io is not None:
        doc["blkio_stats"] = {"io_service_bytes_recursive": io}
    if net is not None:
        doc["networks"] = net
    return doc


def _client(handler) -> httpx.Client:
    """A client whose transport answers from `handler`, so no daemon is needed."""
    return httpx.Client(transport=httpx.MockTransport(handler), base_url="http://docker")


def test_cpu_arrives_in_nanoseconds_and_is_stored_as_microseconds():
    """The daemon's unit is not the column's unit, and the gap is 1000x."""
    sample = sample_from_stats(_stats(cpu_ns=12_000_000_000), mono=0.0)
    assert sample.cpu_usec == 12_000_000


def test_memory_matches_what_docker_stats_shows_a_person():
    """Page cache is subtracted, as the CLI does on cgroup v2.

    An operator who runs `docker stats` against the same container while
    reading our number must not be shown two different answers to the same
    question.
    """
    assert sample_from_stats(_stats(mem=1000, inactive=400), mono=0.0).mem_bytes == 600


def test_an_unpopulated_blkio_is_unmeasured_rather_than_zero():
    """The one place the direct-cgroup rule must NOT be carried over.

    Reading `io.stat` directly, an empty file is written by the kernel and
    means no io happened. Through the daemon the same emptiness is
    ambiguous — Docker has shipped versions that never populate this on
    cgroup v2, whatever the container did — so it is reported as unmeasured.
    Losing an occasional true zero is a smaller harm than inventing one.
    """
    for empty in ([], None):
        sample = sample_from_stats(_stats(io=empty), mono=0.0)
        assert sample.io_read_bytes is None
        assert sample.io_write_bytes is None


def test_block_io_sums_every_device_and_reads_either_case():
    """Docker has spelled the op both ways across versions."""
    sample = sample_from_stats(
        _stats(
            io=[
                {"op": "read", "value": 100},
                {"op": "Write", "value": 200},
                {"op": "read", "value": 5},
            ]
        ),
        mono=0.0,
    )
    assert (sample.io_read_bytes, sample.io_write_bytes) == (105, 200)


def test_network_sums_interfaces_and_is_absent_when_there_are_none():
    """A closed-network sandbox has nothing to report, which is not zero."""
    with_net = sample_from_stats(_stats(net={"eth0": {"rx_bytes": 5, "tx_bytes": 6}}), mono=0.0)
    assert (with_net.net_rx_bytes, with_net.net_tx_bytes) == (5, 6)

    without = sample_from_stats(_stats(net={}), mono=0.0)
    assert (without.net_rx_bytes, without.net_tx_bytes) == (None, None)


def test_the_stats_request_asks_for_one_shot():
    """Without it the daemon pre-reads for a CPU percentage we never use.

    That pre-read costs about a second of waiting per tick — at one sample a
    second, the sampler would spend its whole life inside one request.
    """
    seen: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen.update(dict(request.url.params))
        return httpx.Response(200, json=_stats())

    ContainerStats("hyi-job-abc", _client(handler)).read()

    assert seen.get("one-shot") == "true"
    assert seen.get("stream") == "false"


def test_a_container_the_daemon_has_forgotten_ends_the_window():
    """404 is terminal: later numbers would belong to something else."""
    stats = ContainerStats("gone", _client(lambda _r: httpx.Response(404, json={})))
    assert stats.read() is None


def test_a_transient_daemon_error_costs_coverage_but_not_the_window():
    """A hiccup is a missing reading, not the end of the measurement."""
    stats = ContainerStats("flaky", _client(lambda _r: httpx.Response(500, text="boom")))
    sample = stats.read()
    assert sample is not None
    assert sample.cpu_usec is None


def test_a_daemon_that_cannot_be_reached_is_reported_by_preflight():
    """The check that the first sampler's whole failure did not have.

    An assumption about the deployment that nothing verifies is how a
    measurement feature ships dead and stays dead until somebody happens to
    read the UI.
    """
    reason = daemon_reachable(timeout_s=0.05)
    # On a host with a live daemon this is None; without one it names the
    # reason. Either way it must answer rather than raise.
    assert reason is None or isinstance(reason, str)
