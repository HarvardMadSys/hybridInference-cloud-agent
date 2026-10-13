"""What the Admin control service refuses, mints and reports.

Against an explicit in-memory capability, because the refusal branches are the
thing under test and a mock's configured returns would make the test assert what
it was told to assert. The Postgres transactions behind the same port are
covered against a real database by
``tests/integration/servers/test_profiling_control_acceptance_db.py``.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from cloud_agent.application import profiling_control as control
from cloud_agent.domain.profiling_control import (
    CONTROL_OUTCOME_APPLIED,
    CONTROL_OUTCOME_REFUSED,
    CONTROL_OUTCOME_REPLAYED,
    CONTROL_REFUSED_ALREADY_ACTIVE,
    CONTROL_REFUSED_ALREADY_FINISHED,
    CONTROL_REFUSED_CONFLICTING_REQUEST,
    CONTROL_REFUSED_STALE_REVISION,
    LIFECYCLE_ACTIVE,
    LIFECYCLE_EXPIRED,
    LIFECYCLE_PENDING,
    LIFECYCLE_STOPPED,
    ControlCommandResult,
)

NOW = datetime(2026, 9, 21, 12, 0, tzinfo=UTC)


class _FakeControl:
    """An in-memory stand-in for the control capability.

    Every port method is written out rather than mocked, so a method added to
    the Protocol shows up here as a missing one rather than as a test that keeps
    passing against something the real implementation no longer does.
    """

    def __init__(self) -> None:
        self.control: dict[str, Any] | None = {
            "deployment": "test",
            "revision": 0,
            "desired_state": "off",
            "current_run_id": None,
            "updated_at": NOW,
            "updated_by": None,
            "last_stop_revision": None,
            "last_stopped_at": None,
            "last_stopped_run_id": None,
        }
        self.runs: dict[str, dict[str, Any]] = {}
        self.commands: dict[tuple[str, str], dict[str, Any]] = {}
        self.stored = 0

    async def get_control_state(self, *, deployment: str) -> dict[str, Any] | None:
        """Return the control row, if this deployment has one."""
        return self.control

    async def start_run(
        self,
        *,
        deployment: str,
        request_id: str,
        fingerprint: str,
        expected_revision: int,
        run: Any,
        actor_id: str | None,
        now: datetime,
    ) -> ControlCommandResult:
        """Create the experiment, or explain why it is not the one in force."""
        assert self.control is not None
        seen = self.commands.get((deployment, request_id))
        if seen is not None:
            if seen["fingerprint"] != fingerprint:
                return ControlCommandResult(
                    kind=CONTROL_OUTCOME_REFUSED,
                    revision=int(self.control["revision"]),
                    reason=CONTROL_REFUSED_CONFLICTING_REQUEST,
                    detail="this request id was used for a different command",
                )
            return ControlCommandResult(
                kind=CONTROL_OUTCOME_REPLAYED,
                revision=int(seen["revision"]),
                run_id=seen["run_id"],
            )
        if self.control["current_run_id"]:
            return ControlCommandResult(
                kind=CONTROL_OUTCOME_REFUSED,
                revision=int(self.control["revision"]),
                run_id=str(self.control["current_run_id"]),
                reason=CONTROL_REFUSED_ALREADY_ACTIVE,
                detail="another experiment is active; stop it first",
            )
        if int(self.control["revision"]) != int(expected_revision):
            return ControlCommandResult(
                kind=CONTROL_OUTCOME_REFUSED,
                revision=int(self.control["revision"]),
                reason=CONTROL_REFUSED_STALE_REVISION,
                detail="expected revision differs",
            )
        revision = int(self.control["revision"]) + 1
        self.runs[str(run["run_id"])] = {
            "run_id": str(run["run_id"]),
            "operations": list(run["operations"]),
            "target": dict(run.get("target") or {}),
            "expires_at": run["expires_at"],
            "config_version": int(run.get("config_version") or 1),
            "admission_instances": [],
            "created_at": now,
            "received_at": now,
            "source": "admin",
            "lifecycle": LIFECYCLE_PENDING,
            "created_by": actor_id,
            "control_revision": revision,
            "started_at": now,
            "stopped_at": None,
            "stop_reason": None,
            "deployment": deployment,
        }
        self.control.update(
            {
                "revision": revision,
                "desired_state": "on",
                "current_run_id": str(run["run_id"]),
                "updated_at": now,
                "updated_by": actor_id,
            }
        )
        self.commands[(deployment, request_id)] = {
            "revision": revision,
            "run_id": str(run["run_id"]),
            "fingerprint": fingerprint,
        }
        return ControlCommandResult(
            kind=CONTROL_OUTCOME_APPLIED,
            revision=revision,
            run_id=str(run["run_id"]),
            expires_at=run["expires_at"],
        )

    async def stop_run(
        self,
        *,
        deployment: str,
        request_id: str,
        fingerprint: str,
        run_id: str,
        actor_id: str | None,
        now: datetime,
        reason: str,
    ) -> ControlCommandResult:
        """Stop the named run, or replay a Stop already recorded."""
        assert self.control is not None
        seen = self.commands.get((deployment, request_id))
        if seen is not None:
            return ControlCommandResult(
                kind=CONTROL_OUTCOME_REPLAYED,
                revision=int(seen["revision"]),
                run_id=seen["run_id"],
            )
        current = self.control["current_run_id"]
        if current is None:
            return ControlCommandResult(
                kind=CONTROL_OUTCOME_REPLAYED,
                revision=int(self.control["revision"]),
                run_id=None,
            )
        if str(current) != run_id:
            return ControlCommandResult(
                kind=CONTROL_OUTCOME_REFUSED,
                revision=int(self.control["revision"]),
                run_id=str(current),
                reason=CONTROL_REFUSED_ALREADY_FINISHED,
                detail="that experiment is not the one in force",
            )
        revision = int(self.control["revision"]) + 1
        self.control.update(
            {
                "revision": revision,
                "desired_state": "off",
                "current_run_id": None,
                "last_stop_revision": revision,
                "last_stopped_at": now,
                "last_stopped_run_id": str(current),
            }
        )
        row = self.runs.get(str(current))
        if row is not None:
            row.update({"lifecycle": LIFECYCLE_STOPPED, "stopped_at": now, "stop_reason": reason})
        self.commands[(deployment, request_id)] = {
            "revision": revision,
            "run_id": str(current),
            "fingerprint": fingerprint,
        }
        return ControlCommandResult(
            kind=CONTROL_OUTCOME_APPLIED, revision=revision, run_id=str(current)
        )

    async def expire_run(
        self, *, deployment: str, run_id: str, now: datetime
    ) -> ControlCommandResult:
        """Land a lapsed run's terminal state."""
        assert self.control is not None
        row = self.runs.get(run_id)
        if row is None:
            return ControlCommandResult(
                kind=CONTROL_OUTCOME_REFUSED,
                revision=int(self.control["revision"]),
                reason="unknown_run",
                detail="no such experiment",
            )
        if str(row.get("lifecycle")) in {LIFECYCLE_STOPPED, LIFECYCLE_EXPIRED, "failed"}:
            return ControlCommandResult(
                kind=CONTROL_OUTCOME_REPLAYED,
                revision=int(self.control["revision"]),
                run_id=run_id,
            )
        row.update({"lifecycle": LIFECYCLE_EXPIRED, "stop_reason": "expired"})
        revision = int(self.control["revision"]) + 1
        if str(self.control["current_run_id"] or "") == run_id:
            self.control.update(
                {
                    "revision": revision,
                    "desired_state": "off",
                    "current_run_id": None,
                    "last_stopped_run_id": run_id,
                    "last_stopped_at": now,
                }
            )
        return ControlCommandResult(kind=CONTROL_OUTCOME_APPLIED, revision=revision, run_id=run_id)

    async def count_stored_measurements(self, *, run_id: str) -> int:
        """Return how many samples this experiment stored."""
        return self.stored

    async def mark_run_lifecycle(self, **kwargs: Any) -> None:
        """Attach lifecycle to a run that exists."""
        return None

    async def get_run(self, *, run_id: str) -> dict[str, Any] | None:
        """Return one run."""
        return self.runs.get(run_id)

    async def list_runs(self, *, deployment: str, limit: int = 20) -> list[dict[str, Any]]:
        """Return the deployment's runs."""
        return list(self.runs.values())[:limit]


