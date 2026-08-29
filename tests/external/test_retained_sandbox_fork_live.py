"""Real-Docker acceptance for a complete, independently diverging chat fork."""

from __future__ import annotations

import json
import os
import secrets
import signal
import threading

import pytest

from cloud_agent_host.chat_sandbox import ChatSandboxManager, ChatSandboxSpec
from cloud_agent_host.sandbox import ContainerBackend
from cloud_agent_host.session_supervisor import ChatSessionSupervisor

pytestmark = pytest.mark.external


def _capture(manager: ChatSandboxManager, spec: ChatSandboxSpec, script: str) -> str:
    result = manager.exec_capture(
        spec,
        ["/bin/sh", "-c", f"set -eu; {script}"],
        timeout_s=60,
    )
    assert result.returncode == 0, result.stderr.decode("utf-8", "replace")
    return result.stdout.decode("utf-8", "replace")


def test_real_docker_full_fork_preserves_then_independently_diverges():
    image = (os.getenv("AGENT_EXTERNAL_SANDBOX_IMAGE") or "").strip()
    network = (os.getenv("AGENT_EXTERNAL_SANDBOX_NETWORK") or "").strip()
    if not image or not network:
        pytest.skip(
            "set AGENT_EXTERNAL_SANDBOX_IMAGE and AGENT_EXTERNAL_SANDBOX_NETWORK "
            "to run the real retained-fork acceptance"
        )

    nonce = secrets.token_hex(8)
    source = ChatSandboxSpec(
        thread_id=f"athr_external_source_{nonce}",
        sandbox_id=f"asbx_external_source_{nonce}",
        generation=1,
        runtime_id="claude-code",
        sandbox_image_digest=image,
        stop_timeout_s=2,
    )
    target_thread_id = f"athr_external_target_{nonce}"
    target_sandbox_id = f"asbx_external_target_{nonce}"
    operation_id = f"afork_external_{nonce}"
    manager = ChatSandboxManager(ContainerBackend(image=image, network=network))
    supervisor = ChatSessionSupervisor(manager, idle_timeout_s=1800)
    target_spec: ChatSandboxSpec | None = None
    target_container_id: str | None = None
    source_container_id: str | None = None
    try:
        lease = supervisor.acquire(source, owner="external:test", has_settled_context=False)
        source_container_id = lease.container_id
        manager.require_network_phase(source, "agent")
        _capture(
            manager,
            source,
            "git init -q; git config user.email agent@example.test; "
            "git config user.name Agent; printf 'base\\n' > README.md; "
            "git add README.md; git commit -qm base; "
            "printf 'dirty\\n' >> README.md; printf 'staged\\n' > staged.txt; "
            "git add staged.txt; printf 'untracked\\n' > untracked.txt; "
            "mkdir -p /agent-state/native; "
            "printf 'opaque-session-state\\n' > /agent-state/native/session.log; sync",
        )
        before = _capture(manager, source, "git status --porcelain=v1")
        supervisor.settle(
            source.thread_id,
            lease.token,
            job_id="ajob_external_anchor",
            context_status="new",
        )

        forked = supervisor.fork_settled(
            source.thread_id,
            operation_id=operation_id,
            source_sandbox_id=source.sandbox_id,
            source_generation=source.generation,
            source_activation_generation=lease.activation_generation,
            source_container_id=lease.container_id,
            source_anchor_job_id="ajob_external_anchor",
            source_native_session_id="native-external-session",
            target_thread_id=target_thread_id,
            target_sandbox_id=target_sandbox_id,
        )
        target_container_id = forked.target_container_id
        target_spec = ChatSandboxSpec(
            thread_id=target_thread_id,
            sandbox_id=target_sandbox_id,
            generation=1,
            runtime_id=source.runtime_id,
            sandbox_image_digest=forked.snapshot_image_digest,
            stop_timeout_s=source.stop_timeout_s,
        )
        manager.start(target_spec)
        assert _capture(manager, target_spec, "git status --porcelain=v1") == before
        assert _capture(manager, target_spec, "git branch --show-current").strip() == (
            f"agent/{target_thread_id}"
        )
        assert _capture(manager, target_spec, "cat /agent-state/native/session.log") == (
            "opaque-session-state\n"
        )
        metadata = json.loads(
            _capture(manager, target_spec, "cat /agent-state/.freeinference/chat.json")
        )
        assert metadata["thread_id"] == target_thread_id
        assert metadata["native_session_id"] == "native-external-session"

        _capture(manager, target_spec, "printf 'clone-only\\n' > clone-only.txt")
        assert _capture(manager, source, "test ! -e clone-only.txt; printf ok") == "ok"
        _capture(manager, source, "printf 'source-only\\n' > source-only.txt")
        assert _capture(manager, target_spec, "test ! -e source-only.txt; printf ok") == "ok"
    finally:
        if target_spec is not None and target_container_id is not None:
            current = manager.inspect(target_spec.container_name)
            if current is not None:
                manager.remove(target_spec, container_id=target_container_id)
        if source_container_id is not None:
            current = manager.inspect(source.container_name)
            if current is not None:
                manager.remove(source, container_id=source_container_id)
        manager.remove_image_reference(manager.snapshot_repository(operation_id))


