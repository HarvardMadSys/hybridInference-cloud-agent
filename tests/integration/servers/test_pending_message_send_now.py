"""Sending a waiting message into the live turn, over the real store and routes.

The owner's route, the store, the worker's event and finish routes are real;
only the host's sandbox manager is a stand-in, answering the way the sandbox manager does: the
turn has the message, or it cannot have it and says why. The host's report of
where the turn took it arrives as the runner sends it, a ``user_message`` event.
What these cases pin is what the platform does with each — where the message
ends up, and that it is never sent twice or lost.

The database is required (``-m dbtest``); a missing one fails, never skips.
"""

from __future__ import annotations

import asyncio
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

from cloud_agent.application import send_now as send_now_delivery
from cloud_agent.sandbox_manager_client import SandboxManagerError
from cloud_agent_shared.shared_protocol import (
    SHARED_SANDBOX_CAPABILITY,
    mid_turn_input_capability,
)

pytestmark = [pytest.mark.dbtest, pytest.mark.asyncio]


class _SandboxManager:
    """The host's answer to one hand-over, and a record of what it was asked."""

    def __init__(self, answer: Any = None, *, during: Any = None) -> None:
        self.answer = {"accepted": True, "reason": None} if answer is None else answer
        #: Something that happens on the host while the hand-over is in flight.
        self.during = during
        self.calls: list[tuple[str, str, str, str]] = []

    async def inject_shared_input(
        self, thread_id: str, *, owner: str, message_id: str, text: str
    ) -> Any:
        self.calls.append((thread_id, owner, message_id, text))
        if self.during is not None:
            await self.during()
        if isinstance(self.answer, Exception):
            raise self.answer
        return self.answer


class _ResponseLossSandboxManager:
    """A host receipt survives the control plane losing its first response."""

    def __init__(self) -> None:
        self.calls: list[str] = []
        self.native_steers: list[str] = []
        self._accepted: set[str] = set()

    async def inject_shared_input(
        self, _thread_id: str, *, owner: str, message_id: str, text: str
    ) -> dict[str, Any]:
        self.calls.append(message_id)
        if message_id not in self._accepted:
            self._accepted.add(message_id)
            self.native_steers.append(f"{owner}:{message_id}:{text}")
            raise SandboxManagerError(503, "response lost after acceptance")
        return {"accepted": True, "reason": None}


def _route_input_to(monkeypatch, sandbox_manager: Any | None) -> None:
    async def resolve(_host_pool: Any, _attempt_id: int) -> Any | None:
        return sandbox_manager

    monkeypatch.setattr(send_now_delivery, "sandbox_manager_for_attempt", resolve)


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


