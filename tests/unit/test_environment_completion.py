"""An environment script's end: its receipt, waited for in the guest, and its output.

A script that leaves a process holding its output ends before its stream
does, so its end is read from its completion receipt. The receipt is noticed
within a guest check, at one exec per guest wait. It counts the bytes the
script wrote, which its helper forwards first, so the script's output is
split exactly from a service's: a started service gets the stream at that
count, and any other phase is reaped at once without losing a byte.
"""

from __future__ import annotations

import array
import contextlib
import fcntl
import os
import queue
import select
import signal
import subprocess
import sys
import termios
import threading
import time
from concurrent.futures import Future
from types import SimpleNamespace

import pytest

from cloud_agent_host import environment_process
from cloud_agent_host.environment_preparation import run_step
from cloud_agent_host.environment_process import EnvironmentProcess
from cloud_agent_host.sandbox import SandboxError, _PopenProcess
from cloud_agent_host.setup import SCRIPT_OUTPUT_WAIT_S, run_setup

#: How long a script's last output takes to arrive once its receipt is read:
#: the tens of milliseconds bytes take to cross a docker or kata exec.
_IN_FLIGHT_S = 0.03
_SECRET = "demo-secret-123456"
_SERVICE = {"chatty": b"service output\n", "flood": b"x" * 65536}


def _eventually(check, seconds=5.0):
    deadline = time.monotonic() + seconds
    while not check():
        assert time.monotonic() < deadline
        time.sleep(0.01)


class _Script:
    """An environment script whose receipt is read before the last of its output arrives.

    Its stream carries what the script printed, then, once the receipt has
    been read, ``last``: bytes the helper forwarded before writing the
    receipt, still crossing the exec, so a kill no longer stops them. The
    receipt counts all of them. A service may write after them, silent,
    chatty or flooding, until the phase's kill, or on past it when it
    ``escapes`` the reap; ``after`` and its first output share a chunk with the
    script's last bytes.
    """

    def __init__(
        self,
        *,
        code,
        last,
        service=None,
        after=b"",
        in_flight=_IN_FLIGHT_S,
        runs_for=0.0,
        missing=0,
        escapes=False,
    ):
        self.code = code
        self.printed = [b"working\n"]
        pieces = last if isinstance(last, list) else [last]
        self.last = [piece.encode() if isinstance(piece, str) else piece for piece in pieces]
        self.service = service
        self.after = after
        self.escapes = escapes
        self.done = threading.Event()
        self.in_flight = in_flight
        self.runs_for = runs_for
        # The receipt counts bytes that never arrive.
        self.counted = len(self.printed[0]) + sum(map(len, self.last)) + missing
        self.reaped_first = None
        self.script_bytes = None
        self.ended = threading.Event()
        self.read = threading.Event()
        self.killed = threading.Event()
        self.read_at = None
        self.service_chunks = 0
        self.kills = 0

    def byte_chunks(self):
        yield from self.printed
        time.sleep(self.runs_for)
        self.ended.set()
        self.read.wait(5)
        time.sleep(self.in_flight)
        self.reaped_first = self.killed.is_set()
        yield from self.last[:-1]
        if self.killed.is_set() and not self.escapes:
            yield self.last[-1]
            return
        yield self.last[-1] + self.after + _SERVICE.get(self.service, b"")
        pause = {"chatty": 0.02, "flood": 0}.get(self.service, 0.5)
        ends = self.done if self.escapes else self.killed
        while self.service and not ends.wait(pause):
            if self.service != "silent":
                self.service_chunks += 1
                yield _SERVICE[self.service]

    def poll(self):
        if not self.ended.is_set():
            return None
        if self.read_at is None:
            self.read_at = time.monotonic()
            self.script_bytes = self.counted
        self.read.set()
        return self.code

    def wait(self):
        return self.code

    def kill(self):
        self.kills += 1
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
        script.done.set()


def _run(script, phase, output, *, on_output=None, timeout_s=60, stop=None, cancel=None):
    """Run ``script`` as ``phase``; return its result and when it returned after its receipt."""
    finished: Future = Future()

    def run():
        try:
            result = run_setup(
                script="./script",
                workdir="/workspace",
                backend=_ScriptBackend(script),
                timeout_s=timeout_s,
                secrets={"TOKEN": _SECRET},
                on_output=on_output or output.append,
                stop=stop,
                cancel=cancel,
                phase=phase,
            )
            finished.set_result((result, time.monotonic() - script.read_at))
        except Exception as exc:
            finished.set_exception(exc)

    # A run that never hands its stream over fails here instead of hanging.
    threading.Thread(target=run, daemon=True).start()
    return finished.result(timeout=10)


