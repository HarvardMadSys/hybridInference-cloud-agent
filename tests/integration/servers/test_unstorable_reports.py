"""What a turn reports is stored even when it holds text Postgres cannot keep as sent.

A tool that prints NUL-separated output puts NUL characters in it, and a CLI
that cuts a string between the two halves of a surrogate pair leaves an
unpaired surrogate in its JSON. Both are ordinary, and Postgres keeps neither:
``text`` and ``jsonb`` refuse NUL, and a surrogate is not UTF-8 at all. These
drive the worker routes a runner calls, over the real store, with the bytes
such a turn sends.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

import pytest
from tests.integration.servers.test_pending_message_queue import (
    api as api,
    pool as pool,
)

if TYPE_CHECKING:
    from tests.integration.servers.test_pending_message_queue import Api

pytestmark = [pytest.mark.dbtest, pytest.mark.asyncio]

#: What a tool printed -- ``cat /proc/self/cmdline``, or a line a CLI cut in the
#: middle of an emoji -- and what is stored for it.
_OUTPUT = {
    "nul": ("cat\x00/proc/self/cmdline\x00", "cat\ufffd/proc/self/cmdline\ufffd"),
    "surrogate": ("build ok \ud83d", "build ok \ufffd"),
}


async def _post(api: Api, turn: dict[str, Any], path: str, body: dict[str, Any]) -> Any:
    """Send ``body`` as a runner's JSON encoder writes it, escapes and all."""
    return await api.client.post(
        f"/v1/agent/worker/jobs/{turn['job_id']}/{path}",
        content=json.dumps(body).encode(),
        headers={
            "Authorization": f"Bearer {turn['worker_token']}",
            "Content-Type": "application/json",
        },
    )


@pytest.mark.parametrize("output", ["nul", "surrogate"])
async def test_tool_output_and_a_message_holding_unstorable_text_are_stored(api: Api, output: str):
    printed, stored = _OUTPUT[output]
    job = await api.create()
    turn = await api.start(job)

    tool = await _post(
        api,
        turn,
        "events",
        {
            "event_type": "tool_result",
            "payload": {"tool_use_id": "toolu_1", "content": printed},
            "event_seq": 1,
        },
    )
    message = await _post(
        api,
        turn,
        "events",
        {"event_type": "message", "payload": {"text": f"It printed {printed}"}, "event_seq": 2},
    )

    assert tool.status_code == 201, tool.text
    assert message.status_code == 201, message.text
    events = (await api.client.get(f"/v1/agent/jobs/{job['id']}/events")).json()["events"]
    assert [event["payload"] for event in events[-2:]] == [
        {"tool_use_id": "toolu_1", "content": stored},
        {"text": f"It printed {stored}"},
    ]
    transcript = (await api.thread(job["id"]))["messages"]
    assert (transcript[-1]["role"], transcript[-1]["content"]) == (
        "assistant",
        f"It printed {stored}",
    )


@pytest.mark.parametrize("output", ["nul", "surrogate"])
async def test_a_failed_turn_ends_with_the_reason_its_sandbox_printed(api: Api, output: str):
    """A runner's reason quotes the CLI's last words, as it releases and then ends a turn.

    Refusing the reason refused the report: the activation stayed held and the
    turn sat on its lease until the reaper ended it, without that reason.
    """
    printed, stored = _OUTPUT[output]
    job = await api.create()
    turn = await api.start(job)

    abandoned = await _post(
        api,
        turn,
        "sandbox/abandon",
        {
            "sandbox_generation": turn["fence"]["sandbox_generation"],
            "activation_generation": turn["fence"]["activation_generation"],
            "lost": True,
            "detail": f"agent exited 1: {printed}",
        },
    )
    finished = await _post(
        api, turn, "finish", {"state": "failed", "detail": f"agent exited 1: {printed}"}
    )

    assert abandoned.status_code == 200, abandoned.text
    assert finished.status_code == 200, finished.text
    ended = (await api.client.get(f"/v1/agent/jobs/{job['id']}")).json()
    assert (ended["state"], ended["detail"]) == ("failed", f"agent exited 1: {stored}")
    async with api.pool.acquire() as conn:
        released = await conn.fetchrow(
            "SELECT active_attempt_id, last_error FROM agent_thread_sandbox_memberships "
            "WHERE thread_id = $1",
            job["thread_id"],
        )
    assert dict(released) == {
        "active_attempt_id": None,
        "last_error": f"agent exited 1: {stored}",
    }
