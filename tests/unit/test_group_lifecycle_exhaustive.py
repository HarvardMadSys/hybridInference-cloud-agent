"""Every lifecycle state a group can be in, enumerated (A2 spec section 7.2).

The state space of ``GroupLifecycleState`` is finite once its holders are
reduced to their kinds: 92,160 states. Three checks run over it:

* admission parity: for each state, and each ``Operation``, the admission
  decided from the new state is the one the flags it replaced decided.
  ``_OLD_HOLDS`` below is a frozen copy of the blocker predicates as they read
  those flags before the state existed; it is the reference, and is never to
  be changed to follow the code;
* invariant closure: no transition from a state that satisfies the proven
  invariants leaves one that breaks them, unless its precondition refuses it;
* reachability: from an idle group, every caller's steps taken in every order,
  each only where its admission lets it through, reach exactly the
  combinations of operations in progress that section 4.2 lists.
"""

from __future__ import annotations

import itertools
import threading
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any, Final

import pytest

from cloud_agent_host.container.chat_sandbox import (
    ChatSandboxInspection,
    ChatSandboxSpec,
    ChatSandboxStatus,
)
from cloud_agent_host.session_lifecycle import StaleStartFence
from cloud_agent_host.supervisor.group_lifecycle import ADMISSION, Blocker, Operation
from cloud_agent_host.supervisor.group_lifecycle_state import (
    START,
    ExclusiveHolder,
    FinalArchive,
    GroupLifecycleState,
    Hold,
    LifecycleInvariantError,
    OrphanFinalization,
    StartOutcome,
    StopHolder,
)
from cloud_agent_host.supervisor.shared_session_state import _Group, _Member

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator

#: Stands for the lifecycle check a group may hold.
_CHECK: Final[Any] = object()

# The blocker predicates over the flags, frozen as they were before
# GroupLifecycleState replaced the flags (commit a4b4ef436).


def _old_is_quiescent(group: Any, *, allow_lifecycle_check: bool = False) -> bool:
    return (
        (allow_lifecycle_check or group.lifecycle_check is None)
        and not group.snapshotting
        and not group.starting
        and not group.stopping
        and not group.operation_tokens
        and not group.terminal_tokens
        and all(
            member.lease_token is None and member.current_turn is None
            for member in group.members.values()
        )
    )


_OLD_HOLDS: Final[dict[Blocker, Callable[[Any, bool], bool]]] = {
    Blocker.LIFECYCLE_CHECK: lambda group, allow: group.lifecycle_check is not None and not allow,
    Blocker.LIFECYCLE_CHECK_OUTSIDE_STOP: lambda group, allow: (
        group.lifecycle_check is not None and not allow and not group.stopping
    ),
    Blocker.SNAPSHOTTING: lambda group, _: group.snapshotting,
    Blocker.STARTING: lambda group, _: group.starting,
    Blocker.STOPPING: lambda group, _: group.stopping,
    Blocker.CONTAINER_LOST: lambda group, _: group.container_lost,
    Blocker.INITIAL_ARCHIVE_PRESERVATION: lambda group, _: (
        group.initial_archive_preservation_pending
    ),
    Blocker.STOP_ARCHIVE_PENDING: lambda group, _: group.stop_archive_pending,
    Blocker.STOP_ARCHIVE_BLOCKED: lambda group, _: group.stop_archive_blocked,
    Blocker.ORPHAN_FINALIZATION: lambda group, _: group.orphan_finalization_pending,
    Blocker.START_ARCHIVE_COMPLETION: lambda group, _: group.start_archive_completion_pending,
    Blocker.ARCHIVE_REQUEST: lambda group, _: group.archive_request_id is not None,
    Blocker.NOT_RUNNING: lambda group, _: not group.inspection.running,
    Blocker.NOT_QUIESCENT: lambda group, allow: (
        not _old_is_quiescent(group, allow_lifecycle_check=allow)
    ),
    # Read by the initial archive's hand-written drain wait before it was a row.
    Blocker.OPEN_MUTATIONS: lambda group, _: bool(group.operation_tokens or group.terminal_tokens),
}


def _old_initial_archive_wait(group: Any) -> str | None:
    """The initial archive's first wait, as ``capture_initial_archive`` wrote it."""
    return (
        "shared workspace start is in progress"
        if group.starting
        else "shared sandbox lifecycle check is in progress"
        if group.lifecycle_check is not None
        else None
    )


