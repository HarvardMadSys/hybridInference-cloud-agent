"""Docker stats primitives and the honest shared-member fallback.

A shared retained sandbox runs several members at once, so a whole-container
delta can never be one member's work: assigning it to either would duplicate
the other's. Shared turns therefore start from
``shared_member_resource_rollup()`` and may add UID-filtered Kata guest process
telemetry and per-tool timing. The empty top-level counters are an explicit
unavailable result, never a measured zero. The sample primitives here serve
the group-level collector and per-tool-call attribution.

**Why through the daemon, and not through cgroupfs.** The first version read
``/proc/<pid>/cgroup`` and ``/sys/fs/cgroup`` directly, having assumed the
runner is a host process. It is not: the runner is a container beside the
sandboxes it supervises, so a host pid the daemon reports does not exist in
its own pid namespace and every read failed. Granting it ``pid: host``,
``cgroup: host`` and a cgroupfs mount would work, but it manages that
assumption rather than removing it — and each of the three knobs fails
exactly the way the original bug did, silently. Asking the daemon over the
socket the runner already holds has no such assumption: it is the same
answer whether the runner runs on the host, in a container, or on a machine
that joins the pool later.

**Why these reads are still outside.** Nothing is asked of the sandbox and no
agent-visible process participates in these Docker totals.  The separate Kata
guest observer is a reviewed, root-only helper and filters records by member
UID; it is not implemented in this module.

**Why deltas, never absolutes.** These counters are cumulative from the
container's creation. Every value reported is the difference between the
window's last and first sample. Peak memory is the maximum of *our own* samples
for the same reason: a high-water mark since creation would attribute an
earlier window's spike to this one. Callers must additionally prove that the
whole container is the accounting unit; a shared member turn cannot do so.

**Why a missing number is not a zero.** A daemon that cannot be reached, a
container that has gone, a field this Docker version does not populate — all
yield ``None`` and a ``coverage`` below 1. Reporting zeros there would be a
lie a dashboard cannot detect, so the honest shape is carried all the way to
the row: ``coverage`` says how much of the window was actually observed.

Nothing here may fail a turn. Every read is defensive and every failure
degrades to a missing counter, because a measurement problem must never
become the user's problem.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any

import httpx

from cloud_agent_shared.shared_protocol import SHARED_MEMBER_ACCOUNTING_VERSION


def shared_member_resource_rollup() -> dict[str, object]:
    """Return the honest top-level base for one shared-member turn.

    The sandbox manager may add tool windows and UID-scoped guest observations to this
    object. Keeping all aggregate counters absent prevents an unforked turn and
    a forked turn from silently acquiring different accounting semantics.
    """
    return {
        "sample_count": 0,
        "coverage": 0.0,
        "sampler_version": SHARED_MEMBER_ACCOUNTING_VERSION,
    }


_DEFAULT_SOCKET = "/var/run/docker.sock"


@dataclass(frozen=True)
class Sample:
    """One instant's counters. ``None`` means that counter was unavailable."""

    mono: float
    cpu_usec: int | None = None
    mem_bytes: int | None = None
    io_read_bytes: int | None = None
    io_write_bytes: int | None = None
    net_rx_bytes: int | None = None
    net_tx_bytes: int | None = None


