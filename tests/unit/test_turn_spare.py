"""A spare turn child: started before its claim, then handed the turn as a cold child is.

What the spare must not change: the turn, and every credential in it, still
travels on the child's standard input only, and the lease owner name with
it, set by the host; a spare that exited or wrote before its turn is never
given one, and a cold child takes the turn; a failed write to a live spare
is as ambiguous as one to a cold child; a spare retired without a turn never
runs the probe the deployment canary relies on; and a host that keeps no
spare starts its children exactly as before.
"""

from __future__ import annotations

import contextlib
import errno
import io
import json
import logging
import os
import subprocess
import sys
from types import SimpleNamespace
from typing import Any

import pytest
from tests.unit.test_runner_host_service import (
    FakeHandle,
    _delivered_job,
    _job,
    service as service,
)

from cloud_agent_host import runner as runner_mod, turn_launch
from cloud_agent_host.host_runtime import (
    LAUNCH_NOT_STARTED,
    LAUNCH_STARTED,
    LAUNCH_UNCERTAIN,
    LaunchResult,
    encode_turn,
)
from cloud_agent_host.sandbox import ProcessBackend

#: This host's configuration, which every child's command line carries.
_CONFIG: dict[str, Any] = {
    "base_url": "http://control",
    "gateway_url": "http://gateway",
    "lease_ttl": 120.0,
    "agent_timeout_s": 3600.0,
    "host": "host-a",
}


def _argv(handover_fd: str, worker_id: str) -> list[str]:
    """A turn child's command line as the host service has always written it."""
    return [
        sys.executable,
        "-m",
        "cloud_agent_host.runner",
        "--turn",
        "--handover-fd",
        handover_fd,
        "--base-url",
        "http://control",
        "--gateway-url",
        "http://gateway",
        "--worker-id",
        worker_id,
        "--host",
        "host-a",
        "--lease-ttl",
        "120.0",
        "--agent-timeout",
        "3600.0",
    ]


def _spare() -> turn_launch.TurnSpare:
    return turn_launch.TurnSpare(worker_id="runner-a", **_CONFIG)


def _launch(
    payload: dict[str, Any],
    *,
    spare: turn_launch.TurnSpare | None = None,
    worker_id: str = "runner-a-3",
) -> LaunchResult:
    """Hand a turn over through the runner's own entry point and the real handover pipe."""
    return runner_mod._spawn_turn(payload, worker_id=worker_id, spare=spare, **_CONFIG)


# ── Children, as their host sees them ──────────────────────────────────


class _Stdin(io.BytesIO):
    """A child's standard input. Closing it is the end of what the host delivers."""

    def __init__(self, child: FakeChild, *, broken: bool) -> None:
        super().__init__()
        self._child = child
        self._broken = broken

    def write(self, data: Any) -> int:
        if self._broken:
            raise BrokenPipeError(errno.EPIPE, "Broken pipe")
        return super().write(data)

    def close(self) -> None:
        if not self.closed:
            data = self.getvalue()
            super().close()
            self._child.read_input(data)


class FakeChild:
    """A turn child: its input, its own copy of the handover descriptor, and its exit.

    ``answer`` is what it says when handed a turn; any answer but an
    acceptance is followed by its exit, as a declining child's is. At
    end-of-file without a turn it exits, unless ``exits_on_eof`` is false.
    """

    def __init__(
        self,
        argv: list[str],
        pass_fds: tuple[int, ...],
        *,
        answer: str = "accepted",
        exits_on_eof: bool = True,
        broken_stdin: bool = False,
    ) -> None:
        self.argv = list(argv)
        # Held for as long as it runs, as a real child holds its descriptor.
        self.handover = os.dup(pass_fds[0])
        self.stdin = _Stdin(self, broken=broken_stdin)
        self.pid = 40000 + self.handover
        self.returncode: int | None = None
        self.answer = answer
        self.exits_on_eof = exits_on_eof
        self.delivered: dict[str, Any] | None = None
        self.killed = False

    def read_input(self, data: bytes) -> None:
        if not data:
            if self.exits_on_eof:
                self.exit(0)
            return
        self.delivered = json.loads(data)
        self.speak(f"{self.answer} {self.delivered['job_id']}")
        if not self.answer.startswith("accepted"):
            self.exit(0)

    def speak(self, line: str) -> None:
        os.write(self.handover, f"{line}\n".encode())

    def exit(self, code: int) -> None:
        if self.returncode is None:
            self.returncode = code
            os.close(self.handover)

    def poll(self) -> int | None:
        return self.returncode

    def wait(self, timeout: float | None = None) -> int:
        if self.returncode is None:
            raise subprocess.TimeoutExpired("turn", timeout or 0)
        return self.returncode

    def kill(self) -> None:
        self.killed = True
        self.exit(-9)

    def terminate(self) -> None:
        self.exit(-15)


