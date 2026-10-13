"""Exercise scoped command cleanup through real Docker exec attachments.

Use a locally available POSIX shell image with coreutils and util-linux setsid;
AGENT_COMMAND_TEST_IMAGE defaults to postgres:15. Images are never pulled here.
Each test owns one isolated, network-free container. The retained backend's
network proof is deliberately seeded: this verifies command cleanup and Docker
output backpressure, not network policy, Kata, or Agent CLI compatibility.
"""

from __future__ import annotations

import contextlib
import json
import os
import shlex
import shutil
import subprocess
import time
import uuid
from typing import TYPE_CHECKING

import pytest
from fastapi import HTTPException

from cloud_agent_host.chat_sandbox import (
    ACTIVATION_TMPFS,
    CHAT_NAMESPACE_LABEL,
    CHAT_WORKDIR,
    ChatSandboxManager,
    ChatSandboxSpec,
    RetainedChatBackend,
)
from cloud_agent_host.sandbox import ContainerBackend, SandboxProcess, SandboxSpec
from cloud_agent_host.sandbox_manager import _run_sandbox

if TYPE_CHECKING:
    from pathlib import Path

pytestmark = [pytest.mark.external, pytest.mark.timeout(90)]
IMAGE = os.environ.get("AGENT_COMMAND_TEST_IMAGE", "postgres:15")
DOCKER_SELECTORS = (
    "DOCKER_HOST",
    "DOCKER_CONTEXT",
    "DOCKER_TLS",
    "DOCKER_TLS_VERIFY",
    "DOCKER_CERT_PATH",
)


def _docker(
    *arguments: str, check: bool = True, timeout_s: float = 10
) -> subprocess.CompletedProcess[str]:
    """Address only the local daemon and retain registry/proxy configuration."""
    environment = os.environ.copy()
    for selector in DOCKER_SELECTORS:
        environment.pop(selector, None)
    return subprocess.run(
        ["docker", "--host=unix:///var/run/docker.sock", *arguments],
        env=environment,
        capture_output=True,
        text=True,
        check=check,
        timeout=timeout_s,
    )


class _TrackedRetainedBackend(RetainedChatBackend):
    """Record attached clients so failed tests can unblock daemon cleanup."""

    def __init__(self, manager: ChatSandboxManager, spec: ChatSandboxSpec) -> None:
        super().__init__(manager, spec, network_proved_at=time.monotonic())
        self.processes: list[SandboxProcess] = []

    def spawn(self, spec: SandboxSpec) -> SandboxProcess:
        """Run the production spawn unchanged and retain its cleanup handle."""
        process = super().spawn(spec)
        self.processes.append(process)
        return process

    def close_attached_clients(self) -> None:
        """Release host pipes before removing this test's container on failure."""
        for process in self.processes:
            # The production retained process wraps a Popen Docker client. A
            # failed reaper must not leave its full stdout pipe blocking the
            # daemon's exec completion or even container removal.
            client = process._process  # type: ignore[attr-defined]
            if client.poll() is None:
                with contextlib.suppress(ProcessLookupError):
                    client.kill()
            with contextlib.suppress(subprocess.TimeoutExpired):
                client.wait(timeout=2)


@pytest.fixture
def retained_command_container(tmp_path: Path):
    """Create one label-fenced retained fixture and always remove only it."""
    if not shutil.which("docker"):
        pytest.skip("retained command cleanup requires the local Docker daemon")
    try:
        if _docker("info", check=False).returncode:
            pytest.skip("retained command cleanup requires the local Docker daemon")
        image = _docker("image", "inspect", IMAGE, check=False)
    except (OSError, subprocess.TimeoutExpired):
        pytest.skip("the local Docker daemon is unavailable")
    if image.returncode:
        pytest.skip(f"pull the command test image first: {IMAGE}")

    wrapper = tmp_path / "docker-local"
    wrapper.write_text(
        "#!/bin/sh\nexec env "
        + " ".join(f"-u {selector}" for selector in DOCKER_SELECTORS)
        + f" {shlex.quote(shutil.which('docker') or 'docker')} "
        + '--host=unix:///var/run/docker.sock "$@"\n'
    )
    wrapper.chmod(0o700)
    token = uuid.uuid4().hex
    outer = ContainerBackend(
        image=IMAGE,
        docker_binary=str(wrapper),
        sandbox_namespace=f"command-limit-test-{token}",
    )
    spec = ChatSandboxSpec(
        thread_id=f"command-limit-thread-{token}",
        sandbox_id=f"command-limit-sandbox-{token}",
        generation=1,
        runtime_id="codex",
        memory_limit="512m",
        cpu_limit="1",
    )
    manager = ChatSandboxManager(outer)
    backend = _TrackedRetainedBackend(manager, spec)
    create = [
        "run",
        "--detach",
        "--pull",
        "never",
        "--name",
        spec.container_name,
        "--network",
        "none",
        "--memory",
        spec.memory_limit,
        "--cpus",
        spec.cpu_limit,
        "--tmpfs",
        f"{ACTIVATION_TMPFS}:rw,noexec,nosuid,nodev,mode=0700,uid={outer.uid},gid={outer.gid}",
    ]
    # postgres:15 declares a data volume. Override every image volume with
    # tmpfs so the production retained mount check sees no bind/volume mounts.
    volumes = json.loads(image.stdout)[0]["Config"].get("Volumes") or {}
    for destination in volumes:
        if destination != ACTIVATION_TMPFS:
            create.extend(["--tmpfs", f"{destination}:rw,noexec,nosuid,nodev"])
    labels = {**spec.docker_labels(), CHAT_NAMESPACE_LABEL: outer.sandbox_namespace}
    for key, value in labels.items():
        create.extend(["--label", f"{key}={value}"])
    create.extend(
        [
            "--entrypoint",
            "/bin/sh",
            IMAGE,
            "-c",
            f"mkdir -p {CHAT_WORKDIR} {outer.home}; "
            f"chown {outer.uid}:{outer.gid} {CHAT_WORKDIR} {outer.home}; exec sleep 120",
        ]
    )
    try:
        # A vfs daemon copies the complete cached image for every container.
        # Allow that fixture work separately from the command cleanup deadline.
        _docker(*create, timeout_s=30)
        deadline = time.monotonic() + 5
        while _docker(
            "exec", spec.container_name, "test", "-d", CHAT_WORKDIR, check=False
        ).returncode:
            assert time.monotonic() < deadline, "test workspace never became ready"
            time.sleep(0.05)
        # These are actual Docker inspections, not patched identity or mount
        # checks. Spawning and reaping likewise use the production wrapper.
        assert manager._require(spec).running
        _docker(
            "exec",
            spec.container_name,
            "/bin/sh",
            "-c",
            'for tool in setsid sed stat yes sleep find wc; do command -v "$tool" || exit 1; done',
        )
        yield backend
    finally:
        backend.close_attached_clients()
        cleanup = _docker("rm", "--force", "--volumes", spec.container_name, check=False)
        assert cleanup.returncode == 0 or "No such container" in cleanup.stderr, cleanup.stderr