def _service(fake: _FakeControl) -> control.ProfilingControlService:
    """The service under test, over the fake capability."""
    return control.ProfilingControlService(control=fake, deployment="test")


def _start_request(**overrides: Any) -> control.StartRequest:
    """A valid Start, with only the named fields changed."""
    fields: dict[str, Any] = {
        "request_id": "req-1",
        "expected_revision": 0,
        "operations": ("files.open",),
        "host": None,
        "duration_seconds": 600,
        "actor_id": "admin",
    }
    fields.update(overrides)
    return control.StartRequest(**fields)


# ── validation ─────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("overrides", "expected_fragment"),
    [
        ({"request_id": ""}, "request_id"),
        ({"request_id": "x" * 200}, "request_id"),
        ({"duration_seconds": 10}, "duration_seconds"),
        ({"duration_seconds": 4000}, "duration_seconds"),
        ({"operations": ()}, "at least one"),
        ({"operations": ("files.delete",)}, "files.delete"),
        ({"host": "nope"}, "nope"),
        ({"expected_revision": -1}, "expected_revision"),
    ],
)
def test_an_invalid_request_is_refused_and_named(
    overrides: dict[str, Any], expected_fragment: str
) -> None:
    """Every refusal says which field it refused, before any transaction opens."""
    service = _service(_FakeControl())
    payload: dict[str, Any] = {
        "request_id": "req-1",
        "expected_revision": 0,
        "operations": ["files.open"],
        "host": None,
        "duration_seconds": 600,
        "actor_id": "admin",
        "known_hosts": ["gpu-1", "gpu-2"],
    }
    payload.update(overrides)
    result = service.validate_start(**payload)
    assert isinstance(result, control.Refusal), overrides
    assert expected_fragment in result.detail


