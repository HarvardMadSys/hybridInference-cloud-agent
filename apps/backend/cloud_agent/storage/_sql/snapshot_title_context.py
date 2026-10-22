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


async def snapshot_title_context(
    conn: Any, *, snapshot_id: int, sandbox_id: str, workspace_epoch: int
) -> str:
    """Build one bounded, incremental title input for an immutable snapshot.

    The previous snapshot already holds the bounded tail through its boundary;
    only this snapshot's newly included turns are read. Both SQL result sets
    are capped before text crosses the database boundary, and stable message
    ids deduplicate the deliberate "publish the newest turn again" case after
    a failed or no-change publication.
    """
    previous = load_json(
        await conn.fetchval(
            """
            SELECT title_context
            FROM agent_sandbox_snapshots
            WHERE sandbox_id = $1 AND id <> $2 AND workspace_epoch < $3
            ORDER BY workspace_epoch DESC
            LIMIT 1
            """,
            sandbox_id,
            snapshot_id,
            workspace_epoch,
        )
    ) or {"messages": []}
    previous_messages = previous.get("messages")
    if not isinstance(previous_messages, list):
        previous_messages = []
    message_rows = await conn.fetch(
        """
        SELECT message.id AS message_id, message.thread_id, message.job_id,
               message.role, LEFT(message.content, $2) AS content
        FROM agent_snapshot_jobs included
        JOIN agent_jobs history ON history.id = included.job_id
        JOIN agent_thread_messages message ON message.job_id = history.id
            AND message.thread_id = history.thread_id
        LEFT JOIN agent_job_events event ON event.id = message.source_event_id
        WHERE included.snapshot_id = $1
          AND (
              message.role = 'user'
              OR (message.role = 'assistant' AND (
                  message.source_event_id IS NULL
                  OR event.attempt_id = history.current_attempt_id
              ))
          )
        ORDER BY message.id DESC
        LIMIT $3
        """,
        snapshot_id,
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
