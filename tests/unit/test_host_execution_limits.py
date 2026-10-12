"""Host memory bounds and shared-container command cleanup regressions."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from cloud_agent_host import bounded_output, chat_sandbox
from cloud_agent_host.bounded_output import (
    OUTPUT_CHUNK_BYTES,
    OUTPUT_QUEUE_ITEMS,
    STDERR_TAIL_BYTES,
    OutputPump,
    protocol_lines,
    text_chunks,
)
from cloud_agent_host.chat_sandbox import ChatSandboxManager, ChatSandboxSpec, RetainedChatBackend
from cloud_agent_host.sandbox import ContainerBackend, ProcessBackend, SandboxError, SandboxSpec
from cloud_agent_host.workspace_broker import _run_sandbox


@pytest.mark.parametrize("failure", ["timeout", "output"])
def test_bounded_command_cleanup_preserves_a_running_sibling(monkeypatch, tmp_path, failure):
    """Run the actual fenced wrapper/reaper locally, with no Docker operation."""
    monkeypatch.setattr(chat_sandbox, "ACTIVATION_TMPFS", str(tmp_path))
    spec = ChatSandboxSpec(
        thread_id="athr_test", sandbox_id="asbx_test", generation=1, runtime_id="codex"
    )
    manager = ChatSandboxManager(ContainerBackend(image="test", uid=os.getuid(), gid=os.getgid()))
    monkeypatch.setattr(manager, "_require", lambda _spec: SimpleNamespace(running=True))
    monkeypatch.setattr(manager, "require_network", lambda *_args, **_kw: None)
    monkeypatch.setattr(manager, "build_exec_command", lambda _spec, argv, **_kw: argv)
    monkeypatch.setattr(
        manager,
        "exec_capture",
        lambda _spec, argv, **kw: subprocess.run(
            argv, capture_output=True, timeout=kw["timeout_s"], check=False
        ),
    )
    stopped = []
    monkeypatch.setattr(manager, "stop", lambda _spec: stopped.append(_spec))
    sibling = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(20)"])
    script = "sleep 20" if failure == "timeout" else "yes output"
    try:
        with pytest.raises(HTTPException) as caught:
            _run_sandbox(
                RetainedChatBackend(manager, spec),
                workdir="/workspace",
                script=script,
                env={},
                timeout_s=0.2 if failure == "timeout" else 5,
                max_output_bytes=1024,
            )
        assert caught.value.status_code == (408 if failure == "timeout" else 413)
        assert stopped == []
        assert sibling.poll() is None
        assert list(tmp_path.glob("command-*.pid")) == []
    finally:
        sibling.kill()
        sibling.wait()


def test_newline_free_stdout_is_read_in_bounded_chunks():
    process = ProcessBackend(acknowledged_unsafe=True).spawn(
        SandboxSpec(
            argv=[sys.executable, "-c", 'import sys; sys.stdout.write("x" * (5 * 1024 * 1024))'],
            workdir="/tmp",
        )
    )
    sizes = [len(chunk.encode()) for chunk in process.chunks()]
    assert sum(sizes) == 5 * 1024 * 1024
    assert max(sizes) <= OUTPUT_CHUNK_BYTES
    assert process.wait() == 0


def test_command_reaper_refuses_a_stale_start_receipt(tmp_path):
    """A PID reused by a different process is a cleanup failure, never a kill target."""
    sibling = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(20)"], start_new_session=True
    )
    pidfile = tmp_path / "command-stale.pid"
    pidfile.write_text(f"{sibling.pid} 0\n")
    try:
        result = subprocess.run(
            [
                "/bin/sh",
                "-c",
                ChatSandboxManager._member_reaper_script('"$2"'),
                "sh",
                str(os.getuid()),
                str(pidfile),
            ],
            capture_output=True,
            timeout=5,
            check=False,
        )
        assert result.returncode == 126
        assert sibling.poll() is None
        assert not pidfile.exists()
    finally:
        sibling.kill()
        sibling.wait()


def test_newline_free_stderr_retains_only_a_byte_bounded_tail():
    process = ProcessBackend(acknowledged_unsafe=True).spawn(
        SandboxSpec(
            argv=[
                sys.executable,
                "-c",
                'import sys; sys.stderr.write("x" * (5 * 1024 * 1024) + "TAIL")',
            ],
            workdir="/tmp",
        )
    )
    assert process.wait() == 0
    tail = process.stderr_text()
    assert len(tail.encode()) <= STDERR_TAIL_BYTES
    assert tail.endswith("TAIL")


def test_oversized_native_protocol_record_is_refused_before_full_accumulation():
    with pytest.raises(SandboxError, match="output record"):
        list(protocol_lines(iter(["x" * 512] * 10), max_bytes=1024))


def test_fast_producer_backpressures_and_can_be_abandoned():
    produced = 0
    reached_capacity = threading.Event()

    def values():
        nonlocal produced
        for _ in range(1_000_000):
            produced += 1
            if produced == OUTPUT_QUEUE_ITEMS + 1:
                reached_capacity.set()
            yield "x" * OUTPUT_CHUNK_BYTES

    pump = OutputPump(values(), name="test-backpressure")
    try:
        assert reached_capacity.wait(1)
        assert pump.queue.qsize() == OUTPUT_QUEUE_ITEMS
        assert produced == OUTPUT_QUEUE_ITEMS + 1
    finally:
        pump.close()
        pump.thread.join(timeout=1)
    assert not pump.thread.is_alive()


def test_protocol_crlf_split_across_chunks_does_not_create_a_blank_record():
    assert list(protocol_lines(iter(['{"a":1}\r', '\n{"b":2}\r', "\n"]))) == [
        '{"a":1}\n',
        '{"b":2}\n',
    ]


def test_unicode_separators_inside_native_json_keep_one_record():
    record = json.dumps({"text": "before\u2028middle\u2029after"}, ensure_ascii=False) + "\n"
    lines = list(protocol_lines(iter([record[:15], record[15:]])))
    assert lines == [record]
    assert json.loads(lines[0])["text"] == "before\u2028middle\u2029after"
    assert list(protocol_lines(iter(["a\vb\fc\r", "d\n"]))) == ["a\vb\fc\rd\n"]


def test_pipe_decoder_preserves_a_utf8_character_split_across_reads(monkeypatch):
    reads = iter([b"start \xe2", b"\x82", b"\xac end", b""])
    monkeypatch.setattr(bounded_output.os, "read", lambda *_args: next(reads))
    assert "".join(text_chunks(SimpleNamespace(fileno=lambda: 42))) == "start \u20ac end"


def test_graceful_close_discards_output_without_blocking_history_flush():
    remaining = threading.Event()
    flushed = threading.Event()

    def values():
        yield "before"
        assert remaining.wait(1)
        for _ in range(OUTPUT_QUEUE_ITEMS * 3):
            yield "during EOF flush"
        flushed.set()

    pump = OutputPump(values(), name="test-graceful-drain")
    assert pump.get(timeout=1) == "before"
    pump.close(drain=True)
    remaining.set()
    assert flushed.wait(1)
    pump.thread.join(timeout=1)
    assert not pump.thread.is_alive()


def test_output_overflow_keeps_draining_during_scoped_cleanup():
    """Daemon attachment progress must not block the exec that reaps a command."""
    cleanup_started = threading.Event()
    discarded = threading.Event()

    class Process:
        @staticmethod
        def chunks():
            yield "x" * OUTPUT_CHUNK_BYTES
            assert cleanup_started.wait(1)
            for _ in range(OUTPUT_QUEUE_ITEMS + 1):
                yield "x" * OUTPUT_CHUNK_BYTES
            discarded.set()

        lines = chunks

        @staticmethod
        def kill():
            cleanup_started.set()
            assert discarded.wait(1), "cleanup cannot progress while its stdout attach is blocked"

        @staticmethod
        def wait():
            return -15

    backend = SimpleNamespace(base_env=lambda: {}, spawn=lambda _spec: Process())
    with pytest.raises(HTTPException) as caught:
        _run_sandbox(
            backend, workdir="/workspace", script="", env={}, timeout_s=5, max_output_bytes=1024
        )
    assert caught.value.status_code == 413
