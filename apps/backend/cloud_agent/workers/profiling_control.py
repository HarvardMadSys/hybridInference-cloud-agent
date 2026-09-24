"""Reading the control row into this process, and nothing else.

The collector can be told what to collect; this is what tells it. Every
control-plane process runs one of these loops, reads the persisted control state
and publishes it into that process's policy store, so a file or terminal request
rooted here is selected by the same revision every replica agrees on.

Four numbers run this loop, and each is here for a different reason:

* **30 s idle, 5 s active.** Idle polling is the cost of having runtime control
  at all -- the design is explicit that a deployment can no longer claim zero
  I/O while switched off. When a run is in force the loop tightens, because that
  is when a Stop has a deadline to meet.
* **A local wake.** After this replica's own Admin write the loop refreshes
  immediately instead of waiting out the interval. It moves only this process:
  the other replicas still learn through the same database read, which is what
  keeps one code path authoritative.
* **No lease, and no query on the hot path.** No file or terminal request ever
  reads the database: it consults this process's published snapshot, which the
  loop below keeps current. Timed runs expire independently; continuous runs
  retain the last applied state while the loop cannot run.
* **A sweep.** Landing the terminal state of a lapsed run is bookkeeping, not
  authority: readers already treat a past deadline as over. Doing it here keeps
  the control row honest for the *next* Start, which is the one place the
  difference matters.
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import replace
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any

from cloud_agent.logging_utils import get_logger
from cloud_agent_shared.operation_profiling import (
    MODE_ADMIN,
    SOURCE_ADMIN,
    ControlState,
    ProfilingRun,
    parse_run as parse_profile_run,
    policy_store,
    profile_mode,
)

if TYPE_CHECKING:
    from collections.abc import Mapping

    from cloud_agent.application.profiling_control import ProfilingControlService

logger = get_logger(__name__)

#: How often to look while an experiment is in force. The design's Start
#: contract is 35 s and this is a seventh of it.
ACTIVE_INTERVAL_S = 5.0
#: How often to look when nothing is running. This is the idle cost the design
#: asks to be accounted for rather than denied.
IDLE_INTERVAL_S = 30.0
#: After a failed read, come back sooner than a full idle interval but not in a
#: tight loop: a database that is down must not be hammered by every replica.
ERROR_INTERVAL_S = 10.0


class ProfilingPolicyWorker:
    """One control-plane process's view of the deployment's control state."""

    def __init__(self, *, service: ProfilingControlService) -> None:
        """Bind the loop to the deployment whose control state it publishes."""
        self._service = service
        self._wake = asyncio.Event()
        self._interval = IDLE_INTERVAL_S
        self._running = False

    @property
    def interval_s(self) -> float:
        """Return the interval the last pass settled on, for diagnostics."""
        return self._interval

    def wake(self) -> None:
        """Ask the loop to refresh now rather than at the next interval.

        Called after a write this process performed. It is deliberately not a
        broadcast: a replica that refreshed because a *different* replica wrote
        would be reaching around the database, and the database read is the one
        authority the whole feature is built on.
        """
        self._wake.set()

    async def run(self) -> None:
        """Refresh the local snapshot until cancelled."""
        if profile_mode() != MODE_ADMIN:
            # Nothing to publish: in ``environment`` mode the descriptor is
            # process configuration, and in ``disabled`` mode there is no
            # collection to control. Publishing anything here would replace a
            # static deployment's own switch with this loop's opinion.
            logger.info(
                "operation profiling control loop is idle",
                extra={
                    "event": "agent_profiling_control_idle",
                    "mode": profile_mode(),
                },
            )
            while True:
                await asyncio.sleep(IDLE_INTERVAL_S)
        self._running = True
        try:
            while True:
                interval = await self._refresh_once()
                self._interval = interval
                await self._wait(interval)
        except asyncio.CancelledError:
            # Publishing is what stops this process collecting, so a cancelled
            # loop leaves the last snapshot in place. The lease on it is what
            # ends collection: no refresh, no new authorization, and the
            # existing one expires in AUTHORIZATION_TTL_S.
            logger.info(
                "operation profiling control loop stopped",
                extra={"event": "agent_profiling_control_stopped"},
            )
            raise

    async def _refresh_once(self) -> float:
        """Read the control state once, publish it, and say when to read again."""
        now = datetime.now(timezone.utc)
        try:
            # The sweep first: it may end a lapsed experiment, and the snapshot
            # published below should describe the state after that rather than
            # before it.
            await self._service.sweep(now=now)
            snapshot = await self._service.control_snapshot(now=now)
        except asyncio.CancelledError:
            raise
        except Exception:
            # A control plane that cannot read the control row must not keep
            # collecting on the strength of its last answer. Publishing "no
            # run" is the fail-closed direction: the business path is untouched,
            # and the experiment's own deadline bounds anything already
            # selected.
            logger.warning(
                "operation profiling control read failed; pausing collection",
                exc_info=True,
                extra={"event": "agent_profiling_control_read_failed"},
            )
            self._publish(None)
            return ERROR_INTERVAL_S
        # Only a run that is actually in force becomes policy. A stopped
        # experiment is reported in the snapshot's ``last_stopped_run`` so the
        # page can name it; publishing it here would keep every producer
        # collecting for the experiment an administrator just ended.
        run = snapshot.get("run") if snapshot.get("run_active") else None
        published = self._publish(run, revision=int(snapshot.get("revision") or 0))
        if published is None:
            return ERROR_INTERVAL_S
        # The run's own manifest is unusable, so nothing can be collected for
        # it. Retry soon: the alternative is a deployment that silently ignores
        # an administrator's Start for as long as the idle interval.
        if run is not None and published.run is None:
            return ERROR_INTERVAL_S
        # Poll quickly only while there is something to notice quickly: an
        # experiment in force is the state in which a Stop has to land soon.
        return ACTIVE_INTERVAL_S if published.run is not None else IDLE_INTERVAL_S

    def _publish(self, run: Mapping[str, Any] | None, *, revision: int = 0) -> Any:
        """Publish one configuration, or the fail-closed "nothing"."""
        if run is None:
            return policy_store().publish(
                ControlState(mode=MODE_ADMIN, config_version=revision, run=None)
            )
        parsed = _descriptor_for_run(run)
        if parsed is None:
            logger.warning(
                "operation profiling run descriptor is unusable; not collecting",
                extra={
                    "event": "agent_profiling_run_descriptor_unusable",
                    "run_id": str(run.get("run_id") or ""),
                },
            )
            return policy_store().publish(
                ControlState(
                    mode=MODE_ADMIN,
                    config_version=revision,
                    run=None,
                    stop_reason="descriptor_unusable",
                )
            )
        return policy_store().publish(
            ControlState(mode=MODE_ADMIN, config_version=revision, run=parsed),
            run=parsed,
        )

    async def _wait(self, interval: float) -> None:
        """Sleep the interval, cut short by :meth:`wake`."""
        try:
            await asyncio.wait_for(self._wake.wait(), timeout=interval)
        except asyncio.TimeoutError:
            return
        self._wake.clear()


def _descriptor_for_run(run: Mapping[str, Any]) -> ProfilingRun | None:
    """Build a parsed run descriptor from a stored catalogue row.

    Rebuilt through the collector's own parser rather than assembled field by
    field, so the descriptor every process acts on is validated by exactly the
    code that validates a deployment's static descriptor. A catalogue row the
    parser refuses is a row no producer could have collected for, and the loop
    reports that rather than publishing a half-built run.
    """
    expires_at = run.get("expires_at")
    if isinstance(expires_at, datetime):
        expires_at_text = expires_at.astimezone(timezone.utc).isoformat()
    else:
        expires_at_text = str(expires_at or "")
    descriptor: dict[str, Any] = {
        "run_id": str(run.get("run_id") or ""),
        "operations": [str(item) for item in run.get("operations") or ()],
        "expires_at": expires_at_text,
        "config_version": int(run.get("config_version") or 1),
        "max_operations": int(run.get("max_operations") or 100),
        "target": dict(run.get("target") or {}),
        "admission_instances": [dict(item) for item in run.get("admission_instances") or ()],
    }
    if run.get("continuous") is True:
        if "expires_at" not in run:
            return None
        descriptor.update(continuous=True, expires_at=run.get("expires_at"), max_operations=0)
    try:
        parsed = parse_profile_run(json.dumps(descriptor), allow_continuous=True)
    except Exception:
        return None
    # The parsed descriptor states which control source rooted it. That travels
    # into every measurement, so a reader can tell an administrator's
    # experiment from one a deployment's static configuration described.
    return replace(parsed, source=SOURCE_ADMIN)


__all__ = [
    "ACTIVE_INTERVAL_S",
    "ERROR_INTERVAL_S",
    "IDLE_INTERVAL_S",
    "ProfilingPolicyWorker",
]
