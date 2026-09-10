"""Row projections shared by the repositories that read the same tables."""

from __future__ import annotations

import json
from typing import Any

import cloud_agent.storage._sql.rows as _rows
import cloud_agent.storage._sql.session_forks as _session_forks
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
    result["group_members"] = _rows.load_json(result.get("group_members")) or []
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
        "cancel_requested": row["cancel_requested"],
        "terminal_resume_pending": row["terminal_resume_pending"],
        "retained_followup_eligible": row["retained_followup_eligible"],
        "current_attempt_id": row["current_attempt_id"],
        "published_pr_url": row["published_pr_url"],
        "published_commit_sha": row["published_commit_sha"],
        "detail": row["detail"],
        "metadata": _rows.load_json(row["metadata"]),
        "fork_source_job_id": row["fork_source_job_id"],
        # Resolved once, at creation, against the deployment registry — not
        # re-read per attempt. A job runs with the tool surface it was created
        # with, so removing a server from the registry stops new jobs from
        # asking for it without changing what a running one was promised.
        "mcp_servers": list(row["mcp_servers"] or []),
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
        "pinned_at": row.get("pinned_at"),
        "archived_at": row.get("archived_at"),
        "deleted_at": row.get("deleted_at"),
        "thread_sandbox": _rows.load_json(row.get("thread_sandbox")),
    }


_JOB_COLUMNS = (
    "id, thread_id, parent_job_id, turn_no, native_turn_no, user_id, repo, base_sha, task_prompt, "
    "setup_script, runtime, model, execution_profile, reasoning_effort, state, "
    "cancel_requested, terminal_resume_pending, retained_followup_eligible, current_attempt_id, "
    "published_pr_url, published_commit_sha, detail, "
    "metadata, fork_source_job_id, mcp_servers, "
    "created_at, updated_at"
)


_QUALIFIED_JOB_COLUMNS = ", ".join(
    f"j.{column.strip()}" for column in _rows._JOB_COLUMNS.split(",")
)


_THREAD_SANDBOX_SOURCE = f"""
(
    SELECT
        m.thread_id, g.id AS sandbox_id, g.generation, g.host, g.container_id,
        g.container_name, g.runtime_id, g.runtime_version,
        g.sandbox_image_digest, g.execution_profile, g.state,
        g.memory_limit, g.cpu_limit, g.stop_timeout_seconds,
        m.context_status, m.activation_generation, m.process_generation,
        m.active_attempt_id, m.admitted_job_id, m.admitted_at,
        m.last_settled_job_id, m.native_session_id, g.idle_deadline,
        COALESCE(m.last_error, g.last_error) AS last_error,
        m.created_at, m.updated_at, m.fork_operation_id,
        TRUE AS shared_live, g.origin_thread_id, m.resident_state,
        m.residency_generation, m.resident_slot_id,
        g.resident_slot_capacity, m.native_state,
        m.forked_from_thread_id, m.fork_anchor_job_id,
        g.last_activity_at AS group_last_activity_at,
        (
            SELECT snapshot.state
            FROM agent_sandbox_snapshots snapshot
            WHERE snapshot.sandbox_id = g.id
            ORDER BY snapshot.workspace_epoch DESC, snapshot.id DESC
            LIMIT 1
        ) AS workspace_publication_state,
        g.workspace_epoch,
        EXISTS (
            SELECT 1 FROM agent_runner_hosts h
            WHERE h.host = g.host
              AND h.status = 'online'
              AND h.capabilities @> '{_session_forks._NATIVE_FORK_CAPABILITY_JSON}'::jsonb
              AND h.sandbox_image_digest = g.sandbox_image_digest
        ) AS native_fork_capable,
        COALESCE((
            SELECT jsonb_agg(
                jsonb_build_object(
                    'thread_id', peer.thread_id,
                    'native_session_id', peer.native_session_id,
                    'native_state', peer.native_state,
                    'context_status', peer.context_status,
                    'activation_generation', peer.activation_generation,
                    'process_generation', peer.process_generation,
                    'residency_generation', peer.residency_generation,
                    'resident_state', peer.resident_state,
                    'resident_slot_id', peer.resident_slot_id,
                    'active_attempt_id', peer.active_attempt_id,
                    'admitted_job_id', peer.admitted_job_id,
                    'admitted_at', peer.admitted_at,
                    'last_settled_job_id', peer.last_settled_job_id,
                    'forked_from_thread_id', peer.forked_from_thread_id,
                    'fork_anchor_job_id', peer.fork_anchor_job_id
                ) ORDER BY peer.created_at, peer.thread_id
            )
            FROM agent_thread_sandbox_memberships peer
            WHERE peer.sandbox_id = g.id
        ), '[]'::jsonb) AS group_members
    FROM agent_thread_sandbox_memberships m
    JOIN agent_sandboxes g ON g.id = m.sandbox_id
    WHERE g.mode = 'shared_live'
) AS s
"""
