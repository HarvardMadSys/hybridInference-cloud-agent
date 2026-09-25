"""Postgres implementation of the pending-message port.

Every write here takes the thread's row lock first — the same lock appending a
turn, forking, cancelling and archiving take first — so the queue, the turns it
becomes and the thread's pause reason can only be changed in one order. That
order is also what makes settling safe to run from anywhere at once: a second
settlement waits for the first, then sees the turn it created and does nothing.
"""

from __future__ import annotations

import json
from typing import Any

import asyncpg

from cloud_agent.attachments.models import AttachmentError
from cloud_agent.domain.execution import RUNNING
from cloud_agent.domain.ids import new_pending_message_id
from cloud_agent.domain.pending_messages import (
    MAX_PENDING_ERROR_CHARS,
    PENDING_HELD,
    PENDING_QUEUED,
    PENDING_SENDING,
    QUEUE_PAUSED_FAILED,
    QUEUE_PAUSED_STOPPED,
    PreparedHead,
    SettleOutcome,
)
from cloud_agent.domain.requests import check_client_request
from cloud_agent.storage._sql.pending_messages import (
    LIVE_TURN_SQL,
    PAUSE_QUEUE_SQL,
    PENDING_MESSAGE_COLUMNS,
    PendingAttachmentsUnavailable,
    list_pending_messages,
    pending_message_row,
    read_pending_message,
)
from cloud_agent.storage._sql.rows import load_json
from cloud_agent.storage.repositories.conversation_commands import insert_follow_up

_CLIENT_REQUEST_CONSTRAINT = "uq_agent_pending_messages_client_request"

#: How the newest turn's ending stops a queue that has messages waiting.
_PAUSE_FOR_ENDING = {"failed": QUEUE_PAUSED_FAILED, "cancelled": QUEUE_PAUSED_STOPPED}

#: What a held message says when its conversation can no longer take a turn.
_CONTEXT_LOST = (
    "This chat's retained sandbox has an indeterminate or lost context and cannot "
    "safely accept another message."
)


async def _lock_owned_thread(conn: Any, *, thread_id: str, user_id: str) -> Any:
    """The owner's live thread, locked, or ``None`` when it is not theirs."""
    return await conn.fetchrow(
        "SELECT id, archived_at, queue_paused_reason, queue_settled_job_id "
        "FROM agent_threads WHERE id = $1 AND user_id = $2 AND deleted_at IS NULL FOR UPDATE",
        thread_id,
        user_id,
    )


async def _continue_if_idle(conn: Any, *, thread_id: str) -> bool:
    """Record the owner continuing a conversation nothing is running in.

    Clearing the pause is the obvious half. The other half is naming the newest
    turn as seen: its ending — cancelled or failed — is what the owner is
    continuing past, and a settlement that looked at it afresh would pause the
    queue straight back. Returns whether the conversation was idle; with a turn
    still running there is no ending yet to have seen, and nothing is recorded.
    """
    live = await conn.fetchval(
        f"SELECT EXISTS (SELECT 1 FROM agent_jobs WHERE thread_id = $1 AND {LIVE_TURN_SQL})",
        thread_id,
    )
    if live:
        return False
    latest = await conn.fetchval(
        "SELECT id FROM agent_jobs WHERE thread_id = $1 ORDER BY turn_no DESC LIMIT 1",
        thread_id,
    )
    await conn.execute(
        "UPDATE agent_threads SET queue_paused_reason = NULL, queue_settled_job_id = $2 "
        "WHERE id = $1",
        thread_id,
        latest,
    )
    return True


