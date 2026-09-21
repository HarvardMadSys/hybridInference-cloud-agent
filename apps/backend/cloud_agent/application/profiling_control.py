"""The Admin-facing lifecycle of an operation-measurement experiment.

This module is where "an administrator clicked Start" becomes "three processes
are collecting", and it exists because that sentence has a lot of ways to be
half-true. Four rules shape it.

**The server owns time.** ``started_at`` and ``expires_at`` are computed here,
never sent by the browser, and every read re-decides "is this still running"
from the authoritative clock rather than from a stored boolean. A page that is
closed, a sweep that never ran and a process that restarted all leave the
experiment ending at the instant it was asked to end.

**Nothing is reported that is not evidenced.** ``collecting`` is not "Start
returned 200": it is the participants this experiment actually needs having
confirmed the revision that started it. Anything less is ``starting`` or
``partially_active`` with a reason, and a deployment whose control row does not
exist is ``unavailable`` rather than a comforting ``off``.

**A run is written before it is pointed at.** The manifest row is inserted and
the control row is advanced in one transaction, so there is no instant at which
the deployment is collecting for an experiment nobody can read, or holding a
run nobody is collecting for.

**The budget is a per-producer allowance and says so.** The design explicitly
declines to add a fleet-wide cap in this slice: a local counter presented as a
global limit would be a quota nobody enforces. Every answer that mentions a
budget names its scope.
"""

from __future__ import annotations

import hashlib
import json
import re
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Any

from cloud_agent.domain.profiling_control import (
    CONTROL_REFUSED_INVALID,
    ControlCommandResult,
)
from cloud_agent.logging_utils import get_logger
from cloud_agent.storage.repositories.profiling_control import (
    LIFECYCLE_EXPIRED,
    LIFECYCLE_PENDING,
    PARTICIPANT_APPLIED,
    PARTICIPANT_BROKER,
    PARTICIPANT_CONTROL_PLANE,
    PARTICIPANT_EXHAUSTED,
    PARTICIPANT_RUNNER,
    TERMINAL_LIFECYCLES,
)
from cloud_agent_profiling.operation_profiling import profile_mode

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

#: The capability a host agent must advertise to be a participant in a
#: runtime-controlled experiment. A host without it is visibly unsupported
#: rather than counted as a participant that has not answered yet.
PROFILING_CONTROL_CAPABILITY = "operation-profiling-control-v1"

#: How long a participant confirmation is believed. Longer than the idle sync
#: interval so a quiet deployment is not reported as lost, short enough that a
#: decommissioned replica leaves the page.
PARTICIPANT_TTL_SECONDS = 300

#: The statuses the Admin page renders. ``unavailable`` and ``unknown`` are
#: deliberately distinct from ``off``: a deployment that cannot be controlled
#: must not look like one that chose not to collect.
STATUS_OFF = "off"
STATUS_STARTING = "starting"
STATUS_COLLECTING = "collecting"
STATUS_PARTIALLY_ACTIVE = "partially_active"
STATUS_STOPPING = "stopping"
STATUS_STOPPED = "stopped"
STATUS_EXPIRED = "expired"
STATUS_UNAVAILABLE = "unavailable"
STATUS_UNKNOWN = "unknown"
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
    duration_seconds: int
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
            "duration_seconds": int(self.duration_seconds),
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