def _ticks(backend: _TrackedRetainedBackend) -> int:
    return int(
        _docker(
            "exec",
            backend.spec.container_name,
            "/bin/sh",
            "-c",
            f"wc -l < {CHAT_WORKDIR}/sibling.ticks",
        ).stdout.strip()
    )


@pytest.mark.parametrize("failure", ["timeout", "overflow"])
def test_command_limits_reap_only_the_failed_command(retained_command_container, failure):
    """Timeout and overflowing Docker attachments preserve a live sibling."""
    backend = retained_command_container
    sibling = backend.spawn(
        SandboxSpec(
            argv=[
                "/bin/sh",
                "-c",
                "echo $$ > /workspace/sibling.pid; "
                "while :; do echo tick >> /workspace/sibling.ticks; sleep 0.1; done",
            ],
            workdir=CHAT_WORKDIR,
        )
    )
    deadline = time.monotonic() + 5
    while _docker(
        "exec", backend.spec.container_name, "test", "-s", "/workspace/sibling.ticks", check=False
    ).returncode:
        assert time.monotonic() < deadline, "sibling never became ready"
        time.sleep(0.05)
    before = _ticks(backend)
    command = "sleep 60" if failure == "timeout" else "yes output"
    started = time.monotonic()
    with pytest.raises(HTTPException) as caught:
        _run_sandbox(
            backend,
            workdir=CHAT_WORKDIR,
            script=f"echo $$ > /workspace/command.pid; {command} & "
            "echo $! > /workspace/command.child.pid; wait",
            env={},
            timeout_s=0.5 if failure == "timeout" else 5,
            max_output_bytes=1024,
        )
    assert caught.value.status_code == (408 if failure == "timeout" else 413)
    assert time.monotonic() - started < 10, "cleanup stalled behind a Docker attachment"
    time.sleep(0.25)
    assert _ticks(backend) > before
    assert sibling.poll() is None
    assert backend.manager._require(backend.spec).running
    census = _docker(
        "exec",
        backend.spec.container_name,
        "/bin/sh",
        "-c",
        r"""
set -eu
leader=$(cat /workspace/command.pid)
child=$(cat /workspace/command.child.pid)
for task in /proc/[0-9]*/stat; do
    read -r raw < "$task" 2>/dev/null || continue
    set -- ${raw##*) }
    [ "${3:-}" = "$leader" ] || continue
    case "${1:-}" in Z|X) ;; *) echo "live failed-command descendant: $task"; exit 1;; esac
done
if [ -r "/proc/$child/stat" ]; then
    fields=$(sed 's/^[^)]*) //' "/proc/$child/stat")
    set -- $fields
    case "$1" in Z|X) ;; *) echo "live failed-command child: $child"; exit 1;; esac
fi
count=0
sibling=$(cat /workspace/sibling.pid)
for file in /run/agent-activation/command-*.pid; do
    [ -f "$file" ] || continue
    read -r pid ignored < "$file"
    [ "$pid" = "$sibling" ] || { echo "failed command pidfile remains"; exit 1; }
    count=$((count + 1))
done
[ "$count" = 1 ]
""",
        check=False,
    )
    assert census.returncode == 0, census.stdout + census.stderr
    sibling.kill()
    sibling.wait()
    assert backend.manager._require(backend.spec).running
    remaining = _docker(
        "exec", backend.spec.container_name, "find", ACTIVATION_TMPFS, "-name", "command-*.pid"
    ).stdout.strip()
    assert not remaining, remaining
