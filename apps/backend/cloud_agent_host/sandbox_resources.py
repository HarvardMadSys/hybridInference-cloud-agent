"""Bounded background Docker resource observation for retained sandbox groups.

The sandbox manager supplies exact container identities from its label-verified group
inventory. Its inventory and the host heartbeat only read this collector's
cache; neither waits for Docker stats. These measurements belong to the whole
shared container and are never attributed to an individual member turn.
"""

from __future__ import annotations

import contextlib
import json
import math
import threading
import time
import uuid
from collections import deque
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

import httpx

from cloud_agent_host.logging_utils import get_logger
from cloud_agent_host.resources import Sample, daemon_client, sample_from_stats
from cloud_agent_shared.sandbox_resources import SandboxResourceSample

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping, Sequence

logger = get_logger(__name__)

MAX_TARGETS = 4096
MAX_SAMPLE_AGE_MS = 300_000
MAX_STATS_BYTES = 512 * 1024
_COUNTERS = ("cpu_usec", "io_read_bytes", "io_write_bytes", "net_rx_bytes", "net_tx_bytes")


def _counter(value: Any) -> int | None:
    """Read a Docker integer counter without treating bad data as zero."""
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else None


def _mapping(value: Any) -> dict[str, Any]:
    """Keep one optional stats object independent of malformed siblings."""
    return value if isinstance(value, dict) else {}


def _network_counter(networks: dict[str, Any], key: str) -> int | None:
    """Sum one direction only when every reported interface measured it."""
    if not networks:
        return None
    values = [_counter(_mapping(interface).get(key)) for interface in networks.values()]
    return sum(value for value in values if value is not None) if None not in values else None


@dataclass(frozen=True)
class DockerResourceReading:
    """One daemon reading on the sandbox manager's monotonic clock."""

    counters: Sample
    memory_limit_bytes: int | None = None
    pids: int | None = None


def reading_from_stats(stats: dict[str, Any], *, mono: float) -> DockerResourceReading:
    """Parse group counters while preserving independently missing metrics.

    The existing container sampler defines the cache-adjusted memory and CPU
    units. Normalizing the optional Docker objects keeps a bad sub-object from
    discarding valid readings of the others. Network directions and block I/O
    operations remain absent unless their own counters were reported.
    """
    memory = _mapping(stats.get("memory_stats"))
    memory_stats = _mapping(memory.get("stats"))
    cpu = _mapping(stats.get("cpu_stats"))
    usage = _mapping(cpu.get("cpu_usage"))
    normalized = {
        "cpu_stats": {"cpu_usage": {"total_usage": _counter(usage.get("total_usage"))}},
        "memory_stats": {
            "usage": _counter(memory.get("usage")),
            "stats": {"inactive_file": _counter(memory_stats.get("inactive_file"))},
        },
    }
    base = sample_from_stats(normalized, mono=mono)
    io_entries = _mapping(stats.get("blkio_stats")).get("io_service_bytes_recursive")
    io_totals: dict[str, int | None] = {"read": None, "write": None}
    if isinstance(io_entries, list):
        invalid: set[str] = set()
        for entry in io_entries:
            if not isinstance(entry, dict):
                continue
            op = str(entry.get("op") or "").lower()
            if op not in io_totals:
                continue
            value = _counter(entry.get("value"))
            if value is None:
                invalid.add(op)
            else:
                io_totals[op] = (io_totals[op] or 0) + value
        for op in invalid:
            io_totals[op] = None
    networks = _mapping(stats.get("networks"))
    return DockerResourceReading(
        counters=Sample(
            mono=mono,
            cpu_usec=base.cpu_usec,
            mem_bytes=base.mem_bytes,
            io_read_bytes=io_totals["read"],
            io_write_bytes=io_totals["write"],
            net_rx_bytes=_network_counter(networks, "rx_bytes"),
            net_tx_bytes=_network_counter(networks, "tx_bytes"),
        ),
        memory_limit_bytes=_counter(memory.get("limit")),
        pids=_counter(_mapping(stats.get("pids_stats")).get("current")),
    )


