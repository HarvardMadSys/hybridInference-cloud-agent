"""Bounded title context persisted with an immutable workspace snapshot."""

from __future__ import annotations

import json
from typing import Any

from cloud_agent.storage._sql.rows import load_json

#: Title inference accepts at most 96,000 serialized characters. Keep the
#: durable transcript comfortably below that while retaining the most recent
#: requests, corrections, and completed responses across snapshot epochs.
TITLE_CONTEXT_MESSAGE_LIMIT = 24
TITLE_CONTEXT_MESSAGE_CHARS = 24000
#: Leave room below the inference source cap for JSON, stable ids and paths.
TITLE_CONTEXT_TOTAL_CHARS = 72000
#: The deterministic fallback uses at most five entries. A few more are kept
#: so duplicate/non-prose closing messages do not crowd out every useful one.
TITLE_CONTEXT_SUMMARY_LIMIT = 8
TITLE_CONTEXT_SUMMARY_CHARS = 4000
#: How many earlier epochs a context looks back over for the newest one that
#: stored a context. The epochs after that one hold turns no stored context has
#: read: a capture refused before it was taken, or one made before 0096 or by
#: the release before it. Their newest TITLE_CONTEXT_MESSAGE_LIMIT turns are
#: read; each holds at least its request, so those alone can fill the message
#: budget. A longer gap leaves its older turns out of the title.
TITLE_CONTEXT_EPOCH_LIMIT = 24
#: What 0096 gave every snapshot, and what a capture by the release before it
#: still leaves: no context computed yet.
EMPTY_TITLE_CONTEXT = '{"messages": [], "summaries": [], "summary_count": 0}'
_EMPTY = json.loads(EMPTY_TITLE_CONTEXT)


async def _earlier_context(
    conn: Any, *, snapshot_id: int, sandbox_id: str, workspace_epoch: int
) -> tuple[int, list[Any]]:
    """The epoch after which turns are still unread, and the messages stored up to it.

    That is the newest earlier snapshot captured with a context, found by a
    backward scan of the group's epoch index that stops at it and never goes
    past ``TITLE_CONTEXT_EPOCH_LIMIT`` epochs. With none in reach, the turns of
    those epochs are all unread, and nothing is carried forward.
    """
    row = await conn.fetchrow(
        """
        SELECT workspace_epoch, title_context
        FROM agent_sandbox_snapshots
        WHERE sandbox_id = $1 AND id <> $2
          AND workspace_epoch < $3 AND workspace_epoch >= $3 - $4
          AND captured_at IS NOT NULL
          AND title_context <> $5::jsonb
        ORDER BY workspace_epoch DESC
        LIMIT 1
        """,
        sandbox_id,
        snapshot_id,
        workspace_epoch,
        TITLE_CONTEXT_EPOCH_LIMIT,
        EMPTY_TITLE_CONTEXT,
    )
    if row is None:
        return workspace_epoch - TITLE_CONTEXT_EPOCH_LIMIT - 1, []
    stored = load_json(row["title_context"])
    messages = stored.get("messages") if isinstance(stored, dict) else None
    return int(row["workspace_epoch"]), messages if isinstance(messages, list) else []


