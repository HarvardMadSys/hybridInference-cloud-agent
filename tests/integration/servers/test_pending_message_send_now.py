"""Sending a waiting message into the running turn, over the real store and routes.

The owner's route, the store and the worker's finish are real; only the host's
broker is a stand-in, answering the way the broker does: the turn took the
message, or it did not and says why. What these cases pin is what the platform
does with each answer — where the message ends up, and that it is never sent
twice or lost.

The database is required (``-m dbtest``); a missing one fails, never skips.
"""

from __future__ import annotations

import json
from typing import Any

import pytest
from tests.integration.servers.test_pending_message_queue import (
    _HOST,
    _IMAGE_DIGEST,
    _OWNER,
    Api,
    api as api,
    pool as pool,
)

from cloud_agent.api import turns
from cloud_agent.domain.pending_messages import SEND_NOW_IN_FLIGHT_S
from cloud_agent.workspace_broker_client import WorkspaceBrokerError
from cloud_agent_shared.shared_protocol import (
    SHARED_SANDBOX_CAPABILITY,
    mid_turn_input_capability,
)

pytestmark = [pytest.mark.dbtest, pytest.mark.asyncio]


class _Broker:
    """The host's answer to one hand-over, and a record of what it was asked."""

    def __init__(self, answer: Any = None, *, during: Any = None) -> None:
        self.answer = {"accepted": True, "reason": None} if answer is None else answer
        #: Something that happens on the host while the hand-over is in flight.
        self.during = during
        self.calls: list[tuple[str, str, str]] = []

    async def inject_shared_input(self, thread_id: str, *, owner: str, text: str) -> Any:
        self.calls.append((thread_id, owner, text))
        if self.during is not None:
            await self.during()
        if isinstance(self.answer, Exception):
            raise self.answer
        return self.answer


def _route_input_to(monkeypatch, broker: _Broker | None) -> None:
    async def resolve(_host_pool: Any, _attempt_id: int) -> _Broker | None:
        return broker

    monkeypatch.setattr(turns, "broker_for_attempt", resolve)


async def _steerable(api: Api) -> None:
    """The host now says it can hand a message to a running claude-code turn."""
    await api.store.host_pool.record_host_heartbeat(
        host=_HOST,
        capabilities=[SHARED_SANDBOX_CAPABILITY, mid_turn_input_capability("claude-code")],
        sandbox_image_digest=_IMAGE_DIGEST,
    )


async def _send_now(api: Api, thread_id: str, message: dict[str, Any]) -> Any:
    return await api.client.post(
        f"/v1/agent/threads/{thread_id}/messages/{message['id']}/send-now",
        json={"revision": message["revision"]},
    )


def _payload(value: Any) -> dict[str, Any]:
    return json.loads(value) if isinstance(value, str) else dict(value)


async def _mid_turn_rows(api: Api, job_id: str) -> tuple[list[dict], list[dict]]:
    async with api.pool.acquire() as conn:
        events = await conn.fetch(
            "SELECT id, attempt_id, seq, payload FROM agent_job_events "
            "WHERE job_id = $1 AND event_type = 'user_message' ORDER BY id",
            job_id,
        )
        rows = await conn.fetch(
            "SELECT role, content, mid_turn, source_event_id FROM agent_thread_messages "
            "WHERE job_id = $1 ORDER BY id",
            job_id,
        )
    return [dict(row) for row in events], [dict(row) for row in rows]


async def _waiting_behind_a_running_turn(api: Api, prompt: str = "also check the docs"):
    job = await api.create()
    thread_id = job["thread_id"]
    running = await api.start(job)
    sent = await api.send(thread_id, prompt)
    assert sent.status_code == 201, sent.text
    return job, thread_id, running, sent.json()["pending"]


