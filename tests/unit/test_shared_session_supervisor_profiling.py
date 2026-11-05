"""Operation profiling and checkpoint behavior for shared sessions."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

from tests.unit.test_shared_session_supervisor import (
    SharedChatSessionSupervisor,
    SharedMemberManifest,
    SimpleNamespace,
    _launch,
    _Manager,
    _members,
    _spec,
    _TransportFactory,
    operation_profiling,
    pytest,
    replace,
    time,
)

from cloud_agent_host.sandbox import KATA_RUNTIME
from cloud_agent_host.tool_windows import ToolMark


def test_shared_claude_turn_fences_and_returns_exact_tool_marks():
    """The supervisor must carry the retained hook slice to the sandbox manager."""

    class Manager(_Manager):
        def tool_mark_reader(self, _spec):
            self.events.append(("marks-begin",))

            def read():
                self.events.append(("marks-finish",))
                return [ToolMark(10.0, "pre", "toolu_1"), ToolMark(10.1, "post", "toolu_1")]

            return SimpleNamespace(read=read)

    manager = Manager()
    factory = _TransportFactory()
    supervisor = SharedChatSessionSupervisor(manager, transport_factory=factory)
    lease = supervisor.acquire(
        _spec(),
        thread_id="athr_source",
        owner="attempt:1",
        activation_generation=1,
        has_settled_context=True,
        manifest=_members(),
    )
    supervisor.ensure_resident(
        "athr_source",
        lease.token,
        _launch("athr_source", native_session_id="native-source", slot=0),
    )

    turn = supervisor.start_turn(
        "athr_source",
        lease.token,
        prompt="work",
        model="glm-5.1",
        reasoning_effort=None,
    )
    telemetry = supervisor.complete_turn("athr_source", lease.token, turn)

    assert telemetry.tool_marks == (
        ToolMark(10.0, "pre", "toolu_1"),
        ToolMark(10.1, "post", "toolu_1"),
    )
    # The census and the tool-mark fence are armed side by side, so either may
    # be recorded first; both come before the marks are read back and before
    # the turn's new processes are reaped.
    assert sorted(manager.events[-5:-3]) == [("census", 0), ("marks-begin",)]
    assert manager.events[-3:] == [
        ("marks-finish",),
        ("reap-except", 0, ((41, 400),)),
        ("normalize", 0),
    ]


def test_the_census_has_its_own_clock_and_honours_its_own_interval():
    """The census must not depend on the turn emitting events.

    Driving censuses from the turn's event stream meant a turn that spent its
    time waiting on a model produced almost none, while the summary still
    advertised a one-second period. This pins the independent tick: one
    census, then nothing until the interval has passed.
    """
    manager = _Manager()
    manager.backend.runtime = KATA_RUNTIME
    supervisor = SharedChatSessionSupervisor(
        manager,
        transport_factory=_TransportFactory(),
        window_sample_interval_s=10.0,
    )
    lease = supervisor.acquire(
        _spec(),
        thread_id="athr_source",
        owner="attempt:1",
        activation_generation=1,
        has_settled_context=True,
        manifest=_members(),
    )
    supervisor.ensure_resident(
        "athr_source",
        lease.token,
        _launch("athr_source", native_session_id="native-source", slot=0),
    )
    # The sampler thread is started with the supervisor; stop it so this test
    # drives the tick itself and asserts the interval deterministically.
    supervisor._stop.set()
    if supervisor._sampler_thread is not None:
        supervisor._sampler_thread.join(timeout=2)

    supervisor.start_turn(
        "athr_source",
        lease.token,
        prompt="work",
        model="glm-5.1",
        reasoning_effort=None,
    )
    registered = [event for event in manager.events if event[0] == "member-window"]
    assert registered[-1] == ("member-window", "register")

    assert supervisor.sample_open_windows() == 1
    sampled = [event for event in manager.events if event[0] == "member-window"]
    assert sampled[-1] == ("member-window", "sample")

    # Within the interval nothing more is due, so the period is the census's
    # own property rather than however often a caller happens to ask.
    assert supervisor.sample_open_windows() == 0
    assert [event for event in manager.events if event[0] == "member-window"] == sampled


def test_a_member_without_an_open_window_is_never_censused():
    """Nothing is measured for a member that is not running a turn."""
    manager = _Manager()
    manager.backend.runtime = KATA_RUNTIME
    supervisor = SharedChatSessionSupervisor(
        manager,
        transport_factory=_TransportFactory(),
        window_sample_interval_s=10.0,
    )
    supervisor._stop.set()

    assert supervisor.sample_open_windows() == 0
    assert [event for event in manager.events if event[0] == "member-window"] == []


def test_a_checkpoint_is_produced_without_any_turn_event():
    """Uploads must not wait for the agent to say something.

    The sampler thread owns both the census and the upload cadence, so a turn
    that spends its time inside one silent tool call still leaves a report
    behind. This drives the tick directly and asserts a checkpoint appears with
    no event stream involved. The checkpoint is parked for the turn's stream,
    which is the only delivery there is: the sandbox manager builds its
    supervisor with no observer of its own.
    """
    manager = _Manager()
    manager.backend.runtime = KATA_RUNTIME
    supervisor = SharedChatSessionSupervisor(
        manager,
        transport_factory=_TransportFactory(),
        window_sample_interval_s=10.0,
        checkpoint_interval_s=0.0,
    )
    lease = supervisor.acquire(
        _spec(),
        thread_id="athr_source",
        owner="attempt:1",
        activation_generation=1,
        has_settled_context=True,
        manifest=_members(),
    )
    supervisor.ensure_resident(
        "athr_source",
        lease.token,
        _launch("athr_source", native_session_id="native-source", slot=0),
    )
    supervisor._stop.set()
    if supervisor._sampler_thread is not None:
        supervisor._sampler_thread.join(timeout=2)

    turn = supervisor.start_turn(
        "athr_source",
        lease.token,
        prompt="work",
        model="glm-5.1",
        reasoning_effort=None,
    )
    assert supervisor.take_reported_checkpoint("athr_source", turn) is None

    supervisor.sample_open_windows()

    # A checkpoint was produced and is waiting for the turn stream to forward,
    # without a single turn event having been read.
    pending = supervisor.take_reported_checkpoint("athr_source", turn)
    assert pending is not None and pending["state"] == "collecting"
    assert pending["cpu"]["capacity_cores"] == 2.0
    # Taking it is what makes forwarding happen once rather than repeatedly.
    assert supervisor.take_reported_checkpoint("athr_source", turn) is None


def test_the_final_report_sequence_stays_above_every_checkpoint():
    """A checkpoint produced while the turn closes must not block the final.

    The sampler keeps ticking while the closing observation runs, which takes a
    guest exec. A final report that derived its sequence from the census counter
    could come out below a checkpoint made in that window, and the store
    rejects a lower sequence -- leaving a completed turn stored as collecting.
    """
    manager = _Manager()
    manager.backend.runtime = KATA_RUNTIME
    supervisor = SharedChatSessionSupervisor(
        manager,
        transport_factory=_TransportFactory(),
        window_sample_interval_s=10.0,
        checkpoint_interval_s=0.0,
    )
    lease = supervisor.acquire(
        replace(_spec(), cpu_limit="0.5"),
        thread_id="athr_source",
        owner="attempt:1",
        activation_generation=1,
        has_settled_context=True,
        manifest=_members(),
    )
    supervisor.ensure_resident(
        "athr_source",
        lease.token,
        _launch("athr_source", native_session_id="native-source", slot=0),
    )
    supervisor._stop.set()
    if supervisor._sampler_thread is not None:
        supervisor._sampler_thread.join(timeout=2)

    turn = supervisor.start_turn(
        "athr_source",
        lease.token,
        prompt="work",
        model="glm-5.1",
        reasoning_effort=None,
    )
    # A later configuration must never relabel an already-open measurement.
    group = supervisor._members["athr_source"]
    group.spec = replace(group.spec, cpu_limit="4")
    sequences: list[int] = []
    for _ in range(3):
        supervisor.sample_open_windows()
        checkpoint = supervisor.take_reported_checkpoint("athr_source", turn)
        if checkpoint is not None:
            sequences.append(checkpoint["report_seq"])
            assert checkpoint["cpu"]["capacity_cores"] == 0.5
    telemetry = supervisor.complete_turn("athr_source", lease.token, turn)

    assert sequences, "checkpoints should have been produced"
    assert telemetry.turn_resources is not None
    assert telemetry.turn_resources["report_seq"] > max(sequences)

    assert telemetry.turn_resources["cpu"]["capacity_cores"] == 0.5


@pytest.mark.parametrize(
    ("pre_existing", "running", "expected"),
    [
        # No container yet: this activation pays for creating it.
        (False, None, "create"),
        # It exists and is stopped: this activation starts it.
        (True, False, "start"),
        # It exists and is running: this activation only attaches to it.
        (True, True, "reuse"),
    ],
)
def test_acquire_reports_what_it_did_to_the_container(pre_existing, running, expected):
    """The four startup paths differ by the action, and only the call knows it.

    A group can outlive the sandbox manager that made it, and a container that was
    stopped and restarted looks exactly like one that never stopped once it is
    running again. So the action is reported by the code that performed it
    rather than inferred later from the container's state.
    """
    manager = _Manager()
    spec = _spec()
    if pre_existing:
        manager.items[spec.container_name] = manager._inspection(spec, running=running)
    supervisor = SharedChatSessionSupervisor(manager)

    lease = supervisor.acquire(
        spec,
        thread_id="athr_source",
        owner="attempt:1",
        activation_generation=1,
        has_settled_context=True,
        manifest=_members(),
    )

    assert lease.container_action == expected


def test_a_container_created_then_started_is_still_reported_as_created():
    """Creating a group also starts it; the stronger fact must survive."""
    manager = _Manager()
    supervisor = SharedChatSessionSupervisor(manager)

    lease = supervisor.acquire(
        _spec(),
        thread_id="athr_source",
        owner="attempt:1",
        activation_generation=1,
        has_settled_context=True,
        manifest=_members(),
    )

    # The fake manager, like the real one, starts what it just created.
    assert manager.starts == [_spec().container_name]
    assert lease.container_action == "create"


def test_container_action_is_reported_even_when_this_process_is_not_profiling():
    """The action is a fact, not a measurement: it never depends on a switch."""
    assert operation_profiling.active_run() is None
    manager = _Manager()
    supervisor = SharedChatSessionSupervisor(manager)

    lease = supervisor.acquire(
        _spec(),
        thread_id="athr_source",
        owner="attempt:1",
        activation_generation=1,
        has_settled_context=True,
        manifest=_members(),
    )

    assert lease.container_action == "create"
    assert lease.stages == []


def test_ensure_resident_reports_a_reopened_native_session():
    """Reopening a retained native session is a resume, not a fresh CLI."""
    manager = _Manager()
    factory = _TransportFactory()
    supervisor = SharedChatSessionSupervisor(manager, transport_factory=factory)
    lease = supervisor.acquire(
        _spec(),
        thread_id="athr_source",
        owner="attempt:1",
        activation_generation=1,
        has_settled_context=True,
        manifest=_members(),
    )

    descriptor = supervisor.ensure_resident(
        "athr_source",
        lease.token,
        _launch("athr_source", native_session_id=None, slot=0),
    )

    # The retained member owns the locator, so an omitted field still resumes
    # the exact session rather than starting a fresh chat.
    assert descriptor.resident_action == "resume"


def test_ensure_resident_reports_a_first_process_as_new():
    """A member with no session to resume starts a new CLI."""
    manager = _Manager()
    factory = _TransportFactory()
    supervisor = SharedChatSessionSupervisor(manager, transport_factory=factory)
    fresh = [
        SharedMemberManifest(
            thread_id="athr_source",
            native_session_id=None,
            context_status="new",
            resident_slot_id=0,
            last_settled_job_id=None,
        )
    ]
    lease = supervisor.acquire(
        _spec(),
        thread_id="athr_source",
        owner="attempt:1",
        activation_generation=1,
        has_settled_context=False,
        manifest=fresh,
    )

    descriptor = supervisor.ensure_resident(
        "athr_source",
        lease.token,
        _launch("athr_source", native_session_id=None, slot=0),
    )

    assert descriptor.resident_action == "new"


def test_ensure_resident_reports_reuse_for_a_hot_process():
    """The same live process accepting another turn is a reuse, not a start."""
    manager = _Manager()
    factory = _TransportFactory()
    supervisor = SharedChatSessionSupervisor(manager, transport_factory=factory)
    launch = _launch("athr_source", native_session_id="native-source", slot=0)
    first = supervisor.acquire(
        _spec(),
        thread_id="athr_source",
        owner="attempt:1",
        activation_generation=1,
        has_settled_context=True,
        manifest=_members(),
    )
    assert (
        supervisor.ensure_resident("athr_source", first.token, launch).resident_action == "resume"
    )
    supervisor.settle("athr_source", first.token, job_id="ajob_source", context_status="restored")

    second = supervisor.acquire(
        _spec(),
        thread_id="athr_source",
        owner="attempt:2",
        activation_generation=2,
        has_settled_context=True,
        manifest=_members(),
    )
    descriptor = supervisor.ensure_resident("athr_source", second.token, launch)

    assert descriptor.resident_action == "reuse"
    assert len(factory.opened) == 1


def test_sandbox_manager_sub_stages_are_reported_when_this_process_is_profiling(
    monkeypatch, tmp_path
):
    """With the sandbox manager's switch on, the container work comes back as durations."""
    monkeypatch.setenv(operation_profiling.ENV_PROFILING_ENABLED, "true")
    monkeypatch.setenv(
        operation_profiling.ENV_PROFILING_RUN,
        json.dumps(
            {
                "run_id": "sandbox-manager-stages-1",
                "operations": ["attempt.startup"],
                "expires_at": (datetime.now(UTC) + timedelta(minutes=15)).isoformat(),
                "max_operations": 10,
            }
        ),
    )
    operation_profiling.reset_cache()
    manager = _Manager()
    supervisor = SharedChatSessionSupervisor(manager)

    lease = supervisor.acquire(
        _spec(),
        thread_id="athr_source",
        owner="attempt:1",
        activation_generation=1,
        has_settled_context=True,
        manifest=_members(),
        profiling={
            "run_id": "sandbox-manager-stages-1",
            "measurement_id": "msm_x",
            "operation": "attempt.startup",
            "call_id": "c7",
            "expires_at": time.time() + 60,
        },
    )

    # The answer says which call it belongs to and that this process collected.
    assert lease.profiling_call_id == "c7"
    assert lease.profiling_recorded is True
    # The fake manager is instrumented like the real one: the container was
    # created here and then found already running.
    names = [item["name"] for item in lease.stages]
    assert names == ["container_create", "service_hosts_sync"]
    assert all(item["duration_us"] >= 0 for item in lease.stages)
    # Only durations travel: the caller's timeline cannot place them.
    assert all("start_offset_us" not in item for item in lease.stages)
    assert lease.container_action == "create"


