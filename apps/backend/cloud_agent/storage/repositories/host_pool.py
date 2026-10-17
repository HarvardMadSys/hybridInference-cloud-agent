"""Postgres implementation of the runner-host pool port.

Lifted verbatim from the single storage class this package replaced. The
statements, their bound parameters and their transaction ownership are what
they were; only the class holding them changed.
"""

from __future__ import annotations

import json
import uuid
from collections import deque
from datetime import UTC
from typing import TYPE_CHECKING

from cloud_agent.capacity_protocol import (
    CAPACITY_HISTORY_MAX_AGE_S,
    FRESH_AFTER_SECONDS,
    history_point_write_from_block,
)
from cloud_agent.domain.runtimes import KIND_SELECTION, KIND_UPDATE
from cloud_agent.execution_profiles import KATA_RUNTIME
from cloud_agent.host_pool import (
    HOST_DISABLED,
    HOST_DRAINING,
    HOST_ENROLLING,
    HOST_OFFLINE,
    HOST_ONLINE,
    normalize_capabilities,
    transition_allowed,
)
from cloud_agent.logging_utils import get_logger
from cloud_agent.sandbox_archive_credentials import (
    archive_settings_payload,
    load_stored_archive_policy,
    seal_archive_storage,
)
from cloud_agent.settings import number_setting
from cloud_agent.storage._sql.compute_admission import (
    host_compute_admission,
    lock_compute_entry,
    lock_compute_topology,
)
from cloud_agent.storage._sql.hosts import (
    COVERED_GROUP_SQL,
    HOST_CAPACITY_COLUMNS,
    HOST_COLUMNS,
    HOST_RETAINED_GROUP,
    QUALIFIED_HOST_COLUMNS,
    lock_revocation_rows,
    release_late_covered_sandboxes,
    revoke_host_credentials,
)
from cloud_agent.storage._sql.move_out import (
    MOVABLE_GROUP_SQL,
    UNMOVABLE_GROUP_SQL,
    UNMOVED_GROUP_SQL,
    move_out_possible,
)
from cloud_agent.storage._sql.rows import load_json
from cloud_agent.storage._sql.sandbox import THREAD_SANDBOX_SOURCE
from cloud_agent.storage.repositories.host_capacity import insert_capacity_history_point
from cloud_agent_shared.host_settings import (
    HOST_SETTINGS_BY_KEY,
    RUNNER_SETTING_KEYS,
    admin_values,
    reported_values,
)
from cloud_agent_shared.runtime_catalog import downloadable_runtimes
from cloud_agent_shared.sandbox_archive_settings import (
    SANDBOX_ARCHIVE_CAPABILITY,
    SandboxArchivePolicy,
    SandboxArchivePolicyError,
)
from cloud_agent_shared.sandbox_restore import SANDBOX_RESTORE_CAPABILITY

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence
    from datetime import datetime
    from typing import Any

    import asyncpg


logger = get_logger(__name__)

#: A removal that moves groups out still waits on host ``h``: for a group that
#: can still move, or for the administrator's word on one that never will.
_REMOVAL_WAITS_SQL = (
    "h.removal_moves_out AND ("
    "EXISTS (SELECT 1 FROM agent_sandboxes g WHERE g.host = h.host AND "
    + MOVABLE_GROUP_SQL
    + ") OR (NOT h.removal_leaves_unmoved AND "
    "EXISTS (SELECT 1 FROM agent_sandboxes g WHERE g.host = h.host AND "
    + UNMOVABLE_GROUP_SQL
    + ")))"
)

_LEGACY_SANDBOX_MANAGER_KEYS = frozenset(
    {"terminal_idle_ttl_s", "terminal_hard_ttl_s", "terminal_max_sessions"}
)

_JobDemand = tuple[frozenset[str] | None, str]
_HostShape = tuple[frozenset[str], str | None]
_HostCapacity = tuple[_HostShape, int]


def _job_matches_host(job: _JobDemand, host: _HostShape) -> bool:
    """Mirror the capability and execution-profile half of a new-group claim."""
    required, execution_profile = job
    capabilities, sandbox_runtime = host
    if required is None or not required.issubset(capabilities):
        return False
    return execution_profile == "trusted-v1" or (
        execution_profile == "untrusted-kata-v1" and sandbox_runtime == KATA_RUNTIME
    )


def _maximum_compatible_jobs(jobs: Sequence[_JobDemand], hosts: Sequence[_HostCapacity]) -> int:
    """Return the exact maximum cardinality of the job/host-capacity matching.

    Equal jobs and equal host shapes are aggregated before a small Dinic flow.
    That keeps the graph bounded by compatibility shapes rather than queue
    depth, while retaining the same answer as expanding every free slot into a
    separate host vertex. In particular it handles the constrained/generic
    trap where a greedy pass can waste the only flexible slot.
    """
    job_counts: dict[_JobDemand, int] = {}
    for job in jobs:
        job_counts[job] = job_counts.get(job, 0) + 1
    host_counts: dict[_HostShape, int] = {}
    for shape, capacity in hosts:
        if capacity > 0:
            host_counts[shape] = host_counts.get(shape, 0) + capacity
    if not job_counts or not host_counts:
        return 0

    job_types = list(job_counts)
    host_types = list(host_counts)
    source = 0
    job_offset = 1
    host_offset = job_offset + len(job_types)
    sink = host_offset + len(host_types)
    graph: list[list[list[int]]] = [[] for _ in range(sink + 1)]

    def add_edge(start: int, end: int, capacity: int) -> None:
        graph[start].append([end, capacity, len(graph[end])])
        graph[end].append([start, 0, len(graph[start]) - 1])

    for index, job in enumerate(job_types):
        count = job_counts[job]
        add_edge(source, job_offset + index, count)
        for host_index, host in enumerate(host_types):
            if _job_matches_host(job, host):
                add_edge(job_offset + index, host_offset + host_index, count)
    for index, host in enumerate(host_types):
        add_edge(host_offset + index, sink, host_counts[host])

    def send(node: int, available: int, level: list[int], next_edge: list[int]) -> int:
        if node == sink:
            return available
        while next_edge[node] < len(graph[node]):
            edge = graph[node][next_edge[node]]
            target, capacity, reverse = edge
            if capacity > 0 and level[target] == level[node] + 1:
                pushed = send(target, min(available, capacity), level, next_edge)
                if pushed:
                    edge[1] -= pushed
                    graph[target][reverse][1] += pushed
                    return pushed
            next_edge[node] += 1
        return 0

    total = 0
    while True:
        level = [-1] * len(graph)
        level[source] = 0
        pending = deque([source])
        while pending:
            node = pending.popleft()
            for target, capacity, _reverse in graph[node]:
                if capacity > 0 and level[target] < 0:
                    level[target] = level[node] + 1
                    pending.append(target)
        if level[sink] < 0:
            return total

        next_edge = [0] * len(graph)
        while pushed := send(source, sum(job_counts.values()), level, next_edge):
            total += pushed


def _changed_settings(
    stored: Mapping[str, Any], changes: Mapping[str, int | None]
) -> dict[str, Any]:
    """Apply an admin's change to stored settings: set each value, or drop it on None."""
    updated = dict(stored)
    for key, value in changes.items():
        if value is None:
            updated.pop(key, None)
        else:
            updated[key] = value
    return updated


def _before(stamp: datetime) -> str:
    """``npm install --before`` for an instant: whole seconds, UTC."""
    return stamp.strftime("%Y-%m-%dT%H:%M:%SZ")


def _download_of(record: Mapping[str, Any]) -> dict[str, str]:
    """The part of a stored record a heartbeat answer carries."""
    return {key: str(record.get(key) or "") for key in ("version", "before", "nonce")}


def _replaced(earlier: object, selected: object) -> dict[str, Any]:
    """What a new update returns to on Roll back, and how hosts hold it meanwhile.

    An update replacing one that never switched returns to what that one would
    have: new chats still use it. Otherwise it returns to the selection, whose
    download is the replaced record's when that record is the selection's.
    """
    if isinstance(earlier, dict) and not earlier.get("activated_at"):
        return {
            "previous_version": earlier.get("previous_version"),
            "previous_download": earlier.get("previous_download"),
        }
    held = None
    if isinstance(earlier, dict) and selected is not None and earlier.get("version") == selected:
        held = _download_of(earlier)
    return {"previous_version": selected, "previous_download": held}


