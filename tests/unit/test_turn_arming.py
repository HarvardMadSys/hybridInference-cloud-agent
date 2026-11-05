"""What a member turn arms before its prompt, and what may run side by side."""

from __future__ import annotations

import threading

import pytest
from tests.unit.test_shared_session_supervisor import (
    SharedChatSessionSupervisor,
    _launch,
    _Manager,
    _members,
    _spec,
    _TransportFactory,
)

from cloud_agent_host.chat_sandbox_models import MEMBER_UID_BASE
from cloud_agent_host.sandbox import KATA_RUNTIME, SandboxError
from cloud_agent_host.session_lifecycle import SessionLeaseLost
from cloud_agent_host.turn_arming import run_beside

_CENSUS_THREAD = "turn-member-census"


def _resident(manager: _Manager, factory: _TransportFactory | None = None):
    """A supervisor whose source member holds a lease and a live CLI."""
    factory = factory or _TransportFactory()
    supervisor = SharedChatSessionSupervisor(manager, transport_factory=factory)
    lease = supervisor.acquire(
        _spec(),
        thread_id="athr_source",
        owner="attempt:1",
        activation_generation=1,
        has_settled_context=True,
        manifest=_members(),
    )
    supervisor.ensure_resident(
        "athr_source",
        lease.token,
        _launch("athr_source", native_session_id="native-source", slot=0),
    )
    # Window censuses tick on their own thread; none is under test here.
    supervisor._stop.set()
    if supervisor._sampler_thread is not None:
        supervisor._sampler_thread.join(timeout=2)
    return supervisor, lease, factory


def _start(supervisor: SharedChatSessionSupervisor, token: str):
    return supervisor.start_turn(
        "athr_source", token, prompt="work", model="glm-5.1", reasoning_effort=None
    )


def _census_threads() -> list[threading.Thread]:
    return [thread for thread in threading.enumerate() if thread.name == _CENSUS_THREAD]


class _MarkedManager(_Manager):
    """A runc manager whose tool-mark fence is an exec of its own, as in production."""

    def __init__(self) -> None:
        super().__init__()
        self.marks_fenced = threading.Event()

    def tool_mark_reader(self, _spec):
        self.events.append(("marks-begin",))
        self.marks_fenced.set()
        return object()


def test_the_census_and_the_tool_mark_fence_run_side_by_side():
    """Neither exec waits for the other: each is held until both have begun."""
    both_running = threading.Barrier(2, timeout=5)

    class Manager(_MarkedManager):
        def capture_member_processes(self, spec, **kwargs):
            both_running.wait()
            return super().capture_member_processes(spec, **kwargs)

        def tool_mark_reader(self, spec):
            reader = super().tool_mark_reader(spec)
            both_running.wait()
            return reader

    manager = Manager()
    supervisor, lease, factory = _resident(manager)

    turn = _start(supervisor, lease.token)

    member = supervisor._members["athr_source"].members["athr_source"]
    assert factory.transports[-1].turns == [turn]
    assert member.activation_processes == ((41, 400),)
    assert member.tool_mark_reader is not None
    assert _census_threads() == []


def test_a_kata_census_ends_before_the_guest_observer_fence_is_taken():
    """The census runs as the member: still running, it would be billed to the turn."""
    census_done = threading.Event()

    class Manager(_Manager):
        def capture_member_processes(self, spec, **kwargs):
            baseline = super().capture_member_processes(spec, **kwargs)
            census_done.set()
            return baseline

        def begin_guest_observation(self, spec, **kwargs):
            assert census_done.is_set()
            assert _census_threads() == []
            return super().begin_guest_observation(spec, **kwargs)

        def tool_mark_reader(self, _spec):
            raise AssertionError("Kata must not trust the agent-owned mark file")

    manager = Manager()
    manager.backend.runtime = KATA_RUNTIME
    supervisor, lease, _ = _resident(manager)
    manager.events.clear()

    _start(supervisor, lease.token)

    assert manager.events == [
        ("census", 0),
        ("observe-begin", MEMBER_UID_BASE),
        ("member-window", "register"),
    ]


def test_a_failed_census_beside_the_fence_fails_the_turn_before_its_prompt():
    """The census error is the turn's, its thread is gone, and the member can retry."""

    class Manager(_MarkedManager):
        failing = True

        def capture_member_processes(self, spec, **kwargs):
            # Fail only once the fence is under way, so the two overlapped.
            assert self.marks_fenced.wait(5)
            if self.failing:
                raise SandboxError("member process census failed: exec refused")
            return super().capture_member_processes(spec, **kwargs)

    manager = Manager()
    supervisor, lease, factory = _resident(manager)

    with pytest.raises(SandboxError, match="member process census failed"):
        _start(supervisor, lease.token)

    member = supervisor._members["athr_source"].members["athr_source"]
    assert factory.transports[-1].turns == []
    assert member.turn_lock.locked() is False
    assert member.current_turn is None
    assert _census_threads() == []

    manager.failing = False
    turn = _start(supervisor, lease.token)
    assert factory.transports[-1].turns == [turn]