async def test_a_message_sent_now_joins_the_running_turn_and_is_not_sent_again(api, monkeypatch):
    await _steerable(api)
    broker = _Broker()
    _route_input_to(monkeypatch, broker)
    job, thread_id, running, waiting = await _waiting_behind_a_running_turn(api)
    polled = await api.client.get(f"/v1/agent/threads/{thread_id}/messages")
    assert polled.json()["mid_turn_input_supported"] is True
    assert (await api.client.get(f"/v1/agent/jobs/{job['id']}")).json()[
        "mid_turn_input_supported"
    ] is True

    sent = await _send_now(api, thread_id, waiting)

    assert sent.status_code == 200, sent.text
    body = sent.json()
    assert body["delivered_to_job_id"] == job["id"]
    assert (body["pending"], body["job"], body["not_delivered_reason"]) == (None, None, None)
    # Handed to the attempt that was running, and to nobody else.
    assert broker.calls == [(thread_id, f"attempt:{running['attempt_id']}", "also check the docs")]
    assert (await api.queue(thread_id))["messages"] == []
    events, rows = await _mid_turn_rows(api, job["id"])
    assert len(events) == 1 and events[0]["attempt_id"] == running["attempt_id"]
    assert _payload(events[0]["payload"]) == {
        "text": "also check the docs",
        "pending_message_id": waiting["id"],
    }
    assert [(row["role"], row["content"], row["mid_turn"]) for row in rows] == [
        ("user", "fix it", False),
        ("user", "also check the docs", True),
    ]
    assert rows[1]["source_event_id"] == events[0]["id"]
    # The transcript reads it inside the turn it joined, marked as such.
    messages = (await api.thread(job["id"]))["messages"]
    assert [(message["content"], message["mid_turn"]) for message in messages] == [
        ("fix it", False),
        ("also check the docs", True),
    ]
    # Recording the same delivery again finds nothing left to record.
    again = await api.store.pending_messages.finish_send_now(
        message_id=waiting["id"],
        revision=waiting["revision"] + 1,
        job_id=job["id"],
        attempt_id=running["attempt_id"],
        delivered=True,
    )
    assert again == {"stale": True}
    assert await _mid_turn_rows(api, job["id"]) == (events, rows)

    # The turn ends: the message was delivered, so it is not a turn of its own.
    await api.finish(running)
    assert [turn["task_prompt"] for turn in await api.turns(thread_id)] == ["fix it"]


async def test_a_turn_that_cannot_take_it_now_leaves_it_to_go_when_the_turn_ends(api, monkeypatch):
    await _steerable(api)
    _route_input_to(monkeypatch, _Broker({"accepted": False, "reason": "no_boundary"}))
    job, thread_id, running, waiting = await _waiting_behind_a_running_turn(api)

    sent = await _send_now(api, thread_id, waiting)

    assert sent.status_code == 200, sent.text
    body = sent.json()
    assert (body["delivered_to_job_id"], body["not_delivered_reason"]) == (None, "no_boundary")
    # Back where it was: waiting, in line, at a newer revision.
    assert (body["pending"]["state"], body["pending"]["revision"]) == (
        "queued",
        waiting["revision"] + 2,
    )
    assert await _mid_turn_rows(api, job["id"]) == (
        [],
        [{"role": "user", "content": "fix it", "mid_turn": False, "source_event_id": None}],
    )

    await api.finish(running)
    assert [turn["task_prompt"] for turn in await api.turns(thread_id)] == [
        "fix it",
        "also check the docs",
    ]


@pytest.mark.parametrize(
    "broker",
    [None, _Broker(WorkspaceBrokerError(503, "The shared chat supervisor is unavailable."))],
    ids=["no-broker", "broker-down"],
)
async def test_a_host_out_of_reach_leaves_it_waiting(api, monkeypatch, broker):
    await _steerable(api)
    _route_input_to(monkeypatch, broker)
    _job, thread_id, _running, waiting = await _waiting_behind_a_running_turn(api)

    sent = await _send_now(api, thread_id, waiting)

    assert sent.status_code == 200, sent.text
    assert sent.json()["not_delivered_reason"] == "unreachable"
    assert [row["state"] for row in (await api.queue(thread_id))["messages"]] == ["queued"]


async def test_send_now_with_nothing_running_is_an_ordinary_send(api, monkeypatch):
    await _steerable(api)
    broker = _Broker()
    _route_input_to(monkeypatch, broker)
    job, thread_id, running, waiting = await _waiting_behind_a_running_turn(api, "after the stop")
    await api.client.post(f"/v1/agent/jobs/{job['id']}/cancel")
    await api.finish(running, "cancelled")
    assert (await api.queue(thread_id))["queue_paused_reason"] == "stopped"

    sent = await _send_now(api, thread_id, waiting)

    assert sent.status_code == 200, sent.text
    body = sent.json()
    # Nothing to steer: it is the next turn, and the owner's "now" lifted the pause.
    assert body["job"]["task_prompt"] == "after the stop"
    assert body["delivered_to_job_id"] is None
    assert broker.calls == []
    assert (await api.queue(thread_id))["queue_paused_reason"] is None


