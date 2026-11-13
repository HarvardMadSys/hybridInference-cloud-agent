"""Fenced MCP invocations, their approvals and remembered allow rules."""

from __future__ import annotations

import asyncio
import json
from typing import Any

from cloud_agent.mcp.models import Capability, CapabilityKind
from cloud_agent.storage._sql.mcp import mcp_binding_row
from cloud_agent.storage._sql.mcp_maintenance import ALLOW_GROUP_PAGE, pending_allow_groups
from cloud_agent.storage._sql.mcp_residency import lock_mcp_residency, residency_fence_sql
from cloud_agent.storage._sql.rows import load_json as _load_json
from cloud_agent.storage.repositories.mcp._common import McpRepositoryBase, logger


def _remembered_rule_matches(intent: Any, snapshot: Any) -> bool:
    """Bind a remembered permission to the current stored capability definition."""
    if snapshot is None or any(
        snapshot[key] != intent[key] for key in ("config_revision", "capability_revision")
    ):
        return False
    surfaces = {
        "tool": "tools",
        "prompt": "prompts",
        "resource": "resources",
        "resource_template": "resource_templates",
    }
    kind = intent["capability_kind"]
    if kind not in surfaces:
        return False
    rule = _load_json(intent["always_allow_rule"])
    if not isinstance(rule, dict) or rule.get("policy") != "allow":
        return False
    for entry in _load_json(snapshot[surfaces[kind]]) or []:
        if not isinstance(entry, dict) or entry.get("name") != intent["capability_name"]:
            continue
        schema = entry.get("input_schema")
        capability = Capability(
            kind=CapabilityKind(kind),
            name=str(entry["name"]),
            description=str(entry.get("description") or ""),
            input_schema=schema if isinstance(schema, dict) else None,
        )
        return rule.get("fingerprint") == capability.fingerprint()
    return False


def fence_params(
    *,
    config_revision: int | None = None,
    authorization_epoch: int | None = None,
    capability_revision: int | None = None,
    policy_revision: int | None = None,
    attempt_id: int | None = None,
    lease_generation: int | None = None,
) -> list[Any]:
    """The values :func:`_invocation_fence_sql` numbered, in its order.

    Kept as one pair so the clauses and their arguments cannot drift apart: a
    fence whose ``$n`` refers to the wrong value silently matches the wrong
    rows, which is worse than not checking at all because it looks like it does.
    """
    values: list[Any] = []
    for value in (config_revision, authorization_epoch, capability_revision, policy_revision):
        if value is not None:
            values.append(int(value))
    if attempt_id is not None:
        values.append(attempt_id)
    if lease_generation is not None:
        values.append(lease_generation)
    return values


def _invocation_fence_sql(
    *,
    first_index: int,
    config_revision: int | None = None,
    authorization_epoch: int | None = None,
    capability_revision: int | None = None,
    policy_revision: int | None = None,
    attempt_id: int | None = None,
    lease_generation: int | None = None,
) -> str:
    """The ``AND`` clauses binding an approval to the state it was given for.

    Every clause is optional so a caller that has nothing to compare against --
    a migration, a repair script -- can still address the row. The API never
    omits them.
    """
    clauses: list[str] = []
    index = first_index
    for column, value in (
        ("config_revision", config_revision),
        ("authorization_epoch", authorization_epoch),
        ("capability_revision", capability_revision),
        ("policy_revision", policy_revision),
    ):
        if value is not None:
            clauses.append(f" AND {column} = ${index}")
            index += 1
    if attempt_id is not None:
        # A job may have no attempt recorded, and ``= NULL`` is never true.
        clauses.append(f" AND attempt_id IS NOT DISTINCT FROM ${index}")
        index += 1
    if lease_generation is not None:
        clauses.append(f" AND lease_generation = ${index}")
        index += 1
    return "".join(clauses)


