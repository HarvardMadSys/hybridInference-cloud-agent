"""The long-lived host-rule helper, and the guard's steps on it.

The helper's program runs here under a real interpreter with a stand-in
``nft`` on ``PATH``, so its stdin protocol, its deadlines and its exit at EOF
are the real ones; only Docker is missing. The guard's use of it runs against
the model of nft in ``test_network_guard``: the long-lived helper's client is a
stand-in speaking the same protocol over real pipes, and it runs each step as
a disposable helper would, so both kinds of helper change the same model.
"""

from __future__ import annotations

import contextlib
import gc
import json
import logging
import os
import resource
import select
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pytest
from tests.unit.test_network_guard import (
    _ENV,
    _IMAGE,
    _KINDS,
    _NET,
    _X,
    _Y,
    FakeNft,
    _hold,
    _in_thread,
    _network,
    _update,
)

from cloud_agent_host import network_guard, network_guard_helper
from cloud_agent_host.network_guard import (
    _NFT_BATCH,
    _NFT_SERVE,
    HostNetworkGuard,
    NetworkGuardError,
)
from cloud_agent_host.network_guard_helper import LongLivedHelper, serve_program
from cloud_agent_shared.egress import render_ruleset

if TYPE_CHECKING:
    from collections.abc import Callable, Collection, Iterator

# ── the program, under a real interpreter ───────────────────────────────

#: A stand-in for ``nft``: it lists what ``<record>.kernel`` holds now,
#: appends what it is told to apply to ``<record>.applied``, refuses a
#: transaction that says so, hangs on a listing of ``hang`` and lists what its
#: own stdin holds for a listing of ``stdin``.
_STAND_IN_NFT = """\
import json
import os
import sys
import time

args = sys.argv[1:]
record = os.environ["STAND_IN_NFT"]
if args[:2] == ["-f", "-"]:
    text = sys.stdin.read()
    if "refuse" in text:
        sys.stderr.write("Error: Could not process rule")
        sys.exit(1)
    with open(record + ".applied", "a") as applied:
        applied.write(text)
elif args[-1] == "hang":
    with open(record + ".pid", "w") as pid:
        pid.write(str(os.getpid()))
    time.sleep(60)
elif args[-1] == "stdin":
    print(json.dumps({"nftables": [{"stdin": sys.stdin.read()}]}))
else:
    with open(record + ".kernel") as kernel:
        print(json.dumps({"nftables": [{"args": args, "kernel": kernel.read()}]}))
"""
#: Stands in for the helper's ``docker run`` command line, which the stand-in
#: client below runs as the program alone.
_ARGV = ["docker", "run", "the-helper"]
_READ = json.dumps({"reads": [["list", "tables"]]})


class _Interrupted(BaseException):
    """An interrupt, as a SIGINT's KeyboardInterrupt is to the guard.

    Not KeyboardInterrupt itself: one a failing test let through would stop
    the whole pytest session.
    """


def _listings(result: subprocess.CompletedProcess[str]) -> list[Any]:
    return json.loads(result.stdout)["listings"]


def _soon(seconds: float = 30) -> float:
    return time.monotonic() + seconds


class _ProgramHelper:
    """A long-lived helper whose client is the real program, run here instead of in Docker."""

    def __init__(self, tmp_path: Path, *, idle_s: int = 60, request_s: int = 60) -> None:
        bin_dir = tmp_path / "bin"
        bin_dir.mkdir()
        (tmp_path / "nft.py").write_text(_STAND_IN_NFT)
        nft = bin_dir / "nft"
        nft.write_text(f'#!/bin/sh\nexec "{sys.executable}" "{tmp_path / "nft.py"}" "$@"\n')
        nft.chmod(0o755)
        self.record = tmp_path / "nft"
        Path(f"{self.record}.kernel").write_text("v1")
        self._env = {
            **os.environ,
            "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}",
            "STAND_IN_NFT": str(self.record),
        }
        self._program = serve_program(_NFT_BATCH, idle_s=idle_s, request_s=request_s)
        self.started: list[subprocess.Popen[bytes]] = []
        self.removed: list[str] = []
        #: For each removal, whether the helper's client was still running.
        self.alive_at_removal: list[bool] = []
        self._named: dict[str, subprocess.Popen[bytes]] = {}
        self.helper = LongLivedHelper(self._spawn, self._remove, name_prefix="test")

    def run(self, request: str = _READ, *, within: float = 30, accept: Any = _listings) -> Any:
        return self.helper.run(_ARGV, request, deadline=_soon(within), accept=accept)

    def _spawn(self, argv: list[str], errors: Any) -> subprocess.Popen[bytes]:
        assert argv[:3] == ["docker", "run", "--name"] and argv[4:] == _ARGV[2:], argv
        process = subprocess.Popen(
            [sys.executable, "-c", self._program],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=errors,
            bufsize=0,
            env=self._env,
        )
        self.started.append(process)
        self._named[argv[3]] = process
        return process

    def _remove(self, name: str, timeout: float) -> bool:
        # What ``docker rm -f`` does to the container: everything in it ends.
        process = self._named[name]
        self.removed.append(name)
        self.alive_at_removal.append(process.poll() is None)
        with contextlib.suppress(OSError):
            process.kill()
        process.wait(timeout)
        return True


def test_one_process_answers_every_request_from_its_own_read_of_the_kernel(tmp_path) -> None:
    lab = _ProgramHelper(tmp_path)
    install = json.dumps({"install": "add table inet t\n", "reads": []})
    try:
        first = lab.run()
        Path(f"{lab.record}.kernel").write_text("v2")
        second = lab.run()
        installed = lab.run(install)
    finally:
        lab.helper.close()

    listed = ["-j", "list", "tables"]
    assert first == [{"nftables": [{"args": listed, "kernel": "v1"}]}]
    assert second == [{"nftables": [{"args": listed, "kernel": "v2"}]}], "answered from memory"
    assert installed == []
    assert Path(f"{lab.record}.applied").read_text() == "add table inet t\n"
    assert len(lab.started) == 1, "each request started a helper of its own"
    assert lab.started[0].returncode == 0, "it ends when its stdin closes"
    assert lab.removed == []


def test_nothing_a_step_starts_can_read_the_requests_meant_for_the_helper(tmp_path) -> None:
    """Its nft calls find their stdin at its end, as in a disposable helper that read its request."""
    lab = _ProgramHelper(tmp_path)
    try:
        listed = lab.run(json.dumps({"reads": [["list", "stdin"]]}), within=10)
    finally:
        lab.helper.close()
    assert listed == [{"nftables": [{"stdin": ""}]}]


def test_a_failed_step_is_handed_back_after_its_helper_is_removed(tmp_path) -> None:
    """The disposable helper runs it again and reports the failure; until then none is trusted."""
    lab = _ProgramHelper(tmp_path)
    refused = json.dumps({"install": "refuse\n", "reads": []})

    assert lab.run(refused) is None

    assert len(lab.removed) == 1
    assert lab.alive_at_removal == [False], "removed before its client was killed and reaped"
    assert lab.started[0].returncode is not None, "the helper was left running"
    assert lab.run() is None, "not cooling down"
    assert len(lab.started) == 1


def test_an_idle_helper_ends_by_itself_and_the_next_step_starts_another(tmp_path) -> None:
    lab = _ProgramHelper(tmp_path, idle_s=1)
    try:
        assert lab.run()
        assert lab.started[0].wait(10) == network_guard_helper.IDLE_EXIT, "it did not end its wait"
        assert lab.run(), "an idle exit is no failure"
    finally:
        lab.helper.close()
    assert len(lab.started) == 2
    assert lab.removed == []


@pytest.mark.skipif(sys.platform != "linux", reason="reads the client's state from /proc")
def test_the_next_step_reaps_a_helper_that_idled_out_and_closes_its_pipes(tmp_path) -> None:
    lab = _ProgramHelper(tmp_path, idle_s=1)
    try:
        assert lab.run()
        client = lab.started[0]
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline and _state(client.pid) != "Z":
            time.sleep(0.05)
        assert _state(client.pid) == "Z", "it never idled out"
        assert lab.run()
        assert client.returncode == network_guard_helper.IDLE_EXIT, "left a zombie"
        assert client.stdin is not None and client.stdin.closed
        assert client.stdout is not None and client.stdout.closed
    finally:
        lab.helper.close()