def resource_sample(
    current: DockerResourceReading,
    previous: DockerResourceReading | None,
) -> SandboxResourceSample:
    """Derive interval rates, warming up after missing or observed counter resets."""
    sample = current.counters
    baseline = None if previous is None else previous.counters
    window = None if baseline is None else sample.mono - baseline.mono
    if window is not None and not 0 < window <= MAX_SAMPLE_AGE_MS / 1000:
        window = None
    if baseline is not None and any(
        getattr(sample, key) is not None
        and getattr(baseline, key) is not None
        and getattr(sample, key) < getattr(baseline, key)
        for key in _COUNTERS
    ):
        # An exact Docker ID can restart. A counter regression proves a reset,
        # so rewarm every rate even if another counter did not fall. A restart
        # entirely between observations cannot always be detected this way.
        window = None

    def rate(key: str) -> float | None:
        value = getattr(sample, key)
        before = None if baseline is None else getattr(baseline, key)
        if window is None or value is None or before is None:
            return None
        try:
            result = (value - before) / window
        except OverflowError:
            return None
        return result if math.isfinite(result) else None

    cpu_rate = rate("cpu_usec")
    return SandboxResourceSample(
        sample_id=str(uuid.uuid4()),
        sample_age_ms=0.0,
        cpu_cores=None if cpu_rate is None else cpu_rate / 1_000_000,
        cpu_window_seconds=None if cpu_rate is None else window,
        memory_bytes=sample.mem_bytes,
        memory_limit_bytes=current.memory_limit_bytes,
        pids=current.pids,
        io_read_bytes=sample.io_read_bytes,
        io_write_bytes=sample.io_write_bytes,
        net_rx_bytes=sample.net_rx_bytes,
        net_tx_bytes=sample.net_tx_bytes,
        io_read_bytes_per_second=rate("io_read_bytes"),
        io_write_bytes_per_second=rate("io_write_bytes"),
        net_rx_bytes_per_second=rate("net_rx_bytes"),
        net_tx_bytes_per_second=rate("net_tx_bytes"),
    )


