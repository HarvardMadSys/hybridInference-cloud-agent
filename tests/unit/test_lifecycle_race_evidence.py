"""Evidence for lifecycle races the A2 spec could not rule out (§4.4, §8).

Each case drives the real supervisor into the state the spec's invariant
forbids and asserts the invariant. They are strict xfails: they pass while the
race exists and fail, asking to be promoted to ordinary tests, once it is
fixed. Only the final invariant check may produce the expected failure (it
raises :class:`InvariantBroken`); any other failure means the race was not
reproduced and fails the test outright.
"""

from __future__ import annotations

import threading

import pytest
from tests.support.archive_policy import hooked_supervisor
from tests.unit.test_shared_session_supervisor import (
    SessionBusy,
    SharedChatSessionSupervisor,
    _Manager,
    _members,
    _spec,
)

_WAIT_S = 5.0


class InvariantBroken(AssertionError):
    """The invariant under test does not hold in the reached state."""


class _RaceManager(_Manager):
    """Hold one stop, and one inspect on one thread, until the test lets each go."""

    def __init__(self) -> None:
        super().__init__()
        self.hold_next_stop = False
        self.stop_entered = threading.Event()
        self.stop_release = threading.Event()
        self.inspect_thread: threading.Thread | None = None
        self.inspect_entered = threading.Event()
        self.inspect_release = threading.Event()

    def stop(self, spec, **kwargs):
        if self.hold_next_stop:
            self.hold_next_stop = False
            self.stop_entered.set()
            assert self.stop_release.wait(timeout=_WAIT_S)
        return super().stop(spec, **kwargs)

    def inspect(self, container_name):
        if self.inspect_thread is threading.current_thread():
            self.inspect_thread = None
            self.inspect_entered.set()
            assert self.inspect_release.wait(timeout=_WAIT_S)
        return super().inspect(container_name)


def _supervisor(manager: _Manager, archives: str) -> SharedChatSessionSupervisor:
    if archives == "disabled":
        return SharedChatSessionSupervisor(manager)
    # Final archives on, every hook succeeding: the competing stop finishes
    # its final archive and clears ``stop_archive_pending``.
    return hooked_supervisor(
        manager,
        prepare_final=lambda *_args: None,
        stopped=lambda *_args: None,
    )


@pytest.mark.xfail(
    strict=True,
    raises=InvariantBroken,
    reason=(
        "A2 §4.4 I5 / §8 Q2-Q3: a concurrent exact stop clears the failed initial "
        "archive's preservation and snapshot barrier (accept_inspection_locked); a "
        "restart lands before the capture re-inspects, and initial_archive_capture "
        "re-sets initial_archive_preservation_pending without the snapshot barrier"
    ),
)
@pytest.mark.parametrize("archives", ["disabled", "enabled"])
def test_preserved_initial_archive_keeps_its_snapshot_barrier_across_a_restart(
    archives: str,
) -> None:
    """I5: ``initial_archive_preservation_pending`` implies ``snapshotting``.

    1. The initial capture holds the snapshot barrier, froze the writers, and
       fails after its marker exists, so it must preserve the frozen bytes.
    2. A control-plane exact stop (``reason="orphaned_attempt"``) already owns
       ``stopping``; the capture's own fail-stop is refused as busy, and it
       waits for that stop to finish.
    3. That stop accepts the STOPPED inspection, which clears preservation and
       the snapshot barrier together (``accept_inspection_locked``).
    4. Before the capture re-inspects, a workspace read restarts the group:
       ``START`` and ``MUTATION`` read neither field.
    5. The capture sees the container running, keeps its barrier "unreleased",
       and writes ``initial_archive_preservation_pending = True`` -- with no
       snapshot barrier behind it.
    """
    manager = _RaceManager()
    supervisor = _supervisor(manager, archives)
    lease = supervisor.acquire(
        _spec(),
        thread_id="athr_source",
        owner="attempt:1",
        activation_generation=1,
        has_settled_context=False,
        manifest=_members(),
    )
    group = supervisor.state.members["athr_source"]
    capture_thread: threading.Thread | None = None
    collided = threading.Event()
    outcomes: dict[str, BaseException | object] = {}

    stop_if_idle = supervisor._lifecycle.stop_if_idle

    def observed_stop_if_idle(*args, **kwargs):
        try:
            return stop_if_idle(*args, **kwargs)
        except SessionBusy:
            if threading.current_thread() is capture_thread:
                # Arm before signalling: the capture's next inspect is the
                # one after the competing stop, which this test holds.
                manager.inspect_thread = capture_thread
                collided.set()
            raise

    supervisor._lifecycle.stop_if_idle = observed_stop_if_idle  # type: ignore[method-assign]

    def competing_stop() -> None:
        try:
            outcomes["stop"] = supervisor.stop_if_idle(
                "athr_source", generation=1, reason="orphaned_attempt"
            )
        except BaseException as exc:  # pragma: no cover - asserted below
            outcomes["stop"] = exc

    stop_thread = threading.Thread(target=competing_stop)

    def capture(*_args):
        # The bytes are frozen and the marker exists; the stop takes the group
        # and is held in Docker stop before this capture reports its failure.
        manager.initial_archive_marker = b"prepared-initial-marker"
        manager.hold_next_stop = True
        stop_thread.start()
        assert manager.stop_entered.wait(timeout=_WAIT_S)
        raise RuntimeError("initial upload failed")

    def run_capture() -> None:
        try:
            outcomes["capture"] = supervisor.capture_initial_archive(
                "athr_source", lease.token, capture=capture
            )
        except BaseException as exc:
            outcomes["capture"] = exc

    capture_thread = threading.Thread(target=run_capture)
    try:
        capture_thread.start()
        assert collided.wait(timeout=_WAIT_S), outcomes
        assert group.initial_archive_preservation_pending
        assert group.snapshotting
        assert group.stopping

        # Step 3: the competing stop finishes, clearing both fields.
        manager.stop_release.set()
        stop_thread.join(timeout=_WAIT_S)
        assert not stop_thread.is_alive()
        assert not isinstance(outcomes["stop"], BaseException), outcomes["stop"]
        assert manager.items[_spec().container_name].running is False
        assert group.initial_archive_preservation_pending is False
        assert group.snapshotting is False
        assert group.stop_archive_pending is False

        # Step 4: the capture is about to re-inspect; a workspace read restarts
        # the group first.
        assert manager.inspect_entered.wait(timeout=_WAIT_S)
        supervisor.operation_opened("athr_peer")
        assert manager.items[_spec().container_name].running is True
    finally:
        manager.stop_release.set()
        manager.inspect_release.set()
        if stop_thread.is_alive():
            stop_thread.join(timeout=_WAIT_S)
        capture_thread.join(timeout=_WAIT_S)
    assert not capture_thread.is_alive()
    assert isinstance(outcomes["capture"], SessionBusy), outcomes["capture"]

    # Step 5, the invariant.
    if group.initial_archive_preservation_pending and not group.snapshotting:
        raise InvariantBroken(
            "initial_archive_preservation_pending is set on a running group whose "
            "snapshot barrier is gone: writers are admitted while preservation "
            "claims to hold them off"
        )


