"""The sampler against a real Docker daemon and a real container.

Everything else about the sampler is covered with mocked responses, which
proves the arithmetic and proves nothing about the wire: a fixture answers in
whatever shape its author imagined. The bug this whole module was rewritten
for — the runner reading a pid namespace it does not share — was invisible to
every mocked test and to the developer's own machine, and only appeared as
grey text in production.

So these run against a live daemon. They are marked ``external`` and skip
where there is none; run them with::

    pytest -m external tests/external/test_resource_sampler_live.py

On a host whose daemon is not at the default socket (colima, Docker Desktop
on some setups), export ``DOCKER_HOST`` first — the sampler honours it, which
is the same escape hatch the ``docker`` CLI uses.
"""

from __future__ import annotations

import subprocess
import time
import uuid

import pytest

from cloud_agent_host.resources import ResourceSampler, daemon_reachable
from cloud_agent_host.tool_windows import ToolWindow, attribute

pytestmark = pytest.mark.external


@pytest.fixture(autouse=True)
def _requires_daemon():
    """Skip rather than fail where no daemon is reachable."""
    reason = daemon_reachable(timeout_s=5.0)
    if reason is not None:
        pytest.skip(f"no reachable Docker daemon: {reason}")


def _run(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["docker", *args], capture_output=True, text=True, check=False)


@pytest.fixture
def busy_container():
    """A container that burns CPU for long enough to be measured."""
    name = f"hyi-test-{uuid.uuid4().hex[:12]}"
    started = _run(
        "run",
        "-d",
        "--name",
        name,
        "--rm",
        "alpine",
        "sh",
        "-c",
        "dd if=/dev/zero of=/dev/null bs=1M count=100000 2>/dev/null; sleep 20",
    )
    if started.returncode != 0:
        pytest.skip(f"could not start a container: {started.stderr.strip()[:200]}")
    try:
        yield name
    finally:
        _run("rm", "-f", name)


def test_a_busy_container_reports_the_cpu_it_actually_burned(busy_container):
    """The end-to-end proof: real socket, real daemon, real container.

    Asserted loosely on purpose. The exact number depends on the host, and a
    test that pinned it would fail for reasons that say nothing about this
    code. What must hold is that real work produces a real number and a full
    window reports full coverage.
    """
    sampler = ResourceSampler.for_container(busy_container, interval_s=0.5)
    sampler.start()
    time.sleep(4)

    result = sampler.stop()

    assert result["sample_count"] >= 3
    assert result["coverage"] > 0.9, (
        f"a fully observed window reported coverage {result['coverage']}, which would "
        "show a partial-measurement warning on a turn that was measured fine"
    )
    assert isinstance(result["cpu_usec"], int) and result["cpu_usec"] > 0
    assert isinstance(result["mem_peak_bytes"], int) and result["mem_peak_bytes"] > 0


def test_a_container_that_exits_mid_window_reports_partial_coverage():
    """The window outlives the container, and the row says so."""
    name = f"hyi-test-{uuid.uuid4().hex[:12]}"
    started = _run("run", "-d", "--name", name, "--rm", "alpine", "sleep", "2")
    if started.returncode != 0:
        pytest.skip(f"could not start a container: {started.stderr.strip()[:200]}")
    try:
        sampler = ResourceSampler.for_container(name, interval_s=0.5)
        sampler.start()
        time.sleep(6)
        result = sampler.stop()
    finally:
        _run("rm", "-f", name)

    assert result["sample_count"] >= 2
    assert result["coverage"] < 0.6, (
        "a window whose container died a third of the way through claimed to have "
        "measured it — coverage counts readings, not elapsed time"
    )
    # `sleep` burns no CPU, so the only honest answers are a measured zero or
    # — when the container went before two readable samples existed — nothing
    # at all. Pinning either one would make this test fail on timing rather
    # than on behaviour; what must never appear is a fabricated large number.
    assert result["cpu_usec"] in (0, None)


def test_binding_waits_for_a_container_that_is_still_starting():
    """The race that made cold turns measure nothing.

    ``docker run`` returns its client process before the daemon has created
    the container, so a sampler that asked once at spawn time bound to
    nothing. Here the sampler is started *before* the container exists.
    """
    name = f"hyi-test-{uuid.uuid4().hex[:12]}"
    sampler = ResourceSampler.for_container(name, interval_s=0.3)
    sampler.start()

    started = _run("run", "-d", "--name", name, "--rm", "alpine", "sleep", "10")
    if started.returncode != 0:
        sampler.stop()
        pytest.skip(f"could not start a container: {started.stderr.strip()[:200]}")
    try:
        time.sleep(3)
        result = sampler.stop()
    finally:
        _run("rm", "-f", name)

    assert result["sample_count"] >= 2, (
        "the sampler never bound to a container that appeared after it started, "
        "which is exactly the shape of a cold turn"
    )