@pytest.mark.parametrize("service", [None, "silent", "chatty", "flood"])
def test_a_start_scripts_output_ends_exactly_where_its_receipt_counts(scripts, service):
    script = scripts(code=0, last=f"listening with {_SECRET}\n", service=service)
    output = []

    result, handed_over = _run(script, "start", output)

    assert result.exit_code == 0
    # A secret wholly inside the count is masked; nothing past it is kept.
    assert "".join(output) == "working\nlistening with [secret:TOKEN]\n"
    # A started service keeps running, and gets its stream once the counted
    # bytes have crossed the exec, not after a second of its own output.
    assert not script.killed.is_set()
    assert handed_over < _IN_FLIGHT_S + 0.25


def test_the_split_counts_raw_bytes_before_decoding_and_masking(scripts):
    # A character cut across two reads, an invalid byte, a secret masked
    # longer than itself and a mark: the count is of bytes as read.
    last = b"\xc3\xa9 \xff " + _SECRET.encode() + b" \xe2\x9c\x93\n"
    script = scripts(code=0, last=last, service="flood")
    script.printed = [b"working h\xc3", b"\xa9llo "]
    script.counted = len(b"working h\xc3\xa9llo ") + len(last)
    output = []

    result, _ = _run(script, "start", output)

    assert result.exit_code == 0
    assert "".join(output) == "working héllo é � [secret:TOKEN] ✓\n"


class _Environment:
    """What run_step reads of a bound environment."""

    def values(self, _which):
        return {}

    def secrets(self):
        return {"TOKEN": _SECRET}


def test_a_secret_split_at_the_count_shows_no_part_of_it(scripts):
    # A block-buffered service wrote the start of a secret before the script
    # ended silently, so the count takes it in; the rest came with its next
    # flush, past the count.
    script = scripts(code=0, last=b"", service="chatty", after=b"cret-123456@db\n")
    script.printed = [b"ready\n", b"service: db=postgres://app:demo-se"]
    script.counted = len(b"ready\nservice: db=postgres://app:demo-se")
    step = {"phase": "start", "state": "running", "output": ""}
    reported = []

    result = run_step(
        _ScriptBackend(script),
        _Environment(),
        step,
        script="./serve",
        values="runtime",
        timeout_s=60,
        on_output=lambda step: reported.append(step["output"]),
        cancel=None,
    )

    assert result.exit_code == 0
    # Not one character of it reaches the durable step output or a report.
    assert step["output"] == "ready\nservice: db=postgres://app:"
    assert reported
    assert all("demo" not in output for output in reported)


def test_a_character_cut_at_the_count_is_shown_as_one_that_could_not_be_read(scripts):
    # The script's last byte begins a character the service's first ends.
    script = scripts(code=0, last=b"caf\xc3", service="chatty", after=b"\xa9 service\n")
    output = []

    result, _ = _run(script, "start", output)

    assert result.exit_code == 0
    assert "".join(output) == "working\ncaf\ufffd"


def test_the_rest_of_a_reaped_phase_is_read_no_longer_than_its_deadline(scripts):
    # A writer the reap did not reach keeps the stream flowing.
    script = scripts(code=1, last="error: it failed\n", service="chatty", escapes=True)
    output = []
    started = time.monotonic()

    result, _ = _run(script, "setup", output, timeout_s=1.0)

    assert result.exit_code == 1
    assert time.monotonic() - started < 1.0 + 0.3
    assert "working\nerror: it failed\n" in "".join(output)


def test_a_start_whose_service_output_was_read_first_hands_over_at_once(scripts):
    # The service wrote after the script's end, and that was read before the
    # receipt was: the stream is past the count already.
    script = scripts(code=0, last=b"", service="chatty", in_flight=0, runs_for=0.2)
    script.printed = [b"working\n", b"listening\n", b"service output\n"]
    script.counted = len(b"working\nlistening\n")
    output = []

    result, handed_over = _run(script, "start", output)

    assert result.exit_code == 0
    # What was read before the receipt stays the step's; nothing after it is.
    assert "".join(output) == "working\nlistening\nservice output\n"
    assert handed_over < 0.2


