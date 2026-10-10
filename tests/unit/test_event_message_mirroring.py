"""Recorded child activity must not become the conversation's assistant answer."""

from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from cloud_agent.storage._sql.events import insert_event


@pytest.mark.parametrize("truncated", [False, True])
async def test_subagent_reply_is_recorded_without_an_assistant_transcript_mirror(truncated):
    """Fallback transcript reads and goal reports receive only the main answer."""
    conn = SimpleNamespace(fetchval=AsyncMock(return_value=42), execute=AsyncMock())
    payload = {
        "text": "The child agent's findings",
        "parent_tool_use_id": "delegate-call",
        "usage_id": "child-message",
        "truncated": truncated,
    }
    event_id = await insert_event(
        conn, job_id="job", attempt_id=1, event_type="message", payload=payload
    )

    assert event_id == 42
    conn.fetchval.assert_awaited_once()
    arguments = conn.fetchval.await_args.args
    assert "INSERT INTO agent_job_events" in arguments[0]
    assert arguments[1:4] == ("job", 1, "message")
    assert json.loads(arguments[4]) == payload
    conn.execute.assert_not_awaited()


@pytest.mark.parametrize("parent", [None, "", 1, {"id": "invalid"}])
async def test_main_reply_keeps_its_transcript_mirror_without_a_recorded_parent(parent):
    """Missing or malformed parent metadata must not hide a main answer."""
    conn = SimpleNamespace(fetchval=AsyncMock(return_value=42), execute=AsyncMock())
    payload = {"text": "The main answer", "parent_tool_use_id": parent, "truncated": True}
    event_id = await insert_event(
        conn, job_id="job", attempt_id=1, event_type="message", payload=payload
    )

    assert event_id == 42
    assert conn.execute.await_count == 2
    mirror = conn.execute.await_args_list[0].args
    assert "INSERT INTO agent_thread_messages" in mirror[0]
    assert mirror[1:] == ("job", "The main answer", 42, True)
    assert "UPDATE agent_threads" in conn.execute.await_args_list[1].args[0]
