"""Enrollment tokens, host registration and host authentication."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

from cloud_agent.domain.host_pool import HOST_DISABLED, HOST_ENROLLING, normalize_capabilities
from cloud_agent.ports.errors import ConflictCode, StorageConflict
from cloud_agent.storage._sql.compute_admission import lock_compute_entry
from cloud_agent.storage._sql.hosts import HOST_COLUMNS
from cloud_agent.storage.repositories.host_pool._common import HostPoolBase

if TYPE_CHECKING:
    from typing import Any


class EnrollmentMixin(HostPoolBase):
    """Enroll, register and authenticate hosts."""

    async def issue_enroll_token(
        self, *, token_hash: str, host: str, ttl_seconds: float, issued_by: str | None = None
    ) -> dict[str, Any]:
        """Record a one-time enrollment token by hash and return its expiry.

        Only the hash is stored. An operator who loses the token issues another
        one; nobody — including anyone who can read this table — can recover
        the value that was handed out.

        ``host`` is the name this token may register, and it is stored in
        clear: it is not a secret, it is the authenticated decision the token
        carries. See :meth:`consume_enroll_token`.
        """
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                INSERT INTO agent_host_enroll_tokens (token_hash, host, issued_by, expires_at)
                VALUES ($1, $2, $3, clock_timestamp() + make_interval(secs => $4))
                RETURNING expires_at, created_at
                """,
                token_hash,
                host,
                issued_by,
                ttl_seconds,
            )
        return {"expires_at": row["expires_at"], "created_at": row["created_at"]}

    async def consume_enroll_token(self, *, token_hash: str, host: str) -> bool:
        """Spend an enrollment token, exactly once and only under its own name.

        Single-use is the UPDATE's own ``consumed_at IS NULL`` predicate rather
        than a read followed by a write: two machines booting from the same
        pasted token race here, and only one row update can win.

        **The name is part of the predicate, not a check after the fact.**
        ``register_host`` upserts, so registering under a live host's name
        revokes that host's credential and resets its row to ``enrolling`` —
        one typo during a setup takes a working machine out of the pool. The
        name therefore comes from the token an admin issued rather than from
        the machine presenting it. Because it is a predicate, a mismatch also
        does not *spend* the token: the typo is retried, not re-issued.

        A row whose ``host`` is NULL — issued before the binding existed —
        matches nothing, because ``NULL = $2`` is never true. That is the
        fail-closed direction: such a token was redeemable under any name.
        """
        async with self._pool.acquire() as conn, conn.transaction():
            locked = await conn.fetchval(
                "SELECT token_hash FROM agent_host_enroll_tokens "
                "WHERE token_hash = $1 AND consumed_at IS NULL AND host = $2 FOR UPDATE",
                token_hash,
                host,
            )
            if locked is None:
                return False
            consumed = await conn.fetchval(
                """
                UPDATE agent_host_enroll_tokens
                SET consumed_at = NOW(), consumed_host = $2
                WHERE token_hash = $1 AND consumed_at IS NULL AND expires_at > clock_timestamp()
                  AND host = $2
                RETURNING token_hash
                """,
                token_hash,
                host,
            )
        return consumed is not None

    async def get_host(self, *, host: str) -> dict[str, Any] | None:
        """Return one host row, without its credential hash."""
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow(
                f"SELECT {HOST_COLUMNS} FROM agent_runner_hosts WHERE host = $1", host
            )
        return None if row is None else self._host_row_to_dict(row)

    async def register_host(
        self,
        *,
        host: str,
        credential_hash: str,
        enrollment_token_hash: str | None = None,
        capabilities: list[str] | None = None,
        slots_total: int | None = None,
        sandbox_manager_url: str | None = None,
        runner_version: str | None = None,
        labels: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Enrol a machine, or re-enrol one that was rebuilt.

        Starts at ``enrolling``, not ``online``: registration proves somebody
        held a valid enrollment token, not that the machine is up. The first
        heartbeat is what says that, and it is one round trip away.

        Re-enrollment resets ``slots_used`` to the number of attempts actually
        running on this host rather than to zero. A machine that was rebuilt
        mid-job has a stale counter, and zero would over-subscribe it while the
        old attempts are still leased; counting is the only answer that is
        right in both the rebuilt and the merely-re-registered case.

        The upsert is what makes a rebuild possible, and it is also what makes
        a mistyped name destructive — it revokes whatever credential the name
        currently holds. It stays an upsert; what changed is who chooses the
        name. :meth:`consume_enroll_token` will only spend a token under the
        name an admin bound to it, so reaching this method under a live host's
        name is an operator's decision rather than a machine's.

        Re-enrollment also **rotates the capacity epoch and clears the stored
        observation**. A rebuilt machine keeps its name, so nothing else
        distinguishes the new box from the one it replaced; without the
        rotation the admin chart would join the two machines' histories into
        one line that no machine ever drew.
        """
        async with self._pool.acquire() as conn, conn.transaction():
            await lock_compute_entry(conn)
            if enrollment_token_hash is not None and not await conn.fetchval(
                "SELECT 1 FROM agent_host_enroll_tokens "
                "WHERE token_hash=$1 AND host=$2 AND consumed_host=$2 AND consumed_at IS NOT NULL",
                enrollment_token_hash,
                host,
            ):
                # Removal may have invalidated this token after consumption but
                # before registration acquired the topology fence.
                raise StorageConflict(ConflictCode.ENROLLMENT_TOKEN_REMOVED)
            existing = await conn.fetchrow(
                "SELECT drain_fence_active FROM agent_runner_hosts WHERE host = $1 FOR UPDATE",
                host,
            )
            if existing is not None and existing["drain_fence_active"]:
                raise StorageConflict(ConflictCode.HOST_DRAIN_FENCED)
            retiring = await conn.fetchval(
                """SELECT 1 FROM agent_runner_hosts h
                JOIN agent_compute_nodes n ON n.id=h.compute_node_id
                JOIN agent_compute_allocations a ON a.id=n.allocation_id
                WHERE h.host=$1 AND (a.desired_state='absent' OR a.release_committed_at IS NOT NULL)
            """,
                host,
            )
            if retiring:
                raise StorageConflict(ConflictCode.RESOURCE_RETIRING)
            row = await conn.fetchrow(
                f"""
                INSERT INTO agent_runner_hosts
                    (host, status, capabilities, slots_total, broker_url, runner_version,
                     labels, credential_hash)
                VALUES ($1, '{HOST_ENROLLING}', $2::jsonb, $3, $4, $5, $6::jsonb, $7)
                ON CONFLICT (host) DO UPDATE
                SET status = '{HOST_ENROLLING}',
                    drain_fence_token = NULL,
                    drain_fence_status_before = NULL,
                    drain_fence_active = FALSE,
                    removal_requested_at = NULL,
                    capabilities = EXCLUDED.capabilities,
                    slots_total = EXCLUDED.slots_total,
                    broker_url = EXCLUDED.broker_url,
                    runner_version = EXCLUDED.runner_version,
                    labels = EXCLUDED.labels,
                    credential_hash = EXCLUDED.credential_hash,
                    last_heartbeat_at = NULL,
                    capacity_snapshot = NULL,
                    capacity_received_at = NULL,
                    capacity_sampled_at = NULL,
                    capacity_sample_id = NULL,
                    capacity_epoch = gen_random_uuid(),
                    -- What a host reports about its runtime settings belongs to
                    -- the runner that reported it, which re-enrollment may
                    -- have replaced; its next heartbeat says again. The values
                    -- an admin set for this name stay, as slots_desired does.
                    settings_applied = NULL,
                    settings_local = NULL,
                    sandbox_archive_settings_applied_revision = NULL,
                    slots_used = (
                        SELECT count(*) FROM agent_attempts
                        WHERE host_id = EXCLUDED.host AND status = 'running'
                    )
                RETURNING {HOST_COLUMNS}
                """,
                host,
                json.dumps(normalize_capabilities(capabilities)),
                slots_total,
                sandbox_manager_url,
                runner_version,
                json.dumps(labels or {}),
                credential_hash,
            )
        return self._host_row_to_dict(row)

    async def authenticate_host(self, *, host: str, credential_hash: str) -> dict[str, Any] | None:
        """Return the host iff this credential is the one it currently holds.

        Fails closed on every other reading: an unknown host, a revoked
        credential (``credential_hash`` nulled), a superseded one, or a
        disabled row all answer ``None``. Compared in SQL so a revocation that
        commits mid-request is seen by the next call rather than by the next
        restart.
        """
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow(
                f"""
                SELECT {HOST_COLUMNS} FROM agent_runner_hosts
                WHERE host = $1 AND credential_hash IS NOT NULL AND credential_hash = $2
                  AND status <> '{HOST_DISABLED}'
                """,
                host,
                credential_hash,
            )
        return None if row is None else self._host_row_to_dict(row)