def _state(pid: int) -> str:
    with contextlib.suppress(OSError), open(f"/proc/{pid}/stat") as stat:
        return stat.read().rsplit(")", 1)[1].split()[0]
    return "gone"


def test_a_request_past_its_deadline_ends_the_helper_before_the_guard_gives_up(tmp_path) -> None:
    """Its own alarm ends it; in its container the nft call it waited on dies with it."""
    lab = _ProgramHelper(tmp_path, request_s=1)
    hang = json.dumps({"reads": [["list", "hang"]]})
    began = time.monotonic()
    try:
        served = lab.run(hang)
        took = time.monotonic() - began
    finally:
        # Outside a container nothing ends the orphaned stand-in for it.
        with contextlib.suppress(OSError, ValueError):
            os.kill(int(Path(f"{lab.record}.pid").read_text()), signal.SIGKILL)
    assert served is None
    assert took < 10, "the guard waited out its own timeout"
    assert lab.started[0].returncode == network_guard_helper.OVERDUE_EXIT
    assert len(lab.removed) == 1, "a helper that failed a step is removed all the same"


def test_an_interrupted_step_removes_its_helper(tmp_path) -> None:
    """The helper may still be running it, so nothing after may share that helper."""
    lab = _ProgramHelper(tmp_path)

    def interrupted(_result: subprocess.CompletedProcess[str]) -> None:
        raise _Interrupted

    with pytest.raises(_Interrupted):
        lab.run(accept=interrupted)
    assert len(lab.removed) == 1
    assert lab.started[0].returncode is not None


def test_a_helper_still_works_on_descriptors_past_1024(tmp_path) -> None:
    """A pool of open files puts its pipes past what ``select.select`` can watch.

    Under a soft limit of 1024 open files, the usual default, the pool raises
    that limit for itself as far as the hard limit lets it.
    """
    soft, hard = resource.getrlimit(resource.RLIMIT_NOFILE)
    room = 4096 if hard == resource.RLIM_INFINITY else min(hard, 4096)
    if room < 1500:
        pytest.skip(f"a hard limit of {hard} open files leaves no room past 1024")
    if soft != resource.RLIM_INFINITY and soft < room:
        resource.setrlimit(resource.RLIMIT_NOFILE, (room, hard))
    fillers: list[int] = []
    try:
        while len(fillers) < 1100:
            fillers.append(os.open(os.devnull, os.O_RDONLY))
        lab = _ProgramHelper(tmp_path)
        try:
            answers = [lab.run() for _ in range(3)]
            assert lab.started[0].stdout is not None and lab.started[0].stdout.fileno() >= 1024
        finally:
            lab.helper.close()
    finally:
        for fd in fillers:
            os.close(fd)
        resource.setrlimit(resource.RLIMIT_NOFILE, (soft, hard))
    assert all(answer is not None for answer in answers), answers
    assert len(lab.started) == 1 and lab.removed == []


def test_a_helper_left_behind_is_stopped_when_its_object_is_collected(tmp_path) -> None:
    lab = _ProgramHelper(tmp_path)
    assert lab.run()
    client = lab.started[0]

    del lab.helper
    gc.collect()

    assert client.returncode == 0, "it ended at EOF, reaped"


#: The guard's process, here: a helper's step, then a normal exit. Its client
#: must be reaped before the interpreter exits, not left to the parent.
_GUARD_PROCESS = """\
import json, subprocess, sys, time
from cloud_agent_host.network_guard import _NFT_BATCH
from cloud_agent_host.network_guard_helper import LongLivedHelper, serve_program

program = serve_program(_NFT_BATCH, idle_s=600)

def spawn(argv, errors):
    return subprocess.Popen([sys.executable, "-c", program], stdin=subprocess.PIPE,
                            stdout=subprocess.PIPE, stderr=errors, bufsize=0)

helper = LongLivedHelper(spawn, lambda name, timeout: True, name_prefix="exit")
answer = helper.run(["docker", "run", "x"], json.dumps({"reads": []}),
                    deadline=time.monotonic() + 20, accept=lambda r: json.loads(r.stdout))
print(json.dumps(answer), flush=True)
"""
#: Stands in for a container's first process that reaps nothing: a child
#: subreaper, so a client the guard's process leaves behind becomes its child.
_SUBREAPER = """\
import ctypes, json, os, subprocess, sys, time
assert ctypes.CDLL(None, use_errno=True).prctl(36, 1, 0, 0, 0) == 0
guard = subprocess.run([sys.executable, "-c", sys.argv[1]], capture_output=True, text=True)
time.sleep(1)
orphans = []
for entry in filter(str.isdigit, os.listdir("/proc")):
    try:
        with open("/proc/%s/stat" % entry) as stat:
            if int(stat.read().rsplit(")", 1)[1].split()[1]) == os.getpid():
                orphans.append(entry)
    except OSError:
        pass
print(json.dumps({"exit": guard.returncode, "answer": guard.stdout.strip(), "orphans": orphans}))
"""


@pytest.mark.skipif(sys.platform != "linux", reason="a child subreaper and /proc are Linux's")
def test_a_guard_process_that_exits_leaves_no_client_for_its_parent_to_reap() -> None:
    harness = subprocess.run(
        [sys.executable, "-c", _SUBREAPER, _GUARD_PROCESS],
        capture_output=True,
        text=True,
        timeout=60,
    )
    found = json.loads(harness.stdout)
    assert found["exit"] == 0 and json.loads(found["answer"]) == {"listings": []}, found
    assert found["orphans"] == [], "a client outlived its guard's process"


#: The guard's process, here: it starts the helper with its stdin a pipe,
#: shares its own stdout with it, says so and waits to be killed.
_STARTER = """\
import subprocess
import sys
import time

helper = subprocess.Popen(
    [sys.executable, "-c", sys.argv[1]], stdin=subprocess.PIPE, stderr=subprocess.DEVNULL
)
print(helper.pid, flush=True)
time.sleep(600)
"""


def _ends_within(stream: Any, seconds: float) -> bool:
    """Whether ``stream`` reaches its end within ``seconds``, whatever comes before it."""
    deadline = time.monotonic() + seconds
    while (left := deadline - time.monotonic()) > 0:
        if select.select([stream], [], [], left)[0] and not os.read(stream.fileno(), 1 << 16):
            return True
    return False


def test_the_helper_ends_when_the_process_that_started_it_is_killed() -> None:
    """Even by SIGKILL: the kernel closes that process's end of the pipe, and the helper reads EOF.

    Docker carries the EOF on to a helper container: ``docker run -i`` sends
    it on, and the container's stdin is closed when the client's stream ends.
    """
    program = serve_program(_NFT_BATCH, idle_s=600, request_s=600)
    starter = subprocess.Popen([sys.executable, "-c", _STARTER, program], stdout=subprocess.PIPE)
    assert starter.stdout is not None
    helper = int(starter.stdout.readline())
    ended = False
    try:
        starter.kill()
        starter.wait(5)
        # The helper holds this pipe too, so it ends only once the helper has.
        ended = _ends_within(starter.stdout, 10)
    finally:
        if not ended:
            with contextlib.suppress(OSError):
                os.kill(helper, signal.SIGKILL)
        starter.stdout.close()
    assert ended, "the helper outlived the process that started it"


# ── removing a helper by name ───────────────────────────────────────────

#: The real one: ``conftest`` puts a stand-in in its place for every test.
_REMOVE_HELPER = network_guard_helper.remove_helper

#: Stands in for ``docker``: each call records its arguments and does what the
#: next word of its plan says, the last one again once the plan runs out.
_FAKE_DOCKER = """\
import pathlib, sys, time
here = pathlib.Path(sys.argv[0]).parent
plan = (here / "plan").read_text().split()
with open(here / "calls", "a+") as calls:
    calls.seek(0)
    done = len(calls.readlines())
    calls.write(" ".join(sys.argv[1:]) + "\\n")
step, name = plan[min(done, len(plan) - 1)], sys.argv[-1]
missing = "Error response from daemon: No such container: " + name + "\\n"
if step == "removed":
    print(name)
elif step == "gone":
    sys.stderr.write(missing)
elif step == "missing":
    sys.stderr.write(missing)
    sys.exit(1)
elif step == "busy":
    sys.stderr.write("Error response from daemon: removal of container " + name)
    sys.stderr.write(" is already in progress\\n")
    sys.exit(1)
elif step == "garbled":
    sys.stderr.buffer.write(b"\\xff\\xfe\\n")
    sys.exit(1)
elif step == "down":
    sys.stderr.write("Cannot connect to the Docker daemon at unix:///var/run/docker.sock\\n")
    sys.exit(1)
elif step == "hang":
    time.sleep(60)
"""
_NAME = "t-helper-0123456789ab"


