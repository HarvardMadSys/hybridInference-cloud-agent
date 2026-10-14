"""Output a sandbox writes is read in bounded pieces by the trusted host.

Setup scripts, their dependencies' install hooks and the git a hostile
repository configures all write to pipes the runner reads. Reading those
line by line buffered a line whole before any limit was checked, so a stream
that never wrote a newline grew the trusted reader until it ran out of
memory. These run a real child process that floods without one.
"""

from __future__ import annotations

import shlex
import subprocess
import sys
import tracemalloc

import pytest

from cloud_agent_host import chat_sandbox as chat_sandbox_mod, runner as runner_mod
from cloud_agent_host.bounded_output import OUTPUT_CHUNK_BYTES, STDERR_TAIL_BYTES
from cloud_agent_host.chat_sandbox import (
    ChatSandboxInspection,
    ChatSandboxManager,
    ChatSandboxSpec,
    ChatSandboxStatus,
)
from cloud_agent_host.sandbox import (
    ContainerBackend,
    ProcessBackend,
    SandboxSpec,
)
from cloud_agent_host.setup import run_setup

#: Larger than every bound involved, the retained stderr tail included.
_FLOOD_CHARS = 16 * 1024 * 1024
#: Half the flood, and far above the stderr tail's own bound: reading a line
#: whole holds all of it at once, bounded reading never comes close.
_PEAK_BYTES = 8 * 1024 * 1024


def _flood(*, stdout: str = "x", stderr: str = "", exit_code: int = 0) -> str:
    """Python source that writes ``_FLOOD_CHARS`` with no newline and exits."""
    writes = [f"sys.stdout.write({stdout!r} * 65536)"]
    if stderr:
        writes.append(f"sys.stderr.write({stderr!r} * 65536)")
    loop = "; ".join(writes)
    return (
        "import sys\n"
        f"for _ in range({_FLOOD_CHARS // 65536}):\n"
        f"    {loop}\n"
        "sys.stdout.flush(); sys.stderr.flush()\n"
        f"raise SystemExit({exit_code})\n"
    )


def _peak_while(call):
    """Run ``call`` and return its result and the peak traced allocation."""
    tracemalloc.start()
    try:
        result = call()
        _current, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    return result, peak


class _ProcessBackend(ProcessBackend):
    def __init__(self) -> None:
        super().__init__(acknowledged_unsafe=True)
        self.processes = []

    def spawn(self, spec):
        process = super().spawn(spec)
        self.processes.append(process)
        return process


def test_a_setup_flood_with_no_newline_is_never_held_whole(tmp_path):
    backend = _ProcessBackend()
    script = f"{sys.executable} -c {shlex.quote(_flood(stderr='y', exit_code=7))}"

    result, peak = _peak_while(
        lambda: run_setup(script=script, workdir=str(tmp_path), backend=backend)
    )

    assert result.exit_code == 7
    # The failure detail is the tail of what setup printed, stderr last.
    assert result.detail == "y" * 500
    assert peak < _PEAK_BYTES
    # The stderr drain kept a byte-bounded tail, not one endless line.
    assert len(backend.processes[0].stderr_text().encode()) <= STDERR_TAIL_BYTES


def test_the_patch_reader_cap_holds_before_anything_is_buffered(tmp_path):
    process = ProcessBackend(acknowledged_unsafe=True).spawn(
        SandboxSpec(argv=[sys.executable, "-c", _flood()], workdir=str(tmp_path))
    )

    def read():
        with pytest.raises(runner_mod.WorktreeError, match="exceeded 65536 bytes"):
            runner_mod._read_bounded(process, deadline_s=60, max_bytes=65536)

    _result, peak = _peak_while(read)
    process.wait()

    assert peak < _PEAK_BYTES


def test_the_retained_exec_reads_both_streams_in_bounded_pieces(monkeypatch, tmp_path):
    spec = ChatSandboxSpec(
        thread_id="athr_01",
        sandbox_id="asbx_01",
        generation=3,
        runtime_id="claude-code",
    )
    manager = ChatSandboxManager(ContainerBackend(image="img"))
    running = ChatSandboxInspection(
        container_id="sha256:container-one",
        name=spec.container_name,
        status=ChatSandboxStatus.RUNNING,
        running=True,
        image="img",
        labels=spec.docker_labels(),
        mounts=(),
        networks=("agent-net",),
    )
    monkeypatch.setattr(manager, "_require", lambda _spec: running)
    real_popen = subprocess.Popen

    def flood(_argv, **kwargs):
        return real_popen([sys.executable, "-c", _flood(stderr="y")], **kwargs)

    monkeypatch.setattr(chat_sandbox_mod.subprocess, "Popen", flood)
    process = manager.spawn(spec, ["git", "diff"])

    sizes = [len(chunk.encode()) for chunk in process.chunks()]

    assert process.wait() == 0
    assert sum(sizes) == _FLOOD_CHARS
    assert max(sizes) <= OUTPUT_CHUNK_BYTES
    assert len(process.stderr_text().encode()) <= STDERR_TAIL_BYTES