def test_a_valid_request_is_accepted_with_its_operations_deduplicated() -> None:
    """A list that repeated an operation would be a manifest that disagrees with itself."""
    service = _service(_FakeControl())
    result = service.validate_start(
        request_id="req-1",
        expected_revision=3,
        operations=["files.open", "files.open", "attempt.startup"],
        host="",
        duration_seconds=control.MIN_DURATION_SECONDS,
        actor_id="admin",
        known_hosts=["gpu-1"],
    )
    assert isinstance(result, control.StartRequest)
    assert result.operations == ("files.open", "attempt.startup")
    assert result.host is None
    assert result.expected_revision == 3


def test_an_unreadable_host_pool_does_not_refuse_every_start() -> None:
    """``None`` means the host could not be checked here, not that it is wrong."""
    service = _service(_FakeControl())
    result = service.validate_start(
        request_id="req-1",
        expected_revision=0,
        operations=["files.open"],
        host="gpu-1",
        duration_seconds=600,
        actor_id="admin",
        known_hosts=None,
    )
    assert isinstance(result, control.StartRequest)
    assert result.host == "gpu-1"


def test_a_stop_names_an_experiment_and_requires_a_bounded_id() -> None:
    """A Stop without a request id cannot be retried; a malformed run id is refused."""
    service = _service(_FakeControl())
    assert isinstance(
        service.validate_stop(request_id="", run_id="admin-1", actor_id="a"), control.Refusal
    )
    assert isinstance(
        service.validate_stop(request_id="r", run_id="has spaces", actor_id="a"),
        control.Refusal,
    )
    accepted = service.validate_stop(request_id="r", run_id="admin-1", actor_id="a")
    assert isinstance(accepted, control.StopRequest)
    assert accepted.reason == "stopped"


# ── the manifest a Start creates ───────────────────────────────────────