def _old_initial_archive_drain(group: Any) -> str | None:
    """The wait under its raised barrier, as ``capture_initial_archive`` wrote it."""
    return (
        "shared workspace start is in progress"
        if group.starting
        else "shared sandbox lifecycle check is in progress"
        if group.lifecycle_check is not None
        else "shared workspace still has active mutations"
        if group.operation_tokens or group.terminal_tokens
        else None
    )


def _old_refusal(group: Any, operation: Operation, allow: bool) -> tuple[type, str] | None:
    for refusal in ADMISSION[operation]:
        if any(_OLD_HOLDS[blocker](group, allow) for blocker in refusal.blockers):
            return refusal.error, refusal.message
    return None


def _old_flags(group: _Group) -> SimpleNamespace:
    """The flags ``group``'s state stands for, as the old fields held them."""
    life = group.life
    return SimpleNamespace(
        lifecycle_check=life.check,
        snapshotting=life.exclusive is not None,
        starting=life.start is not None,
        stopping=life.stop is not None,
        container_lost=life.container_lost,
        initial_archive_preservation_pending=life.initial_preservation,
        stop_archive_pending=life.final_archive is not None
        and life.final_archive.members is not None,
        stop_archive_blocked=life.final_archive is not None and life.final_archive.blocked,
        orphan_finalization_pending=life.orphan is not None,
        start_archive_completion_pending=life.start_completion_pending,
        archive_request_id=life.archive_request_id,
        inspection=group.inspection,
        operation_tokens=group.operation_tokens,
        terminal_tokens=group.terminal_tokens,
        members=group.members,
    )


# The enumeration (A2 spec section 7.2).

_STOPS: Final = (None, *StopHolder)
_EXCLUSIVES: Final = (None, *ExclusiveHolder)
_FINALS: Final = (
    None,
    FinalArchive(()),
    FinalArchive((), blocked=True),
    FinalArchive(None, blocked=True),
)
_ORPHANS: Final = (None, OrphanFinalization(), OrphanFinalization(marker_unreadable=True))


def _spec() -> ChatSandboxSpec:
    return ChatSandboxSpec(
        thread_id="athr_source",
        sandbox_id="asbx_shared",
        generation=1,
        runtime_id="claude-code",
        runtime_version="2.1.220",
        sandbox_image_digest="sha256:image",
        shared_live=True,
        resident_slot_capacity=4,
    )


def _inspection(spec: ChatSandboxSpec, *, running: bool) -> ChatSandboxInspection:
    return ChatSandboxInspection(
        container_id="container-shared",
        name=spec.container_name,
        status=ChatSandboxStatus.RUNNING if running else ChatSandboxStatus.STOPPED,
        running=running,
        image="sha256:image",
        labels=spec.docker_labels(),
        mounts=(),
        networks=("agent-net",),
    )


def _stop_hold(kind: StopHolder | None) -> Hold | None:
    if kind is None:
        return None
    return Hold(kind, "request" if kind is StopHolder.ARCHIVE_HOLD else None)


def every_state() -> Iterator[_Group]:
    """Each of the 92,160 states, written into one reused group in turn."""
    spec = _spec()
    inspections = {running: _inspection(spec, running=running) for running in (True, False)}
    condition = threading.Condition(threading.RLock())
    lives = {
        finals: GroupLifecycleState(condition, archives_finals=finals) for finals in (True, False)
    }
    group = _Group(spec=spec, inspection=inspections[True], life=lives[True])
    member = _Member(thread_id="athr_source")
    group.members[member.thread_id] = member
    for (
        start,
        stop,
        exclusive,
        check,
        failed,
        lost,
        preservation,
        completion,
        final,
        orphan,
        occupied,
        running,
        finals,
    ) in itertools.product(
        (False, True),
        _STOPS,
        _EXCLUSIVES,
        (False, True),
        (False, True),
        (False, True),
        (False, True),
        (False, True),
        _FINALS,
        _ORPHANS,
        (False, True),
        (False, True),
        (False, True),
    ):
        life = lives[finals]
        life.start = Hold(START) if start else None
        life.stop = _stop_hold(stop)
        life.exclusive = None if exclusive is None else Hold(exclusive)
        life.check = _CHECK if check else None
        life.last_start_failed = failed
        life.container_lost = lost
        life.initial_preservation = preservation
        life.start_completion_pending = completion
        life.final_archive = final
        life.orphan = orphan
        group.life = life
        group.inspection = inspections[running]
        member.lease_token = "lease" if occupied else None
        yield group


