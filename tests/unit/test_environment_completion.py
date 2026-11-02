"""An environment script's end: its receipt, waited for in the guest, and its last output.

A script that leaves a process holding its output ends before its stream
does, so its end is read from its completion receipt. The receipt is noticed
within a guest check, at one exec per guest wait, and the output the script
wrote last stays its own when the receipt arrives first.
"""

from __future__ import annotations

import os
import queue
import subprocess
import sys
import threading
import time
from concurrent.futures import Future
from types import SimpleNamespace

import pytest

from cloud_agent_host import environment_process
from cloud_agent_host.environment_process import EnvironmentProcess
from cloud_agent_host.sandbox import SandboxError
from cloud_agent_host.setup import (
    FINAL_OUTPUT_CHARS,
    FINAL_OUTPUT_QUIET_S,
    FINAL_OUTPUT_WAIT_S,
    run_setup,
)

#: How long a script's last output takes to arrive once its receipt is read:
#: the tens of milliseconds bytes take to cross a docker or kata exec.
_IN_FLIGHT_S = 0.03
_SECRET = "demo-secret-123456"


def _eventually(check, seconds=5.0):
    deadline = time.monotonic() + seconds
    while not check():
        assert time.monotonic() < deadline
        time.sleep(0.01)


class _Script:
    """An environment script whose receipt is read before its last output arrives.

    The stream yields what the script printed, then, once the receipt has been
    read, ``last`` a moment later: those bytes were still crossing the exec,
    and a kill before they arrive loses them with the helper. Then the stream
    ends with the script, or stays open for a service until the phase's kill:
    one that is silent, chatty, or floods it.
    """

    def __init__(self, *, code, last, service=None):
        self.code = code
        self.last = [last] if isinstance(last, str) else list(last)
        self.service = service
        self.ended = threading.Event()
        self.read = threading.Event()
        self.killed = threading.Event()
        self.read_at = None
        self.service_chunks = 0

    def chunks(self):
        yield "working\n"
        self.ended.set()
        self.read.wait(5)
        if self.killed.wait(_IN_FLIGHT_S):
            return
        yield from self.last
        pause = {"chatty": 0.02, "flood": 0}.get(self.service, 0.5)
        while self.service and not self.killed.wait(pause):
            if self.service != "silent":
                self.service_chunks += 1
                yield "service output\n" if self.service == "chatty" else "x" * 65536

    def lines(self):
        yield from self.chunks()

    def poll(self):
        if not self.ended.is_set():
            return None
        if self.read_at is None:
            self.read_at = time.monotonic()
        self.read.set()
        return self.code

    def wait(self):
        return self.code

    def kill(self):
        self.killed.set()

    def stderr_text(self):
        return ""


class _ScriptBackend:
    def __init__(self, script):
        self.script = script

    def spawn_environment_script(self, *, phase, script, env):
        return self.script


@pytest.fixture
def scripts():
    made = []

    def make(**kwargs):
        made.append(_Script(**kwargs))
        return made[-1]

    yield make
    # Ends the stream of a service the test handed over.
    for script in made:
        script.kill()


def _run(script, phase, output, on_output=None):
    """Run ``script`` as ``phase``; return its result and when it returned after its receipt."""
    finished: Future = Future()

    def run():
        try:
            result = run_setup(
                script="./script",
                workdir="/workspace",
                backend=_ScriptBackend(script),
                timeout_s=60,
                secrets={"TOKEN": _SECRET},
                on_output=on_output or output.append,
                phase=phase,
            )
            finished.set_result((result, time.monotonic() - script.read_at))
        except Exception as exc:
            finished.set_exception(exc)

    # A run that never hands its stream over fails here instead of hanging.
    threading.Thread(target=run, daemon=True).start()
    return finished.result(timeout=10)


@pytest.mark.parametrize(
    ("service", "last", "shown"),
    [
        # The stream ends with the script: it is finished, so even the "["
        # held back in case it begins a mask marker is shown.
        (None, f"listening with {_SECRET} on [", "listening with [secret:TOKEN] on ["),
        ("silent", f"listening with {_SECRET}\n", "listening with [secret:TOKEN]\n"),
    ],
)
def test_a_start_scripts_last_lines_are_kept_when_its_receipt_comes_first(
    scripts, service, last, shown
):
    script = scripts(code=0, last=last, service=service)
    output = []

    result, handed_over = _run(script, "start", output)

    assert result.exit_code == 0
    assert "".join(output) == "working\n" + shown
    # A started service keeps running, and has its stream once it is quiet.
    assert not script.killed.is_set()
    assert handed_over < _IN_FLIGHT_S + FINAL_OUTPUT_QUIET_S + 0.5