@pytest.fixture
def children(monkeypatch):
    """Every child started, as a :class:`FakeChild`; queue options to shape the next."""
    started: list[FakeChild] = []
    upcoming: list[dict[str, Any] | BaseException] = []

    def popen(argv: list[str], *, stdin: int, pass_fds: tuple[int, ...]) -> FakeChild:
        assert stdin == subprocess.PIPE
        shape = upcoming.pop(0) if upcoming else {}
        if isinstance(shape, BaseException):
            raise shape
        child = FakeChild(argv, pass_fds, **shape)
        started.append(child)
        return child

    monkeypatch.setattr(subprocess, "Popen", popen)
    yield SimpleNamespace(started=started, upcoming=upcoming)
    for child in started:
        child.exit(0)


# ── The host's side ────────────────────────────────────────────────────


def test_a_spare_waits_with_this_hosts_configuration_and_no_turn(children):
    spare = _spare()
    spare.replenish()

    (waiting,) = children.started
    handover_fd = waiting.argv[waiting.argv.index("--handover-fd") + 1]
    # The host's own worker id: a turn's travels with the turn.
    assert waiting.argv == [*_argv(handover_fd, "runner-a"), "--spare"]
    assert waiting.delivered is None
    assert not waiting.stdin.closed
    # This process's copy of the spare's descriptor is closed, so that the
    # spare's exit reads as end-of-file rather than as silence.
    with pytest.raises(OSError):
        os.fstat(int(handover_fd))
    spare.close()


def test_a_claim_hands_its_turn_to_the_waiting_spare(children):
    spare = _spare()
    spare.replenish()
    (waiting,) = children.started

    result = _launch(_delivered_job(), spare=spare)

    assert result.outcome == LAUNCH_STARTED
    assert result.handle is waiting
    assert waiting.delivered is not None
    assert waiting.delivered["worker_token"] == "ajt.1"
    assert waiting.delivered["_worker_id"] == "runner-a-3"
    # No cold child: the next spare, started once this handover was over.
    assert len(children.started) == 2
    assert children.started[1].argv[-1] == "--spare"
    assert children.started[1].delivered is None
    for child in children.started:
        assert "ajt.1" not in child.argv
        assert "grant" not in child.argv
    spare.close()


@pytest.mark.parametrize("gone", ["exited", "wrote"])
def test_a_spare_that_exited_or_wrote_before_its_turn_is_never_given_one(children, caplog, gone):
    """It never had the turn, so the turn goes to a cold child rather than back to the queue."""
    spare = _spare()
    spare.replenish()
    (stale,) = children.started
    if gone == "exited":
        stale.exit(1)
    else:
        stale.speak("accepted ajob_0")

    with caplog.at_level(logging.WARNING):
        result = _launch(_delivered_job(), spare=spare)

    stale_spare, cold, fresh = children.started
    assert stale_spare is stale
    assert stale.delivered is None, "never handed the turn"
    assert stale.returncode is not None, "collected"
    assert result.outcome == LAUNCH_STARTED
    assert result.handle is cold
    handover_fd = cold.argv[cold.argv.index("--handover-fd") + 1]
    assert cold.argv == _argv(handover_fd, "runner-a-3")
    assert cold.delivered is not None
    assert cold.delivered["_worker_id"] == "runner-a-3"
    assert fresh.argv[-1] == "--spare"
    assert "agent_turn_spare_lost" in caplog.text
    spare.close()


