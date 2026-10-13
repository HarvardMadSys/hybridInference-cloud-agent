"""The Admin-facing lifecycle of an operation-measurement experiment.

This module is where "an administrator clicked Start" becomes "three processes
are collecting", and it exists because that sentence has a lot of ways to be
half-true. Four rules shape it.

**The server owns time.** ``started_at`` and ``expires_at`` are computed here,
never sent by the browser, and every read re-decides "is this still running"
from the authoritative clock rather than from a stored boolean. A page that is
closed, a sweep that never ran and a process that restarted all leave the
timed experiment ending at the instant it was asked to end. Continuous runs
have no expiry and stay enabled until a Stop.

**The card reports the configuration, not a consensus.** ``on`` means the
deployment's stored configuration names an experiment that has not expired; it
does not claim that every process has applied it yet. Sync is allowed to lag and
the page says so, which is the honest version of a distributed switch -- the
alternative was a status derived from acknowledgements nobody needed. A
deployment whose control row does not exist is ``unavailable`` rather than a
comforting ``off``, and one that is not under Admin control is ``unmanaged``.

**A run is written before it is pointed at.** The manifest row is inserted and
the control row is advanced in one transaction, so there is no instant at which
the deployment is collecting for an experiment nobody can read, or holding a
run nobody is collecting for.

**The budget is a per-producer allowance and says so.** The design explicitly
declines to add a fleet-wide cap in this slice: a local counter presented as a
global limit would be a quota nobody enforces. Every answer that mentions a
budget names its scope. Continuous runs have no cumulative sample limit.
"""

from __future__ import annotations

import hashlib
import json
import re
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Any

from cloud_agent.domain.profiling_control import (
    CONTROL_REFUSED_INVALID,
    ControlCommandResult,
)
from cloud_agent.logging_utils import get_logger
from cloud_agent.storage.repositories.profiling_control import TERMINAL_LIFECYCLES
from cloud_agent_shared.operation_profiling import profile_mode

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

    from cloud_agent.ports.profiling_control import ProfilingControl

logger = get_logger(__name__)

#: The root operation names an administrator may select. These are the
#: contract's own names, and the Admin request maps its three checkboxes onto
#: them rather than inventing a friendlier vocabulary the collector would then
#: have to translate back.
OPERATION_STARTUP = "attempt.startup"
OPERATION_TERMINAL_OPEN = "terminal.open"
OPERATION_FILES_LIST = "files.list"
OPERATION_FILES_OPEN = "files.open"

#: What the three checkboxes select. "File access" is two root operations
#: because a directory listing and a file read are different waits, and a
#: filter that could only name one of them would silently drop half the
#: requests it was asked to measure.
SELECTABLE_OPERATIONS: dict[str, tuple[str, ...]] = {
    "sandbox_startup": (OPERATION_STARTUP,),
    "terminal_open": (OPERATION_TERMINAL_OPEN,),
    "file_access": (OPERATION_FILES_LIST, OPERATION_FILES_OPEN),
}

#: Every operation a request may name, flattened.
KNOWN_OPERATIONS = frozenset(
    operation for group in SELECTABLE_OPERATIONS.values() for operation in group
)

#: What a request body names, so a typo is refused rather than silently
#: selecting nothing.
SELECTABLE_GROUPS = frozenset(SELECTABLE_OPERATIONS)

#: Duration bounds. The UI offers four choices; the bounds are enforced here
#: because a request that did not come from the UI is still a request.
MIN_DURATION_SECONDS = 60
MAX_DURATION_SECONDS = 30 * 60

#: Per-root-producer allowance for a run this service created. It is a ceiling
#: on how many root operations *one process* will select, chosen so an
#: experiment left running cannot produce an unbounded table; the API states
#: that scope in every answer.
DEFAULT_MAX_OPERATIONS = 100

#: The deployment-level hard ceiling on that per-producer number, so a future
#: caller cannot ask for an unbounded experiment through the same field.
MAX_MAX_OPERATIONS = 1000

