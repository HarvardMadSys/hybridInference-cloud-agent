"""Whole-container sampling primitives and honest shared-member fallbacks.

``ResourceSampler`` reads one Docker container's counters and rolls a closed
window up into one set of numbers.  That primitive remains useful for
group/residency diagnostics and for tests, but it is deliberately **not** bound
to a chat-member attempt: fork siblings can execute concurrently in the same
container, so assigning the container delta to either member would duplicate
the other's work.  Current shared turns instead start from
``shared_member_resource_rollup()`` and may add UID-filtered Kata guest process
telemetry and per-tool timing.  The empty top-level counters are an explicit
unavailable result, never a measured zero.

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

**Why this sampler is still outside.** Nothing is asked of the sandbox and no
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

import contextlib
import os
import threading
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

import httpx

from cloud_agent_host.logging_utils import get_logger
from cloud_agent_shared.shared_protocol import SHARED_MEMBER_ACCOUNTING_VERSION

if TYPE_CHECKING:
    from collections.abc import Callable

logger = get_logger(__name__)

#: Bumped when the meaning of a reported number changes, so a stored rollup
#: can always be read back with the semantics it was written under. ``2`` is
#: the move from host cgroupfs reads to the daemon's own accounting.
SAMPLER_VERSION = "2"


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


#: One sample per second. A container-owned accounting window is normally
#: measured in tens of seconds, so this is fine-grained without being a load
#: of its own — one small request per sandbox per second, on a connection
#: that stays open. Shared member turns deliberately do not use this sampler.
SAMPLE_INTERVAL_S = 1.0

#: How long to keep asking the daemon about a container before giving up.
#: ``docker run`` returns its client process well before the container
#: exists, and on a host that must first pull an image that gap is seconds,
#: not milliseconds — asking once at spawn time is asking too early, every
#: time.
BIND_TIMEOUT_S = 30.0

#: How often to re-ask while waiting for the container to appear. Much
#: shorter than the sampling interval on purpose: every millisecond between
#: the container starting and the sampler binding is consumption nobody
#: measured. The extra asks happen only during startup and stop the moment
#: one succeeds.
BIND_POLL_S = 0.1

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


class ContainerStats:
    """Reads one container's counters from the daemon.

    Identity is the container name the platform generated, so there is no pid
    to go stale: the pid-reuse hazard the cgroupfs reader had to guard
    against — a released pid handed to a host process, whose interface
    counters would arrive as this turn's network — cannot arise here.
    """

    def __init__(self, container: str, client: httpx.Client) -> None:
        self._container = container
        self._client = client

    def read(self) -> Sample | None:
        """Take one sample, or None once the container is gone.

        None is terminal, not a failed read: a 404 means the container no
        longer exists, and every later number would belong to something else.
        A transient error is not terminal and yields an empty sample, which
        counts against coverage without ending the window.
        """
        try:
            response = self._client.get(
                f"/containers/{self._container}/stats",
                # `one-shot` skips the daemon's initial pre-read, which exists
                # only to compute a CPU percentage from two samples. We take
                # our own deltas, so without it every tick would pay about a
                # second of the daemon waiting on our behalf.
                params={"stream": "false", "one-shot": "true"},
            )
        except httpx.HTTPError:
            return Sample(mono=time.monotonic())
        if response.status_code == 404:
            return None
        if response.status_code >= 400:
            return Sample(mono=time.monotonic())
        try:
            stats = response.json()
        except ValueError:
            return Sample(mono=time.monotonic())
        if not isinstance(stats, dict):
            return Sample(mono=time.monotonic())
        return sample_from_stats(stats, mono=time.monotonic())


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


def rollup(samples: list[Sample], *, window_s: float) -> dict[str, object]:
    """Turn a window's samples into the numbers one attempt row carries.

    ``coverage`` is the share of the window that readable samples actually
    span. It is the row's honesty: a sampler that bound late, died early, or
    read nothing at all produces a number below 1, and a reader must present
    that as incomplete rather than as a measurement.
    """
    read = [sample for sample in samples if _readable(sample)]
    if len(read) < 2 or window_s <= 0:
        return {
            "sample_count": len(read),
            "coverage": 0.0,
            "sampler_version": SAMPLER_VERSION,
        }
    observed = read[-1].mono - read[0].mono
    memory = [s.mem_bytes for s in read if s.mem_bytes is not None]
    return {
        "cpu_usec": _delta(read, "cpu_usec"),
        # Peak and mean come from our own samples, so both describe this
        # window; a since-creation high-water mark would not.
        "mem_peak_bytes": max(memory) if memory else None,
        "mem_avg_bytes": int(sum(memory) / len(memory)) if memory else None,
        "io_read_bytes": _delta(read, "io_read_bytes"),
        "io_write_bytes": _delta(read, "io_write_bytes"),
        "net_rx_bytes": _delta(read, "net_rx_bytes"),
        "net_tx_bytes": _delta(read, "net_tx_bytes"),
        "sample_count": len(read),
        "coverage": max(0.0, min(1.0, observed / window_s)),
        "sampler_version": SAMPLER_VERSION,
    }


def daemon_reachable(*, timeout_s: float = 5.0) -> str | None:
    """Check that the daemon answers for sampler tests/group-level callers."""
    try:
        with daemon_client(timeout_s=timeout_s) as client:
            response = client.get("/version")
    except httpx.HTTPError as exc:
        return f"the Docker API could not be reached ({exc})"
    except OSError as exc:
        return f"the Docker socket could not be opened ({exc})"
    if response.status_code >= 400:
        return f"the Docker API answered {response.status_code}"
    return None


class ResourceSampler:
    """Sample one Docker container for one container-owned window.

    Do not bind this to one member of a shared retained sandbox. Container
    totals include every concurrent sibling; the live topology uses
    :func:`shared_member_resource_rollup` plus UID-scoped guest observations.

    Binding is **lazy and retried**, because ``docker run`` returns a client
    process long before the daemon has created the container: asking who to
    measure at spawn time reliably asks too early and gets nothing. The
    sampler therefore starts immediately, keeps asking on its own thread
    until the container appears, and records how much of the window it
    managed to observe.

    Lives on a daemon thread so a stuck read can never hold up the turn's
    own exit, and every public method contains its own failures: the sampler
    is an observer, and an observer that can break the thing it observes is
    not worth having.
    """

    #: Bounded so a long turn cannot grow the runner's memory without limit.
    #: At one sample a second this is over three hours of window; older
    #: samples fall off the front, which costs the delta its true origin and
    #: is reported honestly through the shortened coverage span.
    MAX_SAMPLES = 12_000

    def __init__(
        self,
        counters: ContainerStats | None = None,
        *,
        resolver: Callable[[], ContainerStats | None] | None = None,
        interval_s: float = SAMPLE_INTERVAL_S,
        bind_timeout_s: float = BIND_TIMEOUT_S,
        closer: Callable[[], None] | None = None,
    ) -> None:
        self._counters = counters
        self._resolver = resolver
        self._interval = interval_s
        self._bind_timeout = bind_timeout_s
        self._closer = closer
        self._samples: list[Sample] = []
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._started_mono: float | None = None

    @property
    def active(self) -> bool:
        """Whether this sampler has something to measure, or may yet bind."""
        return self._counters is not None or self._resolver is not None

    def samples(self) -> list[Sample]:
        """This window's readings, for attributing spans narrower than it.

        A copy, and only meaningful once :meth:`stop` has taken the closing
        reading. The list covers the current binding alone: ``rebind`` throws
        the previous container's readings away with the container, so a call
        issued before a re-spawn correctly finds nothing to measure it with.
        """
        return list(self._samples)

    def start(self) -> None:
        """Begin sampling. Safe to call on an inert sampler."""
        self._started_mono = time.monotonic()
        if not self.active or self._thread is not None:
            return
        self._thread = threading.Thread(target=self._loop, name="resource-sampler", daemon=True)
        self._thread.start()

    def _bind(self, deadline: float) -> bool:
        """Resolve the container, retrying until it exists or time runs out."""
        if self._counters is not None:
            return True
        if self._resolver is None:
            return False
        while not self._stop.is_set() and time.monotonic() < deadline:
            try:
                counters = self._resolver()
            except Exception:
                counters = None
            if counters is not None:
                self._counters = counters
                return True
            self._stop.wait(BIND_POLL_S)
        return False

    def _sample_once(self) -> bool:
        """Append one sample; False once the container is gone."""
        counters = self._counters
        if counters is None:
            return False
        try:
            sample = counters.read()
        except Exception:
            return True
        if sample is None:
            return False
        self._samples.append(sample)
        del self._samples[: -self.MAX_SAMPLES]
        return True

    def _loop(self) -> None:
        """Bind, then sample until stopped or until the container exits."""
        if not self._bind(time.monotonic() + self._bind_timeout):
            return
        # An immediate first sample anchors the window at its true start: a
        # delta measured from one tick in would silently omit the busiest
        # second of most turns, and the missing span would read as a partial
        # measurement of a turn that was measured fine.
        while not self._stop.is_set():
            if not self._sample_once():
                return
            self._stop.wait(self._interval)

    def stop(self) -> dict[str, object]:
        """Stop sampling and return the window's rollup.

        A final sample is taken before the thread is joined so the window
        closes where it actually ended rather than at the last tick, which is
        what keeps a healthy turn's coverage at 1 instead of a systematic
        fraction below it.

        The join is bounded: a sampler thread wedged in a slow read must not
        delay the turn's terminal transition.
        """
        window = time.monotonic() - self._started_mono if self._started_mono is not None else 0.0
        self._stop.set()
        with contextlib.suppress(Exception):
            self._sample_once()
        thread, self._thread = self._thread, None
        if thread is not None:
            thread.join(timeout=2 * self._interval)
        if self._closer is not None:
            with contextlib.suppress(Exception):
                self._closer()
            self._closer = None
        return rollup(list(self._samples), window_s=window)
