"""The turn transition table is exactly what the store writes.

``domain/execution.py`` records which changes of ``agent_jobs.state`` the
store's statements make. This file lists every statement that writes the
column, with the changes it can make read off its SQL and the checks its
caller makes first, holds the table to exactly their union, and checks the
list against the source, as ``test_sandbox_group_transitions.py`` does for
sandbox groups.
"""

from __future__ import annotations

from collections import Counter

from tests.support.sql_writes import writes

from cloud_agent.domain.execution import (
    CANCELLED,
    FAILED,
    INITIAL_STATE,
    JOB_STATE_TRANSITIONS,
    QUEUED,
    RUNNING,
    SUCCEEDED,
    TERMINAL_STATES,
    job_state_change_allowed,
)

CLAIMS = "repositories/attempt_commands/claims.py"
LIFECYCLE = "repositories/attempt_commands/lifecycle.py"
REAPER = "repositories/attempt_commands/reaper.py"
TERMINAL = frozenset(TERMINAL_STATES)
#: The reaper's job writes test no state; it skips a terminal job first.
UNFINISHED = frozenset({QUEUED, RUNNING})

#: ``(module, anchor, from states, to states)`` for every statement that
#: writes ``state``. An anchor with several statements is listed once each.
SITES = (
    # Each of these is ``WHERE ... state = 'queued'``.
    ("_sql/goals.py", "CANCEL_QUEUED_GOAL_ROUNDS_SQL", {QUEUED}, {CANCELLED}),
    ("_sql/jobs.py", "fail_unrunnable_follow_ups", {QUEUED}, {FAILED}),
    ("_sql/jobs.py", "fail_discontinuous_follow_ups", {QUEUED}, {FAILED}),
    ("repositories/conversation_commands.py", "delete_sessions", {QUEUED}, {CANCELLED}),
    ("repositories/goals.py", "sweep_goal_scheduling", {QUEUED}, {CANCELLED}),
    ("repositories/owner_work.py", "end_owner_work", {QUEUED}, {CANCELLED}),
    # ``request_cancel`` cancels a queued turn; a running one is only flagged.
    (LIFECYCLE, "request_cancel", {QUEUED}, {CANCELLED}),
    # The claim selects a claimable, that is queued, turn.
    (CLAIMS, "claim_job", {QUEUED}, {RUNNING}),
    # ``WHERE ... state = 'running'``: back to the queue, or cancelled.
    (CLAIMS, "release_claim", {RUNNING}, {QUEUED, CANCELLED}),
    # Every caller passes ``from_states=(RUNNING,)``; the worker's finish
    # refuses a state that is not terminal, and the others fail the turn.
    (LIFECYCLE, "_transition_job", {RUNNING}, TERMINAL),
    # The reaper: an indeterminate or settled-but-unfinished turn (two
    # statements), a restore out of attempts, a cancel, a spent budget, a retry.
    (REAPER, "_reap_attempt", UNFINISHED, {CANCELLED, FAILED}),
    (REAPER, "_reap_attempt", UNFINISHED, {CANCELLED, FAILED}),
    (REAPER, "_reap_attempt", UNFINISHED, {FAILED}),
    (REAPER, "_reap_attempt", UNFINISHED, {CANCELLED}),
    (REAPER, "_reap_attempt", UNFINISHED, {FAILED}),
    (REAPER, "_reap_attempt", UNFINISHED, {QUEUED}),
)

#: The statements that create a turn: ``(module, anchor, the states it may
#: name)``. ``create_job`` leaves the column to its default, ``queued``.
INSERTS = {
    ("repositories/conversation_commands.py", "create_job"): {QUEUED},
    ("repositories/conversation_commands.py", "insert_follow_up"): {QUEUED},
    ("repositories/conversation_commands.py", "_create_goal_round_on"): {QUEUED},
    # A Fork copies its source's turns, each in the terminal state it reached
    # (an unfinished one as cancelled).
    ("repositories/session_fork.py", "finalize_session_fork"): TERMINAL,
}


def test_the_table_is_the_union_of_the_writes() -> None:
    table: dict[str, set[str]] = {state: set() for state in JOB_STATE_TRANSITIONS}
    for _module, _anchor, sources, targets in SITES:
        for source in sources:
            table[source] |= set(targets) - {source}
    assert {state: frozenset(targets) for state, targets in table.items()} == (
        JOB_STATE_TRANSITIONS
    )


def test_the_sites_are_every_statement_that_writes_the_state() -> None:
    found = writes("agent_jobs", ("state",))
    updates = Counter(
        (write.module, write.anchor) for write in found if write.verb == "UPDATE" and write.columns
    )
    assert updates == Counter((module, anchor) for module, anchor, _, _ in SITES)
    assert {(write.module, write.anchor) for write in found if write.verb == "INSERT"} == set(
        INSERTS
    )


def test_a_turn_starts_queued_or_settled() -> None:
    assert INITIAL_STATE == QUEUED
    assert set().union(*INSERTS.values()) == {QUEUED, *TERMINAL}


def test_a_settled_turn_never_changes_state() -> None:
    assert all(JOB_STATE_TRANSITIONS[state] == frozenset() for state in TERMINAL)
    assert job_state_change_allowed(RUNNING, SUCCEEDED)
    assert not job_state_change_allowed(QUEUED, SUCCEEDED)
    assert not job_state_change_allowed(CANCELLED, QUEUED)