async def _replayed_on(
    conn: Any, *, user_id: str, client_request_id: str, fingerprint: str | None
) -> dict[str, Any] | None:
    """What an owner's key already made: a turn first, then a waiting message.

    The turn is asked first because a message that was sent carried its key onto
    the turn it became and was deleted here. Between the two reads that turn can
    only move from the second place to the first, never out of both, when the
    caller holds the thread's lock that promotion takes.
    """
    job = await conn.fetchrow(
        "SELECT id, client_request_fingerprint FROM agent_jobs "
        "WHERE user_id = $1 AND client_request_id = $2",
        user_id,
        client_request_id,
    )
    if job is not None:
        if job["client_request_fingerprint"] != fingerprint:
            return {"error": "client_request_conflict"}
        return {"job_id": str(job["id"])}
    row = await conn.fetchrow(
        "SELECT id, client_request_fingerprint FROM agent_pending_messages "
        "WHERE user_id = $1 AND client_request_id = $2",
        user_id,
        client_request_id,
    )
    if row is None:
        return None
    if row["client_request_fingerprint"] != fingerprint:
        return {"error": "client_request_conflict"}
    message = await read_pending_message(conn, str(row["id"]))
    return None if message is None else {"message": message}


async def _pin_attachments(
    conn: Any, *, message_id: str, user_id: str, attachment_ids: list[int] | None
) -> None:
    """Pin the named uploads to this message, and release any it no longer names.

    Only a staged upload of the same owner that no other message holds can be
    pinned; anything else rolls the write back. A pinned upload is left alone by
    the staged-upload sweeper and cannot be deleted from the composer.
    """
    ids = sorted({int(value) for value in attachment_ids or []})
    await conn.execute(
        "UPDATE agent_job_attachments SET pending_message_id = NULL "
        "WHERE pending_message_id = $1 AND NOT (id = ANY($2::bigint[]))",
        message_id,
        ids,
    )
    if not ids:
        return
    pinned = await conn.fetchval(
        """
        WITH pinned AS (
            UPDATE agent_job_attachments SET pending_message_id = $1
            WHERE user_id = $2 AND job_id IS NULL AND id = ANY($3::bigint[])
              AND (pending_message_id IS NULL OR pending_message_id = $1)
            RETURNING id
        )
        SELECT count(*) FROM pinned
        """,
        message_id,
        user_id,
        ids,
    )
    if int(pinned or 0) != len(ids):
        raise PendingAttachmentsUnavailable


def _json_or_null(value: list[int] | None) -> str | None:
    """A JSON array column's text, or NULL — this pool registers no JSONB codec."""
    return None if value is None else json.dumps(value)