def test_a_start_script_with_a_chatty_service_hands_over_within_the_wait(scripts):
    script = scripts(code=0, last="listening\n", service="chatty")
    output = []

    result, handed_over = _run(script, "start", output)

    assert result.exit_code == 0
    assert "working\nlistening\n" in "".join(output)
    assert handed_over < FINAL_OUTPUT_WAIT_S + 0.5
    # The service's pipe keeps flowing, and nothing it writes now is the step's.
    seen, written = len(output), script.service_chunks
    time.sleep(0.3)
    assert script.service_chunks > written + 3
    assert len(output) == seen


def test_a_start_script_whose_service_floods_hands_over_once_enough_is_read(scripts):
    # Output never has to be waited for, so only the amount read ends the reading.
    script = scripts(code=0, last="listening\n", service="flood")
    output = []

    result, _ = _run(script, "start", output)

    assert result.exit_code == 0
    assert "working\nlistening\n" in "".join(output)
    assert FINAL_OUTPUT_CHARS <= sum(map(len, output)) < FINAL_OUTPUT_CHARS + 2 * 65536


def test_a_slow_consumer_does_not_cut_a_scripts_last_lines_short(scripts):
    # Reporting the first of the last lines takes longer than the whole wait,
    # while the second, and the service's output, queue behind it.
    script = scripts(code=0, last=["error: first\n", "error: second\n"], service="chatty")
    output = []

    def report(text):
        output.append(text)
        if "first" in text:
            time.sleep(FINAL_OUTPUT_WAIT_S + 0.3)

    result, _ = _run(script, "start", output, on_output=report)

    assert result.exit_code == 0
    assert "working\nerror: first\nerror: second\n" in "".join(output)


@pytest.mark.parametrize(
    ("phase", "service"),
    [("setup", "silent"), ("update", "chatty"), ("start", "silent"), ("verify", "chatty")],
)
def test_a_failing_scripts_final_lines_are_kept_before_the_kill(scripts, phase, service):
    # A background process holds the stream, so only the receipt says the
    # script ended. Its output ends on what could begin a mask marker.
    script = scripts(code=1, last=f"error: {_SECRET} was refused\n[", service=service)
    output = []

    result, _ = _run(script, phase, output)

    assert result.exit_code == 1
    shown = "".join(output)
    assert "working\nerror: [secret:TOKEN] was refused\n[" in shown
    assert _SECRET not in shown
    assert script.killed.is_set()
    if service == "silent":
        # The kill ended the stream, so even the held "[" is shown.
        assert result.detail.endswith("was refused\n[")


def test_an_idle_stream_is_looked_at_every_check_interval():
    killed = threading.Event()
    polls = []

    class _Idle:
        """A script that prints nothing and runs until it is stopped."""

        def chunks(self):
            killed.wait(5)
            yield from ()

        lines = chunks

        def poll(self):
            polls.append(time.monotonic())

        def wait(self):
            return 137

        def kill(self):
            killed.set()

        def stderr_text(self):
            return ""

    stopped = time.monotonic() + 0.6

    result = run_setup(
        script="./script",
        workdir="/workspace",
        backend=_ScriptBackend(_Idle()),
        stop=lambda: time.monotonic() >= stopped,
        phase="setup",
    )

    assert result.exit_code == 130
    # A receipt the guest saw waits for the loop's next look, so the loop
    # looks about as often as the guest checks, every 50 ms: at least six
    # times here, where a quarter-second tick would look three times.
    assert len(polls) >= 6


class _Helper:
    """The helper's exec: a stream the test feeds until the helper ends or is reaped."""

    def __init__(self):
        self._stream = queue.Queue()
        self._ended = threading.Event()
        self._lock = threading.Lock()
        self.code = None

    def say(self, text):
        self._stream.put(text)

    def end(self, code):
        with self._lock:
            if self._ended.is_set():
                return
            self.code = code
            self._ended.set()
        self._stream.put(None)

    def chunks(self):
        while (chunk := self._stream.get()) is not None:
            yield chunk

    def lines(self):
        yield from self.chunks()

    def poll(self):
        return self.code

    def wait(self):
        self._ended.wait(10)
        return self.code

    def stderr_text(self):
        return ""