def _as_int(value: Any) -> int | None:
    """Coerce a JSON number to a non-negative int, or None."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    number = int(value)
    return number if number >= 0 else None


def _parse_cpu(stats: dict[str, Any]) -> int | None:
    """Cumulative CPU time in microseconds.

    The daemon reports nanoseconds on Linux; the rest of this module speaks
    microseconds because that is what the stored column holds.
    """
    total = _as_int(((stats.get("cpu_stats") or {}).get("cpu_usage") or {}).get("total_usage"))
    return None if total is None else total // 1000


def _parse_memory(stats: dict[str, Any]) -> int | None:
    """Current memory, matching what ``docker stats`` shows a person.

    Page cache is subtracted the way the CLI does it, because an operator
    comparing our number against ``docker stats`` on the same container must
    not find two different answers to the same question.
    """
    memory = stats.get("memory_stats") or {}
    usage = _as_int(memory.get("usage"))
    if usage is None:
        return None
    inactive = _as_int((memory.get("stats") or {}).get("inactive_file")) or 0
    return max(0, usage - inactive)


def _parse_io(stats: dict[str, Any]) -> tuple[int | None, int | None]:
    """Cumulative block io, or ``None`` where this daemon does not report it.

    Deliberately NOT zero when the list is empty or missing. Reading a cgroup
    file directly, an empty file is written by the kernel and means no io
    happened; here the same emptiness is ambiguous — Docker has shipped
    versions that never populate this on cgroup v2, whatever the container
    did. An ambiguous emptiness is reported as unmeasured, because losing an
    occasional true zero is a smaller harm than inventing one.
    """
    entries = (stats.get("blkio_stats") or {}).get("io_service_bytes_recursive")
    if not entries or not isinstance(entries, list):
        return None, None
    read_total = write_total = 0
    seen = False
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        value = _as_int(entry.get("value"))
        op = str(entry.get("op") or "").lower()
        if value is None or op not in ("read", "write"):
            continue
        seen = True
        if op == "read":
            read_total += value
        else:
            write_total += value
    return (read_total, write_total) if seen else (None, None)


def _parse_network(stats: dict[str, Any]) -> tuple[int | None, int | None]:
    """Cumulative bytes across the container's interfaces.

    Absent when the container has no network of its own to report — which is
    a real state, and not the same as having sent nothing.
    """
    networks = stats.get("networks")
    if not isinstance(networks, dict) or not networks:
        return None, None
    rx_total = tx_total = 0
    seen = False
    for interface in networks.values():
        if not isinstance(interface, dict):
            continue
        rx, tx = _as_int(interface.get("rx_bytes")), _as_int(interface.get("tx_bytes"))
        if rx is None and tx is None:
            continue
        seen = True
        rx_total += rx or 0
        tx_total += tx or 0
    return (rx_total, tx_total) if seen else (None, None)


def sample_from_stats(stats: dict[str, Any], *, mono: float) -> Sample:
    """Project one daemon stats document into one sample."""
    io_read, io_write = _parse_io(stats)
    net_rx, net_tx = _parse_network(stats)
    return Sample(
        mono=mono,
        cpu_usec=_parse_cpu(stats),
        mem_bytes=_parse_memory(stats),
        io_read_bytes=io_read,
        io_write_bytes=io_write,
        net_rx_bytes=net_rx,
        net_tx_bytes=net_tx,
    )


def daemon_client(*, timeout_s: float = 5.0) -> httpx.Client:
    """Open a keep-alive client onto the Docker API.

    One connection for the life of a sampler: a fresh TCP or unix connection
    per tick would spend more time on the handshake than on the measurement.
    ``DOCKER_HOST`` is honoured so a deployment that talks to a remote or
    non-default daemon is measured through the same daemon it spawns
    through — measuring a different one would be worse than not measuring.
    """
    host = os.environ.get("DOCKER_HOST", "")
    if host.startswith("tcp://"):
        return httpx.Client(base_url=host.replace("tcp://", "http://", 1), timeout=timeout_s)
    socket = host[len("unix://") :] if host.startswith("unix://") else _DEFAULT_SOCKET
    transport = httpx.HTTPTransport(uds=socket)
    return httpx.Client(transport=transport, base_url="http://docker", timeout=timeout_s)


def _delta(samples: list[Sample], field: str) -> int | None:
    """Last minus first for one cumulative counter.

    ``None`` when fewer than two samples carried it: one reading of a
    cumulative counter says how much the container has ever used, which is
    not what this window consumed. A counter that went backwards reports
    ``None`` rather than a negative or a wildly large number.
    """
    values = [getattr(s, field) for s in samples if getattr(s, field) is not None]
    if len(values) < 2:
        return None
    delta = values[-1] - values[0]
    return delta if delta >= 0 else None


def _readable(sample: Sample) -> bool:
    """Whether this sample actually carried a counter.

    A sample whose every counter is ``None`` is a heartbeat, not a reading.
    Counting heartbeats as coverage is how a sampler watching a container
    that died five seconds in would still claim to have measured the whole
    turn — the precise failure this distinction exists to prevent.
    """
    return any(
        getattr(sample, field) is not None
        for field in (
            "cpu_usec",
            "mem_bytes",
            "io_read_bytes",
            "io_write_bytes",
            "net_rx_bytes",
            "net_tx_bytes",
        )
    )
