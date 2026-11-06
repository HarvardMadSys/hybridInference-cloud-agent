"""Regression tests for lifecycle races the A2 spec could not rule out (§4.4, §8).

Each case drives the real supervisor into the interleaving that used to
reach the state the spec's invariant forbids, and asserts the invariant: the
final check raises :class:`InvariantBroken` if the race is back. They were
strict xfails until each race was fixed (A2 spec §9).
"""

from __future__ import annotations

import contextlib
import logging
import threading
from types import SimpleNamespace

import pytest
from tests.support.archive_policy import hooked_supervisor
from tests.unit.test_shared_session_supervisor import (
    SessionBusy,
    SessionLeaseLost,
    SharedChatSessionSupervisor,
    _Manager,
    _members,
    _spec,
)

from cloud_agent_shared.environment import BoundEnvironment

_WAIT_S = 5.0


def _released_by_stop(caplog: pytest.LogCaptureFixture) -> int:
    event = "sandbox_initial_archive_preservation_released_by_stop"
    return sum(getattr(record, "event", None) == event for record in caplog.records)


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


class _ThawingManager(_Manager):
    """Accept the preparation thaw's census option the core fake does not take."""

    def thaw_shared_writers(self, spec, *, require_exact_census: bool = True, **kwargs):
        del require_exact_census
        return super().thaw_shared_writers(spec, **kwargs)


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


@pytest.mark.parametrize("archives", ["disabled", "enabled"])
def test_preserved_initial_archive_keeps_its_snapshot_barrier_across_a_restart(
    archives: str, caplog: pytest.LogCaptureFixture
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
    caplog.set_level(logging.INFO, logger="cloud_agent_host.shared_session_supervisor")
    try:
        capture_thread.start()
        assert collided.wait(timeout=_WAIT_S), outcomes
        assert group.life.initial_preservation
        assert group.life.exclusive is not None
        assert group.life.stop is not None

        # Step 3: the competing stop finishes, clearing both fields.
        manager.stop_release.set()
        stop_thread.join(timeout=_WAIT_S)
        assert not stop_thread.is_alive()
        assert not isinstance(outcomes["stop"], BaseException), outcomes["stop"]
        assert manager.items[_spec().container_name].running is False
        assert group.life.initial_preservation is False
        assert group.life.exclusive is None
        assert group.life.final_archive_pending is False

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
    if group.life.initial_preservation and group.life.exclusive is None:
        raise InvariantBroken(
            "initial_archive_preservation_pending is set on a running group whose "
            "snapshot barrier is gone: writers are admitted while preservation "
            "claims to hold them off"
        )
    assert _released_by_stop(caplog) == 1


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
    assert group.life.run.sealed_seq == 5
    assert group.life.run.start_seq is None

    token, _ = supervisor.operation_opened("athr_source", start_seq=3)
    running_label = group.life.run.start_seq
    supervisor.operation_closed("athr_source", token)
    assert supervisor.stop_if_idle("athr_source", generation=1)
    # Before the fix the stop's checkpoint named 3 (fail-closed: the control
    # plane's counter is at least 6, and a receipt covers the group only when
    # it names the counter). The late request is no longer counted, so the
    # checkpoint names no fence.
    assert labels in ([3], [None])

    if running_label is not None and running_label <= group.life.run.sealed_seq:
        raise InvariantBroken(
            f"a running group was labelled {running_label}, at or below the "
            f"sealed checkpoint fence {group.life.run.sealed_seq}"
        )


def test_a_stopped_group_is_never_started_again_under_its_checkpointed_label() -> None:
    """A run started after a stop never carries the label that stop's checkpoint names.

    The attempt acquired under fence 4. The control plane takes it for
    orphaned and stops the group (``reason="orphaned_attempt"``): the stop
    clears the lease and its checkpoint names 4, which covers the group while
    nothing fences again (``COVERED_GROUP_SQL``: ``c.start_seq = g.start_seq``).
    The attempt's runner, late, asks to prepare its environment: it sends its
    lease as ``activation_owner`` and no fence header (``shared_live_attempt``
    ``_prepare_environment``), so ``start_seq`` is ``_SAME_RUN``
    (``group_environment.prepare_environment``). ``ensure_running`` starts the
    stopped container before the lease is checked, and ``start_group`` begins
    no run, so the restarted container still carries label 4.
    """
    manager = _ThawingManager()
    labels: list[int | None] = []
    supervisor = hooked_supervisor(
        manager,
        stopped=lambda _spec, *_args, start_seq: labels.append(start_seq),
    )
    lease = supervisor.acquire(
        _spec(),
        thread_id="athr_source",
        owner="attempt:1",
        activation_generation=1,
        has_settled_context=True,
        manifest=_members(),
        start_seq=4,
    )
    assert supervisor.stop_if_idle("athr_source", generation=1, reason="orphaned_attempt")
    assert labels == [4]
    name = _spec().container_name
    assert manager.items[name].running is False

    not_ready = {"binding_id": "bound", "container_id": "container-shared", "ready": False}
    scripts_run: list[str] = []
    preparer = SimpleNamespace(
        status=lambda _backend, _binding_id: not_ready,
        prepare=lambda _backend, environment, cancel: (
            scripts_run.append(environment.setup_script) or {"ready": True}
        ),
    )
    environment = BoundEnvironment(binding_id="bound", setup_script="install")
    with contextlib.suppress(SessionLeaseLost):
        supervisor.prepare_environment(
            "athr_source", environment, preparer, activation_owner=lease.token
        )

    label = supervisor.start_seq_for("asbx_shared")
    if manager.items[name].running and label in labels:
        raise InvariantBroken(
            f"the stopped group was started again under label {label}, which its "
            f"last stop's checkpoint already names; scripts run for the stale "
            f"activation: {scripts_run}"
        )


def test_an_initial_archives_own_fail_stop_releasing_its_fence_is_not_logged(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Only a release another stop made is logged; the capture's own is the expected one."""
    manager = _Manager()
    supervisor = _supervisor(manager, "enabled")
    lease = supervisor.acquire(
        _spec(),
        thread_id="athr_source",
        owner="attempt:1",
        activation_generation=1,
        has_settled_context=True,
        manifest=_members(),
    )

    def capture(*_args):
        manager.initial_archive_marker = b"prepared-initial-marker"
        raise RuntimeError("initial upload failed")

    caplog.set_level(logging.INFO, logger="cloud_agent_host.shared_session_supervisor")
    with pytest.raises(RuntimeError, match="initial upload failed"):
        supervisor.capture_initial_archive("athr_source", lease.token, capture=capture)

    group = supervisor.state.members["athr_source"]
    assert group.life.exclusive is None
    assert not group.life.initial_preservation
    assert _released_by_stop(caplog) == 0