@pytest.fixture
def docker(tmp_path: Path) -> Callable[..., tuple[str, Path]]:
    """A stand-in ``docker`` following the plan it is given: it, and where it records its calls."""

    def plan(*steps: str) -> tuple[str, Path]:
        (tmp_path / "docker.py").write_text(_FAKE_DOCKER)
        binary = tmp_path / "docker"
        binary.write_text(f'#!/bin/sh\nexec "{sys.executable}" "{tmp_path / "docker.py"}" "$@"\n')
        binary.chmod(0o755)
        (tmp_path / "plan").write_text(" ".join(steps))
        return str(binary), tmp_path / "calls"

    return plan


@pytest.mark.parametrize(
    "plan",
    [
        pytest.param(["removed"], id="removed"),
        pytest.param(["gone"], id="no-such-container-and-success"),
        pytest.param(["missing"], id="no-such-container-from-an-older-client"),
        pytest.param(["busy", "missing"], id="another-removal-under-way"),
        pytest.param(["garbled", "removed"], id="output-that-is-not-text"),
    ],
)
def test_removing_a_helper_by_name_shows_it_gone(docker, plan) -> None:
    binary, calls = docker(*plan)

    assert _REMOVE_HELPER(binary, _NAME, 10.0) is True

    assert calls.read_text().splitlines() == [f"rm -f {_NAME}"] * len(plan)


def test_a_removal_the_daemon_refuses_is_tried_again_until_its_time_runs_out(docker) -> None:
    binary, calls = docker("down")

    began = time.monotonic()
    assert _REMOVE_HELPER(binary, _NAME, 1.0) is False
    assert time.monotonic() - began < 2.0
    assert len(calls.read_text().splitlines()) > 1, "tried only once"


def test_a_removal_whose_client_hangs_is_not_confirmed_once_its_time_is_up(docker) -> None:
    binary, calls = docker("hang")

    began = time.monotonic()
    assert _REMOVE_HELPER(binary, _NAME, 1.0) is False
    assert time.monotonic() - began < 2.5
    assert calls.read_text().splitlines() == [f"rm -f {_NAME}"]


def test_a_removal_with_no_docker_client_to_run_is_not_confirmed(tmp_path) -> None:
    assert _REMOVE_HELPER(str(tmp_path / "docker"), _NAME, 1.0) is False


# ── the guard's steps on its long-lived helper ──────────────────────────

#: Appended to a step the stand-in runs, so the record tells its steps from a
#: disposable helper's; the model of nft ignores it.
_SERVED = "run-by-the-long-lived-helper"


class _Steps:
    """The guard's runner over the model of nft, recording which kind of helper ran each step."""

    def __init__(self, nft: FakeNft) -> None:
        self.nft = nft
        #: ("long-lived" or "disposable", the step's request), in order.
        self.ran: list[tuple[str, dict[str, Any]]] = []
        #: Removals and disposable steps, in order, as (what, which).
        self.events: list[tuple[str, str]] = []
        #: The time each disposable step was given, and when it started.
        self.timeouts: list[float | None] = []
        self.started_at: list[float] = []
        #: A disposable step waits this out first, as a stalled one would.
        self.hang = False
        self._model = threading.Lock()

    def __call__(
        self, argv: list[str], stdin: str | None, timeout: float | None = None
    ) -> subprocess.CompletedProcess[str]:
        python = "--entrypoint" in argv and argv[argv.index("--entrypoint") + 1] == "python3"
        if python and argv[-1] != _SERVED:
            self.events.append(("disposable", ""))
            self.timeouts.append(timeout)
            self.started_at.append(time.monotonic())
            if self.hang:
                time.sleep(timeout or 0)
                raise NetworkGuardError("network guard helper could not run: timed out")
        with self._model:
            if python:
                kind = "long-lived" if argv[-1] == _SERVED else "disposable"
                self.ran.append((kind, json.loads(stdin or "{}")))
            return self.nft(argv, stdin)

    def kinds(self) -> list[str]:
        return [kind for kind, _request in self.ran]


class _PipeEnd:
    """One end of a pipe, as the guard holds a client's stdin or stdout."""

    def __init__(self, fd: int) -> None:
        self.fd = fd
        self.closed = False

    def fileno(self) -> int:
        return self.fd

    def close(self) -> None:
        if not self.closed:
            self.closed = True
            os.close(self.fd)


class _StandIn:
    """A long-lived helper's client and container, here: the program's protocol over real pipes.

    Each request is the step a disposable helper would run, run through the
    guard's runner. What goes back is ``answer``'s to decide: ``None`` ends it
    without an answer. Removing the container, as ``docker rm -f`` does, ends
    everything in it at once. Killing the client ends the client alone: a
    container already started goes on, and one the daemon is still creating
    never starts.
    """

    def __init__(
        self,
        argv: list[str],
        run: Any,
        answer: Any,
        errors: Any,
        events: list[tuple[str, str]],
        *,
        late: bool = False,
    ) -> None:
        self.argv = argv
        self.name = argv[3]
        self.requests: list[dict[str, Any]] = []
        self.returncode: int | None = None
        self.exit_status = 0
        #: "creating" while the daemon creates it, then "running", "created"
        #: (never to start) or "removed".
        self.container = "creating" if late else "running"
        self.killed = False
        #: Whether SIGKILL ends the client, as it does unless the client is
        #: stuck in the kernel.
        self.reapable = True
        #: Whether the guard's own process is interrupted, once, as it kills
        #: the client.
        self.interrupt_kill = False
        #: How long it takes to exit once its stdin closes.
        self.linger_s = 0.0
        self.gone = threading.Event()
        self.errors_fd = errors
        self._events = events
        self._run, self._answer = run, answer
        os.write(errors, b"stand-in client: attached\n")
        requests, stdin = os.pipe()
        stdout, self._answers = os.pipe()
        self.stdin, self.stdout = _PipeEnd(stdin), _PipeEnd(stdout)
        self._thread = threading.Thread(target=self._serve, args=(requests,), daemon=True)
        self._thread.start()

    @property
    def removed(self) -> bool:
        return self.container == "removed"

    def step(self, frame: dict[str, Any]) -> subprocess.CompletedProcess[str]:
        """Run a request as a disposable helper runs it; a removed container runs nothing."""
        if self.container != "running":
            return subprocess.CompletedProcess(self.argv, -9, "", "")
        command = [*self.argv[:2], *self.argv[4:-1], _NFT_BATCH, _SERVED]
        return self._run(command, frame["stdin"])

    def hold(self, until: threading.Event) -> None:
        """Stall, as a stuck container does, until ``until`` or until it is removed."""
        while not until.wait(0.01):
            if self.gone.is_set():
                return

    def blurt(self, data: bytes) -> None:
        """Write to the guard, asked or not."""
        os.write(self._answers, data)

    def rm_f(self) -> None:
        """What ``docker rm -f`` does: everything in the container ends, and it is removed.

        A container the daemon is still creating is not found, and the create
        completes just after: a live client then starts it, and a dead one
        leaves it created, never to start.
        """
        if self.container == "creating":
            self.container = "created" if self.killed else "running"
            return
        self.container = "removed"
        self.gone.set()

    def _serve(self, requests_fd: int) -> None:
        try:
            with open(requests_fd, "rb") as requests, open(self._answers, "wb", 0) as answers:
                while self.container == "creating" and not self.killed:
                    time.sleep(0.005)
                if self.container != "running":
                    return  # its client died first, so nothing starts it
                for line in requests:
                    frame = json.loads(line)
                    self.requests.append(json.loads(frame["stdin"]))
                    reply = self._answer(self, frame)
                    if reply is None:
                        break
                    answers.write(reply)
        except BrokenPipeError:
            pass  # the guard stopped reading, as a client's write would find
        finally:
            if not self.removed:
                self.gone.wait(self.linger_s)
            self.returncode = -9 if self.removed else self.exit_status

    def poll(self) -> int | None:
        # A client killed, or whose container is gone, has exited: ``docker
        # run`` returns once its container is removed.
        if self.killed or self.removed:
            return -9
        return None if self._thread.is_alive() else self.returncode

    def wait(self, timeout: float | None = None) -> int | None:
        if not (self.killed or self.removed):
            self._thread.join(timeout)
        status = self.poll()
        if status is None:
            raise subprocess.TimeoutExpired(self.argv, timeout or 0)
        return status

    def kill(self) -> None:
        """SIGKILL for its client, which ends unless it has ended already or cannot."""
        self._events.append(("killed", self.name))
        if self.interrupt_kill:
            self.interrupt_kill = False
            raise _Interrupted
        if self.reapable and self._thread.is_alive():
            self.killed = True


