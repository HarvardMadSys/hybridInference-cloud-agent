"""A just-booted Kata guest's observer starts before its first prompt asks for it."""

from __future__ import annotations

import logging
import subprocess
import threading
import time
from dataclasses import replace
from typing import TYPE_CHECKING

import pytest
from tests.unit.test_guest_observer import _LocalObserver
from tests.unit.test_shared_session_supervisor import (
    SharedChatSessionSupervisor,
    _Manager,
    _members,
    _spec as _shared_spec,
)

from cloud_agent_host.chat_sandbox import ChatSandboxManager
from cloud_agent_host.chat_sandbox_models import (
    MEMBER_UID_BASE,
    ChatSandboxInspection,
    ChatSandboxSpec,
    ChatSandboxStatus,
)
from cloud_agent_host.guest_observer import (
    GUEST_OBSERVER_BINARY,
    GuestObserverCursor,
    GuestObserverRuntimeError,
)
from cloud_agent_host.guest_observer_warmup import warm_guest_observer
from cloud_agent_host.sandbox import KATA_RUNTIME, ContainerBackend

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path

_CONTAINER = "sha256:container-one"


def _spec() -> ChatSandboxSpec:
    return ChatSandboxSpec(
        thread_id="athr_warm",
        sandbox_id="asbx_warm",
        generation=1,
        runtime_id="claude-code",
    )


def _proven_manager(*, runtime: str = KATA_RUNTIME) -> ChatSandboxManager:
    """A manager whose last start proved ``_CONTAINER`` for ``_spec()``."""
    manager = ChatSandboxManager(ContainerBackend(image="registry/agent:1", runtime=runtime))
    spec = _spec()
    manager._proven.record(
        spec,
        ChatSandboxInspection(
            container_id=_CONTAINER,
            name=spec.container_name,
            status=ChatSandboxStatus.RUNNING,
            running=True,
            image="registry/agent:1",
            labels=spec.docker_labels(),
            mounts=(),
            networks=("agent-net",),
            sandbox_runtime=runtime,
        ),
    )
    return manager


class _Controller:
    """Stands in for the observer controller the manager binds to a container."""

    def __init__(self) -> None:
        self.locks: list[object] = []
        self.order: list[str] = []
        self.entered = threading.Event()
        self.release = threading.Event()
        self.release.set()
        self.failure: Exception | None = None
        self.held = False

    def ensure(self) -> None:
        lock = self.locks[-1]
        outside: list[bool] = []

        def try_lock() -> None:
            acquired = lock.acquire(blocking=False)
            outside.append(acquired)
            if acquired:
                lock.release()

        prober = threading.Thread(target=try_lock)
        prober.start()
        prober.join(5)
        self.held = outside == [False]
        self.entered.set()
        assert self.release.wait(5)
        if self.failure is not None:
            raise self.failure
        self.order.append("ensure")

    def begin(self, *, user_uid, restart_if_missing, rotate_threshold, measurement_id):
        self.order.append(f"begin:restart={restart_if_missing}")
        return GuestObserverCursor(uid=user_uid, observer_pid=91, available=True)


@pytest.fixture
def controller(monkeypatch) -> Iterator[_Controller]:
    fake = _Controller()

    def bind(_manager, _spec, *, lock=None):
        fake.locks.append(lock)
        return fake

    monkeypatch.setattr(ChatSandboxManager, "_guest_observer_controller", bind)
    yield fake


def test_the_observer_starts_on_a_daemon_thread_under_its_containers_lock(controller):
    manager = _proven_manager()

    thread = warm_guest_observer(manager, _spec(), _CONTAINER)

    assert thread is not None
    assert thread.daemon is True
    thread.join(5)
    assert controller.order == ["ensure"]
    # The controller can start or rotate the helper under that lock, which
    # the warm-up already holds while it asks.
    assert controller.locks[0] is not None
    assert controller.held is True


def test_a_runc_sandbox_has_no_observer_to_start(controller):
    manager = _proven_manager(runtime="runc")

    assert warm_guest_observer(manager, _spec(), _CONTAINER) is None
    assert controller.order == []


@pytest.mark.parametrize("proven", ["another", "none"])
def test_only_the_exact_proven_container_is_warmed(controller, proven):
    """A container replaced under the same name since the proof is left alone."""
    manager = _proven_manager()
    if proven == "none":
        manager._proven.forget(_spec())
    else:
        current = manager._proven.get(_spec())
        manager._proven.record(_spec(), replace(current, container_id="sha256:container-two"))

    thread = warm_guest_observer(manager, _spec(), _CONTAINER)
    thread.join(5)

    assert controller.order == []


def test_a_guest_with_an_open_cursor_is_never_restarted(controller):
    manager = _proven_manager()
    cursor = GuestObserverCursor(uid=MEMBER_UID_BASE, observer_pid=91, available=True)
    with manager._guest_observers.container(_spec().container_name) as observers:
        observers.admit(cursor)

    thread = warm_guest_observer(manager, _spec(), _CONTAINER)
    thread.join(5)

    assert controller.order == []