#: The statuses the Admin page renders. ``unavailable`` and ``unmanaged`` are
#: deliberately distinct from ``off``: a deployment that cannot be controlled
#: must not look like one that chose not to collect.
#:
#: There is no ``starting``, ``collecting``, ``partially_active`` or
#: ``stopping``. Those existed to describe how far a configuration had
#: propagated, which only a registry of acknowledgements could answer -- and
#: nothing in this feature asks that question.
STATUS_OFF = "off"
STATUS_ON = "on"
STATUS_UNAVAILABLE = "unavailable"
STATUS_UNMANAGED = "unmanaged"

#: Run ids are generated here and appear in Admin URL path segments, so they
#: are restricted to characters that survive URL decoding.
_RUN_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]*$")

#: Longest accepted ids, matching the collector's own bounds.
MAX_RUN_ID_CHARS = 128
MAX_REQUEST_ID_CHARS = 128


@dataclass(frozen=True)
class StartRequest:
    """One validated Start, as the service needs it.

    Built by the route from the request body so that every refusal that depends
    on the *content* of a request happens before a transaction is opened.
    """

    request_id: str
    expected_revision: int
    operations: tuple[str, ...]
    host: str | None
    duration_seconds: int | None
    actor_id: str | None

    def fingerprint(self) -> str:
        """Return the content fingerprint that makes a retry idempotent.

        Over the parts that decide *what was asked for*, canonically encoded so
        two spellings of one request hash alike. ``request_id`` is excluded
        because it is the key rather than the content, and ``actor_id`` because
        the same administrator retrying from a second tab is the same command.
        """
        body = {
            "action": "start",
            "operations": sorted(self.operations),
            "host": self.host or "",
            "duration_seconds": self.duration_seconds,
        }
        return _fingerprint(body)


@dataclass(frozen=True)
class StopRequest:
    """One validated Stop."""

    request_id: str
    run_id: str
    actor_id: str | None
    reason: str = "stopped"

    def fingerprint(self) -> str:
        """Return the content fingerprint that makes a repeated Stop idempotent."""
        return _fingerprint({"action": "stop", "run_id": self.run_id, "reason": self.reason})


@dataclass(frozen=True)
class Refusal:
    """A Start or Stop this service declined before opening a transaction."""

    reason: str
    detail: str