def _line(request_id: str, returncode: Any, stdout: Any, stderr: Any = "") -> bytes:
    answer = {"id": request_id, "returncode": returncode, "stdout": stdout, "stderr": stderr}
    return json.dumps(answer).encode() + b"\n"


def _answers(stand_in: _StandIn, frame: dict[str, Any]) -> bytes:
    """As the program answers: the step's status, output and errors, for this request."""
    result = stand_in.step(frame)
    return _line(frame["id"], result.returncode, result.stdout, result.stderr)


def _silent(_stand_in: _StandIn, _frame: dict[str, Any]) -> bytes:
    """Never answers, and ends when its stdin closes."""
    return b""


def _exits(_stand_in: _StandIn, _frame: dict[str, Any]) -> None:
    """Ends without answering."""
    return None


def _stuck(stand_in: _StandIn, _frame: dict[str, Any]) -> bytes:
    """Never answers, and ends only once removed."""
    stand_in.hold(threading.Event())
    return b""


def _for_relays(behaviour: Callable[[_StandIn, dict[str, Any]], Any]) -> Any:
    """Answers every step as the program does, except relay updates, which ``behaviour`` takes."""

    def answer(stand_in: _StandIn, frame: dict[str, Any]) -> Any:
        if "relays" not in json.loads(frame["stdin"]):
            return _answers(stand_in, frame)
        return behaviour(stand_in, frame)

    return answer


@pytest.fixture
def long_lived() -> Iterator[Callable[..., tuple[FakeNft, _Steps, HostNetworkGuard, list[Any]]]]:
    """Guards whose helpers both run on the model; their first helpers answer as told.

    ``removable=False`` stands for a daemon that cannot show a helper removed;
    a callable decides each removal, given its time. ``late`` lists the
    helpers, by the order they start in, whose containers the daemon creates
    only after the guard first tries to remove them. Every guard made is
    closed afterwards.
    """
    guards: list[HostNetworkGuard] = []

    def make(
        *answers: Any,
        removable: bool | Callable[[float], bool] = True,
        late: Collection[int] = (),
    ) -> tuple[FakeNft, _Steps, HostNetworkGuard, list[_StandIn]]:
        nft = FakeNft()
        steps = _Steps(nft)
        started: list[_StandIn] = []

        def spawn(argv: list[str], errors: Any) -> _StandIn:
            index = len(started)
            answer = answers[index] if index < len(answers) else _answers
            started.append(
                _StandIn(
                    argv,
                    lambda *step: guard._run(*step),
                    answer,
                    errors,
                    steps.events,
                    late=index in late,
                )
            )
            return started[-1]

        def remove(name: str, timeout: float) -> bool:
            steps.events.append(("removed", name))
            works = removable(timeout) if callable(removable) else removable
            if works:
                for stand_in in started:
                    if stand_in.name == name:
                        stand_in.rm_f()
            return works

        guard = HostNetworkGuard(
            _network(), helper_image=_IMAGE, run=steps, spawn=spawn, remove=remove
        )
        guards.append(guard)
        return nft, steps, guard, started

    yield make
    for guard in guards:
        guard.close()


@pytest.fixture
def quick(monkeypatch: pytest.MonkeyPatch) -> None:
    """A long-lived step gives up in half a second rather than tens of seconds."""
    monkeypatch.setattr(network_guard_helper, "SERVE_REQUEST_S", 0.3)
    monkeypatch.setattr(network_guard_helper, "ANSWER_MARGIN_S", 0.1)
    monkeypatch.setattr(network_guard_helper, "START_ALLOWANCE_S", 0.1)


def test_every_step_runs_on_one_long_lived_helper_and_routes_stay_unprivileged(long_lived) -> None:
    nft, steps, guard, started = long_lived()

    guard.ensure()
    guard.sync_relays(_X, {("10.240.0.3", 8080)})
    guard.sync_ssh_targets(_X, {("93.184.216.34", 22)})
    guard.recheck()

    assert len(started) == 1, "a helper container per step"
    assert steps.kinds() == ["long-lived"] * 4
    assert [request for _kind, request in steps.ran] == started[0].requests
    routes = [argv for argv, _ in nft.calls if argv[argv.index("--entrypoint") + 1] == "ip"]
    assert len(routes) == 1 and "--cap-add" not in routes[0], "the route read gained a capability"
    assert guard.relay_endpoints(_X) == {("10.240.0.3", 8080)}


def test_the_long_lived_helper_is_a_disposable_helper_with_a_name_of_its_own(long_lived) -> None:
    """Today's flags, ``--interactive`` included as a disposable helper has it, and ``--name``."""
    nft, _steps, guard, started = long_lived()
    guard.ensure()
    calls = len(nft.calls)
    HostNetworkGuard(_network(), helper_image=_IMAGE, run=nft).ensure()
    (step, _), (route, _) = nft.calls[calls:]

    flags = [
        "--rm",
        "--interactive",
        "--pull",
        "never",
        "--network",
        "host",
        "--runtime",
        "runc",
        "--cap-drop",
        "ALL",
        "--cap-add",
        "NET_ADMIN",
        "--security-opt",
        "no-new-privileges",
        "--read-only",
        "--user",
        "0:0",
    ]
    argv = started[0].argv
    assert argv[:3] == ["docker", "run", "--name"]
    prefix = f"{guard.network.table}-helper-"
    assert argv[3].startswith(prefix) and len(argv[3]) == len(prefix) + 12, argv[3]
    assert argv[4:] == [*flags, "--entrypoint", "python3", _IMAGE, "-c", _NFT_SERVE]
    assert step == ["docker", "run", *flags, "--entrypoint", "python3", _IMAGE, "-c", _NFT_BATCH]
    without = {"--interactive", "--cap-add", "NET_ADMIN"}
    routes = ["-4", "-j", "route", "show", "table", "all"]
    unprivileged = [flag for flag in flags if flag not in without]
    assert route == ["docker", "run", *unprivileged, "--entrypoint", "ip", _IMAGE, *routes]


def test_every_helper_gets_a_name_of_its_own(monkeypatch, long_lived) -> None:
    _nft, _steps, guard, started = long_lived()
    monkeypatch.setattr(network_guard_helper, "RETIRE_AGE_S", 0.0)

    guard.ensure()
    guard.recheck()

    assert len({stand_in.name for stand_in in started}) == len(started) == 2


def test_a_guard_without_spawn_or_with_a_runner_of_its_own_keeps_no_long_lived_helper() -> None:
    """Only a guard given ``spawn`` ever starts one, so a stand-in for Docker never meets it."""
    assert HostNetworkGuard(_network())._long_lived is None
    assert HostNetworkGuard(_network(), helper_image=_IMAGE, run=FakeNft())._long_lived is None


def test_a_step_on_the_long_lived_helper_shares_a_disposable_helpers_minute(long_lived) -> None:
    """The long-lived attempt and any disposable re-run come out of one minute together."""
    _nft, _steps, guard, _started = long_lived()
    assert guard._long_lived is not None
    serve = guard._long_lived.run
    lefts: list[float] = []

    def run(*args: Any, deadline: float, **kwargs: Any) -> Any:
        lefts.append(deadline - time.monotonic())
        return serve(*args, deadline=deadline, **kwargs)

    guard._long_lived.run = run  # type: ignore[method-assign]

    guard.ensure()

    (left,) = lefts
    assert network_guard._HELPER_TIMEOUT_S - 1 < left <= network_guard._HELPER_TIMEOUT_S == 60