def test_the_enumeration_covers_the_92160_states() -> None:
    assert sum(1 for _ in every_state()) == 92_160


def test_every_state_admits_every_operation_as_the_flags_it_replaced_did() -> None:
    mismatches: list[tuple[Operation, bool, tuple[str, ...]]] = []
    for group in every_state():
        old = _old_flags(group)
        lifecycle = group.lifecycle
        for operation in Operation:
            for allow in (False, True):
                refused = lifecycle.refusal(operation, allow_lifecycle_check=allow)
                new = None if refused is None else (type(refused), str(refused))
                if new != _old_refusal(old, operation, allow):
                    mismatches.append((operation, allow, group.life.combination()))
    assert mismatches == []


@pytest.mark.parametrize("allow", [False, True])
def test_the_old_table_never_read_an_unreadable_marker_on_its_own(allow: bool) -> None:
    """``orphan_marker_unreadable`` alone, a state I3 rules out, refused no deletion."""
    group = next(every_state())
    old = _old_flags(group)
    old.orphan_marker_unreadable = True
    assert _old_refusal(old, Operation.DELETION, allow) is None


def test_the_initial_archive_waits_name_what_the_hand_written_waits_named() -> None:
    for group in every_state():
        for tokens in ({}, {"t": "athr_source"}):
            group.operation_tokens = dict(tokens)
            for terminals in ({}, {"t": "athr_source"}):
                group.terminal_tokens = dict(terminals)
                old = _old_flags(group)
                lifecycle = group.lifecycle
                assert lifecycle.busy_reason(
                    Operation.INITIAL_ARCHIVE_WAIT
                ) == _old_initial_archive_wait(old)
                assert lifecycle.busy_reason(
                    Operation.INITIAL_ARCHIVE_DRAIN
                ) == _old_initial_archive_drain(old)


# Invariant closure (A2 spec section 7.2, check 2): from every state that
# satisfies the proven invariants, every transition either is refused by its
# precondition or leaves a state that still satisfies them.

_MEMBERS: Final = ()


def _closure_calls() -> list[tuple[str, Callable[[GroupLifecycleState], object]]]:
    calls: list[tuple[str, Callable[[GroupLifecycleState], object]]] = [
        ("begin_start", lambda life: life.begin_start_locked()),
        ("begin_run", lambda life: life.begin_run_locked(5)),
        ("join_run", lambda life: life.join_run_locked(5)),
        ("owe_start_completion", lambda life: life.owe_start_completion_locked()),
        ("settle_start_completion", lambda life: life.settle_start_completion_locked()),
        ("end_stop", lambda life: life.end_stop_locked(life.stop)),
        ("end_stop/none", lambda life: life.end_stop_locked(None)),
        ("hold_archive", lambda life: life.hold_archive_locked("request")),
        ("release_archive", lambda life: life.release_archive_locked("request")),
        ("end_exclusive", lambda life: life.end_exclusive_locked(life.exclusive)),
        ("expect_exclusive", lambda life: life.expect_exclusive_locked(life.exclusive)),
        ("preserve_initial", lambda life: life.preserve_initial_locked()),
        ("release_initial", lambda life: life.release_initial_locked(life.exclusive)),
        ("accepted_stopped", lambda life: life.accepted_stopped_locked()),
        ("advance_generation", lambda life: life.advance_generation_locked()),
        ("begin_check", lambda life: life.begin_check_locked(object())),
        ("end_check", lambda life: life.end_check_locked(life.check)),
        ("mark_lost", lambda life: life.mark_lost_locked()),
        ("refresh_final_members", lambda life: life.refresh_final_members_locked(_MEMBERS)),
        ("marker_read", lambda life: life.marker_read_locked()),
        ("settle_orphan", lambda life: life.settle_orphan_locked()),
        ("settle_archive", lambda life: life.settle_archive_locked()),
    ]
    for outcome in StartOutcome:
        calls.append(
            (
                f"end_start/{outcome.value}",
                lambda life, o=outcome: life.end_start_locked(life.start, o),
            )
        )
    for kind in (StopHolder.STOP, StopHolder.FINALIZE, StopHolder.SHUTDOWN_RETRY):
        calls.append(
            (
                f"begin_stop/{kind.value}",
                lambda life, k=kind: life.begin_stop_locked(k, check=life.check),
            )
        )
    for holder in ExclusiveHolder:
        calls.append(
            (
                f"begin_exclusive/{holder.value}",
                lambda life, h=holder: life.begin_exclusive_locked(h),
            )
        )
    for members in (_MEMBERS, None):
        for blocked in (False, True):
            calls.append(
                (
                    f"owe_final/{members is not None}/{blocked}",
                    lambda life, m=members, b=blocked: life.owe_final_locked(m, blocked=b),
                )
            )
    for unreadable in (None, False, True):
        calls.append(
            (
                f"owe_orphan/{unreadable}",
                lambda life, u=unreadable: life.owe_orphan_locked(marker_unreadable=u),
            )
        )
    return calls


