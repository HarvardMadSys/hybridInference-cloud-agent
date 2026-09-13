"""Session paging, and the cursors that address it.

Lifted verbatim from the single storage class this package replaced. The
text is what it was; only the module that owns it changed, so a statement
that reads these can import them without importing everything else.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import json
from datetime import datetime

from cloud_agent.storage._sql.jobs import (
    MCP_BINDING_COLUMNS,
    MCP_BINDING_JOIN,
    QUALIFIED_JOB_COLUMNS,
)


def session_query(*, filter_repo: bool = False) -> str:
    """Filter project rows before looking up each conversation's latest turn."""
    repo_filter = "AND ($5::text IS NULL OR t.repo = $5)" if filter_repo else ""
    return f"""
WITH latest AS (
    SELECT {QUALIFIED_JOB_COLUMNS}, {MCP_BINDING_COLUMNS}, t.id AS session_id, t.title AS session_title, t.title_source,
           t.archived_at, t.pinned_at, COALESCE(t.archived_at, j.created_at) AS sort_at,
           CASE
               WHEN j.state = 'queued' THEN 'queued'
               WHEN j.state = 'running' THEN 'running'
               WHEN j.state = 'succeeded' AND NULLIF(j.published_pr_url, '') IS NOT NULL THEN 'done'
               WHEN j.state = 'succeeded' THEN 'needs_review'
               ELSE j.state
           END AS display_state
    FROM agent_threads t
    CROSS JOIN LATERAL (
        SELECT * FROM agent_jobs latest_job
        WHERE latest_job.thread_id = t.id AND latest_job.user_id = $1
        ORDER BY latest_job.turn_no DESC, latest_job.id DESC
        LIMIT 1
    ) j
    {MCP_BINDING_JOIN}
    WHERE t.user_id = $1 AND t.deleted_at IS NULL
      AND (($2 AND t.archived_at IS NOT NULL) OR (NOT $2 AND t.archived_at IS NULL))
      AND ($3::text IS NULL OR STRPOS(LOWER(t.title), LOWER($3)) > 0)
      {repo_filter}
), matched AS (
    SELECT * FROM latest
    WHERE $4::text IS NULL
       OR CASE WHEN $4 IN ('done', 'needs_review', 'cancelled')
               THEN display_state ELSE conversation_state END = $4
)
"""


def session_cursor_scope(
    user_id: str, archived: bool, search: str | None, repo: str | None, state: str | None
) -> str:
    """Bind pagination to the same owner's filter selection."""
    return hashlib.sha256(json.dumps([user_id, archived, search, repo, state]).encode()).hexdigest()


def decode_session_cursor(cursor: str | None, scope: str) -> tuple[datetime | None, str | None]:
    """Validate a bounded keyset cursor before sending its values to SQL."""
    if cursor is None:
        return None, None
    try:
        if len(cursor) > 1024:
            raise ValueError
        payload = json.loads(base64.b64decode(cursor, altchars=b"-_", validate=True))
        if not isinstance(payload, dict) or payload.get("scope") != scope:
            raise ValueError
        sort_at = datetime.fromisoformat(payload["at"])
        thread_id = payload["id"]
        if (
            sort_at.tzinfo is None
            or not isinstance(thread_id, str)
            or not thread_id
            or len(thread_id) > 128
            or "\x00" in thread_id
        ):
            raise ValueError
        return sort_at, thread_id
    except (ValueError, TypeError, KeyError, binascii.Error) as exc:
        raise ValueError("Invalid session cursor for these filters") from exc


def encode_session_cursor(sort_at: datetime, thread_id: str, scope: str) -> str:
    """Encode the last conversation key, not an offset into a moving list."""
    return base64.urlsafe_b64encode(
        json.dumps({"at": sort_at.isoformat(), "id": thread_id, "scope": scope}).encode()
    ).decode()


def decode_conversation_cursor(
    cursor: str, scope: str
) -> tuple[bool, datetime | None, datetime, str]:
    """Validate a pagination boundary, including its owner and search scope."""
    try:
        if not cursor or len(cursor) > 2048:
            raise ValueError
        data = json.loads(base64.b64decode(cursor, altchars=b"-_", validate=True))
        if (
            not isinstance(data, dict)
            or type(data.get("v")) is not int
            or data["v"] != 1
            or data.get("scope") != scope
        ):
            raise ValueError
        pin = data["pin"]
        activity = datetime.fromisoformat(data["activity"])
        thread = data["thread"]
        if pin is not None:
            pin = datetime.fromisoformat(pin)
            if pin.tzinfo is None:
                raise ValueError
        if activity.tzinfo is None or not isinstance(thread, str) or not 1 <= len(thread) <= 128:
            raise ValueError
        if "\x00" in thread:
            raise ValueError
        return pin is not None, pin, activity, thread
    except (ValueError, TypeError, KeyError, binascii.Error, OverflowError, RecursionError) as exc:
        raise ValueError("Invalid conversation cursor for this search") from exc