def test_a_steps_minute_starts_once_its_image_is_known(monkeypatch, long_lived) -> None:
    """Looking the image up is not the helper's time, as it never was a disposable helper's."""
    _nft, steps, guard, _started = long_lived()
    guard._helper_image = None
    assert guard._long_lived is not None
    serve = guard._long_lived.run
    lefts: list[float] = []

    def run(*args: Any, deadline: float, **kwargs: Any) -> Any:
        lefts.append(deadline - time.monotonic())
        return serve(*args, deadline=deadline, **kwargs)

    def slow_lookup(argv: list[str], stdin: str | None, timeout: float | None = None) -> Any:
        if argv[1:3] == ["container", "inspect"]:
            time.sleep(0.5)
        return steps(argv, stdin, timeout)

    monkeypatch.setattr(guard._long_lived, "run", run)
    monkeypatch.setattr(guard, "_run", slow_lookup)

    guard.ensure()

    assert guard._helper_image == _IMAGE
    assert lefts[0] > network_guard._HELPER_TIMEOUT_S - 0.25, "the lookup came out of the minute"


@pytest.mark.parametrize("answer", [_silent, _exits, _stuck], ids=["silent", "exits", "stuck"])
def test_a_helper_that_does_not_answer_is_removed_before_a_disposable_one_reruns_its_step(
    quick, long_lived, answer
) -> None:
    nft, steps, guard, started = long_lived(answer)

    guard.ensure()

    assert ("inet", guard.network.table) in nft.tables
    assert steps.kinds() == ["disposable"]
    assert started[0].requests == [steps.ran[0][1]], "not the same step run again"
    name = started[0].name
    assert steps.events == [("killed", name), ("removed", name), ("disposable", "")]
    assert started[0].removed and started[0].wait(5) is not None
    assert guard._long_lived is not None
    assert guard._long_lived._running.retiring == [], "a reaped client was kept to reap again"


def _misanswering(make: Callable[[str, subprocess.CompletedProcess[str]], bytes]) -> Any:
    """A helper that runs each relay update, so the kernel changes, then answers as ``make`` says.

    A relay update's answer is a few hundred bytes, so even two of them reach
    the guard in one atomic pipe write.
    """
    return _for_relays(lambda stand_in, frame: make(frame["id"], stand_in.step(frame)))


@pytest.mark.parametrize(
    "make",
    [
        pytest.param(lambda _id, _done: b"{not json\n", id="not-json"),
        pytest.param(lambda _id, _done: b"[]\n", id="not-an-object"),
        pytest.param(lambda _id, done: _line("0" * 16, 0, done.stdout), id="another-request"),
        pytest.param(lambda id_, done: _line(id_, "0", done.stdout), id="status-as-text"),
        pytest.param(lambda id_, done: _line(id_, False, done.stdout), id="status-false"),
        pytest.param(lambda id_, _done: _line(id_, 0, ["listings"]), id="output-not-text"),
        pytest.param(lambda id_, done: _line(id_, 0, done.stdout, ["boom"]), id="errors-not-text"),
        pytest.param(lambda id_, done: _line(id_, 0, done.stdout) * 2, id="two-answers"),
        pytest.param(lambda id_, done: _line(id_, 0, done.stdout) + b"\n", id="a-line-after-it"),
        pytest.param(lambda id_, done: _line(id_, 1, done.stdout, "boom"), id="step-failed"),
        pytest.param(lambda id_, _done: _line(id_, 0, "not json"), id="unreadable-output"),
    ],
)
def test_any_answer_but_a_readable_success_reruns_the_step_after_removing_the_helper(
    long_lived, make
) -> None:
    """Even one whose output looks right: a status, its own request's id and one line, or nothing."""
    _nft, steps, guard, started = long_lived(_misanswering(make))
    guard.ensure()

    guard.sync_relays(_X, {("10.240.0.3", 8080)})

    assert steps.kinds() == ["long-lived", "long-lived", "disposable"]
    assert steps.ran[-2][1] == steps.ran[-1][1], "not the same step run again"
    name = started[0].name
    assert steps.events == [("killed", name), ("removed", name), ("disposable", "")]
    assert guard.relay_endpoints(_X) == {("10.240.0.3", 8080)}