class SandboxResourceCollector:
    """Fairly sample a bounded exact-ID target set outside the heartbeat path."""

    def __init__(
        self,
        *,
        interval_s: float = 15.0,
        read_timeout_s: float = 1.0,
        cycle_budget_s: float = 5.0,
        max_reads_per_cycle: int = 64,
        clock: Callable[[], float] = time.monotonic,
        client_factory: Callable[[], httpx.Client] | None = None,
    ) -> None:
        """Configure bounded worker timing; injectable clocks/clients support tests."""
        if any(
            not math.isfinite(value) or value <= 0
            for value in (interval_s, read_timeout_s, cycle_budget_s)
        ):
            raise ValueError("resource collector intervals must be finite and positive")
        if not 1 <= max_reads_per_cycle <= MAX_TARGETS:
            raise ValueError("resource collector read limit is outside the target bound")
        self._interval_s = interval_s
        self._read_timeout_s = read_timeout_s
        self._cycle_budget_s = cycle_budget_s
        self._max_reads = max_reads_per_cycle
        self._clock = clock
        self._client_factory = client_factory or (
            lambda: daemon_client(timeout_s=self._read_timeout_s)
        )
        self._lock = threading.Lock()
        self._targets: dict[str, object] = {}
        self._rotation: deque[str] = deque()
        self._previous: dict[str, DockerResourceReading] = {}
        self._samples: dict[str, tuple[SandboxResourceSample, float]] = {}
        self._stop = threading.Event()
        self._wake = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        """Start one worker; construction alone never opens the Docker socket."""
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._run, daemon=True, name="sandbox-resource-collector"
        )
        self._thread.start()

    def close(self) -> None:
        """Stop the worker and release its client before sandbox manager shutdown."""
        self._stop.set()
        self._wake.set()
        if self._thread is not None and self._thread.is_alive():
            self._thread.join(timeout=self._read_timeout_s + 2.0)
        with self._lock:
            self._targets.clear()
            self._rotation.clear()
            self._previous.clear()
            self._samples.clear()

    def sync_targets(self, rows: Sequence[Mapping[str, Any]]) -> None:
        """Publish running targets from the sandbox manager's label-verified group listing."""
        identities = dict.fromkeys(
            row["container_id"]
            for row in rows[:MAX_TARGETS]
            if row.get("running") is True
            and isinstance(row.get("container_id"), str)
            and 0 < len(row["container_id"]) <= 128
        )
        with self._lock:
            changed = identities.keys() != self._targets.keys()
            for container in self._targets.keys() - identities.keys():
                self._previous.pop(container, None)
                self._samples.pop(container, None)
            self._targets = {
                container: self._targets.get(container, object()) for container in identities
            }
            self._rotation = deque(
                container for container in self._rotation if container in self._targets
            )
            queued = set(self._rotation)
            self._rotation.extend(container for container in identities if container not in queued)
        if changed:
            self._wake.set()

    def latest(self, container_id: str) -> dict[str, Any] | None:
        """Read the cache without Docker I/O, preserving the original sample age."""
        with self._lock:
            cached = self._samples.get(container_id)
        if cached is None:
            return None
        sample, sampled_at = cached
        age_ms = max(0.0, (self._clock() - sampled_at) * 1000)
        if not math.isfinite(age_ms) or age_ms > MAX_SAMPLE_AGE_MS:
            return None
        payload = sample.model_dump()
        payload["sample_age_ms"] = age_ms
        return payload

    def sample_cycle(self, client: httpx.Client) -> int:
        """Observe at most one fair, bounded batch; never hold the lock during I/O."""
        deadline = self._clock() + self._cycle_budget_s
        with self._lock:
            count = min(len(self._rotation), self._max_reads)
        reads = 0
        for _ in range(count):
            remaining = deadline - self._clock()
            if remaining <= 0 or self._stop.is_set():
                break
            with self._lock:
                if not self._rotation:
                    break
                container = self._rotation.popleft()
                self._rotation.append(container)
                token = self._targets.get(container)
            if token is None:
                continue
            reading = self._read(client, container, timeout=min(self._read_timeout_s, remaining))
            reads += 1
            with self._lock:
                if self._targets.get(container) is not token:
                    continue
                if reading is None:
                    self._previous.pop(container, None)
                    self._samples.pop(container, None)
                else:
                    sample = resource_sample(reading, self._previous.get(container))
                    self._previous[container] = reading
                    self._samples[container] = (sample, reading.counters.mono)
        return reads

    def _read(
        self, client: httpx.Client, container: str, *, timeout: float
    ) -> DockerResourceReading | None:
        deadline = self._clock() + timeout
        try:
            with client.stream(
                "GET",
                f"/containers/{container}/stats",
                params={"stream": "false", "one-shot": "true"},
                headers={"Accept-Encoding": "identity"},
                timeout=timeout,
            ) as response:
                if response.is_error:
                    return None
                content = bytearray()
                # Mock transports can return an already buffered response.
                # Real daemon responses are read raw, before decompression or
                # buffering, so the cap bounds what this observer retains.
                chunks = (
                    iter((response.content,))
                    if response.is_stream_consumed
                    else response.iter_raw()
                )
                for chunk in chunks:
                    if (
                        self._stop.is_set()
                        or self._clock() >= deadline
                        or len(content) + len(chunk) > MAX_STATS_BYTES
                    ):
                        return None
                    content.extend(chunk)
                if self._stop.is_set() or self._clock() >= deadline:
                    return None
                body = json.loads(content)
                sampled_at = self._clock()
                if sampled_at >= deadline:
                    return None
                return reading_from_stats(body, mono=sampled_at) if isinstance(body, dict) else None
        except (httpx.HTTPError, httpx.StreamError, OSError, TypeError, ValueError, OverflowError):
            return None

    def _run(self) -> None:
        client: httpx.Client | None = None
        try:
            while not self._stop.is_set():
                self._wake.clear()
                try:
                    if client is None:
                        client = self._client_factory()
                    self.sample_cycle(client)
                except Exception:
                    logger.warning("sandbox_resource_sample_failed", exc_info=True)
                self._wake.wait(self._interval_s)
        finally:
            if client is not None:
                with contextlib.suppress(Exception):
                    client.close()
