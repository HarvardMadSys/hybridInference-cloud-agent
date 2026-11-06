"""The sandbox group transition tables are exactly what the store writes.

``domain/sandbox_group.py`` records, for ``agent_sandboxes.state`` and
``deletion_state``, which changes the store's statements make. The record is
only worth something if it follows the statements, so this file lists every
statement that writes either column, with the changes it can make read off its
SQL and the checks its caller makes first, and holds the tables to exactly
their union: a change no statement makes is not in a table, and none a
statement makes is missing.

The list is checked against the source too. Each statement is named by its
module and its anchor (the constant it is, or the function it is written in),
and a statement that starts or stops writing either column, or moves, fails
here until the list says what it does.
"""

from __future__ import annotations

from collections import Counter
from typing import TYPE_CHECKING, NamedTuple

from tests.support.sql_writes import writes

from cloud_agent.domain.sandbox_group import (
    DELETION_STATE_TRANSITIONS,
    GROUP_STATE_TRANSITIONS,
    INITIAL_DELETION_STATE,
    INITIAL_GROUP_STATE,
    DeletionState,
    GroupState,
    deletion_state_change_allowed,
    group_state_change_allowed,
)

if TYPE_CHECKING:
    from collections.abc import Iterable

P, S, R = GroupState.PROVISIONING, GroupState.STARTING, GroupState.RUNNING
STOPPING, STOPPED, DIRTY = GroupState.STOPPING, GroupState.STOPPED, GroupState.STOPPED_DIRTY
LOST, COLD, RESTORING = GroupState.LOST, GroupState.COLD, GroupState.RESTORING
ANY = frozenset(GroupState)

NONE, PENDING, ARCHIVED = DeletionState.NONE, DeletionState.PENDING, DeletionState.ARCHIVED
DELETED, FAILED = DeletionState.DELETED, DeletionState.FAILED

TRANSITIONS = "_sql/sandbox_transitions.py"


class Site(NamedTuple):
    """One statement, and the changes of one column it can make."""

    module: str
    anchor: str
    column: str
    #: ``(from states, to states)`` pairs; a statement whose result depends on
    #: the state it replaces has one pair per case.
    moves: tuple[tuple[frozenset[str], frozenset[str]], ...]


def _site(
    module: str, anchor: str, column: str, *moves: tuple[frozenset[str], frozenset[str]]
) -> Site:
    return Site(module, anchor, column, moves)


def _m(sources: Iterable[str], targets: Iterable[str]) -> tuple[frozenset[str], frozenset[str]]:
    """Each of ``sources`` may become any of ``targets``."""
    return frozenset(sources), frozenset(targets)


#: Every statement that writes ``state``, and what it can do.
STATE_SITES = (
    # A Host silent long enough hands a covered stopped group to the claimant.
    _site("_sql/hosts.py", "take_silent_host_group", "state", _m({STOPPED}, {COLD})),
    # A Host moving out releases its covered stopped groups.
    _site("_sql/hosts.py", "release_covered_sandboxes", "state", _m({STOPPED}, {COLD})),
    # ``WHERE state = 'restoring'``: back to cold, or lost once out of attempts.
    _site("_sql/sandbox.py", "release_failed_restore", "state", _m({RESTORING}, {LOST, COLD})),
    _site("_sql/sandbox.py", "release_unstarted_restore", "state", _m({RESTORING}, {COLD})),
    # The observation path returns early for provisioning (its own branch),
    # cold and restoring groups, and for a stop the database requested.
    _site(
        "_sql/sandbox_observations.py",
        "record_running_container",
        "state",
        _m({S, R, STOPPING, STOPPED, DIRTY, LOST}, {R}),
    ),
    # ``WHERE state IN (stopped, stopping, stopped_dirty, lost) OR (running,
    # past its idle deadline)``.
    _site(
        "_sql/sandbox_observations.py",
        "record_stopped_container",
        "state",
        _m({STOPPING, DIRTY, LOST, R}, {STOPPED}),
    ),
    # A stale running row the host proved stopped is repaired to stopped.
    _site(TRANSITIONS, "REQUEST_ADMIN_DELETION_SQL", "state", _m({R}, {STOPPED})),
    # ``_locked_admin_deletion`` requires a stopped group.
    _site(TRANSITIONS, "COMPLETE_ADMIN_SANDBOX_DELETION_GROUP_SQL", "state", _m({STOPPED}, {LOST})),
    # The reclaimed-host recovery reads no state, only the deletion request.
    _site(TRANSITIONS, "COMPLETE_ADMIN_SANDBOX_DISCARD_GROUP_SQL", "state", _m(ANY, {LOST})),
    # Admin, deleted-chat and move-out stops each require ``running``.
    _site(TRANSITIONS, "REQUEST_GROUP_STOP_SQL", "state", _m({R}, {STOPPING})),
    # A confirmed container start, fenced by generation and attempt, not state.
    _site(TRANSITIONS, "MARK_THREAD_SANDBOX_RUNNING_GROUP_AND_MEMBERS_SQL", "state", _m(ANY, {R})),
    _site(TRANSITIONS, "SETTLE_THREAD_SANDBOX_GROUP_SQL", "state", _m(ANY, {R})),
    _site(
        TRANSITIONS,
        "ABANDON_THREAD_SANDBOX_GROUP_SQL",
        "state",
        _m({RESTORING}, {LOST, COLD}),
        _m(ANY - {RESTORING}, {LOST, P, R, STOPPING}),
    ),
    # The reservation refuses lost, stopped_dirty, stopping and cold groups;
    # running and restoring keep their state.
    _site(TRANSITIONS, "RESERVE_ACTIVATE_GROUP_SQL", "state", _m({P, STOPPED}, {S})),
    _site(
        TRANSITIONS, "STOP_ORPHANED_PROVISIONING_GROUP_SQL", "state", _m({P}, {STOPPING, STOPPED})
    ),
    # Provisioning, cold and restoring groups returned earlier; lost and
    # stopped_dirty keep their state.
    _site(TRANSITIONS, "STOP_LOST_ONLY_GROUP_SQL", "state", _m({S, R, STOPPED}, {STOPPING})),
    _site(TRANSITIONS, "RECORD_SESSION_FORK_MATERIALIZED_GROUP_SQL", "state", _m(ANY, {R})),
    _site(TRANSITIONS, "CLAIM_COLD_GROUP_FOR_RESTORE_SQL", "state", _m({COLD}, {RESTORING})),
    # A turn's end and an expired lease decide by container and peers, not state.
    _site(TRANSITIONS, "TRANSITION_GROUP_SQL", "state", _m(ANY, {LOST, R, STOPPING})),
    _site(TRANSITIONS, "REAP_ATTEMPT_GROUP_SQL", "state", _m(ANY, {LOST, R, STOPPING})),
    # ``UNMOVED_GROUP_SQL`` excludes cold, restoring and lost groups.
    _site(
        TRANSITIONS, "LOSE_RECLAIMED_GROUP_SQL", "state", _m(ANY - {COLD, RESTORING, LOST}, {LOST})
    ),
)