def _enable_sandbox_manager_profiling(monkeypatch) -> None:
    monkeypatch.setenv(operation_profiling.ENV_PROFILING_ENABLED, "true")
    monkeypatch.setenv(
        operation_profiling.ENV_PROFILING_RUN,
        json.dumps(
            {
                "run_id": "sandbox-manager-context-1",
                "operations": ["attempt.startup"],
                "expires_at": (datetime.now(UTC) + timedelta(minutes=15)).isoformat(),
                "max_operations": 10,
            }
        ),
    )
    operation_profiling.reset_cache()


def test_a_lapsed_selection_stops_the_sandbox_manager_recording(monkeypatch):
    """A run that already expired must not collect container sub-stages.

    Regression: the sandbox manager checked only that its cached run existed, so it
    kept measuring after the runner had stopped.
    """
    _enable_sandbox_manager_profiling(monkeypatch)
    manager = _Manager()
    supervisor = SharedChatSessionSupervisor(manager)

    lease = supervisor.acquire(
        _spec(),
        thread_id="athr_source",
        owner="attempt:1",
        activation_generation=1,
        has_settled_context=True,
        manifest=_members(),
        profiling={
            "run_id": "sandbox-manager-context-1",
            "measurement_id": "msm_gone",
            "operation": "attempt.startup",
            "expires_at": time.time() - 1,
        },
    )

    assert lease.stages == []
    # A refusal is still an answer about collection: it says this process did
    # not record, which is what makes the caller's fragment missing.
    assert lease.profiling_recorded is False
    # The action is a fact, so it survives the lapsed measurement.
    assert lease.container_action == "create"


