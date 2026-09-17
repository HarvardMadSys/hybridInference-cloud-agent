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