def test_build_run_mints_a_server_side_experiment() -> None:
    """The id and the deadline are the server's, never the browser's."""
    service = _service(_FakeControl())
    manifest, expires_at = service.build_run(_start_request(host="gpu-1"), now=NOW)
    assert manifest["run_id"].startswith("admin-20260921T120000Z-")
    assert manifest["operations"] == ["files.open"]
    assert manifest["target"] == {"host": "gpu-1"}
    assert manifest["max_operations"] == control.DEFAULT_MAX_OPERATIONS
    assert manifest["config_version"] == 1
    assert expires_at == NOW + timedelta(seconds=600)
    assert manifest["expires_at"] == expires_at


def test_build_run_leaves_all_hosts_as_an_empty_target() -> None:
    """ "All hosts" is the absence of a filter, not a host named "all"."""
    service = _service(_FakeControl())
    manifest, _ = service.build_run(_start_request(), now=NOW)
    assert manifest["target"] == {}


def test_two_starts_never_share_a_run_id() -> None:
    """The id is an identity; a collision would file two experiments together."""
    service = _service(_FakeControl())
    first, _ = service.build_run(_start_request(), now=NOW)
    second, _ = service.build_run(_start_request(), now=NOW)
    assert first["run_id"] != second["run_id"]


def test_a_request_fingerprint_ignores_order_but_not_content() -> None:
    """The fingerprint is what decides whether a retry is the same command."""
    first = _start_request(operations=("files.open", "files.list"), host="gpu-1")
    reordered = _start_request(
        request_id="other",
        expected_revision=99,
        operations=("files.list", "files.open"),
        host="gpu-1",
        actor_id="someone-else",
    )
    other_host = _start_request(operations=("files.open", "files.list"), host="gpu-2")
    assert first.fingerprint() == reordered.fingerprint()
    assert first.fingerprint() != other_host.fingerprint()
    stop_a = control.StopRequest(request_id="a", run_id="admin-1", actor_id="x")
    stop_b = control.StopRequest(request_id="a", run_id="admin-1", actor_id="y")
    assert stop_a.fingerprint() == stop_b.fingerprint()


# ── status ─────────────────────────────────────────────────────────────


def test_a_deployment_with_no_control_row_is_unavailable() -> None:
    """Not "off": off is a state somebody chose."""
    service = _service(_FakeControl())
    assert service.effective_status(control=None, run=None, now=NOW) == control.STATUS_UNAVAILABLE


def test_nothing_configured_is_off() -> None:
    """A control row that names no experiment is not collecting."""
    service = _service(_FakeControl())
    row = {"revision": 4, "current_run_id": None}
    assert service.effective_status(control=row, run=None, now=NOW) == control.STATUS_OFF


def test_a_live_experiment_is_on() -> None:
    """A run whose deadline has not passed is what the page reports as measuring."""
    service = _service(_FakeControl())
    row = {"revision": 4, "current_run_id": "admin-1"}
    run = {"expires_at": NOW + timedelta(minutes=5), "lifecycle": LIFECYCLE_ACTIVE}
    assert service.effective_status(control=row, run=run, now=NOW) == control.STATUS_ON


def test_a_passed_deadline_is_off_even_without_a_sweep() -> None:
    """The deadline is the authority; a sweep that has not run cannot extend it."""
    service = _service(_FakeControl())
    row = {"revision": 4, "current_run_id": "admin-1"}
    run = {"expires_at": NOW - timedelta(seconds=1), "lifecycle": LIFECYCLE_ACTIVE}
    assert service.effective_status(control=row, run=run, now=NOW) == control.STATUS_OFF


def test_a_terminal_lifecycle_is_off() -> None:
    """A stopped or expired run is over whatever its deadline still says."""
    service = _service(_FakeControl())
    row = {"revision": 4, "current_run_id": "admin-1"}
    for lifecycle in (LIFECYCLE_STOPPED, LIFECYCLE_EXPIRED):
        run = {"expires_at": NOW + timedelta(minutes=5), "lifecycle": lifecycle}
        assert service.effective_status(control=row, run=run, now=NOW) == control.STATUS_OFF


# ── the snapshot ───────────────────────────────────────────────────────


