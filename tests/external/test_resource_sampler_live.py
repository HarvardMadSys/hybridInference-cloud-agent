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