def _lifecycle_states() -> Iterator[tuple[Any, ...]]:
    """The 23,040 lifecycle parts of the enumeration: no occupant or inspection."""
    return itertools.product(
        (False, True),
        _STOPS,
        _EXCLUSIVES,
        (False, True),
        (False, True),
        (False, True),
        (False, True),
        (False, True),
        _FINALS,
        _ORPHANS,
        (False, True),
    )


def _build(condition: threading.Condition, parts: tuple[Any, ...]) -> GroupLifecycleState:
    start, stop, exclusive, check, failed, lost, preservation, completion, final, orphan, finals = (
        parts
    )
    return GroupLifecycleState(
        condition,
        archives_finals=finals,
        start=Hold(START) if start else None,
        stop=_stop_hold(stop),
        exclusive=None if exclusive is None else Hold(exclusive),
        check=_CHECK if check else None,
        initial_preservation=preservation,
        last_start_failed=failed,
        container_lost=lost,
        final_archive=final,
        orphan=orphan,
        start_completion_pending=completion,
    )


def test_no_transition_from_a_valid_state_breaks_a_proven_invariant() -> None:
    condition = threading.Condition(threading.RLock())
    calls = _closure_calls()
    valid = 0
    broken: list[tuple[str, tuple[str, ...]]] = []
    with condition:
        for parts in _lifecycle_states():
            if _build(condition, parts).check_invariants():
                continue
            valid += 1
            for name, call in calls:
                life = _build(condition, parts)
                try:
                    call(life)
                except LifecycleInvariantError:
                    broken.append((name, _build(condition, parts).combination()))
                except (AssertionError, StaleStartFence):
                    pass
    assert broken == []
    # 23,040 less those breaking I1 (2,880) or with a check-less stop (4,608), 576 both.
    assert valid == 16_128


# Reachable combinations (A2 spec section 7.2, check 3). Starting from an idle
# group, every caller's steps are taken in every order, each ``begin_*`` only
# where the admission the caller makes lets it through. The operations in
# progress that can then hold at once must be exactly section 4.2's table.

#: The lifecycle checks of the callers that take one.
_CHECKS: Final = {name: object() for name in ("stop", "reconcile", "archive", "commit")}
_CHECK_NAMES: Final = {id(check): name for name, check in _CHECKS.items()}

_Key = tuple[Any, ...]


def _key(group: _Group) -> _Key:
    life = group.life
    return (
        life.start is not None,
        None if life.stop is None else life.stop.kind,
        None if life.exclusive is None else life.exclusive.kind,
        None if life.check is None else _CHECK_NAMES[id(life.check)],
        life.last_start_failed,
        life.container_lost,
        life.initial_preservation,
        life.start_completion_pending,
        life.final_archive,
        life.orphan,
        group.operation_tokens.get("op") is not None,
    )


def _group_for(key: _Key, template: _Group) -> _Group:
    start, stop, exclusive, check, failed, lost, preservation, completion, final, orphan, busy = key
    life = template.life
    group = _Group(
        spec=template.spec,
        inspection=template.inspection,
        life=GroupLifecycleState(
            life.condition,
            archives_finals=life.archives_finals,
            start=Hold(START) if start else None,
            stop=_stop_hold(stop),
            exclusive=None if exclusive is None else Hold(exclusive),
            check=None if check is None else _CHECKS[check],
            initial_preservation=preservation,
            last_start_failed=failed,
            container_lost=lost,
            final_archive=final,
            orphan=orphan,
            start_completion_pending=completion,
        ),
        members=template.members,
        operation_tokens={"op": "athr_source"} if busy else {},
    )
    return group


def _admits(group: _Group, *operations: Operation, allow: bool = False) -> bool:
    return all(
        group.lifecycle.refusal(operation, allow_lifecycle_check=allow) is None
        for operation in operations
    )


