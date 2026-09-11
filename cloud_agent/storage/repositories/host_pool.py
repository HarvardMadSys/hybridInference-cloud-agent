"""Postgres implementation of the runner-host pool port.

Lifted verbatim from the single storage class this package replaced. The
statements, their bound parameters and their transaction ownership are what
they were; only the class holding them changed.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

from cloud_agent.host_pool import (
    HOST_DISABLED,
    HOST_ENROLLING,
    HOST_OFFLINE,
    HOST_ONLINE,
    normalize_capabilities,
    transition_allowed,
)
from cloud_agent.logging_utils import get_logger
from cloud_agent.storage._sql.hosts import (
    HOST_CAPACITY_COLUMNS,
    HOST_COLUMNS,
    QUALIFIED_HOST_COLUMNS,
)
from cloud_agent.storage._sql.rows import load_json
from cloud_agent.storage._sql.sandbox import THREAD_SANDBOX_SOURCE

if TYPE_CHECKING:
    from collections.abc import Mapping
    from datetime import datetime
    from typing import Any

    import asyncpg


logger = get_logger(__name__)


class PostgresHostPoolRepository:
    """The runner-host pool: enrollment, heartbeats, and placement."""

    def __init__(self, pool: asyncpg.Pool) -> None:
        """Wrap the pool the control plane already owns."""
        self._pool = pool

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
                VALUES ($1, $2, $3, NOW() + make_interval(secs => $4))
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
        async with self._pool.acquire() as conn:
            consumed = await conn.fetchval(
                """
                UPDATE agent_host_enroll_tokens
                SET consumed_at = NOW(), consumed_host = $2
                WHERE token_hash = $1 AND consumed_at IS NULL AND expires_at > NOW()
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
        capabilities: list[str] | None = None,
        slots_total: int | None = None,
        broker_url: str | None = None,
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
            row = await conn.fetchrow(
                f"""
                INSERT INTO agent_runner_hosts
                    (host, status, capabilities, slots_total, broker_url, runner_version,
                     labels, credential_hash)
                VALUES ($1, '{HOST_ENROLLING}', $2::jsonb, $3, $4, $5, $6::jsonb, $7)
                ON CONFLICT (host) DO UPDATE
                SET status = '{HOST_ENROLLING}',
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
                    slots_used = (
                        SELECT count(*) FROM agent_attempts
                        WHERE host_id = EXCLUDED.host AND status = 'running'
                    )
                RETURNING {HOST_COLUMNS}
                """,
                host,
                json.dumps(normalize_capabilities(capabilities)),
                slots_total,
                broker_url,
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

    async def record_host_heartbeat(
        self,
        *,
        host: str,
        credential_hash: str | None = None,
        capacity: Mapping[str, Any] | None = None,
        capacity_sample_age_ms: int | None = None,
        physical_host: str | None = None,
        slots_total: int | None = None,
        runner_version: str | None = None,
        broker_url: str | None = None,
        capabilities: list[str] | None = None,
        sandbox_runtime: str | None = None,
        sandbox_image_digest: str | None = None,
        runtime_versions: dict[str, str] | None = None,
        runtime_versions_available: dict[str, list[str]] | None = None,
    ) -> dict[str, Any] | None:
        """Record a heartbeat, promoting the host to ``online`` when it may.

        ``enrolling → online`` and ``offline → online`` both happen here: a
        machine that comes back after a network partition needs no operator to
        put it back to work. ``draining`` and ``disabled`` deliberately do
        **not** — a heartbeat is the host saying "I am here", not "I should be
        taking work", and letting it re-promote itself would make drain a
        suggestion.

        Returns ``None`` for an unknown host. Every field is optional and only
        overwrites when supplied, so a heartbeat that carries nothing but
        liveness does not blank out what enrollment declared.

        **The capacity block is written by a second statement, deliberately.**
        Its acceptance conditions are not the heartbeat's: the credential must
        still be the one this request authenticated with, the sample must not
        be one already stored, and an older observation must not overwrite a
        younger one. They are enforced in SQL rather than here because the
        sequence they guard against is a race — credential A authenticates, an
        operator re-enrolls the name under credential B, and A's delayed
        request arrives afterwards. Checking "is this still the right
        credential" outside the write would let exactly that request through.
        That second statement runs in its own savepoint, so a block the
        database refuses costs the host nothing but the block: the liveness
        half of the same call still commits.

        Args:
            host: The authenticated host name.
            credential_hash: Digest the request authenticated with. The
                capacity write is refused unless the row still holds it.
            capacity: The validated, normalized capacity block to store.
            capacity_sample_age_ms: How old the observation was when sent.
            physical_host: Admin grouping label; never an identity.
            slots_total: Claim ceiling this host now reports.
            runner_version: Runner build the host is running.
            broker_url: Address the control plane reaches this host's broker.
            capabilities: What this host can run.
            sandbox_runtime: Attested container runtime.
            sandbox_image_digest: Digest of the sandbox image it launches.
            runtime_versions: Default CLI version per runtime.
            runtime_versions_available: Every CLI version its image holds.

        Returns:
            The updated host row, or ``None`` when the host no longer exists.
        """
        async with self._pool.acquire() as conn, conn.transaction():
            row = await conn.fetchrow(
                f"""
                UPDATE agent_runner_hosts
                SET last_heartbeat_at = NOW(),
                    last_seen_at = NOW(),
                    status = CASE
                        WHEN status IN ('{HOST_ENROLLING}', '{HOST_OFFLINE}') THEN '{HOST_ONLINE}'
                        ELSE status
                    END,
                    slots_total = COALESCE($2, slots_total),
                    runner_version = COALESCE($3, runner_version),
                    broker_url = COALESCE($4, broker_url),
                    capabilities = COALESCE($5::jsonb, capabilities),
                    sandbox_runtime = COALESCE($6, sandbox_runtime),
                    sandbox_image_digest = COALESCE($7, sandbox_image_digest),
                    runtime_versions = COALESCE($8::jsonb, runtime_versions),
                    runtime_versions_available =
                        COALESCE($9::jsonb, runtime_versions_available),
                    labels = COALESCE(labels, '{{}}'::jsonb) || $10::jsonb
                WHERE host = $1
                RETURNING {HOST_COLUMNS}
                """,
                host,
                slots_total,
                runner_version,
                broker_url,
                None if capabilities is None else json.dumps(normalize_capabilities(capabilities)),
                # "" survives COALESCE deliberately: an empty runtime is the
                # host attesting shared-kernel runc, not declining to answer.
                sandbox_runtime,
                sandbox_image_digest,
                None if runtime_versions is None else json.dumps(runtime_versions),
                (
                    None
                    if runtime_versions_available is None
                    else json.dumps(runtime_versions_available)
                ),
                json.dumps({"physical_host": physical_host} if physical_host is not None else {}),
            )
            if row is None:
                return None
            if (
                capacity is not None
                and capacity_sample_age_ms is not None
                and credential_hash is not None
            ):
                await self._record_host_capacity_isolated(
                    conn,
                    host=host,
                    credential_hash=credential_hash,
                    capacity=capacity,
                    sample_age_ms=capacity_sample_age_ms,
                )
        return self._host_row_to_dict(row)

    async def set_host_status(self, *, host: str, status: str) -> str:
        """Move a host to ``status``, refusing transitions the table forbids.

        Returns ``"ok"``, ``"unknown"`` or ``"invalid"``. The table is consulted
        inside the transaction that writes, on a locked row: checking it in the
        caller means an admin draining a host and the reaper marking it offline
        can both read ``online`` and both write, and the loser's decision is the
        one that sticks.
        """
        async with self._pool.acquire() as conn, conn.transaction():
            current = await conn.fetchval(
                "SELECT status FROM agent_runner_hosts WHERE host = $1 FOR UPDATE", host
            )
            if current is None:
                return "unknown"
            if not transition_allowed(current, status):
                return "invalid"
            await conn.execute(
                "UPDATE agent_runner_hosts SET status = $2 WHERE host = $1", host, status
            )
        return "ok"

    async def remove_host(self, *, host: str, force: bool = False) -> str:
        """Revoke a host's credential and take it out of the pool.

        Returns ``"ok"``, ``"unknown"`` or ``"busy"``. Busy means the host is
        still holding slots: removing it then would leave running attempts
        whose machine has officially stopped existing, and nothing would notice
        until their leases lapsed. ``force`` accepts exactly that — the lease
        expiry *is* the recovery path. A retained activation cannot be replayed
        safely after its host disappears, so its expiry marks that chat LOST
        and fails already-queued descendants instead.

        Disabling rather than deleting. The row is what
        ``agent_attempts.host_id`` points at, and what an operator reads when
        asking where a job ran; #1158's ``forget`` is still there for the
        machine that is genuinely gone. Removing or forgetting the host row is
        administrative bookkeeping; it is not by itself proof that a retained
        Docker writable layer was deleted.
        """
        async with self._pool.acquire() as conn, conn.transaction():
            row = await conn.fetchrow(
                "SELECT status, slots_used FROM agent_runner_hosts WHERE host = $1 FOR UPDATE",
                host,
            )
            if row is None:
                return "unknown"
            if row["slots_used"] > 0 and not force:
                return "busy"
            await conn.execute(
                f"""
                UPDATE agent_runner_hosts
                SET status = '{HOST_DISABLED}', credential_hash = NULL
                WHERE host = $1
                """,
                host,
            )
        return "ok"

    async def reap_offline_hosts(
        self,
        *,
        interval_seconds: float,
        missed_intervals: int,
    ) -> list[str]:
        """Mark enrolled hosts that stopped heartbeating as ``offline``.

        Only *enrolled* hosts — ones holding a credential — are candidates.
        A host that joined by polling never promised to heartbeat, and marking
        it offline would stop the claims of a machine that is working
        perfectly, which is the whole pre-G deployment.

        ``COALESCE(last_heartbeat_at, first_seen_at)`` so a machine that
        registered and then never came up ages out of ``enrolling`` too,
        instead of sitting there looking like it is mid-setup forever.

        **``draining`` is not a candidate, and that is the point.** It was, and
        the result was that an operator's drain undid itself: this pass moved
        ``draining → offline``, and the next heartbeat moved ``offline →
        online``, because a machine returning from a partition is supposed to
        come back without an operator. A drained host that goes quiet and
        returns would silently rejoin the claim pool — quietly taking work the
        operator had just decided it must not take. Drain is an instruction,
        not an observation, so nothing here overwrites it; ``remove`` is what
        ends a drained host's life.

        Returns the hosts it changed.
        """
        deadline = interval_seconds * missed_intervals
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(
                f"""
                UPDATE agent_runner_hosts
                SET status = '{HOST_OFFLINE}'
                WHERE credential_hash IS NOT NULL
                  AND status IN ('{HOST_ENROLLING}', '{HOST_ONLINE}')
                  AND COALESCE(last_heartbeat_at, first_seen_at)
                      < NOW() - make_interval(secs => $1)
                RETURNING host
                """,
                deadline,
            )
        return [row["host"] for row in rows]

    async def host_for_attempt(self, *, attempt_id: int) -> dict[str, Any] | None:
        """The host row an attempt ran on, or ``None`` when it had none.

        ``None`` covers both "this attempt predates host reporting" and "the
        host it ran on was forgotten" — the FK is ``ON DELETE SET NULL``, so
        both present as the same absence, and both mean the caller falls back
        to the single-host configuration.
        """
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow(
                f"""
                SELECT {QUALIFIED_HOST_COLUMNS} FROM agent_runner_hosts h
                JOIN agent_attempts a ON a.host_id = h.host
                WHERE a.id = $1
                """,
                attempt_id,
            )
        return None if row is None else self._host_row_to_dict(row)

    async def host_for_job(self, *, job_id: str) -> dict[str, Any] | None:
        """The job attempt's host, or its retained thread's durable host."""
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow(
                f"""
                SELECT COALESCE(a.host_id, s.host) AS owner_host,
                       {QUALIFIED_HOST_COLUMNS}
                FROM agent_jobs j
                LEFT JOIN agent_attempts a ON a.id = j.current_attempt_id
                LEFT JOIN {THREAD_SANDBOX_SOURCE} ON s.thread_id = j.thread_id
                LEFT JOIN agent_runner_hosts h
                  ON h.host = COALESCE(a.host_id, s.host)
                WHERE j.id = $1
                """,
                job_id,
            )
        if row is None or row["owner_host"] is None:
            return None
        host = self._host_row_to_dict(row)
        # A retained sandbox remains hard-pinned even if its host row was
        # administratively forgotten. Returning a placeholder (with no broker
        # URL) makes broker routing fail closed instead of falling back to a
        # different machine's single-host environment setting.
        host["host"] = row["owner_host"]
        return host

    async def active_runner_host(self) -> str | None:
        """The new-chat placement host, or ``None`` when any may materialize."""
        async with self._pool.acquire() as conn:
            return await conn.fetchval("SELECT active_host FROM agent_runner_policy WHERE id")

    async def runner_pool(
        self,
    ) -> tuple[list[dict[str, Any]], str | None, dict[str, str], datetime]:
        """The pool, the pinned host and the version selection, read together.

        One transaction because all three are shown side by side: read
        separately, a switch landing between them renders a page whose list and
        whose header name different hosts, which reads as a bug in the thing
        the operator is using to fix a bug. The version selection joined that
        for the same reason — a page offering a menu that no longer contains
        the selection it displays is the same failure wearing a different hat.

        **Repeatable read, and read-only.** The pool is the page an operator
        reads while changing things, so a claim, a pin and a drain landing
        between the policy read and the host read used to be able to produce a
        list that never existed: slots already spent shown against a pin not
        yet switched. One snapshot for all of it removes that class of answer,
        and ``observed_at`` is the database's own instant for the same
        snapshot, so the ages the page renders are measured from the moment the
        data was taken rather than from whenever the client's clock says.

        Returns:
            ``(hosts, active_host, selected_versions, observed_at)``.
        """
        async with (
            self._pool.acquire() as conn,
            conn.transaction(isolation="repeatable_read", readonly=True),
        ):
            observed_at = await conn.fetchval("SELECT NOW()")
            policy = await conn.fetchrow(
                "SELECT active_host, runtime_versions FROM agent_runner_policy WHERE id"
            )
            rows = await conn.fetch(
                f"""
                SELECT {HOST_COLUMNS}, {HOST_CAPACITY_COLUMNS}
                FROM agent_runner_hosts
                ORDER BY last_seen_at DESC
                """
            )
        active = None if policy is None else policy["active_host"]
        selected = {} if policy is None else load_json(policy["runtime_versions"]) or {}
        return (
            [{**self._host_row_to_dict(row), "is_active": row["host"] == active} for row in rows],
            active,
            selected,
            observed_at,
        )

    async def deployment_runner_pool(
        self,
    ) -> tuple[list[dict[str, Any]], str | None, dict[str, str], set[str]]:
        """Read placement policy, hosts, and retained owners for rollout fencing.

        The admin pin governs only chats with no sandbox. A control-plane
        rollout must also know which other hosts still own shared-live Docker
        layers so it can reverse-probe their brokers. Read the policy under a
        shared lock and the owners in the same transaction: a concurrent host
        switch cannot make the snapshot describe two different placement
        policies, and any newly claimed group while this lock is held belongs
        to one of the placement hosts this snapshot already includes.
        """
        async with self._pool.acquire() as conn, conn.transaction():
            policy = await conn.fetchrow(
                "SELECT active_host, runtime_versions FROM agent_runner_policy WHERE id FOR SHARE"
            )
            rows = await conn.fetch(
                f"""
                SELECT {HOST_COLUMNS}
                FROM agent_runner_hosts
                ORDER BY last_seen_at DESC
                """
            )
            owner_rows = await conn.fetch(
                """
                SELECT DISTINCT host
                FROM agent_sandboxes
                WHERE state <> 'lost'
                """
            )
        active = None if policy is None else policy["active_host"]
        selected = {} if policy is None else load_json(policy["runtime_versions"]) or {}
        return (
            [{**self._host_row_to_dict(row), "is_active": row["host"] == active} for row in rows],
            active,
            selected,
            {str(row["host"]) for row in owner_rows},
        )

    async def list_runner_hosts(self) -> list[dict[str, Any]]:
        """Return every host seen polling, most recent poll first."""
        hosts, _, _, _ = await self.runner_pool()
        return hosts

    async def set_runtime_version(self, *, runtime: str, version: str | None) -> bool:
        """Choose the CLI version this deployment runs for one runtime.

        ``version=None`` clears the choice, which returns that runtime to
        whatever its claiming host reports as the image default.

        Returns ``False`` when no host in the pool offers ``version``. Storing a
        selection nothing can serve is not harmless: the claim would fall back
        silently, so the admin page would show a version that never runs — the
        pin's "switching to a typo" failure, one layer along.

        Same lock order as ``set_active_runner_host`` — policy row first, then
        the host rows it is checked against — so the two admin writes and the
        claim's shared read serialize with no cycle to deadlock on.
        """
        async with self._pool.acquire() as conn, conn.transaction():
            current = await conn.fetchval(
                "SELECT runtime_versions FROM agent_runner_policy WHERE id FOR UPDATE"
            )
            if version is not None:
                offered = await conn.fetch(
                    "SELECT runtime_versions_available FROM agent_runner_hosts "
                    "WHERE runtime_versions_available IS NOT NULL FOR SHARE"
                )
                menus = (load_json(row[0]) or {} for row in offered)
                if not any(version in (menu.get(runtime) or []) for menu in menus):
                    return False
            selection = dict(load_json(current) or {})
            if version is None:
                selection.pop(runtime, None)
            else:
                selection[runtime] = version
            # Upsert for the same reason the host pin is one: against a database
            # whose singleton row is missing, a bare UPDATE touches nothing and
            # still reports success.
            await conn.execute(
                """
                INSERT INTO agent_runner_policy (id, runtime_versions) VALUES (TRUE, $1::jsonb)
                ON CONFLICT (id) DO UPDATE
                SET runtime_versions = EXCLUDED.runtime_versions, updated_at = NOW()
                """,
                json.dumps(selection),
            )
            return True

    async def set_active_runner_host(self, *, host: str | None) -> bool:
        """Pin new-chat placement to one host, or unpin with ``None``.

        Returns ``False`` when ``host`` names a machine that has never polled —
        pinning to one would park every chat without a sandbox on a host that
        may not exist. Existing groups remain on their owning hosts.

        Takes the policy row exclusively *first*, then the host row it is about
        to point at. Every writer here uses that order, and the claim takes the
        policy row shared, so the paths serialize with no cycle to deadlock on:
        a claim past its own read finishes before this commits, one that has
        not reached it waits and then sees this value, and a concurrent removal
        of the host being pinned cannot slip in between the check and the write.
        """
        async with self._pool.acquire() as conn, conn.transaction():
            await conn.execute("SELECT active_host FROM agent_runner_policy WHERE id FOR UPDATE")
            # Checked before anything is written — and locked, not merely read:
            # a bare existence check lets a concurrent removal commit before
            # this does, leaving the queue pinned to a host that is gone.
            if host is not None:
                known = await conn.fetchval(
                    "SELECT 1 FROM agent_runner_hosts WHERE host = $1 FOR SHARE", host
                )
                if not known:
                    return False
            # Upsert, not a bare UPDATE: against a database whose policy row is
            # missing, an UPDATE touches nothing and still reports success —
            # the switch would read as applied while the gate kept using the
            # old value.
            await conn.execute(
                """
                INSERT INTO agent_runner_policy (id, active_host) VALUES (TRUE, $1)
                ON CONFLICT (id) DO UPDATE
                SET active_host = EXCLUDED.active_host, updated_at = NOW()
                """,
                host,
            )
            return True

    async def forget_runner_host(self, *, host: str) -> str:
        """Drop a host from the pool. A runner still polling re-adds itself.

        Returns ``"deleted"``, ``"active"`` (refused) or ``"unknown"``.

        The active-host refusal lives here rather than in the caller because
        checking there is a read the switch can outrun: the endpoint sees the
        host idle, an admin pins it, and the delete then removes the row the
        new-chat placement gate is pointing at — silently unpinning that
        policy. Same policy row, same order as the switch, so the two serialize.
        """
        async with self._pool.acquire() as conn, conn.transaction():
            active = await conn.fetchval(
                "SELECT active_host FROM agent_runner_policy WHERE id FOR UPDATE"
            )
            if active == host:
                return "active"
            deleted = await conn.fetchval(
                "DELETE FROM agent_runner_hosts WHERE host = $1 RETURNING host", host
            )
        return "deleted" if deleted is not None else "unknown"

    @staticmethod
    def _host_row_to_dict(row: Any) -> dict[str, Any]:
        """Decode a host row's JSON columns; ``credential_hash`` never leaves."""
        host = dict(row)
        host["labels"] = load_json(host.get("labels")) or {}
        host["capabilities"] = load_json(host.get("capabilities")) or []
        # None (never reported) and {} (reported: no labels) stay distinct.
        host["runtime_versions"] = load_json(host.get("runtime_versions"))
        host["runtime_versions_available"] = load_json(host.get("runtime_versions_available"))
        # Absent from most projections, so read with ``.get``: a row that never
        # selected the capacity columns must decode to "no observation", not
        # raise a KeyError on a path that has nothing to do with capacity.
        if "capacity_snapshot" in host:
            host["capacity_snapshot"] = load_json(host["capacity_snapshot"])
        return host

    @staticmethod
    async def _record_host_capacity_isolated(
        conn: Any,
        *,
        host: str,
        credential_hash: str,
        capacity: Mapping[str, Any],
        sample_age_ms: int,
    ) -> None:
        """Store a capacity observation without ever risking the heartbeat.

        The two halves of this request share a transaction, and a transaction
        that has raised is finished: catching the exception in Python would not
        un-abort it, so the heartbeat's own write would be rolled back with the
        monitoring field that failed. A nested transaction is a SAVEPOINT,
        which is the only thing that lets the failure be contained here.

        That matters because the failure modes are the database's, not this
        code's: a namespace carrying U+0000 passes any string validation and is
        then refused by JSONB, and a constraint added tomorrow could refuse
        something else. A host that is alive must be recorded as alive whatever
        the control plane thinks of its monitoring data.
        """
        try:
            async with conn.transaction():
                await PostgresHostPoolRepository._record_host_capacity(
                    conn,
                    host=host,
                    credential_hash=credential_hash,
                    capacity=capacity,
                    sample_age_ms=sample_age_ms,
                )
        except Exception:
            logger.warning(
                "agent_host_capacity_write_failed",
                exc_info=True,
                extra={"event": "agent_host_capacity_write_failed", "host": host},
            )

    @staticmethod
    async def _record_host_capacity(
        conn: Any,
        *,
        host: str,
        credential_hash: str,
        capacity: Mapping[str, Any],
        sample_age_ms: int,
    ) -> None:
        """Store a capacity observation, iff it is new, newer, and still ours.

        Three conditions, all in the predicate rather than in a prior read:

        * ``credential_hash`` still matches — a re-enrollment between
          authentication and this write has cleared the row, and the delayed
          request must not repopulate it with the previous machine's numbers.
        * ``sample_id`` differs — the same observation arriving again (a
          retried heartbeat, a second replica replaying a cached block) does
          not refresh its timestamps and so cannot be made to look fresh.
        * ``sampled_at`` is newer than what is stored — replicas report
          independently and out of order, and the older report must not
          overwrite the younger one merely because it arrived later.

        ``sampled_at`` is derived from the server's own ``NOW()`` minus the
        reported age. The runner's wall clock is never trusted, and the two
        timestamps therefore come from one clock that cannot jump.
        """
        await conn.execute(
            """
            UPDATE agent_runner_hosts
            SET capacity_snapshot = $3::jsonb,
                capacity_received_at = NOW(),
                capacity_sampled_at = NOW() - make_interval(secs => $4::double precision),
                capacity_sample_id = $5::uuid
            WHERE host = $1
              AND credential_hash = $2
              AND capacity_sample_id IS DISTINCT FROM $5::uuid
              AND (
                  capacity_sampled_at IS NULL
                  OR capacity_sampled_at < NOW() - make_interval(secs => $4::double precision)
              )
            """,
            host,
            credential_hash,
            json.dumps(capacity),
            sample_age_ms / 1000.0,
            str(capacity["sample_id"]),
        )
