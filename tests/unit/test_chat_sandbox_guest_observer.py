"""The guest observer of each retained Kata container: one per guest, never shared.

Its helper is a singleton inside each guest, so starting or rotating it waits
for that guest's open cursors -- rotation only ever happens with no turn
observed -- while another container's guest never waits on it.
"""

from __future__ import annotations

import threading
import time
from dataclasses import replace

from tests.unit.test_chat_sandbox import _plain_group_network, _spec  # noqa: F401 - autouse fixture

from cloud_agent_host.chat_sandbox import MEMBER_UID_BASE, ChatSandboxManager
from cloud_agent_host.guest_observer import (
    GUEST_OBSERVER_ROTATE_BYTES,
    GuestObservation,
    GuestObserverCursor,
    GuestObserverSnapshot,
    GuestObserverStatus,
)
from cloud_agent_host.sandbox import KATA_RUNTIME, ContainerBackend


def test_retained_guest_journal_rotates_only_without_active_turns(monkeypatch):
    manager = ChatSandboxManager(ContainerBackend(image="registry/agent:1", runtime=KATA_RUNTIME))

    class Controller:
        def __init__(self):
            self.pid = 91
            self.size = 0
            self.starts = 0
            self.restart_choices: list[bool] = []

        def snapshot(self, **_kwargs):
            return GuestObserverSnapshot(
                status=GuestObserverStatus(self.pid, 1, 0, 0, 0, self.size),
                journal_size=self.size,
                guest_mono_ns=1,
            )

        def start(self):
            self.starts += 1
            self.pid += 1
            self.size = 0

        def rotate_if_needed(self, *, threshold):
            if self.size < threshold:
                return False
            self.start()
            return True

        def begin(
            self,
            *,
            user_uid,
            restart_if_missing=True,
            rotate_threshold=None,
            measurement_id=None,
        ):
            self.restart_choices.append(restart_if_missing)
            # The real controller decides rotation on its opening read.
            if rotate_threshold is not None:
                self.rotate_if_needed(threshold=rotate_threshold)
            return GuestObserverCursor(
                uid=user_uid,
                observer_pid=self.pid,
                offset=self.size,
                available=True,
                measurement_id=measurement_id,
            )

        def finish(self, _cursor):
            return GuestObservation(
                source="kata-guest-ebpf-v1",
                complete=True,
                lost_events=0,
                truncated=False,
            )

    controller = Controller()
    monkeypatch.setattr(manager, "_guest_observer_controller", lambda _spec, **_kwargs: controller)

    first = manager.begin_guest_observation(_spec(), user_uid=MEMBER_UID_BASE)
    controller.size = GUEST_OBSERVER_ROTATE_BYTES
    second = manager.begin_guest_observation(_spec(), user_uid=MEMBER_UID_BASE + 1)
    assert controller.starts == 0

    manager.finish_guest_observation(_spec(), first)
    manager.finish_guest_observation(_spec(), second)
    third = manager.begin_guest_observation(_spec(), user_uid=MEMBER_UID_BASE)

    assert controller.starts == 1
    assert controller.restart_choices == [True, False, True]
    assert third.observer_pid == 92


def test_one_containers_observer_begin_never_waits_for_anothers(monkeypatch):
    """Each guest's observer lifecycle is serialized, and only that guest's."""
    manager = ChatSandboxManager(ContainerBackend(image="registry/agent:1", runtime=KATA_RUNTIME))
    held = replace(_spec(), thread_id="athr_held", sandbox_id="asbx_held")
    other = replace(_spec(), thread_id="athr_other", sandbox_id="asbx_other")
    held_inside = threading.Event()
    sibling_calling = threading.Event()
    other_done = threading.Event()
    release = threading.Event()
    guard = threading.Lock()
    inside: dict[str, int] = {}
    most: dict[str, int] = {}

    class Controller:
        def __init__(self, spec):
            self.name = spec.container_name

        def begin(
            self,
            *,
            user_uid,
            restart_if_missing=True,
            rotate_threshold=None,
            measurement_id=None,
        ):
            with guard:
                inside[self.name] = inside.get(self.name, 0) + 1
                most[self.name] = max(most.get(self.name, 0), inside[self.name])
            try:
                if self.name == held.container_name:
                    held_inside.set()
                    release.wait(timeout=10)
                return GuestObserverCursor(uid=user_uid, observer_pid=91, available=True)
            finally:
                with guard:
                    inside[self.name] -= 1

        def finish(self, _cursor):
            return GuestObservation(
                source="kata-guest-ebpf-v1",
                complete=True,
                lost_events=0,
                truncated=False,
            )

    monkeypatch.setattr(
        manager, "_guest_observer_controller", lambda spec, **_kwargs: Controller(spec)
    )

    def other_turn():
        cursor = manager.begin_guest_observation(other, user_uid=MEMBER_UID_BASE)
        manager.finish_guest_observation(other, cursor)
        other_done.set()

    def sibling_turn():
        sibling_calling.set()
        manager.begin_guest_observation(held, user_uid=MEMBER_UID_BASE + 1)

    threads = [
        threading.Thread(
            target=manager.begin_guest_observation,
            args=(held,),
            kwargs={"user_uid": MEMBER_UID_BASE},
        ),
        threading.Thread(target=other_turn),
        threading.Thread(target=sibling_turn),
    ]
    try:
        threads[0].start()
        assert held_inside.wait(timeout=5)
        threads[1].start()
        # Another guest's turn opens and closes while this begin holds its own.
        assert other_done.wait(timeout=5), "another container's begin waited for this one"
        threads[2].start()
        assert sibling_calling.wait(timeout=5)
        time.sleep(0.1)
    finally:
        release.set()
        for thread in threads:
            if thread.ident is not None:
                thread.join(timeout=5)

    # A fork sibling on the held guest waited for it: one lifecycle per guest.
    assert most == {held.container_name: 1, other.container_name: 1}