def _steps(group: _Group) -> Iterator[Callable[[], object]]:
    """Each step a caller may take from ``group``'s state, as a call that changes it."""
    life = group.life
    owner = None if life.check is None else _CHECK_NAMES[id(life.check)]
    stop = None if life.stop is None else life.stop.kind
    exclusive = None if life.exclusive is None else life.exclusive.kind
    members = _MEMBERS if life.archives_finals else None
    # Anyone, at any time: a container loss, an accepted STOPPED inspection,
    # a workspace operation opening (admitted as a mutation) and closing.
    yield life.mark_lost_locked
    if not life.container_lost:
        yield life.accepted_stopped_locked
    if not group.operation_tokens and _admits(group, Operation.MUTATION):
        yield lambda: group.operation_tokens.update(op="athr_source")
    if group.operation_tokens:
        yield group.operation_tokens.clear
    # A start (start_group), and what it does while it holds the start.
    if (
        life.start is None
        and not life.container_lost
        and _admits(group, Operation.START_WAIT, Operation.START)
    ):
        yield life.begin_start_locked
    if life.start is not None:
        yield life.owe_start_completion_locked
        yield life.settle_start_completion_locked
        yield lambda: life.owe_final_locked(members)
        yield life.settle_archive_locked
        for outcome in (StartOutcome.COMPLETED, StartOutcome.FAILED):
            yield lambda o=outcome: life.end_start_locked(life.start, o)  # type: ignore[arg-type]
    # The exclusive workspace (prepare_environment, capture_initial_archive).
    if exclusive is None and _admits(group, Operation.MUTATION):
        for holder in ExclusiveHolder:
            yield lambda h=holder: life.begin_exclusive_locked(h)
    if exclusive is ExclusiveHolder.INITIAL_ARCHIVE:
        yield life.preserve_initial_locked
        yield lambda: life.release_initial_locked(life.exclusive)  # type: ignore[arg-type]
    if exclusive is not None:
        yield lambda: life.end_exclusive_locked(life.exclusive)  # type: ignore[arg-type]
    # A stop (stop_if_idle): admitted, it checks the group, then stops it.
    if (
        life.check is None
        and not life.container_lost
        and _admits(group, Operation.STOP, Operation.LIFECYCLE_CHECK)
    ):
        yield lambda: life.begin_check_locked(_CHECKS["stop"])
    # Reconciliation checks a group nothing starts, stops or checks.
    if life.check is None and life.start is None and life.stop is None:
        yield lambda: life.begin_check_locked(_CHECKS["reconcile"])
    if owner in ("stop", "reconcile"):
        kind = StopHolder.STOP if owner == "stop" else StopHolder.FINALIZE
        if stop is None:
            yield lambda: life.owe_orphan_locked(marker_unreadable=True)
            yield life.marker_read_locked
            yield life.settle_orphan_locked
            yield lambda: life.owe_final_locked(members, blocked=True)
            yield lambda: life.owe_final_locked(members)
            if _admits(group, Operation.TAKE_STOP):
                yield lambda k=kind: life.begin_stop_locked(k, check=life.check)
            yield lambda: life.end_check_locked(life.check)  # type: ignore[arg-type]
        if stop is kind:
            yield lambda: life.owe_final_locked(members, blocked=True)
            yield lambda: life.owe_orphan_locked()
            yield life.settle_orphan_locked
            yield life.settle_archive_locked
            yield lambda: life.end_stop_locked(life.stop)
    # The shutdown's final archive retry.
    if life.final_archive_pending and _admits(group, _SHUTDOWN_TAKE_STOP):
        yield lambda: life.begin_stop_locked(StopHolder.SHUTDOWN_RETRY)
    if stop is StopHolder.SHUTDOWN_RETRY:
        yield lambda: life.owe_final_locked(members, blocked=True)
        yield life.settle_archive_locked
        yield lambda: life.end_stop_locked(life.stop)
    # An archive request: begin, then abort or commit.
    held_for_request = life.archive_request_id == "request"
    if life.check is None and _admits(
        group,
        Operation.DELETION if held_for_request else Operation.ARCHIVE,
        Operation.LIFECYCLE_CHECK,
    ):
        yield lambda: life.begin_check_locked(_CHECKS["archive"])
    if owner == "archive":
        yield lambda: life.hold_archive_locked("request")
        yield lambda: life.end_check_locked(life.check)  # type: ignore[arg-type]
    if held_for_request and _admits(group, Operation.LIFECYCLE_CHECK):
        yield lambda: life.release_archive_locked("request")
    commit_admitted = (
        held_for_request or life.container_lost or _admits(group, Operation.IDLE_DELETION)
    )
    if life.check is None and commit_admitted and _admits(group, Operation.LIFECYCLE_CHECK):
        yield lambda: life.begin_check_locked(_CHECKS["commit"])
    if owner == "commit":
        deletion = Operation.DELETION if held_for_request else Operation.IDLE_DELETION
        if _admits(group, deletion, allow=True):
            # The group is removed right after: nothing follows this hold.
            yield lambda: life.hold_archive_locked("request")
        yield lambda: life.end_check_locked(life.check)  # type: ignore[arg-type]


