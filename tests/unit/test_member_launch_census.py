"""A turn's process census includes the meter the CLI's launcher forked.

The launcher (``model-protocol-translator.pyz``) forks the member's request
meter, waits until it serves, then replaces itself with the CLI. A census
taken before that leaves the meter out, so the turn's reap killed it as the
turn's own leftover: the meter became a zombie under the CLI, which never
waits for it, and the turn failed with "member processes survived cleanup".
Every new chat's first turn on staging failed this way.

The fix waits, at the resident's spawn, until the receipt's process is no
longer the launcher. These tests run that wait for real against a launcher
that forks late, and take the census with the reaper's own read-only scan.
"""

from __future__ import annotations

import contextlib
import importlib.util
import os
import signal
import subprocess
import sys
import time
from dataclasses import replace
from importlib.machinery import SourceFileLoader
from pathlib import Path
from types import SimpleNamespace

import pytest
from tests.support.member_loopback import HELPER_IN_PLACE
from tests.support.owners import launch_connection

from cloud_agent_host.container.chat_sandbox import ChatSandboxSpec
from cloud_agent_host.container.chat_sandbox_members import _AWAIT_LAUNCH_SCRIPT
from cloud_agent_host.resident_transport import ResidentLaunch, ResidentTransportFactory
from cloud_agent_host.runtimes.base import COMPAT_LAUNCHER
from cloud_agent_shared.errors import SandboxError

pytestmark = pytest.mark.skipif(not Path("/proc/self/stat").exists(), reason="needs /proc")

REAPER = Path(__file__).resolve().parents[2] / "deploy" / "sandbox" / "member-process-reaper"

#: The receipt the spawn wrapper writes, then the command: as ``spawn`` does.
_RECEIPT = (
    'fields=$(sed "s/^[^)]*) //" "/proc/$$/stat"); '
    'started=$(set -- $fields; printf "%s" "${20}"); '
    'printf "%s %s\\n" "$$" "$started" > "$0"; exec "$@"'
)

#: A launcher that takes a while to fork its meter, then becomes the CLI.
_LATE_LAUNCHER = """
import os, sys, time
time.sleep(0.4)
if os.fork() == 0:
    time.sleep(60)
    os._exit(0)
time.sleep(0.1)
if sys.argv[1] == "exit":
    os._exit(3)
os.execvp(sys.argv[1], sys.argv[1:])
"""


def _census() -> set[tuple[int, int]]:
    loader = SourceFileLoader("member_process_reaper", str(REAPER))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    assert spec is not None
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module._scan(os.getuid())


def _children(pid: int) -> set[int]:
    found = set()
    for name in os.listdir("/proc"):
        if not name.isdigit():
            continue
        try:
            raw = Path(f"/proc/{name}/stat").read_text()
        except OSError:
            continue
        if int(raw.rsplit(") ", 1)[1].split()[1]) == pid:
            found.add(int(name))
    return found


@pytest.fixture
def launch(tmp_path):
    """Start a late-forking launcher behind a receipt; stop its whole group after."""
    launcher = tmp_path / "model-protocol-translator.pyz"
    launcher.write_text(_LATE_LAUNCHER)
    pidfile = tmp_path / "resident.pid"
    started: list[subprocess.Popen[bytes]] = []

    def start(*cli: str) -> tuple[subprocess.Popen[bytes], Path, Path]:
        process = subprocess.Popen(
            ["/bin/sh", "-c", _RECEIPT, str(pidfile), sys.executable, str(launcher), *cli],
            start_new_session=True,
        )
        started.append(process)
        while not pidfile.exists() or len(pidfile.read_text().split()) != 2:
            time.sleep(0.01)
        return process, pidfile, launcher

    yield start
    for process in started:
        with contextlib.suppress(ProcessLookupError):
            os.killpg(process.pid, signal.SIGKILL)
        process.wait()


def _await(pidfile: Path, launcher: Path) -> int:
    return subprocess.run(
        ["/bin/sh", "-c", _AWAIT_LAUNCH_SCRIPT, "sh", str(pidfile), str(launcher)],
        timeout=30,
        check=False,
    ).returncode