class _Sandbox:
    """What an environment process needs of its backend: one helper and its identity's execs."""

    spec = None

    def __init__(self):
        self.manager = self
        self.backend = SimpleNamespace(gid=10001)
        self.helper = _Helper()
        self.calls = []
        self.reaps = 0

    def base_env(self):
        return {}

    def spawn(self, _spec, _argv, **_kwargs):
        return self.helper

    def spawn_environment_script(self, *, phase, script, env):
        return EnvironmentProcess(self, phase=phase, script=script, env=env)

    def exec_capture(self, _spec, argv, **kwargs):
        if argv[3] == environment_process._PREPARE:
            return SimpleNamespace(returncode=0, stdout=b"")
        if argv[3] == environment_process._REAP:
            self.reaps += 1
            self.reap()
            return SimpleNamespace(returncode=0, stdout=b"")
        self.calls.append((argv, kwargs))
        return self.guest_wait(float(argv[5]))

    def guest_wait(self, limit):
        raise NotImplementedError

    def reap(self):
        self.helper.end(-9)

    def limits(self):
        return [argv[5] for argv, _kwargs in self.calls]


class _Guest(_Sandbox):
    """The guest on this machine: each wait runs the real wait script on a receipt here.

    The reap kills only the waits still running and ends the helper: the real
    one would kill every process of the identity on this machine.
    """

    def __init__(self, tmp_path):
        super().__init__()
        self.status = tmp_path / "status"
        self.waits_fail = False
        self.running = set()
        self.most_at_once = 0
        self._lock = threading.Lock()

    def guest_wait(self, limit):
        if self.waits_fail:
            return SimpleNamespace(returncode=137, stdout=b"")
        wait = subprocess.Popen(
            [sys.executable, "-I", "-c", environment_process._WAIT, str(self.status), str(limit)],
            stdout=subprocess.PIPE,
        )
        with self._lock:
            self.running.add(wait)
            self.most_at_once = max(self.most_at_once, len(self.running))
        try:
            stdout, _stderr = wait.communicate(timeout=limit + 10)
        finally:
            with self._lock:
                self.running.discard(wait)
        return SimpleNamespace(returncode=wait.returncode, stdout=stdout)

    def reap(self):
        with self._lock:
            running = list(self.running)
        for wait in running:
            wait.kill()
        super().reap()

    def write_receipt(self, code):
        temporary = self.status.with_name("status.tmp")
        temporary.write_text(str(code))
        os.replace(temporary, self.status)


class _ClockGuest(_Sandbox):
    """A guest on a clock the test turns, whose script ends at ``ends_at``."""

    def __init__(self, ends_at):
        super().__init__()
        self.now = 0.0
        self.ends_at = ends_at
        self._turned = threading.Condition()

    def guest_wait(self, limit):
        with self._turned:
            until = min(self.now + limit, self.ends_at)
            self._turned.wait_for(lambda: self.now >= until, timeout=10)
            if self.now >= self.ends_at:
                return SimpleNamespace(returncode=0, stdout=b"0")
            return SimpleNamespace(returncode=environment_process._NOT_YET, stdout=b"")

    def turn(self, seconds):
        with self._turned:
            self.now += seconds
            self._turned.notify_all()


@pytest.fixture
def guest(tmp_path):
    made = _Guest(tmp_path)
    yield made
    made.reap()


def test_a_guest_wait_runs_isolated_as_the_phase_identity_with_only_its_receipt(guest):
    process = guest.spawn_environment_script(phase="verify", script="make check", env={})

    assert process.poll() is None
    _eventually(lambda: guest.calls)

    ((argv, kwargs),) = guest.calls
    assert argv == [
        "python3",
        "-I",
        "-c",
        environment_process._WAIT,
        "/run/agent-environments/30002/status",
        "30",
    ]
    assert (kwargs["user_uid"], kwargs["user_gid"], kwargs["workdir"]) == (30002, 10001, "/")
    # The wait's limit, and the runner's start beside it.
    assert kwargs["timeout_s"] == 40


def test_completion_is_noticed_within_a_guest_check(guest):
    process = guest.spawn_environment_script(phase="start", script="./serve &", env={})
    assert process.poll() is None
    time.sleep(0.3)

    guest.write_receipt(0)
    written = time.monotonic()
    while (code := process.poll()) is None:
        assert time.monotonic() - written < 5
        time.sleep(0.005)

    assert code == 0
    # One check every 50 ms and the wait's own exit.
    assert time.monotonic() - written < 0.25
    assert guest.limits() == ["30"]


def test_a_long_script_costs_one_exec_per_guest_wait():
    # Ten minutes of a script that holds its output: a read a second was 600 execs.
    guest = _ClockGuest(ends_at=600)
    process = guest.spawn_environment_script(phase="setup", script="./build", env={})

    while (code := process.poll()) is None:
        guest.turn(1)
        time.sleep(0.001)

    assert code == 0
    # One exec per 30 s wait, two a minute, and the one that saw the receipt.
    assert 2 <= len(guest.calls) <= 21


