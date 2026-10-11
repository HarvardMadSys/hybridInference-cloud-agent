"""The host's turn accounting, and the handover that decides what may be requeued.

Two things are being asserted here and they carry different weight. The
arithmetic — how many turns are in flight, when another may be claimed, how a
run of failures backs off — is ordinary and tested with fakes. The handover is
not ordinary: it is the rule that decides whether a user's message goes back to
the queue or waits out a lease, and getting it wrong in the permissive
direction runs someone's instruction twice. So every outcome it can produce is
tested, including with a real child process, and the ambiguous case is asserted
to stay ambiguous rather than resolve itself into a requeue.
"""

from __future__ import annotations

import os
import subprocess
import sys
import textwrap
from typing import TYPE_CHECKING

import pytest

from cloud_agent_host.host_runtime import (
    HANDOVER_ACCEPTED,
    HANDOVER_DECLINED,
    LAUNCH_NOT_STARTED,
    LAUNCH_STARTED,
    LAUNCH_UNCERTAIN,
    MAX_SLOTS,
    MIN_SLOTS,
    LaunchResult,
    TurnPool,
    decode_turn,
    default_slots,
    encode_turn,
    read_handover,
    requested_slots,
    write_handover,
)

if TYPE_CHECKING:
    from pathlib import Path


class FakeHandle:
    """A turn's child process, exiting only when a test says so."""

    def __init__(self) -> None:
        self.exit_code: int | None = None
        self.terminated = False
        self.killed = False

    def poll(self) -> int | None:
        return self.exit_code

    def terminate(self) -> None:
        self.terminated = True

    def kill(self) -> None:
        self.killed = True
        self.exit_code = -9


class Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


def _job(job_id: str = "ajob_1", attempt_id: int = 1) -> dict:
    return {"job_id": job_id, "attempt_id": attempt_id, "worker_token": "ajt.a.b"}


@pytest.fixture
def pool():
    """A pool whose launches are scripted, with a clock a test can move."""
    clock = Clock()
    scripted: list[LaunchResult] = []
    launched: list[dict] = []

    def launch(job: dict) -> LaunchResult:
        launched.append(job)
        return scripted.pop(0) if scripted else LaunchResult(LAUNCH_STARTED, FakeHandle())

    turns = TurnPool(
        launch=launch,
        clock=clock,
        failures_before_pause=3,
        backoff_s=5.0,
        backoff_cap_s=20.0,
    )
    return turns, scripted, launched, clock


# ── Configuration ──────────────────────────────────────────────────────


def test_the_default_limit_is_read_and_bounded() -> None:
    assert default_slots({}) == 3
    assert default_slots({"AGENT_HOST_SLOTS": "5"}) == 5
    for bad in ("1", "0", "-2", "nope", str(MAX_SLOTS + 1)):
        with pytest.raises(ValueError, match="AGENT_HOST_SLOTS"):
            default_slots({"AGENT_HOST_SLOTS": bad})


def test_a_saved_limit_is_honoured_only_inside_the_protocols_bounds() -> None:
    """The admin schema enforces the same bounds; the host checks again because it acts."""
    assert requested_slots(None) is None
    assert requested_slots(True) is None, "a boolean is not a count"
    assert requested_slots("4") is None
    assert requested_slots(MIN_SLOTS - 1) is None
    assert requested_slots(MAX_SLOTS + 1) is None
    assert requested_slots(MIN_SLOTS) == MIN_SLOTS
    assert requested_slots(MAX_SLOTS) == MAX_SLOTS


# ── Accounting ─────────────────────────────────────────────────────────


def test_a_claimed_turn_occupies_its_slot_from_the_moment_it_is_launched(pool) -> None:
    """Including while its child is still starting.

    Waiting for the handover before counting a turn would let a second claim
    land during the first handover, and the host would run one turn more than
    it was allowed to.
    """
    turns, scripted, _launched, _clock = pool
    scripted.append(LaunchResult(LAUNCH_UNCERTAIN, FakeHandle(), "no answer yet"))

    turns.launch(_job())

    assert turns.running() == 1
    assert turns.may_claim(2) is True
    assert turns.may_claim(1) is False


def test_the_limit_is_compared_fresh_so_lowering_it_stops_claiming_at_once(pool) -> None:
    turns, _scripted, _launched, _clock = pool
    for n in range(3):
        turns.launch(_job(f"ajob_{n}", n))

    assert turns.may_claim(5) is True
    assert turns.may_claim(3) is False
    assert turns.may_claim(2) is False, "already over a lowered limit: claim nothing"
    # And nothing was touched to make that true.
    assert turns.running() == 3


def test_a_finished_turn_gives_its_slot_back(pool) -> None:
    turns, _scripted, _launched, _clock = pool
    turns.launch(_job("a", 1))
    turns.launch(_job("b", 2))
    live = [turn.handle for turn in turns.turns()]
    live[0].exit_code = 0

    finished = turns.reap()

    assert [item.job_id for item in finished] == ["a"]
    assert finished[0].exit_code == 0
    assert turns.running() == 1


