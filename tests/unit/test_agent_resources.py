"""Docker stats primitives: what they read, and what they refuse to invent.

The contract these pin is not "the numbers are right" — a unit test cannot
know a real cgroup's truth — but that every *shape* of a daemon document
produces an honest sample. Plausible zeros where nothing could be read would
be worse than no number, because a dashboard cannot tell a measured zero from
a fabricated one.
"""

from __future__ import annotations

from cloud_agent_host.resources import (
    SHARED_MEMBER_ACCOUNTING_VERSION,
    sample_from_stats,
    shared_member_resource_rollup,
)


def test_shared_member_rollup_never_claims_group_container_totals():
    assert shared_member_resource_rollup() == {
        "sample_count": 0,
        "coverage": 0.0,
        "sampler_version": SHARED_MEMBER_ACCOUNTING_VERSION,
    }


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