def test_a_census_taken_before_the_launcher_became_the_cli_misses_its_meter(launch):
    process, pidfile, launcher = launch("sleep", "30")
    census = {pid for pid, _ in _census()}
    assert process.pid in census
    assert _await(pidfile, launcher) == 0
    # The CLI was in the census and its meter was not: the turn's reap kills it.
    meter = _children(process.pid)
    assert meter and not meter & census


def test_waiting_for_the_launch_puts_the_meter_in_the_census(launch):
    process, pidfile, launcher = launch("sleep", "30")
    assert _await(pidfile, launcher) == 0

    meter = _children(process.pid)
    assert len(meter) == 1
    assert meter <= {pid for pid, _ in _census()}
    assert Path(f"/proc/{process.pid}/cmdline").read_bytes().startswith(b"sleep\0")


def test_a_launch_that_ends_instead_of_becoming_the_cli_fails_the_wait(launch):
    _, pidfile, launcher = launch("exit")
    assert _await(pidfile, launcher) == 126


# -- the call site --------------------------------------------------------------------


def _context(events: list, *, await_error: Exception | None = None, base_url: str | None = None):
    process = SimpleNamespace(kill=lambda: events.append(("kill",)))

    def wait_for_launch(spec, **kwargs):
        events.append(("await", kwargs["pidfile"], kwargs["launcher"]))
        if await_error is not None:
            raise await_error

    manager = SimpleNamespace(
        backend=SimpleNamespace(gid=10001, base_env=lambda: {}),
        reap_member_processes=lambda spec, **kwargs: None,
        exec_capture=lambda spec, argv, **kwargs: subprocess.CompletedProcess(argv, 0, b"", b""),
        ensure_mcp_helpers=lambda spec, runtime: None,
        spawn=lambda spec, argv, **kwargs: (
            events.append(("spawn", kwargs["process_pidfile"])) or process
        ),
        await_member_launch=wait_for_launch,
        exact=HELPER_IN_PLACE,
    )
    spec = ChatSandboxSpec(
        thread_id="athr_meter",
        sandbox_id="asbx_meter",
        generation=1,
        runtime_id="claude-code",
        shared_live=True,
    )
    launch = ResidentLaunch(
        thread_id="athr_meter",
        runtime_id="claude-code",
        runtime_version=None,
        native_session_id=None,
        model="model",
        reasoning_effort=None,
        credential="grant",
        **launch_connection("claude-code"),
    )
    if base_url is not None:
        launch = replace(launch, inference_base_url=base_url)
    context = ResidentTransportFactory(manager)._launch_context(spec, launch, credential="grant")
    return context, process


def test_a_cli_behind_the_launcher_is_handed_over_only_once_it_is_the_cli():
    events: list = []
    context, process = _context(events)

    # A translated plan's argv arrives behind the launcher already.
    assert context.spawn_live([*COMPAT_LAUNCHER, "claude", "-p"], {}) is process
    (spawned,) = [event for event in events if event[0] == "spawn"]
    assert events[-1] == ("await", spawned[1], COMPAT_LAUNCHER[-1])

    # A direct plan is put behind its meter's launcher here (#1118), and waits too.
    events.clear()
    assert context.spawn_live(["claude", "-p"], {}) is process
    assert [event[0] for event in events] == ["spawn", "await"]

    # A CLI on its vendor's own host is neither metered nor waited for.
    events.clear()
    direct, _ = _context(events, base_url="https://api.anthropic.com")
    direct.spawn_live(["claude", "-p"], {})
    assert [event[0] for event in events] == ["spawn"]


def test_a_launch_that_never_became_the_cli_is_stopped_and_fails():
    events: list = []
    context, _ = _context(events, await_error=SandboxError("member CLI launch did not finish"))

    with pytest.raises(SandboxError, match="did not finish"):
        context.spawn_live([*COMPAT_LAUNCHER, "claude", "-p"], {})
    assert [event[0] for event in events][-2:] == ["await", "kill"]