def test_a_selection_for_another_run_is_refused(monkeypatch):
    """The sandbox manager honours only the run configured here."""
    _enable_sandbox_manager_profiling(monkeypatch)
    manager = _Manager()
    supervisor = SharedChatSessionSupervisor(manager)

    lease = supervisor.acquire(
        _spec(),
        thread_id="athr_source",
        owner="attempt:1",
        activation_generation=1,
        has_settled_context=True,
        manifest=_members(),
        profiling={
            "run_id": "some-other-run",
            "measurement_id": "msm_x",
            "operation": "attempt.startup",
            "expires_at": time.time() + 60,
        },
    )

    assert lease.stages == []


def test_a_live_selection_records_container_sub_stages(monkeypatch):
    """With a matching, unexpired selection the sandbox manager reports its work."""
    _enable_sandbox_manager_profiling(monkeypatch)
    manager = _Manager()
    supervisor = SharedChatSessionSupervisor(manager)

    lease = supervisor.acquire(
        _spec(),
        thread_id="athr_source",
        owner="attempt:1",
        activation_generation=1,
        has_settled_context=True,
        manifest=_members(),
        profiling={
            "run_id": "sandbox-manager-context-1",
            "measurement_id": "msm_x",
            "operation": "attempt.startup",
            "call_id": "c9",
            "expires_at": time.time() + 60,
        },
    )

    assert [item["name"] for item in lease.stages] == [
        "container_create",
        "service_hosts_sync",
    ]
    assert all(item["duration_us"] >= 0 for item in lease.stages)
    assert lease.profiling_call_id == "c9"
    assert lease.profiling_recorded is True
    assert lease.profiling_truncated is False


