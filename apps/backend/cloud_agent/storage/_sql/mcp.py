"""MCP binding projections and transactional inserts shared by repositories."""

from __future__ import annotations

import json
from typing import Any

from cloud_agent.storage._sql.rows import load_json as _load_json


def mcp_binding_row(row: Any) -> dict[str, Any]:
    """Decode one binding row, rules included."""
    record = dict(row)
    for key in ("rules", "resource_rules", "prompt_rules"):
        record[key] = _load_json(record.get(key)) or {}
    return record


async def ensure_mcp_bindings(conn: Any, *, thread_id: str) -> None:
    """Make the owner's servers available, preserving every conversation override.

    Call inside a transaction. The thread lock also serializes explicit edits;
    missing rows are defaults, whereas disabled rows are durable opt-outs.
    Calls still require approval: automatic discovery never grants execution.
    """
    owner = await conn.fetchval(
        "SELECT user_id FROM agent_threads WHERE id = $1 FOR UPDATE", thread_id
    )
    if owner is None:
        return
    missing = await conn.fetch(
        """
        SELECT c.id, c.config_revision, c.authorization_epoch, c.capability_revision
        FROM agent_mcp_connections c
        WHERE c.user_id = $1 AND c.deleted_at IS NULL
          AND NOT EXISTS (SELECT 1 FROM agent_mcp_thread_bindings b
                          WHERE b.thread_id = $2 AND b.connection_id = c.id)
        ORDER BY c.id
        """,
        owner,
        thread_id,
    )
    if not missing:
        return
    reserved = {
        row["alias"]
        for row in await conn.fetch(
            "SELECT alias FROM agent_mcp_thread_bindings WHERE thread_id = $1", thread_id
        )
    }
    for row in missing:
        base = f"mcp{row['id']}"
        alias = base
        suffix = 1
        while alias in reserved:
            alias = f"{base}-{suffix}"
            suffix += 1
        reserved.add(alias)
        await conn.execute(
            """
            INSERT INTO agent_mcp_thread_bindings
                (thread_id, connection_id, alias, enabled, selection_pending,
                 config_revision, authorization_epoch, capability_revision, policy_revision)
            VALUES ($1, $2, $3, TRUE, TRUE, $4, $5, $6,
                    nextval('agent_mcp_policy_revision_seq'))
            ON CONFLICT (thread_id, connection_id) DO NOTHING
            """,
            thread_id,
            row["id"],
            alias,
            row["config_revision"],
            row["authorization_epoch"],
            row["capability_revision"],
        )


async def write_mcp_bindings(
    conn: Any, *, thread_id: str, bindings: list[dict[str, Any]] | None
) -> None:
    """Insert a conversation's initial MCP selection inside its transaction."""
    for binding in bindings or []:
        await conn.execute(
            """
            INSERT INTO agent_mcp_thread_bindings
                (thread_id, connection_id, alias, enabled, selection_pending,
                 config_revision, authorization_epoch, capability_revision,
                 policy_revision, rules, resource_rules, prompt_rules)
            VALUES ($1, $2, $3, $4, FALSE, $5, $6, $7, $8, $9::jsonb, $10::jsonb, $11::jsonb)
            """,
            thread_id,
            int(binding["connection_id"]),
            str(binding["alias"]),
            bool(binding.get("enabled", True)),
            int(binding["config_revision"]),
            int(binding["authorization_epoch"]),
            int(binding["capability_revision"]),
            int(binding.get("policy_revision", 1)),
            json.dumps(binding.get("rules") or {}),
            json.dumps(binding.get("resource_rules") or {}),
            json.dumps(binding.get("prompt_rules") or {}),
        )
