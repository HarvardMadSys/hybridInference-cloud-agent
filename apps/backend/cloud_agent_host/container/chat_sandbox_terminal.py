"""The interactive PTY a retained chat terminal runs in.

:class:`_RetainedTerminalProcess` is the process
:meth:`~cloud_agent_host.container.chat_sandbox.ChatSandboxManager.spawn_terminal`
returns: one Docker-exec PTY whose suspend, resume and kill signal only its own
process group, never the chat container.
"""

from __future__ import annotations

import contextlib
import errno
import fcntl
import os
import shlex
import signal
import struct
import subprocess
import termios
import threading
import time
from typing import TYPE_CHECKING

from cloud_agent_host.container.retained_backend import RetainedChatBackend
from cloud_agent_host.container.sandbox import SandboxError, TerminalProcess

if TYPE_CHECKING:
    from collections.abc import Iterator

    from cloud_agent_host.container.chat_sandbox import ChatSandboxManager
    from cloud_agent_host.container.chat_sandbox_models import ChatSandboxSpec


def _set_pty_size(fd: int, rows: int, cols: int) -> None:
    fcntl.ioctl(fd, termios.TIOCSWINSZ, struct.pack("HHHH", rows, cols, 0, 0))


def _terminal_rcfile(pidfile: str) -> str:
    """Name the bash startup file that belongs to one terminal's pidfile."""
    return pidfile.removesuffix(".pid") + ".bashrc"


def _terminal_bashrc(pidfile: str, rcfile: str) -> str:
    """Return the startup file a terminal's interactive bash reads.

    It deletes itself first: bash has read it whole by then, and its path
    stays in bash's argv, which is what marks the process as this terminal's.
    """
    lines = (
        f"rm -f -- {shlex.quote(rcfile)}",
        "[ -r ~/.bashrc ] && . ~/.bashrc",
        # bash re-enables job control after reading its startup files, which
        # would put each command in a process group of its own. Switching it
        # off before every prompt keeps commands in the group the sandbox manager
        # suspends and signals.
        'PROMPT_COMMAND="set +m${PROMPT_COMMAND:+;$PROMPT_COMMAND}"',
        f"trap {shlex.quote('rm -f -- ' + shlex.quote(pidfile))} EXIT",
    )
    return "\n".join(lines) + "\n"