async def test_send_now_refuses_what_it_cannot_do_and_leaves_the_message_as_it_was(
    api, monkeypatch
):
    broker = _Broker()
    _route_input_to(monkeypatch, broker)

    # A turn that has not started: nothing to hand it to yet.
    queued_job = await api.create()
    queued_thread = queued_job["thread_id"]
    early = (await api.send(queued_thread, "too early")).json()["pending"]
    refused = await _send_now(api, queued_thread, early)
    assert refused.status_code == 409
    assert refused.json()["detail"]["error"]["type"] == "not_running"

    # A running turn on a host that cannot steer it.
    await api.start(queued_job)
    refused = await _send_now(api, queued_thread, early)
    assert refused.status_code == 409
    assert refused.json()["detail"]["error"]["type"] == "mid_turn_input_unsupported"

    await _steerable(api)
    # Uploads start a turn of their own.
    upload = await api.store.attachments.create_attachment(
        user_id=_OWNER, path="notes.txt", content=b"notes", content_type="text/plain"
    )
    with_file = (await api.send(queued_thread, "read this", attachment_ids=[upload["id"]])).json()[
        "pending"
    ]
    refused = await _send_now(api, queued_thread, with_file)
    assert refused.status_code == 409
    assert refused.json()["detail"]["error"]["type"] == "send_now_has_attachments"

    # A stale revision gets the message as it is now.
    stale = await _send_now(api, queued_thread, {**early, "revision": early["revision"] + 5})
    assert stale.status_code == 409
    assert stale.json()["detail"]["error"]["type"] == "revision_conflict"
    assert stale.json()["detail"]["error"]["current"]["id"] == early["id"]

    # A message being edited is the owner's to save first.
    held = (await api.edit(queued_thread, early, state="held")).json()["pending"]
    refused = await _send_now(api, queued_thread, held)
    assert refused.status_code == 409
    assert refused.json()["detail"]["error"]["type"] == "not_editable"

    assert broker.calls == []
    queue = await api.queue(queued_thread)
    assert [(row["prompt"], row["state"]) for row in queue["messages"]] == [
        ("too early", "held"),
        ("read this", "queued"),
    ]


async def test_a_turn_ending_while_the_message_is_handed_over_does_not_send_it_twice(
    api, monkeypatch
):
    await _steerable(api)
    job, thread_id, running, waiting = await _waiting_behind_a_running_turn(api)
    # The turn ends on the host while the hand-over is in flight; its finish
    # settles the queue, and must leave the in-flight message where it is.
    broker = _Broker(during=lambda: api.finish(running))
    _route_input_to(monkeypatch, broker)

    sent = await _send_now(api, thread_id, waiting)

    assert sent.status_code == 200, sent.text
    assert sent.json()["delivered_to_job_id"] == job["id"]
    assert [turn["task_prompt"] for turn in await api.turns(thread_id)] == ["fix it"]
    _events, rows = await _mid_turn_rows(api, job["id"])
    assert [row["content"] for row in rows] == ["fix it", "also check the docs"]
    assert (await api.queue(thread_id))["messages"] == []


async def test_a_turn_that_ended_before_taking_it_sends_it_next_at_once(api, monkeypatch):
    await _steerable(api)
    _job, thread_id, running, waiting = await _waiting_behind_a_running_turn(api)
    _route_input_to(
        monkeypatch,
        _Broker({"accepted": False, "reason": "turn_ended"}, during=lambda: api.finish(running)),
    )

    sent = await _send_now(api, thread_id, waiting)

    assert sent.status_code == 200, sent.text
    body = sent.json()
    assert body["not_delivered_reason"] == "turn_ended"
    # Back in line with nothing running: it is the next turn, now.
    assert body["job"]["task_prompt"] == "also check the docs"
    assert [turn["task_prompt"] for turn in await api.turns(thread_id)] == [
        "fix it",
        "also check the docs",
    ]


async def test_a_hand_over_stranded_by_a_crash_goes_back_in_line_when_the_turn_ends(api):
    await _steerable(api)
    _job, thread_id, running, waiting = await _waiting_behind_a_running_turn(api)
    began = await api.store.pending_messages.begin_send_now(
        thread_id=thread_id, user_id=_OWNER, message_id=waiting["id"], revision=waiting["revision"]
    )
    assert began["message"]["state"] == "sending"
    # The control plane that began it never came back to say how it went.
    async with api.pool.acquire() as conn:
        await conn.execute(
            "UPDATE agent_pending_messages "
            "SET send_now_requested_at = NOW() - make_interval(secs => $2) WHERE id = $1",
            waiting["id"],
            SEND_NOW_IN_FLIGHT_S + 60,
        )

    await api.finish(running)

    assert [turn["task_prompt"] for turn in await api.turns(thread_id)] == [
        "fix it",
        "also check the docs",
    ]