async def snapshot_title_context(
    conn: Any, *, snapshot_id: int, sandbox_id: str, workspace_epoch: int
) -> str:
    """Build one bounded, incremental title input for an immutable snapshot.

    The newest earlier snapshot with a stored context already holds the bounded
    tail through its boundary, so only the turns of the epochs after it are
    read: this snapshot's, and those of any epoch between whose capture stored
    none (:func:`_earlier_context`). A failed epoch's turns stay that
    snapshot's, yet this capture, cut from the same published base, carries
    their changes. Both SQL result sets are capped before text crosses the
    database boundary, and stable message ids deduplicate the deliberate
    "publish the newest turn again" case after a failed or no-change
    publication.
    """
    since, previous_messages = await _earlier_context(
        conn, snapshot_id=snapshot_id, sandbox_id=sandbox_id, workspace_epoch=workspace_epoch
    )
    message_rows = await conn.fetch(
        """
        SELECT message.id AS message_id, message.thread_id, message.job_id,
               message.role, LEFT(message.content, $4) AS content
        FROM (
            -- The newest turns of the unread epochs, through the group's epoch
            -- index. The LIMIT keeps the plan starting from them: matched as
            -- a bare IN list over a long gap, the planner walked the whole
            -- transcript backwards by primary key instead.
            SELECT included.job_id
            FROM agent_sandbox_snapshots snapshot
            JOIN agent_snapshot_jobs included ON included.snapshot_id = snapshot.id
            WHERE snapshot.sandbox_id = $1
              AND snapshot.workspace_epoch > $2 AND snapshot.workspace_epoch <= $3
            GROUP BY included.job_id
            ORDER BY MAX(included.created_at) DESC, included.job_id DESC
            LIMIT $5
        ) recent
        JOIN agent_jobs history ON history.id = recent.job_id
        JOIN agent_thread_messages message ON message.job_id = history.id
            AND message.thread_id = history.thread_id
        LEFT JOIN agent_job_events event ON event.id = message.source_event_id
        WHERE message.role = 'user'
           OR (message.role = 'assistant' AND (
               message.source_event_id IS NULL
               OR event.attempt_id = history.current_attempt_id
           ))
        ORDER BY message.id DESC
        LIMIT $5
        """,
        sandbox_id,
        since,
        workspace_epoch,
        TITLE_CONTEXT_MESSAGE_CHARS,
        TITLE_CONTEXT_MESSAGE_LIMIT,
    )
    messages_by_id: dict[int, dict[str, Any]] = {}
    for item in previous_messages:
        if not isinstance(item, dict):
            continue
        message_id = item.get("message_id")
        if isinstance(message_id, int):
            messages_by_id[message_id] = item
    for row in reversed(message_rows):
        message_id = int(row["message_id"])
        messages_by_id[message_id] = {
            "message_id": message_id,
            "thread_id": row["thread_id"],
            "job_id": row["job_id"],
            "role": row["role"],
            "content": row["content"],
        }
    candidate_messages = [
        messages_by_id[message_id]
        for message_id in sorted(messages_by_id)[-TITLE_CONTEXT_MESSAGE_LIMIT:]
    ]
    messages = []
    retained_chars = 0
    for message in reversed(candidate_messages):
        content = message.get("content")
        if not isinstance(content, str):
            continue
        if retained_chars + len(content) > TITLE_CONTEXT_TOTAL_CHARS:
            break
        messages.append(message)
        retained_chars += len(content)
    messages.reverse()

    summary_rows = await conn.fetch(
        """
        SELECT included.job_id, LEFT(summary.content, $2) AS content,
               COUNT(*) OVER () AS current_summary_count
        FROM agent_snapshot_jobs included
        JOIN agent_jobs included_job ON included_job.id = included.job_id
        JOIN LATERAL (
            SELECT message.content
            FROM agent_thread_messages message
            JOIN agent_job_events event ON event.id = message.source_event_id
            WHERE message.job_id = included.job_id
              AND message.thread_id = included_job.thread_id
              AND message.role = 'assistant'
              AND event.attempt_id = included_job.current_attempt_id
            ORDER BY message.id DESC
            LIMIT 1
        ) summary ON TRUE
        WHERE included.snapshot_id = $1
        ORDER BY included.created_at DESC, included.job_id DESC
        LIMIT $3
        """,
        snapshot_id,
        TITLE_CONTEXT_SUMMARY_CHARS,
        TITLE_CONTEXT_SUMMARY_LIMIT,
    )
    summaries = [
        {"job_id": str(row["job_id"]), "content": row["content"]} for row in reversed(summary_rows)
    ]
    summary_count = int(summary_rows[0]["current_summary_count"]) if summary_rows else 0
    return json.dumps(
        {"messages": messages, "summaries": summaries, "summary_count": summary_count},
        ensure_ascii=False,
    )


async def claimed_title_context(
    conn: Any, *, snapshot_id: int, sandbox_id: str, workspace_epoch: int, stored: Any
) -> dict[str, Any]:
    """The title input a publication claim hands the publisher.

    A snapshot captured before 0096, or by the release before it while this one
    rolls out, still holds the empty default. Its context is built here, inside
    the claim's transaction and from the same bounded reads as a capture's, and
    stored, so a retried claim and the group's next capture continue from it.
    Returns ``session_messages``, ``session_summaries`` and
    ``session_summary_count``.
    """
    context = load_json(stored)
    # COMPAT(remove-after=2026-12-31): only a capture made before 0096, or by
    # the release before it, leaves the default for a claim to find; once none
    # can still be waiting to publish, nothing reaches this branch.
    if context == _EMPTY:
        built = await snapshot_title_context(
            conn, snapshot_id=snapshot_id, sandbox_id=sandbox_id, workspace_epoch=workspace_epoch
        )
        await conn.execute(
            "UPDATE agent_sandbox_snapshots SET title_context = $2::jsonb WHERE id = $1",
            snapshot_id,
            built,
        )
        context = json.loads(built)
    if not isinstance(context, dict):
        context = {}
    stored_messages = context.get("messages")
    session_messages = []
    if isinstance(stored_messages, list):
        session_messages = [
            {key: message.get(key) for key in ("thread_id", "job_id", "role", "content")}
            for message in stored_messages
            if isinstance(message, dict)
        ]
    stored_summaries = context.get("summaries")
    session_summaries = []
    if isinstance(stored_summaries, list):
        session_summaries = [
            summary["content"]
            for summary in stored_summaries
            if isinstance(summary, dict) and isinstance(summary.get("content"), str)
        ]
    return {
        "session_messages": session_messages,
        "session_summaries": session_summaries,
        "session_summary_count": context.get("summary_count", len(session_summaries)),
    }