def test_a_reuse_with_no_stages_still_reports_that_it_collected(monkeypatch):
    """An empty stage list is a completed collection, not a missing one.

    Regression: the reuse path runs no `cli_spawn`, so it answers with no
    stages, and the caller read that as never having heard from the sandbox manager.
    """
    _enable_sandbox_manager_profiling(monkeypatch)
    manager = _Manager()
    factory = _TransportFactory()
    supervisor = SharedChatSessionSupervisor(manager, transport_factory=factory)
    live = {
        "run_id": "sandbox-manager-context-1",
        "measurement_id": "msm_x",
        "operation": "attempt.startup",
        "call_id": "c1",
        "expires_at": time.time() + 60,
    }
    lease = supervisor.acquire(
        _spec(),
        thread_id="athr_source",
        owner="attempt:1",
        activation_generation=1,
        has_settled_context=True,
        manifest=_members(),
        profiling=live,
    )
    supervisor.ensure_resident(
        "athr_source",
        lease.token,
        _launch("athr_source", native_session_id="native-source", slot=0),
        profiling=live,
    )
    supervisor.settle("athr_source", lease.token, job_id="ajob_source", context_status="live")

    second = supervisor.acquire(
        _spec(),
        thread_id="athr_source",
        owner="attempt:2",
        activation_generation=2,
        has_settled_context=True,
        manifest=_members(),
        profiling={**live, "call_id": "c2"},
    )
    descriptor = supervisor.ensure_resident(
        "athr_source",
        second.token,
        _launch("athr_source", native_session_id="native-source", slot=0),
        profiling={**live, "call_id": "c2"},
    )

    assert descriptor.resident_action == "reuse"
    assert descriptor.stages == []
    assert descriptor.profiling_call_id == "c2"
    # The member's work was skipped on purpose and that is a complete answer.
    assert descriptor.profiling_recorded is True