class _RetainedTerminalProcess(TerminalProcess):
    """One Docker-exec PTY whose lifecycle never removes the chat container."""

    # The wrapper falls back to ``sh`` only for an image without bash; the
    # sandbox image this platform ships always has it.
    shell = "bash"

    def __init__(
        self,
        process: subprocess.Popen[bytes],
        *,
        manager: ChatSandboxManager,
        spec: ChatSandboxSpec,
        pidfile: str,
        input_fd: int,
        operation_uid: int | None = None,
    ) -> None:
        self._process = process
        self._manager = manager
        self._spec = spec
        self._pidfile = pidfile
        self._operation_uid = operation_uid
        self._input_fd: int | None = input_fd
        self._state_lock = threading.Lock()
        self._write_lock = threading.Lock()
        self._killed = False
        self._suspended = False

    def chunks(self) -> Iterator[bytes]:
        # The output side reads its own descriptor for the PTY master, so
        # closing the input side (kill, wait) can neither cut off output still
        # buffered nor hand this read a reused descriptor number.
        with self._write_lock:
            if self._input_fd is None:
                return
            output_fd = os.dup(self._input_fd)
        try:
            while True:
                try:
                    chunk = os.read(output_fd, 64 * 1024)
                except OSError as exc:
                    # EIO: the Docker client exited and closed the last
                    # descriptor for the PTY's other end.
                    if exc.errno == errno.EIO:
                        return
                    raise
                if not chunk:
                    return
                yield chunk
        finally:
            os.close(output_fd)

    def write(self, data: bytes) -> None:
        with self._write_lock:
            if self._process.poll() is not None or self._input_fd is None:
                raise SandboxError("terminal is closed")
            try:
                remaining = memoryview(data)
                while remaining:
                    written = os.write(self._input_fd, remaining)
                    if written == 0:
                        raise OSError("terminal input closed")
                    remaining = remaining[written:]
            except OSError as exc:
                raise SandboxError("terminal is closed") from exc

    def resize(self, rows: int, cols: int) -> None:
        with self._write_lock:
            if self._input_fd is None or self._process.poll() is not None:
                raise SandboxError("terminal is closed")
            try:
                _set_pty_size(self._input_fd, rows, cols)
                # Docker CLI watches its controlling TTY on SIGWINCH and
                # forwards the new dimensions to this exec instance.
                os.kill(self._process.pid, signal.SIGWINCH)
            except OSError as exc:
                raise SandboxError("terminal cannot be resized") from exc

    def _signal_tree(self, signal_name: str) -> None:
        script = (
            'attempt=0; while [ ! -r "$1" ]; do '
            'attempt=$((attempt + 1)); test "$attempt" -lt 100 || exit 3; '
            'sleep 0.05; done; pid=$(cat "$1") || exit 4; '
            'case "$pid" in ""|*[!0-9]*) exit 2;; esac; '
            # The minimal sandbox image intentionally has no /bin/kill. The
            # POSIX shell builtin supports negative process-group operands.
            f'kill -{signal_name} -"$pid" 2>/dev/null'
        )
        result = self._manager.exec_capture(
            self._spec,
            ["/bin/sh", "-c", script, "sh", self._pidfile],
            timeout_s=15,
            user_uid=self._operation_uid,
            user_gid=self._manager.backend.gid if self._operation_uid is not None else None,
        )
        # Natural exit can win the small poll-to-signal race. That is already
        # the requested end state; a live client without a confirmed signal
        # must fail closed so protected workspace phases cannot overlap it.
        if result.returncode != 0 and self._process.poll() is None:
            raise SandboxError(f"terminal cannot receive {signal_name}")

    def _set_suspended(self, suspended: bool) -> None:
        with self._state_lock, self._write_lock:
            if self._killed or self._process.poll() is not None:
                return
            if self._suspended is suspended:
                return
            self._signal_tree("STOP" if suspended else "CONT")
            self._suspended = suspended

    def suspend(self) -> None:
        self._set_suspended(True)

    def resume(self) -> None:
        self._set_suspended(False)

    def _close_input(self) -> None:
        with self._write_lock:
            input_fd = self._input_fd
            self._input_fd = None
            if input_fd is not None:
                with contextlib.suppress(OSError):
                    os.close(input_fd)

    def kill(self) -> None:
        with self._state_lock:
            if self._killed:
                return
            if self._operation_uid is not None:
                from cloud_agent_host.environment_process import kill_identity

                kill_identity(RetainedChatBackend(self._manager, self._spec), self._operation_uid)
                self._close_input()
                if self._process.poll() is None:
                    self._process.kill()
                self._process.wait(timeout=5)
                self._killed = True
                return
            if self._process.poll() is None:
                # A hangup, as when a terminal window closes. An interactive
                # shell ignores SIGTERM, so a TERM would always wait out the
                # deadline below before the KILL.
                self._signal_tree("HUP")
                deadline = time.monotonic() + 3
                while self._process.poll() is None and time.monotonic() < deadline:
                    time.sleep(0.05)
            if self._process.poll() is None:
                self._signal_tree("KILL")
            if self._process.poll() is None:
                with contextlib.suppress(ProcessLookupError, OSError):
                    os.killpg(self._process.pid, signal.SIGKILL)
            try:
                self._process.wait(timeout=5)
            except subprocess.TimeoutExpired as exc:
                raise SandboxError("terminal Docker exec could not be stopped") from exc
            self._close_input()
            self._killed = True

    def wait(self) -> int:
        try:
            return self._process.wait()
        finally:
            self._close_input()