def test_a_failed_write_to_a_live_spare_stays_as_ambiguous_as_for_a_cold_child(children):
    """Once a live spare's input is being written, the failure proves nothing."""
    children.upcoming.append({"broken_stdin": True})
    spare = _spare()
    spare.replenish()
    (waiting,) = children.started

    result = _launch(_delivered_job(), spare=spare)

    assert result.outcome == LAUNCH_UNCERTAIN
    assert result.handle is waiting, "tracked, and waited for, like any child that may hold it"
    assert "handover failed (BrokenPipeError)" in result.detail
    assert not waiting.killed
    # No cold child takes a turn that may already be taken.
    assert [child.argv[-1] for child in children.started] == ["--spare", "--spare"]
    spare.close()


def test_a_spare_that_declines_its_turn_hands_it_back_unspent(children):
    children.upcoming.append({"answer": "declined docker is not usable"})
    spare = _spare()
    spare.replenish()
    (waiting,) = children.started

    result = _launch(_delivered_job(), spare=spare)

    assert result.outcome == LAUNCH_NOT_STARTED
    assert result.detail.startswith("declined docker is not usable")
    assert result.handle is None
    assert waiting.returncode == 0, "collected"
    spare.close()


@pytest.mark.parametrize("with_spare", [False, True])
def test_a_child_runs_under_the_worker_id_its_host_set_never_the_claims(children, with_spare):
    spare = _spare() if with_spare else None
    if spare is not None:
        spare.replenish()

    _launch({**_delivered_job(), "_worker_id": "forged"}, spare=spare, worker_id="runner-a-3")

    assert children.started[0].delivered is not None
    assert children.started[0].delivered["_worker_id"] == "runner-a-3"
    if spare is not None:
        spare.close()


def test_without_a_spare_a_turns_child_starts_exactly_as_before(children):
    result = _launch(_delivered_job())

    (child,) = children.started
    handover_fd = child.argv[child.argv.index("--handover-fd") + 1]
    assert child.argv == _argv(handover_fd, "runner-a-3")
    assert result.outcome == LAUNCH_STARTED
    assert result.handle is child


@pytest.mark.parametrize(
    ("exits_on_eof", "immediate", "killed"),
    [
        pytest.param(True, False, False, id="exits-at-end-of-file"),
        pytest.param(False, False, True, id="stays-and-is-killed"),
        pytest.param(True, True, True, id="ctrl-c"),
    ],
)
def test_retiring_a_spare_ends_its_input_and_kills_only_one_that_stays(
    children, exits_on_eof, immediate, killed
):
    children.upcoming.append({"exits_on_eof": exits_on_eof})
    spare = _spare()
    spare.replenish()
    (waiting,) = children.started

    spare.close(immediate=immediate)

    assert waiting.stdin.closed
    assert waiting.delivered is None, "retired with nothing on its input"
    assert waiting.returncode is not None
    assert waiting.killed is killed
    spare.replenish()
    assert len(children.started) == 1, "a host that has closed its spare starts no other"


def test_a_host_that_cannot_start_a_spare_starts_cold_children(children, caplog):
    children.upcoming.append(OSError(errno.EAGAIN, "Resource temporarily unavailable"))
    spare = _spare()

    with caplog.at_level(logging.WARNING):
        spare.replenish()

    assert children.started == []
    assert "agent_turn_spare_unstarted" in caplog.text
    result = _launch(_delivered_job(), spare=spare)
    cold = children.started[0]
    assert result.handle is cold
    assert "--spare" not in cold.argv
    spare.close()


def test_a_stopping_host_starts_no_spare(children):
    runner_mod._STOPPING.set()
    try:
        _spare().replenish()
    finally:
        runner_mod._STOPPING.clear()

    assert children.started == []


def test_ctrl_c_during_a_spares_handover_kills_it_and_starts_no_other(children, monkeypatch):
    spare = _spare()
    spare.replenish()
    (waiting,) = children.started

    def interrupted(fd: int) -> str:
        os.close(fd)
        raise KeyboardInterrupt

    monkeypatch.setattr(runner_mod, "read_handover", interrupted)
    with pytest.raises(KeyboardInterrupt):
        _launch(_delivered_job(), spare=spare)

    assert waiting.killed
    assert len(children.started) == 1