def test_a_failed_tool_mark_fence_waits_for_the_census_before_failing_the_turn():
    """Nothing the census does may outlive the turn it was taken for."""
    census_finished = threading.Event()
    release_census = threading.Event()

    class Manager(_Manager):
        def capture_member_processes(self, spec, **kwargs):
            assert release_census.wait(5)
            baseline = super().capture_member_processes(spec, **kwargs)
            census_finished.set()
            return baseline

        def tool_mark_reader(self, _spec):
            release_census.set()
            raise SandboxError("tool-mark fence could not be read")

    manager = Manager()
    supervisor, lease, factory = _resident(manager)

    with pytest.raises(SandboxError, match="tool-mark fence"):
        _start(supervisor, lease.token)

    assert census_finished.is_set()
    assert _census_threads() == []
    member = supervisor._members["athr_source"].members["athr_source"]
    assert member.turn_lock.locked() is False
    assert factory.transports[-1].turns == []


def test_a_lease_lost_while_arming_side_by_side_is_refused_before_the_prompt():
    """Admission is rechecked only after both have finished, and the prompt never sent."""
    holder: dict[str, object] = {}

    class Manager(_MarkedManager):
        def capture_member_processes(self, spec, **kwargs):
            assert self.marks_fenced.wait(5)
            holder["supervisor"].abandon("athr_source", holder["token"], lost=False)
            return super().capture_member_processes(spec, **kwargs)

    manager = Manager()
    supervisor, lease, factory = _resident(manager)
    holder.update(supervisor=supervisor, token=lease.token)

    with pytest.raises(SessionLeaseLost, match="before turn admission"):
        _start(supervisor, lease.token)

    member = supervisor._members["athr_source"].members["athr_source"]
    assert factory.transports[-1].turns == []
    assert member.turn_lock.locked() is False
    assert _census_threads() == []


def test_a_kata_census_failure_opens_no_observation():
    """Under Kata the census comes first, so its failure leaves nothing to close."""

    class Manager(_Manager):
        def capture_member_processes(self, spec, **kwargs):
            raise SandboxError("member process census failed: exec refused")

    manager = Manager()
    manager.backend.runtime = KATA_RUNTIME
    supervisor, lease, factory = _resident(manager)
    manager.events.clear()

    with pytest.raises(SandboxError, match="member process census failed"):
        _start(supervisor, lease.token)

    assert manager.events == []
    member = supervisor._members["athr_source"].members["athr_source"]
    assert member.turn_lock.locked() is False
    assert factory.transports[-1].turns == []


def test_run_beside_returns_both_results():
    assert run_beside(lambda: "census", lambda: "fence", name=_CENSUS_THREAD) == (
        "census",
        "fence",
    )


def test_run_beside_joins_its_worker_before_a_foreground_failure_propagates():
    finished = threading.Event()
    started = threading.Event()

    def background() -> str:
        started.set()
        # Still running when the foreground fails: the join must wait for it.
        threading.Event().wait(0.05)
        finished.set()
        return "census"

    def foreground() -> str:
        assert started.wait(5)
        raise RuntimeError("fence failed")

    with pytest.raises(RuntimeError, match="fence failed"):
        run_beside(background, foreground, name=_CENSUS_THREAD)

    assert finished.is_set()
    assert _census_threads() == []


def test_run_beside_raises_the_workers_failure_chained_to_the_foregrounds():
    def background() -> str:
        raise SandboxError("member process census failed")

    def foreground() -> str:
        raise RuntimeError("fence failed")

    with pytest.raises(SandboxError, match="census failed") as raised:
        run_beside(background, foreground, name=_CENSUS_THREAD)

    assert isinstance(raised.value.__context__, RuntimeError)
    assert _census_threads() == []


def test_run_beside_runs_both_in_turn_when_no_thread_can_start(monkeypatch):
    """A host out of threads arms the turn as before rather than failing it."""
    order: list[str] = []

    def refuse(_thread):
        raise RuntimeError("can't start new thread")

    monkeypatch.setattr(threading.Thread, "start", refuse)

    result = run_beside(
        lambda: order.append("census") or "census",
        lambda: order.append("fence") or "fence",
        name=_CENSUS_THREAD,
    )

    assert result == ("census", "fence")
    assert order == ["census", "fence"]
