"""Deployment agents and intents (``agent_deploy_*``)."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

import asyncpg

from cloud_agent.storage._sql.rows import load_json
from cloud_agent_shared.deployment_contract import TERMINAL_STATES, can_transition

if TYPE_CHECKING:
    from collections.abc import Mapping
    from datetime import datetime


_AGENT_COLUMNS = (
    "agent_id, name, role, machine, public_key, report, enrolled_by, enrolled_at, "
    "last_report_at, next_seq, revoked_at"
)
_INTENT_COLUMNS = (
    "intent_id, agent_id, seq, operation, params, idempotency_key, origin, state, detail, "
    "evidence, workflow_id, created_by, created_at, expires_at, delivered_at, finished_at"
)


def _agent(row: Any) -> dict[str, Any]:
    item = dict(row)
    item["report"] = load_json(item.get("report")) or {}
    return item


def _intent(row: Any, *, with_sealed: bool = False) -> dict[str, Any]:
    item = dict(row)
    item["params"] = load_json(item.get("params")) or {}
    item["evidence"] = load_json(item.get("evidence")) or {}
    if with_sealed:
        item["sealed"] = load_json(item.get("sealed")) or {}
    else:
        item.pop("sealed", None)
    return item


class PostgresDeploymentAgentRepository:
    """The deployment-agent capability over the control plane's pool."""

    def __init__(self, pool: asyncpg.Pool) -> None:
        """Bind to the pool."""
        self._pool = pool

    async def create_enroll_token(
        self, *, token_hash: str, role: str, created_by: str, expires_at: datetime
    ) -> None:
        """Store a one-time token's digest."""
        async with self._pool.acquire() as conn:
            await conn.execute(
                "INSERT INTO agent_deploy_enroll_tokens (token_hash, role, created_by, expires_at) "
                "VALUES ($1, $2, $3, $4)",
                token_hash,
                role,
                created_by,
                expires_at,
            )

    async def register_agent(
        self,
        *,
        token_hash: str,
        agent_id: str,
        name: str,
        role: str,
        machine: str,
        credential_hash: str,
        public_key: str,
    ) -> dict[str, Any] | None:
        """Consume an unexpired token of the same role and create the agent, or ``None``."""
        async with self._pool.acquire() as conn, conn.transaction():
            token = await conn.fetchrow(
                """
                UPDATE agent_deploy_enroll_tokens
                SET consumed_at = NOW(), agent_id = $3
                WHERE token_hash = $1 AND role = $2 AND consumed_at IS NULL AND expires_at > NOW()
                RETURNING created_by
                """,
                token_hash,
                role,
                agent_id,
            )
            if token is None:
                return None
            row = await conn.fetchrow(
                f"""
                INSERT INTO agent_deploy_agents (
                    agent_id, name, role, machine, credential_hash, public_key, enrolled_by
                ) VALUES ($1, $2, $3, $4, $5, $6, $7)
                ON CONFLICT DO NOTHING
                RETURNING {_AGENT_COLUMNS}
                """,
                agent_id,
                name,
                role,
                machine,
                credential_hash,
                public_key,
                token["created_by"],
            )
            if row is None:
                # A live agent already has this name: refuse, and keep the token unspent.
                raise ValueError("an agent with this name is already enrolled")
            return _agent(row)

    async def agent_by_credential(self, credential_hash: str) -> dict[str, Any] | None:
        """The unrevoked agent holding this credential."""
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow(
                f"SELECT {_AGENT_COLUMNS} FROM agent_deploy_agents "
                "WHERE credential_hash = $1 AND revoked_at IS NULL",
                credential_hash,
            )
        return None if row is None else _agent(row)

    async def get_agent(self, agent_id: str) -> dict[str, Any] | None:
        """One agent."""
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow(
                f"SELECT {_AGENT_COLUMNS} FROM agent_deploy_agents WHERE agent_id = $1", agent_id
            )
        return None if row is None else _agent(row)

    async def list_agents(self) -> list[dict[str, Any]]:
        """Every agent, newest report first."""
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(
                f"SELECT {_AGENT_COLUMNS} FROM agent_deploy_agents "
                "ORDER BY revoked_at IS NOT NULL, last_report_at DESC NULLS LAST, name"
            )
        return [_agent(row) for row in rows]

    async def revoke_agent(self, agent_id: str) -> bool:
        """Revoke an agent's credential; its pending intents are cancelled."""
        async with self._pool.acquire() as conn, conn.transaction():
            status = await conn.execute(
                "UPDATE agent_deploy_agents SET revoked_at = NOW() "
                "WHERE agent_id = $1 AND revoked_at IS NULL",
                agent_id,
            )
            await conn.execute(
                "UPDATE agent_deploy_intents SET state = 'cancelled', finished_at = NOW(), "
                "detail = 'the agent was revoked' WHERE agent_id = $1 AND state = 'pending'",
                agent_id,
            )
        return status.endswith(" 1")

    async def record_report(
        self, agent_id: str, report: Mapping[str, Any], operations: list[Mapping[str, Any]]
    ) -> None:
        """Store the agent's report and move its intents forward from the journal summary.

        A result the agent could not deliver (the control plane was restarting,
        for example) arrives again here; only a forward transition is applied,
        so a stale summary never moves an intent back.
        """
        async with self._pool.acquire() as conn, conn.transaction():
            await conn.execute(
                "UPDATE agent_deploy_agents SET report = $2::jsonb, last_report_at = NOW() "
                "WHERE agent_id = $1",
                agent_id,
                json.dumps({**dict(report), "operations": list(operations)}, default=str),
            )
            for summary in operations:
                intent_id = summary.get("intent_id")
                state = summary.get("state")
                if not intent_id or not isinstance(state, str):
                    continue
                await self._advance(
                    conn, agent_id, str(intent_id), state, str(summary.get("detail") or ""), {}
                )

    async def _advance(
        self,
        conn: Any,
        agent_id: str,
        intent_id: str,
        state: str,
        detail: str,
        evidence: Mapping[str, Any],
    ) -> bool:
        current = await conn.fetchval(
            "SELECT state FROM agent_deploy_intents WHERE intent_id = $1 AND agent_id = $2 "
            "FOR UPDATE",
            intent_id,
            agent_id,
        )
        if current is None or current == state or not _reachable(current, state):
            return False
        await conn.execute(
            """
            UPDATE agent_deploy_intents
            SET state = $3,
                detail = left($4, 2000),
                evidence = CASE WHEN $5::jsonb = '{}'::jsonb THEN evidence ELSE $5::jsonb END,
                finished_at = CASE WHEN $6 THEN NOW() ELSE finished_at END
            WHERE intent_id = $1 AND agent_id = $2
            """,
            intent_id,
            agent_id,
            state,
            detail,
            json.dumps(dict(evidence), default=str),
            state in TERMINAL_STATES,
        )
        return True

    async def create_intent(
        self,
        *,
        agent_id: str,
        intent_id: str,
        operation: str,
        params: Mapping[str, Any],
        sealed: Mapping[str, Any],
        idempotency_key: str,
        created_by: str,
        expires_at: datetime,
        workflow_id: str | None = None,
    ) -> dict[str, Any]:
        """Create an intent with the agent's next sequence number, or return the existing one."""
        try:
            return await self._insert_intent(
                agent_id=agent_id,
                intent_id=intent_id,
                operation=operation,
                params=params,
                sealed=sealed,
                idempotency_key=idempotency_key,
                created_by=created_by,
                expires_at=expires_at,
                workflow_id=workflow_id,
            )
        except asyncpg.UniqueViolationError:
            # Two advances of one workflow raced to the same step; one row won.
            async with self._pool.acquire() as conn:
                winner = await conn.fetchrow(
                    f"SELECT {_INTENT_COLUMNS} FROM agent_deploy_intents "
                    "WHERE agent_id = $1 AND idempotency_key = $2",
                    agent_id,
                    idempotency_key,
                )
            if winner is None:
                raise
            return _intent(winner)

    async def _insert_intent(
        self,
        *,
        agent_id: str,
        intent_id: str,
        operation: str,
        params: Mapping[str, Any],
        sealed: Mapping[str, Any],
        idempotency_key: str,
        created_by: str,
        expires_at: datetime,
        workflow_id: str | None,
    ) -> dict[str, Any]:
        async with self._pool.acquire() as conn, conn.transaction():
            existing = await conn.fetchrow(
                f"SELECT {_INTENT_COLUMNS} FROM agent_deploy_intents "
                "WHERE agent_id = $1 AND idempotency_key = $2",
                agent_id,
                idempotency_key,
            )
            if existing is not None:
                return _intent(existing)
            seq = await conn.fetchval(
                "UPDATE agent_deploy_agents SET next_seq = next_seq + 1 "
                "WHERE agent_id = $1 AND revoked_at IS NULL RETURNING next_seq - 1",
                agent_id,
            )
            if seq is None:
                raise LookupError("no such agent")
            row = await conn.fetchrow(
                f"""
                INSERT INTO agent_deploy_intents (
                    intent_id, agent_id, seq, operation, params, sealed, idempotency_key,
                    state, created_by, expires_at, workflow_id
                ) VALUES ($1, $2, $3, $4, $5::jsonb, $6::jsonb, $7, 'pending', $8, $9, $10)
                RETURNING {_INTENT_COLUMNS}
                """,
                intent_id,
                agent_id,
                seq,
                operation,
                json.dumps(dict(params)),
                json.dumps(dict(sealed)),
                idempotency_key,
                created_by,
                expires_at,
                workflow_id,
            )
        return _intent(row)

    async def deliver_intents(self, agent_id: str, after_seq: int) -> list[dict[str, Any]]:
        """Pending intents above ``after_seq``, marked delivered; overdue ones expire."""
        async with self._pool.acquire() as conn, conn.transaction():
            await conn.execute(
                "UPDATE agent_deploy_intents SET state = 'expired', finished_at = NOW(), "
                "detail = 'not fetched before it expired' "
                "WHERE agent_id = $1 AND state = 'pending' AND expires_at <= NOW()",
                agent_id,
            )
            rows = await conn.fetch(
                f"""
                UPDATE agent_deploy_intents
                SET state = 'delivered', delivered_at = NOW()
                WHERE agent_id = $1 AND state = 'pending' AND seq > $2
                RETURNING {_INTENT_COLUMNS}, sealed
                """,
                agent_id,
                after_seq,
            )
        return sorted((_intent(row, with_sealed=True) for row in rows), key=lambda i: i["seq"])

    async def update_intent(
        self,
        *,
        agent_id: str,
        intent_id: str,
        state: str,
        detail: str,
        evidence: Mapping[str, Any],
    ) -> bool:
        """Move one of the agent's intents forward; refuse any other transition."""
        async with self._pool.acquire() as conn, conn.transaction():
            moved = await self._advance(conn, agent_id, intent_id, state, detail, evidence)
            if moved and state in TERMINAL_STATES:
                # The sealed values have done their job; keep no ciphertext either.
                await conn.execute(
                    "UPDATE agent_deploy_intents SET sealed = '{}'::jsonb WHERE intent_id = $1",
                    intent_id,
                )
            return moved

    async def cancel_intent(self, intent_id: str) -> bool:
        """Cancel a pending intent."""
        async with self._pool.acquire() as conn:
            status = await conn.execute(
                "UPDATE agent_deploy_intents SET state = 'cancelled', finished_at = NOW(), "
                "sealed = '{}'::jsonb WHERE intent_id = $1 AND state = 'pending'",
                intent_id,
            )
        return status.endswith(" 1")

    async def list_intents(
        self, *, agent_id: str | None = None, workflow_id: str | None = None, limit: int = 100
    ) -> list[dict[str, Any]]:
        """Recent intents, newest first, without their sealed values."""
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(
                f"""
                SELECT {_INTENT_COLUMNS} FROM agent_deploy_intents
                WHERE ($1::text IS NULL OR agent_id = $1)
                  AND ($2::text IS NULL OR workflow_id = $2)
                ORDER BY created_at DESC
                LIMIT $3
                """,
                agent_id,
                workflow_id,
                max(1, min(limit, 500)),
            )
        return [_intent(row) for row in rows]

    # -- workflows ----------------------------------------------------------

    async def create_workflow(
        self,
        *,
        workflow_id: str,
        kind: str,
        step: str,
        params: Mapping[str, Any],
        created_by: str,
    ) -> dict[str, Any] | None:
        """Start a workflow, or ``None`` while another of the same kind is unfinished."""
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow(
                f"""
                INSERT INTO agent_deploy_workflows (workflow_id, kind, state, step, params, created_by)
                VALUES ($1, $2, 'running', $3, $4::jsonb, $5)
                ON CONFLICT DO NOTHING
                RETURNING {_WORKFLOW_COLUMNS}
                """,
                workflow_id,
                kind,
                step,
                json.dumps(dict(params)),
                created_by,
            )
        return None if row is None else _workflow(row)

    async def get_workflow(self, workflow_id: str) -> dict[str, Any] | None:
        """One workflow."""
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow(
                f"SELECT {_WORKFLOW_COLUMNS} FROM agent_deploy_workflows WHERE workflow_id = $1",
                workflow_id,
            )
        return None if row is None else _workflow(row)

    async def list_workflows(
        self, *, active_only: bool = False, limit: int = 50
    ) -> list[dict[str, Any]]:
        """Recent workflows, newest first."""
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(
                f"""
                SELECT {_WORKFLOW_COLUMNS} FROM agent_deploy_workflows
                WHERE NOT $1 OR state IN ('running', 'waiting')
                ORDER BY created_at DESC LIMIT $2
                """,
                active_only,
                max(1, min(limit, 200)),
            )
        return [_workflow(row) for row in rows]

    async def update_workflow(
        self,
        workflow_id: str,
        *,
        expected_version: int,
        state: str,
        step: str,
        detail: str,
        data: Mapping[str, Any],
    ) -> dict[str, Any] | None:
        """Move a workflow on, only from the version the caller read (``None`` if it moved)."""
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow(
                f"""
                UPDATE agent_deploy_workflows
                SET state = $3, step = $4, detail = left($5, 2000), data = $6::jsonb,
                    version = version + 1, updated_at = NOW()
                WHERE workflow_id = $1 AND version = $2
                RETURNING {_WORKFLOW_COLUMNS}
                """,
                workflow_id,
                expected_version,
                state,
                step,
                detail,
                json.dumps(dict(data), default=str),
            )
        return None if row is None else _workflow(row)


_WORKFLOW_COLUMNS = (
    "workflow_id, kind, state, step, params, data, detail, version, created_by, created_at, "
    "updated_at"
)


def _workflow(row: Any) -> dict[str, Any]:
    item = dict(row)
    item["params"] = load_json(item.get("params")) or {}
    item["data"] = load_json(item.get("data")) or {}
    return item


def _reachable(current: str, target: str) -> bool:
    """Whether ``target`` is ``current``'s successor, possibly through ``running``."""
    if can_transition(current, target):
        return True
    return current == "delivered" and can_transition("running", target)


__all__ = ["PostgresDeploymentAgentRepository"]