def test_ctrl_c_while_the_next_spare_starts_stops_the_child_with_the_turn(children):
    """It has its turn, but TurnPool has not received it yet: left running, it would be lost."""
    spare = _spare()
    spare.replenish()
    (waiting,) = children.started
    children.upcoming.append(KeyboardInterrupt())

    with pytest.raises(KeyboardInterrupt):
        _launch(_delivered_job(), spare=spare)

    assert waiting.delivered is not None, "it had accepted the turn"
    assert waiting.killed


# ── The child's side ───────────────────────────────────────────────────


def _serve(monkeypatch, raw: str, *, spare: bool) -> tuple[int, bytes]:
    """Run a turn's child on ``raw``; its exit status and everything it answered."""
    read_fd, write_fd = os.pipe()
    pipe = os.fstat(write_fd).st_ino
    monkeypatch.setattr("sys.stdin", io.StringIO(raw))
    try:
        code = runner_mod._serve_turn(
            handover_fd=write_fd,
            base_url="http://control",
            gateway_url="http://gateway",
            worker_id="runner-a",
            lease_ttl=120.0,
            agent_timeout_s=3600.0,
            host="host-a",
            spare=spare,
        )
    finally:
        # An answer closes the descriptor; a spare retired without one leaves
        # it to its exit. Closed here only while it is still this pipe's.
        with contextlib.suppress(OSError):
            if os.fstat(write_fd).st_ino == pipe:
                os.close(write_fd)
    with os.fdopen(read_fd, "rb") as answers:
        return code, answers.read()


def test_a_spare_retired_without_a_turn_exits_without_the_probe(monkeypatch):
    """End-of-file is how its host retires it. The canary's probe is not its to run."""
    warmed: list[bool] = []
    monkeypatch.setattr(turn_launch, "_warm_http_stack", lambda: warmed.append(True))
    monkeypatch.setattr(
        runner_mod, "build_backend_from_env", lambda: pytest.fail("a retired spare probed")
    )

    assert _serve(monkeypatch, "", spare=True) == (0, b"")
    assert warmed == [True]


def test_without_spare_mode_empty_input_still_runs_the_full_probe(monkeypatch):
    """The deployment canary: a turn process on empty input proves it could run a turn."""
    probed: list[str] = []
    backend = ProcessBackend(acknowledged_unsafe=True)
    monkeypatch.setattr(backend, "preflight", lambda **_kw: probed.append("full"))
    monkeypatch.setattr(runner_mod, "build_backend_from_env", lambda: backend)
    monkeypatch.setattr(
        turn_launch, "_warm_http_stack", lambda: pytest.fail("a cold child warmed up")
    )

    code, answer = _serve(monkeypatch, "", spare=False)

    assert code == 0
    assert answer.decode().startswith("declined no turn was delivered")
    assert probed == ["full"]


@pytest.mark.parametrize("raw", ["{", "[]", " \n"])
def test_a_spare_handed_anything_but_end_of_file_answers_as_a_cold_child(monkeypatch, raw):
    """Only end-of-file before a single byte is a retirement; the rest is probed and declined."""
    probed: list[str] = []
    backend = ProcessBackend(acknowledged_unsafe=True)
    monkeypatch.setattr(backend, "preflight", lambda **_kw: probed.append("full"))
    monkeypatch.setattr(runner_mod, "build_backend_from_env", lambda: backend)
    monkeypatch.setattr(turn_launch, "_warm_http_stack", lambda: None)

    code, answer = _serve(monkeypatch, raw, spare=True)

    assert code == 0
    assert answer.decode().startswith("declined no turn was delivered")
    assert probed == ["full"]


@pytest.mark.parametrize("spare", [False, True])
def test_a_child_runs_its_turn_under_the_worker_id_in_the_turn(monkeypatch, spare):
    backend = ProcessBackend(acknowledged_unsafe=True)
    monkeypatch.setattr(runner_mod, "build_backend_from_env", lambda: backend)
    monkeypatch.setattr(turn_launch, "_warm_http_stack", lambda: None)
    ran: list[dict[str, Any]] = []
    monkeypatch.setattr(runner_mod, "run_turn", lambda _job, **kwargs: ran.append(kwargs) or 0)
    payload = {**_delivered_job(), "_worker_id": "runner-a-7"}

    code, answer = _serve(monkeypatch, encode_turn(payload).decode(), spare=spare)

    assert code == 0
    assert answer.decode().startswith("accepted ajob_1")
    assert [kwargs["worker_id"] for kwargs in ran] == ["runner-a-7"]


