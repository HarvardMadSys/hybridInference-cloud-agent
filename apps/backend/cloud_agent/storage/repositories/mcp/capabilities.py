"""A connection's discovered capabilities and a session's bindings to them."""

from __future__ import annotations

import json
from typing import Any

from cloud_agent.storage._sql.mcp import ensure_mcp_bindings, mcp_binding_row
from cloud_agent.storage._sql.rows import load_json as _load_json
from cloud_agent.storage.repositories.mcp._common import McpRepositoryBase


class CapabilitiesMixin(McpRepositoryBase):
    """Store capabilities and a session's bindings."""

    async def get_mcp_capabilities(self, *, connection_id: int) -> dict[str, Any] | None:
        """The stored capability snapshot for one connection."""
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT * FROM agent_mcp_capabilities WHERE connection_id = $1",
                connection_id,
            )
        if row is None:
            return None
        record = dict(row)
        for key in ("tools", "resources", "resource_templates", "prompts", "support"):
            record[key] = _load_json(record.get(key)) or ({} if key == "support" else [])
        return record

    async def save_mcp_capabilities(
        self,
        *,
        connection_id: int,
        config_revision: int,
        protocol_version: str,
        server_name: str,
        server_version: str,
        tools: list[dict[str, Any]],
        resources: list[dict[str, Any]],
        resource_templates: list[dict[str, Any]],
        prompts: list[dict[str, Any]],
        support: dict[str, Any],
        partial: bool = False,
        expected_authorization_epoch: int | None = None,
        expected_capability_revision: int | None = None,
        expected_credential_version: int | None = None,
    ) -> int:
        """Store a discovery result and return the new capability revision.

        The revision is incremented in the same statement that writes the
        snapshot, so a binding's fence can never be checked against a snapshot
        written by a different discovery. ``partial`` records a discovery that
        succeeded on some surfaces and failed on others: the connection is
        usable, and the interface says which part is missing rather than
        presenting a short catalogue as the whole truth.
        """
        async with self._pool.acquire() as conn, conn.transaction():
            revision = await conn.fetchval(
                """
                UPDATE agent_mcp_connections
                SET capability_revision = capability_revision + 1, updated_at = NOW()
                WHERE id = $1 AND deleted_at IS NULL
                  AND config_revision = $2
                  AND ($3::bigint IS NULL OR authorization_epoch = $3)
                  AND ($4::bigint IS NULL OR capability_revision = $4)
                  AND ($5::bigint IS NULL OR credential_version = $5)
                RETURNING capability_revision
                """,
                connection_id,
                config_revision,
                expected_authorization_epoch,
                expected_capability_revision,
                expected_credential_version,
            )
            if revision is None:
                return 0
            await conn.execute(
                """
                INSERT INTO agent_mcp_capabilities
                    (connection_id, capability_revision, config_revision, protocol_version,
                     server_name, server_version, tools, resources, resource_templates,
                     prompts, support, discovered_at)
                VALUES ($1, $2, $3, $4, $5, $6, $7::jsonb, $8::jsonb, $9::jsonb,
                        $10::jsonb, $11::jsonb, NOW())
                ON CONFLICT (connection_id) DO UPDATE SET
                    capability_revision = EXCLUDED.capability_revision,
                    config_revision = EXCLUDED.config_revision,
                    protocol_version = EXCLUDED.protocol_version,
                    server_name = EXCLUDED.server_name,
                    server_version = EXCLUDED.server_version,
                    tools = EXCLUDED.tools,
                    resources = EXCLUDED.resources,
                    resource_templates = EXCLUDED.resource_templates,
                    prompts = EXCLUDED.prompts,
                    support = EXCLUDED.support,
                    discovered_at = NOW()
                """,
                connection_id,
                revision,
                config_revision,
                protocol_version,
                server_name,
                server_version,
                json.dumps(tools),
                json.dumps(resources),
                json.dumps(resource_templates),
                json.dumps(prompts),
                json.dumps({**support, "partial": partial}),
            )
        return int(revision)

    async def list_mcp_bindings(self, *, thread_id: str) -> list[dict[str, Any]]:
        """Ensure default bindings for the owner's servers and return all overrides."""
        async with self._pool.acquire() as conn, conn.transaction():
            await ensure_mcp_bindings(conn, thread_id=thread_id)
            rows = await conn.fetch(
                """
                SELECT b.thread_id, b.connection_id, b.alias, b.enabled, b.selection_pending,
                       b.config_revision, b.authorization_epoch, b.capability_revision,
                       b.policy_revision, b.rules, b.resource_rules, b.prompt_rules,
                       c.label, c.transport, c.location, c.status AS connection_status,
                       c.error_kind, c.error_detail, c.deleted_at
                FROM agent_mcp_thread_bindings b
                JOIN agent_mcp_connections c ON c.id = b.connection_id
                WHERE b.thread_id = $1
                ORDER BY b.alias, b.connection_id
                """,
                thread_id,
            )
        bindings: list[dict[str, Any]] = []
        for row in rows:
            record = dict(row)
            for key in ("rules", "resource_rules", "prompt_rules"):
                record[key] = _load_json(record.get(key)) or {}
            bindings.append(record)
        return bindings

    async def replace_mcp_bindings(
        self, *, thread_id: str, bindings: list[dict[str, Any]], merge: bool = False
    ) -> None:
        """Replace a conversation's selection wholesale.

        With merge=True only named servers change. Otherwise unmentioned
        existing bindings become disabled opt-outs instead of disappearing.
        """
        async with self._pool.acquire() as conn, conn.transaction():
            # Serialize replacements even when the old selection is empty.
            await conn.fetchval("SELECT id FROM agent_threads WHERE id = $1 FOR UPDATE", thread_id)
            previous = {
                int(row["connection_id"]): mcp_binding_row(row)
                for row in await conn.fetch(
                    "SELECT * FROM agent_mcp_thread_bindings WHERE thread_id = $1 FOR UPDATE",
                    thread_id,
                )
            }
            revisions: dict[int, int] = {}
            supplied = {int(binding["connection_id"]) for binding in bindings}
            # An omitted server is explicitly disabled, never deleted: otherwise
            # the next automatic sync would re-enable a user-removed server.
            bindings = [
                *bindings,
                *[
                    old if merge else {**old, "enabled": False}
                    for connection_id, old in previous.items()
                    if connection_id not in supplied
                ],
            ]
            changed: set[int] = set()
            for binding in bindings:
                connection_id = int(binding["connection_id"])
                old = previous.get(connection_id)
                unchanged = old is not None and all(
                    old[key] == binding.get(key, default)
                    for key, default in (
                        ("alias", ""),
                        ("enabled", True),
                        ("selection_pending", False),
                        ("config_revision", 1),
                        ("authorization_epoch", 1),
                        ("capability_revision", 1),
                        ("rules", {}),
                        ("resource_rules", {}),
                        ("prompt_rules", {}),
                    )
                )
                revisions[connection_id] = (
                    int(old["policy_revision"])
                    if unchanged and old is not None
                    else int(await conn.fetchval("SELECT nextval('agent_mcp_policy_revision_seq')"))
                )
                if not unchanged:
                    changed.add(connection_id)
            if changed:
                # Revocation is visible to both the waiting relay and the UI.
                # A call already dispatching keeps its outcome; it is never replayed.
                await conn.execute(
                    f"UPDATE agent_mcp_invocations SET {self._EXPIRED} "
                    "WHERE thread_id = $1 AND connection_id = ANY($2::bigint[]) "
                    "AND status IN ('pending', 'approved')",
                    thread_id,
                    list(changed),
                )
            await conn.execute(
                "DELETE FROM agent_mcp_thread_bindings WHERE thread_id = $1", thread_id
            )
            for binding in bindings:
                await conn.execute(
                    """
                    INSERT INTO agent_mcp_thread_bindings
                        (thread_id, connection_id, alias, enabled, selection_pending,
                         config_revision, authorization_epoch, capability_revision,
                         policy_revision, rules, resource_rules, prompt_rules)
                    VALUES ($1, $2, $3, $4, $12, $5, $6, $7, $8, $9::jsonb, $10::jsonb, $11::jsonb)
                    """,
                    thread_id,
                    int(binding["connection_id"]),
                    str(binding["alias"]),
                    bool(binding.get("enabled", True)),
                    int(binding["config_revision"]),
                    int(binding["authorization_epoch"]),
                    int(binding["capability_revision"]),
                    revisions[int(binding["connection_id"])],
                    json.dumps(binding.get("rules") or {}),
                    json.dumps(binding.get("resource_rules") or {}),
                    json.dumps(binding.get("prompt_rules") or {}),
                    bool(binding.get("selection_pending", False)),
                )