#: Two seconds idle, five burning one core, two idle, then a 64 MiB write
#: and a sync. Every assertion below is against a number this script chose.
_PHASED_SCRIPT = (
    "sleep 2; "
    "end=$(( $(date +%s) + 5 )); while [ $(date +%s) -lt $end ]; do :; done; "
    "sleep 2; "
    "dd if=/dev/zero of=/tmp/blob bs=1M count=64 2>/dev/null; sync; "
    "sleep 4"
)


def _phase_windows(origin: float) -> list[ToolWindow]:
    """Tool calls drawn over the phases the script above runs."""
    return [
        ToolWindow("burn", "Bash", origin + 2.0, origin + 7.0),
        ToolWindow("write", "Write", origin + 9.0, origin + 13.0),
        ToolWindow("blink", "Read", origin + 13.2, origin + 13.235),
    ]


def test_a_known_burn_comes_back_as_itself():
    """The calibration: known work in, per-call numbers out.

    Everything else about attribution is unit-tested against synthetic
    samples, which proves the arithmetic and cannot prove it is being done on
    the right thing. This drives a container through phases whose sizes we
    chose, and checks the rows say so.
    """
    name = f"hyi-test-{uuid.uuid4().hex[:12]}"
    started = _run("run", "-d", "--name", name, "--rm", "alpine", "sh", "-c", _PHASED_SCRIPT)
    if started.returncode != 0:
        pytest.skip(f"could not start a container: {started.stderr.strip()[:200]}")
    try:
        sampler = ResourceSampler.for_container(name, interval_s=1.0)
        origin = time.monotonic()
        sampler.start()
        time.sleep(16)
        sampler.stop()
        samples = sampler.samples()
    finally:
        _run("rm", "-f", name)

    rows = {row["tool_use_id"]: row for row in attribute(samples, _phase_windows(origin))}

    burn = rows["burn"]["cpu_usec_exclusive"]
    assert burn is not None, "a five-second burn was reported as unmeasured"
    # A floor, not the total: the readings start after the call opened and end
    # before it closed, so the head and tail seconds fall outside them. Loose
    # bounds on purpose — the point is the shape, not the host's speed.
    assert 2_500_000 <= burn <= 5_200_000, f"a known 5s burn attributed {burn}us"
    assert rows["burn"]["measured_span_ms"] < rows["burn"]["duration_ms"]

    # The 97 % case, on a real daemon: nothing to measure it with.
    assert rows["blink"]["sample_count"] == 0
    assert rows["blink"]["cpu_usec_exclusive"] is None


def test_block_io_cannot_be_read_as_what_the_sandbox_wrote():
    """Why the turn footer stopped showing a disk figure.

    A container that writes a known 64 MiB and calls ``sync`` reports that
    exactly — sometimes. Run again on the same daemon it reports zero, with
    the same script and the same sync: the write is absorbed above the block
    layer and never reaches the counter. Both answers were observed minutes
    apart while building this.

    So the counter is not "block io rather than file io", which would at
    least be a true statement about something. It is a number that silently
    omits real writes, and a reader shown ``0`` would conclude the sandbox
    wrote nothing. It is still recorded — it costs nothing and belongs in the
    dataset — and it is no longer displayed.

    This test pins the ambiguity rather than either answer, so it stays true
    if a future daemon starts accounting reliably; what must never happen is
    the counter reporting some third thing that looks like a measurement.
    """
    name = f"hyi-test-{uuid.uuid4().hex[:12]}"
    written = 64 * 1024 * 1024
    started = _run("run", "-d", "--name", name, "--rm", "alpine", "sh", "-c", _PHASED_SCRIPT)
    if started.returncode != 0:
        pytest.skip(f"could not start a container: {started.stderr.strip()[:200]}")
    try:
        sampler = ResourceSampler.for_container(name, interval_s=1.0)
        sampler.start()
        time.sleep(16)
        rollup = sampler.stop()
    finally:
        _run("rm", "-f", name)

    observed = rollup["io_write_bytes"]
    assert observed in (None, 0, written), (
        f"a container that wrote {written} bytes and synced reported {observed} — "
        "neither the write, nor nothing, nor unmeasured"
    )