@pytest.mark.parametrize(
    ("handed", "expected"),
    [
        ("runner-a-7", "runner-a-7"),
        (None, "runner-a"),
        ("", "runner-a"),
        ("  ", "runner-a"),
        (7, "runner-a"),
    ],
)
def test_a_turn_handed_no_usable_worker_id_keeps_the_command_lines(handed, expected):
    payload = _job(1) if handed is None else {**_job(1), "_worker_id": handed}

    assert turn_launch.handed_worker_id(payload, "runner-a") == expected


def test_the_warm_up_keeps_nothing_and_never_fails(monkeypatch):
    def unusable(**_kwargs: Any) -> Any:
        raise ValueError("a proxy variable the turn's own client will refuse")

    monkeypatch.setattr(turn_launch.httpx, "Client", unusable)

    turn_launch._warm_http_stack()


# ── The command line ───────────────────────────────────────────────────


def test_the_cli_starts_a_spare_only_as_a_turn_process(monkeypatch):
    parser = runner_mod.build_parser()
    assert parser.parse_args([]).spare is False
    assert parser.parse_args(["--turn", "--spare", "--handover-fd", "3"]).spare is True
    assert "--spare" not in parser.format_help()

    served: list[dict[str, Any]] = []
    monkeypatch.setenv("AGENT_AUTH_MODE", "gateway")
    monkeypatch.setenv("AGENT_CONTROL_PLANE_URL", "http://control")
    monkeypatch.setenv("AGENT_GATEWAY_URL", "http://gateway")
    monkeypatch.setattr(runner_mod, "configure_process_logging", lambda: None)
    monkeypatch.setattr(runner_mod, "_serve_turn", lambda **kwargs: served.append(kwargs) or 0)
    monkeypatch.setattr(
        runner_mod, "run_forever", lambda **_kwargs: pytest.fail("a spare served the host")
    )

    runner_mod.main(["--turn", "--spare", "--handover-fd", "9", "--host", "host-a"])
    runner_mod.main(["--turn", "--handover-fd", "9", "--host", "host-a"])
    with pytest.raises(SystemExit):
        runner_mod.main(["--spare", "--host", "host-a"])

    assert [kwargs["spare"] for kwargs in served] == [True, False]


# ── The host service ───────────────────────────────────────────────────


class _RecordedSpare:
    """What ``run_forever`` does with its spare: builds it, hands it on, closes it."""

    def __init__(self, made: list[_RecordedSpare], **kwargs: Any) -> None:
        self.kwargs = kwargs
        self.closed: list[bool] = []
        made.append(self)

    def close(self, *, immediate: bool = False) -> None:
        self.closed.append(immediate)


@pytest.mark.parametrize("stop", ["drain", "ctrl-c"])
@pytest.mark.parametrize("mode,gateway_url", [("gateway", "http://gateway"), ("local", "")])
def test_the_service_hands_its_spare_to_each_launch_and_retires_it(
    service, monkeypatch, stop, mode, gateway_url
):
    _identity, backend, _released = service
    monkeypatch.setenv("AGENT_AUTH_MODE", mode)
    monkeypatch.delenv("AGENT_GATEWAY_URL", raising=False)
    made: list[_RecordedSpare] = []
    monkeypatch.setattr(runner_mod, "TurnSpare", lambda **kwargs: _RecordedSpare(made, **kwargs))
    handed: list[Any] = []

    def spawn(_payload: dict[str, Any], **kwargs: Any) -> LaunchResult:
        assert kwargs["gateway_url"] == gateway_url
        handed.append(kwargs["spare"])
        return LaunchResult(LAUNCH_STARTED, FakeHandle())

    def claim(**_kwargs: Any) -> dict[str, Any] | None:
        if not handed:
            return _job(1)
        if stop == "ctrl-c":
            raise KeyboardInterrupt
        runner_mod._STOPPING.set()
        return None

    monkeypatch.setattr(runner_mod, "claim_payload", claim)
    monkeypatch.setattr(runner_mod, "_spawn_turn", spawn)

    assert (
        runner_mod.run_forever(
            base_url="http://control",
            gateway_url=gateway_url,
            worker_id="runner-a",
            backend=backend,
            host="host-a",
        )
        == 0
    )

    (spare,) = made
    assert spare.kwargs == {**_CONFIG, "gateway_url": gateway_url, "worker_id": "runner-a"}
    assert handed == [spare]
    assert spare.closed == [stop == "ctrl-c"]


