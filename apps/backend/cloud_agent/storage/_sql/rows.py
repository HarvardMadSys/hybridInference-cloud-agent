"""Row decoding shared by the store and the admin read models."""

from __future__ import annotations

import json
from typing import Any

from cloud_agent.logging_utils import get_logger

logger = get_logger(__name__)


def load_json(value: Any) -> Any:
    """Decode a JSONB column that asyncpg may hand back as str or object."""
    if isinstance(value, (str, bytes)):
        try:
            return json.loads(value)
        except (json.JSONDecodeError, ValueError):
            logger.warning("Failed to decode agent job JSON column", exc_info=True)
            return None
    return value


def sandbox_row_to_dict(row: Any) -> dict[str, Any]:
    """Decode the JSON members embedded in the shared sandbox read shape."""
    result = dict(row)
    result["group_members"] = load_json(result.get("group_members")) or []
    return result


def job_row_to_dict(row: Any) -> dict[str, Any]:
    """Convert an ``agent_jobs`` row to a plain dict."""
    return {
        "id": row["id"],
        "thread_id": row["thread_id"],
        "parent_job_id": row["parent_job_id"],
        "turn_no": row["turn_no"],
        "native_turn_no": row["native_turn_no"],
        "user_id": row["user_id"],
        "repo": row["repo"],
        "base_sha": row["base_sha"],
        "task_prompt": row["task_prompt"],
        "title": row.get("title") or row.get("session_title"),
        "title_source": row.get("title_source"),
        "setup_script": row["setup_script"],
        "runtime": row["runtime"],
        "model": row["model"],
        "execution_profile": row["execution_profile"],
        "reasoning_effort": row["reasoning_effort"],
        "state": row["state"],
        "conversation_state": row.get("conversation_state"),
        "cancel_requested": row["cancel_requested"],
        "terminal_resume_pending": row["terminal_resume_pending"],
        "current_attempt_id": row["current_attempt_id"],
        "published_pr_url": row["published_pr_url"],
        "published_commit_sha": row["published_commit_sha"],
        "detail": row["detail"],
        "metadata": load_json(row["metadata"]),
        "skills": load_json(row.get("skills")),
        "attachments": load_json(row.get("attachments")),
        "fork_source_job_id": row["fork_source_job_id"],
        "mcp_bindings": load_json(row.get("mcp_bindings")) or [],
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
        "pinned_at": row.get("pinned_at"),
        "archived_at": row.get("archived_at"),
        "deleted_at": row.get("deleted_at"),
        "thread_sandbox": load_json(row.get("thread_sandbox")),
    }