@dataclass
class ParticipantView:
    """What one participant reported, and whether it counts as confirmed."""

    participant_id: str
    kind: str
    host: str | None
    run_id: str | None
    applied_revision: int
    capability: str | None
    state: str
    detail: str
    budget: Mapping[str, Any] = field(default_factory=dict)
    confirmed_at: datetime | None = None
    expires_at: datetime | None = None
    #: Fixed expectations this participant is being judged against. An entry
    #: that has never reported is synthesised with ``missing=True`` rather than
    #: being left out, because "host gpu-2 never answered" is the finding.
    expected: bool = False
    missing: bool = False

    def to_payload(self) -> dict[str, Any]:
        """Return this participant as the Admin surface reads it."""
        return {
            "participant_id": self.participant_id,
            "kind": self.kind,
            "host": self.host,
            "run_id": self.run_id,
            "applied_revision": self.applied_revision,
            "capability": self.capability,
            "supported": self.capability == PROFILING_CONTROL_CAPABILITY,
            "state": self.state,
            "detail": self.detail,
            "budget": dict(self.budget),
            "confirmed_at": self.confirmed_at,
            "expires_at": self.expires_at,
            "expected": self.expected,
            "missing": self.missing,
        }


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
        duration_seconds: int,
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
        if not MIN_DURATION_SECONDS <= int(duration_seconds) <= MAX_DURATION_SECONDS:
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
            duration_seconds=int(duration_seconds),
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

    def build_run(self, request: StartRequest, *, now: datetime) -> tuple[dict[str, Any], datetime]:
        """Return the manifest a Start creates, and the instant it expires.

        The run id and the expiry are minted here from the server's own clock.
        The manifest is the *immutable* description of what was asked for: the
        operations, the target, the duration expressed as an absolute instant,
        and the per-producer allowance. Lifecycle and provenance are separate
        columns, so a Stop never rewrites anything a producer validated against.
        """
        expires_at = now + timedelta(seconds=request.duration_seconds)
        run_id = f"admin-{now.strftime('%Y%m%dT%H%M%SZ')}-{uuid.uuid4().hex[:8]}"
        manifest = {
            "run_id": run_id,
            "operations": list(request.operations),
            "target": {"host": request.host} if request.host else {},
            "expires_at": expires_at,
            "config_version": 1,
            "max_operations": DEFAULT_MAX_OPERATIONS,
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
        if run["expires_at"] > now:
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

    async def prune_participants(self, *, now: datetime) -> int:
        """Drop participant rows nobody has refreshed."""
        return await self._control.prune_participants(deployment=self._deployment, now=now)

    # -- reads -------------------------------------------------------------

    async def control_snapshot(self, *, now: datetime) -> dict[str, Any]:
        """Return everything the control card needs in one answer.

        One call rather than four, because the card polls and a page assembled
        from four reads taken at four instants is a page whose status can
        contradict its own revision.
        """
        control = await self._control.get_control_state(deployment=self._deployment)
        revision = int(control["revision"]) if control is not None else 0
        run: dict[str, Any] | None = None
        participants: list[dict[str, Any]] = []
        stored = 0
        stopped_run: dict[str, Any] | None = None
        if control is not None and not control.get("current_run_id"):
            # Nothing is running. If something was stopped, the page still has
            # to be able to say so: "off" and "you stopped it four seconds ago"
            # are different findings, and only the second one tells an operator
            # whether the Stop they just issued landed. The tombstone the Stop
            # wrote is what makes that answer possible without guessing.
            #
            # It is *context*, never the run in force -- see ``run_active``
            # below. A caller that could not tell the two apart would keep
            # collecting for an experiment that was already stopped, which is
            # exactly the failure this whole feature exists to prevent.
            stopped_run = await self._last_stopped_run(control)
        if control is not None and control.get("current_run_id"):
            run_id = str(control["current_run_id"])
            run = await self._control.get_run(run_id=run_id)
            # The revision is read once and passed down. Re-reading it inside
            # the participant list would let a Start or a Stop that landed
            # between the two reads produce a payload whose own status and
            # whose participant details disagree about which revision is in
            # force -- precisely the contradiction this method exists to
            # prevent.
            participants = await self._expected_participants(run=run, now=now, revision=revision)
            stored = await self._control.count_stored_measurements(run_id=run_id)
        status, gaps = self.effective_status(
            control=control,
            run=run if run is not None else stopped_run,
            participants=participants,
            now=now,
        )
        return {
            "deployment": self._deployment,
            "mode": profile_mode(),
            "revision": revision,
            "configured": control is not None,
            "server_time": now,
            "status": status,
            "gaps": gaps,
            # ``run`` is the experiment *in force*. A stopped one is reported
            # beside it rather than as it, so a reader -- the policy worker, a
            # host, the page -- cannot mistake a tombstone for a live command.
            "run": _run_payload(run, now=now) if run is not None else None,
            "run_active": run is not None,
            "last_stopped_run": (
                _run_payload(stopped_run, now=now) if stopped_run is not None else None
            ),
            "stored_samples": stored,
            "budget_scope": "producer_instance",
            "budget_note": (
                "max_operations is per root producer process, not a deployment-wide "
                "cap; a run on two replicas may admit up to that number on each"
            ),
            "participants": participants,
            "participant_ttl_seconds": PARTICIPANT_TTL_SECONDS,
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
        so the control card can show why collection is not happening, and a Stop
        from last week is not why.
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
        participants: Sequence[Mapping[str, Any]],
        now: datetime,
    ) -> tuple[str, list[dict[str, str]]]:
        """Decide what this deployment is actually doing, and why.

        The order of the questions is the order of their authority: a control
        row that does not exist outranks a run, a lapsed deadline outranks a
        confirmation, and an unconfirmed participant outranks a stored sample.

        Returns the status and the gaps that explain anything short of
        ``collecting``.
        """
        gaps: list[dict[str, str]] = []
        if control is None:
            # No control row: runtime control was never enabled for this
            # deployment. That is not "off" -- off is a state somebody chose.
            return STATUS_UNAVAILABLE, [
                {
                    "kind": "control_row_missing",
                    "detail": "this deployment has no profiling control state",
                }
            ]
        if run is None:
            # Never commanded, or commanded and since stopped with the
            # tombstone already cleared. Either way nothing is being collected,
            # and there is no experiment to name.
            return STATUS_OFF, []
        lifecycle = str(run.get("lifecycle") or "")
        expires_at = run.get("expires_at")
        if expires_at is not None and expires_at <= now:
            # The authoritative deadline, not a sweep. A run whose sweep has
            # not run yet is over, and saying otherwise would be the page
            # reporting an experiment it cannot stop.
            return STATUS_EXPIRED, [
                {
                    "kind": "deadline_passed",
                    "detail": f"the experiment ended at {expires_at.isoformat()}",
                }
            ]
        if lifecycle in TERMINAL_LIFECYCLES:
            reason = str(run.get("stop_reason") or lifecycle)
            status = STATUS_EXPIRED if lifecycle == LIFECYCLE_EXPIRED else STATUS_STOPPED
            return status, [{"kind": "terminal", "detail": reason}]
        if run.get("stopped_at") is not None and control.get("current_run_id") is None:
            return STATUS_STOPPED, [
                {"kind": "stopped", "detail": str(run.get("stop_reason") or "stopped")}
            ]
        # From here the experiment is, as far as the database knows, live.
        stopped_at = control.get("last_stopped_at")
        wanted = [item for item in participants if item.get("expected")]
        missing = [item for item in wanted if item.get("missing")]
        unsupported = [
            item for item in wanted if not item.get("missing") and not item.get("supported")
        ]
        stale = [
            item
            for item in wanted
            if not item.get("missing")
            and item.get("applied_revision", 0) < int(control["revision"])
        ]
        exhausted = [
            item
            for item in wanted
            if not item.get("missing") and item.get("state") == PARTICIPANT_EXHAUSTED
        ]
        if stopped_at is not None and not wanted:
            # A Stop was issued and nothing is expected to still hold the old
            # authorization: reporting "off" is exact, and the run keeps its
            # history.
            return STATUS_STOPPED, [{"kind": "stopped", "detail": "collection was stopped"}]
        for name, bucket in (
            ("participant_missing", missing),
            ("participant_unsupported", unsupported),
            ("participant_behind", stale),
            ("participant_budget_exhausted", exhausted),
        ):
            for item in bucket:
                gaps.append(
                    {
                        "kind": name,
                        "detail": _gap_detail(name, item),
                    }
                )
        if gaps:
            # Somebody in the selected scope is not collecting. The experiment
            # is real and partial, and the page must not round that up.
            if lifecycle == LIFECYCLE_PENDING and not any(
                item.get("applied_revision", 0) >= int(control["revision"]) for item in wanted
            ):
                return STATUS_STARTING, gaps
            return STATUS_PARTIALLY_ACTIVE, gaps
        if not wanted:
            # Nothing in the selected scope has a process to confirm with --
            # for example a host that has never enrolled. The run is live but
            # unobserved, and saying "collecting" would be a claim with no
            # evidence behind it.
            return STATUS_STARTING, [
                {
                    "kind": "no_participants",
                    "detail": "no process in the selected scope has reported yet",
                }
            ]
        if lifecycle == LIFECYCLE_PENDING:
            # Every expected participant is already on the current revision,
            # which is what "confirmed" means; the run is collecting.
            return STATUS_COLLECTING, []
        return STATUS_COLLECTING, []

    async def _expected_participants(
        self,
        *,
        run: Mapping[str, Any] | None,
        now: datetime,
        revision: int = 0,
    ) -> list[dict[str, Any]]:
        """Return the participants this run expects, with what each reported.

        The expectation is derived from the run's own manifest: which
        operations it selects decides which *kinds* of process are needed, and
        its target decides which hosts. A host added to the pool after the run
        started is expected too, because it will claim turns and its broker will
        serve calls; a host the run did not target is not, because nothing was
        asked of it.
        """
        if run is None:
            return []
        reported = await self._control.list_participants(
            deployment=self._deployment, now=now, run_id=str(run["run_id"])
        )
        # The hosts this run actually asked something of. A process on a host
        # the run did not target is reported (its revision is real evidence)
        # but is *not* judged against the revision, because nothing was asked
        # of it: counting it would flip a run that no target host has answered
        # for from "starting" to "partially active" on the strength of a
        # machine that was never in scope.
        target_hosts = await self._target_hosts(run.get("target") or {})
        in_scope = set(target_hosts)
        views = [
            _view(item, expected=(item.get("host") in in_scope) if item.get("host") else True)
            for item in reported
        ]
        # The control plane's own root producers are one process each, and there
        # is no registry of replicas to compare against: the ones that report
        # are counted, and a replica that has never reported is simply not
        # listed rather than invented. Runners and brokers *are* enumerable --
        # by host, from the run's target -- so a slot nobody filled is added as
        # missing, because "that host never answered" is the finding the page
        # exists to show.
        for kind, host in await self._required_slots(run=run, hosts=target_hosts):
            if kind == PARTICIPANT_CONTROL_PLANE:
                continue
            if any(view.kind == kind and (view.host or "") == (host or "") for view in views):
                continue
            views.append(
                ParticipantView(
                    participant_id=f"{kind}:{host or 'any'}",
                    kind=kind,
                    host=host,
                    run_id=None,
                    applied_revision=-1,
                    capability=None,
                    state=PARTICIPANT_APPLIED,
                    detail="has not reported this experiment yet",
                    confirmed_at=None,
                    expires_at=None,
                    expected=True,
                    missing=True,
                )
            )
        for view in views:
            if view.expected and not view.missing and view.applied_revision < revision:
                view.detail = view.detail or (
                    f"applied revision {view.applied_revision}, deployment is on {revision}"
                )
        views.sort(key=lambda item: (item.kind, item.host or "", item.participant_id))
        return [view.to_payload() for view in views]

    async def _required_slots(
        self, *, run: Mapping[str, Any], hosts: Sequence[str | None] | None = None
    ) -> list[tuple[str, str | None]]:
        """Return the (kind, host) pairs this run needs to be collecting.

        Sandbox startup is rooted in the runner and its container work happens
        in the broker, so both are required per selected host. File and terminal
        operations are rooted in the control plane but still served by the
        host's broker, so a target host's broker is required for those too.
        """
        operations = {str(item) for item in run.get("operations") or ()}
        selected = (
            list(hosts) if hosts is not None else await self._target_hosts(run.get("target") or {})
        )
        slots: list[tuple[str, str | None]] = [(PARTICIPANT_CONTROL_PLANE, None)]
        if OPERATION_STARTUP in operations:
            for host in selected:
                slots.append((PARTICIPANT_RUNNER, host))
        for host in selected:
            slots.append((PARTICIPANT_BROKER, host))
        return slots

    async def _target_hosts(self, target: Mapping[str, Any]) -> list[str | None]:
        """Return the hosts a run's target names, or every enrolled host."""
        wanted = str(target.get("host") or "").strip()
        if wanted:
            return [wanted]
        if self._host_pool is None:
            return []
        try:
            hosts = await self._host_pool.list_runner_hosts()
        except Exception:  # pragma: no cover - a read that must not break the page
            logger.warning("agent_profiling_host_list_failed", exc_info=True)
            return []
        return [str(item.get("host")) for item in hosts if item.get("host")]


def _view(item: Mapping[str, Any], *, expected: bool) -> ParticipantView:
    """Return one reported participant as a view."""
    return ParticipantView(
        participant_id=str(item.get("participant_id") or ""),
        kind=str(item.get("kind") or ""),
        host=item.get("host"),
        run_id=item.get("run_id"),
        applied_revision=int(item.get("applied_revision") or 0),
        capability=item.get("capability"),
        state=str(item.get("state") or PARTICIPANT_APPLIED),
        detail=str(item.get("detail") or ""),
        budget=item.get("budget") or {},
        confirmed_at=item.get("confirmed_at"),
        expires_at=item.get("expires_at"),
        expected=expected,
    )


def _gap_detail(kind: str, item: Mapping[str, Any]) -> str:
    """Return a one-line explanation for one participant's gap."""
    who = item.get("host") or item.get("participant_id") or "a participant"
    if kind == "participant_missing":
        return f"{who} has not reported this experiment"
    if kind == "participant_unsupported":
        return f"{who} does not support runtime profiling control"
    if kind == "participant_behind":
        return (
            f"{who} applied revision {item.get('applied_revision')}, which is behind the deployment"
        )
    if kind == "participant_budget_exhausted":
        return f"{who} spent its local operation allowance"
    return f"{who} is not confirmed"


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
    "PARTICIPANT_TTL_SECONDS",
    "PROFILING_CONTROL_CAPABILITY",
    "SELECTABLE_GROUPS",
    "SELECTABLE_OPERATIONS",
    "STATUS_COLLECTING",
    "STATUS_EXPIRED",
    "STATUS_OFF",
    "STATUS_PARTIALLY_ACTIVE",
    "STATUS_STARTING",
    "STATUS_STOPPED",
    "STATUS_STOPPING",
    "STATUS_UNAVAILABLE",
    "STATUS_UNKNOWN",
    "STATUS_UNMANAGED",
    "ParticipantView",
    "ProfilingControlService",
    "Refusal",
    "StartRequest",
    "StopRequest",
]