#: Every statement that writes ``deletion_state``, and what it can do.
DELETION_SITES = (
    # A failed request is resumable only with its archive, so it returns to
    # archived; an exhausted pending or archived one keeps its state.
    _site(TRANSITIONS, "RESUME_ADMIN_DELETION_SQL", "deletion_state", _m({FAILED}, {ARCHIVED})),
    # Requests in progress, resumable ones and finished ones return earlier.
    _site(
        TRANSITIONS, "REQUEST_ADMIN_DELETION_SQL", "deletion_state", _m({NONE, FAILED}, {PENDING})
    ),
    _site(
        TRANSITIONS,
        "RECORD_ADMIN_SANDBOX_ARCHIVE_GROUP_SQL",
        "deletion_state",
        _m({PENDING}, {ARCHIVED}),
    ),
    _site(
        TRANSITIONS,
        "COMPLETE_ADMIN_SANDBOX_DELETION_GROUP_SQL",
        "deletion_state",
        _m({ARCHIVED}, {DELETED}),
    ),
    _site(
        TRANSITIONS,
        "COMPLETE_ADMIN_SANDBOX_DISCARD_GROUP_SQL",
        "deletion_state",
        _m({PENDING}, {DELETED}),
    ),
    # A retry or a stopped retry keeps the state; anything else fails it.
    _site(
        TRANSITIONS,
        "FAIL_ADMIN_SANDBOX_DELETION_GROUP_SQL",
        "deletion_state",
        _m({PENDING, ARCHIVED}, {FAILED}),
    ),
)

#: The statements that create a group, leaving both columns to their defaults.
INSERT_SITES = {(TRANSITIONS, "INSERT_SANDBOX_GROUP_SQL")}


def _union(sites: tuple[Site, ...], values: frozenset[str]) -> dict[str, frozenset[str]]:
    table: dict[str, set[str]] = {value: set() for value in values}
    for site in sites:
        for sources, targets in site.moves:
            for source in sources:
                table[source] |= targets - {source}
    return {value: frozenset(targets) for value, targets in table.items()}


def test_the_state_table_is_the_union_of_the_state_writes() -> None:
    assert _union(STATE_SITES, ANY) == GROUP_STATE_TRANSITIONS


def test_the_deletion_table_is_the_union_of_the_deletion_writes() -> None:
    assert _union(DELETION_SITES, frozenset(DeletionState)) == DELETION_STATE_TRANSITIONS


def test_the_sites_are_every_statement_that_writes_either_column() -> None:
    found = writes("agent_sandboxes", ("state", "deletion_state"))
    listed = Counter(
        (site.module, site.anchor, site.column) for site in (*STATE_SITES, *DELETION_SITES)
    )
    written = Counter(
        (write.module, write.anchor, column)
        for write in found
        if write.verb == "UPDATE"
        for column in write.columns
    )
    assert written == listed
    inserts = {(write.module, write.anchor) for write in found if write.verb == "INSERT"}
    assert inserts == INSERT_SITES
    assert all(not write.columns for write in found if write.verb == "INSERT"), (
        "a group insert now names its state; record it as an initial state here"
    )


def test_a_new_group_starts_where_the_column_defaults_put_it() -> None:
    assert (INITIAL_GROUP_STATE, INITIAL_DELETION_STATE) == (P, NONE)


def test_nothing_moves_a_group_into_stopped_dirty_or_out_of_deleted() -> None:
    assert all(DIRTY not in targets for targets in GROUP_STATE_TRANSITIONS.values())
    assert DELETION_STATE_TRANSITIONS[DELETED] == frozenset()


def test_the_predicates_read_the_tables() -> None:
    assert group_state_change_allowed(COLD, COLD)
    assert group_state_change_allowed(COLD, RESTORING)
    assert not group_state_change_allowed(RESTORING, P)
    assert deletion_state_change_allowed(PENDING, ARCHIVED)
    assert not deletion_state_change_allowed(ARCHIVED, PENDING)