async def test_the_snapshot_describes_the_configuration_and_its_samples() -> None:
    """One call answers what is configured, until when, and what it has stored."""
    fake = _FakeControl()
    service = _service(fake)
    started = await service.start(_start_request(host="gpu-1"), now=NOW)
    assert started.applied
    fake.stored = 7
    snapshot = await service.control_snapshot(now=NOW + timedelta(seconds=30))
    assert snapshot["status"] == control.STATUS_ON
    assert snapshot["revision"] == 1
    assert snapshot["configured"] is True
    assert snapshot["run_active"] is True
    assert snapshot["run"]["run_id"] == started.run_id
    assert snapshot["stored_samples"] == 7
    assert snapshot["budget_scope"] == "producer_instance"
    assert "per root producer process" in snapshot["budget_note"]
    # The page has to be able to say that a change takes a moment to land.
    assert "30 seconds" in snapshot["sync_note"]
    # Every operation the page may offer, and the grouping it offers them in.
    assert snapshot["operations"]["selectable"]["file_access"] == ["files.list", "files.open"]
    assert snapshot["bounds"]["min_duration_seconds"] == control.MIN_DURATION_SECONDS
    # Nothing in the answer claims to know what any process has applied.
    assert "participants" not in snapshot
    assert "gaps" not in snapshot


async def test_the_snapshot_names_the_last_stopped_experiment_but_not_as_active() -> None:
    """ "Off" and "you just switched it off" are different findings."""
    fake = _FakeControl()
    service = _service(fake)
    started = await service.start(_start_request(), now=NOW)
    await service.stop(
        control.StopRequest(request_id="s", run_id=str(started.run_id), actor_id="admin"),
        now=NOW + timedelta(seconds=1),
    )
    snapshot = await service.control_snapshot(now=NOW + timedelta(seconds=2))
    assert snapshot["status"] == control.STATUS_OFF
    assert snapshot["run"] is None
    assert snapshot["run_active"] is False
    assert snapshot["last_stopped_run"]["run_id"] == started.run_id
    assert snapshot["last_stopped_run"]["stop_reason"] == "stopped"


async def test_the_snapshot_reports_a_deployment_that_was_never_configured() -> None:
    """No control row is reported as unavailable, with nothing invented."""
    fake = _FakeControl()
    fake.control = None
    service = _service(fake)
    snapshot = await service.control_snapshot(now=NOW)
    assert snapshot["status"] == control.STATUS_UNAVAILABLE
    assert snapshot["configured"] is False
    assert snapshot["run"] is None
    assert snapshot["revision"] == 0


# ── sweep ──────────────────────────────────────────────────────────────


async def test_the_sweep_ends_a_lapsed_experiment_and_nothing_else() -> None:
    """Bookkeeping, not authority: readers already treat a past deadline as over."""
    fake = _FakeControl()
    service = _service(fake)
    assert await service.sweep(now=NOW) == []
    started = await service.start(_start_request(), now=NOW)
    assert await service.sweep(now=NOW + timedelta(seconds=30)) == []
    assert await service.sweep(now=NOW + timedelta(seconds=601)) == [started.run_id]
    assert fake.runs[str(started.run_id)]["lifecycle"] == LIFECYCLE_EXPIRED


@pytest.mark.asyncio
async def test_continuous_control_does_not_expire_when_read_days_later(monkeypatch):
    """Neither the sweeper nor a new service instance imposes a hidden deadline."""
    monkeypatch.setenv("AGENT_OPERATION_PROFILING_MODE", "admin")
    fake = _FakeControl()
    service = _service(fake)
    command = _start_request(duration_seconds=None)
    result = await service.start(command, now=NOW)
    assert result.run_id is not None
    restored = _service(fake)
    later = NOW + timedelta(days=7)
    assert await restored.sweep(now=later) == []
    snapshot = await restored.control_snapshot(now=later)
    assert snapshot["status"] == "on"
    assert snapshot["run"]["run_id"] == result.run_id
    assert snapshot["run"]["expires_at"] is None
    assert snapshot["run"]["continuous"] is True
    assert snapshot["budget_scope"] == "unlimited"
    assert command.fingerprint() != _start_request(duration_seconds=600).fingerprint()