def test_the_first_prompt_waits_on_the_same_lock_for_the_start_to_finish(controller):
    manager = _proven_manager()
    controller.release.clear()
    warming = warm_guest_observer(manager, _spec(), _CONTAINER)
    assert controller.entered.wait(5)
    cursors: list[GuestObserverCursor] = []
    prompt = threading.Thread(
        target=lambda: cursors.append(
            manager.begin_guest_observation(_spec(), user_uid=MEMBER_UID_BASE)
        )
    )
    prompt.start()

    prompt.join(0.2)
    assert prompt.is_alive(), "the prompt's fence did not wait for the start"
    controller.release.set()
    warming.join(5)
    prompt.join(5)

    assert controller.order == ["ensure", "begin:restart=True"]
    assert cursors[0].available is True


def test_a_failed_start_is_logged_and_leaves_the_prompt_its_own(controller, caplog):
    """The first prompt's fence then starts the helper itself, as before."""
    manager = _proven_manager()
    controller.failure = GuestObserverRuntimeError("guest observer could not start")

    with caplog.at_level(logging.INFO, logger="cloud_agent_host.guest_observer_warmup"):
        warm_guest_observer(manager, _spec(), _CONTAINER).join(5)
    cursor = manager.begin_guest_observation(_spec(), user_uid=MEMBER_UID_BASE)

    assert [record.event for record in caplog.records] == ["guest_observer_warmup_failed"]
    assert controller.order == ["begin:restart=True"]
    assert cursor.available is True


def test_a_host_out_of_threads_leaves_the_start_to_the_prompt(controller, monkeypatch, caplog):
    """Acquire never fails for want of the early start."""
    manager = _proven_manager()

    def refuse(_thread):
        raise RuntimeError("can't start new thread")

    monkeypatch.setattr(threading.Thread, "start", refuse)

    with caplog.at_level(logging.INFO, logger="cloud_agent_host.guest_observer_warmup"):
        assert warm_guest_observer(manager, _spec(), _CONTAINER) is None

    assert [record.event for record in caplog.records] == ["guest_observer_warmup_failed"]
    assert controller.order == []


@pytest.fixture
def stopped_helper(tmp_path: Path) -> Iterator[_LocalObserver]:
    """A guest whose helper is not running yet: no status, and nothing publishing."""
    helper = _LocalObserver(tmp_path)
    helper.polling.clear()
    time.sleep(0.05)
    helper.status_path.unlink()
    try:
        yield helper
    finally:
        helper.close()


def test_a_warmed_guest_is_fenced_with_nothing_left_to_start(stopped_helper, monkeypatch):
    """The start runs the real controller's own path; the prompt finds it running.

    The prompt's fence is then only its two reads, the opening one and the
    fence after it, with no start between them.
    """
    manager = _proven_manager()
    monkeypatch.setattr(
        manager,
        "exec_capture",
        lambda _spec, argv, **kwargs: stopped_helper.capture(argv, kwargs["timeout_s"]),
    )
    privileged: list[list[str]] = []

    def start_helper(_spec, argv, *, detach=False, timeout_s=30):
        privileged.append(list(argv))
        if argv == [GUEST_OBSERVER_BINARY]:
            stopped_helper.polling.set()
        return subprocess.CompletedProcess(args=argv, returncode=0, stdout="", stderr="")

    monkeypatch.setattr(manager, "_observer_privileged_exec", start_helper)

    warm_guest_observer(manager, _spec(), _CONTAINER).join(10)
    started = len(privileged)
    stopped_helper.execs.clear()
    cursor = manager.begin_guest_observation(_spec(), user_uid=MEMBER_UID_BASE)

    assert privileged[-1] == [GUEST_OBSERVER_BINARY]
    assert cursor.available, cursor.detail
    assert len(stopped_helper.execs) == 2
    assert len(privileged) == started


class _WarmingManager(_Manager):
    def __init__(self) -> None:
        super().__init__()
        self.warmed: list[tuple[str, str]] = []

    def warm_guest_observer(self, spec, container_id: str) -> None:
        self.warmed.append((spec.container_name, container_id))


@pytest.mark.parametrize(
    ("pre_existing", "running", "warmed"),
    [
        # A new group: this activation creates, starts and proves the guest.
        (False, None, True),
        # A stopped group: this activation boots and proves it.
        (True, False, True),
        # A running group: its observer is whatever its own boot left.
        (True, True, False),
    ],
)
def test_acquire_warms_only_a_guest_it_booted(pre_existing, running, warmed):
    manager = _WarmingManager()
    spec = _shared_spec()
    if pre_existing:
        manager.items[spec.container_name] = manager._inspection(spec, running=running)
    supervisor = SharedChatSessionSupervisor(manager)

    lease = supervisor.acquire(
        spec,
        thread_id="athr_source",
        owner="attempt:1",
        activation_generation=1,
        has_settled_context=True,
        manifest=_members(),
    )

    expected = [(spec.container_name, lease.container_id)] if warmed else []
    assert manager.warmed == expected


def test_a_manager_without_the_hook_still_activates():
    manager = _Manager()
    supervisor = SharedChatSessionSupervisor(manager)

    lease = supervisor.acquire(
        _shared_spec(),
        thread_id="athr_source",
        owner="attempt:1",
        activation_generation=1,
        has_settled_context=True,
        manifest=_members(),
    )

    assert lease.container_action == "create"
