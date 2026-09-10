"""Sandbox grouping SQL shared by reserve, admission and settlement."""

from __future__ import annotations

from typing import Any

import cloud_agent.storage._sql.rows as _rows


async def reserve_shared_thread_sandbox(
    self,
    conn: Any,
    *,
    thread_id: str,
    attempt_id: int,
    host: str,
    sandbox_id: str,
    container_name: str,
    runtime_id: str,
    runtime_version: str | None,
    sandbox_image_digest: str | None,
    execution_profile: str,
    resident_slot_capacity: int,
    memory_limit: str,
    cpu_limit: str,
    stop_timeout_seconds: int,
    user_id: str,
    job_id: str,
) -> dict[str, Any] | None:
    """Reserve one independently fenced member in a shared-live group."""
    if not 2 <= resident_slot_capacity <= 128:
        raise ValueError("shared sandbox resident slot capacity must be between 2 and 128")
    historical = await conn.fetchval(
        "SELECT 1 FROM agent_thread_sandboxes WHERE thread_id = $1",
        thread_id,
    )
    # Lock membership first everywhere; Docker reconciliation uses the
    # same order before it fences the shared group row.
    await conn.fetchval(
        """
        SELECT thread_id
        FROM agent_thread_sandbox_memberships
        WHERE thread_id = $1
        FOR UPDATE
        """,
        thread_id,
    )
    membership = await conn.fetchrow(
        """
        SELECT m.*, g.origin_thread_id
        FROM agent_thread_sandbox_memberships m
        JOIN agent_sandboxes g ON g.id = m.sandbox_id
        WHERE m.thread_id = $1 AND g.mode = 'shared_live'
        FOR UPDATE OF g
        """,
        thread_id,
    )
    if historical is not None and membership is None:
        raise ValueError("this historical chat has no runnable shared sandbox")
    if membership is None:
        capability = await conn.fetchval(
            """
            SELECT 1 FROM agent_runner_hosts
            WHERE host = $1 AND status = 'online'
              AND capabilities @> '["shared-sandbox-v1"]'::jsonb
            """,
            host,
        )
        if capability is None:
            raise ValueError("host has not advertised shared-sandbox-v1")
        await conn.execute(
            """
            INSERT INTO agent_sandboxes (
                id, origin_thread_id, user_id, host, container_name,
                runtime_id, runtime_version, sandbox_image_digest,
                execution_profile, resident_slot_capacity, memory_limit,
                cpu_limit, stop_timeout_seconds
            )
            VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13)
            """,
            sandbox_id,
            thread_id,
            user_id,
            host,
            container_name,
            runtime_id,
            runtime_version,
            sandbox_image_digest,
            execution_profile,
            resident_slot_capacity,
            memory_limit,
            cpu_limit,
            stop_timeout_seconds,
        )
        await conn.execute(
            """
            INSERT INTO agent_thread_sandbox_memberships (
                thread_id, sandbox_id, resident_slot_id
            ) VALUES ($1, $2, 0)
            """,
            thread_id,
            sandbox_id,
        )
        membership = await conn.fetchrow(
            """
            SELECT m.*, g.origin_thread_id
            FROM agent_thread_sandbox_memberships m
            JOIN agent_sandboxes g ON g.id = m.sandbox_id
            WHERE m.thread_id = $1
            FOR UPDATE OF g
            """,
            thread_id,
        )
    if membership is None:
        return None
    group = await conn.fetchrow(
        "SELECT * FROM agent_sandboxes WHERE id = $1 FOR UPDATE",
        membership["sandbox_id"],
    )
    if group is None:
        return None
    immutable = {
        "id": sandbox_id,
        "host": host,
        "container_name": container_name,
        "runtime_id": runtime_id,
        "runtime_version": runtime_version,
        "sandbox_image_digest": sandbox_image_digest,
        "execution_profile": execution_profile,
        "memory_limit": memory_limit,
        "cpu_limit": cpu_limit,
        "stop_timeout_seconds": stop_timeout_seconds,
    }
    if any(group[key] != value for key, value in immutable.items()):
        raise ValueError("shared sandbox immutable identity or runtime changed")
    if group["state"] in {"lost", "stopped_dirty"} or membership["native_state"] == "lost":
        return None
    if membership["fork_operation_id"] is not None:
        # The dequeue query carries the same fence, but admission is a
        # separate transaction and may race a Fork request that wins after
        # claim. Never append a turn to the source session while its
        # settled prefix is being materialized.
        return None
    active_attempt = membership["active_attempt_id"]
    if active_attempt is not None and active_attempt != attempt_id:
        return None
    admitted_job = membership["admitted_job_id"]
    if admitted_job is not None and admitted_job != job_id:
        return None
    already_admitted = active_attempt == attempt_id and group["state"] in {
        "starting",
        "running",
    }
    activation_generation = int(membership["activation_generation"])
    if not already_admitted:
        activation_generation += 1
    context_status = "new" if membership["last_settled_job_id"] is None else "restored"
    await conn.execute(
        """
        UPDATE agent_thread_sandbox_memberships
        SET active_attempt_id = $2, activation_generation = $3,
            context_status = $4, resident_state = CASE
                WHEN resident_state = 'live' THEN resident_state ELSE 'starting' END,
            last_error = NULL, updated_at = NOW()
        WHERE thread_id = $1
        """,
        thread_id,
        attempt_id,
        activation_generation,
        context_status,
    )
    await conn.execute(
        """
        UPDATE agent_sandboxes
        SET state = 'starting', idle_deadline = NULL,
            last_activity_at = NOW(), updated_at = NOW()
        WHERE id = $1
        """,
        group["id"],
    )
    row = await conn.fetchrow(
        f"SELECT s.* FROM {_rows._THREAD_SANDBOX_SOURCE} WHERE s.thread_id = $1",
        thread_id,
    )
    return None if row is None else _rows.sandbox_row_to_dict(row)