def test_a_guest_wait_that_runs_out_is_followed_by_the_next(guest, monkeypatch):
    monkeypatch.setattr(environment_process, "_WAIT_S", 0.2)
    process = guest.spawn_environment_script(phase="start", script="./serve &", env={})
    waited = time.monotonic() + 2.0
    while time.monotonic() < waited:
        assert process.poll() is None
        time.sleep(0.01)

    guest.write_receipt(3)
    _eventually(lambda: process.poll() is not None)

    assert process.poll() == 3
    # Each started as the one before ran out, one at a time.
    assert 4 <= len(guest.calls) <= 12
    assert set(guest.limits()) == {"0.2"}
    assert guest.most_at_once == 1


def test_a_guest_wait_that_fails_is_not_started_again_at_once(guest):
    # As when the script's own processes kill it.
    guest.waits_fail = True
    process = guest.spawn_environment_script(phase="setup", script="./install", env={})
    for _ in range(20):
        assert process.poll() is None
        time.sleep(0.01)
    assert len(guest.calls) == 1

    time.sleep(environment_process._RETRY_S)

    assert process.poll() is None
    _eventually(lambda: len(guest.calls) == 2)


@pytest.mark.parametrize(
    ("trouble", "error"),
    [("exec", "container is gone"), ("receipt", "Invalid environment completion receipt")],
)
def test_a_guest_wait_that_failed_or_read_no_receipt_raises_as_a_read_would(guest, trouble, error):
    if trouble == "exec":

        def refuse(_limit):
            raise SandboxError("chat sandbox exec failed: container is gone")

        guest.guest_wait = refuse
    else:
        guest.write_receipt("not a number")
    process = guest.spawn_environment_script(phase="start", script="./serve &", env={})

    with pytest.raises(SandboxError, match=error):
        _eventually(lambda: process.poll() is not None, seconds=2)


@pytest.mark.parametrize("by", ["stop", "cancel"])
def test_stop_and_cancel_end_a_script_while_its_guest_wait_is_in_flight(guest, by):
    guest.helper.say("installing\n")
    cancel = threading.Event()
    asked = threading.Event()
    threading.Timer(0.3, (asked if by == "stop" else cancel).set).start()
    started = time.monotonic()

    result = run_setup(
        script="npm ci",
        workdir="/workspace",
        backend=guest,
        stop=asked.is_set,
        cancel=cancel,
        phase="setup",
    )

    assert (result.exit_code, result.detail) == (130, "setup was stopped")
    # Not the 30 s the guest wait could still have taken.
    assert time.monotonic() - started < 3
    # The phase's reap ended the wait too, as the same identity; wait() then read once.
    assert guest.reaps == 1
    assert guest.limits() == ["30", "0"]
    _eventually(lambda: not guest.running)


def test_a_helper_that_ended_without_a_receipt_reports_its_own_failure(guest):
    guest.helper.say("partial output\n")
    threading.Timer(0.3, guest.helper.end, args=(139,)).start()

    result = run_setup(
        script="./install", workdir="/workspace", backend=guest, timeout_s=60, phase="setup"
    )

    assert result.exit_code == 139
    assert "partial output" in result.detail
    # Its guest wait was still waiting for a receipt that never came; the reap ended it.
    assert guest.reaps == 1
    _eventually(lambda: not guest.running)


def test_a_helper_that_exited_cleanly_without_a_receipt_never_reports_success(guest):
    threading.Timer(0.3, guest.helper.end, args=(0,)).start()

    with pytest.raises(SandboxError, match="receipt is missing"):
        run_setup(
            script="./install", workdir="/workspace", backend=guest, timeout_s=60, phase="setup"
        )


def _wait_script(status, limit):
    return subprocess.run(
        [sys.executable, "-I", "-c", environment_process._WAIT, str(status), str(limit)],
        capture_output=True,
        timeout=10,
    )


def test_the_guest_wait_reads_once_and_no_more_than_a_receipt(tmp_path):
    status = tmp_path / "status"
    started = time.monotonic()
    absent = _wait_script(status, 0)
    assert (absent.returncode, absent.stdout) == (environment_process._NOT_YET, b"")
    assert time.monotonic() - started < 5

    status.write_text("7")
    found = _wait_script(status, 0)
    assert (found.returncode, found.stdout) == (0, b"7")

    # A receipt the script's own processes replaced is read only as far as one.
    status.write_text("1" * 100_000)
    assert _wait_script(status, 0).stdout == b"1" * 64
    status.unlink()
    elsewhere = tmp_path / "elsewhere"
    elsewhere.write_text("0")
    status.symlink_to(elsewhere)
    linked = _wait_script(status, 0)
    assert linked.returncode not in (0, environment_process._NOT_YET)
    assert linked.stdout == b""
