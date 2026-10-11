"""The retained terminal's shell wrapper, run for real under a PTY.

``docker exec --tty`` starts its command as a session leader whose controlling
terminal is the exec's PTY; ``pty.fork`` reproduces exactly that, so the
wrapper ``ChatSandboxManager.spawn_terminal`` builds can be exercised here
without Docker. These are the behaviours a person at the terminal notices:
line editing, Ctrl-C and Ctrl-Z, the exit status, and -- for the broker's
suspend and kill -- every command staying in the process group the pidfile
names.
"""

from __future__ import annotations

import contextlib
import os
import pty
import re
import select
import shutil
import signal
import sys
import time
from types import SimpleNamespace

import pytest

from cloud_agent_host.chat_sandbox import (
    ChatSandboxManager,
    ChatSandboxSpec,
    _terminal_bashrc,
)
from cloud_agent_host.sandbox import ContainerBackend

pytestmark = pytest.mark.skipif(
    sys.platform != "linux" or shutil.which("bash") is None or not os.path.exists("/bin/sh"),
    reason="needs Linux /proc, /bin/sh and bash",
)


def _wrapper_script(monkeypatch) -> str:
    """The exact script ``spawn_terminal`` hands ``docker exec``."""
    spec = ChatSandboxSpec(
        thread_id="athr_01", sandbox_id="asbx_01", generation=3, runtime_id="claude-code"
    )
    manager = ChatSandboxManager(ContainerBackend(image="img"))
    monkeypatch.setattr(
        manager,
        "_require",
        lambda _spec: SimpleNamespace(running=True),
    )
    captured: list[list[str]] = []

    class Process:
        stdout = None

        def poll(self):
            return 0

        def wait(self, timeout=None):
            return 0

    def fake_popen(argv, **_kwargs):
        captured.append(list(argv))
        return Process()

    monkeypatch.setattr("cloud_agent_host.chat_sandbox.subprocess.Popen", fake_popen)
    manager.spawn_terminal(spec, rows=24, cols=80).wait()
    command = captured[0]
    return command[command.index("-c") + 1]


class _Terminal:
    """One wrapper process on a PTY, as Docker's TTY exec would start it."""

    def __init__(self, script: str, tmp_path, *, path: str | None = None) -> None:
        self.pidfile = tmp_path / "terminal-test.pid"
        self.rcfile = tmp_path / "terminal-test.bashrc"
        bashrc = _terminal_bashrc(str(self.pidfile), str(self.rcfile))
        env = {
            "HOME": str(tmp_path),
            "PATH": path or os.environ.get("PATH", "/usr/bin:/bin"),
            "TERM": "xterm-256color",
            "LANG": "C.UTF-8",
        }
        argv = ["/bin/sh", "-c", script, "sh", str(self.pidfile), str(self.rcfile), bashrc]
        self.pid, self.master = pty.fork()
        if self.pid == 0:  # pragma: no cover - replaced by exec
            os.execve("/bin/sh", argv, env)
        self.output = ""
        self.status: int | None = None

    def send(self, data: str) -> None:
        os.write(self.master, data.encode())

    def expect(self, pattern: str, timeout: float = 10.0) -> re.Match[str]:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            match = re.search(pattern, self.output)
            if match:
                return match
            ready, _, _ = select.select([self.master], [], [], 0.05)
            if ready:
                try:
                    chunk = os.read(self.master, 65536)
                except OSError:
                    break
                if not chunk:
                    break
                self.output += chunk.decode("utf-8", "replace")
        match = re.search(pattern, self.output)
        assert match, f"{pattern!r} never appeared in {self.output[-500:]!r}"
        return match

    def ready(self) -> None:
        # The sum, not the echoed command line, shows the shell has run it.
        self.send("echo ready-$((40+2))\r")
        self.expect(r"ready-42")

    def wait(self, timeout: float = 10.0) -> int:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            finished, status = os.waitpid(self.pid, os.WNOHANG)
            if finished:
                self.status = os.waitstatus_to_exitcode(status)
                return self.status
            # Keep the PTY drained so the shell can finish writing.
            if select.select([self.master], [], [], 0.05)[0]:
                with contextlib.suppress(OSError):
                    self.output += os.read(self.master, 65536).decode("utf-8", "replace")
        raise AssertionError(f"the terminal did not exit: {self.output[-500:]!r}")

    def close(self) -> None:
        if self.status is None:
            with contextlib.suppress(ProcessLookupError):
                os.killpg(self.pid, signal.SIGKILL)
            with contextlib.suppress(ChildProcessError):
                os.waitpid(self.pid, 0)
        os.close(self.master)


@pytest.fixture
def terminal(monkeypatch, tmp_path):
    script = _wrapper_script(monkeypatch)
    opened: list[_Terminal] = []

    def open_terminal(**kwargs) -> _Terminal:
        current = _Terminal(script, tmp_path, **kwargs)
        opened.append(current)
        return current

    yield open_terminal
    for current in opened:
        current.close()


def test_bash_runs_every_command_in_the_group_the_pidfile_names(terminal):
    shell = terminal()
    shell.ready()
    leader = int(shell.pidfile.read_text())

    shell.send(
        "echo kind=${BASH_VERSION:+bash} shell-group=$(cut -d' ' -f5 /proc/$$/stat); "
        "sh -c 'echo child-group=$(cut -d\" \" -f5 /proc/$$/stat)'\r"
    )
    kind = shell.expect(r"kind=(\w+) shell-group=(\d+)")
    child = shell.expect(r"child-group=(\d+)")

    assert kind.group(1) == "bash"
    assert int(kind.group(2)) == leader == shell.pid
    # bash turns job control on at startup; a command in a group of its own
    # would escape the broker's suspend and kill.
    assert int(child.group(1)) == leader


def test_bash_has_history_and_completion(terminal):
    shell = terminal()
    shell.ready()

    shell.send("echo history-$((20+1))\r")
    shell.expect(r"history-21\r?\n")
    shell.send("\x1b[A\r")
    shell.expect(r"history-21\r?\n[\s\S]*history-21\r?\n")

    shell.send("ech\t completed-$((3*3))\r")
    shell.expect(r"completed-9")


def test_ctrl_z_cannot_freeze_the_terminal(terminal):
    shell = terminal()
    shell.ready()

    shell.send("sleep 1; echo slept-$((6*7))\r")
    time.sleep(0.3)
    shell.send("\x1a")

    # Nothing in this terminal can resume a stopped job, so a stop would
    # have hung it for good.
    shell.expect(r"slept-42", timeout=10)


def test_exit_reports_the_shells_status_even_after_ctrl_c(terminal):
    shell = terminal()
    shell.ready()

    shell.send("\x03")
    shell.ready()
    shell.send("exit 5\r")

    assert shell.wait() == 5
    assert not shell.pidfile.exists()
    assert not shell.rcfile.exists()


def test_without_bash_the_sh_wrapper_survives_ctrl_c(terminal, tmp_path):
    tools = tmp_path / "bin"
    tools.mkdir()
    for tool in ("rm", "cut"):
        (tools / tool).symlink_to(shutil.which(tool))
    shell = terminal(path=str(tools))
    shell.ready()

    shell.send("\x03")
    shell.ready()
    shell.send("exit 4\r")

    # The wrapper shares the shell's process group, so the Ctrl-C reached it
    # too; it must still report the shell's own status and remove the pidfile.
    assert shell.wait() == 4
    assert not shell.pidfile.exists()
    assert not shell.rcfile.exists()