class ProfilingControlService:
    """Start, stop and describe one deployment's measurement experiment."""

    def __init__(
        self,
        *,
        control: ProfilingControl,
        deployment: str,
        host_pool: Any = None,
    ) -> None:
        """Bind the service to one deployment's namespace.

        ``deployment`` comes from trusted configuration and never from a
        request: a caller that could name its namespace could read or stop
        another deployment's experiment. ``host_pool`` is used only to validate
        a target and to enumerate the hosts a scope expects to hear from.
        """
        self._control = control
        self._deployment = deployment
        self._host_pool = host_pool

    @property
    def deployment(self) -> str:
        """Return the namespace this service commands."""
        return self._deployment

    # -- validation --------------------------------------------------------

    def validate_start(
        self,
        *,
        request_id: str,
        expected_revision: int,
        operations: Sequence[str],
        host: str | None,
        duration_seconds: int | None,
        actor_id: str | None,
        known_hosts: Sequence[str] | None,
    ) -> StartRequest | Refusal:
        """Return a Start this service will attempt, or why it will not.

        Every check here is about the *request*: shape, bounds, and whether the
        operations and the host exist. Whether the deployment is in a state to
        accept it is a database question and is answered under the control row's
        lock, because only there is the answer not already stale.
        """
        wanted = (request_id or "").strip()
        if not wanted or len(wanted) > MAX_REQUEST_ID_CHARS:
            return Refusal(CONTROL_REFUSED_INVALID, "request_id is required and bounded")
        if expected_revision < 0:
            return Refusal(CONTROL_REFUSED_INVALID, "expected_revision must not be negative")
        if (
            duration_seconds is not None
            and not MIN_DURATION_SECONDS <= duration_seconds <= MAX_DURATION_SECONDS
        ):
            return Refusal(
                CONTROL_REFUSED_INVALID,
                f"duration_seconds must be between {MIN_DURATION_SECONDS} and "
                f"{MAX_DURATION_SECONDS}",
            )
        selected = tuple(str(item).strip() for item in operations)
        # A request that mixed duplicates would create a manifest whose
        # operation list disagrees with itself, and the run identity is that
        # list.
        selected = tuple(dict.fromkeys(item for item in selected if item))
        if not selected:
            return Refusal(CONTROL_REFUSED_INVALID, "select at least one operation")
        unknown = sorted(item for item in selected if item not in KNOWN_OPERATIONS)
        if unknown:
            return Refusal(CONTROL_REFUSED_INVALID, f"unknown operation: {unknown[0]}")
        target = (host or "").strip() or None
        if target is not None and known_hosts is not None and target not in set(known_hosts):
            # A target nobody is enrolled as would be an experiment that selects
            # nothing: refused here, where the caller still has the request in
            # front of it, rather than reported later as "no samples".
            return Refusal(CONTROL_REFUSED_INVALID, f"unknown host: {target}")
        return StartRequest(
            request_id=wanted,
            expected_revision=int(expected_revision),
            operations=selected,
            host=target,
            duration_seconds=duration_seconds,
            actor_id=actor_id,
        )

    def validate_stop(
        self, *, request_id: str, run_id: str, actor_id: str | None, reason: str = "stopped"
    ) -> StopRequest | Refusal:
        """Return a Stop this service will attempt, or why it will not."""
        wanted = (request_id or "").strip()
        if not wanted or len(wanted) > MAX_REQUEST_ID_CHARS:
            return Refusal(CONTROL_REFUSED_INVALID, "request_id is required and bounded")
        target = (run_id or "").strip()
        if not target or len(target) > MAX_RUN_ID_CHARS or not _RUN_ID_PATTERN.fullmatch(target):
            return Refusal(CONTROL_REFUSED_INVALID, "run_id is not a valid experiment id")
        return StopRequest(
            request_id=wanted, run_id=target, actor_id=actor_id, reason=reason or "stopped"
        )

    # -- commands ----------------------------------------------------------

    def build_run(
        self, request: StartRequest, *, now: datetime
    ) -> tuple[dict[str, Any], datetime | None]:
        """Return the manifest a Start creates, and the instant it expires.

        The run id and the expiry are minted here from the server's own clock.
        The manifest is the *immutable* description of what was asked for: the
        operations, the target, the duration expressed as an absolute instant,
        and the per-producer allowance. Lifecycle and provenance are separate
        columns, so a Stop never rewrites anything a producer validated against.
        """
        expires_at = (
            now + timedelta(seconds=request.duration_seconds)
            if request.duration_seconds is not None
            else None
        )
        run_id = f"admin-{now.strftime('%Y%m%dT%H%M%SZ')}-{uuid.uuid4().hex[:8]}"
        manifest = {
            "run_id": run_id,
            "operations": list(request.operations),
            "target": {"host": request.host} if request.host else {},
            "expires_at": expires_at,
            "config_version": 1,
            "max_operations": DEFAULT_MAX_OPERATIONS if expires_at is not None else 0,
            **({"continuous": True} if expires_at is None else {}),
            # The admission identity of the process that created this run. The
            # per-producer budgets of the processes that actually collect are
            # merged into the catalogue row as they report.
            "admission_instances": [],
        }
        return manifest, expires_at

    async def start(self, request: StartRequest, *, now: datetime) -> ControlCommandResult:
        """Create and activate one experiment."""
        manifest, _expires_at = self.build_run(request, now=now)
        return await self._control.start_run(
            deployment=self._deployment,
            request_id=request.request_id,
            fingerprint=request.fingerprint(),
            expected_revision=request.expected_revision,
            run=manifest,
            actor_id=request.actor_id,
            now=now,
        )

    async def stop(self, request: StopRequest, *, now: datetime) -> ControlCommandResult:
        """Stop one experiment, if it is the one in force."""
        return await self._control.stop_run(
            deployment=self._deployment,
            request_id=request.request_id,
            fingerprint=request.fingerprint(),
            run_id=request.run_id,
            actor_id=request.actor_id,
            now=now,
            reason=request.reason,
        )

    async def sweep(self, *, now: datetime) -> list[str]:
        """Land the terminal state of every experiment whose deadline has passed.

        This is bookkeeping, not the authority. A deployment whose control row
        still points at a lapsed run is treated as off by every reader here --
        see :meth:`effective_status` -- so a missed sweep cannot extend an
        experiment, and a restart cannot resurrect one.
        """
        control = await self._control.get_control_state(deployment=self._deployment)
        if control is None or not control.get("current_run_id"):
            return []
        run_id = str(control["current_run_id"])
        run = await self._control.get_run(run_id=run_id)
        if run is None:
            # The control row names a run the catalogue does not have. That is a
            # broken pair, and the safe reading is "not collecting"; the run id
            # is reported so an operator can see what the row points at.
            logger.warning(
                "agent_profiling_control_run_missing",
                extra={
                    "event": "agent_profiling_control_run_missing",
                    "deployment": self._deployment,
                    "run_id": run_id,
                },
            )
            return []
        if run["expires_at"] is None or run["expires_at"] > now:
            return []
        result = await self._control.expire_run(deployment=self._deployment, run_id=run_id, now=now)
        if result.applied:
            logger.info(
                "agent_profiling_run_expired",
                extra={
                    "event": "agent_profiling_run_expired",
                    "deployment": self._deployment,
                    "run_id": run_id,
                    "revision": result.revision,
                },
            )
            return [run_id]
        return []

    # -- reads -------------------------------------------------------------

    async def control_snapshot(self, *, now: datetime) -> dict[str, Any]:
        """Return everything the control card needs in one answer.

        One call rather than four, because the card polls and a page assembled
        from four reads taken at four instants is a page whose status can
        contradict its own revision.

        Everything here describes the *configuration*: what the deployment is
        currently set to collect, until when, and what that configuration has
        stored so far. It deliberately says nothing about which processes have
        applied it -- sync lags by design, and the page states the lag instead
        of pretending to have measured it.
        """
        control = await self._control.get_control_state(deployment=self._deployment)
        revision = int(control["revision"]) if control is not None else 0
        run: dict[str, Any] | None = None
        stopped_run: dict[str, Any] | None = None
        stored = 0
        if control is not None and control.get("current_run_id"):
            run_id = str(control["current_run_id"])
            run = await self._control.get_run(run_id=run_id)
            stored = await self._control.count_stored_measurements(run_id=run_id)
        elif control is not None:
            # Nothing is configured right now. If something was stopped, the
            # page can still name it: "off" and "you stopped it four seconds
            # ago" are different findings, and only the second one tells an
            # operator whether the change they just made landed.
            #
            # It is *context*, never the run in force -- see ``run_active``
            # below. A reader that confused the two would keep collecting for
            # an experiment that was already over.
            stopped_run = await self._last_stopped_run(control)
        status = self.effective_status(control=control, run=run, now=now)
        return {
            "deployment": self._deployment,
            "mode": profile_mode(),
            "revision": revision,
            "configured": control is not None,
            "server_time": now,
            "status": status,
            "run": _run_payload(run, now=now),
            "run_active": run is not None,
            "last_stopped_run": _run_payload(stopped_run, now=now),
            "stored_samples": stored,
            "budget_scope": "unlimited"
            if run is not None and run.get("expires_at") is None
            else "producer_instance",
            "budget_note": (
                "Continuous collection until Disable, with bounded export queues."
                if run is not None and run.get("expires_at") is None
                else "max_operations is per root producer process, not a deployment-wide "
                "cap; a run on two replicas may admit up to that number on each"
            ),
            # How stale a page may be. Stated as a number because the honest
            # version of a background switch is "this takes a few seconds", and
            # a page that did not say so would be implying instant agreement.
            "sync_note": (
                "this switch reaches the control plane, runners and sandbox managers through a "
                "background sync, which normally takes about 30 seconds; a measurement "
                "that already started is not affected"
            ),
            "operations": {
                "selectable": {
                    group: list(names) for group, names in SELECTABLE_OPERATIONS.items()
                },
                "all": sorted(KNOWN_OPERATIONS),
            },
            "bounds": {
                "min_duration_seconds": MIN_DURATION_SECONDS,
                "max_duration_seconds": MAX_DURATION_SECONDS,
                "default_max_operations": DEFAULT_MAX_OPERATIONS,
                "hard_max_operations": MAX_MAX_OPERATIONS,
            },
        }

    async def _last_stopped_run(self, control: Mapping[str, Any]) -> dict[str, Any] | None:
        """Return the experiment the last Stop ended, if it is worth reporting.

        Only the deployment's *most recent* stop, and only until a new
        experiment replaces it. A catalogue replay is not the point: this exists
        so the control card can say what was switched off, and a Stop from last
        week is not what an operator is asking about.
        """
        run_id = control.get("last_stopped_run_id")
        if not run_id:
            return None
        return await self._control.get_run(run_id=str(run_id))

    def effective_status(
        self,
        *,
        control: Mapping[str, Any] | None,
        run: Mapping[str, Any] | None,
        now: datetime,
    ) -> str:
        """Return what the deployment is configured to do right now.

        Four answers, and each is a different fact:

        ``unavailable``
            There is no control row: runtime control was never enabled for this
            deployment. Not "off" -- off is a state somebody chose.
        ``off``
            There is a control row and it is not collecting. This covers "never
            switched on" and "switched off" and "the experiment ended"; the page
            names the last experiment separately when there is one.
        ``on``
            The configured experiment has not reached its deadline.

        The deadline is checked here rather than trusted from a sweep having
        run. A lapsed experiment is over whether or not anything recorded it,
        and reporting otherwise would be the page claiming collection it cannot
        stop.
        """
        if control is None:
            return STATUS_UNAVAILABLE
        if run is None:
            return STATUS_OFF
        expires_at = run.get("expires_at")
        if expires_at is not None and expires_at <= now:
            return STATUS_OFF
        lifecycle = str(run.get("lifecycle") or "")
        if lifecycle in TERMINAL_LIFECYCLES:
            return STATUS_OFF
        return STATUS_ON