def test_a_start_whose_counted_bytes_never_arrive_hands_over_after_its_wait(scripts):
    script = scripts(code=0, last="listening\n", service="silent", missing=10)
    output = []

    result, handed_over = _run(script, "start", output)

    assert result.exit_code == 0
    assert "".join(output) == "working\nlistening\n"
    assert SCRIPT_OUTPUT_WAIT_S - 0.1 < handed_over < SCRIPT_OUTPUT_WAIT_S + 0.5


def test_a_start_whose_stream_ends_short_of_its_count_is_finished(scripts):
    # No service holds the stream, and it ends before the count: it is over,
    # so even the "[" held back for a mask marker is shown, and at once.
    script = scripts(code=0, last="listening on [", missing=10)
    output = []

    result, handed_over = _run(script, "start", output)

    assert result.exit_code == 0
    assert "".join(output) == "working\nlistening on ["
    assert handed_over < 0.25


def test_a_slow_report_does_not_cut_a_starts_counted_bytes_short(scripts):
    # Reporting the first of the counted bytes takes longer than their whole
    # wait, while the rest, and the service's output, queue behind them.
    script = scripts(code=0, last=["error: first\n", "error: second\n"], service="chatty")
    output = []

    def report(text):
        output.append(text)
        if "first" in text:
            time.sleep(SCRIPT_OUTPUT_WAIT_S + 0.3)

    result, _ = _run(script, "start", output, on_output=report)

    assert result.exit_code == 0
    assert "".join(output) == "working\nerror: first\nerror: second\n"


@pytest.mark.parametrize("by", ["deadline", "stop", "cancel"])
def test_the_deadline_or_a_stop_ends_the_hand_over(scripts, by):
    # The counted bytes take longer to arrive than the deadline or the stop.
    script = scripts(code=0, last="listening\n", service="chatty", in_flight=1.5, runs_for=0.3)
    output = []
    stopped, cancel = threading.Event(), threading.Event()
    if by != "deadline":
        script_read = script.read

        def stop_soon():
            script_read.wait(5)
            time.sleep(0.1)
            (stopped if by == "stop" else cancel).set()

        threading.Thread(target=stop_soon, daemon=True).start()
    started = time.monotonic()

    result, handed_over = _run(
        script,
        "start",
        output,
        timeout_s=0.5 if by == "deadline" else 60,
        stop=stopped.is_set,
        cancel=cancel,
    )

    # The script succeeded before either came: its result stands, and the
    # service it started keeps the stream.
    assert result.exit_code == 0
    assert "".join(output) == "working\n"
    assert not script.killed.is_set()
    if by == "deadline":
        assert time.monotonic() - started < 0.5 + 0.2
    else:
        assert 0.05 < handed_over < 0.1 + 0.2


def test_a_report_that_fails_during_a_start_hand_over_leaves_the_service_drained(scripts):
    script = scripts(code=0, last="listening\n", service="chatty")

    def report(text):
        if "listening" in text:
            raise RuntimeError("control plane answered 503")

    with pytest.raises(RuntimeError, match="503"):
        _run(script, "start", [], on_output=report)

    # The service runs on, and its pipe keeps flowing.
    assert not script.killed.is_set()
    written = script.service_chunks
    time.sleep(0.3)
    assert script.service_chunks > written + 3


def test_a_report_that_fails_after_a_failed_phase_leaves_it_reaped(scripts):
    script = scripts(code=1, last="error: the build failed\n", service="chatty")

    def report(text):
        if "error:" in text:
            raise RuntimeError("control plane answered 503")

    with pytest.raises(RuntimeError, match="503"):
        _run(script, "setup", [], on_output=report)

    assert script.killed.is_set()


@pytest.mark.parametrize(
    ("phase", "service"),
    [("setup", "silent"), ("update", "chatty"), ("start", "chatty"), ("verify", "flood")],
)
def test_a_failing_scripts_last_lines_survive_a_process_it_left_writing(scripts, phase, service):
    # The phase is reaped as soon as the receipt is read; what the script
    # wrote was already in the stream, and the leftover writes nothing more.
    script = scripts(code=1, last=f"error: {_SECRET} was refused\n[", service=service)
    output = []

    result, _ = _run(script, phase, output)

    assert result.exit_code == 1
    # Reaped once, as soon as the receipt came and before the script's last
    # bytes had crossed; nothing of the identity is left after.
    assert script.kills == 1
    assert script.reaped_first is True
    assert "".join(output) == "working\nerror: [secret:TOKEN] was refused\n["
    assert result.detail == "working\nerror: [secret:TOKEN] was refused\n["