@pytest.mark.xfail(
    strict=True,
    raises=InvariantBroken,
    reason=(
        "A2 §4.4 U3 / §8 Q4: join_run_locked labels a run found going after a restart "
        "with a request's fence without comparing it to sealed_seq. Fail-closed today: "
        "the label is below the control plane's counter, so its checkpoint never covers"
    ),
)
def test_a_group_found_running_is_never_labelled_at_or_below_its_sealed_checkpoint() -> None:
    """U3: while the group runs, ``start_seq`` is ``None`` or above ``sealed_seq``.

    The control plane takes a fence (``fence_sandbox_start``) and only then
    sends the request; nothing orders delivery with the fences taken after it.
    So: requests fenced 3 and 5 leave; 5 arrives, runs, and its stop's
    checkpoint names 5; fence 6 starts the group again; the sandbox manager
    restarts with the container still running and finds the group with
    ``sealed_seq = 5`` (``checkpoint_seq`` of the kept receipt) and no label.
    The request fenced 3 arrives now. A stopped group would refuse it
    (``_begin_run_locked``: ``StaleStartFence``); this running one takes 3 as
    its label.
    """
    manager = _Manager()
    labels: list[int | None] = []
    supervisor = hooked_supervisor(
        manager,
        stopped=lambda _spec, *_args, start_seq: labels.append(start_seq),
        checkpoint_seq=lambda _spec, _inspection: 5,
    )
    # Found going after the restart: registered from the running container.
    supervisor.register_manifest(_spec(), _members())
    group = supervisor.state.members["athr_source"]
    assert manager.items[_spec().container_name].running
    assert group.sealed_seq == 5
    assert group.start_seq is None

    token, _ = supervisor.operation_opened("athr_source", start_seq=3)
    running_label = group.start_seq
    supervisor.operation_closed("athr_source", token)
    assert supervisor.stop_if_idle("athr_source", generation=1)
    # Today the stop's checkpoint names 3. The control plane's counter is at
    # least 6, and a receipt covers the group only when it names the counter,
    # so this receipt covers nothing: the violation is fail-closed.
    assert labels in ([3], [None])

    if running_label is not None and running_label <= group.sealed_seq:
        raise InvariantBroken(
            f"a running group was labelled {running_label}, at or below the "
            f"sealed checkpoint fence {group.sealed_seq}"
        )
