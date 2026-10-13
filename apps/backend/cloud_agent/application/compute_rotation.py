"""Move a Host to a replacement node before its cloud lease ends.

The whole Host moves: its name, its sandbox namespace and its Docker and
containerd state. A replacement node is allocated, registers its host key,
installs the recipe and copies the Host's state while the old node still
serves. Claims then pause, the old node stops, the last changes are copied,
the Host name is handed over and the Host starts on the new node with a fresh
enrollment. Until the new node is verified, every failure rolls back to the
old node, whose state is never touched before that point.

The replacement's own acquisition and copy run as that allocation's ``acquire``
operation, so the old node's observations keep flowing while it prepares. The
``rotate`` operation of the old allocation orchestrates everything else; a
replacement that failed is released by a ``discard`` operation, which needs no
retained-state inventory because the node never ran its Host.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING, Any

from pydantic import SecretStr

from cloud_agent.application.host_commands import HostCommands
from cloud_agent.domain.compute import ComputeRefusal, discard_request_id, fingerprint
from cloud_agent.logging_utils import get_logger
from cloud_agent.operational_health import deliver_event
from cloud_agent.ports.external.compute_provider import NodeObservation, ProviderError
from cloud_agent.ports.external.host_bootstrap import (
    BootstrapIntent,
    DeploymentProfile,
    default_namespace,
)
from cloud_agent.sandbox_manager_client import SandboxManagerError
from cloud_agent.sandbox_manager_routing import sandbox_manager_for_host
from cloud_agent.schemas_compute import RenewalPolicy, RetireComputeRequest

if TYPE_CHECKING:
    from cloud_agent.application.compute_reconcile import ComputeReconciler

logger = get_logger(__name__)

# A replacement that is not ready to take over within this time is abandoned.
_CANDIDATE_TIMEOUT = timedelta(hours=2)
# A pre-copy older than this is refreshed once claims pause, keeping the final
# copy (and so the pause) short after a long wait for an idle moment.
_PRECOPY_FRESHNESS = timedelta(minutes=15)
# Expected duration of stop, final copy, handover and start, plus a margin.
_SWITCH_ALLOWANCE = timedelta(minutes=10)
_DRAIN_MARGIN = timedelta(minutes=30)
# Alert urgently if turns still run this close to expiry. Never kill an
# active native turn as a migration shortcut: that can lose its session.
_LATE_DRAIN = timedelta(minutes=20)
_VERIFY_TIMEOUT = timedelta(minutes=10)
_CLOCK_TOLERANCE = timedelta(seconds=2)
_FINAL_REMINDER = timedelta(hours=1)
_SYSTEMIC_REPEATS = 3
_DEFAULT_TURN_LIMIT = timedelta(hours=1)
_ACTIVE = {"pending", "running", "waiting", "blocked", "unknown"}

_CREDENTIAL_CODES = {
    "credential_invalid",
    "credential_unavailable",
    "permission_denied",
    "secret_store_unavailable",
}
_CONFIGURATION_CODES = {
    "deployment_profile_invalid",
    "invalid_configuration",
    "registration_unavailable",
    "request_rejected",
    "unsupported_capability",
    "unsupported_node_count",
}
# Failures no other node type can fix: stop at the first one.
_STOPPING_CLASSES = {"configuration", "credentials"}


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _moment(value: Any) -> datetime | None:
    if value is None:
        return None
    return value if isinstance(value, datetime) else datetime.fromisoformat(str(value))


def classify_candidate_failure(
    operation: dict[str, Any], candidate: dict[str, Any]
) -> tuple[str, str]:
    """Sort a failed replacement by what can be seen: (failure class, code).

    The provider gives no reason for a failed experiment. One that never got
    nodes most likely found no free capacity; one that failed after it got
    nodes, or never became usable, failed during setup.
    """
    reasons = (operation.get("result") or {}).get("blocked_reasons") or []
    code = str(reasons[0].get("code")) if reasons else str(operation.get("state") or "missing")
    if code in _CREDENTIAL_CODES:
        return "credentials", code
    if code in _CONFIGURATION_CODES:
        return "configuration", code
    if code in {"resource_failed", "capacity_unavailable"} and not candidate.get("nodes"):
        return "no_capacity", code
    return "setup_failed", code


def next_node_type(
    policy: RenewalPolicy, attempts: list[dict[str, Any]]
) -> tuple[str | None, str | None]:
    """Return the next node type to try, or the reason no further type is tried.

    Configuration and credential problems stop at once. The same failure on
    the last three types is treated as systemic rather than tried further.
    """
    for attempt in attempts:
        if attempt["class"] in _STOPPING_CLASSES:
            return None, attempt["class"]
    recent = attempts[-_SYSTEMIC_REPEATS:]
    if len(recent) == _SYSTEMIC_REPEATS and len({a["class"] for a in recent}) == 1:
        return None, "systemic_" + recent[0]["class"]
    tried = {attempt["node_type"] for attempt in attempts}
    for node_type in policy.node_types:
        if node_type not in tried:
            return node_type, None
    return None, "candidates_exhausted"


def candidate_configuration(
    source: dict[str, Any], policy: RenewalPolicy, node_type: str
) -> dict[str, Any]:
    """Derive a replacement's provider request from the node it replaces."""
    configuration = {
        key: value for key, value in source.items() if key not in {"external_id", "name"}
    }
    configuration["bindings"] = {
        **(source.get("bindings") or {}),
        policy.node_type_binding: node_type,
    }
    return configuration


