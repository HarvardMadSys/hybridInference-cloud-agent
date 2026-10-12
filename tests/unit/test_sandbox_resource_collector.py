"""Whole-container telemetry remains optional, scoped, fresh, and off heartbeats."""

from __future__ import annotations

import threading

import httpx
import pytest

from cloud_agent_host.sandbox_resources import (
    MAX_STATS_BYTES,
    SandboxResourceCollector,
    reading_from_stats,
    resource_sample,
)
from cloud_agent_shared.sandbox_resources import parse_sandbox_resource_sample


class _Clock:
    def __init__(self, value=0.0):
        self.value = value

    def __call__(self):
        return self.value


def _target(container="exact-one", *, running=True):
    return {"container_id": container, "running": running}


def _stats(*, cpu=1_000_000_000, memory=500, rx=100, tx=200, read=1000, write=2000):
    return {
        "cpu_stats": {"cpu_usage": {"total_usage": cpu}},
        "memory_stats": {"usage": memory, "limit": 4096, "stats": {"inactive_file": 100}},
        "pids_stats": {"current": 7},
        "networks": {"eth0": {"rx_bytes": rx, "tx_bytes": tx}},
        "blkio_stats": {
            "io_service_bytes_recursive": [
                {"op": "Read", "value": read},
                {"op": "Write", "value": write},
            ]
        },
    }


def test_cpu_and_io_rates_describe_the_interval_and_memory_is_current():
    first = reading_from_stats(_stats(), mono=10)
    second = reading_from_stats(
        _stats(cpu=21_000_000_000, memory=900, rx=300, tx=500, read=2000, write=4000),
        mono=20,
    )
    warming_up = resource_sample(first, None)
    assert warming_up.cpu_cores is None
    assert warming_up.net_rx_bytes == 100
    assert warming_up.net_rx_bytes_per_second is None

    sample = resource_sample(second, first)
    assert sample.cpu_cores == 2
    assert sample.cpu_window_seconds == 10
    assert sample.memory_bytes == 800
    assert sample.memory_limit_bytes == 4096
    assert sample.pids == 7
    assert sample.net_rx_bytes == 300
    assert sample.net_rx_bytes_per_second == 20
    assert sample.net_tx_bytes_per_second == 30
    assert sample.io_read_bytes == 2000
    assert sample.io_read_bytes_per_second == 100
    assert sample.io_write_bytes_per_second == 200
    assert sample.scope == "container"


def test_missing_and_invalid_docker_counters_never_become_zero():
    stats = {
        "cpu_stats": "unsupported",
        "memory_stats": {"usage": True, "limit": 4096},
        "pids_stats": {"current": "7"},
        "networks": {"eth0": {"rx_bytes": 0}, "eth1": {"rx_bytes": 0}},
        "blkio_stats": {"io_service_bytes_recursive": []},
    }
    sample = resource_sample(reading_from_stats(stats, mono=1), None)
    assert sample.cpu_cores is None
    assert sample.memory_bytes is None
    assert sample.memory_limit_bytes == 4096
    assert sample.pids is None
    assert sample.net_rx_bytes == 0
    assert sample.net_tx_bytes is None
    assert sample.io_read_bytes is None
    assert sample.io_write_bytes is None


def test_partially_reported_interfaces_and_block_operations_preserve_missingness():
    stats = _stats()
    stats["networks"]["eth1"] = {"tx_bytes": 20}
    stats["blkio_stats"]["io_service_bytes_recursive"] = [
        {"op": "Read", "value": 0},
        {"op": "Read", "value": -1},
        {"op": "Write", "value": 0},
    ]
    reading = reading_from_stats(stats, mono=1)
    assert reading.counters.net_rx_bytes is None
    assert reading.counters.net_tx_bytes == 220
    assert reading.counters.io_read_bytes is None
    assert reading.counters.io_write_bytes == 0


def test_any_lifetime_counter_reset_rewarms_every_rate():
    first = reading_from_stats(_stats(), mono=10)
    reset = reading_from_stats(_stats(cpu=1, rx=110), mono=20)
    after_reset = resource_sample(reset, first)
    assert after_reset.cpu_cores is None
    assert after_reset.cpu_window_seconds is None
    assert after_reset.net_rx_bytes_per_second is None
    resumed = resource_sample(reading_from_stats(_stats(cpu=1_000_000_001, rx=210), mono=30), reset)
    assert resumed.cpu_cores == pytest.approx(0.1)
    assert resumed.net_rx_bytes_per_second == 10


