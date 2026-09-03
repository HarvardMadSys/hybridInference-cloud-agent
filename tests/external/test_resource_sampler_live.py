"""The retained resource primitives against a real Docker daemon.

Shared-live turns intentionally report zero coverage until process-tree
accounting lands: a whole-container total would duplicate concurrent sibling
usage.  The daemon reader remains the accounting backend, however, so these
tests keep the real wire contract covered without reviving the removed
one-container-per-turn binding API.
"""

from __future__ import annotations

import subprocess
import time
import uuid

import pytest

from cloud_agent_host.resources import (
    ContainerStats,
    ResourceSampler,
    daemon_client,
    daemon_reachable,
)

pytestmark = pytest.mark.external


@pytest.fixture(autouse=True)
def _requires_daemon():
    """Skip rather than fail where no daemon is reachable."""
    reason = daemon_reachable(timeout_s=5.0)
    if reason is not None:
        pytest.skip(f"no reachable Docker daemon: {reason}")


def _run(*args: str) -> subprocess.CompletedProcess[str]:
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


def _sampler(container: str, *, interval_s: float) -> ResourceSampler:
    client = daemon_client()
    return ResourceSampler(
        ContainerStats(container, client),
        interval_s=interval_s,
        closer=client.close,
    )


def test_a_busy_container_reports_the_cpu_it_actually_burned(busy_container):
    """A real daemon response produces usable CPU and memory counters."""
    sampler = _sampler(busy_container, interval_s=0.5)
    sampler.start()
    time.sleep(4)

    result = sampler.stop()

    assert result["sample_count"] >= 3
    assert result["coverage"] > 0.9
    assert isinstance(result["cpu_usec"], int) and result["cpu_usec"] > 0
    assert isinstance(result["mem_peak_bytes"], int) and result["mem_peak_bytes"] > 0


def test_a_container_that_exits_mid_window_reports_partial_coverage():
    """A vanished container shortens coverage instead of inventing zeros."""
    name = f"hyi-test-{uuid.uuid4().hex[:12]}"
    started = _run("run", "-d", "--name", name, "--rm", "alpine", "sleep", "2")
    if started.returncode != 0:
        pytest.skip(f"could not start a container: {started.stderr.strip()[:200]}")
    try:
        sampler = _sampler(name, interval_s=0.5)
        sampler.start()
        time.sleep(6)
        result = sampler.stop()
    finally:
        _run("rm", "-f", name)

    assert result["sample_count"] >= 2
    assert result["coverage"] < 0.6
    assert result["cpu_usec"] in (0, None)
