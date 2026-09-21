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


def thread_goal(row: Any) -> dict[str, Any] | None:
    """The conversation's live goal, or ``None`` when it has none.

    Returned under one key rather than spread across the job's own, so that a
    reader can never confuse the conversation's current objective with the
    snapshot a particular turn was run against. Reads that do not join
    ``agent_threads`` have no goal columns at all; they report ``None``.

    The revision is read under whichever name the projection gave it:
    ``JOB_SELECT`` aliases the thread's column because the job has one of the
    same name, while a goal-only read of ``agent_threads`` has no collision to
    avoid and selects the plain name.
    """
    if row.get("goal_objective") is None:
        return None
    revision = row.get("thread_goal_revision")
    if revision is None:
        revision = row.get("goal_revision")
    return {
        "goal_id": row.get("goal_id"),
        "objective": row["goal_objective"],
        "state": row["goal_state"],
        "pause_reason": row.get("goal_pause_reason"),
        "pause_detail": row.get("goal_pause_detail"),
        "revision": revision or 0,
        "max_rounds": row.get("goal_max_rounds"),
        # Derived, so only a read that asked for it has it. A list read returns
        # the objective and the state from the thread row but no count — a page
        # of conversations has no business counting attempts for each of them —
        # and zero there means "not fetched", which is what ``None`` says
        # elsewhere in this module.
        "rounds_used": row.get("goal_rounds_used") or 0,
        "repairs_used": row.get("goal_repairs_used") or 0,
        "completion_basis": row.get("goal_completion_basis"),
        "completed_by_job_id": row.get("goal_completed_by_job_id"),
        "created_at": row.get("goal_created_at"),
        "updated_at": row.get("goal_updated_at"),
    }


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
        # This turn's immutable goal snapshot: the objective it was created for,
        # what kind of round it was, and how its final message read. Distinct
        # from the conversation's live goal below — this one never changes when
        # the objective is edited.
        "turn_source": row.get("turn_source") or "human",
        "goal_round": row.get("goal_round"),
        "goal_round_kind": row.get("goal_round_kind"),
        "round_goal_revision": row.get("goal_revision"),
        "goal_id": row.get("goal_id"),
        "goal_report_parse": row.get("goal_report_parse"),
        "goal_report_outcome": row.get("goal_report_outcome"),
        "goal_report_reason": row.get("goal_report_reason"),
        "goal_report_detail": row.get("goal_report_detail"),
        "goal_repair_round": row.get("goal_repair_round"),
        "input_seq_at_create": row.get("input_seq_at_create") or 0,
        "thread_user_input_seq": row.get("thread_user_input_seq") or 0,
        # The immediately preceding turn's report, when the read provided it.
        "parent_goal_report_parse": row.get("parent_goal_report_parse"),
        "parent_goal_report_outcome": row.get("parent_goal_report_outcome"),
        # The conversation's live goal, under the job's keys as every other
        # thread-level value is. Present only on reads that join the thread.
        "goal": thread_goal(row),
        "mcp_bindings": load_json(row.get("mcp_bindings")) or [],
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
        "pinned_at": row.get("pinned_at"),
        "archived_at": row.get("archived_at"),
        "deleted_at": row.get("deleted_at"),
        "thread_sandbox": load_json(row.get("thread_sandbox")),
    }