class ComputeRotation:
    """The rotate and discard workflows, executed by the compute reconciler."""

    def __init__(self, reconciler: ComputeReconciler) -> None:
        self.reconciler = reconciler
        self.repository = reconciler.repository
        self.host_pool = reconciler.host_pool
        self.bootstrap = reconciler.bootstrap
        self.commands = reconciler.commands
        self._reminded: set[str] = set()

    # ---- scheduling ------------------------------------------------------

    @staticmethod
    def replaces_renewal(row: dict[str, Any], policy: RenewalPolicy, now: datetime) -> bool:
        """Whether a node is replaced instead of extended: it reached its maximum age."""
        created = _moment(row.get("created_at"))
        return (
            policy.rotation_enabled
            and not row.get("autoscaling_owned")
            and created is not None
            and (now - created).total_seconds() >= policy.max_node_age_seconds
        )

    def due(
        self,
        row: dict[str, Any],
        policy: RenewalPolicy,
        operations: list[dict[str, Any]],
        now: datetime,
        expires: datetime,
    ) -> str | None:
        """Return why this allocation's Host must move now, if it must.

        Called only inside the renewal window. A node at its maximum age moves
        instead of being extended; otherwise a refused extension starts a move.
        """
        if (
            not policy.rotation_enabled
            or row.get("autoscaling_owned")
            or row.get("mode") != "provision"
            or row.get("management_mode") != "manage"
            or len(row.get("hosts") or []) != 1
            or len(row.get("nodes") or []) != 1
            or any(op["kind"] == "rotate" and op["state"] != "cancelled" for op in operations)
        ):
            return None
        if self.replaces_renewal(row, policy, now):
            return "max_age"
        prefix = f"renew:{row['id']}:{int(expires.timestamp())}:"
        renewals = [op for op in operations if str(op.get("request_id", "")).startswith(prefix)]
        if any(op["state"] in {"blocked", "failed", "cancelled"} for op in renewals):
            return "renewal_refused"
        unconfirmed = renewals and all(op["state"] != "succeeded" for op in renewals)
        if unconfirmed and expires - now <= timedelta(seconds=policy.renew_before_seconds / 2):
            return "renewal_unconfirmed"
        return None

    async def start(self, row: dict[str, Any], trigger: str, expires: datetime) -> None:
        """Queue one automatic move for this lease; duplicates are refused by the store."""
        parameters = {"allocation_id": row["id"], "trigger": trigger}
        try:
            await self.repository.create_operation(
                request_id=f"rotate:{row['id']}:{int(expires.timestamp())}",
                fingerprint=fingerprint("rotate", parameters),
                kind="rotate",
                allocation_id=row["id"],
                parameters=parameters,
                requested_by="compute-rotation",
            )
        except ValueError as exc:
            logger.info(
                "compute_rotation_not_started",
                extra={"allocation_id": row["id"], "reason": str(exc)},
            )
            return
        logger.warning(
            "compute_rotation_started", extra={"allocation_id": row["id"], "trigger": trigger}
        )

    async def remind(
        self,
        row: dict[str, Any],
        operations: list[dict[str, Any]],
        now: datetime,
        expires: datetime,
    ) -> None:
        """Alert once more when a move is still unfinished an hour before expiry."""
        if expires - now > _FINAL_REMINDER:
            return
        for operation in operations:
            if (
                operation["kind"] == "rotate"
                and operation["state"] in _ACTIVE
                and operation["id"] not in self._reminded
            ):
                self._reminded.add(operation["id"])
                hosts = row.get("hosts") or []
                await self._notify(
                    "compute_rotation_unfinished",
                    host=hosts[0]["host"] if hosts else row["name"],
                    reason=str(operation.get("step") or operation["state"]),
                )

    # ---- the rotate operation -------------------------------------------

    async def rotate(self, operation: dict[str, Any], allocation: dict[str, Any]) -> None:
        """Resume the move of this allocation's Host from its last checkpoint."""
        if allocation.get("autoscaling_owned"):
            await self._block(
                operation,
                None,
                "autoscaling_managed",
                "CloudLab autoscaling does not support node replacement.",
            )
            return
        result = operation.setdefault("result", {})
        if result.get("rollback"):
            await self._roll_back(operation, allocation)
            return
        if result.get("verified"):
            await self._clean_up(operation, allocation)
            return
        if result.get("rebound"):
            await self._take_over(operation, allocation)
            return
        slot = result.get("slot") or await self._begin(operation, allocation)
        if slot is None:
            return
        if not result.get("source_stopped"):
            # Node setup can be slow. Start draining by the source deadline
            # even while the replacement's acquisition is still in progress.
            expires = _moment(allocation.get("expires_at"))
            if (
                not operation["result"].get("hold_set")
                and expires is not None
                and _now() >= await self._drain_deadline(slot["host"], expires)
            ):
                await self._pause_claims(operation, allocation, slot)
            candidate = await self._prepared_candidate(operation, allocation, slot)
            if candidate is None or not await self._drained(operation, allocation, slot):
                return
            try:
                await self._refresh_copy(operation, allocation, slot, candidate)
                # A failed SSH acknowledgement does not mean the node stayed
                # running. Recovery and cancellation must treat it as stopped
                # from the moment we send the stop, until it is restarted.
                await self.reconciler._checkpoint(
                    operation, "stopping_source", {"source_stopping": True}
                )
                await self.bootstrap.stop(*self._source(operation, allocation, slot))
            except ProviderError as exc:
                await self._start_rollback(operation, allocation, "switch_failed", exc.code)
                return
            await self.reconciler._checkpoint(
                operation,
                "copying_final",
                {"source_stopped": True, "stopped_at": _now().isoformat()},
            )
        try:
            await self._hand_over(operation, allocation, slot)
        except ProviderError as exc:
            await self._start_rollback(operation, allocation, "switch_failed", exc.code)
            return
        await self._take_over(operation, allocation)

    async def _begin(
        self, operation: dict[str, Any], allocation: dict[str, Any]
    ) -> dict[str, Any] | None:
        """Check this allocation can move and record the Host identity that moves."""
        entry = self.commands.registry.get(allocation["provider_id"])
        caps = entry.provider.capabilities()
        nodes, hosts = allocation.get("nodes") or [], allocation.get("hosts") or []
        if (
            allocation.get("mode") != "provision"
            or allocation.get("management_mode") != "manage"
            or not (caps.can_release and caps.can_extend and "provision" in caps.acquisition_modes)
        ):
            await self._block(operation, None, "rotation_unsupported", "This server cannot move.")
            return None
        if (allocation.get("configuration") or {}).get("host_key_source") != "registration":
            await self._block(
                operation,
                None,
                "rotation_needs_registration",
                "A replacement node can only be trusted when it registers its host keys.",
            )
            return None
        if len(nodes) != 1 or len(hosts) != 1 or hosts[0].get("compute_node_id") != nodes[0]["id"]:
            await self._block(
                operation,
                None,
                "rotation_unsupported",
                "Only a server running exactly one Host can move.",
            )
            return None
        node = nodes[0]
        slot = {
            "host": hosts[0]["host"],
            "namespace": allocation.get("slot_namespace") or default_namespace(node["id"]),
            "node_id": node["id"],
        }
        await self.reconciler._checkpoint(
            operation,
            "selecting_candidate",
            {"slot": slot, "attempts": [], "started_at": _now().isoformat()},
        )
        return slot

    # ---- the replacement node -------------------------------------------

    async def _prepared_candidate(
        self, operation: dict[str, Any], allocation: dict[str, Any], slot: dict[str, Any]
    ) -> dict[str, Any] | None:
        """Return the replacement once it is staged and copied, or None while waiting."""
        result = operation["result"]
        current = result.get("candidate")
        if current and current.get("discard_id"):
            discard = await self.repository.get_operation(current["discard_id"])
            if discard is None or discard["state"] in {"blocked", "failed", "cancelled", "unknown"}:
                await self._block(
                    operation,
                    slot,
                    "candidate_release_failed",
                    "A failed replacement node could not be released. Check its operation.",
                )
                return None
            if discard["state"] != "succeeded":
                await self.reconciler._finish(
                    operation,
                    "waiting",
                    "releasing_candidate",
                    "Releasing a replacement node that could not be used.",
                    retry=30,
                )
                return None
            await self.reconciler._checkpoint(operation, "selecting_candidate", {"candidate": None})
            current = None
        if not current:
            current = await self._open_candidate(operation, allocation, slot)
            if current is None:
                return None
        candidate = await self.reconciler._allocation(current["id"])
        acquire = await self.repository.get_operation(current["operation_id"])
        state = acquire["state"] if acquire else "missing"
        if state == "succeeded" and acquire is not None and acquire.get("step") == "staged":
            if candidate.get("observed_state") == "ready" and not candidate.get(
                "release_committed_at"
            ):
                return candidate
            failure = ("setup_failed", "candidate_lost")
        elif state in {"blocked", "failed", "cancelled", "missing", "succeeded"}:
            failure = classify_candidate_failure(acquire or {}, candidate)
        elif state == "unknown":
            await self._block(
                operation,
                slot,
                "candidate_acquisition_unknown",
                "The replacement node's creation could not be confirmed. "
                "Identify it with the provider before retrying.",
            )
            return None
        elif _now() - (_moment(current["created_at"]) or _now()) > _CANDIDATE_TIMEOUT:
            failure = ("setup_failed", "candidate_timeout")
        else:
            await self.reconciler._finish(
                operation,
                "waiting",
                "preparing_candidate",
                f"Preparing a replacement node ({current['node_type']}).",
                retry=30,
            )
            return None
        await self._fail_candidate(operation, slot, current, *failure)
        return None

    async def _open_candidate(
        self, operation: dict[str, Any], allocation: dict[str, Any], slot: dict[str, Any]
    ) -> dict[str, Any] | None:
        """Allocate the next replacement node type, or block when none should be tried."""
        result = operation["result"]
        policy = RenewalPolicy.model_validate(allocation.get("policy") or {})
        attempts = list(result.get("attempts") or [])
        request_id = f"rotate:{operation['id']}:{len(attempts) + 1}"
        # The allocation and its profile reservation commit before the parent
        # checkpoint. Recover that reservation before looking for a free one.
        existing = await self.repository.list_operations(request_id=request_id)
        if existing:
            acquire = existing[0]
            candidate = await self.reconciler._allocation(acquire["allocation_id"])
            current = {
                "id": candidate["id"],
                "operation_id": acquire["id"],
                "node_type": acquire["parameters"]["node_type"],
                "profile": candidate["deployment_profile"]["id"],
                "created_at": candidate["created_at"].isoformat(),
            }
            await self.reconciler._checkpoint(
                operation, "preparing_candidate", {"candidate": current}
            )
            return current
        # An administrator's retry of a blocked move starts the list afresh.
        node_type, stop = next_node_type(policy, attempts[result.get("attempt_floor", 0) :])
        if node_type is None:
            await self._block(
                operation,
                slot,
                stop or "candidates_exhausted",
                "No replacement node could be prepared: " + (stop or "").replace("_", " ") + ".",
            )
            return None
        profile = await self._free_profile(exclude=(allocation.get("deployment_profile") or {}))
        if profile is None:
            await self._block(
                operation,
                slot,
                "deployment_profile_unavailable",
                "Configure a spare deployment profile for the replacement node.",
            )
            return None
        entry = self.commands.registry.get(allocation["provider_id"])
        configuration = entry.provider.validate_config(
            "provision", candidate_configuration(allocation["configuration"], policy, node_type)
        )
        parameters = {
            "rotation_operation_id": operation["id"],
            "source_allocation_id": allocation["id"],
            "node_type": node_type,
        }
        try:
            acquire = await self.repository.create_rotation_candidate(
                source_allocation_id=allocation["id"],
                allocation={
                    "name": f"{allocation['name']} ({node_type})"[:120],
                    "provider_id": allocation["provider_id"],
                    "connection_id": allocation.get("connection_id"),
                    "ssh_connection_id": allocation.get("ssh_connection_id"),
                    "configuration": configuration,
                    "deployment_profile": profile.model_dump(mode="json"),
                    "policy": allocation.get("policy") or {},
                    "lease_managed": allocation.get("lease_managed", False),
                    "release_supported": allocation.get("release_supported", False),
                    "slot_host": slot["host"],
                    "slot_namespace": slot["namespace"],
                },
                request_id=request_id,
                fingerprint=fingerprint("acquire", parameters),
                parameters=parameters,
                **self.reconciler._fence(operation),
            )
        except ValueError as exc:
            if str(exc) != "deployment_profile_in_use":
                raise
            await self.reconciler._finish(
                operation,
                "waiting",
                "selecting_candidate",
                "Waiting for a free deployment profile.",
                retry=30,
            )
            return None
        current = {
            "id": acquire["allocation_id"],
            "operation_id": acquire["id"],
            "node_type": node_type,
            "profile": profile.id,
            "created_at": _now().isoformat(),
        }
        await self.reconciler._checkpoint(operation, "preparing_candidate", {"candidate": current})
        return current

    async def _free_profile(self, *, exclude: dict[str, Any]) -> DeploymentProfile | None:
        """Pick a deployment profile no live allocation holds; the store rechecks it."""
        occupied = {
            (row.get("deployment_profile") or {}).get("id")
            for row in await self.repository.list_allocations()
            if row.get("observed_state") not in {"terminated", "absent"}
        }
        for profile in (await self.commands.deployment_profiles()).values():
            if (
                profile.id not in occupied
                and profile.id != exclude.get("id")
                and profile.deployment_sha == exclude.get("deployment_sha")
                and profile.source_sha256 == exclude.get("source_sha256")
            ):
                if exclude.get("sandbox_storage_path") is not None:
                    # Storage belongs to the Host and survives a change of the
                    # tunnel/sandbox manager profile used by its replacement node.
                    return profile.model_copy(
                        update={"sandbox_storage_path": exclude["sandbox_storage_path"]}
                    )
                return profile
        return None

    async def _fail_candidate(
        self,
        operation: dict[str, Any],
        slot: dict[str, Any],
        current: dict[str, Any],
        failure_class: str,
        code: str,
    ) -> None:
        """Record one failed node type and queue the release of its node."""
        result = operation["result"]
        attempts = list(result.get("attempts") or [])
        if not any(attempt.get("candidate") == current["id"] for attempt in attempts):
            attempts.append(
                {
                    "candidate": current["id"],
                    "node_type": current["node_type"],
                    "class": failure_class,
                    "code": code,
                    "at": _now().isoformat(),
                }
            )
            await self.reconciler._checkpoint(
                operation, "releasing_candidate", {"attempts": attempts}
            )
            await self._alert(
                operation, "compute_rotation_attempt_failed", host=slot["host"], reason=code
            )
        parameters = {"allocation_id": current["id"]}
        discard = await self.repository.abandon_candidate(
            allocation_id=current["id"],
            request_id=discard_request_id(current["id"]),
            fingerprint=fingerprint("discard", parameters),
            **self.reconciler._fence(operation),
        )
        if discard is not None:
            await self.reconciler._checkpoint(
                operation,
                "releasing_candidate",
                {"candidate": {**current, "discard_id": discard["id"]}},
            )
        await self.reconciler._finish(
            operation,
            "waiting",
            "releasing_candidate",
            "Releasing a replacement node that could not be used.",
            retry=30,
        )

    async def stage_candidate(
        self,
        operation: dict[str, Any],
        allocation: dict[str, Any],
        node: NodeObservation,
        profile: DeploymentProfile,
        intent: BootstrapIntent,
    ) -> None:
        """Install the recipe on a replacement node and copy its predecessor's state.

        Runs as the replacement's own acquisition. Nothing starts or enrolls
        here: the Host keeps running on its current node during the copy.
        """
        result = operation.setdefault("result", {})
        if not result.get("staged"):
            await self.reconciler._checkpoint(operation, "staging")
            staged = await self.bootstrap.stage(node, profile, intent)
            if not staged.ready:
                await self.reconciler._bootstrap_blocked(operation, staged.blocked_reasons)
                return
            await self.reconciler._checkpoint(operation, "copying", {"staged": True})
        source = await self.reconciler._allocation(allocation["predecessor_id"])
        first = not result.get("copy_started")
        if first:
            await self.reconciler._checkpoint(operation, "copying", {"copy_started": True})
        copied = await self.bootstrap.transfer(
            self.reconciler._node(source["nodes"][0]),
            node,
            profile,
            intent,
            final=False,
            first=first,
        )
        await self.reconciler._finish(
            operation,
            "succeeded",
            "staged",
            "The replacement node is ready to take the Host over.",
            result={
                "precopied_at": _now().isoformat(),
                "precopy_bytes": copied.bytes_transferred,
                "precopy_seconds": copied.seconds,
                "precopy_streams": copied.streams,
                "external_pending": False,
            },
        )

    # ---- pausing and switching ------------------------------------------

    async def _turn_limit(self, host: str) -> timedelta:
        row = await self.host_pool.get_host(host=host) or {}
        for settings in (row.get("settings_applied"), row.get("settings_desired")):
            value = settings.get("agent_timeout_s") if isinstance(settings, dict) else None
            if isinstance(value, int | float) and value > 0:
                return timedelta(seconds=float(value))
        return _DEFAULT_TURN_LIMIT

    async def _drained(
        self,
        operation: dict[str, Any],
        allocation: dict[str, Any],
        slot: dict[str, Any],
    ) -> bool:
        """Pause claims at a quiet moment, or at the deadline, and wait for running turns.

        The deadline leaves a whole turn's time limit, the switch itself and a
        margin before the old node's lease ends.
        """
        result = operation["result"]
        host = slot["host"]
        now = _now()
        expires = _moment(allocation.get("expires_at"))
        deadline = await self._drain_deadline(host, expires) if expires is not None else now
        running = await self.repository.running_attempts(host=host)
        if not result.get("hold_set"):
            if running and now < deadline:
                await self.reconciler._finish(
                    operation,
                    "waiting",
                    "waiting_for_idle",
                    f"Waiting for a moment with no running turn (until {deadline:%H:%M} UTC).",
                    retry=15,
                )
                return False
            await self._pause_claims(operation, allocation, slot)
            running = await self.repository.running_attempts(host=host)
        if running:
            if expires is not None and expires - now <= _LATE_DRAIN:
                await self._alert(
                    operation,
                    "compute_rotation_drain_overdue",
                    host=host,
                    reason=f"{running}_running",
                )
            await self.reconciler._finish(
                operation,
                "waiting",
                "draining",
                f"New work is paused; waiting for {running} running turn(s) to finish.",
                retry=15,
            )
            return False
        return True

    async def _drain_deadline(self, host: str, expires: datetime) -> datetime:
        """Leave time for one whole running turn, the switch and a safety margin."""
        return expires - await self._turn_limit(host) - _SWITCH_ALLOWANCE - _DRAIN_MARGIN

    async def _pause_claims(
        self, operation: dict[str, Any], allocation: dict[str, Any], slot: dict[str, Any]
    ) -> None:
        """Pause new work even when a replacement is not ready by the drain deadline."""
        now = _now()
        expires = _moment(allocation.get("expires_at"))
        started = _moment(operation["result"].get("started_at")) or now
        if expires is not None and await self._drain_deadline(slot["host"], expires) <= started:
            await self._alert(
                operation,
                "compute_rotation_lead_time_short",
                host=slot["host"],
                reason="forced_drain",
            )
        await self.repository.set_rotation_hold(
            host=slot["host"],
            allocation_id=allocation["id"],
            source_id=operation["id"],
            scope="all_claims",
            **self.reconciler._fence(operation),
        )
        await self.reconciler._checkpoint(
            operation, "draining", {"hold_set": True, "hold_set_at": now.isoformat()}
        )

    async def _refresh_copy(
        self,
        operation: dict[str, Any],
        allocation: dict[str, Any],
        slot: dict[str, Any],
        candidate: dict[str, Any],
    ) -> None:
        """Copy again while paused if the staged copy is old, to keep the stop short."""
        result = operation["result"]
        acquire = await self.repository.get_operation(result["candidate"]["operation_id"]) or {}
        copied_at = _moment(
            result.get("refreshed_at") or (acquire.get("result") or {}).get("precopied_at")
        )
        if copied_at is not None and _now() - copied_at <= _PRECOPY_FRESHNESS:
            return
        await self.reconciler._checkpoint(operation, "refreshing_copy")
        source, _, _ = self._source(operation, allocation, slot)
        target, profile, intent = self._target(operation, candidate)
        await self.bootstrap.transfer(source, target, profile, intent, final=False)
        await self.reconciler._checkpoint(
            operation, "refreshing_copy", {"refreshed_at": _now().isoformat()}
        )

    async def _hand_over(
        self, operation: dict[str, Any], allocation: dict[str, Any], slot: dict[str, Any]
    ) -> None:
        """Copy the stopped Host's last changes and give its name to the replacement."""
        result = operation["result"]
        candidate = await self.reconciler._allocation(result["candidate"]["id"])
        target, profile, intent = self._target(operation, candidate)
        if not result.get("final_copied"):
            source, _, _ = self._source(operation, allocation, slot)
            copied = await self.bootstrap.transfer(source, target, profile, intent, final=True)
            await self.reconciler._checkpoint(
                operation,
                "handing_over",
                {
                    "final_copied": True,
                    "final_copy_seconds": copied.seconds,
                    "final_copy_bytes": copied.bytes_transferred,
                },
            )
        await self.repository.rebind_slot_host(
            host=slot["host"],
            from_node_id=slot["node_id"],
            to_node_id=candidate["nodes"][0]["id"],
            **self.reconciler._fence(operation),
        )
        await self.reconciler._checkpoint(operation, "starting_target", {"rebound": True})

    async def _take_over(self, operation: dict[str, Any], allocation: dict[str, Any]) -> None:
        """Start the Host on the replacement node and resume claims once it is verified."""
        result = operation["result"]
        slot = result["slot"]
        candidate = await self.reconciler._allocation(result["candidate"]["id"])
        target, profile, intent = self._target(operation, candidate)
        if not result.get("target_started"):
            token, _ = await HostCommands(self.host_pool).enroll_token(
                host=slot["host"],
                ttl_seconds=max(900, profile.timeout_seconds + 300),
                actor="compute-rotation",
            )
            issued = _now()
            await self.reconciler._checkpoint(
                operation, "starting_target", {"token_issued_at": issued.isoformat()}
            )
            try:
                deployed = await self.bootstrap.deploy(
                    target, profile, intent, SecretStr(token), prerequisites=False
                )
            except ProviderError as exc:
                await self._start_rollback(operation, allocation, "takeover_failed", exc.code)
                return
            if not deployed.ready:
                code = deployed.blocked_reasons[0] if deployed.blocked_reasons else "not_ready"
                await self._start_rollback(operation, allocation, "takeover_failed", code)
                return
            await self.reconciler._checkpoint(
                operation, "verifying_target", {"target_started": True}
            )
        issued = _moment(result.get("token_issued_at")) or _now()
        if not await self._serving(slot["host"], target, profile, intent, since=issued):
            if _now() - issued > _VERIFY_TIMEOUT:
                await self._start_rollback(operation, allocation, "takeover_failed", "not_verified")
                return
            await self.reconciler._finish(
                operation,
                "waiting",
                "verifying_target",
                "Waiting for the moved Host to come online.",
                retry=15,
            )
            return
        # Persist the point of no return before claims resume: the new node
        # may write state as soon as the hold is lifted. A restarted worker
        # must only finish cleanup from then on, never roll back to the old copy.
        await self.reconciler._checkpoint(
            operation, "cleaning_up", {"verified": True, "verified_at": _now().isoformat()}
        )
        await self._clean_up(operation, allocation)

    async def _serving(
        self,
        host: str,
        node: NodeObservation,
        profile: DeploymentProfile,
        intent: BootstrapIntent,
        *,
        since: datetime,
    ) -> bool:
        """Whether this node runs the Host: ready, enrolled since ``since``, sandbox manager reachable."""
        try:
            readiness = await self.bootstrap.readiness(node, profile, intent)
        except ProviderError:
            return False
        row = await self.host_pool.get_host(host=host)
        heartbeat = _moment((row or {}).get("last_heartbeat_at"))
        if (
            not readiness.ready
            or row is None
            or row.get("status") != "online"
            or not row.get("enrolled")
            or heartbeat is None
            # The heartbeat carries the database's clock, ``since`` this process's.
            or heartbeat < since - _CLOCK_TOLERANCE
        ):
            return False
        sandbox_manager = sandbox_manager_for_host(row)
        if sandbox_manager is None:
            return False
        try:
            await sandbox_manager.health()
        except SandboxManagerError:
            return False
        return True

    async def _clean_up(self, operation: dict[str, Any], allocation: dict[str, Any]) -> None:
        """Remove the transfer key and the old copy, then release the old node."""
        result = operation["result"]
        slot = result["slot"]
        candidate = await self.reconciler._allocation(result["candidate"]["id"])
        await self.repository.set_rotation_hold(
            host=slot["host"],
            allocation_id=candidate["id"],
            source_id=operation["id"],
            scope=None,
            **self.reconciler._fence(operation),
        )
        source, profile, intent = self._source(operation, allocation, slot)
        target, _, _ = self._target(operation, candidate)
        if not result.get("transfer_revoked"):
            try:
                await self.bootstrap.revoke_transfer(source, target)
            except ProviderError as exc:
                await self._defer_cleanup(operation, slot["host"], exc.code)
                return
            await self.reconciler._checkpoint(operation, "cleaning_up", {"transfer_revoked": True})
        released = "The old server is being released."
        if not result.get("retire_requested"):
            try:
                if not result.get("source_wiped"):
                    await self.bootstrap.wipe(source, profile, intent)
                    await self.reconciler._checkpoint(
                        operation, "cleaning_up", {"source_wiped": True}
                    )
                current = await self.reconciler._allocation(allocation["id"])
                request_id = f"retire:{operation['id']}"
                existing = await self.repository.list_operations(request_id=request_id)
                # A crash may lose the acknowledgement after retirement was
                # persisted. Replay its original revision, not an allocation
                # revision that changed while this worker was down.
                revision = (
                    existing[0]["parameters"]["expected_revision"]
                    if existing
                    else current["revision"]
                )
                await self.commands.retire(
                    allocation["id"],
                    RetireComputeRequest(
                        request_id=request_id,
                        expected_revision=revision,
                    ),
                    actor="compute-rotation",
                )
                await self.reconciler._checkpoint(
                    operation, "cleaning_up", {"retire_requested": True}
                )
            except (ProviderError, ComputeRefusal, ValueError) as exc:
                if isinstance(exc, ValueError) and str(exc) == "operation_lease_lost":
                    raise
                # The new Host is serving already; keep retrying the old copy's
                # cleanup without repeating a handover or claiming it succeeded.
                code = getattr(exc, "code", None) or str(exc)
                await self._defer_cleanup(operation, slot["host"], code)
                return
        await self.reconciler._finish(
            operation,
            "succeeded",
            "rotated",
            f"The Host moved to {candidate['name']}. {released}",
            result={"blocked_reasons": [], "external_pending": False},
        )
        logger.warning(
            "compute_rotation_completed",
            extra={"operation_id": operation["id"], "host": slot["host"]},
        )

    async def _defer_cleanup(self, operation: dict[str, Any], host: str, code: str) -> None:
        """Keep serving on the new node while cleanup retries from its checkpoint."""
        await self._alert(operation, "compute_rotation_cleanup_failed", host=host, reason=code)
        await self.reconciler._finish(
            operation,
            "waiting",
            "cleaning_up",
            "The Host is serving on its new node; retrying cleanup of the old node.",
            result={"blocked_reasons": [{"code": code, "message": "Cleanup needs a retry."}]},
            retry=30,
        )

    # ---- rolling back ----------------------------------------------------

    async def _start_rollback(
        self, operation: dict[str, Any], allocation: dict[str, Any], failure_class: str, code: str
    ) -> None:
        await self.reconciler._checkpoint(
            operation,
            "rolling_back",
            {"rollback": {"class": failure_class, "code": code, "at": _now().isoformat()}},
        )
        await self._alert(
            operation,
            "compute_rotation_attempt_failed",
            host=operation["result"]["slot"]["host"],
            reason=code,
        )
        await self._roll_back(operation, allocation)

    async def _roll_back(self, operation: dict[str, Any], allocation: dict[str, Any]) -> None:
        """Return the Host to its original node, whose state was never changed.

        Before the name moved, the original credential is still valid and the
        Host restarts with it. After it moved, the replacement may have
        enrolled, so the original node enrolls again with a fresh token.
        """
        result = operation["result"]
        rollback = dict(result["rollback"])
        slot = result["slot"]
        candidate = await self.reconciler._allocation(result["candidate"]["id"])
        source, source_profile, source_intent = self._source(operation, allocation, slot)
        if result.get("rebound"):
            target, profile, intent = self._target(operation, candidate)
            try:
                await self.bootstrap.stop(target, profile, intent)
            except ProviderError:
                await self._block(
                    operation,
                    slot,
                    "rotation_rollback_target_running",
                    "The replacement node could not be stopped. Claims remain paused until "
                    "it is stopped, so two copies of the Host cannot run together.",
                    critical=True,
                )
                return
            await self.repository.rebind_slot_host(
                host=slot["host"],
                from_node_id=candidate["nodes"][0]["id"],
                to_node_id=slot["node_id"],
                **self.reconciler._fence(operation),
            )
            rollback["reenroll"] = True
            await self.reconciler._checkpoint(
                operation, "rolling_back", {"rebound": False, "rollback": rollback}
            )
        if result.get("source_stopping") or result.get("source_stopped"):
            if not rollback.get("restart_completed"):
                token: SecretStr | None = None
                if rollback.get("reenroll"):
                    issued, _ = await HostCommands(self.host_pool).enroll_token(
                        host=slot["host"],
                        ttl_seconds=max(900, source_profile.timeout_seconds + 300),
                        actor="compute-rotation",
                    )
                    token = SecretStr(issued)
                rollback["restarted_at"] = _now().isoformat()
                await self.reconciler._checkpoint(operation, "rolling_back", {"rollback": rollback})
                deployed = await self.bootstrap.deploy(
                    source,
                    source_profile,
                    source_intent,
                    token,
                    prerequisites=False,
                    reenroll=token is not None,
                )
                if not deployed.ready:
                    await self._block(
                        operation,
                        slot,
                        "rotation_rollback_failed",
                        "The Host could not be restarted on its original node.",
                        critical=True,
                    )
                    return
                rollback["restart_completed"] = True
                await self.reconciler._checkpoint(operation, "rolling_back", {"rollback": rollback})
            restarted = _moment(rollback["restarted_at"]) or _now()
            if not await self._serving(
                slot["host"], source, source_profile, source_intent, since=restarted
            ):
                if _now() - restarted > _VERIFY_TIMEOUT:
                    await self._block(
                        operation,
                        slot,
                        "rotation_rollback_failed",
                        "The Host did not come back online on its original node.",
                        critical=True,
                    )
                    return
                await self.reconciler._finish(
                    operation,
                    "waiting",
                    "rolling_back",
                    "Waiting for the Host to come back on its original node.",
                    retry=15,
                )
                return
        await self.repository.set_rotation_hold(
            host=slot["host"],
            allocation_id=allocation["id"],
            source_id=operation["id"],
            scope=None,
            **self.reconciler._fence(operation),
        )
        await self._revoke_after_rollback(operation, source, candidate)
        await self.reconciler._checkpoint(
            operation,
            "releasing_candidate",
            {
                "rollback": None,
                "hold_set": False,
                "source_stopping": False,
                "source_stopped": False,
                "final_copied": False,
                "target_started": False,
                "refreshed_at": None,
            },
        )
        await self._fail_candidate(
            operation, slot, result["candidate"], rollback["class"], rollback["code"]
        )

    async def _revoke_after_rollback(
        self, operation: dict[str, Any], source: NodeObservation, candidate: dict[str, Any]
    ) -> None:
        """Withdraw the transfer key from a replacement that will not take over.

        Cleanup revokes it after a move succeeds; a move that rolls back used to
        leave it authorized, letting the source keep writing to the replacement
        until that node was released — and release can wait or fail. This runs
        once the original node serves again, so it never delays the restart.
        A failure is logged rather than retried: the node is being released,
        and releasing it removes the key with it.
        """
        if not candidate.get("nodes"):
            return
        target, _, _ = self._target(operation, candidate)
        try:
            await self.bootstrap.revoke_transfer(source, target)
        except ProviderError as exc:
            logger.warning(
                "compute_rotation_rollback_revoke_failed",
                extra={"operation_id": operation["id"], "reason": exc.code},
            )

    # ---- the discard operation ------------------------------------------

    async def discard(self, operation: dict[str, Any], allocation: dict[str, Any]) -> None:
        """Release a replacement node that never took its Host over."""
        if not allocation.get("release_committed_at"):
            committed = await self.repository.commit_candidate_release(
                allocation_id=allocation["id"], **self.reconciler._fence(operation)
            )
            if not committed["release_committed"]:
                await self.reconciler._finish(
                    operation,
                    "waiting",
                    "waiting_for_candidate",
                    "Waiting for the replacement node's own work to stop.",
                    retry=30,
                )
                return
            allocation = await self.reconciler._allocation(allocation["id"])
        if not allocation.get("external_id"):
            # The provider never created it, or definitely refused to.
            await self.repository.observe_allocation(
                allocation_id=allocation["id"],
                observation={
                    "observed_state": "absent",
                    "expiration_known": False,
                    "scope_complete": True,
                },
                **self.reconciler._fence(operation),
            )
            await self.reconciler._finish(
                operation,
                "succeeded",
                "released",
                "The replacement node was never created.",
                result={"external_pending": False, "blocked_reasons": []},
            )
            return
        # It never ran its Host, so its node scope needs no review: a failed
        # experiment has no nodes at all. Delete by its exact identity.
        provider = self.commands.registry.get(allocation["provider_id"]).provider
        ref = self.reconciler._ref(allocation)
        observation = await provider.inspect(ref)
        if observation.state != "terminated":
            await self.reconciler._checkpoint(
                operation, "releasing", {"release_requested": True, "external_pending": True}
            )
            receipt = await provider.release(ref, self.reconciler._intent(operation, allocation))
            observation = receipt.observation or await provider.inspect(ref)
        if observation.state != "terminated":
            await self.reconciler._finish(
                operation,
                "waiting",
                "confirming_release",
                "Waiting for the provider to confirm the replacement node ended.",
                result={"external_pending": True},
                retry=30,
            )
            return
        await self.reconciler._save_observation(operation, allocation, observation)
        await self.reconciler._finish(
            operation,
            "succeeded",
            "released",
            "The replacement node was released.",
            result={"external_pending": False, "blocked_reasons": []},
        )

    # ---- shared ----------------------------------------------------------

    def _source(
        self, operation: dict[str, Any], allocation: dict[str, Any], slot: dict[str, Any]
    ) -> tuple[NodeObservation, DeploymentProfile, BootstrapIntent]:
        node = next(n for n in allocation["nodes"] if n["id"] == slot["node_id"])
        return (
            self.reconciler._node(node),
            DeploymentProfile.model_validate(allocation["deployment_profile"]),
            self.reconciler._bootstrap_intent(operation, allocation, node),
        )

    def _target(
        self, operation: dict[str, Any], candidate: dict[str, Any]
    ) -> tuple[NodeObservation, DeploymentProfile, BootstrapIntent]:
        node = candidate["nodes"][0]
        return (
            self.reconciler._node(node),
            DeploymentProfile.model_validate(candidate["deployment_profile"]),
            self.reconciler._bootstrap_intent(operation, candidate, node),
        )

    async def _block(
        self,
        operation: dict[str, Any],
        slot: dict[str, Any] | None,
        code: str,
        message: str,
        *,
        critical: bool = False,
    ) -> None:
        """Stop for an administrator, first letting the original node serve again."""
        result = operation.get("result") or {}
        if (
            slot is not None
            and result.get("hold_set")
            and not result.get("source_stopping")
            and not result.get("source_stopped")
        ):
            await self.repository.set_rotation_hold(
                host=slot["host"],
                allocation_id=operation["allocation_id"],
                source_id=operation["id"],
                scope=None,
                **self.reconciler._fence(operation),
            )
            await self.reconciler._checkpoint(operation, operation["step"], {"hold_set": False})
        await self._alert(
            operation,
            "compute_rotation_failed" if critical else "compute_rotation_blocked",
            host=slot["host"] if slot else operation["allocation_id"],
            reason=code,
        )
        await self.reconciler._checkpoint(
            operation,
            operation["step"],
            {
                "attempt_floor": len(result.get("attempts") or []),
                # A retry may alert again.
                "alerts": [],
            },
        )
        await self.reconciler._finish(
            operation,
            "blocked",
            operation["step"],
            message,
            result={"blocked_reasons": [{"code": code, "message": message}]},
        )

    async def _alert(
        self, operation: dict[str, Any], event: str, *, host: str, reason: str
    ) -> None:
        """Alert once per operation and event; delivery failures never stop the work."""
        sent = list((operation.get("result") or {}).get("alerts") or [])
        if event in sent:
            return
        await self._notify(event, host=host, reason=reason)
        await self.reconciler._checkpoint(operation, operation["step"], {"alerts": [*sent, event]})

    @staticmethod
    async def _notify(event: str, *, host: str, reason: str) -> None:
        logger.warning(event, extra={"host": host, "reason": reason})
        try:
            await deliver_event(event, {"host": host, "reason": reason})
        except Exception as exc:
            logger.warning(
                "compute_alert_delivery_failed",
                extra={"event": event, "error_type": type(exc).__name__},
            )