class InvocationsMixin(McpRepositoryBase):
    """Create, decide, claim, finish and expire invocations."""

    _allow_after: tuple[str, int] | None

    @staticmethod
    async def _lock_invocation(conn: Any, invocation_id: int) -> bool:
        return bool(
            await conn.fetchval(
                "SELECT id FROM agent_mcp_invocations WHERE id = $1 FOR UPDATE",
                invocation_id,
            )
        )

    async def create_mcp_invocation(
        self,
        *,
        job_id: str,
        attempt_id: int | None,
        lease_generation: int,
        thread_id: str,
        connection_id: int,
        capability_name: str,
        arguments_digest: str,
        arguments_ciphertext: str | None,
        config_revision: int,
        authorization_epoch: int,
        capability_revision: int,
        policy_revision: int,
        expires_at: Any,
        capability_kind: str = "tool",
        residency_generation: int | None = None,
    ) -> dict[str, Any]:
        """Record a call that is waiting for its owner."""
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                INSERT INTO agent_mcp_invocations
                    (job_id, attempt_id, lease_generation, thread_id, connection_id,
                     capability_name, capability_kind, arguments_digest,
                     arguments_ciphertext, config_revision, authorization_epoch,
                     capability_revision, policy_revision, expires_at, residency_generation)
                VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13, $14, $15)
                RETURNING *
                """,
                job_id,
                attempt_id,
                lease_generation,
                thread_id,
                connection_id,
                capability_name,
                capability_kind,
                arguments_digest,
                arguments_ciphertext,
                config_revision,
                authorization_epoch,
                capability_revision,
                policy_revision,
                expires_at,
                residency_generation,
            )
        return dict(row)

    async def get_mcp_invocation(self, *, invocation_id: int) -> dict[str, Any] | None:
        """One invocation by id."""
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT * FROM agent_mcp_invocations WHERE id = $1", invocation_id
            )
        return dict(row) if row is not None else None

    async def list_mcp_invocations(
        self,
        *,
        job_id: str | None = None,
        thread_id: str | None = None,
        statuses: list[str] | None = None,
        user_id: str | None = None,
        limit: int = 50,
        before_id: int | None = None,
    ) -> list[dict[str, Any]]:
        """Invocations filtered by job, thread, state or owner, newest first.

        ``user_id`` scopes through the job rather than a column on the
        invocation, because an invocation belongs to whoever owns the job that
        made it. Filtering the *list* is not a convenience: the rows carry the
        decrypted arguments, so a listing that does not scope by owner hands one
        user another user's tool call bodies. Callers that serve a request must
        pass it. Owner-scoped reads omit deleted conversations; unscoped
        internal audit reads retain their invocation history.

        ``before_id`` continues strictly below the previous page's last id,
        so new calls cannot shift rows between pages. A pending state filter
        omits calls whose deadline has passed, even before the expiry worker
        persists that transition. Reads never change invocation state.
        """
        clauses: list[str] = []
        params: list[Any] = []
        if user_id is not None:
            params.append(user_id)
            clauses.append(
                "job_id IN (SELECT j.id FROM agent_jobs j "
                "JOIN agent_threads t ON t.id = j.thread_id "
                f"WHERE j.user_id = ${len(params)} AND t.deleted_at IS NULL)"
            )
        if job_id is not None:
            params.append(job_id)
            clauses.append(f"job_id = ${len(params)}")
        if thread_id is not None:
            params.append(thread_id)
            clauses.append(f"thread_id = ${len(params)}")
        if statuses:
            params.append(list(statuses))
            clauses.append(f"status = ANY(${len(params)})")
            if "pending" in statuses:
                clauses.append("(status <> 'pending' OR expires_at > clock_timestamp())")
        if before_id is not None:
            params.append(before_id)
            clauses.append(f"id < ${len(params)}")
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        params.append(max(1, min(limit, 200)))
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(
                f"""
                SELECT * FROM agent_mcp_invocations {where}
                ORDER BY id DESC LIMIT ${len(params)}
                """,
                *params,
            )
        return [dict(row) for row in rows]

    async def decide_mcp_invocation(
        self,
        *,
        invocation_id: int,
        approve: bool,
        decided_by: str,
        always_allow_rule: dict[str, Any] | None = None,
    ) -> dict[str, Any] | None:
        """Approve or deny one pending call.

        Only a pending call may be decided, enforced in the predicate rather
        than by reading first: an approval that arrives after the call expired
        must not revive it, and a check-then-write would let exactly that race
        through.
        """
        if always_allow_rule is not None and not approve:
            raise ValueError("Always allow requires approval")
        target = "approved" if approve else "denied"
        async with self._pool.acquire() as conn, conn.transaction():
            if not await self._lock_invocation(conn, invocation_id):
                return None
            row = await conn.fetchrow(
                """
                UPDATE agent_mcp_invocations
                SET status = $2, decided_at = NOW(), decided_by = $3,
                    always_allow_rule = $4::jsonb,
                    always_allow_status = CASE WHEN $4::jsonb IS NULL THEN NULL ELSE 'pending' END
                WHERE id = $1 AND status = 'pending' AND expires_at > clock_timestamp()
                RETURNING *
                """,
                invocation_id,
                target,
                decided_by,
                json.dumps(always_allow_rule) if always_allow_rule is not None else None,
            )
            if row is None:
                # Distinguish "already decided" from "too late" for the caller,
                # without changing either outcome.
                await conn.execute(
                    f"UPDATE agent_mcp_invocations SET {self._EXPIRED} "
                    "WHERE id = $1 AND status = 'pending' "
                    "AND expires_at <= clock_timestamp()",
                    invocation_id,
                )
        if row is not None and not approve:
            # A denial may release another remembered decision on this binding.
            # Save now, before the agent's next call enters the approval queue.
            await self._flush_remembered_approvals(row)
        return dict(row) if row is not None else None

    async def claim_mcp_invocation(  # noqa: PLR0912 complexity baseline, #771
        self,
        *,
        invocation_id: int,
        config_revision: int | None = None,
        authorization_epoch: int | None = None,
        capability_revision: int | None = None,
        policy_revision: int | None = None,
        attempt_id: int | None = None,
        lease_generation: int | None = None,
        residency_generation: int | None = None,
    ) -> dict[str, Any] | None:
        """Take exclusive ownership of dispatching one approved call.

        The final ``UPDATE ... WHERE status = 'approved'`` is the
        mutual-exclusion point. Two workers may both arrive; one matches the
        row, the other matches nothing and must not send. ``dispatch_generation``
        is bumped so that a worker whose lease was superseded can be refused by
        the upstream side even if it still holds this record.

        The fence is in the same predicate, for the same reason the status is:
        the window between "this approval is still valid" and "I am dispatching
        it" is exactly where a connection gets repointed, and a check performed
        before the ``UPDATE`` is a check that races with it. Requiring the
        versions here means an approval whose target moved is not merely
        discouraged but unmatchable.
        """
        fence = _invocation_fence_sql(
            config_revision=config_revision,
            authorization_epoch=authorization_epoch,
            capability_revision=capability_revision,
            policy_revision=policy_revision,
            attempt_id=attempt_id,
            lease_generation=lease_generation,
            first_index=2,
        )
        # A waiting approval is valid only while the live database still
        # describes the target, policy and attempt the owner approved.
        live_fence = (
            ""
            if attempt_id is None
            else f"""
            AND EXISTS (
                SELECT 1 FROM agent_jobs j
                JOIN agent_attempts a ON a.id = j.current_attempt_id
                JOIN agent_mcp_thread_bindings b ON b.thread_id = j.thread_id
                JOIN agent_mcp_connections c ON c.id = b.connection_id
                WHERE j.id = agent_mcp_invocations.job_id
                  AND j.state = 'running' AND a.status = 'running'
                  AND a.id = agent_mcp_invocations.attempt_id
                  AND a.lease_generation = agent_mcp_invocations.lease_generation
                  AND a.lease_expires_at > clock_timestamp()
                  AND {residency_fence_sql("agent_mcp_invocations.residency_generation")}
                  AND b.connection_id = agent_mcp_invocations.connection_id AND b.enabled
                  AND b.policy_revision = agent_mcp_invocations.policy_revision
                  AND c.user_id = j.user_id AND c.deleted_at IS NULL AND c.enabled
                  AND c.config_revision = agent_mcp_invocations.config_revision
                  AND c.authorization_epoch = agent_mcp_invocations.authorization_epoch
                  AND c.capability_revision = agent_mcp_invocations.capability_revision
            )
        """
        )
        async with self._pool.acquire() as conn, conn.transaction():
            target = await conn.fetchrow(
                """
                SELECT job_id, attempt_id, lease_generation, thread_id, connection_id,
                       residency_generation
                FROM agent_mcp_invocations WHERE id = $1
                """,
                invocation_id,
            )
            if target is None or target["residency_generation"] != residency_generation:
                return None
            if residency_generation is not None and attempt_id is None:
                return None
            if attempt_id is not None:
                if target["attempt_id"] != attempt_id or (
                    lease_generation is not None
                    and int(target["lease_generation"]) != lease_generation
                ):
                    return None
                # Follow the execution lock order and hold every row whose live
                # state authorizes dispatch.  The UPDATE below then observes a
                # fresh wall clock and a stable set of revisions after any wait.
                if not await conn.fetchval(
                    "SELECT id FROM agent_attempts WHERE id = $1 FOR SHARE",
                    target["attempt_id"],
                ):
                    return None
                if not await conn.fetchval(
                    "SELECT id FROM agent_threads WHERE id = $1 FOR SHARE",
                    target["thread_id"],
                ):
                    return None
                if not await conn.fetchval(
                    "SELECT id FROM agent_jobs WHERE id = $1 FOR SHARE",
                    target["job_id"],
                ):
                    return None
                if not await lock_mcp_residency(
                    conn,
                    job_id=target["job_id"],
                    attempt_id=attempt_id,
                    generation=residency_generation,
                ):
                    return None
                if not await conn.fetchval(
                    "SELECT id FROM agent_mcp_connections WHERE id = $1 FOR SHARE",
                    target["connection_id"],
                ):
                    return None
                if not await conn.fetchval(
                    "SELECT connection_id FROM agent_mcp_thread_bindings "
                    "WHERE thread_id = $1 AND connection_id = $2 FOR SHARE",
                    target["thread_id"],
                    target["connection_id"],
                ):
                    return None
            locked_target = await conn.fetchrow(
                """
                SELECT job_id, attempt_id, lease_generation, thread_id, connection_id,
                       residency_generation
                FROM agent_mcp_invocations WHERE id = $1 FOR UPDATE
                """,
                invocation_id,
            )
            if locked_target is None or any(
                locked_target[column] != target[column]
                for column in (
                    "job_id",
                    "attempt_id",
                    "lease_generation",
                    "thread_id",
                    "connection_id",
                    "residency_generation",
                )
            ):
                return None
            row = await conn.fetchrow(
                f"""
                UPDATE agent_mcp_invocations
                SET status = 'dispatching', dispatch_generation = dispatch_generation + 1
                WHERE id = $1 AND status = 'approved' AND expires_at > clock_timestamp(){fence}{live_fence}
                RETURNING *
                """,
                invocation_id,
                *fence_params(
                    config_revision=config_revision,
                    authorization_epoch=authorization_epoch,
                    capability_revision=capability_revision,
                    policy_revision=policy_revision,
                    attempt_id=attempt_id,
                    lease_generation=lease_generation,
                ),
            )
        if row is not None:
            try:
                await self._flush_remembered_approvals(row)
            except asyncio.CancelledError:
                # The caller has not received the committed claim yet, so its
                # normal dispatch cleanup cannot know which record it owns.
                await asyncio.shield(
                    self.finish_mcp_invocation(
                        invocation_id=int(row["id"]),
                        dispatch_generation=int(row["dispatch_generation"]),
                        status="outcome_unknown",
                        result_summary="Dispatcher cancelled after claiming the approval.",
                    )
                )
                raise
        return dict(row) if row is not None else None

    async def _flush_remembered_approvals(self, row: Any) -> None:
        """Attempt a prompt save without losing an already committed dispatch claim."""
        try:
            async with asyncio.timeout(5.0):
                await self.apply_pending_mcp_allow_rules(
                    thread_id=str(row["thread_id"]), connection_id=int(row["connection_id"])
                )
        except Exception:
            logger.warning("agent_mcp_remembered_approval_failed", exc_info=True)

    async def apply_pending_mcp_allow_rules(
        self, *, thread_id: str | None = None, connection_id: int | None = None
    ) -> int:
        """Persist remembered approvals after held calls leave the dispatch queue.

        Durable intents are merged under thread, connection and binding locks.
        A cancelled pass resumes after its last attempted group.
        """
        background = thread_id is None and connection_id is None
        async with self._pool.acquire() as conn:
            groups = await pending_allow_groups(
                conn,
                thread_id=thread_id,
                connection_id=connection_id,
                after=self._allow_after if background else None,
            )
        applied = 0
        for group in groups:
            if background:
                self._allow_after = (str(group["thread_id"]), int(group["connection_id"]))
            applied += await self._apply_mcp_allow_group(
                str(group["thread_id"]), int(group["connection_id"])
            )
        if background and len(groups) < ALLOW_GROUP_PAGE:
            self._allow_after = None
        return applied

    async def _apply_mcp_allow_group(self, thread_id: str, connection_id: int) -> int:
        """Recheck fingerprints, ownership and revisions before saving one binding."""
        async with self._pool.acquire() as conn, conn.transaction():
            thread = await conn.fetchrow(
                "SELECT * FROM agent_threads WHERE id = $1 FOR UPDATE SKIP LOCKED", thread_id
            )
            if thread is None:
                # Permission edits and other workers may own the conversation.
                # Leave its durable intents for a later pass and service others.
                return 0
            connection = await conn.fetchrow(
                "SELECT * FROM agent_mcp_connections WHERE id = $1 FOR SHARE", connection_id
            )
            binding = await conn.fetchrow(
                "SELECT * FROM agent_mcp_thread_bindings "
                "WHERE thread_id = $1 AND connection_id = $2 FOR UPDATE",
                thread_id,
                connection_id,
            )
            snapshot = await conn.fetchrow(
                "SELECT * FROM agent_mcp_capabilities WHERE connection_id = $1 FOR SHARE",
                connection_id,
            )
            intents = await conn.fetch(
                "SELECT * FROM agent_mcp_invocations "
                "WHERE thread_id = $1 AND connection_id = $2 "
                "AND always_allow_status = 'pending' ORDER BY id FOR UPDATE",
                thread_id,
                connection_id,
            )
            valid = []
            for intent in intents:
                live = (
                    thread is not None
                    and thread["deleted_at"] is None
                    and connection is not None
                    and connection["deleted_at"] is None
                    and connection["enabled"]
                    and binding is not None
                    and binding["enabled"]
                    and thread["user_id"] == connection["user_id"] == intent["decided_by"]
                    and all(
                        connection[key] == intent[key]
                        for key in ("config_revision", "authorization_epoch", "capability_revision")
                    )
                    and binding["policy_revision"] == intent["policy_revision"]
                    and _remembered_rule_matches(intent, snapshot)
                )
                if live:
                    valid.append(intent)
                else:
                    await conn.execute(
                        "UPDATE agent_mcp_invocations SET always_allow_status = 'discarded' "
                        "WHERE id = $1",
                        intent["id"],
                    )
            if not valid or await conn.fetchval(
                "SELECT EXISTS (SELECT 1 FROM agent_mcp_invocations "
                "WHERE thread_id = $1 AND connection_id = $2 "
                "AND status IN ('pending', 'approved') AND expires_at > clock_timestamp())",
                thread_id,
                connection_id,
            ):
                return 0
            assert binding is not None
            merged = mcp_binding_row(binding)
            keys = {
                "tool": "rules",
                "prompt": "prompt_rules",
                "resource": "resource_rules",
                "resource_template": "resource_rules",
            }
            for intent in valid:
                merged[keys[intent["capability_kind"]]][intent["capability_name"]] = _load_json(
                    intent["always_allow_rule"]
                )
            await conn.execute(
                """
                UPDATE agent_mcp_thread_bindings
                SET rules = $3::jsonb, resource_rules = $4::jsonb, prompt_rules = $5::jsonb,
                    policy_revision = nextval('agent_mcp_policy_revision_seq'), updated_at = NOW()
                WHERE thread_id = $1 AND connection_id = $2
                """,
                thread_id,
                connection_id,
                json.dumps(merged["rules"]),
                json.dumps(merged["resource_rules"]),
                json.dumps(merged["prompt_rules"]),
            )
            await conn.execute(
                "UPDATE agent_mcp_invocations SET always_allow_status = 'applied' "
                "WHERE id = ANY($1::bigint[])",
                [intent["id"] for intent in valid],
            )
            return len(valid)

    async def finish_mcp_invocation(
        self,
        *,
        invocation_id: int,
        dispatch_generation: int,
        status: str,
        result_summary: str | None = None,
    ) -> bool:
        """Record the outcome of a dispatched call, fenced by generation."""
        if status not in ("completed", "failed", "outcome_unknown"):
            raise ValueError(f"invalid terminal invocation status: {status!r}")
        async with self._pool.acquire() as conn:
            updated = await conn.fetchval(
                """
                UPDATE agent_mcp_invocations
                SET status = $3, result_summary = $4
                WHERE id = $1 AND status = 'dispatching' AND dispatch_generation = $2
                RETURNING id
                """,
                invocation_id,
                dispatch_generation,
                status,
                result_summary,
            )
        return updated is not None

    async def find_approved_mcp_invocation(
        self,
        *,
        job_id: str,
        connection_id: int,
        capability_name: str,
        arguments_digest: str,
        capability_kind: str = "tool",
        config_revision: int | None = None,
        authorization_epoch: int | None = None,
        capability_revision: int | None = None,
        policy_revision: int | None = None,
        attempt_id: int | None = None,
        lease_generation: int | None = None,
        residency_generation: int | None = None,
    ) -> dict[str, Any] | None:
        """The oldest approval of one call that is still valid for it.

        Matching on the argument digest as well as the name is what makes an
        approval an approval *of something*: a session-wide "allow" is a rule,
        while this is one decision about one call, and reusing it for different
        arguments would turn a single confirmation into a standing grant.

        The fence is part of the lookup, not a check after it. An approval was
        given for a target, a catalogue and a session *as they were*, and the
        owner agreed to that call and nothing else -- so once the connection is
        repointed, the catalogue grows or the job changes hands, the approval
        describes something that no longer exists and must not be found at all.
        """
        fence = _invocation_fence_sql(
            config_revision=config_revision,
            authorization_epoch=authorization_epoch,
            capability_revision=capability_revision,
            policy_revision=policy_revision,
            attempt_id=attempt_id,
            lease_generation=lease_generation,
            first_index=7,
        )
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow(
                f"""
                SELECT * FROM agent_mcp_invocations
                WHERE job_id = $1 AND connection_id = $2 AND capability_name = $3
                  AND arguments_digest = $4 AND capability_kind = $5
                  AND residency_generation IS NOT DISTINCT FROM $6::bigint
                  AND status = 'approved' AND expires_at > clock_timestamp(){fence}
                ORDER BY id
                LIMIT 1
                """,
                job_id,
                connection_id,
                capability_name,
                arguments_digest,
                capability_kind,
                residency_generation,
                *fence_params(
                    config_revision=config_revision,
                    authorization_epoch=authorization_epoch,
                    capability_revision=capability_revision,
                    policy_revision=policy_revision,
                    attempt_id=attempt_id,
                    lease_generation=lease_generation,
                ),
            )
        return dict(row) if row is not None else None

    async def expire_mcp_invocations(self) -> int:
        """Expire every pending or approved call whose deadline has passed."""
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(
                f"""
                WITH expired AS MATERIALIZED (
                    SELECT id FROM agent_mcp_invocations
                    WHERE status IN ('pending', 'approved')
                      AND expires_at <= clock_timestamp()
                    FOR UPDATE SKIP LOCKED
                )
                UPDATE agent_mcp_invocations AS calls SET {self._EXPIRED}
                FROM expired WHERE calls.id = expired.id RETURNING calls.id
                """
            )
        return len(rows)

    async def expire_mcp_invocation(self, *, invocation_id: int) -> bool:
        """Expire one undispatched call when its waiting client or authorization is gone."""
        async with self._pool.acquire() as conn:
            updated = await conn.fetchval(
                f"UPDATE agent_mcp_invocations SET {self._EXPIRED} "
                "WHERE id = $1 AND status IN ('pending', 'approved') RETURNING id",
                invocation_id,
            )
        return updated is not None