def test_an_idle_stream_is_looked_at_every_check_interval():
    killed = threading.Event()
    polls = []

    class _Idle:
        """A script that prints nothing and runs until it is stopped."""

        def byte_chunks(self):
            killed.wait(5)
            yield from ()

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


@pytest.mark.parametrize(
    ("receipt", "read"),
    [(b"0 12", (0, 12)), (b"-9 0", (-9, 0)), (b"1 9999999999", (1, 9999999999))],
)
def test_a_receipt_records_the_exit_status_and_the_scripts_byte_count(receipt, read):
    process = EnvironmentProcess.__new__(EnvironmentProcess)

    process._take(receipt)

    assert (process._code, process.script_bytes) == read


@pytest.mark.parametrize(
    "receipt",
    [
        *(b"", b"0", b"0 ", b" 0 1", b"0  1", b"0 1\n", b"0 -1", b"0 1 2", b"x 1", b"0 0x1"),
        *(b"0 1_0", b"0 " + b"9" * 20, b"-" + b"1" * 11 + b" 0"),
    ],
)
def test_a_malformed_receipt_is_refused(receipt):
    process = EnvironmentProcess.__new__(EnvironmentProcess)

    with pytest.raises(SandboxError, match="Invalid environment completion receipt"):
        process._take(receipt)


def _fionread(fd):
    waiting = array.array("i", [0])
    fcntl.ioctl(fd, termios.FIONREAD, waiting)
    return waiting[0]


def test_the_helper_forwards_what_the_script_wrote_before_its_receipt_and_counts_it(tmp_path):
    status = tmp_path / "status"
    # More than the pipes hold, written just before the end, then a writer
    # that starts after it.
    script = (
        "head -c 307200 /dev/zero | tr '\\000' a; "
        "(sleep 0.5; while :; do printf b; sleep 0.01; done) &"
    )
    helper = subprocess.Popen(
        [sys.executable, "-I", "-u", "-c", environment_process._HELPER],
        env={
            "PATH": os.environ["PATH"],
            "AGENT_SCRIPT_STATUS": str(status),
            "AGENT_ENVIRONMENT_SCRIPT": script,
        },
        cwd=tmp_path,
        stdout=subprocess.PIPE,
        start_new_session=True,
    )
    fd = helper.stdout.fileno()
    taken = bytearray()
    lock = threading.Lock()
    done = threading.Event()

    def read_slowly():
        while not done.is_set():
            if select.select([fd], [], [], 0.05)[0]:
                with lock:
                    data = os.read(fd, 4096)
                    taken.extend(data)
                if not data:
                    return
                time.sleep(0.005)

    reader = threading.Thread(target=read_slowly, daemon=True)
    reader.start()
    try:
        _eventually(status.exists, seconds=20)
        with lock:
            forwarded = len(taken) + _fionread(fd)
        code, count = status.read_text().split()
        # Every byte the script wrote was in the stream when the receipt
        # appeared, and the receipt counts exactly those.
        assert (code, count) == ("0", "307200")
        assert forwarded >= 307200
        _eventually(lambda: len(taken) > 307200, seconds=20)
        assert bytes(taken[:307200]) == b"a" * 307200
        assert set(bytes(taken[307200:])) == {ord("b")}
    finally:
        done.set()
        os.killpg(helper.pid, signal.SIGKILL)
        reader.join(5)
        helper.wait()
        helper.stdout.close()


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

    def byte_chunks(self):
        while (chunk := self._stream.get()) is not None:
            yield chunk.encode()

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
        self.started = []
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
        self.started.append(time.monotonic())
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
        self.ends_at_once = None
        self.running = set()
        self.most_at_once = 0
        self._lock = threading.Lock()

    def guest_wait(self, limit):
        if self.ends_at_once is not None:
            return SimpleNamespace(returncode=self.ends_at_once, stdout=b"")
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

    def write_receipt(self, receipt):
        temporary = self.status.with_name("status.tmp")
        temporary.write_text(receipt)
        os.replace(temporary, self.status)


class _Local(_Guest):
    """The guest on this machine running the real helper, its script as this user."""

    def __init__(self, tmp_path):
        super().__init__(tmp_path)
        self.workdir = tmp_path
        self.popen = None

    def base_env(self):
        return {"PATH": os.environ["PATH"]}

    def spawn(self, _spec, argv, *, env, **_kwargs):
        self.popen = subprocess.Popen(
            [sys.executable, *argv[1:]],
            env={**env, "AGENT_SCRIPT_STATUS": str(self.status)},
            cwd=self.workdir,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            start_new_session=True,
        )
        return _PopenProcess(self.popen)

    def reap(self):
        if self.popen is not None:
            with contextlib.suppress(ProcessLookupError):
                os.killpg(self.popen.pid, signal.SIGKILL)
        with self._lock:
            running = list(self.running)
        for wait in running:
            wait.kill()


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
                return SimpleNamespace(returncode=0, stdout=b"0 0")
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


