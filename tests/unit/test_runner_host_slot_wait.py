"""A host at capacity claims as soon as a slot comes free, and asks the API nothing more.

With every slot busy the claim loop waits. A turn whose child exits, or an
environment test that ends, ends that wait at once; it used to wait for a tick
of up to a second (#1051). The tick stays as the fallback, every capacity check
runs again after a wake, and nothing shortens the waits that follow an empty
claim or a failed one: those are what keep a host from polling the control
plane (#814).
"""

from __future__ import annotations

import itertools
import os
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

import pytest
from tests.unit.test_runner_host_service import (
    FakeHandle,
    _job,
    service as service,
)

from cloud_agent_host import environment_tests, runner as runner_mod
from cloud_agent_host.host_runtime import LAUNCH_STARTED, LaunchResult, TurnPool

if TYPE_CHECKING:
    from collections.abc import Callable

#: The real wait and sleep, taken before the ``service`` fixture scripts both.
_REAL_WAIT_FOR_SLOT = TurnPool.wait_for_slot
_REAL_SLEEP = time.sleep

#: The exit watcher waits with ``os.waitid``, which Python on macOS lacks.
needs_waitid = pytest.mark.skipif(not hasattr(os, "waitid"), reason="no os.waitid here")


class _Children:
    """Each claimed turn's child: a real process that runs until the test ends it."""

    def __init__(self) -> None:
        self.started: list[subprocess.Popen[bytes]] = []

    def launch(self, _payload: dict) -> LaunchResult:
        """Start a child and hand it the turn once it answers, as a turn's child does."""
        child = subprocess.Popen(
            [sys.executable, "-c", "import sys; print('accepted', flush=True); sys.stdin.read()"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
        )
        self.started.append(child)
        assert child.stdout is not None and child.stdout.readline() == b"accepted\n"
        return LaunchResult(LAUNCH_STARTED, child)

    def end(self, index: int) -> None:
        """End one turn: its child reads the end of its input and exits."""
        stdin = self.started[index].stdin
        assert stdin is not None
        stdin.close()

    def close(self) -> None:
        """Close every pipe, and collect any child the service's drain did not."""
        for child in self.started:
            for pipe in (child.stdin, child.stdout):
                if pipe is not None:
                    pipe.close()
            if child.returncode is None:
                child.kill()
                child.wait()


@dataclass
class _Wait:
    """One wait for a slot: its timeout, when it began, and how it ended."""

    timeout: float
    began: float
    #: Whether a freed slot ended it; ``None`` while it lasts.
    freed: bool | None = None
    ended: float | None = None


class _Loop:
    """The host service's claims and waits for a slot, recorded as it makes them.

    Each wait is the real one. Recording it lets a test act while the loop
    waits, and tell a wait a freed slot ended from one that ran out.
    """

    def __init__(self, monkeypatch: pytest.MonkeyPatch) -> None:
        #: When each claim was asked for.
        self.claims: list[float] = []
        self.waits: list[_Wait] = []
        self._changed = threading.Condition()

        def wait_for_slot(pool: TurnPool, timeout: float) -> bool:
            wait = _Wait(timeout, time.monotonic())
            self._record(lambda: self.waits.append(wait))
            freed = _REAL_WAIT_FOR_SLOT(pool, timeout)
            ended = time.monotonic()

            def finish() -> None:
                wait.freed, wait.ended = freed, ended

            self._record(finish)
            return freed

        monkeypatch.setattr(TurnPool, "wait_for_slot", wait_for_slot)

    def freed(self) -> int:
        """How many waits a freed slot ended."""
        return sum(wait.freed is True for wait in self.waits)

    def waiting(self) -> bool:
        """Whether the loop is in a wait for a slot right now."""
        return bool(self.waits) and self.waits[-1].freed is None

    def claim(self, job: dict[str, Any] | None) -> dict[str, Any] | None:
        """Record one claim, answered with ``job``."""
        now = time.monotonic()
        self._record(lambda: self.claims.append(now))
        return job

    def until(self, reached: Callable[[], bool]) -> None:
        """Return once ``reached`` holds, failing the test after ten seconds."""
        with self._changed:
            assert self._changed.wait_for(reached, timeout=10), "the service never got there"

    def _record(self, change: Callable[[], object]) -> None:
        with self._changed:
            change()
            self._changed.notify_all()


def _serve(
    backend: Any, launch: Callable[[dict], LaunchResult], *, tick_s: float
) -> threading.Thread:
    """Run the host service on a thread of its own, so a test can act while it waits."""
    thread = threading.Thread(
        target=runner_mod.run_forever,
        kwargs={
            "base_url": "http://control",
            "worker_id": "runner-a",
            "backend": backend,
            "host": "host-a",
            "spawn_turn": launch,
            "tick_s": tick_s,
        },
        daemon=True,
    )
    thread.start()
    return thread


def _stop(thread: threading.Thread, children: _Children) -> None:
    """Stop the service as ``docker stop`` does, ending every turn so the drain finishes."""
    runner_mod._STOPPING.set()
    deadline = time.monotonic() + 10
    while thread.is_alive() and time.monotonic() < deadline:
        # Again on every pass: a launch in flight adds a child after the others ended.
        for index in range(len(children.started)):
            children.end(index)
        thread.join(timeout=0.05)
    children.close()
    assert not thread.is_alive(), "the service did not stop"


def _full_queue(loop: _Loop, children: _Children) -> Callable[..., dict[str, Any] | None]:
    """A queue that always has another turn to claim."""
    return lambda **_kwargs: loop.claim(_job(len(children.started)))


def _pools(monkeypatch: pytest.MonkeyPatch) -> list[TurnPool]:
    """The service's turn pool, kept where a test can free a slot through it."""
    pools: list[TurnPool] = []

    def pool(**kwargs: Any) -> TurnPool:
        pools.append(TurnPool(**kwargs))
        return pools[-1]

    monkeypatch.setattr(runner_mod, "TurnPool", pool)
    return pools


@pytest.mark.parametrize("full", [False, True])
def test_freed_slots_are_detected_without_faster_empty_queue_polling(service, monkeypatch, full):
    _identity, backend, _released = service
    waits = []
    launched = []

    def sleep(seconds):
        waits.append(seconds)
        runner_mod._STOPPING.set()

    def launch(_payload):
        launched.append(FakeHandle())
        return LaunchResult(LAUNCH_STARTED, launched[-1])

    monkeypatch.setattr(runner_mod.time, "sleep", sleep)
    monkeypatch.setattr(
        runner_mod, "claim_payload", lambda **_k: _job(len(launched)) if full else None
    )
    assert (
        runner_mod.run_forever(
            base_url="http://control",
            worker_id="runner-a",
            backend=backend,
            host="host-a",
            spawn_turn=launch,
        )
        == 0
    )
    # Shutdown may sleep while draining live handles; the service's first
    # sleep is the relevant interval and never triggers an extra API claim.
    assert waits[0] == (1.0 if full else 5.0)
    assert len(launched) == (3 if full else 0)


@needs_waitid
def test_a_turn_that_ends_at_capacity_is_followed_by_the_next_claim_at_once(service, monkeypatch):
    """The loop waited out a second at capacity; the child's exit now ends that wait."""
    _identity, backend, _released = service
    loop = _Loop(monkeypatch)
    children = _Children()
    monkeypatch.setattr(runner_mod, "claim_payload", _full_queue(loop, children))
    thread = _serve(backend, children.launch, tick_s=5.0)
    try:
        loop.until(loop.waiting)
        assert (len(loop.claims), loop.waits[0].timeout) == (3, 1.0), "full, on the 1 s tick"
        ended = time.monotonic()
        children.end(0)
        loop.until(lambda: len(loop.claims) == 4 and loop.waiting())
        assert loop.freed() == 1, "the exit ended the wait, not its timeout"
        assert loop.claims[3] - ended < 0.5, "well under the second the loop used to wait"
        assert children.started[0].returncode == 0, "and the loop collected it"
        # Full again, and nothing else has ended: the new wait lasts, not a busy loop.
        _REAL_SLEEP(0.3)
        assert loop.freed() == 1 and len(loop.waits) <= 3
    finally:
        _stop(thread, children)


def test_a_wake_that_frees_no_slot_claims_nothing(service, monkeypatch):
    """Every capacity check runs again after a wake, and a host still full stays full."""
    _identity, backend, _released = service
    loop = _Loop(monkeypatch)
    pools = _pools(monkeypatch)
    children = _Children()
    monkeypatch.setattr(runner_mod, "claim_payload", _full_queue(loop, children))
    thread = _serve(backend, children.launch, tick_s=5.0)
    try:
        loop.until(loop.waiting)
        pools[0].slot_freed()
        loop.until(lambda: loop.freed() == 1 and loop.waiting())
        assert len(loop.claims) == 3, "every child still runs, so every slot is still taken"
    finally:
        _stop(thread, children)


def test_with_nothing_freed_the_fallback_tick_still_runs(service, monkeypatch):
    """A raised limit frees no slot and wakes nothing; the tick finds it, as it always did."""
    identity, backend, _released = service
    loop = _Loop(monkeypatch)
    children = _Children()
    monkeypatch.setattr(runner_mod, "claim_payload", _full_queue(loop, children))
    thread = _serve(backend, children.launch, tick_s=0.2)
    try:
        loop.until(loop.waiting)
        identity.slots_desired = 4
        loop.until(lambda: len(loop.claims) == 4 and loop.waiting())
        first = loop.waits[0]
        assert (first.timeout, first.freed) == (0.2, False), "nothing ended it but its timeout"
        assert first.ended is not None and first.ended - first.began >= first.timeout - 0.01
        assert loop.claims[3] >= first.ended
    finally:
        _stop(thread, children)


class _HeldTest:
    """An environment test holding its slot from the start, until the test ends it."""

    def __init__(
        self, _base_url: str, _backend: Any, *, on_idle: Callable[[], None] | None = None
    ) -> None:
        self.busy = True
        self.on_idle = on_idle

    def end(self) -> None:
        self.busy = False
        assert self.on_idle is not None, "the claim loop gave the worker no way to wake it"
        self.on_idle()

    def poll(self, _credential: str, _attestation: Any = None) -> None:
        pass

    def close(self) -> None:
        pass


def test_an_environment_test_that_ends_at_capacity_frees_its_slot_at_once(service, monkeypatch):
    """Below its limit but full with a test, the loop waited a whole interval for it."""
    _identity, _process_backend, _released = service
    backend = runner_mod.ContainerBackend(image="img")
    monkeypatch.setattr(backend, "preflight", lambda **_kw: None)
    monkeypatch.setattr(
        backend, "version_stamp", lambda: {"sandbox_image_digest": "sha256:" + "a" * 64}
    )
    tests: list[_HeldTest] = []

    def worker(*args: Any, **kwargs: Any) -> _HeldTest:
        tests.append(_HeldTest(*args, **kwargs))
        return tests[-1]

    monkeypatch.setattr("cloud_agent_host.environment_tests.EnvironmentTestWorker", worker)
    loop = _Loop(monkeypatch)
    children = _Children()
    monkeypatch.setattr(runner_mod, "claim_payload", _full_queue(loop, children))
    thread = _serve(backend, children.launch, tick_s=30.0)
    try:
        loop.until(loop.waiting)
        assert (len(loop.claims), loop.waits[0].timeout) == (2, 30.0), "two turns and the test"
        ended = time.monotonic()
        tests[0].end()
        loop.until(lambda: len(loop.claims) == 3 and loop.waiting())
        assert loop.waits[0].freed is True
        assert loop.claims[2] - ended < 0.5, "not the thirty seconds the loop would have waited"
    finally:
        _stop(thread, children)


_TICK = 0.3


@pytest.mark.parametrize("after", ["long_poll", "poll", "unreachable"])
def test_a_slot_freed_after_an_empty_or_failed_claim_adds_no_claim(service, monkeypatch, after):
    """Only a full host is woken; a wait after an empty claim or an error runs its course.

    Shortening those would put back the polling #814 took away: a host with a
    free slot that has found the queue empty asks again on the control plane's
    signal or its own interval, whatever ends meanwhile.
    """
    _identity, backend, _released = service
    monkeypatch.setattr(runner_mod.time, "sleep", _REAL_SLEEP)
    if after == "long_poll":
        # A wait the control plane answers only at its timeout: nothing was queued.
        monkeypatch.setattr(
            runner_mod, "wait_for_work", lambda **kw: _REAL_SLEEP(kw["timeout_s"]) or "token"
        )
    loop = _Loop(monkeypatch)
    pools = _pools(monkeypatch)
    children = _Children()

    def free() -> None:
        # The one running turn ends, and an environment test with it.
        children.end(0)
        pools[0].slot_freed()

    def claim(**_kwargs: Any) -> dict[str, Any] | None:
        loop.claim(None)
        if len(loop.claims) == 1:
            return _job(1)
        if len(loop.claims) == 2:
            threading.Timer(_TICK / 3, free).start()
        if len(loop.claims) == 4:
            runner_mod._STOPPING.set()
        elif after == "unreachable":
            raise runner_mod.ClaimUnreachable("connection refused")
        return None

    monkeypatch.setattr(runner_mod, "claim_payload", claim)
    thread = _serve(backend, children.launch, tick_s=_TICK)
    try:
        thread.join(timeout=10)
        assert not thread.is_alive()
    finally:
        _stop(thread, children)
    assert len(loop.claims) == 4
    gaps = [later - earlier for earlier, later in itertools.pairwise(loop.claims[1:])]
    assert min(gaps) >= _TICK - 0.01, f"a freed slot cut a wait short: {gaps}"
    assert loop.waits == [], "never full, so never waiting for a slot"


# ── The environment-test worker's side ─────────────────────────────────


@pytest.mark.parametrize("fails", [False, True])
def test_an_environment_test_frees_its_slot_before_it_wakes_the_loop(monkeypatch, fails):
    """The loop it wakes counts slots at once, so it must find this one free already."""
    held: list[bool] = []
    woken: list[bool] = []

    def run(**_kwargs: Any) -> None:
        held.append(worker.busy)
        if fails:
            raise RuntimeError("the control plane went away")

    monkeypatch.setattr(environment_tests, "claim_and_execute", run)
    worker = environment_tests.EnvironmentTestWorker(
        "http://control", object(), on_idle=lambda: woken.append(worker.busy)
    )
    worker.poll("credential")
    assert worker._thread is not None
    worker._thread.join(5)
    worker.close()
    assert held == [True], "a claim in flight holds its slot"
    assert woken == [False], "woken once, with the slot already given back"


def test_a_poll_whose_thread_cannot_start_holds_no_slot(monkeypatch):
    """Nothing runs, so nothing may keep a slot from the coding turns."""

    def refuse(_thread: threading.Thread) -> None:
        raise RuntimeError("can't start new thread")

    monkeypatch.setattr(environment_tests, "claim_and_execute", lambda **_kw: None)
    worker = environment_tests.EnvironmentTestWorker("http://control", object())
    monkeypatch.setattr(environment_tests.threading.Thread, "start", refuse)
    with pytest.raises(RuntimeError, match="can't start"):
        worker.poll("credential")
    assert not worker.busy
    assert worker._client is not None
    worker._client.close()