def test_an_answer_split_across_reads_is_put_back_together(long_lived) -> None:
    def in_two_pieces(stand_in: _StandIn, frame: dict[str, Any]) -> bytes:
        reply = _answers(stand_in, frame)
        stand_in.blurt(reply[: len(reply) // 2])
        time.sleep(0.2)
        return reply[len(reply) // 2 :]

    _nft, steps, guard, started = long_lived(in_two_pieces)

    guard.ensure()

    assert steps.kinds() == ["long-lived"]
    assert len(started) == 1


def test_an_answer_line_past_its_limit_is_refused(monkeypatch, long_lived, caplog) -> None:
    def endless(stand_in: _StandIn, _frame: dict[str, Any]) -> bytes:
        stand_in.blurt(b"x" * 300)
        stand_in.hold(threading.Event())
        return b""

    monkeypatch.setattr(network_guard_helper, "ANSWER_MAX", 100)
    _nft, steps, guard, started = long_lived(endless)

    with caplog.at_level(logging.WARNING):
        guard.ensure()

    assert steps.kinds() == ["disposable"]
    assert steps.events[:2] == [("killed", started[0].name), ("removed", started[0].name)]
    (record,) = [r for r in caplog.records if r.getMessage() == "network_guard_helper_discarded"]
    assert record.detail == "answered more than 100 bytes"


def test_a_step_that_starts_the_helper_allows_for_the_container_to_start(
    monkeypatch, quick, long_lived
) -> None:
    """A first answer may come later than a running helper's, by the start allowance."""
    monkeypatch.setattr(network_guard_helper, "START_ALLOWANCE_S", 1.6)

    def slow_start(stand_in: _StandIn, frame: dict[str, Any]) -> bytes:
        time.sleep(0.8)  # twice the request time and margin, half what the allowance adds
        return _answers(stand_in, frame)

    _nft, steps, guard, _started = long_lived(slow_start)

    guard.ensure()

    assert steps.kinds() == ["long-lived"]


def test_a_failed_helper_is_reported_with_its_clients_exit_status_and_stderr(
    quick, long_lived, caplog
) -> None:
    _nft, _steps, guard, started = long_lived(_exits)

    with caplog.at_level(logging.WARNING):
        guard.ensure()

    (record,) = [r for r in caplog.records if r.getMessage() == "network_guard_helper_discarded"]
    assert record.helper == started[0].name and record.removed is True
    assert record.exit_status == 0, "the client's own exit status, before its removal"
    assert "stand-in client: attached" in record.stderr
    assert "exited before it answered" in record.detail


def test_a_step_that_fails_on_both_raises_as_a_failing_helper_always_did(long_lived) -> None:
    nft, steps, guard, _started = long_lived()
    guard.ensure()
    del nft.table("bridge", guard.network.table)["sets"]["relays"]

    with pytest.raises(NetworkGuardError, match=r"^nft failed: No such file or directory"):
        guard.sync_relays(_X, {("10.240.0.3", 8080)})

    assert steps.kinds()[-2:] == ["long-lived", "disposable"]


def test_a_read_back_on_the_helper_that_installed_sees_the_kernel_change_since(long_lived) -> None:
    """A failed check on its answer raises as before; nothing runs it again to hope for another."""
    nft, steps, guard, started = long_lived()
    guard.ensure()
    nft.table("inet", guard.network.table)["rules"]["from_group"] = 0  # a flushed chain

    with pytest.raises(NetworkGuardError, match="from_group"):
        guard._verify(render_ruleset(guard.network))

    assert steps.kinds() == ["long-lived", "long-lived"]
    assert len(started) == 1


# ── no late write, and one minute for a step ─────────────────────────────


def test_a_rerun_step_is_never_overtaken_by_the_helper_that_stalled_on_it(
    quick, long_lived
) -> None:
    """The stalled helper is removed before the re-run, so its write can never land after a later step.

    Without the removal, the container goes on: once it resumes it writes the
    first update's exceptions over the later update that emptied the bridge.
    """
    resumed = threading.Event()

    def stalls(stand_in: _StandIn, frame: dict[str, Any]) -> bytes:
        stand_in.hold(resumed)
        stand_in.step(frame)
        return b""

    _nft, steps, guard, started = long_lived(_for_relays(stalls))
    guard.ensure()

    guard.sync_relays(_X, {("10.240.0.3", 8080)})
    guard.sync_relays(_X, set())
    resumed.set()
    started[0].wait(5)

    assert guard.relay_endpoints(_X) == set()
    name = started[0].name
    assert steps.events[:3] == [("killed", name), ("removed", name), ("disposable", "")]


def test_a_helper_that_cannot_be_shown_removed_fails_the_step_and_nothing_reruns_it(
    quick, long_lived
) -> None:
    _nft, steps, guard, started = long_lived(_for_relays(_silent), removable=False)
    guard.ensure()

    with pytest.raises(NetworkGuardError, match="could not be shown removed"):
        guard.sync_relays(_X, {("10.240.0.3", 8080)})

    name = started[0].name
    assert steps.events == [("killed", name), ("removed", name)], "a step ran beside a live helper"


def test_a_hung_step_takes_no_longer_than_a_disposable_helpers_minute(
    monkeypatch, quick, long_lived
) -> None:
    """The long-lived helper gives up after its own request time; the re-run gets what is left."""
    monkeypatch.setattr(network_guard, "_HELPER_TIMEOUT_S", 2.0)
    _nft, steps, guard, _started = long_lived(_for_relays(_silent))
    guard.ensure()
    steps.hang = True

    began = time.monotonic()
    with pytest.raises(NetworkGuardError, match="timed out"):
        guard.sync_relays(_X, {("10.240.0.3", 8080)})
    took = time.monotonic() - began

    (given,) = steps.timeouts
    rerun_after = steps.started_at[0] - began
    assert 0.35 < rerun_after < 1.0, "the running helper did not give up after its request time"
    assert given is not None and given < 2.0 - 0.35
    assert took < 2.3, "the step outlasted a disposable helper's minute"


def test_a_container_created_only_after_the_removal_check_never_takes_the_step(
    monkeypatch, quick, long_lived
) -> None:
    """``docker rm -f`` finds no container yet and succeeds; the create completes just after.

    The guard killed and reaped the client first, so nothing starts that
    container, and the update it was handed never lands. A client killed only
    after the removal could start it, holding that update, to land whenever
    the container got to it.
    """
    resumed = threading.Event()

    def stalls(stand_in: _StandIn, frame: dict[str, Any]) -> bytes:
        stand_in.hold(resumed)
        stand_in.step(frame)
        return b""

    _nft, steps, guard, started = long_lived(_answers, stalls, late={1})
    guard.ensure()
    # The update starts a helper of its own: the one the daemon creates late.
    monkeypatch.setattr(network_guard_helper, "RETIRE_AGE_S", 0.0)

    guard.sync_relays(_X, {("10.240.0.3", 8080)})
    late = started[1]
    assert late.container == "created", "its client was alive to start it after the removal"
    guard.sync_relays(_X, set())
    resumed.set()
    time.sleep(0.2)  # time for a container that did start to write

    assert late.requests == [], "the container started after its removal and took the request"
    assert guard.relay_endpoints(_X) == set()
    assert steps.events[:3] == [("killed", late.name), ("removed", late.name), ("disposable", "")]


def test_a_container_left_created_is_removed_by_later_steps_until_it_has_settled(
    monkeypatch, quick, long_lived
) -> None:
    """A create the daemon had under way when the client died completes after the removal check."""
    monkeypatch.setattr(network_guard_helper, "LEFTOVER_S", 0.5)
    _nft, steps, guard, started = long_lived(late={1})
    guard.ensure()
    monkeypatch.setattr(network_guard_helper, "RETIRE_AGE_S", 0.0)

    guard.relay_endpoints(_X)  # on the late helper, which never answers
    late = started[1]
    assert late.container == "created", "left behind, never to start"
    guard.relay_endpoints(_X)
    assert late.container == "removed", "the next step did not remove it"
    time.sleep(0.6)
    guard.relay_endpoints(_X)  # shown gone once more, now that it has settled
    guard.relay_endpoints(_X)

    assert steps.events.count(("removed", late.name)) == 3, "removed again, or never forgotten"


def test_a_helper_not_shown_removed_is_removed_again_by_later_steps_which_go_ahead(
    quick, long_lived
) -> None:
    """Only the step it failed fails; each later step first tries the removal again."""
    works = False
    _nft, steps, guard, started = long_lived(_for_relays(_stuck), removable=lambda _timeout: works)
    guard.ensure()
    with pytest.raises(NetworkGuardError, match="its removal was not confirmed"):
        guard.sync_relays(_X, {("10.240.0.3", 8080)})
    name = started[0].name

    guard.sync_relays(_X, {("10.240.0.3", 8080)})
    assert steps.events[-2:] == [("removed", name), ("disposable", "")]
    assert not started[0].removed
    works = True
    guard.sync_relays(_X, set())

    assert started[0].removed, "a later step did not remove it once it could"
    assert guard.relay_endpoints(_X) == set()


def test_a_later_step_spends_no_more_than_its_share_removing_names_again(
    monkeypatch, quick, long_lived
) -> None:
    monkeypatch.setattr(network_guard_helper, "SWEEP_S", 0.3)
    hanging = False

    def hangs_later(timeout: float) -> bool:
        if hanging:
            time.sleep(timeout)
        return not hanging

    _nft, steps, guard, started = long_lived(_for_relays(_silent), removable=hangs_later)
    guard.ensure()
    guard.sync_relays(_X, {("10.240.0.3", 8080)})  # its helper fails, and leaves its name
    hanging = True

    began = time.monotonic()
    guard.sync_relays(_X, set())
    took = time.monotonic() - began
    hanging = False

    assert steps.events[-2:] == [("removed", started[0].name), ("disposable", "")]
    assert took < 0.8, "removing a name again held up the step"


def test_a_step_interrupted_while_it_removes_a_name_again_keeps_the_name(quick, long_lived) -> None:
    interrupting = False

    def interrupted(_timeout: float) -> bool:
        if interrupting:
            raise _Interrupted
        return True

    _nft, _steps, guard, started = long_lived(_for_relays(_silent), removable=interrupted)
    guard.ensure()
    guard.sync_relays(_X, {("10.240.0.3", 8080)})  # its helper fails, and leaves its name
    interrupting = True
    with pytest.raises(_Interrupted):
        guard.sync_relays(_X, set())
    interrupting = False

    assert guard._long_lived is not None
    assert list(guard._long_lived._running.leftovers) == [started[0].name]


def test_an_interrupted_take_down_leaves_its_name_and_its_client_to_later_steps(
    quick, long_lived
) -> None:
    """A second SIGINT as the guard kills a failed helper's client, say: later steps finish it."""

    def interrupted_as_it_is_killed(stand_in: _StandIn, frame: dict[str, Any]) -> bytes:
        stand_in.interrupt_kill = True
        return _stuck(stand_in, frame)

    _nft, steps, guard, started = long_lived(_for_relays(interrupted_as_it_is_killed))
    guard.ensure()
    with pytest.raises(_Interrupted):
        guard.sync_relays(_X, {("10.240.0.3", 8080)})
    lived, helper = guard._long_lived, started[0]
    assert lived is not None
    assert list(lived._running.leftovers) == [helper.name], "its name was lost"
    assert [kept.process for kept in lived._running.retiring] == [helper], "its client was lost"
    assert helper.poll() is None

    guard.relay_endpoints(_X)  # removes its name again, so its container and client end
    guard.relay_endpoints(_X)  # reaps the client

    assert helper.removed and ("removed", helper.name) in steps.events
    assert lived._running.retiring == [], "its client was never reaped"
    assert helper.stdin.closed and helper.stdout.closed


def test_stopping_a_helper_whose_client_cannot_be_reaped_closes_its_stderr_file_once(
    monkeypatch, long_lived
) -> None:
    """Its client stays among the retiring, whose files shutdown closes as well."""
    monkeypatch.setattr(network_guard_helper, "CLOSE_GRACE_S", 0.1)
    monkeypatch.setattr(network_guard_helper, "KILL_REAP_S", 0.1)
    _nft, _steps, guard, started = long_lived()
    guard.ensure()
    helper = started[0]
    helper.reapable = False  # stuck in the kernel,
    helper.linger_s = 5.0  # so it does not end at EOF on its stdin either
    closed: list[int] = []
    close = os.close

    def recording(fd: int) -> None:
        closed.append(fd)
        close(fd)

    monkeypatch.setattr(os, "close", recording)

    guard.close()

    assert helper.removed
    assert closed.count(helper.errors_fd) == 1


def test_a_client_that_does_not_exit_when_killed_fails_the_step_and_is_reaped_later(
    monkeypatch, quick, long_lived
) -> None:
    """Until its client is gone it could still start a container, so nothing reruns the step."""
    monkeypatch.setattr(network_guard_helper, "KILL_REAP_S", 0.2)

    def stuck_in_the_kernel(stand_in: _StandIn, _frame: dict[str, Any]) -> bytes:
        stand_in.reapable = False
        stand_in.hold(threading.Event())
        return b""

    _nft, steps, guard, started = long_lived(_for_relays(stuck_in_the_kernel))
    guard.ensure()
    with pytest.raises(NetworkGuardError, match="its client did not exit"):
        guard.sync_relays(_X, {("10.240.0.3", 8080)})
    assert steps.kinds()[-1] == "long-lived", "a disposable helper reran the step"
    assert guard._long_lived is not None
    assert [helper.process for helper in guard._long_lived._running.retiring] == [started[0]]
    started[0].wait(5)  # the removal ended its container, and with it the client

    guard.relay_endpoints(_X)

    assert guard._long_lived._running.retiring == [], "its client was never reaped"


def test_a_removal_that_raises_counts_as_not_shown_removed(quick, long_lived, caplog) -> None:
    def broken(_timeout: float) -> bool:
        raise RuntimeError("an answer the client could not read")

    _nft, steps, guard, _started = long_lived(_for_relays(_silent), removable=broken)
    guard.ensure()

    with (
        caplog.at_level(logging.WARNING),
        pytest.raises(NetworkGuardError, match="its removal was not confirmed"),
    ):
        guard.sync_relays(_X, {("10.240.0.3", 8080)})

    assert steps.kinds()[-1] == "long-lived", "a disposable helper reran the step"
    failures = [
        r for r in caplog.records if r.getMessage() == "network_guard_helper_removal_failed"
    ]
    assert [r.detail for r in failures] == ["RuntimeError: an answer the client could not read"]
    guard.sync_relays(_X, {("10.240.0.3", 8080)})  # later steps go ahead all the same
    assert guard.relay_endpoints(_X) == {("10.240.0.3", 8080)}


def test_a_failed_long_lived_attempt_leaves_a_disposable_helper_time_in_the_minute() -> None:
    """At worst: names removed again, a helper that idled out, a fresh one that never answers."""
    h = network_guard_helper
    idled_out = h.SERVE_REQUEST_S + h.ANSWER_MARGIN_S + h.CLOSE_GRACE_S
    fresh = h.SERVE_REQUEST_S + h.ANSWER_MARGIN_S + h.START_ALLOWANCE_S
    worst = h.SWEEP_S + idled_out + fresh + h.KILL_REAP_S + h.REMOVE_TIMEOUT_S
    assert worst + 5 <= network_guard._HELPER_TIMEOUT_S, worst


def test_every_part_of_a_failed_step_ends_with_its_minute(monkeypatch, long_lived) -> None:
    """A helper that never answers, a client SIGKILL cannot end and a removal that hangs."""
    monkeypatch.setattr(network_guard, "_HELPER_TIMEOUT_S", 1.0)
    hanging = True

    def stuck_in_the_kernel(stand_in: _StandIn, _frame: dict[str, Any]) -> bytes:
        stand_in.reapable = False
        stand_in.hold(threading.Event())
        return b""

    def hangs(timeout: float) -> bool:
        if hanging:
            time.sleep(timeout)
        return not hanging

    _nft, _steps, guard, started = long_lived(_for_relays(stuck_in_the_kernel), removable=hangs)
    guard.ensure()

    began = time.monotonic()
    with pytest.raises(NetworkGuardError, match="could not be shown removed"):
        guard.sync_relays(_X, {("10.240.0.3", 8080)})
    took = time.monotonic() - began
    hanging = False
    started[0].rm_f()

    assert 0.9 < took < 1.4, "a part of the step outlasted its minute"


# ── when a helper is replaced ───────────────────────────────────────────


def test_a_helper_that_idled_out_as_a_request_reached_it_is_replaced_without_a_failure(
    long_lived, caplog
) -> None:
    """It left before it read the request, so the request never ran: a new helper takes it."""

    def idled_out(stand_in: _StandIn, _frame: dict[str, Any]) -> None:
        stand_in.exit_status = network_guard_helper.IDLE_EXIT
        return None

    _nft, steps, guard, started = long_lived(idled_out)

    with caplog.at_level(logging.WARNING):
        guard.ensure()
        guard.recheck()

    assert len(started) == 2
    assert steps.kinds() == ["long-lived", "long-lived"], "cooling down after no failure"
    assert not [r for r in caplog.records if r.getMessage() == "network_guard_helper_discarded"]
    assert not started[0].removed


def test_the_guard_retires_an_idle_helper_well_before_it_would_leave() -> None:
    assert network_guard_helper.RETIRE_IDLE_S + 5 <= network_guard_helper.SERVE_IDLE_S


def test_after_a_failure_every_step_runs_on_disposable_helpers_for_a_while(
    monkeypatch, quick, long_lived
) -> None:
    """A helper that fails every time costs one failure per period, not one per step."""
    monkeypatch.setattr(network_guard_helper, "COOLDOWN_S", 1.0)
    _nft, steps, guard, started = long_lived(_exits)
    guard.ensure()
    guard.recheck()
    assert steps.kinds() == ["disposable", "disposable"]
    assert len(started) == 1
    time.sleep(1.1)

    guard.relay_endpoints(_X)

    assert len(started) == 2
    assert steps.kinds()[-1] == "long-lived"


@pytest.mark.parametrize("limit", ["RETIRE_IDLE_S", "RETIRE_AGE_S"])
def test_a_helper_past_its_time_is_retired_and_a_new_one_takes_the_step(
    monkeypatch, long_lived, limit
) -> None:
    _nft, steps, guard, started = long_lived()
    guard.ensure()
    monkeypatch.setattr(network_guard_helper, limit, 0.0)

    guard.relay_endpoints(_X)

    assert len(started) == 2
    assert started[0].wait(5) == 0 and not started[0].removed
    assert steps.kinds() == ["long-lived", "long-lived"], "a retirement is no failure"


def test_retiring_a_helper_does_not_hold_up_the_step_that_retires_it(
    monkeypatch, long_lived
) -> None:
    _nft, _steps, guard, started = long_lived()
    guard.ensure()
    started[0].linger_s = 1.5  # its client takes its time once its stdin closes
    monkeypatch.setattr(network_guard_helper, "RETIRE_AGE_S", 0.0)

    began = time.monotonic()
    guard.recheck()

    assert time.monotonic() - began < 1.0
    assert len(started) == 2 and started[0].poll() is None, "it waited for the old client"


def test_a_later_step_reaps_the_clients_of_retired_helpers(monkeypatch, long_lived) -> None:
    _nft, _steps, guard, started = long_lived()
    guard.ensure()
    started[0].linger_s = 0.3
    monkeypatch.setattr(network_guard_helper, "RETIRE_AGE_S", 0.0)
    guard.recheck()
    assert guard._long_lived is not None
    assert [helper.process for helper in guard._long_lived._running.retiring] == [started[0]]
    started[0].wait(5)
    monkeypatch.setattr(network_guard_helper, "RETIRE_AGE_S", 300.0)

    guard.recheck()

    assert guard._long_lived._running.retiring == [], "a retired client was left unreaped"


def test_output_a_helper_wrote_unasked_is_never_read_as_an_answer(long_lived) -> None:
    _nft, steps, guard, started = long_lived()
    guard.ensure()
    started[0].blurt(_line("unasked", 0, json.dumps({"listings": [{"nftables": []}]})))

    guard.relay_endpoints(_X)

    assert len(started) == 2, "the helper that spoke unasked was used again"
    assert started[0].wait(5) == 0
    assert steps.kinds() == ["long-lived", "long-lived"]


def test_a_step_for_another_command_line_gets_a_helper_of_its_own(long_lived) -> None:
    _nft, _steps, guard, started = long_lived()
    guard.ensure()
    image = "sha256:" + "b" * 64
    guard._helper_image = image

    guard.relay_endpoints(_X)

    assert [image in stand_in.argv for stand_in in started] == [False, True]
    assert started[0].wait(5) == 0


def test_closing_the_guard_ends_its_long_lived_helper_and_starts_no_other(long_lived) -> None:
    _nft, steps, guard, started = long_lived()
    guard.ensure()

    guard.close()

    assert started[0].poll() == 0 and not started[0].removed, "it should end at EOF on its stdin"
    guard.ensure(max_age_s=0)
    assert len(started) == 1
    assert steps.kinds() == ["long-lived", "disposable"]


def _until(condition: Callable[[], bool], seconds: float = 5) -> bool:
    deadline = time.monotonic() + seconds
    while not condition():
        if time.monotonic() > deadline:
            return False
        time.sleep(0.01)
    return True


def _held_update(
    long_lived: Any,
) -> tuple[_Steps, HostNetworkGuard, _StandIn, threading.Event, Any]:
    """A verified guard whose helper has taken a relay update, which it holds until let go."""
    release = threading.Event()

    def held(stand_in: _StandIn, frame: dict[str, Any]) -> bytes:
        stand_in.hold(release)
        return _answers(stand_in, frame)

    _nft, steps, guard, started = long_lived(_for_relays(held))
    guard.ensure()
    taken = len(started[0].requests)
    update = _in_thread(lambda: guard.sync_relays(_X, {("10.240.0.3", 8080)}))
    assert _until(lambda: len(started[0].requests) > taken), "the update never reached the helper"
    return steps, guard, started[0], release, update


def test_stopping_at_exit_waits_for_a_step_under_way_and_leaves_it_its_helper(
    long_lived,
) -> None:
    """The finalizer, as interpreter exit runs it, takes the step's slot before it stops anything."""
    steps, guard, helper, release, update = _held_update(long_lived)
    assert guard._long_lived is not None

    stopping = _in_thread(guard._long_lived._stop)
    time.sleep(0.2)
    release.set()
    update.result(5)
    stopping.result(5)

    assert steps.kinds()[-1] == "long-lived", "the step lost its helper to the finalizer"
    assert ("removed", helper.name) not in steps.events
    assert helper.poll() == 0 and not helper.removed, "stopped as an idle helper is, after the step"


def test_stopping_at_exit_leaves_a_helper_to_a_step_that_outlasts_its_wait(
    monkeypatch, long_lived
) -> None:
    """A step that holds its slot past the wait keeps its helper: nothing closes it under the step."""
    monkeypatch.setattr(network_guard_helper, "SHUTDOWN_WAIT_S", 0.2)
    steps, guard, helper, release, update = _held_update(long_lived)
    lived = guard._long_lived
    assert lived is not None

    began = time.monotonic()
    network_guard_helper._shutdown(lived._running, lived._remove, lived._slot)
    gave_up_after = time.monotonic() - began
    release.set()
    update.result(5)

    assert 0.15 < gave_up_after < 1.0
    assert steps.kinds()[-1] == "long-lived", "the step lost its helper"
    assert helper.poll() is None and lived._running.helper is not None


# ── which processes keep one ─────────────────────────────────────────────

_BACKEND_ENV = {**_ENV, "AGENT_SANDBOX_BACKEND": "container", "AGENT_SANDBOX_IMAGE": "img:1"}


def test_only_a_backend_built_to_keep_one_gives_its_guard_a_long_lived_helper() -> None:
    """A turn's own process builds its backend plainly, so its guard runs disposable helpers."""
    from cloud_agent_host.sandbox import ContainerBackend, build_backend_from_env

    plain = build_backend_from_env(dict(_BACKEND_ENV))
    kept = build_backend_from_env(dict(_BACKEND_ENV), keep_network_helper=True)

    assert isinstance(plain, ContainerBackend) and isinstance(kept, ContainerBackend)
    assert plain.network_guard()._long_lived is None
    assert kept.network_guard()._long_lived is not None


class _Built(Exception):
    """Raised by a stand-in for ``build_backend_from_env`` once it has seen how it was called."""


def _builder(calls: list[dict[str, Any]]) -> Callable[..., Any]:
    def build(*args: Any, **kwargs: Any) -> Any:
        calls.append(kwargs)
        raise _Built

    return build


def test_the_runners_host_service_and_the_sandbox_manager_keep_one(monkeypatch) -> None:
    """They outlive their steps; a turn's child, which the host service starts, does not."""
    from cloud_agent_host import runner as runner_mod, sandbox_manager

    calls: list[dict[str, Any]] = []
    monkeypatch.setattr(runner_mod, "build_backend_from_env", _builder(calls))
    monkeypatch.setattr(sandbox_manager, "build_backend_from_env", _builder(calls))

    with pytest.raises(_Built):
        runner_mod.run_forever(base_url="http://control", worker_id="worker-1")
    with pytest.raises(_Built):
        sandbox_manager.create_app(token="token")

    assert calls == [{"keep_network_helper": True}] * 2


# ── who waits for whom, with the long-lived helper in use ────────────────


def _ssh_long_lived(long_lived: Any) -> tuple[_Steps, HostNetworkGuard, list[_StandIn]]:
    """A verified guard on its long-lived helper that wrote SSH targets for both bridges."""
    _nft, steps, guard, started = long_lived()
    guard.ensure()
    for bridge in (_X, _Y):
        guard.sync_ssh_targets(bridge, {("93.184.216.34", 22)}, network_id=_NET)
    return steps, guard, started


def test_a_step_that_finds_the_helper_busy_runs_beside_it_on_a_disposable_one(long_lived) -> None:
    """Concurrent updates of different bridges do not queue behind the long-lived helper."""
    steps, guard, _started = _ssh_long_lived(long_lived)
    helpers = _hold(guard)

    first = _in_thread(lambda: guard.sync_relays(_X, {("10.240.0.3", 8080)}))
    assert helpers.wait_for(lambda: helpers.running[_X] == 1)
    second = _in_thread(lambda: guard.sync_relays(_Y, {("10.240.0.11", 8080)}))
    try:
        assert helpers.wait_for(lambda: helpers.running[_Y] or helpers.parked(second))
        assert not helpers.parked(second), "an update waited for another bridge's helper"
        assert helpers.running[_X] == helpers.running[_Y] == 1
    finally:
        helpers.release()
    first.result(5)
    second.result(5)

    assert helpers.overlaps == []
    assert {(kind, request["relays"]["bridge"]) for kind, request in steps.ran[-2:]} == {
        ("long-lived", _X),
        ("disposable", _Y),
    }


@pytest.mark.parametrize("kind", _KINDS)
def test_an_install_still_waits_for_an_update_on_the_long_lived_helper(long_lived, kind) -> None:
    steps, guard, _started = _ssh_long_lived(long_lived)
    helpers = _hold(guard)

    update = _in_thread(lambda: _update(kind, guard, _X))
    assert helpers.wait_for(lambda: helpers.running[_X] == 1)
    install = _in_thread(lambda: guard.ensure(max_age_s=0))
    try:
        assert helpers.wait_for(lambda: helpers.parked(install) or helpers.running["install"])
    finally:
        helpers.release()
    update.result(5)
    install.result(5)

    assert helpers.overlaps == [], "an install ran beside an update"
    assert helpers.started == [_X, "install", "routes"]
    assert steps.kinds()[-2:] == ["long-lived", "long-lived"]


@pytest.mark.parametrize("kind", _KINDS)
def test_an_update_waits_for_an_install_on_the_long_lived_helper_not_for_a_disposable_one(
    long_lived, kind
) -> None:
    """A busy long-lived helper sends a step to a disposable one only once its lock allows the step."""
    steps, guard, _started = _ssh_long_lived(long_lived)
    helpers = _hold(guard)

    install = _in_thread(lambda: guard.ensure(max_age_s=0))
    assert helpers.wait_for(lambda: helpers.running["install"] == 1)
    update = _in_thread(lambda: _update(kind, guard, _X))
    try:
        assert helpers.wait_for(lambda: helpers.parked(update) or helpers.running[_X])
    finally:
        helpers.release()
    install.result(5)
    update.result(5)

    assert helpers.overlaps == [], "an update ran beside an install"
    assert helpers.started == ["install", "routes", _X]
    assert steps.kinds()[-2:] == ["long-lived", "long-lived"]
