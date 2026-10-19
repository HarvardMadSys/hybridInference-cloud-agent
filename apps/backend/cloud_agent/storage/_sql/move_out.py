"""Whether a leaving Host's groups can move out through their checkpoints.

Shared by the lease-driven move-out hold and an administrator's removal. Both
call it inside a transaction that holds the exclusive compute topology fence,
so two Hosts leaving together cannot each count on the other as a target.
"""

from __future__ import annotations

import json
from typing import Any

from cloud_agent.execution_profiles import KATA_RUNTIME, ExecutionProfile
from cloud_agent.sandbox_archive_credentials import load_stored_archive_policy
from cloud_agent.storage._sql.compute_admission import host_compute_admission
from cloud_agent.storage._sql.hosts import COVERED_GROUP_SQL, UNDELETED_GROUP_SQL
from cloud_agent.storage._sql.rows import load_json
from cloud_agent_shared.sandbox_archive_settings import (
    SANDBOX_ARCHIVE_CAPABILITY,
    SANDBOX_CHECKPOINT_CAPABILITY,
)
from cloud_agent_shared.sandbox_restore import (
    PULLABLE_IMAGE_PATTERN,
    SANDBOX_RESTORE_CAPABILITY,
)

#: A leaving Host must have beaten this recently to stop and capture its groups.
_LIVE_HEARTBEAT_SECONDS = 120


async def move_out_possible(conn: Any, leaving: list[str], *, need_target: bool) -> bool:
    """Whether ``leaving`` can stop its groups into checkpoints another Host restores."""
    return await move_out_blocker(conn, leaving, need_target=need_target) is None


async def move_out_blocker(conn: Any, leaving: list[str], *, need_target: bool) -> str | None:
    """Why ``leaving`` cannot stop its groups into checkpoints another Host restores.

    ``None`` means it can. Checkpoints must work: archives and checkpoints on,
    and every leaving Host alive and capturing on the current archive
    revision. Otherwise its stopped groups would have no checkpoint and their
    chats would only wait. With ``need_target``, another Host must also pass
    the claim's own admission for new groups now, since restoring creates the
    group's container there, and run the Kata runtime when an
    ``untrusted-kata-v1`` group would move: the claim hands such a chat to no
    other Host, so it would wait in its checkpoint for good. The reason is a
    fixed code an operator alert can name, never remote output.
    """
    if not leaving:
        return "no_host"
    policy = await conn.fetchrow(
        "SELECT sandbox_archive_settings, sandbox_archive_revision FROM agent_runner_policy WHERE id"
    )
    if policy is None:
        return "archives_disabled"
    try:
        archive = load_stored_archive_policy(
            {
                **(load_json(policy["sandbox_archive_settings"]) or {}),
                "revision": policy["sandbox_archive_revision"],
            }
        )
    except ValueError:
        return "archives_disabled"
    if not archive.enabled:
        return "archives_disabled"
    if not archive.checkpoints_enabled:
        return "checkpoints_disabled"
    hosts = await conn.fetch(
        """SELECT capabilities, sandbox_archive_settings_applied_revision,
                  credential_hash IS NOT NULL AS enrolled,
                  last_heartbeat_at > NOW() - make_interval(secs => $2) AS alive
           FROM agent_runner_hosts WHERE host = ANY($1::text[])""",
        leaving,
        _LIVE_HEARTBEAT_SECONDS,
    )
    if len(hosts) != len(set(leaving)) or any(
        not host["enrolled"] or not host["alive"] for host in hosts
    ):
        return "host_offline"
    if any(
        SANDBOX_CHECKPOINT_CAPABILITY not in (load_json(host["capabilities"]) or [])
        for host in hosts
    ):
        return "host_without_checkpoints"
    if any(host["sandbox_archive_settings_applied_revision"] != archive.revision for host in hosts):
        return "archive_settings_not_applied"
    if not need_target:
        return None
    untrusted = await conn.fetchval(
        f"""SELECT EXISTS (
               SELECT 1 FROM agent_sandboxes g
               WHERE g.host = ANY($1::text[]) AND {MOVABLE_GROUP_SQL}
                 AND g.execution_profile = $2
           )""",
        leaving,
        ExecutionProfile.UNTRUSTED_KATA_V1.value,
    )
    candidates = await conn.fetch(
        """SELECT host FROM agent_runner_hosts
           WHERE status = 'online' AND credential_hash IS NOT NULL
             AND capabilities @> $2::jsonb
             AND sandbox_archive_settings_applied_revision = $3
             AND NOT host = ANY($1::text[])
             AND (NOT $4::boolean OR sandbox_runtime = $5)
           ORDER BY host""",
        leaving,
        json.dumps([SANDBOX_RESTORE_CAPABILITY, SANDBOX_ARCHIVE_CAPABILITY]),
        archive.revision,
        untrusted,
        KATA_RUNTIME,
    )
    for candidate in candidates:
        # Restoring creates the group's container there, which needs the same
        # admission a new group does.
        may_create, may_claim = await host_compute_admission(conn, candidate["host"])
        if may_create and may_claim:
            return None
    return "no_restore_target"


#: Groups still held only by a Host: not yet handed over, lost or deleted.
UNMOVED_GROUP_SQL = f"""{UNDELETED_GROUP_SQL} AND g.state NOT IN ('cold', 'restoring', 'lost')"""

#: An unmoved group a leaving Host can still move: its checkpoint covers it
#: now, or it has not stopped yet and its stop will record a checkpoint of an
#: image another Host can pull. Nothing else ever moves: no checkpoint is
#: taken of a group already stopped, so one whose checkpoint failed, a dirty
#: stop, or a group on a local image stays with this Host. Nor is one taken of
#: a group whose container was never created, as when its first turn gave up
#: before Docker made one: it has nothing to stop until a turn of its own
#: creates the container, and its Host claims none while it moves out.
MOVABLE_GROUP_SQL = f"""({UNMOVED_GROUP_SQL} AND (
    {COVERED_GROUP_SQL}
    OR (
        g.state IN ('provisioning', 'starting', 'running', 'stopping')
        AND g.sandbox_image_digest ~ '{PULLABLE_IMAGE_PATTERN}'
        AND (
            g.container_id IS NOT NULL
            OR EXISTS (
                SELECT 1 FROM agent_thread_sandbox_memberships creating
                WHERE creating.sandbox_id = g.id AND creating.active_attempt_id IS NOT NULL
            )
        )
    )
))"""

#: An unmoved group that can never move: it stays bound to its Host.
UNMOVABLE_GROUP_SQL = f"""({UNMOVED_GROUP_SQL} AND NOT {MOVABLE_GROUP_SQL})"""