@pytest.fixture
def local(tmp_path):
    made = _Local(tmp_path)
    yield made
    made.reap()


def test_a_start_with_the_real_helper_hands_over_at_its_last_line(local):
    script = "(while :; do echo svc; sleep 0.02; done) & sleep 0.3; echo last-line"
    output = []

    result = run_setup(
        script=script,
        workdir="/workspace",
        backend=local,
        timeout_s=30,
        on_output=output.append,
        phase="start",
    )
    returned = time.time()

    assert result.exit_code == 0
    shown = "".join(output)
    # The service's lines written while the script ran are counted with it;
    # what it writes after the end is drained, but what arrived before the
    # receipt did.
    assert "last-line\n" in shown
    assert shown.split("last-line\n", 1)[1].count("svc\n") <= 8
    assert returned - local.status.stat().st_mtime < 0.5


def test_a_failing_scripts_error_survives_a_chatty_leftover_with_the_real_helper(local):
    script = (
        '(while :; do echo "server: GET /healthz 200 0.4ms from 127.0.0.1"; sleep 0.05; done) & '
        "sleep 0.3; echo error: build failed; exit 1"
    )
    output = []

    result = run_setup(
        script=script,
        workdir="/workspace",
        backend=local,
        timeout_s=30,
        on_output=output.append,
        phase="setup",
    )

    assert result.exit_code == 1
    assert "error: build failed" in result.detail


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

    guest.write_receipt("0 42")
    written = time.monotonic()
    while (code := process.poll()) is None:
        assert time.monotonic() - written < 5
        time.sleep(0.005)

    assert (code, process.script_bytes) == (0, 42)
    # One check every 50 ms and the wait's own exit.
    assert time.monotonic() - written < 0.25
    assert guest.limits() == ["30"]


def test_a_long_script_costs_one_exec_per_guest_wait(monkeypatch):
    # Ten minutes of a script that holds its output: a read a second was 600
    # execs. The floor between starts keeps real time; this clock is the test's.
    monkeypatch.setattr(environment_process, "_RETRY_S", 0)
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
    monkeypatch.setattr(environment_process, "_RETRY_S", 0.05)
    process = guest.spawn_environment_script(phase="start", script="./serve &", env={})
    waited = time.monotonic() + 2.0
    while time.monotonic() < waited:
        assert process.poll() is None
        time.sleep(0.01)

    guest.write_receipt("3 0")
    _eventually(lambda: process.poll() is not None)

    assert process.poll() == 3
    # Each started as the one before ran out, one at a time.
    assert 4 <= len(guest.calls) <= 12
    assert set(guest.limits()) == {"0.2"}
    assert guest.most_at_once == 1


@pytest.mark.parametrize("ending", [137, environment_process._NOT_YET])
def test_guest_waits_that_end_at_once_start_no_closer_than_the_floor(guest, ending):
    # As when the script's own processes kill each wait, or one finds a clock
    # that makes it exit at once.
    guest.ends_at_once = ending
    process = guest.spawn_environment_script(phase="setup", script="./install", env={})
    began = time.monotonic()
    while time.monotonic() - began < 2.5:
        assert process.poll() is None
        time.sleep(0.01)

    # Started at about 0, 1 and 2 s: at most an exec a second.
    assert 2 <= len(guest.started) <= 3
    gaps = [
        later - earlier for earlier, later in zip(guest.started, guest.started[1:], strict=False)
    ]
    assert min(gaps) >= environment_process._RETRY_S - 0.05


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
        guest.write_receipt("0")
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

    status.write_text("7 12")
    found = _wait_script(status, 0)
    assert (found.returncode, found.stdout) == (0, b"7 12")

    # A receipt the script's own processes replaced is read only as far as one.
    status.write_text("1" * 100_000)
    assert _wait_script(status, 0).stdout == b"1" * 64
    status.unlink()
    elsewhere = tmp_path / "elsewhere"
    elsewhere.write_text("0 0")
    status.symlink_to(elsewhere)
    linked = _wait_script(status, 0)
    assert linked.returncode not in (0, environment_process._NOT_YET)
    assert linked.stdout == b""
