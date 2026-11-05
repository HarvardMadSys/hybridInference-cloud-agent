"""Agent runtime versions, their downloads and their upgrades."""

from __future__ import annotations

import json
import uuid
from datetime import UTC
from typing import TYPE_CHECKING

from cloud_agent.domain.host_pool import HOST_ONLINE
from cloud_agent.domain.runtimes import KIND_SELECTION, KIND_UPDATE
from cloud_agent.storage._sql.rows import load_json
from cloud_agent.storage._sql.runner_policy import download_of, download_targets
from cloud_agent.storage.repositories.host_pool._common import HostPoolBase
from cloud_agent_shared.runtime_catalog import downloadable_runtimes

if TYPE_CHECKING:
    from datetime import datetime
    from typing import Any


def _before(stamp: datetime) -> str:
    """``npm install --before`` for an instant: whole seconds, UTC."""
    return stamp.strftime("%Y-%m-%dT%H:%M:%SZ")


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
        held = download_of(earlier)
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


class RuntimesMixin(HostPoolBase):
    """Select, download and upgrade agent runtimes."""

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
        return download_targets(policy)

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
                       runtime_versions, runtime_versions_available, runtime_downloads,
                       config_reports
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
                "config_reports": load_json(row["config_reports"]) or {},
            }
            for row in rows
        ]
        return upgrades, selection, hosts