def test_a_service_given_its_own_launcher_starts_no_spare(service, monkeypatch):
    _identity, backend, _released = service
    started: list[Any] = []
    monkeypatch.setattr(turn_launch, "_start_child", started.append)
    claims = iter([_job(1)])

    def claim(**_kwargs: Any) -> dict[str, Any] | None:
        job = next(claims, None)
        if job is None:
            runner_mod._STOPPING.set()
        return job

    launched: list[str] = []

    def launch(payload: dict[str, Any]) -> LaunchResult:
        launched.append(payload["job_id"])
        return LaunchResult(LAUNCH_STARTED, FakeHandle())

    monkeypatch.setattr(runner_mod, "claim_payload", claim)

    runner_mod.run_forever(
        base_url="http://control",
        worker_id="runner-a",
        backend=backend,
        host="host-a",
        spawn_turn=launch,
    )

    assert launched == ["ajob_1"]
    assert started == [], "only the host's own launches keep a spare"


# ── Real children ──────────────────────────────────────────────────────


def test_a_real_spare_retired_without_a_turn_runs_nothing_the_canary_runs(monkeypatch):
    """The same command with and without ``--spare``, given empty input.

    The unacknowledged process backend declines its probe, so a child that
    ran it would answer and exit 2, as the cold one does.
    """
    monkeypatch.setenv("AGENT_SANDBOX_BACKEND", "process")
    monkeypatch.delenv("AGENT_SANDBOX_ALLOW_UNISOLATED", raising=False)
    outcomes: dict[bool, tuple[int, str]] = {}
    for spare in (False, True):
        read_fd, write_fd = os.pipe()
        try:
            argv = turn_launch.turn_argv(write_fd, worker_id="runner-a", spare=spare, **_CONFIG)
            process = subprocess.Popen(argv, stdin=subprocess.PIPE, pass_fds=(write_fd,))
        finally:
            os.close(write_fd)
        assert process.stdin is not None
        process.stdin.close()
        code = process.wait(timeout=120)
        with os.fdopen(read_fd, "rb") as answers:
            outcomes[spare] = (code, answers.read().decode())

    assert outcomes[True] == (0, "")
    code, answer = outcomes[False]
    assert code == 2
    assert answer.startswith("declined ")
    assert "no isolation" in answer


def test_a_real_spare_takes_its_turn_and_the_next_one_waits(monkeypatch):
    """Answered as a cold child would answer, with no cold child started."""
    monkeypatch.setenv("AGENT_SANDBOX_BACKEND", "process")
    monkeypatch.setenv("AGENT_SANDBOX_ALLOW_UNISOLATED", "1")
    # However slowly a loaded machine imports, the retired spare exits by itself.
    monkeypatch.setattr(turn_launch, "SPARE_RETIRE_WAIT_S", 120.0)
    real = subprocess.Popen
    started: list[tuple[list[str], subprocess.Popen[bytes]]] = []

    def recorded(argv: list[str], **kwargs: Any) -> subprocess.Popen[bytes]:
        process = real(argv, **kwargs)
        started.append((list(argv), process))
        return process

    monkeypatch.setattr(subprocess, "Popen", recorded)
    spare = _spare()
    spare.replenish()
    try:
        # A turn the child cannot use: it declines after its probe, and runs nothing.
        result = _launch({"job_id": "ajob_unusable"}, spare=spare)
    finally:
        spare.close()

    assert (result.outcome, result.detail) == (
        LAUNCH_NOT_STARTED,
        "declined unusable turn (KeyError)",
    )
    assert [argv[-1] for argv, _process in started] == ["--spare", "--spare"]
    assert [process.returncode for _argv, process in started] == [0, 0]