def test_real_docker_retained_terminal_stays_interactive():
    image = (os.getenv("AGENT_EXTERNAL_SANDBOX_IMAGE") or "").strip()
    network = (os.getenv("AGENT_EXTERNAL_SANDBOX_NETWORK") or "").strip()
    if not image or not network:
        pytest.skip(
            "set AGENT_EXTERNAL_SANDBOX_IMAGE and AGENT_EXTERNAL_SANDBOX_NETWORK "
            "to run the real retained-terminal acceptance"
        )

    nonce = secrets.token_hex(8)
    spec = ChatSandboxSpec(
        thread_id=f"athr_external_terminal_{nonce}",
        sandbox_id=f"asbx_external_terminal_{nonce}",
        generation=1,
        runtime_id="claude-code",
        sandbox_image_digest=image,
        stop_timeout_s=2,
    )
    manager = ChatSandboxManager(ContainerBackend(image=image, network=network))
    supervisor = ChatSessionSupervisor(manager, idle_timeout_s=1800)
    container_id: str | None = None
    terminal = None
    try:
        lease = supervisor.acquire(spec, owner="external:terminal", has_settled_context=False)
        container_id = lease.container_id
        manager.require_network_phase(spec, "agent")
        terminal = manager.spawn_terminal(spec, rows=31, cols=101)
        output_chunks: list[bytes] = []
        ready = threading.Event()

        def read_output() -> None:
            for chunk in terminal.chunks():
                output_chunks.append(chunk)
                ready.set()

        reader = threading.Thread(target=read_output, daemon=True)
        reader.start()
        assert ready.wait(timeout=10), "retained terminal never produced its initial prompt"
        # xterm sends carriage return for Enter; Docker's allocated terminal
        # performs the CR-to-NL canonical translation inside the container.
        terminal.write(b"printf 'terminal-stayed-live\\n'; pwd; exit\r")
        reader.join(timeout=20)
        if reader.is_alive():
            terminal.kill()
            pytest.fail("retained terminal did not consume input and exit within 20 seconds")
        output = b"".join(output_chunks)

        assert terminal.wait() == 0
        assert b"terminal-stayed-live" in output
        assert b"/workspace" in output

        terminal = manager.spawn_terminal(spec, rows=31, cols=101)
        controlled_chunks: list[bytes] = []
        controlled_ready = threading.Event()
        command_started = threading.Event()

        def read_controlled_output() -> None:
            for chunk in terminal.chunks():
                controlled_chunks.append(chunk)
                controlled_ready.set()
                if b"terminal-command-started" in b"".join(controlled_chunks):
                    command_started.set()

        controlled_reader = threading.Thread(target=read_controlled_output, daemon=True)
        controlled_reader.start()
        assert controlled_ready.wait(timeout=10), "controlled terminal never produced a prompt"
        terminal.suspend()
        terminal.resume()
        terminal.write(b"printf 'terminal-command-started\\n'; sleep 60\r")
        assert command_started.wait(timeout=10), "resumed terminal did not run its command"
        terminal.kill()
        controlled_reader.join(timeout=10)
        assert not controlled_reader.is_alive(), "killed terminal output stream stayed attached"
        current = manager.inspect(spec.container_name)
        assert current is not None and current.running

        # Model an ungraceful broker death: its Docker client disappears
        # without calling TerminalProcess.kill(). A new supervisor cannot
        # reattach that PTY, so startup removes only the exact pidfile-bound
        # wrapper and leaves the chat container running.
        terminal = manager.spawn_terminal(spec, rows=31, cols=101)
        crash_ready = threading.Event()

        def drain_crashed_client() -> None:
            for _chunk in terminal.chunks():
                crash_ready.set()

        crashed_reader = threading.Thread(target=drain_crashed_client, daemon=True)
        crashed_reader.start()
        assert crash_ready.wait(timeout=10), "crash-test terminal never produced a prompt"
        pidfile = terminal._pidfile
        pid_result = manager.exec_capture(spec, ["/bin/sh", "-c", 'cat "$1"', "sh", pidfile])
        assert pid_result.returncode == 0
        terminal_pid = pid_result.stdout.decode().strip()
        os.killpg(terminal._process.pid, signal.SIGKILL)
        terminal._process.wait(timeout=5)

        manager.reap_stale_terminal_processes(spec)
        crashed_reader.join(timeout=10)
        assert not crashed_reader.is_alive()
        cleaned = manager.exec_capture(
            spec,
            [
                "/bin/sh",
                "-c",
                'test ! -e "$1" && ! kill -0 -"$2" 2>/dev/null',
                "sh",
                pidfile,
                terminal_pid,
            ],
        )
        assert cleaned.returncode == 0
        current = manager.inspect(spec.container_name)
        assert current is not None and current.running
    finally:
        if terminal is not None:
            terminal.kill()
        if container_id is not None:
            current = manager.inspect(spec.container_name)
            if current is not None:
                manager.remove(spec, container_id=container_id)