def test_a_shutting_down_host_claims_nothing_more(pool) -> None:
    """Shutdown stops claiming first; the turn in flight is still waited for."""
    turns, _scripted, _launched, _clock = pool
    turns.launch(_job())
    handle = turns.turns()[0].handle

    def wait(_seconds: float) -> None:
        assert turns.may_claim(10) is False, "claimed again while shutting down"
        handle.exit_code = 0

    turns.shutdown(wait=wait)

    assert handle.terminated and not handle.killed
    assert turns.may_claim(10) is False


# ── Launch failures ────────────────────────────────────────────────────


def test_one_launch_failure_does_not_stop_the_host(pool) -> None:
    """It costs one requeued claim, which is cheaper than pausing a healthy host."""
    turns, scripted, _launched, _clock = pool
    scripted.append(LaunchResult(LAUNCH_NOT_STARTED, None, "could not start"))

    result = turns.launch(_job())

    assert result.outcome == LAUNCH_NOT_STARTED
    assert turns.running() == 0, "nothing to track: the child never existed"
    assert turns.may_claim(3) is True
    assert turns.launch_error == ""


def test_a_run_of_launch_failures_stops_the_host_and_says_why(pool) -> None:
    turns, scripted, _launched, clock = pool
    for _ in range(3):
        scripted.append(LaunchResult(LAUNCH_NOT_STARTED, None, "image is broken"))

    for _ in range(3):
        turns.launch(_job())

    assert turns.may_claim(3) is False, "a broken image must not take the queue apart"
    assert "3 turns in a row" in turns.launch_error
    assert "image is broken" in turns.launch_error

    clock.now += 5.0
    assert turns.may_claim(3) is True, "the pause is a backoff, not a latch"


def test_the_backoff_grows_and_is_capped(pool) -> None:
    """The first two failures cost nothing; from the third the wait doubles."""
    turns, scripted, _launched, clock = pool
    for _ in range(6):
        scripted.append(LaunchResult(LAUNCH_NOT_STARTED, None, "broken"))

    waits = []
    for _ in range(6):
        turns.launch(_job())
        start = clock.now
        while not turns.may_claim(3):
            clock.now += 1.0
        waits.append(clock.now - start)

    # Two tolerated, then 5s, 10s, and the cap — so a machine that is down for
    # an hour is retried on a schedule rather than hammered or given up on.
    assert waits == [0.0, 0.0, 5.0, 10.0, 20.0, 20.0]


def test_a_successful_launch_clears_the_reason_and_the_backoff(pool) -> None:
    turns, scripted, _launched, _clock = pool
    for _ in range(3):
        scripted.append(LaunchResult(LAUNCH_NOT_STARTED, None, "broken"))
    for _ in range(3):
        turns.launch(_job())
    assert turns.launch_error != ""

    scripted.append(LaunchResult(LAUNCH_STARTED, FakeHandle()))
    turns.launch(_job())

    assert turns.launch_error == ""
    assert turns.may_claim(3) is True


def test_an_uncertain_handover_counts_as_a_failure_but_keeps_the_turn(pool) -> None:
    """The host stops claiming into whatever is wrong; the turn is still not requeued."""
    turns, scripted, _launched, _clock = pool
    for _ in range(3):
        scripted.append(LaunchResult(LAUNCH_UNCERTAIN, FakeHandle(), "no answer"))

    for n in range(3):
        turns.launch(_job(f"ajob_{n}", n))

    assert turns.running() == 3
    assert turns.launch_error != ""


# ── Shutdown ───────────────────────────────────────────────────────────


def test_shutdown_lets_every_turn_finish(pool) -> None:
    turns, _scripted, _launched, _clock = pool
    for n in range(3):
        turns.launch(_job(f"ajob_{n}", n))
    handles = [turn.handle for turn in turns.turns()]

    def wait(_seconds: float) -> None:
        for handle in handles:
            handle.exit_code = 0

    finished = turns.shutdown(wait=wait)

    assert all(handle.terminated for handle in handles)
    assert not any(handle.killed for handle in handles)
    assert len(finished) == 3
    assert turns.running() == 0


def test_an_immediate_shutdown_kills(pool) -> None:
    turns, _scripted, _launched, _clock = pool
    turns.launch(_job())
    handles = [turn.handle for turn in turns.turns()]

    turns.shutdown(immediate=True, wait=lambda _s: None)

    assert all(handle.killed for handle in handles)
    assert turns.running() == 0


# ── The payload ────────────────────────────────────────────────────────