def _selection_record(earlier: object, version: str, stamp: datetime) -> dict[str, Any]:
    """The record an admin's own choice of ``version`` leaves, already switched.

    Roll back chooses the version the update replaced; its download keeps the
    dependency cutoff it was first made with, so a host downloading it now
    installs the tree every other host holds.
    """
    held = earlier.get("previous_download") if isinstance(earlier, dict) else None
    before = _before(stamp)
    if isinstance(held, dict) and held.get("version") == version and held.get("before"):
        before = str(held["before"])
    return {
        "kind": KIND_SELECTION,
        "version": version,
        "before": before,
        "nonce": uuid.uuid4().hex,
        "requested_at": stamp.isoformat(),
        "previous_version": None,
        "previous_download": None,
        "activated_at": stamp.isoformat(),
    }


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
                raise ValueError("enrollment_token_removed")
            existing = await conn.fetchrow(
                "SELECT drain_fence_active FROM agent_runner_hosts WHERE host = $1 FOR UPDATE",
                host,
            )
            if existing is not None and existing["drain_fence_active"]:
                raise ValueError("host_drain_fenced")
            retiring = await conn.fetchval(
                """SELECT 1 FROM agent_runner_hosts h
                JOIN agent_compute_nodes n ON n.id=h.compute_node_id
                JOIN agent_compute_allocations a ON a.id=n.allocation_id
                WHERE h.host=$1 AND (a.desired_state='absent' OR a.release_committed_at IS NOT NULL)
            """,
                host,
            )
            if retiring:
                raise ValueError("resource_retiring")
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

    async def record_host_heartbeat(
        self,
        *,
        host: str,
        credential_hash: str | None = None,
        capacity: Mapping[str, Any] | None = None,
        capacity_sample_age_ms: int | None = None,
        physical_host: str | None = None,
        slots_total: int | None = None,
        launch_error: str | None = None,
        runner_version: str | None = None,
        sandbox_manager_url: str | None = None,
        capabilities: list[str] | None = None,
        sandbox_runtime: str | None = None,
        sandbox_image_digest: str | None = None,
        runtime_versions: dict[str, str] | None = None,
        runtime_versions_available: dict[str, list[str]] | None = None,
        settings_applied: Mapping[str, int | float] | None = None,
        settings_local: Mapping[str, int | float] | None = None,
        sandbox_archive_settings_applied_revision: int | None = None,
        sandbox_archive_settings_revision_reported: bool = False,
        runtime_downloads: Sequence[Mapping[str, Any]] | None = None,
    ) -> dict[str, Any] | None:
        """Record a heartbeat, promoting the host to ``online`` when it may.

        ``enrolling → online`` and ``offline → online`` both happen here: a
        machine that comes back after a network partition needs no operator to
        put it back to work. ``draining`` and ``disabled`` deliberately do
        **not** — a heartbeat is the host saying "I am here", not "I should be
        taking work", and letting it re-promote itself would make drain a
        suggestion.

        Returns ``None`` for an unknown host, and for one whose credential is
        no longer the one this request authenticated with. Every field is
        optional and only overwrites when supplied, so a heartbeat that
        carries nothing but liveness does not blank out what enrollment
        declared.

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
        half of the same call still commits. A third, in a savepoint of its
        own again, keeps an accepted observation in the history the chart
        reads — this request is the only place that sample is known to exist,
        because the row it was just written into holds one observation and the
        next heartbeat replaces it.

        Args:
            host: The authenticated host name.
            credential_hash: Digest the request authenticated with. The
                whole beat — liveness, archive revision, capacity write and
                legacy settings import — is refused unless the row still
                holds it.
            capacity: The validated, normalized capacity block to store.
            capacity_sample_age_ms: How old the observation was when sent.
            physical_host: Admin grouping label; never an identity.
            slots_total: The concurrency limit this host currently applies.
            launch_error: Why it is not claiming, or ``""`` when nothing is
                wrong. ``None`` is a host that does not report this at all and
                leaves whatever is stored in place, the same way an unreported
                runtime does.
            runner_version: Runner build the host is running.
            sandbox_manager_url: Address the control plane reaches this host's sandbox manager.
            capabilities: What this host can run.
            sandbox_runtime: Attested container runtime.
            sandbox_image_digest: Digest of the sandbox image it launches.
            runtime_versions: Default CLI version per runtime.
            runtime_versions_available: Every CLI version its image holds.
            settings_applied: The runtime settings the host is applying now.
            settings_local: Legacy environment observations, imported once as
                per-host overrides. Either ``None`` leaves the report in place.
            runtime_downloads: This host's progress on each agent version it
                was asked to hold, already validated. ``None`` leaves it.

        Returns:
            The updated host row, or ``None`` when the host no longer exists
            or no longer holds ``credential_hash``.
        """
        async with self._pool.acquire() as conn, conn.transaction():
            # Match the admin writer's lock order: policy before host. Once
            # defaults are authoritative, a legacy observation is imported at
            # most once and cannot resurrect an explicitly reset setting.
            policy = await conn.fetchrow(
                "SELECT host_settings, host_settings_import_excluded "
                "FROM agent_runner_policy WHERE id FOR SHARE"
            )
            global_settings = (load_json(policy["host_settings"]) or {}) if policy else {}
            global_exclusions = set(policy["host_settings_import_excluded"]) if policy else set()
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
                    labels = COALESCE(labels, '{{}}'::jsonb) || $10::jsonb,
                    -- "" survives COALESCE for the same reason an empty
                    -- sandbox runtime does: it is the host saying nothing is
                    -- wrong, which has to be able to clear a stale reason.
                    launch_error = COALESCE($11, launch_error),
                    settings_applied = COALESCE($12::jsonb, settings_applied),
                    settings_local = COALESCE($13::jsonb, settings_local),
                    sandbox_archive_settings_applied_revision = CASE
                        WHEN $15 AND ($16::text IS NULL OR credential_hash = $16) THEN $14
                        ELSE sandbox_archive_settings_applied_revision
                    END,
                    runtime_downloads = COALESCE($17::jsonb, runtime_downloads)
                -- The credential the request authenticated with must still be
                -- the row's: a beat delayed past a re-enrollment or a removal
                -- must not mark the new machine online, or a removed one alive.
                WHERE host = $1 AND ($16::text IS NULL OR credential_hash = $16)
                RETURNING {HOST_COLUMNS}, settings_imported, settings_import_excluded
                """,
                host,
                slots_total,
                runner_version,
                sandbox_manager_url,
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
                launch_error,
                None if settings_applied is None else json.dumps(dict(settings_applied)),
                None if settings_local is None else json.dumps(dict(settings_local)),
                sandbox_archive_settings_applied_revision,
                sandbox_archive_settings_revision_reported,
                credential_hash,
                None
                if runtime_downloads is None
                else json.dumps([dict(report) for report in runtime_downloads]),
            )
            if row is None:
                return None
            if (
                not row["settings_imported"]
                and settings_local is not None
                and (
                    credential_hash is None
                    or await conn.fetchval(
                        "SELECT credential_hash = $2 FROM agent_runner_hosts WHERE host = $1",
                        host,
                        credential_hash,
                    )
                )
            ):
                observed = reported_values(dict(settings_local)) or {}
                previous = dict(load_json(row["settings_desired"]) or {})
                excluded = global_exclusions | set(row["settings_import_excluded"])
                imports = {
                    key: value
                    for key, value in admin_values(observed).items()
                    if value != HOST_SETTINGS_BY_KEY[key].default
                    and key not in previous
                    and key not in global_settings
                    and key not in excluded
                }
                expected = RUNNER_SETTING_KEYS | (
                    _LEGACY_SANDBOX_MANAGER_KEYS if row["sandbox_manager_url"] else frozenset()
                )
                complete = expected <= observed.keys()
                row = await conn.fetchrow(
                    f"""
                    UPDATE agent_runner_hosts
                    SET settings_desired = $2::jsonb, settings_imported = $3
                    WHERE host = $1
                    RETURNING {HOST_COLUMNS}, settings_imported, settings_import_excluded
                    """,
                    host,
                    json.dumps({**previous, **imports}),
                    complete,
                )
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

    async def set_host_status(self, *, host: str, status: str, force: bool = False) -> str:
        """Move a host to ``status``, refusing transitions the table forbids.

        Returns ``"ok"``, ``"unknown"``, ``"invalid"`` or ``"fenced"``. The table is consulted
        inside the transaction that writes, on a locked row: checking it in the
        caller means an admin draining a host and the reaper marking it offline
        can both read ``online`` and both write, and the loser's decision is the
        one that sticks.

        Leaving ``draining`` cancels a pending removal: putting a host back to
        work is the operator taking that request back.
        """
        async with self._pool.acquire() as conn, conn.transaction():
            row = await conn.fetchrow(
                "SELECT status, drain_fence_active FROM agent_runner_hosts "
                "WHERE host = $1 FOR UPDATE",
                host,
            )
            if row is None:
                return "unknown"
            current = row["status"]
            if row["drain_fence_active"]:
                if current == HOST_DRAINING and status == HOST_DRAINING:
                    return "ok"
                if not force:
                    return "fenced"
            if not transition_allowed(current, status):
                return "invalid"
            await conn.execute(
                "UPDATE agent_runner_hosts SET status = $2, drain_fence_token = NULL, "
                "drain_fence_status_before = NULL, drain_fence_active = FALSE, "
                f"removal_requested_at = CASE WHEN $2 = '{HOST_DRAINING}' "
                "THEN removal_requested_at END WHERE host = $1",
                host,
                status,
            )
        return "ok"

    async def acquire_host_drain_fence(
        self,
        *,
        host: str,
        expected_fence_token: Any | None,
        expected_fence_epoch: Any | None,
        fence_token: Any,
    ) -> tuple[str, str | None, Any | None]:
        """CAS one deployment drain under the host row lock."""
        expected_token = None if expected_fence_token is None else str(expected_fence_token)
        expected_epoch = None if expected_fence_epoch is None else str(expected_fence_epoch)
        requested = str(fence_token)
        async with self._pool.acquire() as conn, conn.transaction():
            row = await conn.fetchrow(
                "SELECT status, drain_fence_token, drain_fence_status_before, "
                "drain_fence_active, drain_fence_epoch "
                "FROM agent_runner_hosts WHERE host = $1 FOR UPDATE",
                host,
            )
            if row is None:
                return "unknown", None, None
            current_token = (
                None if row["drain_fence_token"] is None else str(row["drain_fence_token"])
            )
            current_epoch = row["drain_fence_epoch"]
            if current_token == requested:
                if row["drain_fence_active"] and row["status"] == HOST_DRAINING:
                    return "ok", row["drain_fence_status_before"], current_epoch
                if not row["drain_fence_active"]:
                    return "released", None, current_epoch
                return "invalid", None, current_epoch
            if current_token != expected_token or str(current_epoch) != expected_epoch:
                return "stale", None, current_epoch
            if row["status"] == HOST_DRAINING and not row["drain_fence_active"]:
                return "manual", None, current_epoch
            if row["status"] not in (HOST_ONLINE, HOST_DRAINING):
                return "invalid", None, current_epoch
            status_before = row["status"]
            next_epoch = await conn.fetchval(
                "UPDATE agent_runner_hosts SET status = $2, drain_fence_token = $3::uuid, "
                "drain_fence_status_before = $4, drain_fence_active = TRUE WHERE host = $1 "
                "RETURNING drain_fence_epoch",
                host,
                HOST_DRAINING,
                requested,
                status_before,
            )
        return "ok", status_before, next_epoch

    async def release_host_drain_fence(
        self, *, host: str, fence_token: Any
    ) -> tuple[str, Any | None]:
        """Release only the exact active owner, retaining its idempotency receipt."""
        requested = str(fence_token)
        async with self._pool.acquire() as conn, conn.transaction():
            row = await conn.fetchrow(
                "SELECT status, drain_fence_token, drain_fence_active, drain_fence_epoch "
                "FROM agent_runner_hosts WHERE host = $1 FOR UPDATE",
                host,
            )
            if row is None:
                return "unknown", None
            current_epoch = row["drain_fence_epoch"]
            current_token = (
                None if row["drain_fence_token"] is None else str(row["drain_fence_token"])
            )
            if current_token != requested:
                return "stale", current_epoch
            if row["drain_fence_active"]:
                if row["status"] != HOST_DRAINING:
                    return "invalid", current_epoch
                next_epoch = await conn.fetchval(
                    "UPDATE agent_runner_hosts SET status = $2, drain_fence_active = FALSE, "
                    "removal_requested_at = NULL WHERE host = $1 RETURNING drain_fence_epoch",
                    host,
                    HOST_ONLINE,
                )
                return "ok", next_epoch
            return "ok", current_epoch

    async def set_host_slots_desired(self, *, host: str, slots: int | None) -> str:
        """Ask a host to run ``slots`` turns at once, or with ``None`` its default.

        Returns ``"ok"``, ``"unknown"`` or ``"autoscaling_managed"``. Nothing
        here starts or stops anything: the host reads the value off its next heartbeat answer,
        serves it as its limit, and reports it back as ``slots_total``. The
        claim honours the lower of the two from the next poll on, so a number
        the host has not confirmed admits nothing extra and a lowered one takes
        effect at once. Turns already running are never touched.

        The value survives heartbeats and re-enrollment on purpose. It is a
        decision about the named machine, and neither the heartbeat's
        ``COALESCE`` nor the registration upsert names this column. Bounds are
        the request schema's business; the column refuses only a non-positive
        size, which is not a size.
        """
        async with self._pool.acquire() as conn, conn.transaction():
            await lock_compute_entry(conn)
            current = await conn.fetchrow(
                """SELECT h.host,
                          EXISTS (
                              SELECT 1 FROM agent_compute_nodes n
                              JOIN agent_cloudlab_autoscaling_members m
                                ON m.allocation_id=n.allocation_id
                              WHERE n.id=h.compute_node_id AND m.state<>'released'
                          ) AS autoscaling_managed
                   FROM agent_runner_hosts h WHERE h.host=$1 FOR UPDATE OF h""",
                host,
            )
            if current is None:
                return "unknown"
            if current["autoscaling_managed"]:
                return "autoscaling_managed"
            await conn.execute(
                "UPDATE agent_runner_hosts SET slots_desired=$2 WHERE host=$1",
                host,
                slots,
            )
        return "ok"

    async def set_runner_host_settings(
        self, *, host: str, changes: Mapping[str, int | None]
    ) -> dict[str, Any] | None:
        """Set or clear runtime settings for one server, over the global values.

        ``changes`` names only the settings to change, and ``None`` clears one
        so the global value, or the built-in default, applies again. Settings it does
        not name keep their values. The host applies the result from its next
        heartbeat answer.

        Like ``slots_desired``, the values survive heartbeats and
        re-enrollment: they are a decision about the named machine. Validation
        is the caller's (``cloud_agent_shared.host_settings``). The row is
        locked for the read-modify-write, so two admins changing different
        settings on the same server both land.

        Returns the server's values before the change, or ``None`` for an
        unknown host.
        """
        async with self._pool.acquire() as conn, conn.transaction():
            current = await conn.fetchrow(
                "SELECT settings_desired FROM agent_runner_hosts WHERE host = $1 FOR UPDATE",
                host,
            )
            if current is None:
                return None
            previous = dict(load_json(current["settings_desired"]) or {})
            await conn.execute(
                "UPDATE agent_runner_hosts SET settings_desired = $2::jsonb, "
                "settings_import_excluded = ARRAY(SELECT DISTINCT key FROM "
                "unnest(settings_import_excluded || $3::text[]) AS touched(key)) "
                "WHERE host = $1",
                host,
                json.dumps(_changed_settings(previous, changes)),
                list(changes),
            )
        return previous

    async def remove_host(
        self, *, host: str, force: bool = False, allow_unmoved: bool = False
    ) -> str:
        """Take a host out of the pool, moving its groups out first when they can.

        Returns ``"ok"``, ``"unknown"``, ``"fenced"``, ``"moving_out"``,
        ``"draining"``, ``"no_move_target"``, ``"cannot_move"`` or ``"busy"``.

        A stopped group its checkpoint covers moves on revocation. When other
        groups can move -- checkpoints work, another host would restore them,
        and each can still stop into a checkpoint (``MOVABLE_GROUP_SQL``) --
        the host moves to ``draining`` and moves out (``"moving_out"``): it
        claims nothing, its idle groups stop into checkpoints and are handed
        over one at a time, and :meth:`finish_host_removals` revokes it once
        none is left and its last running turn has ended. Nothing running is
        interrupted. A host still running turns moves out whenever it can,
        even with no group to stop yet: a turn restoring a group, or one that
        claimed a covered group before the removal, leaves that group running
        here once it ends.

        A removal that would leave groups bound to the host needs
        ``allow_unmoved``: ``"no_move_target"`` when groups could move but no
        host or checkpoint is there to take them, ``"cannot_move"`` when some
        never can (``UNMOVABLE_GROUP_SQL``: a stopped group whose checkpoint
        failed, say). With it, whatever can move still moves first; then a
        host still holding slots drains (``"draining"``) and is revoked once
        its running attempts end, and an idle one is revoked now. A group that
        stops moving during the move, as when its checkpoint fails, holds the
        removal until the administrator repeats it with ``allow_unmoved``.
        ``"busy"`` is left for the busy host that cannot drain at all.

        ``force`` ends that work now instead of waiting for it — lease expiry
        *is* the recovery path, and the revocation brings it forward to now
        (see ``revoke_host_credentials``): the attempts' worker tokens name no
        host, so a lease left to run would let the removed machine keep
        renewing it. A retained activation cannot be replayed safely after its
        host disappears, so its expiry marks that chat LOST and fails
        already-queued descendants instead. Covered stopped groups still move.

        Disabling rather than deleting. The row is what
        ``agent_attempts.host_id`` points at, and what an operator reads when
        asking where a job ran; #1158's ``forget`` is still there for the
        machine that is genuinely gone. Removing or forgetting the host row is
        administrative bookkeeping; it is not by itself proof that a retained
        Docker writable layer was deleted.
        """
        async with self._pool.acquire() as conn, conn.transaction():
            # The fence a move-out is decided under, before any row: two hosts
            # removed together cannot each count on the other as a target.
            await lock_compute_entry(conn, exclusive=True)
            # The host's running attempts before its row: the order every
            # writer that ends an attempt keeps (move the attempt, then give
            # its slot back). Expiring those leases while holding the host row
            # first would deadlock against an attempt finishing at that moment.
            await conn.execute(
                "SELECT id FROM agent_attempts WHERE host_id = $1 AND status = 'running' "
                "ORDER BY id FOR UPDATE",
                host,
            )
            # Then its groups, which the counts below read and the revocation
            # hands over: a start fenced in between would slip out of both.
            await lock_revocation_rows(conn, [host])
            row = await conn.fetchrow(
                "SELECT status, slots_used, drain_fence_active, removal_requested_at, "
                "removal_moves_out, removal_leaves_unmoved "
                "FROM agent_runner_hosts WHERE host = $1 FOR UPDATE",
                host,
            )
            if row is None:
                return "unknown"
            if row["drain_fence_active"] and not force:
                return "fenced"
            if force:
                await revoke_host_credentials(conn, [host])
                return "ok"
            # Covered stopped groups move on revocation; movable ones need
            # stopping into a checkpoint first; unmovable ones would stay here.
            groups = await conn.fetchrow(
                f"""
                SELECT count(*) FILTER (WHERE {MOVABLE_GROUP_SQL} AND NOT {COVERED_GROUP_SQL})
                           AS to_stop,
                       count(*) FILTER (WHERE {UNMOVABLE_GROUP_SQL}) AS unmovable
                FROM agent_sandboxes g WHERE g.host = $1 AND {UNMOVED_GROUP_SQL}
                """,
                host,
            )
            leaves_unmoved = allow_unmoved
            if row["removal_requested_at"] is not None and row["removal_moves_out"]:
                # Already moving out: a repeat may only give the word for the
                # groups that will never move.
                leaves_unmoved = allow_unmoved or row["removal_leaves_unmoved"]
                if groups["unmovable"] and not leaves_unmoved:
                    return "cannot_move"
                await conn.execute(
                    "UPDATE agent_runner_hosts SET removal_leaves_unmoved = $2 WHERE host = $1",
                    host,
                    leaves_unmoved,
                )
                return "moving_out"
            # A running turn may leave a group running here that this count
            # did not see: one it is restoring, or a covered one it claimed
            # before the removal and starts after it. The revocation, which
            # waits for the turn, would leave that group behind, so a host
            # still running turns moves out whenever it can.
            moving = bool(groups["to_stop"] or row["slots_used"]) and await move_out_possible(
                conn, [host], need_target=True
            )
            if not allow_unmoved:
                if groups["to_stop"] and not moving:
                    return "no_move_target"
                if groups["unmovable"]:
                    return "cannot_move"
            if row["slots_used"] == 0 and not moving:
                await revoke_host_credentials(conn, [host])
                return "ok"
            if row["status"] != HOST_DRAINING and not transition_allowed(
                row["status"], HOST_DRAINING
            ):
                return "busy"
            await conn.execute(
                "UPDATE agent_runner_hosts SET status = $2, "
                "removal_requested_at = COALESCE(removal_requested_at, NOW()), "
                "removal_moves_out = $3, removal_leaves_unmoved = $4 WHERE host = $1",
                host,
                HOST_DRAINING,
                moving,
                leaves_unmoved,
            )
            return "moving_out" if moving else "draining"
        return "ok"

    async def finish_host_removals(self) -> list[str]:
        """Revoke every host whose requested removal has no running work left.

        The second half of a deferred :meth:`remove_host`. Each host is
        decided in its own transaction, with the locks taken in the order
        ``remove_host`` takes them, and its count read again under the row
        lock: an attempt the reaper is ending at this moment must either be
        finished first or keep the host for the next pass.

        Each pass then hands on any group of an already revoked host whose
        checkpoint arrived with a beat still in flight at the revocation
        (``release_late_covered_sandboxes``).

        Returns the hosts it revoked.
        """
        removed: list[str] = []
        async with self._pool.acquire() as conn:
            candidates = await conn.fetch(
                "SELECT h.host FROM agent_runner_hosts h "
                "WHERE h.removal_requested_at IS NOT NULL AND h.slots_used = 0 "
                "AND NOT (" + _REMOVAL_WAITS_SQL + ") ORDER BY h.host"
            )
            for candidate in candidates:
                host = candidate["host"]
                async with conn.transaction():
                    await conn.execute(
                        "SELECT id FROM agent_attempts WHERE host_id = $1 AND status = 'running' "
                        "ORDER BY id FOR UPDATE",
                        host,
                    )
                    await lock_revocation_rows(conn, [host])
                    row = await conn.fetchrow(
                        "SELECT h.slots_used, h.removal_requested_at, "
                        "(" + _REMOVAL_WAITS_SQL + ") AS moving "
                        "FROM agent_runner_hosts h WHERE h.host = $1 FOR UPDATE",
                        host,
                    )
                    if (
                        row is None
                        or row["removal_requested_at"] is None
                        or row["slots_used"]
                        or row["moving"]
                    ):
                        continue
                    await revoke_host_credentials(conn, [host])
                removed.append(host)
            async with conn.transaction():
                released = await release_late_covered_sandboxes(conn)
        if released:
            logger.info(
                "agent_sandboxes_released_after_revocation",
                extra={
                    "event": "agent_sandboxes_released_after_revocation",
                    "sandbox_ids": released,
                },
            )
        return removed

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
                SET status = '{HOST_OFFLINE}',
                    drain_fence_token = NULL,
                    drain_fence_status_before = NULL,
                    drain_fence_active = FALSE
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
        """The job's running attempt's host, else its retained thread's durable host.

        A finished attempt names where the turn ran, which is not where the
        group is once a restore has moved it to another host; the group's own
        host is. Only a thread without a group falls back to that attempt.
        """
        owner = "COALESCE(CASE WHEN a.status = 'running' THEN a.host_id END, s.host, a.host_id)"
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow(
                f"""
                SELECT {owner} AS owner_host,
                       {QUALIFIED_HOST_COLUMNS}
                FROM agent_jobs j
                LEFT JOIN agent_attempts a ON a.id = j.current_attempt_id
                LEFT JOIN {THREAD_SANDBOX_SOURCE} ON s.thread_id = j.thread_id
                LEFT JOIN agent_runner_hosts h
                  ON h.host = {owner}
                WHERE j.id = $1
                """,
                job_id,
            )
        if row is None or row["owner_host"] is None:
            return None
        host = self._host_row_to_dict(row)
        # A retained sandbox remains hard-pinned even if its host row was
        # administratively forgotten. Returning a placeholder (with no sandbox manager
        # URL) makes sandbox manager routing fail closed instead of falling back to a
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
                SELECT {HOST_COLUMNS}, {HOST_CAPACITY_COLUMNS},
                       (SELECT count(*) FROM agent_sandboxes g
                        WHERE g.host = agent_runner_hosts.host AND {MOVABLE_GROUP_SQL})
                           AS sandboxes_left,
                       (SELECT count(*) FROM agent_sandboxes g
                        WHERE g.host = agent_runner_hosts.host AND {UNMOVABLE_GROUP_SQL})
                           AS sandboxes_unmovable
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
        layers so it can reverse-probe their sandbox managers. Read the policy under a
        shared lock and the owners in the same transaction: a concurrent host
        switch cannot make the snapshot describe two different placement
        policies, and any newly claimed group while this lock is held belongs
        to one of the placement hosts this snapshot already includes.

        ``release_follows_control_plane`` marks a Host of a managed compute
        allocation. The control plane rolls such a Host forward itself once it
        runs the new release, so the rollout cannot wait for it first.
        """
        async with self._pool.acquire() as conn, conn.transaction():
            policy = await conn.fetchrow(
                "SELECT active_host, runtime_versions FROM agent_runner_policy WHERE id FOR SHARE"
            )
            rows = await conn.fetch(
                f"""
                SELECT {QUALIFIED_HOST_COLUMNS},
                       COALESCE(a.management_mode = 'manage'
                                AND a.desired_state = 'present'
                                AND a.release_committed_at IS NULL, FALSE)
                           AS release_follows_control_plane
                FROM agent_runner_hosts h
                LEFT JOIN agent_compute_nodes n ON n.id = h.compute_node_id
                LEFT JOIN agent_compute_allocations a ON a.id = n.allocation_id
                ORDER BY h.last_seen_at DESC
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

    async def autoscaling_observation(self, *, expected_deployment_sha: str) -> dict[str, Any]:
        """Read the durable, content-free signal used by compute autoscaling.

        A retained turn is tied to its existing Host and therefore cannot be
        relieved by adding a CloudLab node. Only the first runnable turn of a
        thread with no sandbox membership is demand here. Capacity is the
        effective claim ceiling (the lower of reported and desired slots),
        after liveness, placement, release/rotation holds, deployment
        attestation, and archive-policy gates have all been applied. Pressure
        is an exact maximum compatibility matching, not queue depth minus an
        interchangeable slot count. The shared topology lock stabilizes
        bindings, but it deliberately remains compatible with claims; repeatable
        read prevents a claim committed between the job and host queries from
        combining pre-claim demand with post-claim capacity into a state that
        never existed.
        """
        async with (
            self._pool.acquire() as conn,
            conn.transaction(isolation="repeatable_read"),
        ):
            policy = await conn.fetchrow(
                """
                SELECT active_host, sandbox_archive_settings, sandbox_archive_revision
                FROM agent_runner_policy WHERE id FOR SHARE
                """
            )
            await lock_compute_topology(conn)
            observed_at = await conn.fetchval("SELECT NOW()")
            job_rows = await conn.fetch(
                """
                SELECT t.execution_profile,
                       COALESCE(j.metadata->'requires', '[]'::jsonb) AS requires
                FROM agent_jobs j
                JOIN agent_threads t ON t.id=j.thread_id
                WHERE j.state='queued' AND NOT j.cancel_requested
                  AND NOT EXISTS (
                      SELECT 1 FROM agent_thread_sandbox_memberships m
                      WHERE m.thread_id=j.thread_id
                  )
                  AND NOT EXISTS (
                      SELECT 1 FROM agent_jobs earlier
                      WHERE earlier.thread_id=j.thread_id
                        AND earlier.turn_no < j.turn_no
                        AND earlier.state IN ('queued','running')
                  )
                  AND NOT EXISTS (
                      SELECT 1 FROM agent_jobs parent
                      WHERE parent.id=j.parent_job_id AND parent.state='succeeded'
                  )
                  AND (
                      j.turn_source <> 'goal'
                      OR (
                          t.goal_state='active'
                          AND t.goal_id=j.goal_id
                          AND t.goal_revision=j.goal_revision
                      )
                  )
                ORDER BY j.created_at, j.id
                """
            )
            job_rows.extend(
                await conn.fetch(
                    "SELECT execution_profile, '[\"environment-v1\"]'::jsonb AS requires FROM agent_environment_runs WHERE state='queued'",
                )
            )
            rows = await conn.fetch(
                f"""
                SELECT {HOST_COLUMNS}, {HOST_CAPACITY_COLUMNS}, n.allocation_id,
                       (am.allocation_id IS NOT NULL) AS autoscaler_owned,
                       (SELECT COUNT(*) FROM agent_sandboxes g
                        WHERE g.host=h.host AND g.deletion_state<>'deleted')
                           AS retained_groups
                FROM agent_runner_hosts h
                LEFT JOIN agent_compute_nodes n ON n.id=h.compute_node_id
                LEFT JOIN agent_cloudlab_autoscaling_members am
                  ON am.allocation_id=n.allocation_id AND am.state<>'released'
                ORDER BY h.host
                """
            )

            archive_raw = (
                {}
                if policy is None
                else {
                    **(load_json(policy["sandbox_archive_settings"]) or {}),
                    "revision": policy["sandbox_archive_revision"],
                }
            )
            try:
                archive_policy = SandboxArchivePolicy.from_mapping(archive_raw)
            except SandboxArchivePolicyError:
                archive_policy = None

            jobs: list[_JobDemand] = []
            for row in job_rows:
                raw_requires = load_json(row["requires"])
                required = (
                    frozenset(raw_requires)
                    if isinstance(raw_requires, list)
                    and all(isinstance(value, str) for value in raw_requires)
                    else None
                )
                jobs.append((required, str(row["execution_profile"])))

            hosts: list[dict[str, Any]] = []
            matching_hosts: list[_HostCapacity] = []
            templates: list[tuple[str, _HostShape]] = []
            free_slots = 0
            active_tests = {
                row["host"]: int(row["count"])
                for row in await conn.fetch(
                    "SELECT host,COUNT(*) AS count FROM agent_environment_runs WHERE host IS NOT NULL AND NOT cleanup_complete AND lease_expires_at>NOW() AND started_at>NOW()-INTERVAL '30 minutes' GROUP BY host"
                )
            }
            cleanup_blocked_hosts = {
                row["host"]
                for row in await conn.fetch(
                    "SELECT DISTINCT host FROM agent_environment_runs WHERE host IS NOT NULL AND NOT cleanup_complete "
                    "AND (lease_expires_at<=NOW() OR started_at<=NOW()-INTERVAL '30 minutes')"
                )
            }
            for raw in rows:
                host = self._host_row_to_dict(raw)
                host["allocation_id"] = raw["allocation_id"]
                host["retained_groups"] = int(raw["retained_groups"])
                hosts.append(host)

                capabilities = host.get("capabilities") or []
                heartbeat = host.get("last_heartbeat_at")
                deployment_ready = any(
                    isinstance(capability, str)
                    and capability.startswith("shared-sandbox-deployment:")
                    for capability in capabilities
                ) and (
                    not expected_deployment_sha
                    or f"shared-sandbox-deployment:{expected_deployment_sha}" in capabilities
                )
                archive_ready = bool(
                    archive_policy is not None
                    and (
                        not archive_policy.enabled
                        or (
                            SANDBOX_ARCHIVE_CAPABILITY in capabilities
                            and host.get("sandbox_archive_settings_applied_revision")
                            == archive_policy.revision
                        )
                    )
                )
                fresh = (
                    bool(
                        heartbeat is not None
                        and (observed_at - heartbeat).total_seconds() <= FRESH_AFTER_SECONDS
                    )
                    and host["host"] not in cleanup_blocked_hosts
                )
                if not (
                    host.get("status") == HOST_ONLINE
                    and host.get("enrolled")
                    and not host.get("launch_error")
                    and fresh
                    and (policy is None or policy["active_host"] in (None, host["host"]))
                    and "shared-sandbox-v1" in capabilities
                    and "native-fork-at-anchor-v1" in capabilities
                    and deployment_ready
                    and archive_ready
                ):
                    continue
                allows_new_groups, _ = await host_compute_admission(conn, host["host"])
                if not allows_new_groups:
                    continue
                shape = (
                    frozenset(str(capability) for capability in capabilities),
                    host.get("sandbox_runtime")
                    if isinstance(host.get("sandbox_runtime"), str)
                    else None,
                )
                if raw["autoscaler_owned"]:
                    templates.append((str(host["host"]), shape))
                total = host.get("slots_total")
                desired = host.get("slots_desired")
                ceilings = [value for value in (total, desired) if isinstance(value, int)]
                if not ceilings:
                    continue
                free = max(
                    0,
                    min(ceilings)
                    - int(host.get("slots_used") or 0)
                    - active_tests.get(host["host"], 0),
                )
                free_slots += free
                if free:
                    matching_hosts.append((shape, free))

            matchable_jobs = jobs
            if templates:
                matchable_jobs = [
                    job
                    for job in jobs
                    if any(_job_matches_host(job, shape) for _host, shape in templates)
                ]
            matched = _maximum_compatible_jobs(matchable_jobs, matching_hosts)

        return {
            "observed_at": observed_at,
            "active_host": None if policy is None else policy["active_host"],
            "runnable_new_group_jobs": len(jobs),
            "fresh_effective_free_slots": free_slots,
            "unmatched_new_group_jobs": len(matchable_jobs) - matched,
            "autoscaling_template_hosts": [host for host, _shape in templates],
            "hosts": hosts,
        }

    async def queue_health(self, *, expected_deployment_sha: str) -> dict[str, int]:
        """Observe configured queue stalls without claiming or changing jobs.

        Occupied slots are ordinary backpressure. Missing/stale/disabled hosts,
        lost deployment markers and a wrong placement pin are not. Retained
        members use their owner, independent of the pin for new conversations.
        A ``cold`` group lives in its checkpoint instead: any host that
        restores may take it, except the one it left while that one still
        names its container, as the claim's cold branch reads it.
        No user content, job identifiers or credentials leave this projection.
        """
        async with self._pool.acquire() as conn, conn.transaction():
            archive_row = await conn.fetchrow(
                """
                SELECT sandbox_archive_settings, sandbox_archive_revision
                FROM agent_runner_policy WHERE id FOR SHARE
                """
            )
            try:
                archive_policy = load_stored_archive_policy(
                    {}
                    if archive_row is None
                    else {
                        **(load_json(archive_row["sandbox_archive_settings"]) or {}),
                        "revision": archive_row["sandbox_archive_revision"],
                    }
                )
                archive_required = archive_policy.enabled
                archive_revision = archive_policy.revision
            except SandboxArchivePolicyError:
                # Claims fail closed on an invalid stored policy. Monitoring
                # must not advertise capacity that the claim path will refuse.
                archive_required = True
                archive_revision = -1
            row = await conn.fetchrow(
                """
                WITH waiting AS (
                    SELECT g.host AS owner, t.execution_profile,
                           COALESCE(g.state = 'cold', FALSE) AS cold,
                           g.container_id IS NULL AS uncontained,
                           COALESCE(j.metadata->'requires', '[]'::jsonb) AS requires
                    FROM agent_jobs j
                    JOIN agent_threads t ON t.id = j.thread_id
                    LEFT JOIN agent_thread_sandbox_memberships m ON m.thread_id = t.id
                    LEFT JOIN agent_sandboxes g ON g.id = m.sandbox_id
                    WHERE j.state = 'queued' AND NOT j.cancel_requested
                      AND j.updated_at < NOW() - $3 * INTERVAL '1 second'
                      AND NOT EXISTS (
                          SELECT 1 FROM agent_jobs earlier
                          WHERE earlier.thread_id = j.thread_id AND earlier.turn_no < j.turn_no
                            AND earlier.state IN ('queued', 'running')
                      )
                ), serving AS (
                    SELECT h.* FROM agent_runner_hosts h
                    WHERE h.status = 'online' AND h.credential_hash IS NOT NULL
                      AND h.last_heartbeat_at >= NOW() - INTERVAL '90 seconds'
                      AND h.capabilities @> '["shared-sandbox-v1"]'::jsonb
                      AND COALESCE(h.slots_total, 1) > 0
                      AND EXISTS (
                          SELECT 1 FROM jsonb_array_elements_text(h.capabilities) c
                          WHERE c LIKE 'shared-sandbox-deployment:%'
                      )
                      AND (
                          NOT $4::boolean
                          OR (
                              h.sandbox_archive_settings_applied_revision = $5
                              AND h.capabilities @> $6::jsonb
                          )
                      )
                )
                SELECT COUNT(*) AS queued,
                       COUNT(*) FILTER (WHERE NOT EXISTS (
                           SELECT 1 FROM serving h
                           WHERE waiting.requires <@ h.capabilities
                             AND (waiting.execution_profile = 'trusted-v1' OR h.sandbox_runtime = $2)
                             AND (
                                 (waiting.owner IS NOT NULL AND NOT waiting.cold
                                  AND h.host = waiting.owner)
                                 OR (waiting.cold AND $4::boolean
                                     AND h.capabilities @> $7::jsonb
                                     AND (waiting.uncontained OR h.host <> waiting.owner))
                                 OR (waiting.owner IS NULL
                                     AND h.capabilities @> '["native-fork-at-anchor-v1"]'::jsonb
                                     AND ($1 = '' OR h.capabilities ? ('shared-sandbox-deployment:' || $1))
                                     AND ((SELECT active_host FROM agent_runner_policy WHERE id) IS NULL
                                          OR h.host = (SELECT active_host FROM agent_runner_policy WHERE id)))
                             )
                       )) AS unavailable,
                       (SELECT COUNT(*) FROM agent_jobs WHERE state = 'running') AS running
                FROM waiting
                """,
                expected_deployment_sha,
                KATA_RUNTIME,
                number_setting("queue_alert_after_s", 600.0),
                archive_required,
                archive_revision,
                json.dumps([SANDBOX_ARCHIVE_CAPABILITY]),
                json.dumps([SANDBOX_RESTORE_CAPABILITY]),
                timeout=10,
            )
        return {key: int(row[key]) for key in ("queued", "unavailable", "running")}

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

        A choice made here also replaces that runtime's update, in progress or
        done: the admin picked a version by hand, and an update finishing
        afterwards must not switch new chats away from it. A version an agent
        can download stays a target, so a host that joins later, or lost its
        copy, downloads it too instead of starting new chats on its default.
        Roll back is this call with the version the update replaced, and keeps
        the dependency cutoff that version was first downloaded with.
        """
        async with self._pool.acquire() as conn, conn.transaction():
            policy = await conn.fetchrow(
                "SELECT runtime_versions, runtime_upgrades, NOW() AS now "
                "FROM agent_runner_policy WHERE id FOR UPDATE"
            )
            now = await conn.fetchval("SELECT NOW()") if policy is None else policy["now"]
            current = None if policy is None else policy["runtime_versions"]
            upgrades = {} if policy is None else dict(load_json(policy["runtime_upgrades"]) or {})
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
            record = upgrades.get(runtime)
            if version is None or runtime not in downloadable_runtimes():
                # The image default is on every host; nothing to hand out.
                upgrades.pop(runtime, None)
            elif not (isinstance(record, dict) and record.get("version") == version):
                upgrades[runtime] = _selection_record(record, version, now.astimezone(UTC))
            # Upsert for the same reason the host pin is one: against a database
            # whose singleton row is missing, a bare UPDATE touches nothing and
            # still reports success.
            await conn.execute(
                """
                INSERT INTO agent_runner_policy (id, runtime_versions, runtime_upgrades)
                VALUES (TRUE, $1::jsonb, $2::jsonb)
                ON CONFLICT (id) DO UPDATE
                SET runtime_versions = EXCLUDED.runtime_versions,
                    runtime_upgrades = EXCLUDED.runtime_upgrades,
                    updated_at = NOW()
                """,
                json.dumps(selection),
                json.dumps(upgrades),
            )
            return True

    async def runtime_download_targets(self) -> list[dict[str, str]]:
        """What every heartbeat answer tells hosts to hold, per runtime.

        The record's version, and the downloaded version an update replaced.
        Until the update switches new chats, they still use that one: a host
        that joins while the update is downloading, or after it failed, must
        run what every other host runs. After the switch it is what Roll back
        returns to, so rolling back is instant on every host, one that joins
        later included. The switch waits only for the record's own version.
        The version new chats use comes first. Done targets stay, so a host
        that joins later, or restarts on a new sandbox image, downloads and
        checks the version again.

        A selection record, Roll back's included, replaced nothing to hold:
        the version rolled back from stays only where chats still run it.
        """
        async with self._pool.acquire() as conn:
            policy = await conn.fetchrow(
                "SELECT runtime_versions, runtime_upgrades FROM agent_runner_policy WHERE id"
            )
        selection = {} if policy is None else dict(load_json(policy["runtime_versions"]) or {})
        upgrades = {} if policy is None else dict(load_json(policy["runtime_upgrades"]) or {})
        targets: list[dict[str, str]] = []
        for runtime, record in sorted(upgrades.items()):
            if not isinstance(record, dict):
                continue
            own = {"runtime": runtime, **_download_of(record)}
            held = record.get("previous_download")
            if not isinstance(held, dict) or held.get("version") == record.get("version"):
                targets.append(own)
            elif not record.get("activated_at"):
                if held.get("version") == selection.get(runtime):
                    targets.append({"runtime": runtime, **_download_of(held)})
                targets.append(own)
            else:
                targets.append(own)
                if held.get("version") == record.get("previous_version"):
                    targets.append({"runtime": runtime, **_download_of(held)})
        return targets

    async def request_runtime_upgrade(
        self, *, runtime: str, version: str, requested_by: str
    ) -> dict[str, Any]:
        """Ask every host to download ``version``, replacing any earlier update.

        Asking again for the record's own version is how a failed download is
        retried: only the nonce changes. The dependency cutoff stays, because a
        host that already holds the version keeps the tree it installed, and a
        host retrying must resolve the same one. What Roll back returns to, and
        whether new chats were switched, stay as they were.

        For another version, ``previous_version`` is the selection this
        replaces — what Roll back returns to — or ``None`` for the image
        default, and ``previous_download`` is how that version was downloaded,
        so hosts keep holding it until the switch.
        """
        async with self._pool.acquire() as conn, conn.transaction():
            policy = await conn.fetchrow(
                "SELECT runtime_versions, runtime_upgrades, NOW() AS now "
                "FROM agent_runner_policy WHERE id FOR UPDATE"
            )
            now = await conn.fetchval("SELECT NOW()") if policy is None else policy["now"]
            selection = {} if policy is None else dict(load_json(policy["runtime_versions"]) or {})
            upgrades = {} if policy is None else dict(load_json(policy["runtime_upgrades"]) or {})
            earlier = upgrades.get(runtime)
            if isinstance(earlier, dict) and earlier.get("version") == version:
                target = {**earlier, "nonce": uuid.uuid4().hex}
            else:
                stamp = now.astimezone(UTC)
                target = {
                    "kind": KIND_UPDATE,
                    "version": version,
                    "before": _before(stamp),
                    "nonce": uuid.uuid4().hex,
                    "requested_by": requested_by,
                    "requested_at": stamp.isoformat(),
                    **_replaced(earlier, selection.get(runtime)),
                    "activated_at": None,
                }
            upgrades[runtime] = target
            await conn.execute(
                """
                INSERT INTO agent_runner_policy (id, runtime_upgrades) VALUES (TRUE, $1::jsonb)
                ON CONFLICT (id) DO UPDATE
                SET runtime_upgrades = EXCLUDED.runtime_upgrades, updated_at = NOW()
                """,
                json.dumps(upgrades),
            )
            return target

    async def activate_ready_runtime_upgrades(self) -> list[str]:
        """Switch new chats to each update every online host now holds.

        Called after every heartbeat, so the cheap case — nothing waiting —
        is one unlocked read. "Every online host" is every enrolled host the
        claim would hand a new chat to; with none online, nothing switches,
        because nothing has shown the version runs here.

        Returns the runtimes switched.
        """
        async with self._pool.acquire() as conn:
            stored = await conn.fetchval(
                "SELECT runtime_upgrades FROM agent_runner_policy WHERE id"
            )
            if not any(
                isinstance(target, dict) and not target.get("activated_at")
                for target in (load_json(stored) or {}).values()
            ):
                return []
            async with conn.transaction():
                policy = await conn.fetchrow(
                    "SELECT runtime_versions, runtime_upgrades, NOW() AS now "
                    "FROM agent_runner_policy WHERE id FOR UPDATE"
                )
                if policy is None:
                    return []
                selection = dict(load_json(policy["runtime_versions"]) or {})
                upgrades = dict(load_json(policy["runtime_upgrades"]) or {})
                menus = [
                    load_json(row["runtime_versions_available"]) or {}
                    for row in await conn.fetch(
                        f"""
                        SELECT runtime_versions_available FROM agent_runner_hosts
                        WHERE status = '{HOST_ONLINE}' AND credential_hash IS NOT NULL
                        """
                    )
                ]
                switched: list[str] = []
                for runtime, target in upgrades.items():
                    if not isinstance(target, dict) or target.get("activated_at"):
                        continue
                    version = target.get("version")
                    if menus and all(version in (menu.get(runtime) or []) for menu in menus):
                        selection[runtime] = version
                        upgrades[runtime] = {
                            **target,
                            "activated_at": policy["now"].astimezone(UTC).isoformat(),
                        }
                        switched.append(runtime)
                if switched:
                    await conn.execute(
                        """
                        UPDATE agent_runner_policy
                        SET runtime_versions = $1::jsonb, runtime_upgrades = $2::jsonb,
                            updated_at = NOW()
                        WHERE id
                        """,
                        json.dumps(selection),
                        json.dumps(upgrades),
                    )
                return switched

    async def runtime_upgrade_snapshot(
        self,
    ) -> tuple[dict[str, Any], dict[str, str], list[dict[str, Any]]]:
        """Targets, the selection and every host's versions and progress, together.

        One read-only snapshot, so the progress the page shows and the
        selection it shows beside it are the same moment's.
        """
        async with (
            self._pool.acquire() as conn,
            conn.transaction(isolation="repeatable_read", readonly=True),
        ):
            policy = await conn.fetchrow(
                "SELECT runtime_versions, runtime_upgrades FROM agent_runner_policy WHERE id"
            )
            rows = await conn.fetch(
                """
                SELECT host, status, (credential_hash IS NOT NULL) AS enrolled,
                       runtime_versions, runtime_versions_available, runtime_downloads
                FROM agent_runner_hosts
                ORDER BY host
                """
            )
        upgrades = {} if policy is None else dict(load_json(policy["runtime_upgrades"]) or {})
        selection = {} if policy is None else dict(load_json(policy["runtime_versions"]) or {})
        hosts = [
            {
                "host": row["host"],
                "status": row["status"],
                "enrolled": bool(row["enrolled"]),
                "runtime_versions": load_json(row["runtime_versions"]) or {},
                "runtime_versions_available": load_json(row["runtime_versions_available"]) or {},
                "runtime_downloads": load_json(row["runtime_downloads"]) or [],
            }
            for row in rows
        ]
        return upgrades, selection, hosts

    async def host_settings(self) -> dict[str, Any]:
        """The runtime settings set for every server, as stored.

        A server's own values win over these, and a setting set nowhere falls
        back to the built-in default. Read through
        ``cloud_agent_shared.host_settings.admin_values``, which drops anything
        a release can no longer apply.
        """
        async with self._pool.acquire() as conn:
            stored = await conn.fetchval("SELECT host_settings FROM agent_runner_policy WHERE id")
        return dict(load_json(stored) or {})

    async def set_host_settings(self, *, changes: Mapping[str, int | None]) -> dict[str, Any]:
        """Set or clear runtime settings for every server.

        ``changes`` names only the settings to change, and ``None`` clears one.
        Every host applies the result from its next heartbeat answer, except
        where a value set for that server wins. The policy row is locked for
        the read-modify-write, the same first lock every other policy writer
        takes, so this serializes with them and with a concurrent change to
        another setting. Returns the values before the change, for the audit
        record.
        """
        async with self._pool.acquire() as conn, conn.transaction():
            stored = await conn.fetchval(
                "SELECT host_settings FROM agent_runner_policy WHERE id FOR UPDATE"
            )
            previous = dict(load_json(stored) or {})
            # Upsert for the same reason the pin and the version selection are
            # one: against a database whose singleton row is missing, a bare
            # UPDATE touches nothing and still reports success.
            await conn.execute(
                """
                INSERT INTO agent_runner_policy (id, host_settings, host_settings_import_excluded)
                VALUES (TRUE, $1::jsonb, $2::text[])
                ON CONFLICT (id) DO UPDATE
                SET host_settings = EXCLUDED.host_settings,
                    host_settings_import_excluded = ARRAY(
                        SELECT DISTINCT key FROM unnest(
                            agent_runner_policy.host_settings_import_excluded ||
                            EXCLUDED.host_settings_import_excluded
                        ) AS touched(key)
                    ),
                    updated_at = NOW()
                """,
                json.dumps(_changed_settings(previous, changes)),
                list(changes),
            )
        return previous

    async def sandbox_archive_settings(self) -> dict[str, Any]:
        """Return the normalized deployment-wide archive policy and its timestamp."""
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                SELECT sandbox_archive_settings, sandbox_archive_revision,
                       sandbox_archive_updated_at
                FROM agent_runner_policy WHERE id
                """
            )
        if row is None:
            # The baseline normally creates the singleton. Keep a missing row
            # readable during restore/bootstrap; the first write recreates it.
            return archive_settings_payload(SandboxArchivePolicy(), None)
        raw = dict(load_json(row["sandbox_archive_settings"]) or {})
        raw["revision"] = row["sandbox_archive_revision"]
        policy = load_stored_archive_policy(raw)
        return archive_settings_payload(policy, row["sandbox_archive_updated_at"])

    async def set_sandbox_archive_settings(
        self,
        *,
        expected_revision: int,
        settings: Mapping[str, Any],
    ) -> dict[str, Any] | None:
        """Atomically replace the archive policy, or return ``None`` on a stale revision."""
        async with self._pool.acquire() as conn, conn.transaction():
            await conn.execute(
                "INSERT INTO agent_runner_policy (id) VALUES (TRUE) ON CONFLICT (id) DO NOTHING"
            )
            current = await conn.fetchrow(
                """
                SELECT sandbox_archive_settings, sandbox_archive_revision,
                       sandbox_archive_updated_at
                FROM agent_runner_policy WHERE id FOR UPDATE
                """
            )
            assert current is not None
            current_raw = dict(load_json(current["sandbox_archive_settings"]) or {})
            current_raw["revision"] = current["sandbox_archive_revision"]
            current_policy = load_stored_archive_policy(current_raw)
            requested = SandboxArchivePolicy.from_mapping(
                {**dict(settings), "revision": current_policy.revision}
            )
            current_response = archive_settings_payload(
                current_policy,
                current["sandbox_archive_updated_at"],
                _changed=False,
            )
            if expected_revision > current_policy.revision:
                return None
            if requested.as_storage_dict() == current_policy.as_storage_dict():
                # Covers both an ordinary no-op and a retry after a successful
                # response was lost. Neither invents a new rollout revision.
                return current_response
            if current_policy.revision != expected_revision:
                return None
            policy = SandboxArchivePolicy.from_mapping(
                {**dict(settings), "revision": current_policy.revision + 1}
            )
            updated_at = await conn.fetchval(
                """
                UPDATE agent_runner_policy
                SET sandbox_archive_settings = $1::jsonb,
                    sandbox_archive_revision = $2,
                    sandbox_archive_updated_at = NOW(),
                    updated_at = NOW()
                WHERE id
                RETURNING sandbox_archive_updated_at
                """,
                json.dumps(seal_archive_storage(policy.as_storage_dict())),
                policy.revision,
            )
        return archive_settings_payload(policy, updated_at, _changed=True)

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
            await lock_compute_topology(conn)
            # Checked before anything is written — and locked, not merely read:
            # a bare existence check lets a concurrent removal commit before
            # this does, leaving the queue pinned to a host that is gone.
            if host is not None:
                known = await conn.fetchval(
                    "SELECT 1 FROM agent_runner_hosts WHERE host = $1 FOR SHARE", host
                )
                if not known:
                    return False
                admitted, _ = await host_compute_admission(conn, host)
                if not admitted:
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
        """Drop a host and its enrollment tokens from the pool.

        The removed credential and old enrollment tokens cannot restore it;
        registering it again requires a freshly issued admin enrollment token.

        Returns ``"deleted"`` or ``"unknown"``; refuses ``"active"``,
        ``"fenced"``,
        ``"retained_groups_present"``, ``"resource_bound"``,
        ``"active_attempts_present"`` and ``"pending_compute_operations"``.
        A sandbox whose deletion has completed is an audit row: it keeps the
        host name and does not block forgetting. Nor does a ``cold`` group,
        which any restore-capable host may claim; it keeps naming this host as
        the last it ran on until a claim moves it (``HOST_RETAINED_GROUP``).
        A live or stopped group, or one whose deletion failed or is still in
        progress, does block. A disabled attached machine without external
        release capability may be deregistered after dependency checks; this
        never shuts it down.

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
            await lock_compute_topology(conn, exclusive=True)
            if active == host:
                return "active"
            retained = await conn.fetchval(
                f"SELECT 1 FROM agent_sandboxes g WHERE g.host=$1 AND {HOST_RETAINED_GROUP} LIMIT 1",
                host,
            )
            if retained:
                return "retained_groups_present"
            host_row = await conn.fetchrow(
                "SELECT compute_node_id,status,drain_fence_active "
                "FROM agent_runner_hosts WHERE host=$1 FOR UPDATE",
                host,
            )
            if host_row is not None and host_row["drain_fence_active"]:
                return "fenced"
            bound = None if host_row is None else host_row["compute_node_id"]
            local_cleanup_allocation = None
            if bound is not None:
                resource = await conn.fetchrow(
                    """SELECT a.id,a.observed_state,a.release_supported
                    FROM agent_compute_allocations a JOIN agent_compute_nodes n ON n.allocation_id=a.id
                    WHERE n.id=$1""",
                    bound,
                )
                if resource is None:
                    return "resource_bound"
                local_cleanup = (
                    not resource["release_supported"] and host_row["status"] == HOST_DISABLED
                )
                if resource["observed_state"] not in ("terminated", "absent") and not local_cleanup:
                    return "resource_bound"
                if await conn.fetchval(
                    """SELECT 1 FROM agent_compute_operations
                    WHERE allocation_id=$1 AND state NOT IN ('succeeded','failed','cancelled') LIMIT 1""",
                    resource["id"],
                ):
                    return "pending_compute_operations"
                if local_cleanup:
                    local_cleanup_allocation = resource["id"]
            running = await conn.fetchval(
                "SELECT 1 FROM agent_attempts WHERE host_id=$1 AND status='running' LIMIT 1", host
            )
            if running:
                return "active_attempts_present"
            deleted = await conn.fetchval(
                "DELETE FROM agent_runner_hosts WHERE host = $1 RETURNING host", host
            )
            if deleted is not None:
                await conn.execute(
                    "DELETE FROM agent_host_enroll_tokens WHERE host=$1 OR consumed_host=$1", host
                )
            if deleted is not None and local_cleanup_allocation is not None:
                # This is inventory deregistration for an attached machine,
                # never a provider release, shutdown, or filesystem action.
                await conn.execute(
                    """UPDATE agent_compute_allocations a
                    SET observed_state='absent',desired_state='absent',provider_state='deregistered',
                        revision=revision+1,updated_at=NOW()
                    WHERE a.id=$1 AND NOT EXISTS (
                        SELECT 1 FROM agent_runner_hosts h JOIN agent_compute_nodes n ON n.id=h.compute_node_id
                        WHERE n.allocation_id=a.id)
                """,
                    local_cleanup_allocation,
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
        # Read with ``.get`` like the capacity columns: a row from a projection
        # that predates them must still decode. The report keeps None (never
        # reported) apart from {} (reported, nothing to say).
        host["settings_desired"] = load_json(host.get("settings_desired")) or {}
        host["settings_applied"] = load_json(host.get("settings_applied"))
        host["settings_local"] = load_json(host.get("settings_local"))
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

        An accepted observation is then kept twice: in the host row, which is
        the current reading the table shows, and in the history table, which is
        the chart. Both from this request, because this is the only moment the
        sample is known to exist — the row holds one observation and the next
        heartbeat overwrites it.
        """
        try:
            async with conn.transaction():
                accepted = await PostgresHostPoolRepository._record_host_capacity(
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
            return
        if accepted is None:
            # Refused by one of the write's own conditions: a replay, an
            # observation older than the stored one, or a credential that has
            # since been replaced. None of them is a new measurement, so none
            # of them belongs in the history either.
            return
        await PostgresHostPoolRepository._keep_capacity_history_isolated(
            conn,
            host=host,
            capacity=capacity,
            sample_age_ms=sample_age_ms,
            accepted=accepted,
        )

    @staticmethod
    async def _keep_capacity_history_isolated(
        conn: Any,
        *,
        host: str,
        capacity: Mapping[str, Any],
        sample_age_ms: int,
        accepted: Mapping[str, Any],
    ) -> None:
        """Add this observation to the chart's history, in a savepoint of its own.

        **Why the write is here and not on a timer.** The host row holds one
        observation, and every heartbeat replaces it. A task that re-read that
        row on its own clock could therefore only ever see whichever sample
        happened to be in it at that instant: every other sample was already
        gone. At a 30-second report and a 60-second pass the two clocks drift
        against each other, and each time they crossed, a minute went by whose
        sample had been overwritten before anything read it. That minute was
        then missing from the table for good — nothing backfills, by design —
        and the chart drew a gap no outage had caused. Writing from the request
        that accepts the sample has no clock to drift against.

        **Why a second savepoint.** The reading an operator sees in the table
        was just accepted, and it must not be rolled back because another
        table refused a row. This failure costs one point in a curve; the one
        it is being kept away from costs the current number for a live machine.

        Args:
            conn: The heartbeat's connection, inside its transaction.
            host: The host the observation belongs to.
            capacity: The block that was accepted.
            sample_age_ms: How old the observation was when it was sent.
            accepted: Epoch, instant and identity, as the accepting write
                returned them.
        """
        if sample_age_ms > CAPACITY_HISTORY_MAX_AGE_S * 1000:
            # A sampler that has been stalled for minutes is reporting a
            # measurement, but not this minute's: it is admitted as the current
            # reading, where its age is shown beside it, and kept out of a
            # curve that has no way to say how late it was.
            return
        point = history_point_write_from_block(
            capacity,
            sampled_at=accepted["capacity_sampled_at"],
            sample_id=str(accepted["capacity_sample_id"]),
        )
        if point is None:
            return
        try:
            async with conn.transaction():
                await insert_capacity_history_point(
                    conn,
                    host=host,
                    capacity_epoch=accepted["capacity_epoch"],
                    point=point,
                )
        except Exception:
            logger.warning(
                "agent_host_capacity_history_write_failed",
                exc_info=True,
                extra={"event": "agent_host_capacity_history_write_failed", "host": host},
            )

    @staticmethod
    async def _record_host_capacity(
        conn: Any,
        *,
        host: str,
        credential_hash: str,
        capacity: Mapping[str, Any],
        sample_age_ms: int,
    ) -> Any:
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

        Returns:
            The epoch, instant and identity the row now holds, or ``None`` when
            one of the three conditions refused the write. The caller keeps the
            history point from this row rather than from the block it sent, so
            the point and the snapshot cannot disagree about which observation
            they are, or about when it was taken.
        """
        return await conn.fetchrow(
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
            RETURNING capacity_epoch, capacity_sampled_at, capacity_sample_id
            """,
            host,
            credential_hash,
            json.dumps(capacity),
            sample_age_ms / 1000.0,
            str(capacity["sample_id"]),
        )