#: The admission the shutdown's final archive retry makes.
_SHUTDOWN_TAKE_STOP: Final = Operation.TAKE_STOP


def _in_progress(key: _Key) -> str:
    start, stop, exclusive, check = key[:4]
    names = [
        name
        for name, held in (("S", start), ("T", stop), ("N", exclusive), ("C", check))
        if held not in (False, None)
    ]
    return "+".join(names) or "idle"


def reachable_combinations(*, archives_finals: bool) -> dict[str, bool]:
    """Each combination of operations in progress reached, and whether only with a lost container."""
    spec = _spec()
    condition = threading.Condition(threading.RLock())
    template = _Group(
        spec=spec,
        inspection=_inspection(spec, running=True),
        life=GroupLifecycleState(condition, archives_finals=archives_finals),
    )
    template.members["athr_source"] = _Member(thread_id="athr_source")
    seen = {_key(template)}
    frontier = [_key(template)]
    with condition:
        while frontier:
            key = frontier.pop()
            for index in itertools.count():
                group = _group_for(key, template)
                steps = list(itertools.islice(_steps(group), index, index + 1))
                if not steps:
                    break
                try:
                    steps[0]()
                except AssertionError as refused:
                    if isinstance(refused, LifecycleInvariantError):
                        raise
                    continue
                reached = _key(group)
                if reached not in seen:
                    seen.add(reached)
                    frontier.append(reached)
    only_lost: dict[str, bool] = {}
    for key in seen:
        combination = _in_progress(key)
        lost = key[5]
        only_lost[combination] = only_lost.get(combination, True) and lost
    return only_lost


#: The operations in progress that can hold at once whatever the configuration
#: (section 4.2), each reached with the container identity still known.
_ALWAYS: Final = dict.fromkeys(("idle", "S", "T", "N", "C", "S+N", "N+C", "T+C", "T+N+C"), False)
#: Reached only once the container identity is lost: an archive commit then
#: checks the group without an admission that refuses a start.
_LOST: Final = dict.fromkeys(("S+C", "S+N+C"), True)
#: Reached only through the shutdown's final archive retry, whose admission
#: refuses neither a start nor a check (section 4.1). With no final archives
#: configured, nothing is owed for it to retry. Step 4 closes the starts.
_SHUTDOWN_RETRY: Final = {"T+N": False, "S+T": False, "S+T+N": False}
_SHUTDOWN_RETRY_LOST: Final = dict.fromkeys(("S+T+C", "S+T+N+C"), True)

REACHABLE: Final = {
    True: {**_ALWAYS, **_LOST, **_SHUTDOWN_RETRY, **_SHUTDOWN_RETRY_LOST},
    False: {**_ALWAYS, **_LOST},
}

#: Section 4.2's table of pairs: whether each can hold at once, and whether only
#: once the container is lost. Absent: unreachable.
PAIRS: Final = {
    "S+N": False,
    "T+N": False,
    "N+C": False,
    "T+C": False,
    "S+C": True,
    # Only through the shutdown's retry; section 4.1 rules it out by the
    # shutdown order, which step 4 replaces with the admission.
    "S+T": False,
}


def _pairs(reached: dict[str, bool]) -> dict[str, bool]:
    pairs: dict[str, bool] = {}
    for combination, only_lost in reached.items():
        held = [] if combination == "idle" else combination.split("+")
        for first, second in itertools.combinations(held, 2):
            pair = f"{first}+{second}"
            pairs[pair] = pairs.get(pair, True) and only_lost
    return pairs


@pytest.mark.parametrize("archives_finals", [True, False])
def test_the_reachable_combinations_are_the_specs(archives_finals: bool) -> None:
    reached = reachable_combinations(archives_finals=archives_finals)
    assert reached == REACHABLE[archives_finals]


def test_the_pairs_that_hold_at_once_are_section_4_2s() -> None:
    assert _pairs(reachable_combinations(archives_finals=True)) == PAIRS