def test_rates_do_not_bridge_an_unmeasured_counter_or_an_excessive_window():
    missing = _stats()
    missing.pop("cpu_stats")
    first = reading_from_stats(missing, mono=1)
    second = reading_from_stats(_stats(rx=200), mono=11)
    sample = resource_sample(second, first)
    assert sample.cpu_cores is None
    assert sample.net_rx_bytes_per_second == 10
    stale_window = resource_sample(reading_from_stats(_stats(rx=300), mono=400), second)
    assert stale_window.cpu_cores is None
    assert stale_window.net_rx_bytes_per_second is None


def test_cache_keeps_sample_identity_and_monotonic_age_without_io():
    clock = _Clock(10)
    requests = []

    def handle(request):
        requests.append(request)
        return httpx.Response(200, json=_stats())

    collector = SandboxResourceCollector(clock=clock)
    collector.sync_targets([_target()])
    assert collector.latest("exact-one") is None
    with httpx.Client(transport=httpx.MockTransport(handle), base_url="http://docker") as client:
        assert collector.sample_cycle(client) == 1
    clock.value = 15
    first = collector.latest("exact-one")
    assert first is not None
    assert first["sample_age_ms"] == 5000
    clock.value = 20
    second = collector.latest("exact-one")
    assert second is not None
    assert second["sample_id"] == first["sample_id"]
    assert second["sample_age_ms"] == 10_000
    assert len(requests) == 1
    assert requests[0].url.path == "/containers/exact-one/stats"
    assert requests[0].url.params["one-shot"] == "true"
    clock.value = 311
    assert collector.latest("exact-one") is None


def test_stopping_removing_and_replacing_targets_clear_cached_baselines():
    clock = _Clock(1)
    collector = SandboxResourceCollector(clock=clock)
    with httpx.Client(
        transport=httpx.MockTransport(lambda _: httpx.Response(200, json=_stats())),
        base_url="http://docker",
    ) as client:
        collector.sync_targets([_target()])
        collector.sample_cycle(client)
        collector.sync_targets([_target(running=False)])
        assert collector.latest("exact-one") is None
        assert collector.sample_cycle(client) == 0
        clock.value = 10
        collector.sync_targets([_target()])
        collector.sample_cycle(client)
        assert collector.latest("exact-one")["cpu_cores"] is None
        collector.sync_targets([_target("exact-two")])
        assert collector.latest("exact-one") is None
        collector.sample_cycle(client)
        assert collector.latest("exact-two")["cpu_cores"] is None


def test_a_target_removed_while_its_request_is_in_flight_cannot_repopulate_cache():
    collector = SandboxResourceCollector()
    collector.sync_targets([_target()])

    def handle(_request):
        collector.sync_targets([])
        collector.sync_targets([_target()])
        return httpx.Response(200, json=_stats())

    with httpx.Client(transport=httpx.MockTransport(handle), base_url="http://docker") as client:
        collector.sample_cycle(client)
    assert collector.latest("exact-one") is None


def test_failure_discards_cache_and_the_next_read_starts_a_new_baseline():
    clock = _Clock(1)
    responses = iter([200, 404, 200])
    collector = SandboxResourceCollector(clock=clock)
    collector.sync_targets([_target()])
    with httpx.Client(
        transport=httpx.MockTransport(lambda _: httpx.Response(next(responses), json=_stats())),
        base_url="http://docker",
    ) as client:
        collector.sample_cycle(client)
        assert collector.latest("exact-one") is not None
        clock.value = 11
        collector.sample_cycle(client)
        assert collector.latest("exact-one") is None
        clock.value = 21
        collector.sample_cycle(client)
        assert collector.latest("exact-one")["cpu_cores"] is None


def test_time_bounded_batches_rotate_fairly_instead_of_starving_later_targets():
    clock = _Clock()
    calls = []

    def handle(request):
        calls.append(request.url.path)
        clock.value += 2
        return httpx.Response(200, json=_stats())

    collector = SandboxResourceCollector(clock=clock, cycle_budget_s=3)
    collector.sync_targets([_target(f"exact-{index}") for index in range(4)])
    with httpx.Client(transport=httpx.MockTransport(handle), base_url="http://docker") as client:
        assert collector.sample_cycle(client) == 2
        assert collector.sample_cycle(client) == 2
    assert calls == [f"/containers/exact-{index}/stats" for index in range(4)]


def test_count_bound_rotates_targets_and_deduplicates_shared_members():
    clock = _Clock()
    calls = []
    collector = SandboxResourceCollector(clock=clock, max_reads_per_cycle=1)
    collector.sync_targets([_target(), _target(), _target("exact-two")])

    def handle(request):
        calls.append(request.url.path)
        return httpx.Response(200, json=_stats())

    with httpx.Client(transport=httpx.MockTransport(handle), base_url="http://docker") as client:
        assert collector.sample_cycle(client) == 1
        assert collector.sample_cycle(client) == 1
    assert calls == ["/containers/exact-one/stats", "/containers/exact-two/stats"]


