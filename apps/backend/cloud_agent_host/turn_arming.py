"""What a member's prompt is fenced by, armed just before the prompt is written.

Each of these is a ``docker exec`` into the member's container, paid by every
turn, follow-ups included:

- the member-process census, the baseline the turn's end reaps against;
- under runc, for Claude Code, the tool-mark fence: where the hook-mark file
  ends, so the turn reads back only its own marks;
- under Kata, the guest observer's fence and the member window opened on it.

The census and the tool-mark fence read different things, as different users,
and neither can see the other, so they run side by side. The guest observer's
fence cannot share the census's time: the census runs as the member's own UID,
and one still running when the fence is read would be journaled inside the
turn's slice and counted in its window, billed to the member as a process it
ran. Under Kata the census therefore ends before the fence is taken, as it
always has. Either way, everything here completes before the supervisor
rechecks the turn's admission, and so before the prompt is written.
"""

from __future__ import annotations

import contextlib
import threading
import uuid
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING, Any, TypeVar

from cloud_agent_host.chat_sandbox_models import MEMBER_UID_BASE
from cloud_agent_host.guest_observer import GuestObserverCursor
from cloud_agent_host.member_resources import MemberWindowSampler
from cloud_agent_host.member_window_census import MemberWindow, window_boot_id
from cloud_agent_host.sandbox import KATA_RUNTIME
from cloud_agent_shared.turn_resources import parse_cpu_capacity

if TYPE_CHECKING:
    from collections.abc import Callable

    from cloud_agent_host.chat_sandbox import ChatSandboxManager
    from cloud_agent_host.chat_sandbox_models import ChatSandboxSpec
    from cloud_agent_host.member_resources import MemberWindowHandle
    from cloud_agent_host.tool_windows import ToolMarkReader

_Background = TypeVar("_Background")
_Foreground = TypeVar("_Foreground")


@dataclass
class TurnFences:
    """What one member turn armed before its prompt was written."""

    #: The member's processes alive before the prompt: what the turn's end keeps.
    activation_processes: tuple[tuple[int, int], ...] = ()
    tool_mark_reader: ToolMarkReader | None = None
    observer_cursor: GuestObserverCursor | None = None
    window: MemberWindowHandle | None = None
    window_sampler: MemberWindowSampler | None = None

    def member_window(
        self, manager: ChatSandboxManager, spec: ChatSandboxSpec, *, checkpoint_interval_s: float
    ) -> MemberWindow | None:
        """The member window an admitted turn keeps, if this turn opened one."""
        if self.window is None or self.window_sampler is None:
            return None
        return MemberWindow.opened(
            self.window,
            self.window_sampler,
            cursor=self.observer_cursor,
            boot_id=window_boot_id(manager, spec, self.observer_cursor),
            checkpoint_interval_s=checkpoint_interval_s,
        )

    def close_unadmitted(self, manager: ChatSandboxManager, spec: ChatSandboxSpec) -> None:
        """Close what a turn opened before it was refused: its window, then its cursor.

        The window is the cursor's counterpart, a registration the observer
        still holds, and no turn is left to close either of them.
        """
        if self.window is not None and self.window_sampler is not None:
            with contextlib.suppress(Exception):
                self.window_sampler.close(self.window)
        if self.observer_cursor is not None:
            with contextlib.suppress(Exception):
                manager.finish_guest_observation(spec, self.observer_cursor)


def arm_turn(
    manager: ChatSandboxManager,
    spec: ChatSandboxSpec,
    *,
    slot_id: int,
    runtime_id: str,
    sample_interval_s: float,
) -> TurnFences:
    """Take the census and every fence one member's prompt needs.

    A failure closes whatever was already opened before it is raised, so a
    caller handed nothing has nothing to close.
    """
    fences = TurnFences()
    try:
        _arm(
            manager,
            spec,
            fences,
            slot_id=slot_id,
            runtime_id=runtime_id,
            sample_interval_s=sample_interval_s,
        )
    except BaseException:
        fences.close_unadmitted(manager, spec)
        raise
    return fences


def run_beside(
    background: Callable[[], _Background],
    foreground: Callable[[], _Foreground],
    *,
    name: str,
) -> tuple[_Background, _Foreground]:
    """Run ``background`` on a worker thread while ``foreground`` runs on this one.

    The worker is joined however the foreground ends, so nothing it does
    outlives the call. A failure of either is raised; when both fail, the
    worker's is, chained to the foreground's. A host with no thread to spare
    runs both here, in turn.
    """
    outcome: list[tuple[bool, Any]] = []

    def work() -> None:
        try:
            outcome.append((True, background()))
        except BaseException as exc:
            # Handed to the calling thread, which raises it below.
            outcome.append((False, exc))

    worker = threading.Thread(target=work, name=name, daemon=True)
    try:
        worker.start()
    except RuntimeError:
        return background(), foreground()
    try:
        value = foreground()
    finally:
        worker.join()
        succeeded, result = outcome[0]
        if not succeeded:
            raise result
    return result, value


def _arm(
    manager: ChatSandboxManager,
    spec: ChatSandboxSpec,
    fences: TurnFences,
    *,
    slot_id: int,
    runtime_id: str,
    sample_interval_s: float,
) -> None:
    """Fill ``fences`` in place, so a failure part way leaves what was opened."""
    uid = MEMBER_UID_BASE + slot_id

    def census() -> tuple[tuple[int, int], ...]:
        return manager.capture_member_processes(
            spec, slot_id=slot_id, user_uid=uid, user_gid=manager.backend.gid
        )

    kata = manager.backend.runtime == KATA_RUNTIME
    mark_reader_factory = getattr(manager, "tool_mark_reader", None)
    if mark_reader_factory is not None and runtime_id == "claude-code" and not kata:
        fences.activation_processes, fences.tool_mark_reader = run_beside(
            census, lambda: mark_reader_factory(spec), name="turn-member-census"
        )
        return
    fences.activation_processes = census()
    if kata:
        _open_guest_window(manager, spec, fences, uid=uid, sample_interval_s=sample_interval_s)


def _open_guest_window(
    manager: ChatSandboxManager,
    spec: ChatSandboxSpec,
    fences: TurnFences,
    *,
    uid: int,
    sample_interval_s: float,
) -> None:
    """Fence the guest observer's journal, then open the member window on that fence."""
    begin_observation = getattr(manager, "begin_guest_observation", None)
    if begin_observation is None:
        fences.observer_cursor = GuestObserverCursor.unavailable(
            uid, "sandbox manager has no guest observer client"
        )
        return
    # The window opens on the same fence as the turn: the measurement
    # identifier travels with the cursor so the observation's journal slice
    # contains the opening census as well as everything after it.
    measurement_id = uuid.uuid4().hex
    cursor = begin_observation(spec, user_uid=uid, measurement_id=measurement_id)
    fences.observer_cursor = cursor
    if not cursor.available:
        return
    sampler = MemberWindowSampler(
        run=lambda argv, timeout_s: manager.member_window_request(spec, argv, timeout_s=timeout_s),
        interval_s=sample_interval_s,
    )
    fences.window_sampler = sampler
    fences.window = sampler.open(uid=uid, measurement_id=measurement_id, cursor=cursor)
    fences.window = replace(fences.window, cpu_capacity_cores=parse_cpu_capacity(spec.cpu_limit))


__all__ = ["TurnFences", "arm_turn", "run_beside"]
