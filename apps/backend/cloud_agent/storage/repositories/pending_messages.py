"""Postgres implementation of the pending-message port.

Writes without an attempt fence take the thread's row lock first — the same lock
appending a turn, forking, cancelling and archiving take first — so the queue,
the turns it becomes and the thread's pause reason can only be changed in one
order. Send-now acknowledgements first lock the attempt whose authority they
carry, then resume the thread-first order. That is also what makes settling safe
to run from anywhere at once: a second settlement waits for the first, then sees
the turn it created and does nothing.
"""

from __future__ import annotations

import json
import math
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
    GuardedSubmissionRefused,
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
_SEND_NOW_TARGET_ENDED = (
    "Send now was not delivered because the target turn ended before taking the message."
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
        require_idle: bool = False,
        expected_latest_job_id: str | None = None,
        reset_reasoning_effort: bool = False,
        skills: list[dict[str, Any]] | None = None,
        attachments: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        """Put one message at the end of the owner's thread's queue.

        Answers ``{"message": row}``; ``{"job_id": id}`` for an idle-only turn
        or when this key already made a turn; or ``{"error": ...}`` —
        ``not_found``, ``session_archived``,
        ``client_request_conflict``, ``attachments_unavailable`` or
        ``conversation_changed``.

        An idle-only action locks its membership, then its sandbox group after
        the thread lock. It requires the expected newest turn, no live turn,
        waiting message, active member or Fork in the shared group, and its
        latest snapshot to be published. Using the resolved skills, attachments
        and effort choices, it creates the turn in this same transaction, never
        a pending message. It replays its original receipt before these checks,
        but cannot newly queue behind or resume waiting work.

        When nothing in the thread is running this is also the owner deciding
        to continue: a pause is cleared and the newest turn's ending counts as
        seen, so older waiting messages go first and this one follows them.
        Normal submissions also lock their membership then group before writing
        the queue, if present, so they cannot race an idle-only group's check.
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
                if require_idle:
                    # The thread lock freezes the newest turn. Its job is not
                    # locked until the group shows nothing in flight: snapshot
                    # publication reaches that job without the thread lock, and
                    # it locks the group only after its included jobs.
                    latest_job_id = await conn.fetchval(
                        "SELECT id FROM agent_jobs WHERE thread_id = $1 "
                        "ORDER BY turn_no DESC LIMIT 1",
                        thread_id,
                    )
                    if expected_latest_job_id is None or latest_job_id != expected_latest_job_id:
                        return {"error": "conversation_changed"}
                    # Match member reservation, follow-up creation and Fork:
                    # own membership before group. Never lock sibling members
                    # after the group; they may already hold their member row
                    # while waiting for this shared admission boundary.
                    membership = await conn.fetchrow(
                        "SELECT sandbox_id FROM agent_thread_sandbox_memberships "
                        "WHERE thread_id = $1 FOR UPDATE",
                        thread_id,
                    )
                    if membership is None:
                        return {"error": "conversation_changed"}
                    group = await conn.fetchrow(
                        "SELECT id, active_operation_count FROM agent_sandboxes "
                        "WHERE id = $1 FOR UPDATE",
                        membership["sandbox_id"],
                    )
                    if group is None:
                        return {"error": "conversation_changed"}
                    current = await conn.fetchrow(
                        f"""
                        SELECT
                            EXISTS (SELECT 1 FROM agent_jobs job
                                    JOIN agent_thread_sandbox_memberships member
                                      ON member.thread_id = job.thread_id
                                    WHERE member.sandbox_id = $1
                                      AND job.{LIVE_TURN_SQL}) AS has_live_turn,
                            EXISTS (SELECT 1 FROM agent_pending_messages message
                                    JOIN agent_thread_sandbox_memberships member
                                      ON member.thread_id = message.thread_id
                                    WHERE member.sandbox_id = $1) AS has_pending_message,
                            EXISTS (SELECT 1 FROM agent_thread_sandbox_memberships member
                                    WHERE member.sandbox_id = $1
                                      AND (member.active_attempt_id IS NOT NULL
                                           OR member.fork_operation_id IS NOT NULL))
                                AS has_active_member,
                            (SELECT snapshot.state
                             FROM agent_sandbox_snapshots snapshot
                             WHERE snapshot.sandbox_id = $1
                             ORDER BY snapshot.workspace_epoch DESC, snapshot.id DESC
                             LIMIT 1) AS workspace_publication_state,
                            -- A turn settled since the newest snapshot: the
                            -- published pull request is behind the workspace.
                            -- A Fork's copies of its source's turns do not count.
                            EXISTS (SELECT 1 FROM agent_thread_sandbox_memberships member
                                    JOIN agent_jobs settled
                                      ON settled.id = member.last_settled_job_id
                                    WHERE member.sandbox_id = $1
                                      AND settled.fork_source_job_id IS NULL
                                      AND NOT EXISTS (
                                          SELECT 1 FROM agent_snapshot_jobs held
                                          WHERE held.job_id = member.last_settled_job_id
                                      )) AS has_unpublished_turn
                        """,
                        group["id"],
                    )
                    if (
                        current["has_live_turn"]
                        or current["has_pending_message"]
                        or current["has_active_member"]
                        or group["active_operation_count"] != 0
                        or current["workspace_publication_state"] != "published"
                    ):
                        return {"error": "conversation_changed"}
                    if current["has_unpublished_turn"]:
                        return {"error": "unpublished_changes"}
                    # Every group member is idle, this thread is fenced and the
                    # newest snapshot is published, so no publication of this
                    # group holds the parent: the helper can lock it under the
                    # group without a publication waiting on the group in turn.
                    job = await insert_follow_up(
                        conn,
                        parent_job_id=expected_latest_job_id,
                        user_id=user_id,
                        prompt=prompt,
                        model=model,
                        reasoning_effort=reasoning_effort,
                        reset_reasoning_effort=reset_reasoning_effort,
                        skills=skills,
                        attachments=attachments,
                        yield_to_queued=True,
                        client_request_id=client_request_id,
                        client_request_fingerprint=client_request_fingerprint,
                    )
                    if job is None:
                        raise GuardedSubmissionRefused({"error": "conversation_changed"})
                    if job.get("error") is not None:
                        raise GuardedSubmissionRefused(job)
                    await conn.execute(
                        "UPDATE agent_threads SET queue_paused_reason = NULL, "
                        "queue_settled_job_id = $2 WHERE id = $1",
                        thread_id,
                        expected_latest_job_id,
                    )
                    return {"job_id": str(job["id"])}
                # Queue insertion shares the idle-only admission fence too:
                # a sibling cannot insert waiting work after its empty-queue
                # check but before the guarded turn commits. Legacy threads
                # without a membership keep their ordinary enqueue behavior.
                membership = await conn.fetchrow(
                    "SELECT sandbox_id FROM agent_thread_sandbox_memberships "
                    "WHERE thread_id = $1 FOR UPDATE",
                    thread_id,
                )
                if membership is not None:
                    await conn.fetchval(
                        "SELECT id FROM agent_sandboxes WHERE id = $1 FOR UPDATE",
                        membership["sandbox_id"],
                    )
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
        except GuardedSubmissionRefused as exc:
            return exc.result
        except PendingAttachmentsUnavailable:
            return {"error": "attachments_unavailable"}
        except AttachmentError as exc:
            # The idle-only turn's own claim, which says why: a file was taken,
            # or the conversation's /uploads cannot hold it.
            return {"error": "attachments_unavailable", "message": str(exc)}
        except asyncpg.UniqueViolationError as exc:
            # The same key sent to two conversations at once: the loser reads
            # back whatever the winner made.
            if exc.constraint_name not in {
                _CLIENT_REQUEST_CONSTRAINT,
                "uq_agent_jobs_client_request",
            }:
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
                    send_now_handed_off = FALSE, last_error = NULL,
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

    async def claim_send_now_handoff(
        self, *, message_id: str, revision: int, lease_seconds: float
    ) -> dict[str, Any] | None:
        """Lease a send-now handoff to this control-plane process.

        The message revision is the ownership token. An unclaimed row has the
        identical request/update timestamp written by begin_send_now; a claim
        moves updated_at forward and can be reclaimed only after the lease.
        """
        if not math.isfinite(lease_seconds) or lease_seconds <= 0:
            raise ValueError("lease_seconds must be finite and positive")
        async with self._pool.acquire() as conn, conn.transaction():
            candidate = await conn.fetchrow(
                """
                SELECT p.thread_id, p.send_now_job_id, j.current_attempt_id
                FROM agent_pending_messages p
                LEFT JOIN agent_jobs j ON j.id = p.send_now_job_id
                WHERE p.id = $1
                """,
                message_id,
            )
            if (
                candidate is None
                or candidate["send_now_job_id"] is None
                or candidate["current_attempt_id"] is None
            ):
                return None
            attempt_id = int(candidate["current_attempt_id"])
            if not await conn.fetchval(
                "SELECT id FROM agent_attempts WHERE id = $1 FOR SHARE",
                attempt_id,
            ):
                return None
            thread_id = str(candidate["thread_id"])
            if not await conn.fetchval(
                "SELECT id FROM agent_threads WHERE id = $1 FOR UPDATE",
                thread_id,
            ):
                return None
            message = await conn.fetchrow(
                f"""
                SELECT {PENDING_MESSAGE_COLUMNS}
                FROM agent_pending_messages
                WHERE id = $1 AND state = $2 AND revision = $3
                  AND send_now_requested_at IS NOT NULL
                  AND send_now_handed_off = FALSE
                FOR UPDATE
                """,
                message_id,
                PENDING_SENDING,
                revision,
            )
            if (
                message is None
                or message["thread_id"] != candidate["thread_id"]
                or message["send_now_job_id"] != candidate["send_now_job_id"]
            ):
                return None
            target = await conn.fetchrow(
                """
                SELECT j.state, j.cancel_requested, j.current_attempt_id
                FROM agent_jobs j WHERE j.id = $1 AND j.thread_id = $2
                FOR UPDATE
                """,
                message["send_now_job_id"],
                thread_id,
            )
            if (
                target is None
                or target["state"] != RUNNING
                or target["cancel_requested"]
                or target["current_attempt_id"] != attempt_id
                or not await conn.fetchval(
                    """
                    SELECT (
                            p.updated_at <= p.send_now_requested_at
                            OR p.updated_at < clock_timestamp()
                                - make_interval(secs => $3::double precision)
                        )
                        AND a.status = 'running'
                        AND a.lease_expires_at > clock_timestamp()
                    FROM agent_pending_messages p, agent_attempts a
                    WHERE p.id = $1 AND a.id = $2
                    """,
                    message_id,
                    attempt_id,
                    lease_seconds,
                )
            ):
                return None
            await conn.execute(
                """
                UPDATE agent_pending_messages
                SET revision = revision + 1,
                    updated_at = GREATEST(
                        clock_timestamp(), send_now_requested_at + INTERVAL '1 microsecond'
                    )
                WHERE id = $1 AND revision = $2
                """,
                message_id,
                revision,
            )
            claimed = await read_pending_message(conn, message_id)
            if claimed is None:  # The thread lock makes this defensive only.
                return None
            return {
                "message": claimed,
                "job_id": str(message["send_now_job_id"]),
                "attempt_id": attempt_id,
            }

    async def recoverable_send_now_handoffs(
        self, *, lease_seconds: float, limit: int = 50
    ) -> list[dict[str, Any]]:
        """Candidates a process may claim without holding a lock over sandbox manager I/O."""
        if not math.isfinite(lease_seconds) or lease_seconds <= 0:
            raise ValueError("lease_seconds must be finite and positive")
        if limit <= 0:
            return []
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT p.id, p.revision
                FROM agent_pending_messages p
                JOIN agent_jobs j ON j.id = p.send_now_job_id
                JOIN agent_attempts a ON a.id = j.current_attempt_id
                WHERE p.state = $1 AND p.send_now_requested_at IS NOT NULL
                  AND p.send_now_handed_off = FALSE
                  AND j.state = $2 AND NOT j.cancel_requested
                  AND a.status = 'running'
                  AND a.lease_expires_at > clock_timestamp()
                  AND (
                      p.updated_at <= p.send_now_requested_at
                      OR p.updated_at < clock_timestamp()
                          - make_interval(secs => $3::double precision)
                  )
                ORDER BY p.send_now_requested_at, p.id
                LIMIT $4
                """,
                PENDING_SENDING,
                RUNNING,
                lease_seconds,
                limit,
            )
        return [{"message_id": str(row["id"]), "revision": int(row["revision"])} for row in rows]

    async def mark_send_now_handoff_accepted(
        self, *, message_id: str, revision: int, attempt_id: int
    ) -> bool:
        """Fence retries after the exact current live attempt accepted a handoff."""
        async with self._pool.acquire() as conn, conn.transaction():
            candidate = await conn.fetchrow(
                "SELECT thread_id, send_now_job_id FROM agent_pending_messages WHERE id = $1",
                message_id,
            )
            if candidate is None or candidate["send_now_job_id"] is None:
                return False
            if not await conn.fetchval(
                "SELECT id FROM agent_attempts WHERE id = $1 FOR SHARE",
                attempt_id,
            ):
                return False
            thread_id = str(candidate["thread_id"])
            if not await conn.fetchval(
                "SELECT id FROM agent_threads WHERE id = $1 FOR UPDATE",
                thread_id,
            ):
                return False
            message = await conn.fetchrow(
                """
                SELECT thread_id, send_now_job_id FROM agent_pending_messages
                WHERE id = $1 AND state = $2 AND revision = $3
                  AND send_now_requested_at IS NOT NULL
                FOR UPDATE
                """,
                message_id,
                PENDING_SENDING,
                revision,
            )
            if (
                message is None
                or message["thread_id"] != candidate["thread_id"]
                or message["send_now_job_id"] != candidate["send_now_job_id"]
            ):
                return False
            if not await conn.fetchval(
                "SELECT id FROM agent_jobs WHERE id = $1 AND thread_id = $2 FOR SHARE",
                message["send_now_job_id"],
                thread_id,
            ):
                return False
            authorized = await conn.fetchval(
                """
                SELECT j.state = 'running' AND NOT j.cancel_requested
                   AND j.current_attempt_id = a.id AND a.status = 'running'
                   AND a.lease_expires_at > clock_timestamp()
                FROM agent_jobs j, agent_attempts a
                WHERE j.id = $1 AND a.id = $2
                """,
                message["send_now_job_id"],
                attempt_id,
            )
            if not authorized:
                return False
            changed = await conn.fetchval(
                """
                UPDATE agent_pending_messages
                SET send_now_handed_off = TRUE, updated_at = clock_timestamp()
                WHERE id = $1 AND state = $2 AND revision = $3
                  AND send_now_requested_at IS NOT NULL
                  AND send_now_handed_off = FALSE
                RETURNING TRUE
                """,
                message_id,
                PENDING_SENDING,
                revision,
            )
            return bool(changed)

    async def expired_send_now_handoffs(
        self, *, wait_seconds: float, lease_seconds: float, limit: int = 50
    ) -> list[dict[str, Any]]:
        """Reclaimable handoffs whose turn ended or failed to acquire a host."""
        if not math.isfinite(wait_seconds) or wait_seconds <= 0:
            raise ValueError("wait_seconds must be finite and positive")
        if not math.isfinite(lease_seconds) or lease_seconds <= 0:
            raise ValueError("lease_seconds must be finite and positive")
        if limit <= 0:
            return []
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT p.id, p.revision,
                       CASE
                           WHEN j.id IS NULL
                             OR j.state IN ('succeeded', 'failed', 'cancelled')
                             OR j.cancel_requested
                             OR (
                                 j.state = $2
                                 AND (
                                     a.id IS NULL OR a.status <> 'running'
                                     OR a.lease_expires_at <= clock_timestamp()
                                 )
                             )
                           THEN 'target_ended'
                           ELSE 'claim_timeout'
                       END AS reason
                FROM agent_pending_messages p
                LEFT JOIN agent_jobs j ON j.id = p.send_now_job_id
                LEFT JOIN agent_attempts a ON a.id = j.current_attempt_id
                WHERE p.state = $1 AND p.send_now_requested_at IS NOT NULL
                  AND p.send_now_handed_off = FALSE
                  AND (
                      p.updated_at <= p.send_now_requested_at
                      OR p.updated_at < clock_timestamp()
                          - make_interval(secs => $4::double precision)
                  )
                  AND (
                      j.id IS NULL
                      OR j.state IN ('succeeded', 'failed', 'cancelled')
                      OR j.cancel_requested
                      OR (
                          j.state = $2
                          AND (
                              a.id IS NULL OR a.status <> 'running'
                              OR a.lease_expires_at <= clock_timestamp()
                          )
                      )
                      OR (
                          j.state <> $2
                          AND p.send_now_requested_at < clock_timestamp()
                              - make_interval(secs => $3::double precision)
                      )
                  )
                ORDER BY p.send_now_requested_at, p.id
                LIMIT $5
                """,
                PENDING_SENDING,
                RUNNING,
                wait_seconds,
                lease_seconds,
                limit,
            )
        return [
            {
                "message_id": str(row["id"]),
                "revision": int(row["revision"]),
                "reason": str(row["reason"]),
            }
            for row in rows
        ]

    async def return_to_queue(
        self,
        *,
        message_id: str,
        revision: int,
        last_error: str | None = None,
        attempt_id: int | None = None,
    ) -> dict[str, Any] | None:
        """Put a message its turn will not take back in line, as it was.

        ``revision`` is the one it took when it was chosen to send now; a
        message the queue has moved on from since — taken by the turn, or
        already back in line — is left alone. ``last_error`` persists a
        recovery status for a request whose original HTTP response is gone.
        ``attempt_id`` additionally fences a live sandbox manager answer to the exact
        live, unexpired attempt it addressed. If it expires during sandbox manager I/O,
        the row stays leased for target-ended recovery. Returns the message as
        it now is, or ``None`` when it is gone.
        """
        async with self._pool.acquire() as conn, conn.transaction():
            candidate = await conn.fetchrow(
                "SELECT thread_id, send_now_job_id FROM agent_pending_messages WHERE id = $1",
                message_id,
            )
            if candidate is None:
                return None
            attempt_locked = attempt_id is None or bool(
                await conn.fetchval(
                    "SELECT id FROM agent_attempts WHERE id = $1 FOR SHARE",
                    attempt_id,
                )
            )
            thread_id = str(candidate["thread_id"])
            if not await conn.fetchval(
                "SELECT id FROM agent_threads WHERE id = $1 FOR UPDATE",
                thread_id,
            ):
                return None
            message = await conn.fetchrow(
                """
                SELECT thread_id, send_now_job_id FROM agent_pending_messages
                WHERE id = $1 FOR UPDATE
                """,
                message_id,
            )
            if message is None:
                return None
            authorized = attempt_id is None
            if (
                attempt_id is not None
                and attempt_locked
                and message["thread_id"] == candidate["thread_id"]
                and message["send_now_job_id"] == candidate["send_now_job_id"]
                and message["send_now_job_id"] is not None
                and await conn.fetchval(
                    "SELECT id FROM agent_jobs WHERE id = $1 AND thread_id = $2 FOR SHARE",
                    message["send_now_job_id"],
                    thread_id,
                )
            ):
                authorized = bool(
                    await conn.fetchval(
                        """
                        SELECT j.state = 'running' AND NOT j.cancel_requested
                           AND j.current_attempt_id = a.id AND a.status = 'running'
                           AND a.lease_expires_at > clock_timestamp()
                        FROM agent_jobs j, agent_attempts a
                        WHERE j.id = $1 AND a.id = $2
                        """,
                        message["send_now_job_id"],
                        attempt_id,
                    )
                )
            if authorized:
                await conn.execute(
                    """
                    UPDATE agent_pending_messages
                    SET state = $3, send_now_job_id = NULL, send_now_requested_at = NULL,
                        send_now_handed_off = FALSE, last_error = $5,
                        revision = revision + 1,
                        updated_at = clock_timestamp()
                    WHERE id = $1 AND state = $2 AND revision = $4
                      AND send_now_handed_off = FALSE
                    """,
                    message_id,
                    PENDING_SENDING,
                    PENDING_QUEUED,
                    revision,
                    last_error[:MAX_PENDING_ERROR_CHARS] if last_error is not None else None,
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
                    send_now_handed_off = FALSE, last_error = $3,
                    revision = revision + 1, updated_at = NOW()
                WHERE thread_id = $1 AND state = $2
                """,
                thread_id,
                PENDING_SENDING,
                _SEND_NOW_TARGET_ENDED,
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
                        # The uploads this message pinned are its turn's to
                        # claim, and no other turn's.
                        pending_message_id=str(head["id"]),
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

    async def threads_to_settle(
        self, *, limit: int = 50, scan_interval_seconds: float = 30.0
    ) -> list[str]:
        """Threads whose queue a settlement could move or pause right now.

        For the periodic sweep: a turn that ended without the finish route —
        the reaper's verdict, a control plane restarted mid-request — still
        gets its queue acted on. The bounded page advances with the database
        clock rather than process-local state, so restart or concurrent
        sweepers cannot pin recovery to the same first page.
        """
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(
                f"""
                WITH eligible AS MATERIALIZED (
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
                ), ranked AS (
                    SELECT id,
                           row_number() OVER (ORDER BY id) - 1 AS ordinal,
                           count(*) OVER () AS total
                    FROM eligible
                ), cursor AS MATERIALIZED (
                    SELECT FLOOR(
                        EXTRACT(EPOCH FROM clock_timestamp())
                        / GREATEST($2::double precision, 0.001)
                    )::bigint AS page
                ), positioned AS (
                    SELECT id,
                           (ordinal - ((cursor.page * $1::bigint) % ranked.total)
                            + ranked.total) % ranked.total AS distance
                    FROM ranked
                    CROSS JOIN cursor
                )
                SELECT id FROM positioned
                WHERE distance < $1
                ORDER BY distance
                """,
                limit,
                scan_interval_seconds,
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