async def _report(
    api: Api, turn: dict[str, Any], event_type: str, payload: dict[str, Any], seq: int
) -> Any:
    """An event from the turn's stream, sent the way the runner sends it."""
    return await api.client.post(
        f"/v1/agent/worker/jobs/{turn['job_id']}/events",
        json={"event_type": event_type, "payload": payload, "event_seq": seq},
        headers={"Authorization": f"Bearer {turn['worker_token']}"},
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


async def test_the_turn_takes_it_before_its_next_tool_call_and_it_is_not_sent_again(
    api, monkeypatch
):
    await _steerable(api)
    sandbox_manager = _SandboxManager()
    _route_input_to(monkeypatch, sandbox_manager)
    job, thread_id, running, waiting = await _waiting_behind_a_running_turn(api)
    polled = await api.client.get(f"/v1/agent/threads/{thread_id}/messages")
    assert polled.json()["mid_turn_input_supported"] is True
    assert (await api.client.get(f"/v1/agent/jobs/{job['id']}")).json()[
        "mid_turn_input_supported"
    ] is True

    sent = await _send_now(api, thread_id, waiting)

    assert sent.status_code == 200, sent.text
    body = sent.json()
    # The turn has it; it stays ``sending`` until the turn says where it took it.
    assert (body["pending"]["state"], body["job"], body["not_delivered_reason"]) == (
        "sending",
        None,
        None,
    )
    # Given to the attempt that was running, and to nobody else.
    assert sandbox_manager.calls == [
        (thread_id, f"attempt:{running['attempt_id']}", waiting["id"], "also check the docs")
    ]
    async with api.pool.acquire() as conn:
        marker, handed_off = await conn.fetchrow(
            "SELECT send_now_requested_at, send_now_handed_off "
            "FROM agent_pending_messages WHERE id = $1",
            waiting["id"],
        )
    assert marker is not None
    assert handed_off is True
    assert [row["state"] for row in (await api.queue(thread_id))["messages"]] == ["sending"]
    assert await _mid_turn_rows(api, job["id"]) == (
        [],
        [{"role": "user", "content": "fix it", "mid_turn": False, "source_event_id": None}],
    )

    # The host reports it taken, in the turn's own stream.
    taken = {"text": "also check the docs", "pending_message_id": waiting["id"]}
    reported = await _report(api, running, "user_message", taken, seq=40)
    assert reported.status_code == 201, reported.text

    assert (await api.queue(thread_id))["messages"] == []
    events, rows = await _mid_turn_rows(api, job["id"])
    assert len(events) == 1 and events[0]["attempt_id"] == running["attempt_id"]
    assert _payload(events[0]["payload"]) == taken
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
    # The runner's retry of the same report records nothing more.
    again = await _report(api, running, "user_message", taken, seq=40)
    assert again.json()["event_id"] == reported.json()["event_id"]
    assert await _mid_turn_rows(api, job["id"]) == (events, rows)

    # The turn ends: the message was taken, so it is not a turn of its own.
    await api.finish(running)
    assert [turn["task_prompt"] for turn in await api.turns(thread_id)] == ["fix it"]


async def test_a_turn_that_answers_without_taking_it_sends_it_as_the_next_turn(api, monkeypatch):
    await _steerable(api)
    _route_input_to(monkeypatch, _SandboxManager())
    job, thread_id, running, waiting = await _waiting_behind_a_running_turn(api)
    assert (await _send_now(api, thread_id, waiting)).json()["pending"]["state"] == "sending"

    # No tool call came after it: the host never gave it to the model.
    await api.finish(running)

    assert [turn["task_prompt"] for turn in await api.turns(thread_id)] == [
        "fix it",
        "also check the docs",
    ]
    assert await _mid_turn_rows(api, job["id"]) == (
        [],
        [{"role": "user", "content": "fix it", "mid_turn": False, "source_event_id": None}],
    )


@pytest.mark.parametrize(
    ("sandbox_manager", "state", "reason", "revision_delta"),
    [
        (None, "queued", "unreachable", 3),
        (
            _SandboxManager(SandboxManagerError(503, "The shared chat supervisor is unavailable.")),
            "sending",
            None,
            2,
        ),
    ],
    ids=["no-sandbox-manager", "sandbox-manager-down"],
)
async def test_a_host_out_of_reach_leaves_it_waiting(
    api, monkeypatch, sandbox_manager, state, reason, revision_delta
):
    await _steerable(api)
    _route_input_to(monkeypatch, sandbox_manager)
    _job, thread_id, _running, waiting = await _waiting_behind_a_running_turn(api)

    sent = await _send_now(api, thread_id, waiting)

    assert sent.status_code == 200, sent.text
    assert sent.json()["not_delivered_reason"] == reason
    # No route means no side effect and can requeue. A lost sandbox manager response is
    # uncertain and stays leased until recovery can replay its message ID.
    (message,) = (await api.queue(thread_id))["messages"]
    assert (message["state"], message["revision"]) == (
        state,
        waiting["revision"] + revision_delta,
    )
    if state == "queued":
        assert "host was unreachable" in message["last_error"]
    else:
        assert message["last_error"] is None


async def test_send_now_with_nothing_running_is_an_ordinary_send(api, monkeypatch):
    await _steerable(api)
    sandbox_manager = _SandboxManager()
    _route_input_to(monkeypatch, sandbox_manager)
    job, thread_id, running, waiting = await _waiting_behind_a_running_turn(api, "after the stop")
    await api.client.post(f"/v1/agent/jobs/{job['id']}/cancel")
    await api.finish(running, "cancelled")
    assert (await api.queue(thread_id))["queue_paused_reason"] == "stopped"

    sent = await _send_now(api, thread_id, waiting)

    assert sent.status_code == 200, sent.text
    body = sent.json()
    # Nothing to steer: it is the next turn, and the owner's "now" lifted the pause.
    assert body["job"]["task_prompt"] == "after the stop"
    assert sandbox_manager.calls == []
    assert (await api.queue(thread_id))["queue_paused_reason"] is None


async def test_a_turn_no_host_has_taken_yet_gets_it_once_one_has(api, monkeypatch):
    monkeypatch.setattr(send_now_delivery, "HAND_OVER_POLL_S", 0.05)
    await _steerable(api)
    sandbox_manager = _SandboxManager()
    _route_input_to(monkeypatch, sandbox_manager)
    first = await api.create()
    thread_id = first["thread_id"]
    await api.run(first)
    second = (await api.send(thread_id, "next step")).json()["job"]
    waiting = (await api.send(thread_id, "and mind the tests")).json()["pending"]

    sent = await _send_now(api, thread_id, waiting)

    # The turn is still waiting for a host: nothing to give it to yet.
    assert sent.status_code == 200, sent.text
    assert sent.json()["pending"]["state"] == "sending"
    await asyncio.sleep(0.2)
    assert sandbox_manager.calls == []

    running = await api.start(second)
    await asyncio.wait_for(asyncio.gather(*list(send_now_delivery._hand_overs)), 10)

    assert sandbox_manager.calls == [
        (thread_id, f"attempt:{running['attempt_id']}", waiting["id"], "and mind the tests")
    ]
    assert [row["state"] for row in (await api.queue(thread_id))["messages"]] == ["sending"]


async def test_restart_recovers_a_handoff_committed_before_the_target_was_claimed(api, monkeypatch):
    await _steerable(api)
    sandbox_manager = _SandboxManager()
    _route_input_to(monkeypatch, sandbox_manager)
    first = await api.create()
    thread_id = first["thread_id"]
    await api.run(first)
    target = (await api.send(thread_id, "next step")).json()["job"]
    waiting = (await api.send(thread_id, "and mind the tests")).json()["pending"]
    began = await api.store.pending_messages.begin_send_now(
        thread_id=thread_id,
        user_id=_OWNER,
        message_id=waiting["id"],
        revision=waiting["revision"],
    )
    assert began["attempt_id"] is None
    # The process that committed begin_send_now disappears here.  No request
    # task survives to observe the later host claim.
    running = await api.start(target)

    recovered = await send_now_delivery.recover_handoffs(api.store)

    assert recovered == {"claimed": 1, "accepted": 1, "requeued": 0}
    assert sandbox_manager.calls == [
        (thread_id, f"attempt:{running['attempt_id']}", waiting["id"], "and mind the tests")
    ]
    async with api.pool.acquire() as conn:
        request_marker, handed_off = await conn.fetchrow(
            "SELECT send_now_requested_at, send_now_handed_off "
            "FROM agent_pending_messages WHERE id = $1",
            waiting["id"],
        )
    assert request_marker is not None
    assert handed_off is True
    assert await send_now_delivery.recover_handoffs(api.store) == {
        "claimed": 0,
        "accepted": 0,
        "requeued": 0,
    }
    assert len(sandbox_manager.calls) == 1


async def test_competing_workers_have_one_revision_lease_winner(api, monkeypatch):
    await _steerable(api)
    sandbox_manager = _SandboxManager()
    _route_input_to(monkeypatch, sandbox_manager)
    _job, thread_id, running, waiting = await _waiting_behind_a_running_turn(api)
    began = await api.store.pending_messages.begin_send_now(
        thread_id=thread_id,
        user_id=_OWNER,
        message_id=waiting["id"],
        revision=waiting["revision"],
    )
    revision = int(began["message"]["revision"])

    outcomes = await asyncio.gather(
        send_now_delivery.deliver_handoff(api.store, message_id=waiting["id"], revision=revision),
        send_now_delivery.deliver_handoff(api.store, message_id=waiting["id"], revision=revision),
    )

    assert sum(outcome.claimed for outcome in outcomes) == 1
    assert sum(outcome.accepted for outcome in outcomes) == 1
    assert sandbox_manager.calls == [
        (thread_id, f"attempt:{running['attempt_id']}", waiting["id"], "also check the docs")
    ]


async def test_late_attempt_owner_cannot_accept_or_refuse_after_the_job_moves(api):
    await _steerable(api)
    _job, thread_id, running, waiting = await _waiting_behind_a_running_turn(api)
    began = await api.store.pending_messages.begin_send_now(
        thread_id=thread_id,
        user_id=_OWNER,
        message_id=waiting["id"],
        revision=waiting["revision"],
    )
    claimed = await api.store.pending_messages.claim_send_now_handoff(
        message_id=waiting["id"],
        revision=int(began["message"]["revision"]),
        lease_seconds=send_now_delivery.HAND_OVER_LEASE_S,
    )
    assert claimed is not None
    claimed_revision = int(claimed["message"]["revision"])
    async with api.pool.acquire() as conn:
        await conn.execute(
            "UPDATE agent_jobs SET state = 'queued', current_attempt_id = NULL WHERE id = $1",
            began["job_id"],
        )

    accepted = await api.store.pending_messages.mark_send_now_handoff_accepted(
        message_id=waiting["id"],
        revision=claimed_revision,
        attempt_id=running["attempt_id"],
    )
    current = await api.store.pending_messages.return_to_queue(
        message_id=waiting["id"],
        revision=claimed_revision,
        last_error="stale refusal",
        attempt_id=running["attempt_id"],
    )

    assert accepted is False
    assert current is not None
    assert current["state"] == "sending"
    assert current["revision"] == claimed_revision
    assert current["send_now_requested_at"] is not None
    assert current["last_error"] is None


async def test_response_loss_replays_the_host_receipt_without_a_second_native_steer(
    api, monkeypatch
):
    await _steerable(api)
    sandbox_manager = _ResponseLossSandboxManager()
    _route_input_to(monkeypatch, sandbox_manager)
    _job, thread_id, _running, waiting = await _waiting_behind_a_running_turn(api)

    sent = await _send_now(api, thread_id, waiting)

    assert sent.status_code == 200, sent.text
    assert sent.json()["not_delivered_reason"] is None
    assert sent.json()["pending"]["state"] == "sending"
    assert len(sandbox_manager.native_steers) == 1
    async with api.pool.acquire() as conn:
        await conn.execute(
            "UPDATE agent_pending_messages "
            "SET send_now_requested_at = NOW() - INTERVAL '3 minutes', "
            "updated_at = NOW() - INTERVAL '2 minutes' WHERE id = $1",
            waiting["id"],
        )

    recovered = await send_now_delivery.recover_handoffs(api.store)

    assert recovered == {"claimed": 1, "accepted": 1, "requeued": 0}
    assert sandbox_manager.calls == [waiting["id"], waiting["id"]]
    assert len(sandbox_manager.native_steers) == 1
    async with api.pool.acquire() as conn:
        request_marker, handed_off = await conn.fetchrow(
            "SELECT send_now_requested_at, send_now_handed_off "
            "FROM agent_pending_messages WHERE id = $1",
            waiting["id"],
        )
    assert request_marker is not None
    assert handed_off is True


async def test_recovery_requeues_a_target_that_never_acquired_a_host_with_status(api):
    await _steerable(api)
    first = await api.create()
    thread_id = first["thread_id"]
    await api.run(first)
    _target = (await api.send(thread_id, "next step")).json()["job"]
    waiting = (await api.send(thread_id, "and mind the tests")).json()["pending"]
    began = await api.store.pending_messages.begin_send_now(
        thread_id=thread_id,
        user_id=_OWNER,
        message_id=waiting["id"],
        revision=waiting["revision"],
    )
    assert began["attempt_id"] is None
    async with api.pool.acquire() as conn:
        await conn.execute(
            "UPDATE agent_pending_messages "
            "SET send_now_requested_at = NOW() - INTERVAL '11 minutes', "
            "updated_at = NOW() - INTERVAL '11 minutes' WHERE id = $1",
            waiting["id"],
        )

    recovered = await send_now_delivery.recover_handoffs(api.store)

    assert recovered == {"claimed": 0, "accepted": 0, "requeued": 1}
    (message,) = (await api.queue(thread_id))["messages"]
    assert message["state"] == "queued"
    assert message["revision"] == waiting["revision"] + 2
    assert "did not acquire a host within ten minutes" in message["last_error"]


async def test_an_expired_unreaped_attempt_is_never_given_the_message(api, monkeypatch):
    await _steerable(api)
    sandbox_manager = _SandboxManager()
    _route_input_to(monkeypatch, sandbox_manager)
    _job, thread_id, running, waiting = await _waiting_behind_a_running_turn(api)
    async with api.pool.acquire() as conn:
        await conn.execute(
            "UPDATE agent_attempts SET lease_expires_at = NOW() - INTERVAL '1 second' "
            "WHERE id = $1",
            running["attempt_id"],
        )
        attempt = await conn.fetchrow(
            "SELECT status, lease_expires_at < NOW() AS expired FROM agent_attempts WHERE id = $1",
            running["attempt_id"],
        )
    assert dict(attempt) == {"status": "running", "expired": True}

    sent = await _send_now(api, thread_id, waiting)

    assert sent.status_code == 200, sent.text
    assert sent.json()["pending"]["state"] == "sending"
    assert sandbox_manager.calls == []
    recovered = await send_now_delivery.recover_handoffs(api.store)
    assert recovered == {"claimed": 0, "accepted": 0, "requeued": 1}
    (message,) = (await api.queue(thread_id))["messages"]
    assert message["state"] == "queued"
    assert "target turn ended" in message["last_error"]


@pytest.mark.parametrize("operation", ["claim", "accept", "return"])
async def test_send_now_authority_rechecks_expiry_after_a_plain_attempt_row_lock(
    api, operation: str
):
    await _steerable(api)
    _job, thread_id, running, waiting = await _waiting_behind_a_running_turn(api)
    began = await api.store.pending_messages.begin_send_now(
        thread_id=thread_id,
        user_id=_OWNER,
        message_id=waiting["id"],
        revision=waiting["revision"],
    )
    revision = int(began["message"]["revision"])
    if operation != "claim":
        claimed = await api.store.pending_messages.claim_send_now_handoff(
            message_id=waiting["id"],
            revision=revision,
            lease_seconds=send_now_delivery.HAND_OVER_LEASE_S,
        )
        assert claimed is not None
        revision = int(claimed["message"]["revision"])
    async with api.pool.acquire() as setup:
        await setup.execute(
            "UPDATE agent_attempts "
            "SET lease_expires_at = clock_timestamp() + INTERVAL '400 milliseconds' "
            "WHERE id = $1",
            running["attempt_id"],
        )

    pending = None
    async with api.pool.acquire() as blocker, blocker.transaction():
        blocker_pid = await blocker.fetchval("SELECT pg_backend_pid()")
        await blocker.fetchval(
            "SELECT id FROM agent_attempts WHERE id = $1 FOR UPDATE",
            running["attempt_id"],
        )
        if operation == "claim":
            pending = asyncio.create_task(
                api.store.pending_messages.claim_send_now_handoff(
                    message_id=waiting["id"],
                    revision=revision,
                    lease_seconds=send_now_delivery.HAND_OVER_LEASE_S,
                )
            )
        elif operation == "accept":
            pending = asyncio.create_task(
                api.store.pending_messages.mark_send_now_handoff_accepted(
                    message_id=waiting["id"],
                    revision=revision,
                    attempt_id=running["attempt_id"],
                )
            )
        else:
            pending = asyncio.create_task(
                api.store.pending_messages.return_to_queue(
                    message_id=waiting["id"],
                    revision=revision,
                    last_error="too late",
                    attempt_id=running["attempt_id"],
                )
            )
        blocked = False
        for _ in range(100):
            blocked = await blocker.fetchval(
                "SELECT EXISTS (SELECT 1 FROM pg_locks "
                "WHERE NOT granted AND $1 = ANY(pg_blocking_pids(pid)))",
                blocker_pid,
            )
            if blocked:
                break
            await asyncio.sleep(0.01)
        assert blocked, f"send-now {operation} did not wait on the attempt row lock"
        await asyncio.sleep(0.55)

    assert pending is not None
    result = await asyncio.wait_for(pending, timeout=2)
    if operation == "accept":
        assert result is False
    elif operation == "claim":
        assert result is None
    else:
        assert result is not None and result["state"] == "sending"
    current = (await api.queue(thread_id))["messages"][0]
    assert current["state"] == "sending"
    assert current["revision"] == revision
    async with api.pool.acquire() as conn:
        request_marker, handed_off = await conn.fetchrow(
            "SELECT send_now_requested_at, send_now_handed_off "
            "FROM agent_pending_messages WHERE id = $1",
            waiting["id"],
        )
    assert request_marker is not None
    assert handed_off is False


async def test_attempt_expiry_during_sandbox_manager_io_leaves_recovery_the_receipt(
    api, monkeypatch
):
    """A sandbox manager answer after attempt expiry cannot acknowledge durable delivery."""
    await _steerable(api)
    _job, thread_id, running, waiting = await _waiting_behind_a_running_turn(api)

    async def expire_attempt() -> None:
        async with api.pool.acquire() as conn:
            await conn.execute(
                "UPDATE agent_attempts SET lease_expires_at = NOW() - INTERVAL '1 second' "
                "WHERE id = $1",
                running["attempt_id"],
            )

    sandbox_manager = _SandboxManager(during=expire_attempt)
    _route_input_to(monkeypatch, sandbox_manager)

    sent = await _send_now(api, thread_id, waiting)

    assert sent.status_code == 200, sent.text
    assert sent.json()["pending"]["state"] == "sending"
    assert len(sandbox_manager.calls) == 1
    # Neither an accepted nor refused response from an expired attempt is a
    # durable receipt. Once the handoff lease is reclaimable, recovery treats
    # that attempt as ended without replaying to it.
    async with api.pool.acquire() as conn:
        await conn.execute(
            "UPDATE agent_pending_messages "
            "SET updated_at = NOW() - INTERVAL '2 minutes' WHERE id = $1",
            waiting["id"],
        )
    recovered = await send_now_delivery.recover_handoffs(api.store)

    assert recovered == {"claimed": 0, "accepted": 0, "requeued": 1}
    assert len(sandbox_manager.calls) == 1
    (message,) = (await api.queue(thread_id))["messages"]
    assert message["state"] == "queued"
    assert "target turn ended" in message["last_error"]


async def test_send_now_refuses_what_it_cannot_do_and_leaves_the_message_as_it_was(
    api, monkeypatch
):
    sandbox_manager = _SandboxManager()
    _route_input_to(monkeypatch, sandbox_manager)

    # A live turn whose host cannot take a message mid-turn.
    job = await api.create()
    thread_id = job["thread_id"]
    early = (await api.send(thread_id, "too early")).json()["pending"]
    refused = await _send_now(api, thread_id, early)
    assert refused.status_code == 409
    assert refused.json()["detail"]["error"]["type"] == "mid_turn_input_unsupported"

    await api.start(job)
    await _steerable(api)
    # Uploads start a turn of their own.
    upload = await api.store.attachments.create_attachment(
        user_id=_OWNER, path="notes.txt", content=b"notes", content_type="text/plain"
    )
    with_file = (await api.send(thread_id, "read this", attachment_ids=[upload["id"]])).json()[
        "pending"
    ]
    refused = await _send_now(api, thread_id, with_file)
    assert refused.status_code == 409
    assert refused.json()["detail"]["error"]["type"] == "send_now_has_attachments"

    # A stale revision gets the message as it is now.
    stale = await _send_now(api, thread_id, {**early, "revision": early["revision"] + 5})
    assert stale.status_code == 409
    assert stale.json()["detail"]["error"]["type"] == "revision_conflict"
    assert stale.json()["detail"]["error"]["current"]["id"] == early["id"]

    # A message being edited is the owner's to save first.
    held = (await api.edit(thread_id, early, state="held")).json()["pending"]
    refused = await _send_now(api, thread_id, held)
    assert refused.status_code == 409
    assert refused.json()["detail"]["error"]["type"] == "not_editable"

    # A turn being stopped takes nothing more.
    later = (await api.send(thread_id, "while it stops")).json()["pending"]
    await api.client.post(f"/v1/agent/jobs/{job['id']}/cancel")
    refused = await _send_now(api, thread_id, later)
    assert refused.status_code == 409
    assert refused.json()["detail"]["error"]["type"] == "not_running"

    assert sandbox_manager.calls == []
    queue = await api.queue(thread_id)
    assert [(row["prompt"], row["state"]) for row in queue["messages"]] == [
        ("too early", "held"),
        ("read this", "queued"),
        ("while it stops", "queued"),
    ]


async def test_a_turn_ending_while_the_message_is_handed_over_sends_it_next_once(api, monkeypatch):
    await _steerable(api)
    job, thread_id, running, waiting = await _waiting_behind_a_running_turn(api)
    # The turn ends on the host while the hand-over is in flight, without
    # having taken it: its ending's settlement puts it back in line and sends it.
    sandbox_manager = _SandboxManager(during=lambda: api.finish(running))
    _route_input_to(monkeypatch, sandbox_manager)

    sent = await _send_now(api, thread_id, waiting)

    assert sent.status_code == 200, sent.text
    assert [turn["task_prompt"] for turn in await api.turns(thread_id)] == [
        "fix it",
        "also check the docs",
    ]
    assert (await api.queue(thread_id))["messages"] == []
    _events, rows = await _mid_turn_rows(api, job["id"])
    assert [row["content"] for row in rows] == ["fix it"]


async def test_a_message_taken_before_the_target_finishes_is_never_dispatched_again(
    api, monkeypatch
):
    await _steerable(api)
    job, thread_id, running, waiting = await _waiting_behind_a_running_turn(api)

    async def take_then_finish() -> None:
        taken = {
            "text": "also check the docs",
            "pending_message_id": waiting["id"],
        }
        reported = await _report(api, running, "user_message", taken, seq=40)
        assert reported.status_code == 201, reported.text
        await api.finish(running)

    sandbox_manager = _SandboxManager(during=take_then_finish)
    _route_input_to(monkeypatch, sandbox_manager)

    sent = await _send_now(api, thread_id, waiting)

    assert sent.status_code == 200, sent.text
    assert sent.json()["pending"] is None
    assert [turn["task_prompt"] for turn in await api.turns(thread_id)] == ["fix it"]
    assert (await api.queue(thread_id))["messages"] == []
    events, rows = await _mid_turn_rows(api, job["id"])
    assert len(events) == 1
    assert [(row["content"], row["mid_turn"]) for row in rows] == [
        ("fix it", False),
        ("also check the docs", True),
    ]
    assert sandbox_manager.calls == [
        (thread_id, f"attempt:{running['attempt_id']}", waiting["id"], "also check the docs")
    ]


async def test_a_turn_that_ended_before_taking_it_sends_it_next_at_once(api, monkeypatch):
    await _steerable(api)
    _job, thread_id, running, waiting = await _waiting_behind_a_running_turn(api)
    _route_input_to(
        monkeypatch,
        _SandboxManager(
            {"accepted": False, "reason": "turn_ended"}, during=lambda: api.finish(running)
        ),
    )

    sent = await _send_now(api, thread_id, waiting)

    assert sent.status_code == 200, sent.text
    assert sent.json()["not_delivered_reason"] == "turn_ended"
    # Back in line with nothing running: it is the next turn, now.
    assert [turn["task_prompt"] for turn in await api.turns(thread_id)] == [
        "fix it",
        "also check the docs",
    ]


async def test_a_message_left_sending_by_a_crash_goes_back_in_line_when_the_turn_ends(api):
    await _steerable(api)
    _job, thread_id, running, waiting = await _waiting_behind_a_running_turn(api)
    began = await api.store.pending_messages.begin_send_now(
        thread_id=thread_id, user_id=_OWNER, message_id=waiting["id"], revision=waiting["revision"]
    )
    assert began["message"]["state"] == "sending"
    # The control plane that began it never got as far as the host.

    await api.finish(running)

    assert [turn["task_prompt"] for turn in await api.turns(thread_id)] == [
        "fix it",
        "also check the docs",
    ]


async def test_a_report_for_a_message_no_longer_waiting_is_still_recorded_once(api):
    job, thread_id, running, _waiting = await _waiting_behind_a_running_turn(api)

    reported = await _report(
        api,
        running,
        "user_message",
        {"text": "said earlier", "pending_message_id": "apmsg_gone"},
        seq=41,
    )

    assert reported.status_code == 201, reported.text
    events, rows = await _mid_turn_rows(api, job["id"])
    assert [(row["content"], row["mid_turn"]) for row in rows] == [
        ("fix it", False),
        ("said earlier", True),
    ]
    assert rows[1]["source_event_id"] == events[0]["id"]
    # The message still waiting in line was not the one reported, so it waits.
    assert [row["prompt"] for row in (await api.queue(thread_id))["messages"]] == [
        "also check the docs"
    ]