def test_a_collection_cut_short_by_the_deadline_says_so(monkeypatch):
    """An empty answer is only "nothing to time" when collection finished.

    Regression, at the response the runner actually reads: the sandbox manager's local
    deadline lapsed while it waited for the member lock, the CLI start was
    never timed, and the lease still reported a completed collection.
    """
    _enable_sandbox_manager_profiling(monkeypatch)
    live = {
        "run_id": "sandbox-manager-context-1",
        "measurement_id": "msm_x",
        "operation": "attempt.startup",
        "call_id": "c1",
        "expires_at": time.time() + 600,
    }
    real_recorder = operation_profiling.start_stage_recording

    def lapsed(context):
        """A recorder that already missed its own (shorter) deadline."""
        recorder = real_recorder(context)
        if recorder is not None:
            recorder.deadline_mono = time.monotonic() - 1
        return recorder

    monkeypatch.setattr("cloud_agent_host.shared_session_supervisor.start_stage_recording", lapsed)
    manager = _Manager()
    supervisor = SharedChatSessionSupervisor(manager, transport_factory=_TransportFactory())

    lease = supervisor.acquire(
        _spec(),
        thread_id="athr_source",
        owner="attempt:1",
        activation_generation=1,
        has_settled_context=True,
        manifest=_members(),
        profiling=live,
    )
    descriptor = supervisor.ensure_resident(
        "athr_source",
        lease.token,
        _launch("athr_source", native_session_id="native-source", slot=0),
        profiling={**live, "call_id": "c2"},
    )

    for answer in (lease, descriptor):
        # The recorder existed and the call answered...
        assert answer.profiling_recorded is True
        # ...but every stage was skipped, so nothing was measured at all.
        assert answer.stages == []
        assert answer.profiling_truncated is True