class PostgresPendingMessageRepository:
    """A thread's pending messages, and the settlement that sends them in order."""

    def __init__(self, pool: asyncpg.Pool) -> None:
        """Wrap the pool the control plane already owns."""
        self._pool = pool

    async def submit_message(
        self,
        *,
        thread_id: str,
        user_id: str,
        prompt: str,
        skill_ids: list[int] | None,
        attachment_ids: list[int] | None,
        model: str | None,
        reasoning_effort: str | None,
        client_request_id: str | None = None,
        client_request_fingerprint: str | None = None,
    ) -> dict[str, Any]:
        """Put one message at the end of the owner's thread's queue.

        Answers ``{"message": row}``; ``{"job_id": id}`` when this key already
        made a turn; or ``{"error": ...}`` — ``not_found``, ``session_archived``,
        ``client_request_conflict`` or ``attachments_unavailable``.

        When nothing in the thread is running this is also the owner deciding
        to continue: a pause is cleared and the newest turn's ending counts as
        seen, so older waiting messages go first and this one follows them.
        """
        check_client_request(client_request_id, client_request_fingerprint)
        message_id = new_pending_message_id()
        try:
            async with self._pool.acquire() as conn, conn.transaction():
                thread = await _lock_owned_thread(conn, thread_id=thread_id, user_id=user_id)
                if thread is None:
                    return {"error": "not_found"}
                if client_request_id is not None:
                    # Under the thread's lock, which promotion also holds: a
                    # resend cannot miss a message that became a turn between
                    # two reads.
                    replayed = await _replayed_on(
                        conn,
                        user_id=user_id,
                        client_request_id=client_request_id,
                        fingerprint=client_request_fingerprint,
                    )
                    if replayed is not None:
                        return replayed
                if thread["archived_at"] is not None:
                    return {"error": "session_archived"}
                await _continue_if_idle(conn, thread_id=thread_id)
                await conn.execute(
                    """
                    INSERT INTO agent_pending_messages
                        (id, thread_id, user_id, state, prompt, skill_ids, attachment_ids,
                         model, reasoning_effort, client_request_id,
                         client_request_fingerprint)
                    VALUES ($1, $2, $3, 'queued', $4, $5::jsonb, $6::jsonb, $7, $8, $9, $10)
                    """,
                    message_id,
                    thread_id,
                    user_id,
                    prompt,
                    _json_or_null(skill_ids),
                    _json_or_null(attachment_ids or None),
                    model,
                    reasoning_effort,
                    client_request_id,
                    client_request_fingerprint,
                )
                await _pin_attachments(
                    conn, message_id=message_id, user_id=user_id, attachment_ids=attachment_ids
                )
                message = await read_pending_message(conn, message_id)
        except PendingAttachmentsUnavailable:
            return {"error": "attachments_unavailable"}
        except asyncpg.UniqueViolationError as exc:
            # The same key sent to two conversations at once: the loser reads
            # back whatever the winner made.
            if exc.constraint_name != _CLIENT_REQUEST_CONSTRAINT:
                raise
            assert client_request_id is not None
            async with self._pool.acquire() as conn:
                replayed = await _replayed_on(
                    conn,
                    user_id=user_id,
                    client_request_id=client_request_id,
                    fingerprint=client_request_fingerprint,
                )
            return replayed or {"error": "client_request_conflict"}
        assert message is not None
        return {"message": message}

    async def replayed_message(
        self, *, user_id: str, client_request_id: str, fingerprint: str | None
    ) -> dict[str, Any] | None:
        """The pending message an owner's key already made, a conflict, or ``None``."""
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT id, client_request_fingerprint FROM agent_pending_messages "
                "WHERE user_id = $1 AND client_request_id = $2",
                user_id,
                client_request_id,
            )
            if row is None:
                return None
            if row["client_request_fingerprint"] != fingerprint:
                return {"error": "client_request_conflict"}
            message = await read_pending_message(conn, str(row["id"]))
        return None if message is None else {"message": message}

    async def queue_state(self, *, thread_id: str, user_id: str) -> dict[str, Any] | None:
        """The owner's thread's pause reason and messages, or ``None`` if not theirs."""
        async with self._pool.acquire() as conn:
            reason = await conn.fetchrow(
                "SELECT queue_paused_reason FROM agent_threads "
                "WHERE id = $1 AND user_id = $2 AND deleted_at IS NULL",
                thread_id,
                user_id,
            )
            if reason is None:
                return None
            messages = await list_pending_messages(conn, thread_id)
        return {"queue_paused_reason": reason["queue_paused_reason"], "messages": messages}

    async def edit_message(
        self,
        *,
        thread_id: str,
        user_id: str,
        message_id: str,
        revision: int,
        changes: dict[str, Any],
    ) -> dict[str, Any]:
        """Change what a message says or whether it is held, at the revision read.

        ``changes`` holds only what the caller set: ``prompt``, ``skill_ids``,
        ``attachment_ids``, ``model``, ``reasoning_effort`` and ``state``
        (``held`` or ``queued``). Answers ``{"message": row}`` or ``{"error":
        ...}`` — ``not_found``, ``revision_conflict`` or ``not_editable`` with
        the ``current`` row, or ``attachments_unavailable``.
        """
        try:
            async with self._pool.acquire() as conn, conn.transaction():
                refused = await self._lock_for_change(
                    conn,
                    thread_id=thread_id,
                    user_id=user_id,
                    message_id=message_id,
                    revision=revision,
                )
                if isinstance(refused, dict):
                    return refused
                row = refused
                state = changes.get("state") or row["state"]
                attachment_ids = (
                    changes["attachment_ids"]
                    if "attachment_ids" in changes
                    else load_json(row["attachment_ids"])
                )
                await conn.execute(
                    """
                    UPDATE agent_pending_messages
                    SET prompt = $2, skill_ids = $3::jsonb, attachment_ids = $4::jsonb,
                        model = $5, reasoning_effort = $6, state = $7,
                        -- A message put back in line is being tried afresh, so
                        -- the refusal that held it no longer describes it.
                        last_error = CASE WHEN $7 = 'queued' THEN NULL ELSE last_error END,
                        revision = revision + 1, updated_at = NOW()
                    WHERE id = $1
                    """,
                    message_id,
                    changes.get("prompt", row["prompt"]),
                    _json_or_null(
                        changes["skill_ids"]
                        if "skill_ids" in changes
                        else load_json(row["skill_ids"])
                    ),
                    _json_or_null(attachment_ids or None),
                    changes["model"] if "model" in changes else row["model"],
                    changes["reasoning_effort"]
                    if "reasoning_effort" in changes
                    else row["reasoning_effort"],
                    state,
                )
                if "attachment_ids" in changes:
                    await _pin_attachments(
                        conn,
                        message_id=message_id,
                        user_id=user_id,
                        attachment_ids=attachment_ids,
                    )
                message = await read_pending_message(conn, message_id)
        except PendingAttachmentsUnavailable:
            return {"error": "attachments_unavailable"}
        return {"message": message}

    async def delete_message(
        self, *, thread_id: str, user_id: str, message_id: str, revision: int
    ) -> dict[str, Any]:
        """Remove a message at the revision read; its uploads return to staged.

        The uploads are released by the foreign key, in this statement: they go
        back to being ordinary staged uploads, as if never attached.
        """
        async with self._pool.acquire() as conn, conn.transaction():
            refused = await self._lock_for_change(
                conn,
                thread_id=thread_id,
                user_id=user_id,
                message_id=message_id,
                revision=revision,
            )
            if isinstance(refused, dict):
                return refused
            await conn.execute("DELETE FROM agent_pending_messages WHERE id = $1", message_id)
        return {"deleted": message_id}

    async def resume_queue(self, *, thread_id: str, user_id: str) -> dict[str, Any]:
        """Clear the owner's pause so the queue may move again.

        Answers ``{"queue_paused_reason": None}``, or ``{"error": "not_found"}``
        / ``{"error": "session_archived"}``.
        """
        async with self._pool.acquire() as conn, conn.transaction():
            thread = await _lock_owned_thread(conn, thread_id=thread_id, user_id=user_id)
            if thread is None:
                return {"error": "not_found"}
            if thread["archived_at"] is not None:
                return {"error": "session_archived"}
            if not await _continue_if_idle(conn, thread_id=thread_id):
                # A turn is still running: its ending has not happened, so only
                # the pause is cleared and that ending will be judged when it
                # comes.
                await conn.execute(
                    "UPDATE agent_threads SET queue_paused_reason = NULL WHERE id = $1",
                    thread_id,
                )
        return {"queue_paused_reason": None}

    async def begin_send_now(
        self, *, thread_id: str, user_id: str, message_id: str, revision: int
    ) -> dict[str, Any]:
        """Choose one waiting message to go into the live turn.

        Under the thread's lock the message must be ``queued`` at the revision
        the owner read and carry no uploads — a turn's uploads arrive with its
        prompt, not in the middle. Then, by what the conversation is doing:

        - nothing is live: this is an ordinary send, so a paused queue
          continues and ``{"idle": True}`` asks the caller to settle it;
        - its newest turn is being stopped: ``{"error": "not_running"}``;
        - a turn is live, running or still waiting for a host: the message
          becomes ``sending``, tied to that turn, and ``{"message", "job_id",
          "attempt_id"}`` names what to hand it to. ``attempt_id`` is ``None``
          until a host has the turn.

        Other refusals read like an edit's — ``not_found``, and
        ``revision_conflict`` or ``not_editable`` with ``current`` — plus
        ``has_attachments`` and ``session_archived``.
        """
        async with self._pool.acquire() as conn, conn.transaction():
            locked = await self._lock_for_change(
                conn,
                thread_id=thread_id,
                user_id=user_id,
                message_id=message_id,
                revision=revision,
            )
            if isinstance(locked, dict):
                return locked
            if locked["state"] != PENDING_QUEUED:
                # Held: being edited, or refused and waiting for the owner.
                return {
                    "error": "not_editable",
                    "current": await read_pending_message(conn, message_id),
                }
            if load_json(locked["attachment_ids"]):
                return {"error": "has_attachments"}
            archived = await conn.fetchval(
                "SELECT archived_at FROM agent_threads WHERE id = $1", thread_id
            )
            if archived is not None:
                return {"error": "session_archived"}
            live = await conn.fetchrow(
                f"SELECT id, state, cancel_requested, current_attempt_id FROM agent_jobs "
                f"WHERE thread_id = $1 AND {LIVE_TURN_SQL} ORDER BY turn_no DESC LIMIT 1",
                thread_id,
            )
            if live is None:
                # "Now" with nothing running is simply "next": the owner asked
                # for it to go, so a pause they would otherwise lift goes too.
                await _continue_if_idle(conn, thread_id=thread_id)
                return {"idle": True}
            if live["cancel_requested"]:
                return {"error": "not_running"}
            await conn.execute(
                """
                UPDATE agent_pending_messages
                SET state = $2, send_now_job_id = $3, send_now_requested_at = NOW(),
                    revision = revision + 1, updated_at = NOW()
                WHERE id = $1
                """,
                message_id,
                PENDING_SENDING,
                live["id"],
            )
            running = live["state"] == RUNNING and live["current_attempt_id"] is not None
            return {
                "message": await read_pending_message(conn, message_id),
                "job_id": str(live["id"]),
                "attempt_id": int(live["current_attempt_id"]) if running else None,
            }

    async def return_to_queue(self, *, message_id: str, revision: int) -> dict[str, Any] | None:
        """Put a message its turn will not take back in line, as it was.

        ``revision`` is the one it took when it was chosen to send now; a
        message the queue has moved on from since — taken by the turn, or
        already back in line — is left alone. Returns the message as it now
        is, or ``None`` when it is gone.
        """
        async with self._pool.acquire() as conn, conn.transaction():
            thread_id = await conn.fetchval(
                "SELECT thread_id FROM agent_pending_messages WHERE id = $1", message_id
            )
            if thread_id is None:
                return None
            await conn.execute("SELECT id FROM agent_threads WHERE id = $1 FOR UPDATE", thread_id)
            await conn.execute(
                """
                UPDATE agent_pending_messages
                SET state = $3, send_now_job_id = NULL, send_now_requested_at = NULL,
                    revision = revision + 1, updated_at = NOW()
                WHERE id = $1 AND state = $2 AND revision = $4
                """,
                message_id,
                PENDING_SENDING,
                PENDING_QUEUED,
                revision,
            )
            return await read_pending_message(conn, message_id)

    async def settle_queue(
        self, *, thread_id: str, prepared: PreparedHead | None = None
    ) -> SettleOutcome:
        """Act on the newest turn's ending, and send the first message if it may go.

        One transaction under the thread's lock. A turn that is still running
        means nothing to do. A newest turn that ended ``failed`` or
        ``cancelled`` and has not been acted on pauses a non-empty queue. A
        paused, archived, empty or held-at-the-front queue sends nothing.
        Otherwise the first message becomes the next turn — but only with a
        ``prepared`` check made for exactly that message and that turn; without
        one the outcome names the message and the turn to check.
        """
        async with self._pool.acquire() as conn, conn.transaction():
            # The thread before any of its jobs, the order every writer of both
            # takes.
            thread = await conn.fetchrow(
                "SELECT id, archived_at, queue_paused_reason, queue_settled_job_id "
                "FROM agent_threads WHERE id = $1 AND deleted_at IS NULL FOR UPDATE",
                thread_id,
            )
            if thread is None:
                return SettleOutcome()
            live = await conn.fetchval(
                f"SELECT id FROM agent_jobs WHERE thread_id = $1 AND {LIVE_TURN_SQL} LIMIT 1",
                thread_id,
            )
            if live is not None:
                return SettleOutcome()
            latest = await conn.fetchrow(
                "SELECT id, state FROM agent_jobs WHERE thread_id = $1 "
                "ORDER BY turn_no DESC LIMIT 1 FOR UPDATE",
                thread_id,
            )
            if latest is None:  # Defensive: every thread is created with turn one.
                return SettleOutcome()
            # Nothing is live, so a message chosen to go into a turn was not
            # taken by it: the turn's stream says where it took one, and the
            # runner stores every event before it ends the turn. It goes back
            # in line, to be sent as the next turn.
            await conn.execute(
                """
                UPDATE agent_pending_messages
                SET state = 'queued', send_now_job_id = NULL, send_now_requested_at = NULL,
                    revision = revision + 1, updated_at = NOW()
                WHERE thread_id = $1 AND state = $2
                """,
                thread_id,
                PENDING_SENDING,
            )
            if latest["id"] != thread["queue_settled_job_id"]:
                # Each ending is acted on once. Continuing moves this marker
                # forward, which is why continuing past a cancelled turn is not
                # undone by looking at that turn again.
                await conn.execute(
                    "UPDATE agent_threads SET queue_settled_job_id = $2 WHERE id = $1",
                    thread_id,
                    latest["id"],
                )
                reason = _PAUSE_FOR_ENDING.get(str(latest["state"]))
                if reason is not None:
                    paused = await conn.fetchval(
                        PAUSE_QUEUE_SQL + " RETURNING t.queue_paused_reason",
                        [thread_id],
                        reason,
                    )
                    if paused is not None:
                        return SettleOutcome()
            if thread["archived_at"] is not None or thread["queue_paused_reason"] is not None:
                return SettleOutcome()
            head = await conn.fetchrow(
                f"SELECT {PENDING_MESSAGE_COLUMNS} FROM agent_pending_messages "
                "WHERE thread_id = $1 ORDER BY position LIMIT 1 FOR UPDATE",
                thread_id,
            )
            # A held message stops the line where it stands: nothing behind it
            # overtakes a message its owner is still rewriting.
            if head is None or head["state"] != PENDING_QUEUED:
                return SettleOutcome()
            if prepared is None or (
                prepared.message_id,
                prepared.revision,
                prepared.parent_job_id,
            ) != (head["id"], head["revision"], latest["id"]):
                return SettleOutcome(head=pending_message_row(head), parent_job_id=latest["id"])
            if prepared.error is not None:
                await self._hold(conn, message_id=str(head["id"]), error=prepared.error)
                return SettleOutcome()
            client_request_id = head["client_request_id"]
            fingerprint = head["client_request_fingerprint"]
            if client_request_id is not None and await conn.fetchval(
                "SELECT EXISTS (SELECT 1 FROM agent_jobs "
                "WHERE user_id = $1 AND client_request_id = $2)",
                head["user_id"],
                client_request_id,
            ):
                # The key was spent on another turn after this message took it
                # — a client reusing keys across endpoints. The message is still
                # sent exactly once, because it is deleted in this commit; only
                # its key does not travel with it.
                client_request_id = fingerprint = None
            try:
                # A savepoint: a refused upload claim undoes the turn, and the
                # message is held instead of being lost with it.
                async with conn.transaction():
                    job = await insert_follow_up(
                        conn,
                        parent_job_id=str(latest["id"]),
                        user_id=str(head["user_id"]),
                        prompt=str(head["prompt"]),
                        model=prepared.model,
                        reasoning_effort=prepared.reasoning_effort,
                        reset_reasoning_effort=prepared.reset_reasoning_effort,
                        skills=prepared.skills,
                        attachments=prepared.attachments,
                        yield_to_queued=True,
                        client_request_id=client_request_id,
                        client_request_fingerprint=fingerprint,
                    )
            except AttachmentError as exc:
                await self._hold(conn, message_id=str(head["id"]), error=str(exc))
                return SettleOutcome()
            if job is None or job.get("error") is not None:
                if job is not None and job.get("error") == "sandbox_context_lost":
                    await self._hold(conn, message_id=str(head["id"]), error=_CONTEXT_LOST)
                return SettleOutcome()
            await conn.execute("DELETE FROM agent_pending_messages WHERE id = $1", head["id"])
        return SettleOutcome(job=job, message_id=str(head["id"]))

    async def threads_to_settle(self, *, limit: int = 50) -> list[str]:
        """Threads whose queue a settlement could move or pause right now.

        For the periodic sweep: a turn that ended without the finish route —
        the reaper's verdict, a control plane restarted mid-request — still
        gets its queue acted on.
        """
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(
                f"""
                SELECT t.id
                FROM (SELECT DISTINCT thread_id FROM agent_pending_messages) waiting
                JOIN agent_threads t ON t.id = waiting.thread_id
                WHERE t.deleted_at IS NULL
                  AND NOT EXISTS (
                        SELECT 1 FROM agent_jobs j
                        WHERE j.thread_id = t.id AND j.{LIVE_TURN_SQL}
                  )
                  AND (
                        t.queue_settled_job_id IS DISTINCT FROM (
                            SELECT latest.id FROM agent_jobs latest
                            WHERE latest.thread_id = t.id
                            ORDER BY latest.turn_no DESC LIMIT 1
                        )
                        OR EXISTS (
                            SELECT 1 FROM agent_pending_messages stranded
                            WHERE stranded.thread_id = t.id AND stranded.state = 'sending'
                        )
                        OR (
                            t.queue_paused_reason IS NULL AND t.archived_at IS NULL
                            AND (
                                SELECT head.state FROM agent_pending_messages head
                                WHERE head.thread_id = t.id
                                ORDER BY head.position LIMIT 1
                            ) = 'queued'
                        )
                  )
                ORDER BY t.id
                LIMIT $1
                """,
                limit,
            )
        return [str(row["id"]) for row in rows]

    @staticmethod
    async def _lock_for_change(
        conn: Any, *, thread_id: str, user_id: str, message_id: str, revision: int
    ) -> Any:
        """Lock a message the owner is changing, or say why it cannot be changed.

        Answers the locked row, or a refusal: ``not_found`` when the thread or
        the message is not there (a sent message is gone), ``revision_conflict``
        when somebody changed it since the caller read it, and ``not_editable``
        while it is being handed to a running turn. The last two carry the
        message as it is now, so the caller can show what won.
        """
        thread = await _lock_owned_thread(conn, thread_id=thread_id, user_id=user_id)
        if thread is None:
            return {"error": "not_found"}
        row = await conn.fetchrow(
            f"SELECT {PENDING_MESSAGE_COLUMNS} FROM agent_pending_messages "
            "WHERE id = $1 AND thread_id = $2 FOR UPDATE",
            message_id,
            thread_id,
        )
        if row is None:
            return {"error": "not_found"}
        if int(row["revision"]) != revision:
            return {
                "error": "revision_conflict",
                "current": await read_pending_message(conn, message_id),
            }
        if row["state"] == PENDING_SENDING:
            return {
                "error": "not_editable",
                "current": await read_pending_message(conn, message_id),
            }
        return row

    @staticmethod
    async def _hold(conn: Any, *, message_id: str, error: str) -> None:
        """Stop the line at a message the platform could not send, and say why."""
        await conn.execute(
            """
            UPDATE agent_pending_messages
            SET state = $2, last_error = $3, revision = revision + 1, updated_at = NOW()
            WHERE id = $1
            """,
            message_id,
            PENDING_HELD,
            error[:MAX_PENDING_ERROR_CHARS],
        )
