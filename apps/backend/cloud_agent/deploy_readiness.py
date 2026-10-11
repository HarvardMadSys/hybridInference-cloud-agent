"""Deployment-time readiness for the shared-live sandbox protocol.

This is intentionally a command run from a one-off control-plane container,
not a public HTTP endpoint.  The answer needs both of the control plane's
trusted views: the host row in Postgres and the ability to dial the broker with
the pool credential.  A runner checking either from its own network would be
answering a different question.

The deploy runs this command *before* replacing the live control-plane
container. Host and control jobs start in parallel; this runtime check is the
ordering edge between them. A control plane cannot replace the current API
until every new-chat placement host has heartbeated at the exact target commit
and every online retained owner has proved its compatible broker path. There
is no alternate executor to enable or fall back to.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

import asyncpg

from cloud_agent.broker_routing import ENV_BROKER_TOKEN, probe_broker
from cloud_agent.host_pool import HOST_DRAINING, HOST_ONLINE
from cloud_agent_shared.shared_protocol import (
    NATIVE_FORK_AT_ANCHOR_CAPABILITY,
    SHARED_SANDBOX_CAPABILITY,
    shared_deployment_capability,
)

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from cloud_agent.ports import HostPoolRepository

    BrokerProbe = Callable[..., Awaitable[str | None]]

ENV_DATABASE_URL = "AGENT_DATABASE_URL"


@dataclass(frozen=True)
class SharedLiveReadiness:
    """One authoritative readiness verdict, safe to print in deploy logs."""

    ready: bool
    detail: str
    host: str | None = None


def _heartbeat_at(row: dict[str, Any]) -> datetime | None:
    value = row.get("last_heartbeat_at")
    if not isinstance(value, datetime):
        return None
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value


def _host_rejection(
    row: dict[str, Any],
    *,
    not_before: datetime,
    expected_deployment_sha: str,
    required_status: str = HOST_ONLINE,
    require_idle: bool = False,
    require_target_deployment: bool = True,
    require_native_fork: bool = True,
    require_two_slots: bool = True,
) -> str | None:
    """Return why a host cannot be the deployment barrier, or ``None``."""
    if row.get("status") != required_status:
        return f"status is {row.get('status') or 'unset'}"
    if require_idle:
        slots_used = row.get("slots_used")
        if not isinstance(slots_used, int) or isinstance(slots_used, bool) or slots_used != 0:
            return f"host is not idle (slots_used is {slots_used!r})"
    if not bool(row.get("enrolled")):
        return "host is not enrolled"
    capabilities = row.get("capabilities")
    if not isinstance(capabilities, list) or SHARED_SANDBOX_CAPABILITY not in capabilities:
        return f"host has not advertised {SHARED_SANDBOX_CAPABILITY}"
    if require_native_fork and NATIVE_FORK_AT_ANCHOR_CAPABILITY not in capabilities:
        return f"host has not advertised {NATIVE_FORK_AT_ANCHOR_CAPABILITY}"
    deployment_capability = shared_deployment_capability(expected_deployment_sha)
    if require_target_deployment and deployment_capability not in capabilities:
        return f"host has not completed target deployment {expected_deployment_sha}"
    slots = row.get("slots_total")
    if require_two_slots and (not isinstance(slots, int) or isinstance(slots, bool) or slots < 2):
        return "host has fewer than two declared slots"
    heartbeat_at = _heartbeat_at(row)
    if heartbeat_at is None:
        return "host has never heartbeated"
    if heartbeat_at < not_before:
        return "host has not heartbeated since this deployment check began"
    if not str(row.get("broker_url") or "").strip():
        return "host reported no control-plane-reachable broker URL"
    return None


async def shared_live_readiness(
    store: HostPoolRepository,
    *,
    not_before: datetime,
    broker_token: str,
    expected_deployment_sha: str,
    draining_host: str | None = None,
    broker_probe: BrokerProbe = probe_broker,
) -> SharedLiveReadiness:
    """Require a fresh capability heartbeat and an authoritative broker dial.

    ``not_before`` is load-bearing.  Merely finding a recent database row can
    accept the previous runner generation during a rolling deploy.  Requiring
    a heartbeat after this one-off container started proves the process that is
    live now can authenticate and write the capability the scheduler reads.
    """
    expected_deployment_sha = expected_deployment_sha.strip()
    if not expected_deployment_sha:
        return SharedLiveReadiness(False, "target deployment SHA is empty")
    if not broker_token.strip():
        return SharedLiveReadiness(
            False,
            f"control plane has no {ENV_BROKER_TOKEN}; broker operations cannot authenticate",
        )
    if draining_host is not None:
        draining_host = draining_host.strip()
        if not draining_host:
            return SharedLiveReadiness(False, "draining host name is empty")

    rows, active_host, _, retained_hosts = await store.deployment_runner_pool()
    rows_by_host = {str(row.get("host")): row for row in rows if row.get("host")}
    if draining_host is not None:
        if draining_host not in rows_by_host:
            return SharedLiveReadiness(False, f"draining host {draining_host!r} is not registered")
        if active_host is not None and active_host != draining_host:
            return SharedLiveReadiness(
                False,
                f"pinned host {active_host!r} does not match draining host {draining_host!r}",
            )
    if active_host is not None:
        placement_rows = [row for row in rows if row.get("host") == active_host]
        if not placement_rows:
            return SharedLiveReadiness(False, f"pinned host {active_host!r} is not registered")
    else:
        # With no pin, every online host advertising the current historical-
        # fork surface may claim one of the new jobs this rollout enables.
        # Proving only one of them would leave an older eligible host in the
        # scheduler pool. A v1-only host cannot materialize a new sandbox;
        # replica-local claim attestation admits it only for an existing group
        # it owns. The retained-owner probe below still covers that case.
        pool_rows = rows
        placement_rows = [
            row
            for row in pool_rows
            if row.get("status") == HOST_ONLINE
            and isinstance(row.get("capabilities"), list)
            and SHARED_SANDBOX_CAPABILITY in row["capabilities"]
            and NATIVE_FORK_AT_ANCHOR_CAPABILITY in row["capabilities"]
        ]
        if draining_host is not None and all(
            row.get("host") != draining_host for row in placement_rows
        ):
            # The all-claims deployment fence deliberately takes this host out
            # of the online scheduler pool. Its explicit name is the narrow
            # authority to include that one draining row in the barrier.
            placement_rows.append(rows_by_host[draining_host])
        if not placement_rows:
            reasons = [
                f"{row.get('host') or 'unknown'!s}: "
                f"{_host_rejection(row, not_before=not_before, expected_deployment_sha=expected_deployment_sha)}"
                for row in pool_rows
            ]
            detail = "; ".join(reasons) if reasons else "no host is registered"
            return SharedLiveReadiness(
                False,
                f"host pool is not shared-live ready: {detail}",
            )

    rejected: list[str] = []
    for row in placement_rows:
        host = str(row.get("host") or "unknown")
        is_draining_target = host == draining_host
        reason = _host_rejection(
            row,
            not_before=not_before,
            expected_deployment_sha=expected_deployment_sha,
            required_status=HOST_DRAINING if is_draining_target else HOST_ONLINE,
            require_idle=is_draining_target,
        )
        if reason is not None:
            rejected.append(f"{host}: {reason}")
    if rejected:
        scope = f"pinned host {active_host!r}" if active_host is not None else "host pool"
        return SharedLiveReadiness(
            False, f"{scope} is not shared-live ready: {'; '.join(rejected)}"
        )

    # A host switch does not move retained Docker writable layers. An online
    # previous owner may therefore keep serving follow-ups even though it is no
    # longer eligible to materialize a new chat. It need not carry this target
    # SHA or the v2 historical-fork capability (v1 retained containers may
    # serve ordinary follow-ups), but the new control plane must prove a fresh
    # compatible heartbeat and that it can still authenticate to that broker.
    probe_rows = list(placement_rows)
    probed_hosts = {str(row.get("host")) for row in probe_rows}
    for owner in sorted(retained_hosts):
        owner_row = rows_by_host.get(owner)
        if owner_row is None or owner_row.get("status") != HOST_ONLINE or owner in probed_hosts:
            continue
        reason = _host_rejection(
            owner_row,
            not_before=not_before,
            expected_deployment_sha=expected_deployment_sha,
            require_target_deployment=False,
            require_native_fork=False,
            require_two_slots=False,
        )
        if reason is not None:
            rejected.append(f"retained owner {owner}: {reason}")
            continue
        probe_rows.append(owner_row)
        probed_hosts.add(owner)
    if rejected:
        return SharedLiveReadiness(
            False,
            "new-chat placement or an online retained owner is not shared-live ready: "
            + "; ".join(rejected),
        )

    ready_hosts: list[str] = []
    for row in probe_rows:
        host = str(row.get("host") or "unknown")
        broker_url = str(row["broker_url"]).strip()
        broker_error = await broker_probe(host, broker_url, force=True)
        if broker_error is not None:
            rejected.append(f"{host}: control plane cannot use its broker: {broker_error}")
            continue
        ready_hosts.append(host)

    scope = (
        f"pinned host {active_host!r} and online retained owners"
        if active_host is not None
        else "host pool"
    )
    if rejected:
        return SharedLiveReadiness(
            False, f"{scope} is not shared-live ready: {'; '.join(rejected)}"
        )
    placement_host_names = [str(row["host"]) for row in placement_rows]

    # Broker probes are network awaits outside the database read. Re-read the
    # pool before declaring success so a runner that invalidated its marker,
    # went offline, or changed placement while those probes ran cannot satisfy
    # the deployment with a stale snapshot. Claim admission independently
    # enforces the same exact SHA; this closes the deploy-time availability gap.
    (
        current_rows,
        current_active_host,
        _,
        current_retained_hosts,
    ) = await store.deployment_runner_pool()
    current_by_host = {str(row.get("host")): row for row in current_rows if row.get("host")}
    if current_active_host != active_host or set(current_retained_hosts) != set(retained_hosts):
        return SharedLiveReadiness(
            False,
            "host placement or retained ownership changed while brokers were probed",
        )
    if active_host is not None:
        current_placement_names = {
            str(row["host"]) for row in current_rows if row.get("host") == active_host
        }
    else:
        current_placement_names = {
            str(row["host"])
            for row in current_rows
            if row.get("status") == HOST_ONLINE
            and isinstance(row.get("capabilities"), list)
            and SHARED_SANDBOX_CAPABILITY in row["capabilities"]
            and NATIVE_FORK_AT_ANCHOR_CAPABILITY in row["capabilities"]
        }
        if draining_host is not None and draining_host in current_by_host:
            current_placement_names.add(draining_host)
    if current_placement_names != set(placement_host_names):
        return SharedLiveReadiness(
            False,
            "new-chat placement changed while brokers were probed",
        )
    for row in probe_rows:
        host = str(row["host"])
        current = current_by_host.get(host)
        if current is None:
            return SharedLiveReadiness(
                False, f"host {host} disappeared while its broker was probed"
            )
        placement = host in current_placement_names
        reason = _host_rejection(
            current,
            not_before=not_before,
            expected_deployment_sha=expected_deployment_sha,
            required_status=HOST_DRAINING if host == draining_host else HOST_ONLINE,
            require_idle=host == draining_host,
            require_target_deployment=placement,
            require_native_fork=placement,
            require_two_slots=placement,
        )
        if reason is not None:
            return SharedLiveReadiness(
                False,
                f"host {host} changed while its broker was probed: {reason}",
            )
        if str(current.get("broker_url") or "").strip() != str(row.get("broker_url") or "").strip():
            return SharedLiveReadiness(
                False,
                f"host {host} changed broker URL while its previous broker was probed",
            )

    placement_hosts = ", ".join(placement_host_names)
    retained_owner_hosts = sorted(set(ready_hosts) - set(placement_host_names))
    retained_detail = (
        "; compatible retained owners also answered: " + ", ".join(retained_owner_hosts)
        if retained_owner_hosts
        else ""
    )
    return SharedLiveReadiness(
        True,
        f"new-chat placement hosts {placement_hosts} completed target deployment "
        f"{expected_deployment_sha}, sent fresh {SHARED_SANDBOX_CAPABILITY} and "
        f"{NATIVE_FORK_AT_ANCHOR_CAPABILITY} heartbeats, and their brokers answered"
        f"{retained_detail}",
        host=ready_hosts[0],
    )


async def wait_for_shared_live(
    *,
    timeout_seconds: float,
    interval_seconds: float,
    expected_deployment_sha: str,
    draining_host: str | None = None,
) -> bool:
    """Poll until a post-start heartbeat and broker probe both succeed."""
    dsn = (os.environ.get(ENV_DATABASE_URL) or "").strip()
    if not dsn:
        print(f"[deploy-readiness] ERROR: {ENV_DATABASE_URL} is not configured", file=sys.stderr)
        return False

    # Capture before opening the pool so a heartbeat concurrent with startup
    # counts.  The database and this container run on the control-plane host;
    # an aware UTC timestamp avoids local-time ambiguity in the comparison.
    not_before = datetime.now(UTC)
    deadline = time.monotonic() + timeout_seconds
    from cloud_agent.storage.assembly import Storage

    pool = await asyncpg.create_pool(dsn, min_size=1, max_size=1)
    # The one assembly, so this entry point names no implementation of its
    # own: it needs the host pool and takes it from the same place the
    # service does.
    storage = Storage(pool)
    store = storage.host_pool
    last_detail = ""
    try:
        from cloud_agent.settings import initialize_settings, setting_env

        # deploy.sh runs this command with the control-plane service's complete
        # legacy environment and mounts, before replacing the live API. It must
        # therefore bootstrap missing settings itself: waiting for the new API
        # to seed them would deadlock activation. Saved values remain
        # authoritative; the MCP listener still never seeds its partial env.
        await initialize_settings(storage.settings, seed=True)
        broker_token = (setting_env(ENV_BROKER_TOKEN) or "").strip()
        if not broker_token:
            print(
                "[deploy-readiness] ERROR: set the workspace broker credential in Admin Settings",
                file=sys.stderr,
            )
            return False
        while True:
            try:
                readiness_kwargs: dict[str, Any] = {}
                if draining_host is not None:
                    readiness_kwargs["draining_host"] = draining_host
                verdict = await shared_live_readiness(
                    store,
                    not_before=not_before,
                    broker_token=broker_token,
                    expected_deployment_sha=expected_deployment_sha,
                    **readiness_kwargs,
                )
            except Exception as exc:  # deployment polling must tolerate one transient read
                verdict = SharedLiveReadiness(
                    False,
                    f"readiness query failed: {type(exc).__name__}: {exc}",
                )
            if verdict.ready:
                print(f"[deploy-readiness] ready: {verdict.detail}", flush=True)
                return True
            if verdict.detail != last_detail:
                print(f"[deploy-readiness] waiting: {verdict.detail}", flush=True)
                last_detail = verdict.detail
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                print(
                    f"[deploy-readiness] ERROR: timed out after {timeout_seconds:g}s: "
                    f"{verdict.detail}",
                    file=sys.stderr,
                )
                return False
            await asyncio.sleep(min(interval_seconds, remaining))
    finally:
        await pool.close()


def main(argv: list[str] | None = None) -> int:
    """Run the deployment readiness waiter and return its process status."""
    parser = argparse.ArgumentParser(description="wait for shared-live host readiness")
    parser.add_argument("--timeout-seconds", type=float, default=900.0)
    parser.add_argument("--interval-seconds", type=float, default=3.0)
    parser.add_argument(
        "--expected-deployment-sha",
        required=True,
        help="exact host deployment commit that must have completed its local attestation",
    )
    parser.add_argument(
        "--draining-host",
        help=(
            "explicit host that must remain draining with zero occupied slots while "
            "readiness is checked"
        ),
    )
    args = parser.parse_args(argv)
    if args.timeout_seconds <= 0 or args.interval_seconds <= 0:
        parser.error("timeouts must be positive")
    return (
        0
        if asyncio.run(
            wait_for_shared_live(
                timeout_seconds=args.timeout_seconds,
                interval_seconds=args.interval_seconds,
                expected_deployment_sha=args.expected_deployment_sha,
                draining_host=args.draining_host,
            )
        )
        else 1
    )


if __name__ == "__main__":  # pragma: no cover - exercised by deployment
    raise SystemExit(main())