def test_a_turn_round_trips_and_anything_else_is_not_a_turn() -> None:
    """``decode_turn`` is the child's proof that it was given something to run.

    Strictly a parse, because guessing would let a child accept a turn it was
    not given — and the host would then never hand that turn back.
    """
    payload = {"job_id": "ajob_1", "attempt_id": 7, "worker_token": "t"}
    assert decode_turn(encode_turn(payload).decode()) == payload

    for nothing in ("", "   ", "not json", "[]", "null", '"ajob_1"', "{}", '{"attempt_id": 7}'):
        assert decode_turn(nothing) is None, nothing


# ── The handover, on real descriptors ──────────────────────────────────


def test_an_answer_is_read_back_whole() -> None:
    read_fd, write_fd = os.pipe()
    write_handover(write_fd, f"{HANDOVER_ACCEPTED} ajob_1")

    assert read_handover(read_fd) == f"{HANDOVER_ACCEPTED} ajob_1"


def test_a_closed_descriptor_with_no_answer_reads_as_nothing_happened() -> None:
    """Which is what a child exiting does, and how "it never began" arrives."""
    read_fd, write_fd = os.pipe()
    os.close(write_fd)

    assert read_handover(read_fd) == ""


def test_silence_past_the_deadline_is_not_an_answer() -> None:
    """``None`` is the ambiguous case, and it must stay distinguishable from ``""``."""
    read_fd, write_fd = os.pipe()
    try:
        assert read_handover(read_fd, timeout_s=0.05) is None
    finally:
        os.close(write_fd)


def test_a_child_that_floods_the_descriptor_is_not_an_answer() -> None:
    read_fd, write_fd = os.pipe()
    os.write(write_fd, b"x" * 8192)
    os.close(write_fd)

    assert read_handover(read_fd) == "", "unbounded input from an unproven process"


def test_writing_an_answer_leaves_standard_output_open(tmp_path: Path) -> None:
    """The deployment check reads the answer from the process's own output.

    Closing it would take that process's stdout away from everything after the
    answer, which is exactly what the probe then has to read.
    """
    target = tmp_path / "out"
    with target.open("w") as handle:
        write_handover(1, "irrelevant")  # not this test's stdout; see below
        os.write(handle.fileno(), b"still open\n")
    assert "still open" in target.read_text()


def test_writing_an_answer_never_raises_when_the_host_has_gone() -> None:
    """A host that gave up closed its end; the child still has a turn to run."""
    read_fd, write_fd = os.pipe()
    os.close(read_fd)

    write_handover(write_fd, f"{HANDOVER_ACCEPTED} ajob_1")


def test_a_real_child_answers_before_it_does_anything_else(tmp_path: Path) -> None:
    """The ordering the whole requeue rule rests on.

    The child writes its answer and only then touches anything an operator or
    a user could observe. The test proves the order by having the child record
    its "side effect" after answering, and reading the answer before the file
    exists.
    """
    marker = tmp_path / "side-effect"
    script = tmp_path / "child.py"
    script.write_text(
        textwrap.dedent(
            f"""
            import os, sys
            fd = int(sys.argv[1])
            release_fd = int(sys.argv[2])
            os.write(fd, b"{HANDOVER_ACCEPTED} ajob_1\\n")
            os.close(fd)
            os.read(release_fd, 1)
            os.close(release_fd)
            open({str(marker)!r}, "w").write("ran")
            """
        )
    )
    read_fd, write_fd = os.pipe()
    release_read_fd, release_write_fd = os.pipe()
    try:
        process = subprocess.Popen(
            [sys.executable, str(script), str(write_fd), str(release_read_fd)],
            pass_fds=(write_fd, release_read_fd),
        )
    except BaseException:
        for fd in (read_fd, write_fd, release_read_fd, release_write_fd):
            os.close(fd)
        raise
    os.close(write_fd)
    os.close(release_read_fd)
    try:
        answer = read_handover(read_fd, timeout_s=30)
        assert answer == f"{HANDOVER_ACCEPTED} ajob_1"
        assert not marker.exists(), "the answer must come before anything observable"
    finally:
        # EOF releases the child even when an assertion fails.
        os.close(release_write_fd)
        try:
            process.wait(timeout=30)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5)
            raise
    assert marker.exists()


def test_a_real_child_that_dies_before_answering_reads_as_nothing_happened() -> None:
    read_fd, write_fd = os.pipe()
    process = subprocess.Popen([sys.executable, "-c", "raise SystemExit(1)"], pass_fds=(write_fd,))
    os.close(write_fd)
    try:
        assert read_handover(read_fd, timeout_s=30) == ""
    finally:
        process.wait(timeout=30)


def test_a_real_child_can_decline() -> None:
    read_fd, write_fd = os.pipe()
    process = subprocess.Popen(
        [
            sys.executable,
            "-c",
            f"import os,sys; fd=int(sys.argv[1]); os.write(fd, b'{HANDOVER_DECLINED} empty\\n')",
            str(write_fd),
        ],
        pass_fds=(write_fd,),
    )
    os.close(write_fd)
    try:
        assert read_handover(read_fd, timeout_s=30).startswith(HANDOVER_DECLINED)
    finally:
        process.wait(timeout=30)