def _run_payload(run: Mapping[str, Any] | None, *, now: datetime) -> dict[str, Any] | None:
    """Return one experiment as the control card reads it.

    Used for both the run in force and the one the last Stop ended, because they
    have the same shape and the page renders them with the same vocabulary.
    ``seconds_remaining`` is zero once a deadline has passed rather than a
    negative number: a countdown that ran backwards would be a worse way of
    saying "this is over".
    """
    if run is None:
        return None
    expires_at = run.get("expires_at")
    remaining = max(0, int((expires_at - now).total_seconds())) if expires_at else 0
    return {
        "run_id": run["run_id"],
        "operations": list(run.get("operations") or ()),
        "target": dict(run.get("target") or {}),
        "expires_at": expires_at,
        "continuous": expires_at is None,
        "started_at": run.get("started_at") or run.get("created_at"),
        "stopped_at": run.get("stopped_at"),
        "stop_reason": run.get("stop_reason"),
        "lifecycle": run.get("lifecycle"),
        "source": run.get("source"),
        "created_by": run.get("created_by"),
        "control_revision": run.get("control_revision"),
        "seconds_remaining": remaining,
    }


def _fingerprint(body: Mapping[str, Any]) -> str:
    """Return the canonical content hash of one command."""
    encoded = json.dumps(dict(body), sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


__all__ = [
    "DEFAULT_MAX_OPERATIONS",
    "KNOWN_OPERATIONS",
    "MAX_DURATION_SECONDS",
    "MAX_MAX_OPERATIONS",
    "MIN_DURATION_SECONDS",
    "OPERATION_FILES_LIST",
    "OPERATION_FILES_OPEN",
    "OPERATION_STARTUP",
    "OPERATION_TERMINAL_OPEN",
    "SELECTABLE_GROUPS",
    "SELECTABLE_OPERATIONS",
    "STATUS_OFF",
    "STATUS_ON",
    "STATUS_UNAVAILABLE",
    "STATUS_UNMANAGED",
    "ProfilingControlService",
    "Refusal",
    "StartRequest",
    "StopRequest",
]
