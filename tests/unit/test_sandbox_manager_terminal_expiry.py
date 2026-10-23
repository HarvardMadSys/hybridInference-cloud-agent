"""A terminal past its lifetime is the reaper's to close, never a request's.

A shortened lifetime closes the terminals already past it. A request -- a
keystroke, a listing or an attach anywhere on the host -- treats an expired
terminal as gone and leaves its teardown to the reaper, and a terminal whose
cleanup failed stays listed for another kill.
"""

from __future__ import annotations

import functools
import threading
import time

import pytest
from fastapi import HTTPException
from tests.unit.test_agent_sandbox_manager import (
    _finish_within,
    _listed_session,
    _RunningTerminalProcess,
    _TerminalManager,
    _TerminalProcess,
)

from cloud_agent_host.terminal_session import _TerminalSession


def test_a_shortened_terminal_lifetime_closes_terminals_already_past_it():
    terminals = _TerminalManager(idle_ttl_s=1800, hard_ttl_s=28800, max_sessions=8)
    process = _RunningTerminalProcess()
    session = _TerminalSession(
        terminal_id="term_old",
        process=process,
        workdir=None,
        rows=24,
        cols=80,
        now=time.monotonic() - 600,
        idle_ttl_s=None,
        on_closed=None,
    )
    session.start()
    terminals._sessions["athr_source"] = {"term_old": session}

    terminals.reap()
    assert "athr_source" in terminals._sessions, "ten minutes is inside half an hour"

    terminals.configure(idle_ttl_s=300, hard_ttl_s=28800, max_sessions=8)
    assert terminals._reap_interval() == 30.0
    terminals.reap()
    assert "athr_source" not in terminals._sessions
    assert process.killed


class _SlowToKillTerminalProcess(_RunningTerminalProcess):
    """A shell whose teardown waits to be released: a hangup, polling, then a kill."""

    def __init__(self) -> None:
        super().__init__()
        self.killing = threading.Event()
        self.release = threading.Event()

    def kill(self) -> None:
        self.killing.set()
        assert self.release.wait(5)
        super().kill()


def _started_session(terminals: _TerminalManager, workspace_id: str, process, tmp_path):
    session = _listed_session(terminals, workspace_id, process, tmp_path)
    session.start()
    return session


def test_a_keystroke_never_waits_on_an_expired_terminals_teardown(tmp_path):
    """Requests leave a terminal past its lifetime to the reaper, and treat it as gone.

    They used to close it themselves first, whichever workspace it was in: a
    hangup, up to three seconds of polling and a kill in front of a keystroke,
    a listing or an attach anywhere on the host.
    """
    terminals = _TerminalManager(idle_ttl_s=60, hard_ttl_s=3600, max_sessions=8)
    live = _started_session(terminals, "athr_source", _RunningTerminalProcess(), tmp_path)
    slow = _SlowToKillTerminalProcess()
    expired = _started_session(terminals, "athr_other", slow, tmp_path)
    expired.last_active_at -= 120

    try:
        controlled, listed, listed_expired = _finish_within(
            1,
            lambda: (
                terminals.get_for_control("athr_source", live.id),
                terminals.list("athr_source"),
                terminals.list("athr_other"),
            ),
        )
        for lookup in (terminals.get, terminals.get_for_control):
            with pytest.raises(HTTPException) as gone:
                _finish_within(1, functools.partial(lookup, "athr_other", expired.id))
            assert gone.value.status_code == 404
        assert not slow.killing.is_set(), "a request began another terminal's teardown"
    finally:
        slow.release.set()

    assert controlled is live
    assert [row["id"] for row in listed] == [live.id]
    assert listed_expired == []
    terminals.reap()
    assert slow.killed
    assert "athr_other" not in terminals._sessions
    terminals.delete("athr_source", live.id)


def test_a_terminal_whose_cleanup_failed_stays_listed_for_another_kill(tmp_path):
    """It still holds its process: hiding it would hide what a kill can retry."""
    terminals = _TerminalManager(idle_ttl_s=60, hard_ttl_s=3600, max_sessions=8)
    session = _listed_session(terminals, "athr_source", _TerminalProcess(), tmp_path)
    session.state = "cleanup_failed"
    session.last_active_at -= 120

    assert [row["state"] for row in terminals.list("athr_source")] == ["cleanup_failed"]
    assert terminals.get("athr_source", session.id) is session