def test_oversized_stats_stream_stops_without_buffering_or_reading_the_tail():
    chunks_read = []
    closed = threading.Event()

    class StatsStream(httpx.SyncByteStream):
        def __iter__(self):
            for chunk in (b"{", b" " * MAX_STATS_BYTES, b"unread tail"):
                chunks_read.append(len(chunk))
                yield chunk

        def close(self):
            closed.set()

    collector = SandboxResourceCollector()
    collector.sync_targets([_target()])
    with httpx.Client(
        transport=httpx.MockTransport(lambda _: httpx.Response(200, stream=StatsStream())),
        base_url="http://docker",
    ) as client:
        assert collector.sample_cycle(client) == 1
    assert chunks_read == [1, MAX_STATS_BYTES]
    assert closed.is_set()
    assert collector.latest("exact-one") is None


def test_trickling_stats_body_stops_at_total_deadline_and_closes_stream():
    clock = _Clock()
    chunks_read = []
    closed = threading.Event()

    class StatsStream(httpx.SyncByteStream):
        def __iter__(self):
            for index in range(20):
                clock.value += 0.4
                chunks_read.append(index)
                yield b" "

        def close(self):
            closed.set()

    collector = SandboxResourceCollector(clock=clock, read_timeout_s=1)
    collector.sync_targets([_target()])
    with httpx.Client(
        transport=httpx.MockTransport(lambda _: httpx.Response(200, stream=StatsStream())),
        base_url="http://docker",
    ) as client:
        assert collector.sample_cycle(client) == 1
    assert chunks_read == [0, 1, 2]
    assert clock.value == pytest.approx(1.2)
    assert closed.is_set()
    assert collector.latest("exact-one") is None


def test_cache_and_target_sync_remain_nonblocking_during_slow_docker():
    reading = threading.Event()
    release = threading.Event()
    cache_read = threading.Event()

    def handle(_request):
        reading.set()
        assert release.wait(2)
        return httpx.Response(200, json=_stats())

    collector = SandboxResourceCollector(
        client_factory=lambda: httpx.Client(
            transport=httpx.MockTransport(handle), base_url="http://docker"
        )
    )
    collector.sync_targets([_target()])
    collector.start()
    try:
        assert reading.wait(2)

        def inspect_cache():
            collector.sync_targets([_target()])
            assert collector.latest("exact-one") is None
            cache_read.set()

        thread = threading.Thread(target=inspect_cache)
        thread.start()
        assert cache_read.wait(1)
        thread.join(timeout=1)
    finally:
        release.set()
        collector.close()


def test_worker_shutdown_closes_the_daemon_client_and_clears_cache():
    sampled = threading.Event()
    client = httpx.Client(
        transport=httpx.MockTransport(lambda _: httpx.Response(200, json=_stats())),
        base_url="http://docker",
    )
    collector = SandboxResourceCollector(client_factory=lambda: client)
    cycle = collector.sample_cycle

    def sample_cycle(connection):
        result = cycle(connection)
        sampled.set()
        return result

    collector.sample_cycle = sample_cycle
    collector.sync_targets([_target()])
    collector.start()
    try:
        assert sampled.wait(2)
        assert collector.latest("exact-one") is not None
    finally:
        collector.close()
    assert client.is_closed
    assert collector.latest("exact-one") is None


@pytest.mark.parametrize(
    "key,value",
    [
        ("schema_version", 2),
        ("source", "host-total"),
        ("scope", "member"),
        ("memory_bytes", True),
        ("memory_bytes", "1024"),
        ("memory_bytes", 1.5),
        ("memory_bytes", -1),
        ("cpu_cores", True),
        ("cpu_cores", "1"),
        ("cpu_cores", float("nan")),
        ("cpu_cores", float("inf")),
        ("cpu_window_seconds", 0),
        ("sample_age_ms", 300_001),
        ("sample_age_ms", -1),
        ("extra_payload", {"unexpected": "field"}),
    ],
)
def test_invalid_optional_wire_blocks_are_dropped(key, value):
    payload = {"sample_id": "sample", "sample_age_ms": 0, "memory_bytes": 1024}
    payload[key] = value
    assert parse_sandbox_resource_sample(payload) is None


def test_sparse_optional_wire_reading_is_valid_and_preserves_zero():
    sample = parse_sandbox_resource_sample(
        {"sample_id": "sample", "sample_age_ms": 0, "cpu_cores": 0, "pids": 0}
    )
    assert sample is not None
    assert sample.cpu_cores == 0
    assert sample.pids == 0
    assert sample.memory_bytes is None
    assert parse_sandbox_resource_sample(None) is None
