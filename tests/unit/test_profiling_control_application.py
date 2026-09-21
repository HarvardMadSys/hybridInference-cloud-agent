"""What the Admin control service refuses, mints and reports.

The service owns the two decisions an operator cannot check by looking at the
page: whether a Start was well formed enough to attempt, and what a deployment
is *actually* doing. Both have a comfortable wrong answer. A request that names
a misspelled operation selects nothing and still looks like an experiment; a run
whose sweep has not fired yet still looks like it is collecting. Every test here
drives one of those decisions directly, against an in-memory control port, so
the behaviour is pinned without a database.
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone
from typing import Any

import pytest

from cloud_agent.application import profiling_control as control
from cloud_agent.domain.profiling_control import (
    CONTROL_OUTCOME_APPLIED,
    CONTROL_OUTCOME_REFUSED,
    CONTROL_REFUSED_INVALID,
    ControlCommandResult,
)
from cloud_agent.ports.profiling_control import ProfilingControl
from cloud_agent.storage.repositories.profiling_control import (
    LIFECYCLE_ACTIVE,
    LIFECYCLE_EXPIRED,
    LIFECYCLE_FAILED,
    LIFECYCLE_PENDING,
    LIFECYCLE_STOPPED,
    PARTICIPANT_APPLIED,
    PARTICIPANT_BROKER,
    PARTICIPANT_CONTROL_PLANE,
    PARTICIPANT_EXHAUSTED,
    PARTICIPANT_RUNNER,
)

DEPLOYMENT = "prod"
NOW = datetime(2026, 9, 21, 12, 0, tzinfo=timezone.utc)
RUN_ID = "admin-20260921T120000Z-abcd1234"
#: The shape the Admin surface turns into URL path segments.
RUN_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]*$")


class _FakeControl:
    """An in-memory :class:`ProfilingControl`, written out rather than mocked.

    A mock would let a test assert that a call happened without saying what the
    deployment looks like when it did. This fake holds the rows a test set --
    the control row, the catalogue, the participants, the stored count -- and
    records the commands the service sent, so a test reads as behaviour.
    """

    def __init__(self) -> None:
        self.control: dict[str, Any] | None = None
        self.runs: dict[str, dict[str, Any]] = {}
        self.participants: list[dict[str, Any]] = []
        self.stored: dict[str, int] = {}
        self.started: list[dict[str, Any]] = []
        self.stopped: list[dict[str, Any]] = []
        self.expired: list[str] = []
        self.recorded: list[dict[str, Any]] = []
        self.marked: list[dict[str, Any]] = []
        self.read_runs: list[str] = []
        self.counted: list[str] = []
        self.pruned = 0
        #: What ``start_run`` answers when a test wants the port to refuse.
        self.start_result: ControlCommandResult | None = None
        #: Whether the sweep's ``expire_run`` actually changed anything.
        self.expire_applied = True

    # -- reads -------------------------------------------------------------

    async def get_control_state(self, *, deployment: str) -> dict[str, Any] | None:
        """Return the deployment's control row, or ``None`` when unset."""
        return None if self.control is None else dict(self.control)

    async def get_run(self, *, run_id: str) -> dict[str, Any] | None:
        """Return one catalogue row, remembering which was asked for."""
        self.read_runs.append(run_id)
        row = self.runs.get(run_id)
        return None if row is None else dict(row)

    async def list_participants(
        self, *, deployment: str, now: datetime, run_id: str | None = None
    ) -> list[dict[str, Any]]:
        """Return the reported participants, optionally for one run."""
        return [
            dict(item)
            for item in self.participants
            if run_id is None or item.get("run_id") == run_id
        ]

    async def count_stored_measurements(self, *, run_id: str) -> int:
        """Return the stored count for one run, and record the question."""
        self.counted.append(run_id)
        return self.stored.get(run_id, 0)

    async def list_runs(self, *, deployment: str, limit: int = 20) -> list[dict[str, Any]]:
        """Return the catalogue this fake was handed."""
        return [dict(row) for row in self.runs.values()][:limit]

    # -- commands ----------------------------------------------------------

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
        """Record the command, then point the control row at the new run."""
        self.started.append(
            {
                "deployment": deployment,
                "request_id": request_id,
                "fingerprint": fingerprint,
                "expected_revision": expected_revision,
                "run": dict(run),
                "actor_id": actor_id,
                "now": now,
            }
        )
        if self.start_result is not None:
            return self.start_result
        manifest = dict(run)
        revision = expected_revision + 1
        self.runs[str(manifest["run_id"])] = manifest
        self.control = {
            "deployment": deployment,
            "revision": revision,
            "current_run_id": manifest["run_id"],
            "last_stopped_at": None,
            "last_stop_reason": "",
        }
        return ControlCommandResult(
            kind=CONTROL_OUTCOME_APPLIED,
            revision=revision,
            run_id=str(manifest["run_id"]),
            expires_at=manifest.get("expires_at"),
            state=dict(self.control),
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
        """Record the command and advance the revision with a tombstone."""
        self.stopped.append(
            {
                "deployment": deployment,
                "request_id": request_id,
                "fingerprint": fingerprint,
                "run_id": run_id,
                "actor_id": actor_id,
                "now": now,
                "reason": reason,
            }
        )
        revision = int((self.control or {}).get("revision") or 0) + 1
        if self.control is not None:
            self.control = {
                **self.control,
                "revision": revision,
                "current_run_id": None,
                "last_stopped_at": now,
                "last_stop_reason": reason,
            }
        return ControlCommandResult(
            kind=CONTROL_OUTCOME_APPLIED,
            revision=revision,
            run_id=run_id,
            reason=reason,
            state=dict(self.control or {}),
        )

    async def expire_run(
        self, *, deployment: str, run_id: str, now: datetime
    ) -> ControlCommandResult:
        """Land the terminal state, unless the test made this a refusal."""
        self.expired.append(run_id)
        revision = int((self.control or {}).get("revision") or 0) + 1
        if not self.expire_applied:
            return ControlCommandResult(
                kind=CONTROL_OUTCOME_REFUSED, revision=revision, run_id=run_id
            )
        if self.control is not None:
            self.control = {
                **self.control,
                "revision": revision,
                "current_run_id": None,
                "last_stopped_at": now,
                "last_stop_reason": "expired",
            }
        return ControlCommandResult(
            kind=CONTROL_OUTCOME_APPLIED, revision=revision, run_id=run_id, expires_at=now
        )

    async def record_participant(self, **params: Any) -> None:
        """Store one participant report; the service does not call this yet."""
        self.recorded.append(dict(params))

    async def prune_participants(self, *, deployment: str, now: datetime) -> int:
        """Return how many rows a real deployment would have dropped."""
        return self.pruned

    async def mark_run_lifecycle(self, **params: Any) -> None:
        """Store one lifecycle write; the service does not call this yet."""
        self.marked.append(dict(params))


def _fake() -> tuple[_FakeControl, control.ProfilingControlService]:
    """Return a fake port and the service bound to it."""
    fake = _FakeControl()
    return fake, control.ProfilingControlService(control=fake, deployment=DEPLOYMENT)


def _start_args(**overrides: Any) -> dict[str, Any]:
    """Return a Start request that is valid until a test changes one field."""
    args: dict[str, Any] = {
        "request_id": "req-1",
        "expected_revision": 0,
        "operations": [control.OPERATION_TERMINAL_OPEN],
        "host": None,
        "duration_seconds": 300,
        "actor_id": "admin-1",
        "known_hosts": None,
    }
    args.update(overrides)
    return args


def _validated(**overrides: Any) -> control.StartRequest:
    """Return a validated Start, failing loudly when the stub is not valid."""
    _fake_port, service = _fake()
    result = service.validate_start(**_start_args(**overrides))
    assert isinstance(result, control.StartRequest), result
    return result


def _control_row(
    *,
    revision: int = 1,
    run_id: str | None = RUN_ID,
    last_stopped_at: datetime | None = None,
) -> dict[str, Any]:
    """Return a control row as the repository hands it over."""
    return {
        "deployment": DEPLOYMENT,
        "revision": revision,
        "current_run_id": run_id,
        "last_stopped_at": last_stopped_at,
        "last_stop_reason": "",
    }


def _run_row(
    *,
    lifecycle: str = LIFECYCLE_ACTIVE,
    expires_at: datetime | None = None,
    stop_reason: str = "",
) -> dict[str, Any]:
    """Return a catalogue row as the repository hands it over."""
    return {
        "run_id": RUN_ID,
        "operations": [control.OPERATION_TERMINAL_OPEN],
        "target": {},
        "expires_at": expires_at or (NOW + timedelta(minutes=10)),
        "lifecycle": lifecycle,
        "stop_reason": stop_reason,
    }


def _participant(
    *,
    kind: str = PARTICIPANT_RUNNER,
    host: str | None = "host-a",
    applied_revision: int = 1,
    capability: str | None = control.PROFILING_CONTROL_CAPABILITY,
    state: str = PARTICIPANT_APPLIED,
    expected: bool = True,
    missing: bool = False,
) -> dict[str, Any]:
    """Return one participant as the snapshot hands it to the status decision.

    Built through :class:`ParticipantView` on purpose: ``supported`` is derived
    from the capability there, and a test that hand-wrote it would be asserting
    against a payload the service never produces.
    """
    return control.ParticipantView(
        participant_id=f"{kind}:{host or 'any'}",
        kind=kind,
        host=host,
        run_id=None if missing else RUN_ID,
        applied_revision=applied_revision,
        capability=capability,
        state=state,
        detail="",
        expected=expected,
        missing=missing,
    ).to_payload()


def _gaps(gaps: list[dict[str, str]]) -> list[str]:
    """Return just the gap kinds, which is what a page branches on."""
    return [gap["kind"] for gap in gaps]


def _detail_for(gaps: list[dict[str, str]], kind: str) -> str:
    """Return the detail of one gap kind."""
    return next(gap["detail"] for gap in gaps if gap["kind"] == kind)


# ---------------------------------------------------------------------------
# The fake is a port, not a convenience
# ---------------------------------------------------------------------------


def test_the_fake_implements_every_method_the_port_declares():
    """A fake that drifts from the port would let the service call a ghost."""
    declared = {name for name in vars(ProfilingControl) if not name.startswith("_")}

    assert declared <= set(dir(_FakeControl))


# ---------------------------------------------------------------------------
# validate_start: a bad request is refused before a transaction is opened
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("request_id", ["", "   ", "r" * (control.MAX_REQUEST_ID_CHARS + 1)])
def test_a_start_without_a_bounded_request_id_is_refused(request_id):
    """The request id is the idempotency key; a missing one cannot be replayed."""
    _fake_port, service = _fake()

    refusal = service.validate_start(**_start_args(request_id=request_id))

    assert isinstance(refusal, control.Refusal)
    assert refusal.reason == CONTROL_REFUSED_INVALID
    assert "request_id" in refusal.detail


@pytest.mark.parametrize("duration", [0, 59, 1801, 86_400])
def test_a_duration_outside_the_declared_bounds_is_refused(duration):
    """An experiment nobody bounded is an experiment nobody will stop."""
    _fake_port, service = _fake()

    refusal = service.validate_start(**_start_args(duration_seconds=duration))

    assert isinstance(refusal, control.Refusal)
    assert refusal.reason == CONTROL_REFUSED_INVALID
    assert "duration_seconds" in refusal.detail
    assert str(control.MIN_DURATION_SECONDS) in refusal.detail
    assert str(control.MAX_DURATION_SECONDS) in refusal.detail


@pytest.mark.parametrize("operations", [[], [""], ["   "]])
def test_a_start_that_selects_no_operation_is_refused(operations):
    """An empty selection would create a run that can never collect."""
    _fake_port, service = _fake()

    refusal = service.validate_start(**_start_args(operations=operations))

    assert isinstance(refusal, control.Refusal)
    assert refusal.reason == CONTROL_REFUSED_INVALID
    assert "operation" in refusal.detail


def test_an_unknown_operation_names_itself_in_the_refusal():
    """A typo must not silently select nothing and call it an experiment."""
    _fake_port, service = _fake()

    refusal = service.validate_start(
        **_start_args(operations=[control.OPERATION_TERMINAL_OPEN, "terminal.opne"])
    )

    assert isinstance(refusal, control.Refusal)
    assert refusal.reason == CONTROL_REFUSED_INVALID
    assert "terminal.opne" in refusal.detail


def test_an_unknown_host_is_refused_when_a_host_list_is_supplied():
    """A run targeted at a host that does not exist collects nothing."""
    _fake_port, service = _fake()

    refusal = service.validate_start(**_start_args(host="gpu-9", known_hosts=["gpu-1", "gpu-2"]))

    assert isinstance(refusal, control.Refusal)
    assert refusal.reason == CONTROL_REFUSED_INVALID
    assert "gpu-9" in refusal.detail


def test_a_negative_expected_revision_is_refused():
    """A revision that cannot exist means the caller read the page wrong."""
    _fake_port, service = _fake()

    refusal = service.validate_start(**_start_args(expected_revision=-1))

    assert isinstance(refusal, control.Refusal)
    assert refusal.reason == CONTROL_REFUSED_INVALID
    assert "expected_revision" in refusal.detail


def test_the_duration_bounds_themselves_are_accepted():
    """The bounds are inclusive, so the UI's shortest and longest choices work."""
    for duration in (control.MIN_DURATION_SECONDS, control.MAX_DURATION_SECONDS):
        request = _validated(duration_seconds=duration)

        assert request.duration_seconds == duration


def test_a_request_id_at_the_limit_is_accepted():
    """The bound is a bound, not an off-by-one."""
    request = _validated(request_id="r" * control.MAX_REQUEST_ID_CHARS)

    assert request.request_id == "r" * control.MAX_REQUEST_ID_CHARS


# ---------------------------------------------------------------------------
# validate_start: what is accepted
# ---------------------------------------------------------------------------


def test_all_hosts_is_an_absent_target_not_an_empty_string():
    """Every request the deployment serves is in scope, and the target says so."""
    for host in (None, "", "   "):
        request = _validated(host=host)

        assert request.host is None


def test_a_known_host_is_accepted_when_it_is_in_the_list():
    """The host list narrows the target; it does not reject every host."""
    request = _validated(host="gpu-1", known_hosts=["gpu-1", "gpu-2"])

    assert request.host == "gpu-1"


def test_a_host_list_is_not_required_to_name_the_host():
    """With no registry to compare against, a named host is trusted as given."""
    request = _validated(host="gpu-9", known_hosts=None)

    assert request.host == "gpu-9"


def test_a_duplicated_operation_list_is_deduplicated_in_order():
    """The run identity is that list, so it must not disagree with itself."""
    request = _validated(
        operations=[
            control.OPERATION_TERMINAL_OPEN,
            control.OPERATION_TERMINAL_OPEN,
            control.OPERATION_FILES_OPEN,
            control.OPERATION_TERMINAL_OPEN,
        ]
    )

    assert request.operations == (control.OPERATION_TERMINAL_OPEN, control.OPERATION_FILES_OPEN)


def test_every_declared_operation_is_selectable():
    """A name in the vocabulary that the validator refuses is a dead checkbox."""
    for operation in sorted(control.KNOWN_OPERATIONS):
        request = _validated(operations=[operation])

        assert request.operations == (operation,)


def test_every_selectable_group_names_only_known_operations():
    """The checkboxes and the validator must share one vocabulary."""
    mapped = {name for group in control.SELECTABLE_OPERATIONS.values() for name in group}

    assert mapped == set(control.KNOWN_OPERATIONS)


# ---------------------------------------------------------------------------
# build_run: the run id and the deadline are the server's to mint
# ---------------------------------------------------------------------------


def test_build_run_mints_a_url_safe_run_id_and_an_absolute_deadline():
    """The id is a path segment and the deadline is the authority; both are ours."""
    _fake_port, service = _fake()

    manifest, expires_at = service.build_run(_validated(duration_seconds=600), now=NOW)

    assert RUN_ID_PATTERN.fullmatch(manifest["run_id"])
    assert manifest["run_id"] != RUN_ID
    assert len(manifest["run_id"]) <= control.MAX_RUN_ID_CHARS
    assert manifest["expires_at"] == NOW + timedelta(seconds=600)
    assert expires_at == NOW + timedelta(seconds=600)


def test_build_run_states_the_immutable_experiment():
    """Everything a producer validates against is written once, here."""
    _fake_port, service = _fake()
    request = _validated(
        operations=[control.OPERATION_FILES_OPEN, control.OPERATION_STARTUP],
        duration_seconds=60,
    )

    manifest, _expires_at = service.build_run(request, now=NOW)

    assert manifest["operations"] == [control.OPERATION_FILES_OPEN, control.OPERATION_STARTUP]
    assert manifest["config_version"] == 1
    assert manifest["max_operations"] == control.DEFAULT_MAX_OPERATIONS
    assert manifest["admission_instances"] == []


def test_build_run_writes_an_empty_target_for_all_hosts():
    """An empty target means unscoped, which is not the same as a blank host."""
    _fake_port, service = _fake()

    manifest, _expires_at = service.build_run(_validated(host=None), now=NOW)

    assert manifest["target"] == {}


def test_build_run_writes_the_named_host_as_the_target():
    """A host-scoped experiment carries the scope into every producer."""
    _fake_port, service = _fake()

    manifest, _expires_at = service.build_run(_validated(host="gpu-1"), now=NOW)

    assert manifest["target"] == {"host": "gpu-1"}


def test_two_runs_minted_at_the_same_instant_are_still_distinct():
    """Two Starts in the same second must not collide on one experiment."""
    _fake_port, service = _fake()
    request = _validated()

    first, _ = service.build_run(request, now=NOW)
    second, _ = service.build_run(request, now=NOW)

    assert first["run_id"] != second["run_id"]


# ---------------------------------------------------------------------------
# fingerprint: a retry is the same command, a new command is not
# ---------------------------------------------------------------------------


def test_a_start_fingerprint_is_stable_for_the_same_request():
    """A lost response retried with the same body must look like the same command."""
    first = control.StartRequest(
        request_id="req-1",
        expected_revision=3,
        operations=(control.OPERATION_TERMINAL_OPEN, control.OPERATION_FILES_LIST),
        host="gpu-1",
        duration_seconds=300,
        actor_id="admin-1",
    )
    second = control.StartRequest(
        request_id="req-1",
        expected_revision=3,
        operations=(control.OPERATION_TERMINAL_OPEN, control.OPERATION_FILES_LIST),
        host="gpu-1",
        duration_seconds=300,
        actor_id="admin-1",
    )

    assert first.fingerprint() == second.fingerprint()


def test_a_start_fingerprint_ignores_the_order_operations_were_named_in():
    """Two spellings of one selection are one command, not two experiments."""
    forward = _validated(operations=[control.OPERATION_TERMINAL_OPEN, control.OPERATION_FILES_LIST])
    backward = _validated(
        operations=[control.OPERATION_FILES_LIST, control.OPERATION_TERMINAL_OPEN]
    )

    assert forward.fingerprint() == backward.fingerprint()


def test_a_start_fingerprint_ignores_the_key_and_the_actor():
    """The id is the key and the actor is who retried, not what was asked."""
    original = _validated(request_id="req-1", actor_id="admin-1")
    retried = _validated(request_id="req-2", actor_id="admin-2")

    assert original.fingerprint() == retried.fingerprint()


@pytest.mark.parametrize(
    "changed",
    [
        {"operations": [control.OPERATION_FILES_LIST]},
        {"host": "gpu-1"},
        {"duration_seconds": 600},
    ],
)
def test_a_start_fingerprint_changes_with_what_was_asked_for(changed):
    """A different experiment under the same request id is a conflict, not a replay."""
    original = _validated()
    other = _validated(**changed)

    assert original.fingerprint() != other.fingerprint()


def test_a_start_and_a_stop_never_share_a_fingerprint():
    """The action is part of the content, so one request id cannot mean both."""
    start = _validated()

    stop = control.StopRequest(request_id="req-1", run_id=RUN_ID, actor_id="admin-1")

    assert start.fingerprint() != stop.fingerprint()


def test_a_stop_fingerprint_is_stable_and_content_sensitive():
    """A retried Stop is idempotent; a Stop of another run is another command."""
    original = control.StopRequest(request_id="req-1", run_id=RUN_ID, actor_id="admin-1")
    retried = control.StopRequest(request_id="req-1", run_id=RUN_ID, actor_id="admin-1")
    other_run = control.StopRequest(request_id="req-1", run_id="run-2", actor_id="admin-1")
    other_reason = control.StopRequest(
        request_id="req-1", run_id=RUN_ID, actor_id="admin-1", reason="expired_early"
    )

    assert original.fingerprint() == retried.fingerprint()
    assert original.fingerprint() != other_run.fingerprint()
    assert original.fingerprint() != other_reason.fingerprint()


def test_a_stop_needs_a_run_id_that_could_be_a_path_segment():
    """The run id is echoed into Admin URLs, so a slash is refused up front."""
    _fake_port, service = _fake()

    good = service.validate_stop(request_id="req-1", run_id=RUN_ID, actor_id=None)
    bad = service.validate_stop(request_id="req-1", run_id="run/../etc", actor_id=None)

    assert isinstance(good, control.StopRequest)
    assert isinstance(bad, control.Refusal)
    assert bad.reason == CONTROL_REFUSED_INVALID


# ---------------------------------------------------------------------------
# effective_status: the order of the questions is the order of their authority
# ---------------------------------------------------------------------------


def test_a_deployment_with_no_control_row_is_unavailable_not_off():
    """Nobody ever commanded this deployment; that is not a choice it made."""
    _fake_port, service = _fake()

    status, gaps = service.effective_status(control=None, run=None, participants=[], now=NOW)

    assert status == control.STATUS_UNAVAILABLE
    assert status != control.STATUS_OFF
    assert _gaps(gaps) == ["control_row_missing"]


def test_a_control_row_with_no_run_is_off_with_nothing_to_explain():
    """Stopped or never started: there is nothing running and nothing missing."""
    _fake_port, service = _fake()

    status, gaps = service.effective_status(
        control=_control_row(run_id=None), run=None, participants=[], now=NOW
    )

    assert status == control.STATUS_OFF
    assert gaps == []


@pytest.mark.parametrize("lifecycle", [LIFECYCLE_PENDING, LIFECYCLE_ACTIVE])
def test_a_lapsed_deadline_outranks_every_confirmation(lifecycle):
    """The deadline is the authority; a missed sweep cannot extend a run."""
    _fake_port, service = _fake()
    run = _run_row(lifecycle=lifecycle, expires_at=NOW - timedelta(seconds=1))
    confirmed = [
        _participant(kind=PARTICIPANT_CONTROL_PLANE, host=None),
        _participant(kind=PARTICIPANT_RUNNER, host="host-a"),
        _participant(kind=PARTICIPANT_BROKER, host="host-a"),
    ]

    status, gaps = service.effective_status(
        control=_control_row(), run=run, participants=confirmed, now=NOW
    )

    assert status == control.STATUS_EXPIRED
    assert _gaps(gaps) == ["deadline_passed"]
    assert run["expires_at"].isoformat() in _detail_for(gaps, "deadline_passed")


@pytest.mark.parametrize(
    ("lifecycle", "expected"),
    [
        (LIFECYCLE_STOPPED, control.STATUS_STOPPED),
        (LIFECYCLE_EXPIRED, control.STATUS_EXPIRED),
        # A run recorded as failed is over, and "stopped" is the word the page
        # has for an experiment that is not collecting and was not expired.
        (LIFECYCLE_FAILED, control.STATUS_STOPPED),
    ],
)
def test_a_terminal_lifecycle_reports_its_ending(lifecycle, expected):
    """A stored terminal state is evidence, and it outranks a live-looking run."""
    _fake_port, service = _fake()
    run = _run_row(lifecycle=lifecycle, stop_reason="operator_stopped")

    status, gaps = service.effective_status(
        control=_control_row(), run=run, participants=[], now=NOW
    )

    assert status == expected
    assert _gaps(gaps) == ["terminal"]
    assert "operator_stopped" in _detail_for(gaps, "terminal")


@pytest.mark.parametrize("lifecycle", [LIFECYCLE_PENDING, LIFECYCLE_ACTIVE])
def test_collecting_needs_every_expected_participant_on_the_current_revision(lifecycle):
    """``collecting`` is evidenced, not optimistic: all of them answered."""
    _fake_port, service = _fake()
    participants = [
        _participant(kind=PARTICIPANT_CONTROL_PLANE, host=None),
        _participant(kind=PARTICIPANT_RUNNER, host="host-a"),
        _participant(kind=PARTICIPANT_BROKER, host="host-a"),
    ]

    status, gaps = service.effective_status(
        control=_control_row(revision=1),
        run=_run_row(lifecycle=lifecycle),
        participants=participants,
        now=NOW,
    )

    assert status == control.STATUS_COLLECTING
    assert gaps == []


def test_a_missing_participant_before_any_confirmation_is_starting():
    """Nothing has answered yet, so the honest word is that it is starting."""
    _fake_port, service = _fake()
    participants = [
        _participant(
            kind=PARTICIPANT_BROKER,
            host="host-a",
            applied_revision=-1,
            capability=None,
            missing=True,
        ),
    ]

    status, gaps = service.effective_status(
        control=_control_row(revision=1),
        run=_run_row(lifecycle=LIFECYCLE_PENDING),
        participants=participants,
        now=NOW,
    )

    assert status == control.STATUS_STARTING
    assert _gaps(gaps) == ["participant_missing"]
    assert "host-a" in _detail_for(gaps, "participant_missing")


@pytest.mark.parametrize("lifecycle", [LIFECYCLE_PENDING, LIFECYCLE_ACTIVE])
def test_a_missing_participant_after_a_confirmation_is_partially_active(lifecycle):
    """Part of the scope is collecting, and the page must not round that up."""
    _fake_port, service = _fake()
    participants = [
        _participant(kind=PARTICIPANT_CONTROL_PLANE, host=None),
        _participant(kind=PARTICIPANT_RUNNER, host="host-a"),
        _participant(
            kind=PARTICIPANT_BROKER,
            host="host-b",
            applied_revision=-1,
            capability=None,
            missing=True,
        ),
    ]

    status, gaps = service.effective_status(
        control=_control_row(revision=1),
        run=_run_row(lifecycle=lifecycle),
        participants=participants,
        now=NOW,
    )

    assert status == control.STATUS_PARTIALLY_ACTIVE
    assert _gaps(gaps) == ["participant_missing"]


@pytest.mark.parametrize("capability", [None, "operation-profiling-control-v0"])
def test_a_participant_that_cannot_be_controlled_is_reported_unsupported(capability):
    """A process that cannot honour a Stop is not a participant, it is a gap."""
    _fake_port, service = _fake()
    participants = [
        _participant(kind=PARTICIPANT_CONTROL_PLANE, host=None),
        _participant(kind=PARTICIPANT_BROKER, host="host-a", capability=capability),
    ]

    status, gaps = service.effective_status(
        control=_control_row(revision=1),
        run=_run_row(lifecycle=LIFECYCLE_ACTIVE),
        participants=participants,
        now=NOW,
    )

    assert status == control.STATUS_PARTIALLY_ACTIVE
    assert _gaps(gaps) == ["participant_unsupported"]
    assert "host-a" in _detail_for(gaps, "participant_unsupported")


def test_a_participant_behind_the_deployment_revision_is_reported_behind():
    """An older revision may not know about the Stop that just happened."""
    _fake_port, service = _fake()
    participants = [
        _participant(kind=PARTICIPANT_CONTROL_PLANE, host=None, applied_revision=3),
        _participant(kind=PARTICIPANT_BROKER, host="host-a", applied_revision=2),
    ]

    status, gaps = service.effective_status(
        control=_control_row(revision=3),
        run=_run_row(lifecycle=LIFECYCLE_ACTIVE),
        participants=participants,
        now=NOW,
    )

    assert status == control.STATUS_PARTIALLY_ACTIVE
    assert _gaps(gaps) == ["participant_behind"]
    detail = _detail_for(gaps, "participant_behind")
    assert "host-a" in detail
    assert "revision 2" in detail
    assert "behind" in detail


def test_a_participant_that_spent_its_allowance_is_reported_exhausted():
    """``it collected nothing`` and ``it ran out`` must not look alike."""
    _fake_port, service = _fake()
    participants = [
        _participant(kind=PARTICIPANT_CONTROL_PLANE, host=None),
        _participant(kind=PARTICIPANT_BROKER, host="host-a", state=PARTICIPANT_EXHAUSTED),
    ]

    status, gaps = service.effective_status(
        control=_control_row(revision=1),
        run=_run_row(lifecycle=LIFECYCLE_ACTIVE),
        participants=participants,
        now=NOW,
    )

    assert status == control.STATUS_PARTIALLY_ACTIVE
    assert _gaps(gaps) == ["participant_budget_exhausted"]
    assert "host-a" in _detail_for(gaps, "participant_budget_exhausted")


def test_a_live_run_with_nobody_expected_is_starting_not_collecting():
    """A run nobody can confirm with is unobserved, and saying otherwise lies."""
    _fake_port, service = _fake()

    status, gaps = service.effective_status(
        control=_control_row(),
        run=_run_row(lifecycle=LIFECYCLE_ACTIVE),
        participants=[],
        now=NOW,
    )

    assert status == control.STATUS_STARTING
    assert status != control.STATUS_COLLECTING
    assert _gaps(gaps) == ["no_participants"]


def test_a_reported_participant_outside_the_scope_is_not_evidence():
    """A process that answered for another run does not make this one collect."""
    _fake_port, service = _fake()
    participants = [_participant(kind=PARTICIPANT_CONTROL_PLANE, host=None, expected=False)]

    status, gaps = service.effective_status(
        control=_control_row(),
        run=_run_row(lifecycle=LIFECYCLE_ACTIVE),
        participants=participants,
        now=NOW,
    )

    assert status == control.STATUS_STARTING
    assert _gaps(gaps) == ["no_participants"]


def test_a_stop_tombstone_with_nobody_expected_reports_stopped():
    """A Stop was issued and nothing still holds the old authorization."""
    _fake_port, service = _fake()

    status, gaps = service.effective_status(
        control=_control_row(last_stopped_at=NOW - timedelta(seconds=5)),
        run=_run_row(lifecycle=LIFECYCLE_ACTIVE),
        participants=[],
        now=NOW,
    )

    assert status == control.STATUS_STOPPED
    assert _gaps(gaps) == ["stopped"]


def test_every_reason_a_run_is_not_collecting_is_reported_together():
    """Four different faults must not be reported one poll at a time."""
    _fake_port, service = _fake()
    participants = [
        _participant(kind=PARTICIPANT_CONTROL_PLANE, host=None, applied_revision=2),
        _participant(kind=PARTICIPANT_RUNNER, host="host-a", applied_revision=0),
        _participant(kind=PARTICIPANT_BROKER, host="host-a", applied_revision=2, capability=None),
        _participant(
            kind=PARTICIPANT_BROKER,
            host="host-b",
            applied_revision=2,
            state=PARTICIPANT_EXHAUSTED,
        ),
        _participant(
            kind=PARTICIPANT_RUNNER,
            host="host-b",
            applied_revision=-1,
            capability=None,
            missing=True,
        ),
    ]

    status, gaps = service.effective_status(
        control=_control_row(revision=2),
        run=_run_row(lifecycle=LIFECYCLE_ACTIVE),
        participants=participants,
        now=NOW,
    )

    assert status == control.STATUS_PARTIALLY_ACTIVE
    assert _gaps(gaps) == [
        "participant_missing",
        "participant_unsupported",
        "participant_behind",
        "participant_budget_exhausted",
    ]


# ---------------------------------------------------------------------------
# control_snapshot: one answer, so the card cannot contradict itself
# ---------------------------------------------------------------------------


async def test_control_snapshot_reports_the_row_the_run_and_the_stored_count():
    """The card's numbers are claims about the database, so the fake must be read."""
    fake, service = _fake()
    fake.control = _control_row(revision=4)
    fake.runs[RUN_ID] = _run_row(lifecycle=LIFECYCLE_ACTIVE)
    fake.stored[RUN_ID] = 7
    fake.participants = [
        {
            "participant_id": "cp:1",
            "kind": PARTICIPANT_CONTROL_PLANE,
            "host": None,
            "run_id": RUN_ID,
            "applied_revision": 4,
            "capability": control.PROFILING_CONTROL_CAPABILITY,
            "state": PARTICIPANT_APPLIED,
            "detail": "",
            "budget": {"used": 3},
        }
    ]

    snapshot = await service.control_snapshot(now=NOW)

    assert snapshot["budget_scope"] == "producer_instance"
    assert snapshot["stored_samples"] == 7
    assert snapshot["revision"] == 4
    assert snapshot["configured"] is True
    assert snapshot["server_time"] == NOW
    assert snapshot["status"] == control.STATUS_COLLECTING
    assert snapshot["run"]["run_id"] == RUN_ID
    assert fake.counted == [RUN_ID]
    assert snapshot["budget_note"]


async def test_control_snapshot_without_a_control_row_is_unconfigured():
    """A deployment that was never commanded must not read as switched off."""
    fake, service = _fake()

    snapshot = await service.control_snapshot(now=NOW)

    assert snapshot["configured"] is False
    assert snapshot["status"] == control.STATUS_UNAVAILABLE
    assert snapshot["revision"] == 0
    assert snapshot["stored_samples"] == 0
    assert snapshot["run"] is None
    assert snapshot["participants"] == []
    assert fake.read_runs == []


async def test_control_snapshot_synthesises_the_host_that_never_reported():
    """``host gpu-2 never answered`` is the finding, so the slot is shown."""
    fake, service = _fake()
    fake.control = _control_row(revision=1)
    run = _run_row(lifecycle=LIFECYCLE_PENDING)
    run["operations"] = [control.OPERATION_STARTUP]
    run["target"] = {"host": "gpu-2"}
    fake.runs[RUN_ID] = run
    fake.participants = [
        {
            "participant_id": "cp:1",
            "kind": PARTICIPANT_CONTROL_PLANE,
            "host": None,
            "run_id": RUN_ID,
            "applied_revision": 1,
            "capability": control.PROFILING_CONTROL_CAPABILITY,
            "state": PARTICIPANT_APPLIED,
            "detail": "",
            "budget": {},
        }
    ]

    snapshot = await service.control_snapshot(now=NOW)

    missing = [item for item in snapshot["participants"] if item["missing"]]
    assert {(item["kind"], item["host"]) for item in missing} == {
        (PARTICIPANT_RUNNER, "gpu-2"),
        (PARTICIPANT_BROKER, "gpu-2"),
    }
    assert all(item["capability"] is None for item in missing)
    assert all(item["applied_revision"] == -1 for item in missing)
    assert all(item["expected"] is True for item in missing)
    assert snapshot["status"] == control.STATUS_PARTIALLY_ACTIVE
    assert _gaps(snapshot["gaps"]) == ["participant_missing", "participant_missing"]


async def test_control_snapshot_lists_the_vocabulary_and_the_bounds():
    """The page renders the choices from the server, so they cannot drift."""
    _fake_port, service = _fake()

    snapshot = await service.control_snapshot(now=NOW)

    assert set(snapshot["operations"]["selectable"]) == set(control.SELECTABLE_GROUPS)
    assert snapshot["operations"]["all"] == sorted(control.KNOWN_OPERATIONS)
    assert snapshot["bounds"] == {
        "min_duration_seconds": control.MIN_DURATION_SECONDS,
        "max_duration_seconds": control.MAX_DURATION_SECONDS,
        "default_max_operations": control.DEFAULT_MAX_OPERATIONS,
        "hard_max_operations": control.MAX_MAX_OPERATIONS,
    }


# ---------------------------------------------------------------------------
# Commands go to the port unchanged
# ---------------------------------------------------------------------------


async def test_start_forwards_the_manifest_and_the_fingerprint():
    """The service mints; the port persists. Neither may invent the other's half."""
    fake, service = _fake()

    result = await service.start(_validated(host="gpu-1", duration_seconds=300), now=NOW)

    call = fake.started[0]
    assert call["fingerprint"] == _validated(host="gpu-1", duration_seconds=300).fingerprint()
    assert call["run"]["target"] == {"host": "gpu-1"}
    assert call["run"]["expires_at"] == NOW + timedelta(seconds=300)
    assert call["deployment"] == DEPLOYMENT
    assert call["now"] == NOW
    assert result.applied is True


async def test_stop_forwards_the_run_that_was_named():
    """A page left open on an old experiment must reach the port as itself."""
    fake, service = _fake()

    await service.stop(
        control.StopRequest(request_id="req-9", run_id=RUN_ID, actor_id="admin-1"), now=NOW
    )

    call = fake.stopped[0]
    assert call["run_id"] == RUN_ID
    assert call["request_id"] == "req-9"
    assert call["reason"] == "stopped"


# ---------------------------------------------------------------------------
# sweep: bookkeeping, never the authority
# ---------------------------------------------------------------------------


async def test_sweep_does_nothing_without_a_control_row():
    """Nothing is pointed at, so nothing can be swept."""
    fake, service = _fake()

    assert await service.sweep(now=NOW) == []
    assert fake.expired == []


async def test_sweep_does_nothing_when_the_row_points_at_nothing():
    """The tombstone left by a Stop is not a run to expire."""
    fake, service = _fake()
    fake.control = _control_row(run_id=None)

    assert await service.sweep(now=NOW) == []
    assert fake.expired == []


async def test_sweep_leaves_a_run_that_has_not_expired_alone():
    """An experiment inside its window is not bookkeeping."""
    fake, service = _fake()
    fake.control = _control_row()
    fake.runs[RUN_ID] = _run_row(expires_at=NOW + timedelta(seconds=1))

    assert await service.sweep(now=NOW) == []
    assert fake.expired == []


async def test_sweep_lands_a_run_whose_deadline_has_passed():
    """The sweep records the ending; the deadline already decided it."""
    fake, service = _fake()
    fake.control = _control_row()
    fake.runs[RUN_ID] = _run_row(expires_at=NOW - timedelta(seconds=1))

    assert await service.sweep(now=NOW) == [RUN_ID]
    assert fake.expired == [RUN_ID]


async def test_sweep_does_not_point_at_a_run_the_control_row_does_not_name():
    """Only the run in force can be swept; an older one is history."""
    fake, service = _fake()
    fake.control = _control_row(run_id="run-current")
    fake.runs["run-current"] = _run_row(expires_at=NOW + timedelta(minutes=5))
    fake.runs["run-old"] = _run_row(expires_at=NOW - timedelta(minutes=5))
    fake.runs["run-current"]["run_id"] = "run-current"

    assert await service.sweep(now=NOW) == []
    assert fake.read_runs == ["run-current"]
    assert fake.expired == []


async def test_sweep_reports_nothing_when_the_port_refuses_to_expire():
    """A refused expiry is not a sweep, and the page must not say it was."""
    fake, service = _fake()
    fake.control = _control_row()
    fake.runs[RUN_ID] = _run_row(expires_at=NOW - timedelta(seconds=1))
    fake.expire_applied = False

    assert await service.sweep(now=NOW) == []
    assert fake.expired == [RUN_ID]


async def test_sweep_reports_a_run_the_catalogue_does_not_have(caplog):
    """A control row pointing at a missing run is a broken pair, not a sweep."""
    fake, service = _fake()
    fake.control = _control_row(run_id="run-missing")

    assert await service.sweep(now=NOW) == []
    assert fake.expired == []
    assert "agent_profiling_control_run_missing" in caplog.text


async def test_prune_participants_is_delegated_with_the_deployment():
    """The prune is a storage decision; the service only names the namespace."""
    fake, service = _fake()
    fake.pruned = 3

    assert await service.prune_participants(now=NOW) == 3
    assert fake.pruned == 3
