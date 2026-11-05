"""What an installation depends on, and the locks that order an owner's snapshots."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

from cloud_agent.environments.environments import EnvironmentEncryptionError
from cloud_agent.storage.repositories.environments._common import _cipher

if TYPE_CHECKING:
    from cloud_agent_shared.environment import EnvironmentVariable


def setup_fingerprint(setup_script: str | None, variables: list[EnvironmentVariable]) -> str:
    """HMAC of what an installation depends on: the script and setup-time values.

    Equal fingerprints install the same thing, so an installation snapshot made
    for one serves the other. Keyed, so a fingerprint written into a container
    tells its code nothing about the values.
    """
    material = json.dumps(
        {
            "v": 1,
            "setup_script": setup_script or "",
            "variables": sorted([v.name, v.value] for v in variables if v.phase != "runtime"),
        },
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8")
    return _cipher().mac(material)


def runtime_only(variables: list[EnvironmentVariable]) -> list[EnvironmentVariable]:
    """A member's personal globals, as a team environment uses them: at run time only.

    Installation is the team's alone, so it is the same for every member and
    their snapshots can be shared; a member's own values still reach the
    agent and the commands it runs. The team's definition of a name wins.
    """
    return [v.model_copy(update={"phase": "runtime"}) for v in variables if v.phase != "setup"]


def optional_setup_fingerprint(
    setup_script: str | None, variables: list[EnvironmentVariable]
) -> str | None:
    """The fingerprint, or ``None`` without setup or without the deployment's key.

    Snapshots need the key; tasks do not, so a deployment without it simply
    never snapshots.
    """
    if not setup_script:
        return None
    try:
        return setup_fingerprint(setup_script, variables)
    except EnvironmentEncryptionError:
        return None


async def lock_environment_snapshots(conn: Any, user_id: str) -> None:
    """Take the owner's snapshot lock, before any of their snapshot or environment-run rows.

    A write that locks more than one of an owner's installation snapshots,
    or a snapshot and its run, takes this first and holds it to the end of
    its transaction, so no two of them take those rows in different orders.
    A receipt takes the snapshot it makes ready and its run, then the older
    builds it supersedes and the snapshots the owner's quota evicts; a
    deletion takes an environment's snapshots in whatever order its one
    statement scans them, then their runs.
    """
    await conn.execute(
        "SELECT pg_advisory_xact_lock(hashtextextended('agent-environment-snapshots:' || $1, 0))",
        user_id,
    )


async def lock_team_environment_snapshots(conn: Any, environment_id: str) -> list[str]:
    """Take the snapshot lock of every member with a snapshot of a team environment.

    A team environment's snapshots belong to whichever members built them, so
    a write over several of them takes each of those owners' locks, in one
    order, before any of the rows. Returns the owners; a snapshot queued by
    anyone else afterwards is not the write's to touch.
    """
    owners = [
        row["user_id"]
        for row in await conn.fetch(
            "SELECT DISTINCT user_id FROM agent_environment_snapshots "
            "WHERE environment_id=$1 ORDER BY user_id",
            environment_id,
        )
    ]
    for owner in owners:
        await lock_environment_snapshots(conn, owner)
    return owners
