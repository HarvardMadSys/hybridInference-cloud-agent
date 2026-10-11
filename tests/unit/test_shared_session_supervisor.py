"""Concurrency and capture barriers for shared live chat sandboxes."""

from __future__ import annotations

import io
import json
import subprocess
import tarfile
import threading
import time
from dataclasses import asdict, replace
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from typing import Any

import pytest
from httpx import ASGITransport, AsyncClient

from cloud_agent_host.chat_sandbox import (
    FINAL_ARCHIVE_MARKER_NAME,
    INITIAL_ARCHIVE_MARKER_ROOT,
    MEMBER_UID_BASE,
    ChatSandboxInspection,
    ChatSandboxManager,
    ChatSandboxSpec,
    ChatSandboxStatus,
    RetainedChatFailStopped,
    RetainedChatLost,
    claude_member_config_dir,
)
from cloud_agent_host.drivers import InputOutcome
from cloud_agent_host.guest_observer import (
    GUEST_OBSERVER_SOURCE,
    GuestObservation,
    GuestObserverCursor,
)
from cloud_agent_host.resident_transport import (
    ResidentLaunch,
    ResidentTransport,
    ResidentTransportFactory,
)
from cloud_agent_host.sandbox import KATA_RUNTIME, ProcessBackend, SandboxError
from cloud_agent_host.sandbox_archive_runtime import SandboxArchiveController
from cloud_agent_host.session_lifecycle import SessionBusy, SessionLeaseLost
from cloud_agent_host.shared_session_supervisor import (
    SharedChatSessionSupervisor,
    SharedMemberManifest,
    SharedSessionDescriptor,
    SharedSessionLease,
)
from cloud_agent_host.tool_windows import ToolMark
from cloud_agent_host.workspace_broker import SharedSandboxIdentityRequest, create_app
from cloud_agent_shared import operation_profiling
from cloud_agent_shared.errors import SandboxArchiveBlocked
from cloud_agent_shared.sandbox_archive_settings import SandboxArchivePolicy


class _Manager:
    def __init__(self) -> None:
        self.backend = SimpleNamespace(
            uid=10001,
            gid=10001,
            image="sha256:image",
            runtime="runc",
        )
        self.items: dict[str, ChatSandboxInspection] = {}
        self.events: list[object] = []
        self.starts: list[str] = []
        self.stop_count = 0
        # The host-rule check: how many of the next checks fail, and how many ran.
        self.host_rule_failures = 0
        self.host_rule_checks = 0
        self.pruned_specs: list[ChatSandboxSpec] = []
        self.process_baseline: tuple[tuple[int, int], ...] = ((41, 400),)
        self.initial_archive_marker: bytes | None = None

    @staticmethod
    def _inspection(spec: ChatSandboxSpec, *, running: bool = True) -> ChatSandboxInspection:
        return ChatSandboxInspection(
            container_id="container-shared",
            name=spec.container_name,
            status=ChatSandboxStatus.RUNNING if running else ChatSandboxStatus.STOPPED,
            running=running,
            image=spec.sandbox_image_digest or "sha256:image",
            labels=spec.docker_labels(),
            mounts=(),
            networks=("agent-net",),
        )

    def create(
        self,
        spec: ChatSandboxSpec,
        *,
        facts: Any = None,
        observer: Any = None,
    ) -> ChatSandboxInspection:
        created = spec.container_name not in self.items
        # Instrumented like the real manager, so a test can exercise the stage
        # path rather than only the action path.
        with operation_profiling.substage(observer, "container_create"):
            inspection = self.items.setdefault(spec.container_name, self._inspection(spec))
        if created and facts is not None:
            # Mirrors the real manager: only a container this call actually
            # made is reported as created.
            facts.note("create")
        return inspection

    def require_existing(
        self, spec: ChatSandboxSpec, *, container_id: str
    ) -> ChatSandboxInspection:
        current = self.items[spec.container_name]
        assert current.container_id == container_id
        return current

    def inspect(self, container_name: str) -> ChatSandboxInspection | None:
        return self.items.get(container_name)

    def list(self) -> list[ChatSandboxInspection]:
        return list(self.items.values())

    def network_for(self, _spec: ChatSandboxSpec) -> str:
        return "agent-net"

    def read_stopped_final_archive_marker_optional(
        self,
        _spec: ChatSandboxSpec,
        *,
        expected_container_id: str,
    ) -> bytes | None:
        del expected_container_id
        return None

    def read_running_final_archive_marker(
        self,
        _spec: ChatSandboxSpec,
        *,
        expected_container_id: str,
    ) -> bytes | None:
        del expected_container_id
        return None

    def read_initial_archive_marker(
        self,
        spec: ChatSandboxSpec,
        *,
        expected_container_id: str,
    ) -> bytes | None:
        assert self.items[spec.container_name].container_id == expected_container_id
        return self.initial_archive_marker

    def start(
        self,
        spec: ChatSandboxSpec,
        *,
        facts: Any = None,
        observer: Any = None,
        expected_container_id: str | None = None,
        defer_network_release_on_fail_stop: bool = False,
        before_stopped_start=None,
    ) -> ChatSandboxInspection:
        del defer_network_release_on_fail_stop
        self.starts.append(spec.container_name)
        current = self.items[spec.container_name]
        if expected_container_id is not None:
            assert current.container_id == expected_container_id
        if not current.running:
            if before_stopped_start is not None:
                before_stopped_start(current)
            with operation_profiling.substage(observer, "container_start"):
                current = replace(
                    current,
                    running=True,
                    status=ChatSandboxStatus.RUNNING,
                )
                self.items[spec.container_name] = current
            if facts is not None:
                facts.note("start")
        elif facts is not None:
            with operation_profiling.substage(observer, "service_hosts_sync"):
                facts.note("reuse")
        return current

    def stop(
        self,
        spec: ChatSandboxSpec,
        *,
        expected_container_id: str | None = None,
        release_networks: bool = True,
    ) -> ChatSandboxInspection:
        self.stop_count += 1
        if expected_container_id is not None:
            assert self.items[spec.container_name].container_id == expected_container_id
        current = replace(
            self.items[spec.container_name],
            running=False,
            status=ChatSandboxStatus.STOPPED,
            networks=() if release_networks else self.items[spec.container_name].networks,
        )
        self.items[spec.container_name] = current
        return current

    def release_stopped_networks(
        self,
        spec: ChatSandboxSpec,
        *,
        expected_container_id: str,
    ) -> ChatSandboxInspection:
        current = self.items[spec.container_name]
        assert current.container_id == expected_container_id
        assert current.running is False
        current = replace(current, networks=())
        self.items[spec.container_name] = current
        return current

    def prune_orphan_networks(self, specs: list[ChatSandboxSpec]) -> None:
        self.pruned_specs = specs

    def verify_host_rules(self) -> None:
        self.host_rule_checks += 1
        if self.host_rule_failures:
            self.host_rule_failures -= 1
            raise SandboxError("inet cloud_agent_x is missing chain 'from_group'")

    def freeze_shared_writers(
        self,
        spec: ChatSandboxSpec,
        *,
        slot_ids: list[int],
        token: str,
        expected_container_id: str | None = None,
    ) -> str:
        if expected_container_id is not None:
            assert self.items[spec.container_name].container_id == expected_container_id
        self.events.append(("freeze", tuple(sorted(slot_ids))))
        return f"/run/agent-activation/snapshot-{token}.pids"

    def thaw_shared_writers(
        self,
        spec: ChatSandboxSpec,
        *,
        slot_ids: list[int],
        manifest: str,
        expected_container_id: str | None = None,
    ) -> None:
        if expected_container_id is not None:
            assert self.items[spec.container_name].container_id == expected_container_id
        self.events.append(("thaw", tuple(sorted(slot_ids)), manifest))

    def capture_member_processes(self, spec: ChatSandboxSpec, **kwargs):
        self.events.append(("census", kwargs["slot_id"]))
        return self.process_baseline

    def reap_member_processes_except(self, spec: ChatSandboxSpec, **kwargs) -> None:
        self.events.append(("reap-except", kwargs["slot_id"], kwargs["baseline"]))

    def normalize_member_files(self, spec: ChatSandboxSpec, **kwargs) -> None:
        self.events.append(("normalize", kwargs["slot_id"]))

    def begin_guest_observation(
        self,
        spec: ChatSandboxSpec,
        *,
        user_uid: int,
        measurement_id: str | None = None,
    ) -> GuestObserverCursor:
        self.events.append(("observe-begin", user_uid))
        return GuestObserverCursor(
            uid=user_uid,
            available=True,
            measurement_id=measurement_id,
        )

    def member_window_request(self, spec: ChatSandboxSpec, argv: list[str], *, timeout_s: float):
        """Accept the fixed window verbs without a real guest helper."""
        self.events.append(("member-window", argv[1]))
        return subprocess.CompletedProcess(args=argv, returncode=0, stdout="", stderr="")

    def finish_guest_observation(
        self, spec: ChatSandboxSpec, cursor: GuestObserverCursor | None
    ) -> GuestObservation:
        self.events.append(("observe-finish", None if cursor is None else cursor.uid))
        return GuestObservation(
            source="kata-guest-ebpf-v1",
            complete=True,
            lost_events=0,
            truncated=False,
        )


class _Turn:
    def __init__(self) -> None:
        self.interrupted = False
        self.questions: list[dict[str, Any]] = []
        self.question_answers: list[tuple[str, dict[str, list[str]], bool]] = []

    def interrupt(self) -> None:
        self.interrupted = True

    def pending_questions(self) -> list[dict[str, Any]]:
        return self.questions

    def answer_question(
        self, request_id: str, answers: dict[str, list[str]], *, reject: bool = False
    ) -> InputOutcome:
        if all(question["request_id"] != request_id for question in self.questions):
            return InputOutcome(False, "refused")
        self.question_answers.append((request_id, answers, reject))
        self.questions = [
            question for question in self.questions if question["request_id"] != request_id
        ]
        return InputOutcome(True)


class _Transport:
    retire_after_turn = False

    def __init__(self, launch: ResidentLaunch) -> None:
        self.launch = launch
        self.native_session_id = launch.native_session_id or "native-child"
        self.alive = True
        self.turns: list[_Turn] = []

    def compatible_with(self, launch: ResidentLaunch) -> bool:
        # Mirror ResidentTransport: the renewable credential and its generation
        # are not ordinary config equality. The supervisor must separately
        # force a restart when the residency fence rotates.
        return (
            replace(
                self.launch,
                credential=launch.credential,
                residency_generation=launch.residency_generation,
            )
            == launch
        )

    def close(self) -> None:
        self.alive = False

    def start_turn(self, **_kwargs) -> _Turn:
        turn = _Turn()
        self.turns.append(turn)
        return turn


class _TransportFactory:
    def __init__(self) -> None:
        self.opened: list[ResidentLaunch] = []
        self.transports: list[_Transport] = []

    def open(self, spec: ChatSandboxSpec, launch: ResidentLaunch) -> _Transport:
        self.opened.append(launch)
        transport = _Transport(launch)
        self.transports.append(transport)
        return transport

    def fork_native(self, *_args, **_kwargs) -> str:
        raise AssertionError("this test did not configure native fork behavior")


def _spec() -> ChatSandboxSpec:
    return ChatSandboxSpec(
        thread_id="athr_source",
        sandbox_id="asbx_shared",
        generation=1,
        runtime_id="claude-code",
        runtime_version="2.1.220",
        sandbox_image_digest="sha256:image",
        shared_live=True,
        resident_slot_capacity=4,
    )


def _launch(thread_id: str, *, native_session_id: str | None, slot: int) -> ResidentLaunch:
    return ResidentLaunch(
        thread_id=thread_id,
        runtime_id="claude-code",
        runtime_version="2.1.220",
        native_session_id=native_session_id,
        model="glm-5.1",
        reasoning_effort=None,
        gateway_base_url="http://gateway",
        credential="grant",
        slot_id=slot,
        residency_generation=slot + 1,
    )


def _members() -> list[SharedMemberManifest]:
    return [
        SharedMemberManifest(
            thread_id="athr_source",
            native_session_id="native-source",
            context_status="live",
            resident_slot_id=0,
            last_settled_job_id="ajob_source",
        ),
        SharedMemberManifest(
            thread_id="athr_peer",
            native_session_id="native-peer",
            context_status="live",
            resident_slot_id=1,
            last_settled_job_id="ajob_peer",
        ),
    ]


def _question_activation():
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
    supervisor.ensure_resident(
        "athr_source",
        lease.token,
        _launch("athr_source", native_session_id="native-source", slot=0),
    )
    return manager, supervisor, lease


def _pending_question() -> dict[str, Any]:
    return {
        "request_id": "native-request",
        "questions": [
            {
                "id": "choice",
                "header": "Approach",
                "question": "Which approach should I use?",
                "multiple": True,
                "options": [
                    {"label": "First", "description": "The first approach."},
                    {"label": "Second", "description": "The second approach."},
                ],
                "allow_custom": True,
            }
        ],
    }


def test_native_questions_require_the_active_owner_and_running_turn():
    _, supervisor, lease = _question_activation()
    answers = {"choice": ["First", "Custom response"]}
    for thread_id, owner in (
        ("athr_source", "attempt:1"),
        ("athr_peer", "attempt:1"),
        ("athr_unknown", "attempt:1"),
    ):
        assert supervisor.pending_questions(thread_id, owner=owner) == []
        assert supervisor.answer_question(
            thread_id, owner=owner, request_id="native-request", answers=answers
        ) == InputOutcome(False, "turn_ended")
    assert supervisor._held_inputs == {}

    turn = supervisor.start_turn(
        "athr_source", lease.token, prompt="work", model="glm-5.1", reasoning_effort=None
    )
    turn.questions = [_pending_question()]
    assert supervisor.pending_questions("athr_source", owner="attempt:2") == []
    assert supervisor.answer_question(
        "athr_source", owner="attempt:2", request_id="native-request", answers=answers
    ) == InputOutcome(False, "turn_ended")
    assert turn.question_answers == []
    assert supervisor.pending_questions("athr_source", owner="attempt:1") == [_pending_question()]
    assert supervisor.answer_question(
        "athr_source", owner="attempt:1", request_id="native-request", answers=answers
    ) == InputOutcome(True)
    assert turn.question_answers == [("native-request", answers, False)]
    assert turn.question_answers[0][1] is answers
    assert supervisor.pending_questions("athr_source", owner="attempt:1") == []
    assert supervisor._held_inputs == {}

    supervisor.complete_turn("athr_source", lease.token, turn)
    assert supervisor.answer_question(
        "athr_source", owner="attempt:1", request_id="native-request", answers=answers
    ) == InputOutcome(False, "turn_ended")


def test_native_answers_and_rejections_never_reach_a_replacement_attempt():
    _, supervisor, lease = _question_activation()
    turn = supervisor.start_turn(
        "athr_source", lease.token, prompt="work", model="glm-5.1", reasoning_effort=None
    )
    turn.questions = [_pending_question()]
    assert supervisor.answer_question(
        "athr_source", owner="attempt:1", request_id="native-request", answers={}, reject=True
    ) == InputOutcome(True)
    assert turn.question_answers == [("native-request", {}, True)]
    supervisor.abandon("athr_source", lease.token, lost=False)
    assert supervisor.pending_questions("athr_source", owner="attempt:1") == []
    assert supervisor.answer_question(
        "athr_source", owner="attempt:1", request_id="native-request", answers={"choice": ["First"]}
    ) == InputOutcome(False, "turn_ended")

    next_lease = supervisor.acquire(
        _spec(),
        thread_id="athr_source",
        owner="attempt:2",
        activation_generation=2,
        has_settled_context=True,
        manifest=_members(),
    )
    supervisor.ensure_resident(
        "athr_source",
        next_lease.token,
        _launch("athr_source", native_session_id="native-source", slot=0),
    )
    next_turn = supervisor.start_turn(
        "athr_source", next_lease.token, prompt="continue", model="glm-5.1", reasoning_effort=None
    )
    next_turn.questions = [_pending_question()]
    assert supervisor.answer_question(
        "athr_source", owner="attempt:1", request_id="native-request", answers={}, reject=True
    ) == InputOutcome(False, "turn_ended")
    assert next_turn.question_answers == []
    assert supervisor._held_inputs == {}


def test_native_answer_delivery_holds_the_owner_fence_against_expiry(monkeypatch):
    manager, supervisor, lease = _question_activation()
    turn = supervisor.start_turn(
        "athr_source", lease.token, prompt="work", model="glm-5.1", reasoning_effort=None
    )
    entered = threading.Event()
    release = threading.Event()
    stop_started = threading.Event()
    outcomes: list[InputOutcome] = []
    errors: list[BaseException] = []

    def reply(request_id, answers, *, reject=False):
        entered.set()
        assert release.wait(timeout=2)
        return InputOutcome(True)

    monkeypatch.setattr(turn, "answer_question", reply)

    def answer():
        try:
            outcomes.append(
                supervisor.answer_question(
                    "athr_source", owner="attempt:1", request_id="native-request", answers={}
                )
            )
        except BaseException as exc:
            errors.append(exc)

    def expire():
        stop_started.set()
        try:
            supervisor.stop_if_idle("athr_source", generation=1, reason="orphaned_attempt")
        except BaseException as exc:
            errors.append(exc)

    answering = threading.Thread(target=answer)
    stopping = threading.Thread(target=expire)
    answering.start()
    try:
        assert entered.wait(timeout=1)
        stopping.start()
        assert stop_started.wait(timeout=1)
        assert manager.stop_count == 0
    finally:
        release.set()
        answering.join(timeout=2)
        if stopping.ident is not None:
            stopping.join(timeout=2)
    assert not answering.is_alive() and not stopping.is_alive()
    assert errors == []
    assert outcomes == [InputOutcome(True)]
    assert manager.stop_count == 1
    assert supervisor.pending_questions("athr_source", owner="attempt:1") == []
    assert supervisor.answer_question(
        "athr_source", owner="attempt:1", request_id="native-request", answers={}
    ) == InputOutcome(False, "turn_ended")


@pytest.mark.parametrize("disk_limit_mib", [0, 16384])
def test_broker_reconcile_blocks_an_unmarked_running_orphan_before_accepting_requests(
    disk_limit_mib,
):
    manager = _Manager()
    spec = replace(_spec(), disk_limit_mib=disk_limit_mib)
    manager.items[spec.container_name] = manager._inspection(spec, running=True)
    archived: list[tuple] = []
    markerless_checks: list[bool] = []
    supervisor = SharedChatSessionSupervisor(
        manager,
        markerless_final_prepare_hook=lambda _spec, inspection: (
            markerless_checks.append(inspection.running) or False
        ),
        stopped_archive_hook=lambda *args: archived.append(args),
    )

    descriptors = supervisor.reconcile()
    supervisor.reconcile()

    assert manager.stop_count == 1
    assert archived == []
    assert manager.items[spec.container_name].running is False
    assert manager.items[spec.container_name].networks == ("agent-net",)
    assert manager.pruned_specs == [spec]
    assert markerless_checks == [True]
    assert descriptors[0].running is False
    with pytest.raises(SessionBusy, match="native flush failure"):
        supervisor.operation_opened(spec.thread_id)


@pytest.mark.parametrize("running", [True, False])
def test_disabled_policy_reconcile_cleans_a_markerless_orphan(running: bool) -> None:
    class MarkerManager(_Manager):
        def __init__(self) -> None:
            super().__init__()
            self.marker: bytes | None = None
            self.running_marker_writes = 0
            self.stopped_marker_writes = 0
            self.marker_clears = 0

        def read_running_final_archive_marker(self, spec, *, expected_container_id):
            assert self.items[spec.container_name].container_id == expected_container_id
            return self.marker

        def read_stopped_final_archive_marker_optional(
            self,
            spec,
            *,
            expected_container_id,
        ):
            assert self.items[spec.container_name].container_id == expected_container_id
            return self.marker

        def read_final_archive_marker(self, spec, *, expected_container_id):
            assert self.items[spec.container_name].container_id == expected_container_id
            if self.marker is None:
                raise SandboxError("final archive marker is missing")
            return self.marker

        def write_final_archive_marker(self, spec, payload, *, expected_container_id) -> None:
            current = self.items[spec.container_name]
            assert current.container_id == expected_container_id
            assert current.running is True
            self.marker = payload
            self.running_marker_writes += 1

        def write_stopped_final_archive_marker(
            self,
            spec,
            payload,
            *,
            expected_container_id,
        ) -> None:
            current = self.items[spec.container_name]
            assert current.container_id == expected_container_id
            assert current.running is False
            self.marker = payload
            self.stopped_marker_writes += 1

        def clear_final_archive_marker(self, spec, *, expected_container_id) -> None:
            assert self.items[spec.container_name].container_id == expected_container_id
            self.marker = None
            self.marker_clears += 1

    manager = MarkerManager()
    spec = _spec()
    manager.items[spec.container_name] = manager._inspection(spec, running=running)
    controller = SandboxArchiveController(
        manager,  # type: ignore[arg-type]
        namespace="pool-east",
    )
    controller.start()
    controller.apply(SandboxArchivePolicy(enabled=False, revision=1).as_dict())
    supervisor = SharedChatSessionSupervisor(
        manager,
        stopped_archive_prepare_hook=controller.prepare_final,
        stopped_archive_invalidate_hook=controller.invalidate_final,
        starting_archive_prepare_hook=controller.prepare_start,
        started_archive_hook=controller.complete_start,
        markerless_final_prepare_hook=controller.prepare_markerless_final_if_disabled,
        stopped_archive_hook=controller.queue_final,
    )

    descriptors = supervisor.reconcile()

    assert descriptors[0].running is False
    assert manager.items[spec.container_name].networks == ()
    assert manager.stop_count == 2
    assert manager.running_marker_writes == int(running)
    assert manager.stopped_marker_writes == int(not running)
    assert manager.marker is not None
    group = supervisor._members[spec.thread_id]
    assert group.stop_archive_pending is False
    assert group.stop_archive_blocked is False

    lease = supervisor.acquire(
        spec,
        thread_id=spec.thread_id,
        owner="attempt:1",
        activation_generation=1,
        has_settled_context=True,
        manifest=_members(),
        expected_container_id="container-shared",
    )

    assert lease.token
    assert manager.items[spec.container_name].running is True
    assert manager.marker is None
    assert manager.marker_clears == 1
    controller.close()


def test_reconcile_does_not_finalize_a_registered_never_started_container() -> None:
    manager = _Manager()
    spec = _spec()
    manager.items[spec.container_name] = replace(
        manager._inspection(spec, running=False),
        status=ChatSandboxStatus.CREATED,
    )
    archive_calls: list[tuple] = []
    supervisor = SharedChatSessionSupervisor(
        manager,
        stopped_archive_hook=lambda *args: archive_calls.append(args),
    )
    supervisor.register_manifest(spec, _members())

    supervisor.reconcile()
    lease = supervisor.acquire(
        spec,
        thread_id="athr_source",
        owner="attempt:1",
        activation_generation=1,
        has_settled_context=True,
        manifest=_members(),
        expected_container_id="container-shared",
    )

    assert lease.token
    assert archive_calls == []
    assert manager.stop_count == 0
    assert manager.items[spec.container_name].running is True


def test_unreadable_orphan_marker_isolated_while_healthy_groups_reconcile() -> None:
    class MarkerManager(_Manager):
        def __init__(self) -> None:
            super().__init__()
            self.fail_bad_marker = True

        def read_running_final_archive_marker(self, spec, **_kwargs):
            if spec.sandbox_id == "asbx_bad" and self.fail_bad_marker:
                raise SandboxError("marker read failed")
            return None

    manager = MarkerManager()
    bad = replace(_spec(), thread_id="athr_bad", sandbox_id="asbx_bad")
    good = replace(_spec(), thread_id="athr_good", sandbox_id="asbx_good")
    manager.items[bad.container_name] = manager._inspection(bad, running=True)
    manager.items[good.container_name] = replace(
        manager._inspection(good, running=False),
        status=ChatSandboxStatus.CREATED,
    )
    archived: list[str] = []
    supervisor = SharedChatSessionSupervisor(
        manager,
        stopped_archive_hook=lambda spec, *_args: archived.append(spec.sandbox_id),
    )

    descriptors = supervisor.reconcile()

    assert {item.thread_id for item in descriptors} == {"athr_bad", "athr_good"}
    assert manager.items[bad.container_name].running is False
    assert manager.items[bad.container_name].networks == ("agent-net",)
    with pytest.raises(SessionBusy, match="orphan finalization"):
        supervisor.operation_opened("athr_bad")

    manager.fail_bad_marker = False
    with pytest.raises(SandboxError, match="shutdown stop/archive"):
        supervisor.stop_all_for_shutdown()
    descriptors = supervisor.reconcile()

    assert archived == []
    assert {item.thread_id for item in descriptors} == {"athr_bad", "athr_good"}
    assert manager.items[bad.container_name].running is False
    assert manager.items[bad.container_name].networks == ("agent-net",)
    with pytest.raises(SessionBusy, match="native flush failure"):
        supervisor.operation_opened("athr_bad")


def test_unreadable_running_orphan_stop_failure_cannot_become_a_clean_archive() -> None:
    class MarkerManager(_Manager):
        def __init__(self) -> None:
            super().__init__()
            self.fail_marker_once = True
            self.fail_stop_once = True

        def read_running_final_archive_marker(self, _spec, **_kwargs):
            if self.fail_marker_once:
                self.fail_marker_once = False
                raise SandboxError("marker read failed")
            return None

        def stop(self, spec, **kwargs):
            if self.fail_stop_once:
                self.fail_stop_once = False
                raise SandboxError("docker stop failed")
            return super().stop(spec, **kwargs)

    manager = MarkerManager()
    spec = _spec()
    manager.items[spec.container_name] = manager._inspection(spec, running=True)
    prepared: list[str] = []
    archived: list[str] = []
    supervisor = SharedChatSessionSupervisor(
        manager,
        stopped_archive_prepare_hook=lambda spec, _inspection: prepared.append(spec.sandbox_id),
        stopped_archive_hook=lambda spec, *_args: archived.append(spec.sandbox_id),
    )

    descriptors = supervisor.reconcile()

    assert [item.thread_id for item in descriptors] == [spec.thread_id]
    assert manager.items[spec.container_name].running is True
    assert prepared == []
    assert archived == []

    with pytest.raises(SandboxError, match="shutdown stop/archive"):
        supervisor.stop_all_for_shutdown()

    current = manager.items[spec.container_name]
    assert current.running is False
    assert current.networks == ("agent-net",)
    assert prepared == []
    assert archived == []

    with pytest.raises(SandboxError, match="blocked by native flush failure"):
        supervisor.stop_if_idle(spec.thread_id, generation=spec.generation)
    assert prepared == []
    assert archived == []


def test_malformed_orphan_marker_isolated_while_healthy_groups_reconcile() -> None:
    class MarkerManager(_Manager):
        def read_stopped_final_archive_marker_optional(self, spec, **_kwargs):
            return b"{malformed" if spec.sandbox_id == "asbx_bad" else None

        def read_running_final_archive_marker(self, spec, **_kwargs):
            return b"{malformed" if spec.sandbox_id == "asbx_bad" else None

    manager = MarkerManager()
    bad = replace(_spec(), thread_id="athr_bad", sandbox_id="asbx_bad")
    good = replace(_spec(), thread_id="athr_good", sandbox_id="asbx_good")
    manager.items[bad.container_name] = manager._inspection(bad, running=True)
    manager.items[good.container_name] = replace(
        manager._inspection(good, running=False),
        status=ChatSandboxStatus.CREATED,
    )
    prepare_attempts: list[str] = []
    archive_attempts: list[str] = []

    def prepare(spec, _inspection) -> None:
        prepare_attempts.append(spec.sandbox_id)
        raise SandboxError("invalid sandbox final archive marker")

    def archive(spec, _inspection, _members) -> None:
        archive_attempts.append(spec.sandbox_id)
        raise SandboxError("invalid sandbox final archive marker")

    supervisor = SharedChatSessionSupervisor(
        manager,
        stopped_archive_prepare_hook=prepare,
        stopped_archive_hook=archive,
    )

    descriptors = supervisor.reconcile()
    retried = supervisor.reconcile()

    assert {item.thread_id for item in descriptors} == {"athr_bad", "athr_good"}
    assert {item.thread_id for item in retried} == {"athr_bad", "athr_good"}
    assert prepare_attempts == ["asbx_bad"]
    assert archive_attempts == ["asbx_bad"]
    assert manager.items[bad.container_name].running is False
    assert manager.items[bad.container_name].networks == ("agent-net",)
    with pytest.raises(SessionBusy, match="final archive"):
        supervisor.operation_opened("athr_bad")


def test_durably_blocked_orphan_stop_failure_isolated_from_healthy_groups() -> None:
    class MarkerManager(_Manager):
        def __init__(self) -> None:
            super().__init__()
            self.fail_bad_stop_once = True

        def read_running_final_archive_marker(self, spec, **_kwargs):
            return b"durable-blocked-marker" if spec.sandbox_id == "asbx_bad" else None

        def stop(self, spec, **kwargs):
            if spec.sandbox_id == "asbx_bad" and self.fail_bad_stop_once:
                self.fail_bad_stop_once = False
                raise SandboxError("docker stop failed")
            return super().stop(spec, **kwargs)

    manager = MarkerManager()
    bad = replace(_spec(), thread_id="athr_bad", sandbox_id="asbx_bad")
    good = replace(_spec(), thread_id="athr_good", sandbox_id="asbx_good")
    manager.items[bad.container_name] = manager._inspection(bad, running=True)
    manager.items[good.container_name] = replace(
        manager._inspection(good, running=False),
        status=ChatSandboxStatus.CREATED,
    )
    archived: list[str] = []

    def prepare(spec, _inspection) -> None:
        if spec.sandbox_id == bad.sandbox_id:
            raise SandboxArchiveBlocked("durable marker forbids final archive")

    supervisor = SharedChatSessionSupervisor(
        manager,
        stopped_archive_prepare_hook=prepare,
        stopped_archive_hook=lambda spec, *_args: archived.append(spec.sandbox_id),
    )

    descriptors = supervisor.reconcile()

    assert {item.thread_id for item in descriptors} == {"athr_bad", "athr_good"}
    assert manager.items[bad.container_name].running is True
    assert archived == []
    with pytest.raises(SessionBusy, match="native flush failure"):
        supervisor.operation_opened("athr_bad")

    retried = supervisor.reconcile()

    assert {item.thread_id for item in retried} == {"athr_bad", "athr_good"}
    assert manager.items[bad.container_name].running is False
    assert manager.items[bad.container_name].networks == ("agent-net",)
    assert archived == []


def test_committed_retry_clears_a_transient_unreadable_marker_fence() -> None:
    class MarkerManager(_Manager):
        def __init__(self) -> None:
            super().__init__()
            self.fail_marker = True
            self.marker: bytes | None = b"durable-pending-marker"

        def read_stopped_final_archive_marker_optional(self, _spec, **_kwargs):
            if self.fail_marker:
                raise SandboxError("marker read failed")
            return self.marker

        def read_running_final_archive_marker(self, _spec, **_kwargs):
            return self.marker

    manager = MarkerManager()
    spec = _spec()
    manager.items[spec.container_name] = manager._inspection(spec, running=False)

    def archive(*_args) -> None:
        manager.marker = None

    supervisor = SharedChatSessionSupervisor(manager, stopped_archive_hook=archive)
    supervisor.reconcile()
    manager.fail_marker = False

    supervisor.stop_if_idle(spec.thread_id, generation=spec.generation)
    token, _ = supervisor.operation_opened(spec.thread_id)
    supervisor.reconcile()

    assert manager.items[spec.container_name].running is True
    supervisor.operation_closed(spec.thread_id, token)


def test_created_cleanup_clears_a_transient_unreadable_marker_fence() -> None:
    class MarkerManager(_Manager):
        def __init__(self) -> None:
            super().__init__()
            self.fail_marker = True

        def read_stopped_final_archive_marker_optional(self, _spec, **_kwargs):
            if self.fail_marker:
                raise SandboxError("marker read failed")
            return None

    manager = MarkerManager()
    spec = _spec()
    manager.items[spec.container_name] = replace(
        manager._inspection(spec, running=False),
        status=ChatSandboxStatus.CREATED,
    )
    supervisor = SharedChatSessionSupervisor(
        manager,
        stopped_archive_hook=lambda *_args: pytest.fail("CREATED sandbox must not upload"),
    )
    supervisor.register_manifest(
        spec,
        _members(),
        expected_container_id="container-shared",
    )
    with pytest.raises(SandboxError, match="marker read failed"):
        supervisor.stop_if_idle(spec.thread_id, generation=spec.generation)
    manager.fail_marker = False

    stopped = supervisor.stop_if_idle(spec.thread_id, generation=spec.generation)
    token, _ = supervisor.operation_opened(spec.thread_id)

    assert stopped.running is False
    assert manager.items[spec.container_name].running is True
    supervisor.operation_closed(spec.thread_id, token)


def test_explicit_stop_cleans_up_a_registered_never_started_container_without_archive() -> None:
    manager = _Manager()
    spec = _spec()
    manager.items[spec.container_name] = replace(
        manager._inspection(spec, running=False),
        status=ChatSandboxStatus.CREATED,
    )
    archive_calls: list[tuple] = []
    supervisor = SharedChatSessionSupervisor(
        manager,
        stopped_archive_hook=lambda *args: archive_calls.append(args),
    )
    supervisor.register_manifest(spec, _members())

    descriptor = supervisor.stop_if_idle(
        spec.thread_id,
        generation=spec.generation,
        reason="orphaned_attempt",
    )

    assert descriptor.running is False
    assert archive_calls == []
    assert manager.stop_count == 1
    assert manager.items[spec.container_name].networks == ()


@pytest.mark.parametrize("released_networks", [(), ("none",)])
def test_restart_reconcile_does_not_rearchive_an_already_released_stopped_group(
    released_networks,
):
    manager = _Manager()
    spec = _spec()
    manager.items[spec.container_name] = replace(
        manager._inspection(spec, running=False),
        networks=released_networks,
    )
    archived: list[tuple] = []
    supervisor = SharedChatSessionSupervisor(
        manager,
        stopped_archive_hook=lambda *args: archived.append(args),
    )

    descriptors = supervisor.reconcile()

    assert manager.stop_count == 0
    assert archived == []
    assert len(descriptors) == 1
    assert descriptors[0].running is False


def test_blocking_reconcile_archive_does_not_hold_global_lock_for_other_group_status():
    manager = _Manager()
    archived_entered = threading.Event()
    allow_archive = threading.Event()
    other = replace(_spec(), thread_id="athr_other", sandbox_id="asbx_other")

    def archive(spec, _inspection, _members):
        if spec.sandbox_id == other.sandbox_id:
            archived_entered.set()
            assert allow_archive.wait(timeout=2)

    supervisor = SharedChatSessionSupervisor(manager, stopped_archive_hook=archive)
    supervisor.register_manifest(_spec(), _members())
    manager.items[other.container_name] = manager._inspection(other, running=True)
    manager.read_running_final_archive_marker = (  # type: ignore[method-assign]
        lambda *_args, **_kwargs: b"durable-pending-marker"
    )
    errors: list[BaseException] = []

    def reconcile() -> None:
        try:
            supervisor.reconcile()
        except BaseException as exc:  # pragma: no cover - asserted below
            errors.append(exc)

    reconcile_thread = threading.Thread(target=reconcile)
    reconcile_thread.start()
    assert archived_entered.wait(timeout=1)

    status_done = threading.Event()

    def read_other_group() -> None:
        try:
            supervisor.status("athr_source")
        except BaseException as exc:  # pragma: no cover - asserted below
            errors.append(exc)
        finally:
            status_done.set()

    status_thread = threading.Thread(target=read_other_group)
    status_thread.start()
    assert status_done.wait(timeout=0.5), "archive I/O held the supervisor-wide lock"
    allow_archive.set()
    reconcile_thread.join(timeout=2)
    status_thread.join(timeout=2)

    assert not reconcile_thread.is_alive()
    assert not status_thread.is_alive()
    assert errors == []


def test_status_listing_never_stops_a_managed_running_group():
    manager = _Manager()
    spec = _spec()
    manager.items[spec.container_name] = manager._inspection(spec, running=True)
    supervisor = SharedChatSessionSupervisor(manager)
    supervisor.register_manifest(
        spec,
        _members(),
        expected_container_id="container-shared",
    )
    supervisor.acquire(
        spec,
        thread_id="athr_source",
        owner="attempt:1",
        activation_generation=1,
        has_settled_context=True,
        manifest=_members(),
        expected_container_id="container-shared",
    )

    descriptors = supervisor.list()

    assert manager.stop_count == 0
    assert manager.items[spec.container_name].running is True
    assert next(item for item in descriptors if item.thread_id == "athr_source").group_busy is True


def test_status_marks_a_same_name_running_replacement_lost_without_adopting_it():
    manager = _Manager()
    supervisor = SharedChatSessionSupervisor(manager)
    supervisor.register_manifest(_spec(), _members())
    original = manager.items[_spec().container_name]
    manager.items[_spec().container_name] = replace(
        original,
        container_id="container-replacement",
    )

    with pytest.raises(RetainedChatLost, match="identity changed"):
        supervisor.status("athr_source")

    group = supervisor._members["athr_source"]
    assert group.inspection.container_id == "container-shared"
    assert group.container_lost is True
    assert {member.context_status for member in group.members.values()} == {"lost"}
    assert manager.stop_count == 0


def test_reconcile_refuses_an_externally_stopped_replacement_without_archiving_it():
    manager = _Manager()
    archived: list[tuple] = []
    supervisor = SharedChatSessionSupervisor(
        manager,
        stopped_archive_hook=lambda *args: archived.append(args),
    )
    supervisor.register_manifest(_spec(), _members())
    original = manager.items[_spec().container_name]
    manager.items[_spec().container_name] = replace(
        original,
        container_id="container-replacement",
        running=False,
        status=ChatSandboxStatus.STOPPED,
    )

    with pytest.raises(RetainedChatLost, match="identity changed"):
        supervisor.list()

    group = supervisor._members["athr_source"]
    assert group.inspection.container_id == "container-shared"
    assert group.container_lost is True
    assert manager.stop_count == 0
    assert archived == []


def test_status_blocks_an_externally_stopped_group_without_a_final_marker():
    manager = _Manager()
    spec = _spec()
    manager.items[spec.container_name] = manager._inspection(spec, running=True)
    archived: list[tuple] = []
    supervisor = SharedChatSessionSupervisor(
        manager,
        stopped_archive_hook=lambda *args: archived.append(args),
    )
    supervisor.register_manifest(
        spec,
        _members(),
        expected_container_id="container-shared",
    )
    manager.items[spec.container_name] = manager._inspection(spec, running=False)

    descriptors = supervisor.list()

    assert manager.stop_count == 0
    assert archived == []
    assert manager.items[spec.container_name].networks == ("agent-net",)
    assert all(item.running is False for item in descriptors)
    with pytest.raises(SessionBusy, match="native flush failure"):
        supervisor.operation_opened("athr_source")


def test_acquire_starts_a_settled_context_on_its_group():
    manager = _Manager()
    supervisor = SharedChatSessionSupervisor(manager)
    supervisor.register_manifest(_spec(), _members())

    supervisor.acquire(
        _spec(),
        thread_id="athr_source",
        owner="attempt:1",
        activation_generation=1,
        has_settled_context=True,
        manifest=_members(),
        expected_container_id="container-shared",
    )

    assert manager.starts == [_spec().container_name]


def test_a_lease_claims_a_bridge_proof_only_for_a_call_that_made_one():
    """The runner's commands rely on it for their turn, so a repeat must not claim it."""
    manager = _Manager()
    supervisor = SharedChatSessionSupervisor(manager)
    supervisor.register_manifest(_spec(), _members())

    def acquire() -> SharedSessionLease:
        return supervisor.acquire(
            _spec(),
            thread_id="athr_source",
            owner="attempt:1",
            activation_generation=1,
            has_settled_context=True,
            manifest=_members(),
            expected_container_id="container-shared",
        )

    first = acquire()
    repeat = acquire()

    assert first.network_verified is True
    assert asdict(first)["network_verified"] is True, "what the broker endpoint sends"
    # The same owner is handed its existing lease without touching the group.
    assert repeat.token == first.token
    assert manager.starts == [_spec().container_name]
    assert repeat.network_verified is False


def _running_group(*, stopped_archive_hook=None) -> tuple[_Manager, SharedChatSessionSupervisor]:
    manager = _Manager()
    supervisor = SharedChatSessionSupervisor(
        manager,
        stopped_archive_hook=stopped_archive_hook,
    )
    supervisor.register_manifest(_spec(), _members())
    supervisor.acquire(
        _spec(),
        thread_id="athr_source",
        owner="attempt:1",
        activation_generation=1,
        has_settled_context=True,
        manifest=_members(),
        expected_container_id="container-shared",
    )
    return manager, supervisor


def test_a_host_that_cannot_show_its_rules_stops_every_running_group():
    """Fail closed: a group may not go on without the boundary it started behind."""
    archived: list[tuple] = []
    manager, supervisor = _running_group(stopped_archive_hook=lambda *args: archived.append(args))
    manager.host_rule_failures = 2

    stopped = supervisor.guard_network()

    assert manager.host_rule_checks == 2
    assert manager.stop_count == 2
    assert len(archived) == 1
    assert manager.items[_spec().container_name].running is False
    # Stopped, not lost: the native session wakes once the rules are back.
    assert [descriptor.resident_state for descriptor in stopped] == ["stopped"]
    assert supervisor.status("athr_source").context_status != "lost"


def test_guest_network_fail_stop_archives_once_and_does_not_duplicate_on_listing():
    class FailStoppedStartManager(_Manager):
        def start(
            self,
            spec,
            *,
            facts=None,
            observer=None,
            expected_container_id=None,
            defer_network_release_on_fail_stop=False,
        ):
            stopped = self.stop(
                spec,
                expected_container_id=expected_container_id,
                release_networks=not defer_network_release_on_fail_stop,
            )
            raise RetainedChatFailStopped("guest network proof failed", stopped)

    manager = FailStoppedStartManager()
    archived: list[tuple] = []
    supervisor = SharedChatSessionSupervisor(
        manager,
        stopped_archive_hook=lambda *args: archived.append(args),
    )
    supervisor.register_manifest(_spec(), _members())

    with pytest.raises(RetainedChatFailStopped, match="guest network proof failed"):
        supervisor.acquire(
            _spec(),
            thread_id="athr_source",
            owner="attempt:1",
            activation_generation=1,
            has_settled_context=True,
            manifest=_members(),
            expected_container_id="container-shared",
        )

    assert manager.stop_count == 2
    assert len(archived) == 1
    assert archived[0][1].running is False
    supervisor.list()
    assert len(archived) == 1


def test_one_failed_check_is_retried_before_anything_is_stopped():
    manager, supervisor = _running_group()
    manager.host_rule_failures = 1

    assert supervisor.guard_network() == []
    assert manager.host_rule_checks == 2
    assert manager.stop_count == 0


def test_the_rules_are_not_checked_while_nothing_runs():
    manager = _Manager()
    supervisor = SharedChatSessionSupervisor(manager)

    assert supervisor.guard_network() == []
    assert manager.host_rule_checks == 0


def test_stale_activation_does_not_wake_a_stopped_shared_container():
    manager = _Manager()
    spec = _spec()
    manager.create(spec)
    manager.stop(spec)
    members = [
        replace(member, activation_generation=2) if member.thread_id == "athr_source" else member
        for member in _members()
    ]
    supervisor = SharedChatSessionSupervisor(manager)

    with pytest.raises(SessionLeaseLost, match="activation generation is stale"):
        supervisor.acquire(
            spec,
            thread_id="athr_source",
            owner="attempt:stale",
            activation_generation=1,
            has_settled_context=True,
            manifest=members,
            expected_container_id="container-shared",
        )

    assert manager.starts == []
    assert manager.items[spec.container_name].running is False


def test_invalid_terminal_close_keeps_reference_for_a_valid_retry():
    manager = _Manager()
    supervisor = SharedChatSessionSupervisor(
        manager,
        idle_timeout_s=30,
        clock=lambda: 100.0,
    )
    supervisor.register_manifest(_spec(), _members())
    token, opened = supervisor.terminal_opened("athr_source")
    assert opened.terminal_count == 1

    with pytest.raises(ValueError, match="timestamp is invalid"):
        supervisor.terminal_closed(
            "athr_source",
            token,
            last_activity_monotonic=float("inf"),
        )

    retained = supervisor.status("athr_source")
    assert retained.terminal_count == 1
    assert retained.idle_deadline_monotonic is None

    closed = supervisor.terminal_closed(
        "athr_source",
        token,
        last_activity_monotonic=90.0,
    )
    assert closed.terminal_count == 0
    assert closed.idle_deadline_monotonic == 120.0


def test_a_changed_idle_timeout_arms_the_next_quiet_moment_with_the_new_value():
    manager = _Manager()
    supervisor = SharedChatSessionSupervisor(manager, idle_timeout_s=30, clock=lambda: 100.0)
    supervisor.register_manifest(_spec(), _members())
    token, _ = supervisor.terminal_opened("athr_source")

    supervisor.set_idle_timeout_s(600)
    closed = supervisor.terminal_closed("athr_source", token, last_activity_monotonic=90.0)

    assert supervisor.idle_timeout_s == 600
    assert closed.idle_deadline_monotonic == 690.0
    with pytest.raises(ValueError, match="must be positive"):
        supervisor.set_idle_timeout_s(0)
    assert supervisor.idle_timeout_s == 600


def test_native_fork_journal_uses_the_docker_layer_owner_not_capabilityless_root():
    class JournalManager:
        def __init__(self) -> None:
            self.backend = SimpleNamespace(uid=10001, gid=10001)
            self.calls: list[tuple[list[str], dict]] = []

        def exec_capture(self, _spec, argv, **kwargs):
            self.calls.append((list(argv), kwargs))
            return subprocess.CompletedProcess([], 0, stdout=b"", stderr=b"")

    manager = JournalManager()
    factory = ResidentTransportFactory(manager)  # type: ignore[arg-type]

    factory._write_fork_journal(_spec(), "fork-one", {"state": "preparing"})
    assert factory._read_fork_journal(_spec(), "fork-one") is None

    assert [call[1]["user_uid"] for call in manager.calls] == [10001, 10001]
    assert [call[1]["user_gid"] for call in manager.calls] == [10001, 10001]
    assert all(
        "/home/agent/.freeinference-platform/session-forks" in " ".join(call[0])
        for call in manager.calls
    )


def test_claude_member_launch_shares_only_native_projects(monkeypatch, tmp_path):
    calls: list[tuple[list[str], dict]] = []
    manager = _Manager()
    manager.backend.base_env = lambda: {}

    def capture(_spec, argv, **kwargs):
        calls.append((list(argv), kwargs))
        return subprocess.CompletedProcess([], 0, stdout=b"", stderr=b"")

    monkeypatch.setattr(manager, "exec_capture", capture, raising=False)
    factory = ResidentTransportFactory(manager)  # type: ignore[arg-type]
    source = _launch("athr_source", native_session_id="native-source", slot=0)
    child = _launch("athr_child", native_session_id="native-child", slot=1)
    factory._prepare_claude_member_state(_spec(), source)
    factory._prepare_claude_member_state(_spec(), child)

    assert [call[0][-2] for call in calls] == [
        claude_member_config_dir(0, source.thread_id),
        claude_member_config_dir(1, child.thread_id),
    ]
    assert [call[1]["user_uid"] for call in calls] == [MEMBER_UID_BASE, MEMBER_UID_BASE + 1]

    shared = tmp_path / "agent-state" / "claude"
    shared.mkdir(parents=True)
    (shared / "settings.json").write_text('{"env":{"ANTHROPIC_BASE_URL":"malicious"}}')
    projects = shared / "projects"
    projects.mkdir()
    native = projects / "native-source.jsonl"
    native.write_text('{"type":"assistant"}\n')
    for slot in (0, 1):
        home = tmp_path / f"member-{slot}"
        home.mkdir()
        config = home / ".claude-members" / f"member-{slot}"
        # Exercise the actual preparation script twice, as a native resume
        # after broker/container restart would do, against legacy state.
        for _ in range(2):
            subprocess.run(
                ["python3", "-c", calls[slot][0][2], str(config), str(projects)], check=True
            )
        assert home.stat().st_mode & 0o777 == 0o700
        assert config.stat().st_mode & 0o777 == 0o700
        assert (config / "projects" / "native-source.jsonl").read_text() == native.read_text()
        assert not (config / "settings.json").exists()
        assert not (config / "sessions").exists()

    context = factory._launch_context(_spec(), child, credential="child-grant")
    assert context.claude_config_dir == claude_member_config_dir(1, child.thread_id)
    assert context.base_env["HOME"] == "/home/agent-member-1"


def test_claude_reassigned_slot_gets_fresh_member_settings(monkeypatch, tmp_path):
    manager = _Manager()
    calls: list[list[str]] = []

    def capture(_spec, argv, **_kwargs):
        calls.append(list(argv))
        return subprocess.CompletedProcess([], 0, stdout=b"", stderr=b"")

    monkeypatch.setattr(manager, "exec_capture", capture, raising=False)
    factory = ResidentTransportFactory(manager)  # type: ignore[arg-type]
    old = _launch("athr_old", native_session_id="old", slot=1)
    new = _launch("athr_new", native_session_id="new", slot=1)
    assert claude_member_config_dir(1, old.thread_id) != claude_member_config_dir(1, new.thread_id)
    factory._prepare_claude_member_state(_spec(), old)
    factory._prepare_claude_member_state(_spec(), new)
    home = tmp_path / "member-home"
    home.mkdir()
    shared = tmp_path / "agent-state" / "claude"
    shared.mkdir(parents=True)
    projects = shared / "projects"
    for launch, call in zip((old, new), calls, strict=True):
        config = home / ".claude-members" / call[-2].rsplit("/", 1)[-1]
        subprocess.run(["python3", "-c", call[2], str(config), str(projects)], check=True)
        if launch == old:
            (config / "settings.json").write_text('{"env":{"ANTHROPIC_BASE_URL":"old"}}')
        else:
            assert not (config / "settings.json").exists()


def test_claude_slot_is_reaped_before_new_member_launch_state(monkeypatch):
    manager = _Manager()
    manager.backend.base_env = lambda: {}
    factory = ResidentTransportFactory(manager)  # type: ignore[arg-type]
    events: list[str] = []
    contexts: list[Any] = []
    monkeypatch.setattr(
        manager,
        "reap_member_processes",
        lambda *_args, **_kwargs: events.append("reap"),
        raising=False,
    )
    monkeypatch.setattr(
        factory, "_prepare_claude_member_state", lambda *_args: events.append("prepare")
    )
    monkeypatch.setattr(
        manager, "spawn", lambda *_args, **_kwargs: events.append("spawn"), raising=False
    )

    def launch_live(context):
        contexts.append(context)
        return SimpleNamespace(
            ensure_started=lambda **_kwargs: context.spawn_live(["claude"], {}),
            process_alive=lambda: True,
        )

    monkeypatch.setattr(
        "cloud_agent_host.resident_transport.get_driver",
        lambda _runtime: SimpleNamespace(launch_live=launch_live),
    )

    factory.open(_spec(), _launch("athr_new", native_session_id=None, slot=1))

    assert events == ["reap", "prepare", "spawn"]
    assert contexts[0].claude_config_dir == claude_member_config_dir(1, "athr_new")


@pytest.mark.parametrize("target", ["config", "history"])
def test_claude_member_preparation_rejects_symlinked_state(monkeypatch, tmp_path, target):
    manager = _Manager()
    calls: list[list[str]] = []

    def capture(_spec, argv, **_kwargs):
        calls.append(list(argv))
        return subprocess.CompletedProcess([], 0, stdout=b"", stderr=b"")

    monkeypatch.setattr(manager, "exec_capture", capture, raising=False)
    factory = ResidentTransportFactory(manager)  # type: ignore[arg-type]
    factory._prepare_claude_member_state(
        _spec(), _launch("athr_source", native_session_id="native-source", slot=0)
    )
    home = tmp_path / "member"
    home.mkdir()
    config = home / ".claude-members" / "member-id"
    config.parent.mkdir()
    shared = tmp_path / "shared" / "claude"
    shared.mkdir(parents=True)
    projects = shared / "projects"
    attacker = tmp_path / "attacker"
    attacker.mkdir()
    (config if target == "config" else projects).symlink_to(attacker, target_is_directory=True)

    result = subprocess.run(
        ["python3", "-c", calls[0][2], str(config), str(projects)], capture_output=True
    )

    assert result.returncode != 0
    assert not list(attacker.iterdir())


def test_claude_native_fork_uses_the_source_private_config(monkeypatch):
    manager = _Manager()
    calls: list[tuple[list[str], dict]] = []

    def capture(_spec, argv, **kwargs):
        calls.append((list(argv), kwargs))
        return subprocess.CompletedProcess(
            [], 0, stdout=b'{"source_session_id":"source","session_id":"child"}', stderr=b""
        )

    monkeypatch.setattr(manager, "exec_capture", capture, raising=False)
    factory = ResidentTransportFactory(manager)  # type: ignore[arg-type]

    source_launch = _launch("athr_source", native_session_id="source", slot=1)
    assert factory._fork_claude(_spec(), "source", source_launch, 2) == "child"
    assert calls[0][0] == ["claude-session-fork", "source", "2"]
    assert calls[0][1]["env"]["CLAUDE_CONFIG_DIR"] == claude_member_config_dir(1, "athr_source")
    assert calls[0][1]["user_uid"] == MEMBER_UID_BASE + 1


@pytest.mark.parametrize(
    ("runtime_id", "collection"),
    [
        ("codex", "codex-members"),
        ("opencode", "opencode-members"),
        ("kilo", "kilo-members"),
    ],
)
def test_private_provider_members_get_deterministic_distinct_homes(
    runtime_id: str, collection: str
):
    source = _launch("athr_source", native_session_id="native-source", slot=0)
    child = replace(source, thread_id="athr_child", native_session_id="native-child", slot_id=1)

    source_home = ResidentTransportFactory._member_state_dir(replace(source, runtime_id=runtime_id))
    child_home = ResidentTransportFactory._member_state_dir(replace(child, runtime_id=runtime_id))

    assert source_home.startswith(f"/agent-state/{collection}/")
    assert child_home.startswith(f"/agent-state/{collection}/")
    assert source_home != child_home


def test_codex_preparing_journal_recovers_one_persisted_child_without_refork(monkeypatch):
    manager = _Manager()
    factory = ResidentTransportFactory(manager)  # type: ignore[arg-type]
    spec = replace(_spec(), runtime_id="codex", runtime_version="0.145.0")
    source_launch = replace(
        _launch("athr_source", native_session_id="native-source", slot=0),
        runtime_id="codex",
        runtime_version="0.145.0",
    )
    target_launch = replace(
        _launch("athr_child", native_session_id=None, slot=2),
        runtime_id="codex",
        runtime_version="0.145.0",
    )
    mutation_calls: list[str] = []
    source = SimpleNamespace(
        native_session_id="native-source",
        launch=source_launch,
        session=SimpleNamespace(
            fork_native=lambda **_kwargs: mutation_calls.append("fork") or "duplicate"
        ),
        close=lambda: mutation_calls.append("close"),
    )
    expected = {
        "operation_id": "fork-recover",
        "sandbox_id": spec.sandbox_id,
        "sandbox_generation": spec.generation,
        "runtime_id": "codex",
        "runtime_version": "0.145.0",
        "source_session_id": "native-source",
        "source_native_turn_no": 2,
        "target_thread_id": "athr_child",
        "target_slot_id": 2,
    }
    writes: list[dict[str, object]] = []
    clones: list[tuple[ResidentLaunch, ResidentLaunch]] = []
    monkeypatch.setattr(
        factory,
        "_read_fork_journal",
        lambda *_args: {
            **expected,
            "state": "preparing",
            "source_child_session_ids": ["native-old-child"],
        },
    )
    monkeypatch.setattr(
        factory,
        "_codex_fork_children",
        lambda *_args, **_kwargs: {"native-old-child", "native-recovered-child"},
    )
    monkeypatch.setattr(
        factory,
        "_clone_private_member_state",
        lambda _spec, source_item, target_item: clones.append((source_item, target_item)),
    )
    monkeypatch.setattr(
        factory,
        "_write_fork_journal",
        lambda _spec, _operation_id, value: writes.append(value),
    )

    child_id = factory.fork_native(
        spec,
        source,  # type: ignore[arg-type]
        "fork-recover",
        target_launch,
        2,
    )

    assert child_id == "native-recovered-child"
    assert mutation_calls == ["close"]
    assert clones == [(source_launch, target_launch)]
    assert writes == [{**expected, "state": "ready", "target_session_id": "native-recovered-child"}]


def test_codex_lost_fork_response_recovers_from_the_rollout_inventory(monkeypatch):
    manager = _Manager()
    factory = ResidentTransportFactory(manager)  # type: ignore[arg-type]
    spec = replace(_spec(), runtime_id="codex", runtime_version="0.145.0")
    source_launch = replace(
        _launch("athr_source", native_session_id="native-source", slot=0),
        runtime_id="codex",
        runtime_version="0.145.0",
    )
    target_launch = replace(
        _launch("athr_child", native_session_id=None, slot=2),
        runtime_id="codex",
        runtime_version="0.145.0",
    )
    closed: list[bool] = []

    def lost_response(**_kwargs):
        raise EOFError("response disappeared after persistence")

    source = SimpleNamespace(
        native_session_id="native-source",
        launch=source_launch,
        session=SimpleNamespace(fork_native=lost_response),
        close=lambda: closed.append(True),
    )
    inventories = iter(
        [
            {"native-old-child"},
            {"native-old-child", "native-recovered-child"},
        ]
    )
    writes: list[dict[str, object]] = []
    monkeypatch.setattr(factory, "_read_fork_journal", lambda *_args: None)
    monkeypatch.setattr(
        factory,
        "_codex_fork_children",
        lambda *_args, **_kwargs: next(inventories),
    )
    monkeypatch.setattr(factory, "_clone_private_member_state", lambda *_args: None)
    monkeypatch.setattr(
        factory,
        "_write_fork_journal",
        lambda _spec, _operation_id, value: writes.append(value),
    )

    child_id = factory.fork_native(
        spec,
        source,  # type: ignore[arg-type]
        "fork-lost-response",
        target_launch,
        2,
    )

    assert child_id == "native-recovered-child"
    assert closed == [True]
    assert writes[0]["state"] == "preparing"
    assert writes[0]["source_child_session_ids"] == ["native-old-child"]
    assert writes[1]["state"] == "ready"
    assert writes[1]["target_session_id"] == "native-recovered-child"


@pytest.mark.parametrize("runtime_id", ["opencode", "kilo"])
def test_http_native_fork_flushes_and_clones_private_provider_state(monkeypatch, runtime_id: str):
    manager = _Manager()
    factory = ResidentTransportFactory(manager)  # type: ignore[arg-type]
    spec = replace(_spec(), runtime_id=runtime_id, runtime_version="test-version")
    source_launch = replace(
        _launch("athr_source", native_session_id="native-source", slot=0),
        runtime_id=runtime_id,
        runtime_version="test-version",
    )
    target_launch = replace(
        _launch("athr_child", native_session_id=None, slot=2),
        runtime_id=runtime_id,
        runtime_version="test-version",
    )
    lifecycle: list[str] = []
    source = SimpleNamespace(
        native_session_id="native-source",
        launch=source_launch,
        session=SimpleNamespace(
            fork_native=lambda **_kwargs: lifecycle.append("fork") or "native-child"
        ),
        close=lambda: lifecycle.append("close"),
    )
    writes: list[dict[str, object]] = []
    monkeypatch.setattr(factory, "_read_fork_journal", lambda *_args: None)
    monkeypatch.setattr(
        factory,
        "_clear_http_member_locks",
        lambda _spec, launch: lifecycle.append(f"clear:{launch.thread_id}"),
    )
    monkeypatch.setattr(
        factory,
        "_clone_private_member_state",
        lambda _spec, source_item, target_item: lifecycle.append(
            f"clone:{source_item.thread_id}:{target_item.thread_id}"
        ),
    )
    monkeypatch.setattr(
        factory,
        "_write_fork_journal",
        lambda _spec, _operation_id, value: writes.append(value),
    )

    child_id = factory.fork_native(
        spec,
        source,  # type: ignore[arg-type]
        "fork-http",
        target_launch,
        2,
    )

    assert child_id == "native-child"
    assert lifecycle == [
        "fork",
        "close",
        "clear:athr_source",
        "clone:athr_source:athr_child",
    ]
    assert manager.events == [("normalize", 0), ("normalize", 2)]
    assert writes[0]["state"] == "preparing"
    assert writes[1]["state"] == "ready"


def test_claude_fork_flushes_the_source_before_reading_its_historical_boundary(monkeypatch):
    manager = _Manager()
    factory = ResidentTransportFactory(manager)  # type: ignore[arg-type]
    source_launch = _launch("athr_source", native_session_id="native-source", slot=0)
    target_launch = _launch("athr_child", native_session_id=None, slot=2)
    lifecycle: list[object] = []
    source = SimpleNamespace(
        native_session_id="native-source",
        launch=source_launch,
        close=lambda: lifecycle.append("flush"),
    )
    monkeypatch.setattr(factory, "_read_fork_journal", lambda *_args: None)
    monkeypatch.setattr(factory, "_write_fork_journal", lambda *_args: None)

    def fork_claude(spec, source_id, launch, source_native_turn_no):
        assert lifecycle == ["flush"]
        assert spec == _spec()
        assert launch == source_launch
        lifecycle.append((source_id, launch, source_native_turn_no))
        return "native-child"

    monkeypatch.setattr(factory, "_fork_claude", fork_claude)

    child_id = factory.fork_native(
        _spec(),
        source,  # type: ignore[arg-type]
        "fork-claude",
        target_launch,
        2,
    )

    assert child_id == "native-child"
    assert lifecycle == ["flush", ("native-source", source_launch, 2)]


def test_pi_fork_exposes_the_source_owned_child_session_to_its_target(monkeypatch):
    manager = _Manager()
    factory = ResidentTransportFactory(manager)  # type: ignore[arg-type]
    spec = replace(_spec(), runtime_id="pi", runtime_version="0.73.1")
    source_launch = replace(
        _launch("athr_source", native_session_id="native-source", slot=0),
        runtime_id="pi",
        runtime_version="0.73.1",
    )
    target_launch = replace(
        _launch("athr_child", native_session_id=None, slot=2),
        runtime_id="pi",
        runtime_version="0.73.1",
    )
    fork_calls: list[dict] = []
    closed: list[bool] = []
    source = SimpleNamespace(
        native_session_id="native-source",
        launch=source_launch,
        session=SimpleNamespace(
            fork_native=lambda **kwargs: fork_calls.append(kwargs) or "native-child"
        ),
        close=lambda: closed.append(True),
    )
    monkeypatch.setattr(factory, "_read_fork_journal", lambda *_args: None)
    monkeypatch.setattr(factory, "_write_fork_journal", lambda *_args: None)

    child_id = factory.fork_native(
        spec,
        source,  # type: ignore[arg-type]
        "fork-pi",
        target_launch,
        2,
    )

    assert child_id == "native-child"
    assert fork_calls[0]["source_native_turn_no"] == 2
    assert closed == [True]
    # Pi mutates from the live source process, so the new child file is owned
    # by slot 0 even though slot 2 will subsequently resume it.
    assert manager.events == [("normalize", 0)]


@pytest.mark.parametrize("runtime_id", ["opencode", "kilo"])
def test_http_member_lock_cleanup_runs_as_the_source_slot_owner(runtime_id: str):
    class LockManager:
        def __init__(self) -> None:
            self.backend = SimpleNamespace(gid=10001)
            self.calls: list[tuple[list[str], dict]] = []

        def exec_capture(self, _spec, argv, **kwargs):
            self.calls.append((list(argv), kwargs))
            return subprocess.CompletedProcess([], 0, stdout=b"", stderr=b"")

    manager = LockManager()
    factory = ResidentTransportFactory(manager)  # type: ignore[arg-type]
    launch = replace(
        _launch("athr_source", native_session_id="native-source", slot=3),
        runtime_id=runtime_id,
        runtime_version="test-version",
    )

    factory._clear_http_member_locks(_spec(), launch)

    assert len(manager.calls) == 1
    argv, kwargs = manager.calls[0]
    assert argv[-1] == runtime_id
    assert argv[-2] == factory._member_state_dir(launch)
    assert "locks" in argv[2]
    assert kwargs["user_uid"] == 11004
    assert kwargs["user_gid"] == 10001


def test_snapshot_is_a_short_group_barrier_and_always_thaws_writers():
    manager = _Manager()
    supervisor = SharedChatSessionSupervisor(manager)
    supervisor.register_manifest(_spec(), _members())

    def capture(_backend):
        manager.events.append("capture")
        with pytest.raises(SessionBusy, match="snapshot"):
            supervisor.operation_opened("athr_peer")
        return {"patch": "diff --git a/a b/a\n"}

    result = supervisor.capture_snapshot("athr_source", capture=capture)

    assert result == {"patch": "diff --git a/a b/a\n"}
    assert manager.events[0] == ("freeze", (0, 1))
    assert manager.events[1] == "capture"
    assert manager.events[2][0] == "thaw"
    assert supervisor.status("athr_source").snapshotting is False


def test_snapshot_refuses_active_mutations_instead_of_serializing_normal_work():
    manager = _Manager()
    supervisor = SharedChatSessionSupervisor(manager)
    supervisor.register_manifest(_spec(), _members())
    token, _ = supervisor.operation_opened("athr_peer")

    with pytest.raises(SessionBusy, match="active mutations"):
        supervisor.capture_snapshot("athr_source", capture=lambda _backend: {"patch": ""})

    supervisor.operation_closed("athr_peer", token)
    assert supervisor.capture_snapshot("athr_source", capture=lambda _backend: {"patch": ""}) == {
        "patch": ""
    }


def test_snapshot_starts_the_exact_idle_stopped_container_and_rearms_idle():
    manager = _Manager()
    supervisor = SharedChatSessionSupervisor(manager, idle_timeout_s=30)
    supervisor.register_manifest(_spec(), _members())
    before = manager.stop(_spec())

    result = supervisor.capture_snapshot(
        "athr_source",
        capture=lambda _backend: {"patch": "diff --git a/a b/a\n"},
    )

    after = supervisor.status("athr_source")
    assert result == {"patch": "diff --git a/a b/a\n"}
    assert after.container_id == before.container_id
    assert after.running is True
    assert after.snapshotting is False
    assert after.idle_deadline_monotonic is not None
    assert manager.events[0] == ("freeze", (0, 1))
    assert manager.events[-1][0] == "thaw"


def test_snapshot_start_failure_releases_the_admission_barrier():
    class StartFailureManager(_Manager):
        def start(self, spec, **_kwargs):
            raise SandboxError("docker start failed")

    manager = StartFailureManager()
    supervisor = SharedChatSessionSupervisor(manager)
    supervisor.register_manifest(_spec(), _members())
    manager.stop(_spec())

    with pytest.raises(SandboxError, match="docker start failed"):
        supervisor.capture_snapshot("athr_source", capture=lambda _backend: {"patch": ""})

    status = supervisor.status("athr_source")
    assert status.running is False
    assert status.snapshotting is False


@pytest.mark.parametrize("reserved_child", [False, True])
@pytest.mark.parametrize("with_plugins", [False, True])
def test_native_fork_tracks_activity_but_does_not_lock_a_sibling_operation(
    reserved_child, with_plugins
):
    manager = _Manager()
    factory = _TransportFactory()
    supervisor: SharedChatSessionSupervisor

    def native_forker(spec, source_transport, operation_id, target_launch, source_native_turn_no):
        assert source_transport.native_session_id == "native-source"
        assert operation_id == "fork-one"
        assert source_native_turn_no == 1
        assert target_launch.plugins == source_transport.launch.plugins
        token, peer = supervisor.operation_opened("athr_peer")
        assert peer.operation_count == 1
        assert supervisor.status("athr_source").operation_count == 1
        supervisor.operation_closed("athr_peer", token)
        return "native-child"

    supervisor = SharedChatSessionSupervisor(
        manager,
        transport_factory=factory,
        native_forker=native_forker,
    )
    supervisor.register_manifest(_spec(), _members())
    lease = supervisor.acquire(
        _spec(),
        thread_id="athr_source",
        owner="attempt:1",
        activation_generation=1,
        has_settled_context=True,
        manifest=_members(),
        expected_container_id="container-shared",
    )
    plugins = (
        ({"plugin": "review@fixture", "marketplace": "owner/marketplace"},) if with_plugins else ()
    )
    source_launch = replace(
        _launch("athr_source", native_session_id="native-source", slot=0), plugins=plugins
    )
    supervisor.ensure_resident("athr_source", lease.token, source_launch)
    supervisor.settle(
        "athr_source",
        lease.token,
        job_id="ajob_source",
        context_status="live",
    )

    # The control plane reserves the child membership before asking the host
    # to fork. It has a slot and a residency, but no native session yet.
    pending_manifest = [
        *_members(),
        SharedMemberManifest(
            thread_id="athr_child",
            resident_slot_id=2,
            residency_generation=3,
        ),
    ]
    if reserved_child:
        supervisor.register_manifest(_spec(), pending_manifest)

    result = supervisor.fork_native(
        "athr_source",
        operation_id="fork-one",
        source_native_session_id="native-source",
        source_native_turn_no=1,
        target_thread_id="athr_child",
        target_slot_id=2,
        source_launch=source_launch,
        target_launch=replace(
            _launch("athr_child", native_session_id=None, slot=2), plugins=plugins
        ),
    )

    assert result.container_id == "container-shared"
    assert result.target_native_session_id == "native-child"
    assert supervisor.status("athr_child").resident_state == "live"
    assert supervisor.status("athr_source").operation_count == 0
    assert supervisor.status("athr_source").idle_deadline_monotonic is not None
    assert factory.opened[-1].thread_id == "athr_child"
    assert factory.opened[-1].plugins == plugins

    if reserved_child:
        # A concurrent Files/Git request can still carry the pending database
        # manifest until the fork receipt is finalized by the control plane.
        supervisor.register_manifest(_spec(), pending_manifest)
        child = supervisor.status("athr_child")
        assert child.native_session_id == "native-child"
        assert child.resident_state == "live"
        assert child.residency_generation == 3


@pytest.mark.asyncio
@pytest.mark.parametrize("already_registered", [False, True])
@pytest.mark.parametrize("disk_limit_mib", [0, 16384])
async def test_broker_fork_materializes_reserved_target_and_replays_stale_manifest(
    already_registered,
    disk_limit_mib,
):
    """A durable child reservation must neither block Fork nor erase its retry."""
    manager = _Manager()
    factory = _TransportFactory()
    fork_calls: list[str] = []

    def native_forker(_spec, _transport, operation_id, _launch, _turn_no):
        fork_calls.append(operation_id)
        return "native-child"

    supervisor = SharedChatSessionSupervisor(
        manager, transport_factory=factory, native_forker=native_forker
    )
    spec = replace(_spec(), disk_limit_mib=disk_limit_mib)
    reserved = SharedMemberManifest(thread_id="athr_child", resident_slot_id=2)
    members = [*_members(), reserved]
    supervisor.register_manifest(spec, members if already_registered else _members())
    app = create_app(
        backend=ProcessBackend(acknowledged_unsafe=True),
        token="broker-secret",
        shared_session_supervisor=supervisor,
    )
    body = {
        "sandbox_id": spec.sandbox_id,
        "generation": spec.generation,
        "origin_thread_id": spec.thread_id,
        "runtime_id": spec.runtime_id,
        "runtime_version": spec.runtime_version,
        "sandbox_image_digest": spec.sandbox_image_digest,
        "expected_container_id": "container-shared",
        "resident_slot_capacity": spec.resident_slot_capacity,
        "memory_limit": spec.memory_limit,
        "cpu_limit": spec.cpu_limit,
        "disk_limit_mib": spec.disk_limit_mib,
        "stop_timeout_s": spec.stop_timeout_s,
        "members": [asdict(member) for member in members],
        "operation_id": "fork-reserved-target",
        "source_native_session_id": "native-source",
        "source_native_turn_no": 1,
        "target_thread_id": "athr_child",
        "target_slot_id": 2,
        "source_launch": asdict(_launch("athr_source", native_session_id="native-source", slot=0)),
        "target_launch": asdict(_launch("athr_child", native_session_id=None, slot=2)),
    }
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://broker") as client:
        first = await client.post(
            "/shared-sandboxes/athr_source/fork",
            headers={"X-Agent-Workspace-Token": "broker-secret"},
            json=body,
        )
        assert first.status_code == 200, first.text
        # Files/Git hydration can register the still-pending database manifest
        # after the host has created the child but before receipt finalization.
        registered = await client.post(
            "/shared-sandboxes/athr_source/register",
            headers={"X-Agent-Workspace-Token": "broker-secret"},
            json=SharedSandboxIdentityRequest.model_validate(body).model_dump(),
        )
        assert registered.status_code == 200, registered.text
        # The control plane can retry before storing the receipt, so the
        # durable target manifest still contains no native session identity.
        body["source_launch"]["residency_generation"] = 10
        body["target_launch"]["residency_generation"] = 11
        retried = await client.post(
            "/shared-sandboxes/athr_source/fork",
            headers={"X-Agent-Workspace-Token": "broker-secret"},
            json=body,
        )
        assert retried.status_code == 200, retried.text

        collision = {
            **body,
            "members": [
                *[asdict(member) for member in _members()],
                asdict(replace(reserved, native_session_id="native-child")),
            ],
            "target_thread_id": "athr_collision",
            "target_slot_id": 1,  # The unaffected peer already owns this slot.
            "target_launch": {**body["target_launch"], "slot_id": 1},
        }
        refused = await client.post(
            "/shared-sandboxes/athr_source/fork",
            headers={"X-Agent-Workspace-Token": "broker-secret"},
            json=collision,
        )
        assert refused.status_code == 409, refused.text
        assert "slot is already occupied" in refused.json()["detail"]

    assert fork_calls == ["fork-reserved-target"]
    assert first.json()["target_native_session_id"] == "native-child"
    assert retried.json()["target_native_session_id"] == "native-child"
    assert retried.json()["target_residency_generation"] == 11
    assert supervisor.status("athr_child").resident_state == "live"
    assert supervisor.status("athr_peer").native_session_id == "native-peer"


def test_native_fork_retry_rotates_only_source_and_hidden_child_processes():
    manager = _Manager()
    factory = _TransportFactory()
    fork_calls: list[str] = []

    def native_forker(
        _spec, _source_transport, operation_id, _target_launch, _source_native_turn_no
    ):
        fork_calls.append(operation_id)
        return "native-child"

    supervisor = SharedChatSessionSupervisor(
        manager,
        transport_factory=factory,
        native_forker=native_forker,
    )
    supervisor.register_manifest(_spec(), _members())
    lease = supervisor.acquire(
        _spec(),
        thread_id="athr_source",
        owner="attempt:1",
        activation_generation=1,
        has_settled_context=True,
        manifest=_members(),
        expected_container_id="container-shared",
    )
    source_launch = _launch("athr_source", native_session_id="native-source", slot=0)
    supervisor.ensure_resident("athr_source", lease.token, source_launch)
    supervisor.settle(
        "athr_source",
        lease.token,
        job_id="ajob_source",
        context_status="live",
    )
    target_launch = _launch("athr_child", native_session_id=None, slot=2)
    first = supervisor.fork_native(
        "athr_source",
        operation_id="fork-retry",
        source_native_session_id="native-source",
        source_native_turn_no=1,
        target_thread_id="athr_child",
        target_slot_id=2,
        source_launch=source_launch,
        target_launch=target_launch,
    )

    retried = supervisor.fork_native(
        "athr_source",
        operation_id="fork-retry",
        source_native_session_id="native-source",
        source_native_turn_no=1,
        target_thread_id="athr_child",
        target_slot_id=2,
        source_launch=replace(source_launch, credential="grant-rotated", residency_generation=10),
        target_launch=replace(target_launch, credential="grant-rotated", residency_generation=11),
    )

    assert fork_calls == ["fork-retry"]
    assert first.source_process_generation == first.target_process_generation == 1
    assert retried.source_process_generation == retried.target_process_generation == 2
    assert retried.source_residency_generation == 10
    assert retried.target_residency_generation == 11
    assert [launch.residency_generation for launch in factory.opened] == [1, 3, 10, 11]


@pytest.mark.parametrize("observation", ["none", "status", "list", "residency_lost"])
def test_native_fork_keeps_source_transport_ownership_during_cli_restart(observation):
    manager = _Manager()
    factory = _TransportFactory()
    observations = []

    def native_forker(
        _spec, source_transport, _operation_id, _target_launch, _source_native_turn_no
    ):
        source_transport.alive = False

        # HTTP-backed CLIs close the source while persisting/copying the
        # native fork. Poll from another thread in that exact window, before
        # the forker returns and the supervisor resumes the source.
        def observe():
            if observation == "status":
                observations.append(supervisor.status("athr_source"))
            elif observation == "list":
                observations.extend(supervisor.list())
            elif observation == "residency_lost":
                observations.append(supervisor.residency_lost("athr_source", generation=1))

        observer = threading.Thread(target=observe)
        observer.start()
        observer.join(timeout=2)
        assert not observer.is_alive(), "status polling blocked on the native fork"
        return "native-child"

    supervisor = SharedChatSessionSupervisor(
        manager,
        transport_factory=factory,
        native_forker=native_forker,
    )
    supervisor.register_manifest(_spec(), _members())
    lease = supervisor.acquire(
        _spec(),
        thread_id="athr_source",
        owner="attempt:1",
        activation_generation=1,
        has_settled_context=True,
        manifest=_members(),
        expected_container_id="container-shared",
    )
    source_launch = _launch("athr_source", native_session_id="native-source", slot=0)
    supervisor.ensure_resident("athr_source", lease.token, source_launch)
    supervisor.settle(
        "athr_source",
        lease.token,
        job_id="ajob_source",
        context_status="live",
    )

    fork_args = {
        "operation_id": "fork-closes-source",
        "source_native_session_id": "native-source",
        "source_native_turn_no": 1,
        "target_thread_id": "athr_child",
        "target_slot_id": 2,
        "source_launch": source_launch,
        "target_launch": _launch("athr_child", native_session_id=None, slot=2),
    }
    if observation == "residency_lost":
        # A real grant revocation must still detach the source and reject
        # this fork; the observational race fix cannot bypass that fence.
        with pytest.raises(SessionLeaseLost, match="source transport changed after native fork"):
            supervisor.fork_native("athr_source", **fork_args)
        assert factory.opened == [source_launch]
        assert supervisor.status("athr_source").residency_generation == 0
        assert all(item.thread_id != "athr_child" for item in supervisor.list())
        return

    result = supervisor.fork_native("athr_source", **fork_args)

    child_launch = _launch("athr_child", native_session_id="native-child", slot=2)
    assert result.source_process_generation == 2
    assert supervisor.status("athr_source").resident_state == "live"
    assert factory.opened == [source_launch, source_launch, child_launch]
    if observation != "none":
        source_observation = next(item for item in observations if item.thread_id == "athr_source")
        assert source_observation.operation_count == 1
        assert source_observation.resident_state == "live"
        assert source_observation.residency_generation == 1


def test_idle_sweep_stops_a_group_only_after_its_deadline():
    manager = _Manager()
    now = 100.0
    supervisor = SharedChatSessionSupervisor(manager, idle_timeout_s=30, clock=lambda: now)
    supervisor.register_manifest(_spec(), _members())
    token, _ = supervisor.operation_opened("athr_source")
    closed = supervisor.operation_closed("athr_source", token)
    assert closed.idle_deadline_monotonic == 130.0

    now = 129.0
    assert supervisor.sweep() == []
    assert manager.stop_count == 0

    now = 130.0
    stopped = supervisor.sweep()
    assert len(stopped) == 1
    assert stopped[0].running is False
    assert manager.stop_count == 1
    assert supervisor.sweep() == []
    assert manager.stop_count == 1


@pytest.mark.parametrize("activity", ["operation", "terminal"])
def test_idle_sweep_preserves_a_group_used_after_candidate_selection(monkeypatch, activity):
    manager = _Manager()
    now = 100.0
    supervisor = SharedChatSessionSupervisor(manager, idle_timeout_s=30, clock=lambda: now)
    supervisor.register_manifest(_spec(), _members())
    token, _ = supervisor.operation_opened("athr_source")
    supervisor.operation_closed("athr_source", token)
    original_stop = supervisor.stop_if_idle
    stop_attempts = []

    def activity_before_stop(thread_id, **kwargs):
        # The sweep has selected an expired candidate and released its lock.
        # Complete fresh activity before it admits the selected stop request.
        token, _ = getattr(supervisor, f"{activity}_opened")("athr_peer")
        refreshed = getattr(supervisor, f"{activity}_closed")("athr_peer", token)
        assert refreshed.idle_deadline_monotonic == 160.0
        stop_attempts.append(thread_id)
        return original_stop(thread_id, **kwargs)

    monkeypatch.setattr(supervisor, "stop_if_idle", activity_before_stop)
    now = 130.0

    assert supervisor.sweep() == []
    assert stop_attempts == ["athr_source"]
    assert manager.stop_count == 0
    current = supervisor.status("athr_source")
    assert current.running is True
    assert current.operation_count == current.terminal_count == 0
    assert current.idle_deadline_monotonic == 160.0


@pytest.mark.parametrize("reason", ["idle", "orphaned_attempt", "lost", "unguarded"])
def test_direct_stop_does_not_wait_for_the_inactivity_deadline(reason):
    manager = _Manager()
    supervisor = SharedChatSessionSupervisor(manager, idle_timeout_s=30, clock=lambda: 100.0)
    supervisor.register_manifest(_spec(), _members())
    token, _ = supervisor.operation_opened("athr_source")
    closed = supervisor.operation_closed("athr_source", token)
    assert closed.idle_deadline_monotonic == 130.0

    stopped = supervisor.stop_if_idle("athr_source", generation=1, reason=reason)

    assert stopped.running is False
    assert manager.stop_count == 1


def test_idle_stop_releases_networks_after_docker_already_stopped_the_container():
    manager = _Manager()
    spec = _spec()
    manager.items[spec.container_name] = manager._inspection(spec, running=False)
    supervisor = SharedChatSessionSupervisor(manager)
    supervisor.register_manifest(
        spec,
        _members(),
        expected_container_id="container-shared",
    )

    stopped = supervisor.stop_if_idle("athr_source", generation=spec.generation)

    # The real manager's idempotent stop path releases any phase bridge still
    # attached after an external stop or a partially failed prior cleanup.
    assert manager.stop_count == 1
    assert stopped.running is False
    assert manager.items[spec.container_name].networks == ()


def _archive_shutdown_group(
    *,
    events: list[str],
    fail_transport_close: bool = False,
    fail_invalidate: bool = False,
) -> tuple[_Manager, SharedChatSessionSupervisor]:
    manager = _Manager()
    factory = _TransportFactory()
    original_stop = manager.stop

    def stop(
        spec: ChatSandboxSpec,
        *,
        expected_container_id: str | None = None,
        release_networks: bool = True,
    ) -> ChatSandboxInspection:
        events.append("stop:release" if release_networks else "stop:retain")
        return original_stop(
            spec,
            expected_container_id=expected_container_id,
            release_networks=release_networks,
        )

    manager.stop = stop  # type: ignore[method-assign]

    def prepare(_spec, inspection) -> None:
        assert inspection.running is True
        events.append("prepare")

    def invalidate(_spec, inspection) -> None:
        assert inspection.running is True
        events.append("invalidate")
        if fail_invalidate:
            raise RuntimeError("blocked marker write failed")

    def archive(_spec, inspection, _members) -> None:
        assert inspection.running is False
        assert manager.network_for(_spec) in inspection.networks
        events.append("archive")

    supervisor = SharedChatSessionSupervisor(
        manager,
        transport_factory=factory,
        stopped_archive_prepare_hook=prepare,
        stopped_archive_invalidate_hook=invalidate,
        stopped_archive_hook=archive,
    )
    supervisor.register_manifest(_spec(), _members())
    lease = supervisor.acquire(
        _spec(),
        thread_id="athr_source",
        owner="attempt:1",
        activation_generation=1,
        has_settled_context=True,
        manifest=_members(),
        expected_container_id="container-shared",
    )
    supervisor.ensure_resident(
        "athr_source",
        lease.token,
        _launch("athr_source", native_session_id="native-source", slot=0),
    )
    supervisor.settle(
        "athr_source",
        lease.token,
        job_id="ajob_source",
        context_status="live",
    )
    peer_lease = supervisor.acquire(
        _spec(),
        thread_id="athr_peer",
        owner="attempt:2",
        activation_generation=1,
        has_settled_context=True,
        manifest=_members(),
        expected_container_id="container-shared",
    )
    supervisor.ensure_resident(
        "athr_peer",
        peer_lease.token,
        _launch("athr_peer", native_session_id="native-peer", slot=1),
    )
    supervisor.settle(
        "athr_peer",
        peer_lease.token,
        job_id="ajob_peer",
        context_status="live",
    )
    for index, transport in enumerate(factory.transports):

        def close_transport(
            *,
            _index: int = index,
            _transport: _Transport = transport,
        ) -> None:
            events.append(f"transport.close:{_index}")
            if fail_transport_close and _index == 0:
                raise RuntimeError("native flush failed")
            _transport.alive = False

        transport.close = close_transport  # type: ignore[method-assign]
    return manager, supervisor


def test_shutdown_flushes_then_prepares_stops_archives_and_releases_bridge() -> None:
    events: list[str] = []
    manager, supervisor = _archive_shutdown_group(events=events)

    supervisor.quiesce()
    supervisor.stop_all_for_shutdown()

    assert events == [
        "transport.close:0",
        "transport.close:1",
        "prepare",
        "stop:retain",
        "archive",
        "stop:release",
    ]
    assert manager.items[_spec().container_name].networks == ()


def test_shutdown_flush_failure_stops_with_bridge_but_never_prepares_archives_or_releases() -> None:
    events: list[str] = []
    manager, supervisor = _archive_shutdown_group(
        events=events,
        fail_transport_close=True,
    )

    supervisor.quiesce()
    with pytest.raises(SandboxError, match="shutdown stop/archive"):
        supervisor.stop_all_for_shutdown()

    assert events == [
        "transport.close:0",
        "transport.close:1",
        "invalidate",
        "stop:retain",
    ]
    assert manager.stop_count == 1
    current = manager.items[_spec().container_name]
    assert current.running is False
    assert manager.network_for(_spec()) in current.networks
    with pytest.raises(SandboxError, match="blocked by native flush failure"):
        supervisor.stop_if_idle("athr_source", generation=1)
    with pytest.raises(SessionBusy, match="final archive"):
        supervisor.operation_opened("athr_source")
    with pytest.raises(SessionBusy, match="final archive"):
        supervisor.terminal_opened("athr_source")


def test_flush_failure_blocks_mutations_even_when_docker_stop_raises() -> None:
    events: list[str] = []
    manager, supervisor = _archive_shutdown_group(
        events=events,
        fail_transport_close=True,
    )

    original_stop = manager.stop

    def fail_stop(*_args, **_kwargs):
        events.append("stop:failed")
        raise SandboxError("docker stop failed")

    manager.stop = fail_stop  # type: ignore[method-assign]
    with pytest.raises(SandboxError, match="shutdown stop/archive"):
        supervisor.stop_all_for_shutdown()

    assert events[-2:] == ["invalidate", "stop:failed"]
    with pytest.raises(SessionBusy, match="native flush failure"):
        supervisor.operation_opened("athr_source")
    with pytest.raises(SessionBusy, match="native flush failure"):
        supervisor.terminal_opened("athr_source")
    captured = False

    def capture(_backend):
        nonlocal captured
        captured = True
        return {"patch": ""}

    with pytest.raises(SessionBusy, match="native flush failure"):
        supervisor.capture_snapshot("athr_source", capture=capture)
    assert captured is False

    manager.stop = original_stop  # type: ignore[method-assign]
    with pytest.raises(SandboxError, match="shutdown stop/archive"):
        supervisor.stop_all_for_shutdown()
    assert manager.items[_spec().container_name].running is False
    assert manager.items[_spec().container_name].networks == ("agent-net",)


def test_network_guard_exact_stops_a_running_native_flush_blocked_group() -> None:
    events: list[str] = []
    manager, supervisor = _archive_shutdown_group(
        events=events,
        fail_transport_close=True,
    )
    original_stop = manager.stop

    def fail_stop(*_args, **_kwargs):
        raise SandboxError("docker stop failed")

    manager.stop = fail_stop  # type: ignore[method-assign]
    with pytest.raises(SandboxError, match="shutdown stop/archive"):
        supervisor.stop_all_for_shutdown()
    assert manager.items[_spec().container_name].running is True

    manager.stop = original_stop  # type: ignore[method-assign]
    manager.host_rule_failures = 2
    assert supervisor.guard_network() == []

    current = manager.items[_spec().container_name]
    assert current.running is False
    assert current.networks == ("agent-net",)
    with pytest.raises(SessionBusy, match="native flush failure"):
        supervisor.operation_opened("athr_source")


def test_network_guard_stops_every_group_before_the_first_final_upload_completes() -> None:
    manager = _Manager()
    first = _spec()
    second = replace(first, thread_id="athr_other", sandbox_id="asbx_other")
    manager.items[first.container_name] = manager._inspection(first, running=True)
    manager.items[second.container_name] = manager._inspection(second, running=True)
    first_upload_entered = threading.Event()
    release_first_upload = threading.Event()
    archived: list[str] = []

    def archive(spec, _inspection, _members) -> None:
        if spec.sandbox_id == first.sandbox_id:
            first_upload_entered.set()
            assert release_first_upload.wait(timeout=2)
        archived.append(spec.sandbox_id)

    supervisor = SharedChatSessionSupervisor(manager, stopped_archive_hook=archive)
    supervisor.register_manifest(
        first,
        _members(),
        expected_container_id="container-shared",
    )
    supervisor.register_manifest(
        second,
        [
            replace(
                _members()[0],
                thread_id="athr_other",
                resident_slot_id=0,
            )
        ],
        expected_container_id="container-shared",
    )
    manager.host_rule_failures = 2
    stopped: list[SharedSessionDescriptor] = []
    errors: list[BaseException] = []

    def guard() -> None:
        try:
            stopped.extend(supervisor.guard_network())
        except BaseException as exc:  # pragma: no cover - asserted below
            errors.append(exc)

    guarding = threading.Thread(target=guard)
    guarding.start()
    assert first_upload_entered.wait(timeout=1)
    try:
        assert manager.items[first.container_name].running is False
        assert manager.items[second.container_name].running is False
        assert manager.items[first.container_name].networks == ("agent-net",)
        assert manager.items[second.container_name].networks == ("agent-net",)
    finally:
        release_first_upload.set()
        guarding.join(timeout=2)

    assert not guarding.is_alive()
    assert errors == []
    assert {item.sandbox_id for item in stopped} == {"asbx_shared", "asbx_other"}
    assert archived == ["asbx_shared", "asbx_other"]
    assert manager.items[first.container_name].networks == ()
    assert manager.items[second.container_name].networks == ()


def test_network_guard_stages_a_running_pending_group_before_its_upload() -> None:
    manager = _Manager()
    first = _spec()
    second = replace(first, thread_id="athr_other", sandbox_id="asbx_other")
    manager.items[first.container_name] = manager._inspection(first, running=True)
    manager.items[second.container_name] = manager._inspection(second, running=True)
    first_upload_entered = threading.Event()
    release_first_upload = threading.Event()
    archived: list[str] = []

    def archive(spec, _inspection, _members) -> None:
        if spec.sandbox_id == first.sandbox_id:
            first_upload_entered.set()
            assert release_first_upload.wait(timeout=2)
        archived.append(spec.sandbox_id)

    supervisor = SharedChatSessionSupervisor(
        manager,
        stopped_archive_prepare_hook=lambda *_args: None,
        stopped_archive_hook=archive,
    )
    supervisor.register_manifest(
        first,
        _members(),
        expected_container_id="container-shared",
    )
    supervisor.register_manifest(
        second,
        [replace(_members()[0], thread_id="athr_other", resident_slot_id=0)],
        expected_container_id="container-shared",
    )
    original_stop = manager.stop
    fail_first_stop = True

    def stop(spec, **kwargs):
        nonlocal fail_first_stop
        if spec.sandbox_id == first.sandbox_id and fail_first_stop:
            fail_first_stop = False
            raise SandboxError("docker stop response lost")
        return original_stop(spec, **kwargs)

    manager.stop = stop  # type: ignore[method-assign]
    with pytest.raises(SandboxError, match="docker stop response lost"):
        supervisor.stop_if_idle(first.thread_id, generation=first.generation)
    assert manager.items[first.container_name].running is True

    manager.host_rule_failures = 2
    stopped: list[SharedSessionDescriptor] = []
    errors: list[BaseException] = []

    def guard() -> None:
        try:
            stopped.extend(supervisor.guard_network())
        except BaseException as exc:  # pragma: no cover - asserted below
            errors.append(exc)

    guarding = threading.Thread(target=guard)
    guarding.start()
    assert first_upload_entered.wait(timeout=1)
    try:
        assert manager.items[first.container_name].running is False
        assert manager.items[second.container_name].running is False
        assert manager.items[first.container_name].networks == ("agent-net",)
        assert manager.items[second.container_name].networks == ("agent-net",)
    finally:
        release_first_upload.set()
        guarding.join(timeout=2)

    assert not guarding.is_alive()
    assert errors == []
    assert {item.sandbox_id for item in stopped} == {"asbx_shared", "asbx_other"}
    assert archived == ["asbx_shared", "asbx_other"]
    assert manager.items[first.container_name].networks == ()
    assert manager.items[second.container_name].networks == ()


def test_network_guard_prepare_failure_exact_stops_and_blocks_without_upload() -> None:
    manager = _Manager()
    spec = _spec()
    manager.items[spec.container_name] = manager._inspection(spec, running=True)
    events: list[str] = []

    def prepare(_spec, inspection) -> None:
        assert inspection.running is True
        events.append("prepare")
        raise SandboxError("marker preparation failed")

    def invalidate(_spec, inspection) -> None:
        events.append("invalidate:running" if inspection.running else "invalidate:stopped")

    def archive(*_args) -> None:
        events.append("archive")

    supervisor = SharedChatSessionSupervisor(
        manager,
        stopped_archive_prepare_hook=prepare,
        stopped_archive_invalidate_hook=invalidate,
        stopped_archive_hook=archive,
    )
    supervisor.register_manifest(
        spec,
        _members(),
        expected_container_id="container-shared",
    )
    manager.host_rule_failures = 2

    assert supervisor.guard_network() == []

    current = manager.items[spec.container_name]
    assert current.running is False
    assert current.networks == ("agent-net",)
    assert events == ["prepare", "invalidate:running"]
    with pytest.raises(SessionBusy, match="native flush failure"):
        supervisor.operation_opened(spec.thread_id)


def test_flush_block_persistence_failure_still_stops_and_fences_admission() -> None:
    events: list[str] = []
    manager, supervisor = _archive_shutdown_group(
        events=events,
        fail_transport_close=True,
        fail_invalidate=True,
    )

    with pytest.raises(SandboxError, match="shutdown stop/archive"):
        supervisor.stop_all_for_shutdown()

    assert events[-2:] == ["invalidate", "stop:retain"]
    current = manager.items[_spec().container_name]
    assert current.running is False
    assert current.networks == ("agent-net",)
    with pytest.raises(SessionBusy, match="native flush failure"):
        supervisor.operation_opened("athr_source")
    with pytest.raises(SessionBusy, match="final archive"):
        supervisor.acquire(
            _spec(),
            thread_id="athr_source",
            owner="attempt:3",
            activation_generation=2,
            has_settled_context=True,
            manifest=_members(),
            expected_container_id="container-shared",
        )


def test_restart_reconcile_stops_a_blocked_running_orphan_without_archiving() -> None:
    manager = _Manager()
    spec = _spec()
    manager.items[spec.container_name] = manager._inspection(spec, running=True)
    archive_calls: list[object] = []
    supervisor = SharedChatSessionSupervisor(
        manager,
        stopped_archive_prepare_hook=lambda *_args: (_ for _ in ()).throw(
            SandboxArchiveBlocked("native flush failure")
        ),
        stopped_archive_hook=lambda *args: archive_calls.append(args),
    )

    descriptors = supervisor.reconcile()

    assert manager.stop_count == 1
    assert archive_calls == []
    assert manager.items[spec.container_name].running is False
    assert manager.items[spec.container_name].networks == ("agent-net",)
    assert descriptors[0].running is False
    with pytest.raises(SessionBusy, match="native flush failure"):
        supervisor.operation_opened(spec.thread_id)


def test_durable_flush_block_survives_stop_failure_and_replacement_controller() -> None:
    class DurableManager(_Manager):
        def __init__(self) -> None:
            super().__init__()
            self.final_marker: bytes | None = None
            self.fail_stops = 1

        def write_final_archive_marker(self, _spec, payload, **_kwargs) -> None:
            self.final_marker = payload

        def read_running_final_archive_marker(self, _spec, **_kwargs):
            return self.final_marker

        def read_stopped_final_archive_marker_optional(self, _spec, **_kwargs):
            return self.final_marker

        def read_final_archive_marker(self, _spec, **_kwargs):
            if self.final_marker is None:
                raise SandboxError("final archive marker is missing")
            return self.final_marker

        def write_stopped_final_archive_marker(self, _spec, payload, **_kwargs) -> None:
            self.final_marker = payload

        def clear_final_archive_marker(self, _spec, **_kwargs) -> None:
            self.final_marker = None

        def stop(self, spec, **kwargs):
            if self.fail_stops:
                self.fail_stops -= 1
                self.stop_count += 1
                raise SandboxError("docker stop response lost")
            return super().stop(spec, **kwargs)

    class Service:
        def __init__(self, config) -> None:
            self.config = config
            self.final_calls = 0

        def start(self) -> None:
            return

        def close(self) -> None:
            return

        def status(self):
            return {"enabled": True, "running": True}

        def probe_initial(self, *_args, **_kwargs):
            raise AssertionError("initial capture was not expected")

        def capture_initial(self, *_args, **_kwargs):
            raise AssertionError("initial capture was not expected")

        def queue_final(self, *_args, **_kwargs):
            self.final_calls += 1
            raise AssertionError("blocked native history must never upload")

    manager = DurableManager()
    services: list[Service] = []

    def controller() -> SandboxArchiveController:
        return SandboxArchiveController(
            manager,  # type: ignore[arg-type]
            namespace="pool-east",
            service_factory=lambda config: services.append(Service(config)) or services[-1],
        )

    policy = SandboxArchivePolicy(
        enabled=True,
        region="us-east-1",
        kms_key_arn=("arn:aws:kms:us-east-1:123456789012:key/12345678-1234-1234-1234-123456789abc"),
        expected_bucket_owner="123456789012",
        revision=1,
    ).as_dict()
    first_controller = controller()
    first_controller.start()
    first_controller.apply(policy)
    factory = _TransportFactory()
    first = SharedChatSessionSupervisor(
        manager,
        transport_factory=factory,
        stopped_archive_prepare_hook=first_controller.prepare_final,
        stopped_archive_invalidate_hook=first_controller.invalidate_final,
        stopped_archive_hook=first_controller.queue_final,
    )
    first.register_manifest(_spec(), _members())
    lease = first.acquire(
        _spec(),
        thread_id="athr_source",
        owner="attempt:1",
        activation_generation=1,
        has_settled_context=True,
        manifest=_members(),
        expected_container_id="container-shared",
    )
    first.ensure_resident(
        "athr_source",
        lease.token,
        _launch("athr_source", native_session_id="native-source", slot=0),
    )
    first.settle(
        "athr_source",
        lease.token,
        job_id="ajob_source",
        context_status="live",
    )
    factory.transports[0].close = lambda: (_ for _ in ()).throw(  # type: ignore[method-assign]
        RuntimeError("native flush failed")
    )

    with pytest.raises(SandboxError, match="docker stop response lost"):
        first.stop_if_idle("athr_source", generation=1)
    assert manager.final_marker is not None
    assert b'"state":"blocked"' in manager.final_marker
    assert manager.items[_spec().container_name].running is True
    with pytest.raises(SessionBusy, match="native flush failure"):
        first.operation_opened("athr_source")
    first_controller.close()

    replacement_controller = controller()
    replacement_controller.start()
    replacement_controller.apply(policy)
    replacement = SharedChatSessionSupervisor(
        manager,
        stopped_archive_prepare_hook=replacement_controller.prepare_final,
        stopped_archive_invalidate_hook=replacement_controller.invalidate_final,
        stopped_archive_hook=replacement_controller.queue_final,
    )

    descriptors = replacement.reconcile()

    assert descriptors[0].running is False
    assert manager.stop_count == 2
    assert manager.items[_spec().container_name].networks == ("agent-net",)
    assert all(service.final_calls == 0 for service in services)
    with pytest.raises(SessionBusy, match="native flush failure"):
        replacement.operation_opened(_spec().thread_id)
    replacement_controller.close()


def test_cold_acquire_accepts_an_absent_final_marker_and_completes_the_start_fence() -> None:
    class MarkerManager(_Manager):
        def __init__(self) -> None:
            super().__init__()
            self.marker: bytes | None = None
            self.missing_exports = 0
            self.marker_writes = 0
            self.marker_clears = 0

        def _require(self, spec: ChatSandboxSpec) -> ChatSandboxInspection:
            return self.items[spec.container_name]

        def read_final_archive_marker(self, spec, *, expected_container_id) -> bytes:
            return ChatSandboxManager.read_final_archive_marker(
                self, spec, expected_container_id=expected_container_id
            )

        def read_stopped_final_archive_marker_optional(
            self, spec, *, expected_container_id
        ) -> bytes | None:
            return ChatSandboxManager.read_stopped_final_archive_marker_optional(
                self, spec, expected_container_id=expected_container_id
            )

        def export_archive(self, spec, path, destination, **kwargs) -> int:
            current = self._require(spec)
            assert current.container_id == kwargs["expected_container_id"]
            assert current.running is False
            assert path == f"{INITIAL_ARCHIVE_MARKER_ROOT}/{FINAL_ARCHIVE_MARKER_NAME}"
            if self.marker is None:
                self.missing_exports += 1
                # The real Docker copy normalizes this exact missing source
                # into FileNotFoundError before writing any archive bytes.
                raise FileNotFoundError(path)
            with tarfile.open(fileobj=destination, mode="w") as archive:
                entry = tarfile.TarInfo(FINAL_ARCHIVE_MARKER_NAME)
                entry.uid = entry.gid = 0
                entry.mode = 0o600
                entry.size = len(self.marker)
                archive.addfile(entry, io.BytesIO(self.marker))
            return destination.tell()

        def write_stopped_final_archive_marker(
            self, spec, payload, *, expected_container_id
        ) -> None:
            self.marker = payload
            self.marker_writes += 1
            assert (
                self.read_final_archive_marker(spec, expected_container_id=expected_container_id)
                == payload
            )
            self.events.append("fence")

        def start(self, spec, **kwargs) -> ChatSandboxInspection:
            current = super().start(spec, **kwargs)
            self.events.append("start")
            return current

        def clear_final_archive_marker(self, spec, *, expected_container_id) -> None:
            current = self._require(spec)
            assert current.container_id == expected_container_id
            assert current.running is True
            self.marker = None
            self.marker_clears += 1
            self.events.append("clear")

    manager = MarkerManager()
    spec = _spec()
    manager.items[spec.container_name] = replace(
        manager._inspection(spec, running=False), status=ChatSandboxStatus.CREATED
    )
    controller = SandboxArchiveController(manager, namespace="pool-east")
    controller.start()
    controller.apply(SandboxArchivePolicy(enabled=False, revision=1).as_dict())
    supervisor = SharedChatSessionSupervisor(
        manager,
        starting_archive_prepare_hook=controller.prepare_start,
        started_archive_hook=controller.complete_start,
    )
    try:
        cold = supervisor.acquire(
            spec,
            thread_id=spec.thread_id,
            owner="attempt:1",
            activation_generation=1,
            has_settled_context=False,
            manifest=_members(),
            expected_container_id="container-shared",
        )
        assert cold.token
        assert cold.context_status == "new"
        assert manager.events == ["fence", "start", "clear"]
        supervisor.abandon(spec.thread_id, cold.token, lost=False)
        warm = supervisor.acquire(
            spec,
            thread_id=spec.thread_id,
            owner="attempt:2",
            activation_generation=2,
            has_settled_context=False,
            manifest=_members(),
            expected_container_id="container-shared",
        )
        assert warm.token != cold.token
        assert manager.items[spec.container_name].running is True
        assert manager.marker is None
        assert manager.missing_exports == manager.marker_writes == manager.marker_clears == 1
    finally:
        controller.close()


def test_archive_start_fence_runs_only_for_a_stopped_to_running_transition() -> None:
    class MarkerManager(_Manager):
        def __init__(self) -> None:
            super().__init__()
            self.marker: bytes | None = None
            self.stopped_reads = 0
            self.marker_writes = 0
            self.marker_clears = 0

        def read_stopped_final_archive_marker_optional(self, spec, *, expected_container_id):
            current = self.items[spec.container_name]
            assert current.container_id == expected_container_id
            assert current.running is False
            self.stopped_reads += 1
            return self.marker

        def write_stopped_final_archive_marker(
            self, spec, payload, *, expected_container_id
        ) -> None:
            current = self.items[spec.container_name]
            assert current.container_id == expected_container_id
            assert current.running is False
            self.marker = payload
            self.marker_writes += 1

        def clear_final_archive_marker(self, spec, *, expected_container_id) -> None:
            assert self.items[spec.container_name].container_id == expected_container_id
            self.marker = None
            self.marker_clears += 1

    manager = MarkerManager()
    controller = SandboxArchiveController(
        manager,  # type: ignore[arg-type]
        namespace="pool-east",
    )
    controller.start()
    controller.apply(SandboxArchivePolicy(enabled=False, revision=1).as_dict())
    supervisor = SharedChatSessionSupervisor(
        manager,
        starting_archive_prepare_hook=controller.prepare_start,
        started_archive_hook=controller.complete_start,
    )
    supervisor.register_manifest(_spec(), _members())
    manager.stop(_spec(), release_networks=False)

    first = supervisor.acquire(
        _spec(),
        thread_id="athr_source",
        owner="attempt:1",
        activation_generation=1,
        has_settled_context=True,
        manifest=_members(),
        expected_container_id="container-shared",
    )
    supervisor.abandon("athr_source", first.token, lost=False)
    second = supervisor.acquire(
        _spec(),
        thread_id="athr_source",
        owner="attempt:2",
        activation_generation=2,
        has_settled_context=True,
        manifest=_members(),
        expected_container_id="container-shared",
    )

    assert second.token
    assert manager.items[_spec().container_name].running is True
    assert manager.stopped_reads == 1
    assert manager.marker_writes == 1
    assert manager.marker_clears == 1
    controller.close()


def test_failed_start_marker_completion_is_retried_in_place_before_snapshot() -> None:
    manager = _Manager()
    completions = 0
    preparations = 0

    def prepare(_spec, inspection) -> None:
        nonlocal preparations
        assert inspection.running is False
        preparations += 1

    def complete(_spec, inspection) -> None:
        nonlocal completions
        assert inspection.running is True
        completions += 1
        if completions == 1:
            raise RuntimeError("marker clear failed")

    supervisor = SharedChatSessionSupervisor(
        manager,
        starting_archive_prepare_hook=prepare,
        started_archive_hook=complete,
    )
    supervisor.register_manifest(_spec(), _members())
    manager.stop(_spec(), release_networks=False)

    with pytest.raises(RuntimeError, match="marker clear failed"):
        supervisor.acquire(
            _spec(),
            thread_id="athr_source",
            owner="attempt:1",
            activation_generation=1,
            has_settled_context=True,
            manifest=_members(),
            expected_container_id="container-shared",
        )

    lease = supervisor.acquire(
        _spec(),
        thread_id="athr_source",
        owner="attempt:2",
        activation_generation=2,
        has_settled_context=True,
        manifest=_members(),
        expected_container_id="container-shared",
    )
    assert lease.token
    supervisor.abandon("athr_source", lease.token, lost=False)

    captured = False

    def capture(_backend):
        nonlocal captured
        captured = True
        return {"patch": ""}

    snapshot = supervisor.capture_snapshot("athr_source", capture=capture)

    assert snapshot == {"patch": ""}
    assert captured is True
    assert preparations == 1
    assert completions == 2
    assert manager.stop_count == 1


def test_initial_archive_is_fenced_while_a_group_start_is_in_progress() -> None:
    class BlockingStartManager(_Manager):
        def __init__(self) -> None:
            super().__init__()
            self.block_start = False
            self.start_entered = threading.Event()
            self.start_release = threading.Event()

        def start(self, spec, **kwargs):
            current = self.items[spec.container_name]
            if self.block_start and not current.running:
                self.start_entered.set()
                assert self.start_release.wait(timeout=2)
            return super().start(spec, **kwargs)

    manager = BlockingStartManager()
    supervisor = SharedChatSessionSupervisor(manager)
    lease = supervisor.acquire(
        _spec(),
        thread_id="athr_source",
        owner="attempt:1",
        activation_generation=1,
        has_settled_context=True,
        manifest=_members(),
    )
    manager.stop(_spec(), release_networks=False)
    manager.block_start = True
    opened: list[tuple[str, SharedSessionDescriptor]] = []
    errors: list[BaseException] = []

    def open_operation() -> None:
        try:
            opened.append(supervisor.operation_opened("athr_peer"))
        except BaseException as exc:  # pragma: no cover - asserted below
            errors.append(exc)

    opening = threading.Thread(target=open_operation)
    opening.start()
    assert manager.start_entered.wait(timeout=1)
    captured = False

    def capture(*_args):
        nonlocal captured
        captured = True
        return {"enabled": True, "stored": True}

    try:
        with pytest.raises(SessionBusy, match="start is in progress"):
            supervisor.capture_initial_archive(
                "athr_source",
                lease.token,
                capture=capture,
            )
    finally:
        manager.start_release.set()
        opening.join(timeout=2)

    assert not opening.is_alive()
    assert errors == []
    assert captured is False
    token, _ = opened[0]
    supervisor.operation_closed("athr_peer", token)


def test_failed_marked_initial_archive_stops_and_completes_the_final_archive() -> None:
    manager = _Manager()
    prepared: list[bool] = []
    archived: list[tuple[bool, tuple[str, ...]]] = []
    supervisor = SharedChatSessionSupervisor(
        manager,
        stopped_archive_prepare_hook=lambda _spec, inspection: prepared.append(inspection.running),
        stopped_archive_hook=lambda _spec, inspection, _members: archived.append(
            (inspection.running, inspection.networks)
        ),
    )
    lease = supervisor.acquire(
        _spec(),
        thread_id="athr_source",
        owner="attempt:1",
        activation_generation=1,
        has_settled_context=True,
        manifest=_members(),
    )
    initial_failure = RuntimeError("initial upload failed")

    def capture(*_args):
        manager.initial_archive_marker = b"prepared-initial-marker"
        raise initial_failure

    with pytest.raises(RuntimeError, match="initial upload failed") as raised:
        supervisor.capture_initial_archive(
            "athr_source",
            lease.token,
            capture=capture,
        )

    status = supervisor.status("athr_source")
    assert raised.value is initial_failure
    assert prepared == [True]
    assert archived == [(False, ("agent-net",))]
    assert manager.stop_count == 2
    assert manager.items[_spec().container_name].networks == ()
    assert status.running is False
    assert status.snapshotting is False
    assert all(event[0] != "thaw" for event in manager.events)


def test_failed_marked_initial_archive_waits_for_concurrent_guard_stop() -> None:
    class BlockingStopManager(_Manager):
        def __init__(self) -> None:
            super().__init__()
            self.stop_entered = threading.Event()
            self.stop_release = threading.Event()

        def stop(self, spec, **kwargs):
            self.stop_entered.set()
            assert self.stop_release.wait(timeout=5)
            return super().stop(spec, **kwargs)

    manager = BlockingStopManager()
    supervisor = SharedChatSessionSupervisor(
        manager,
        stopped_archive_prepare_hook=lambda *_args: None,
        stopped_archive_hook=lambda *_args: None,
    )
    fresh_members = [
        SharedMemberManifest(
            thread_id="athr_source",
            context_status="new",
            resident_slot_id=0,
        )
    ]
    lease = supervisor.acquire(
        _spec(),
        thread_id="athr_source",
        owner="attempt:1",
        activation_generation=1,
        has_settled_context=False,
        manifest=fresh_members,
    )
    capture_entered = threading.Event()
    preservation_collided = threading.Event()
    capture_finished = threading.Event()
    capture_errors: list[BaseException] = []
    guard_errors: list[BaseException] = []
    stop_if_idle = supervisor.stop_if_idle

    def observed_stop_if_idle(*args, **kwargs):
        try:
            return stop_if_idle(*args, **kwargs)
        except SessionBusy:
            if kwargs.get("reason") == "orphaned_attempt":
                preservation_collided.set()
            raise

    supervisor.stop_if_idle = observed_stop_if_idle  # type: ignore[method-assign]

    def capture(*_args):
        manager.initial_archive_marker = b"prepared-initial-marker"
        capture_entered.set()
        assert manager.stop_entered.wait(timeout=5)
        raise RuntimeError("initial upload failed")

    def run_capture() -> None:
        try:
            supervisor.capture_initial_archive(
                "athr_source",
                lease.token,
                capture=capture,
            )
        except BaseException as exc:  # pragma: no cover - asserted below
            capture_errors.append(exc)
        finally:
            capture_finished.set()

    def run_guard_stop() -> None:
        try:
            supervisor.stop_if_idle(
                "athr_source",
                generation=1,
                reason="unguarded",
                defer_archive=True,
            )
        except BaseException as exc:  # pragma: no cover - asserted below
            guard_errors.append(exc)

    capture_thread = threading.Thread(target=run_capture)
    guard_thread = threading.Thread(target=run_guard_stop)
    capture_thread.start()
    assert capture_entered.wait(timeout=2)
    guard_thread.start()
    assert manager.stop_entered.wait(timeout=2)
    assert preservation_collided.wait(timeout=2)
    # The preservation cleanup must wait for the already-owned stop instead of
    # sampling RUNNING and permanently retaining the snapshot barrier.
    assert capture_finished.wait(timeout=0.2) is False
    manager.stop_release.set()
    capture_thread.join(timeout=2)
    guard_thread.join(timeout=2)

    assert not capture_thread.is_alive()
    assert not guard_thread.is_alive()
    assert guard_errors == []
    assert len(capture_errors) == 1
    assert isinstance(capture_errors[0], SessionBusy)
    status = supervisor.status("athr_source")
    assert status.running is False
    assert status.snapshotting is False
    assert supervisor.has_pending_archive_work() is True
    assert all(event[0] != "thaw" for event in manager.events)


def test_failed_final_upload_keeps_the_stopped_bridge_and_never_thaws() -> None:
    manager = _Manager()
    prepared: list[bool] = []
    final_failure = RuntimeError("final upload failed")

    def archive(*_args) -> None:
        raise final_failure

    supervisor = SharedChatSessionSupervisor(
        manager,
        stopped_archive_prepare_hook=lambda _spec, inspection: prepared.append(inspection.running),
        stopped_archive_hook=archive,
    )
    lease = supervisor.acquire(
        _spec(),
        thread_id="athr_source",
        owner="attempt:1",
        activation_generation=1,
        has_settled_context=True,
        manifest=_members(),
    )
    initial_failure = RuntimeError("initial upload failed")

    def capture(*_args):
        manager.initial_archive_marker = b"prepared-initial-marker"
        raise initial_failure

    with pytest.raises(RuntimeError, match="final upload failed") as raised:
        supervisor.capture_initial_archive(
            "athr_source",
            lease.token,
            capture=capture,
        )

    status = supervisor.status("athr_source")
    assert raised.value is final_failure
    assert raised.value.__cause__ is initial_failure
    assert prepared == [True]
    assert manager.stop_count == 1
    assert manager.items[_spec().container_name].networks == ("agent-net",)
    assert status.running is False
    assert status.snapshotting is False
    assert supervisor.has_pending_archive_work() is True
    assert all(event[0] != "thaw" for event in manager.events)
    with pytest.raises(SessionBusy, match="final archive retry"):
        supervisor.operation_opened("athr_source")


def test_failed_initial_preservation_stop_releases_barrier_after_exact_retry() -> None:
    class StopFailureManager(_Manager):
        def __init__(self) -> None:
            super().__init__()
            self.fail_stop_once = True

        def stop(self, spec, **kwargs):
            if self.fail_stop_once:
                self.fail_stop_once = False
                self.stop_count += 1
                raise SandboxError("docker stop failed")
            return super().stop(spec, **kwargs)

    manager = StopFailureManager()
    archive_calls: list[object] = []
    supervisor = SharedChatSessionSupervisor(
        manager,
        stopped_archive_prepare_hook=lambda *_args: None,
        stopped_archive_hook=lambda *args: archive_calls.append(args),
    )
    fresh_members = [
        SharedMemberManifest(
            thread_id="athr_source",
            context_status="new",
            resident_slot_id=0,
        )
    ]
    lease = supervisor.acquire(
        _spec(),
        thread_id="athr_source",
        owner="attempt:1",
        activation_generation=1,
        has_settled_context=False,
        manifest=fresh_members,
    )
    initial_failure = RuntimeError("initial upload failed")

    def capture(*_args):
        manager.initial_archive_marker = b"prepared-initial-marker"
        raise initial_failure

    with pytest.raises(SandboxError, match="docker stop failed") as raised:
        supervisor.capture_initial_archive(
            "athr_source",
            lease.token,
            capture=capture,
        )

    status = supervisor.status("athr_source")
    assert raised.value.__cause__ is initial_failure
    assert manager.stop_count == 1
    assert archive_calls == []
    assert status.running is True
    assert status.snapshotting is True
    assert supervisor.has_pending_archive_work() is True
    assert all(event[0] != "thaw" for event in manager.events)
    with pytest.raises(SessionBusy, match="snapshot capture"):
        supervisor.operation_opened("athr_source")

    stopped = supervisor.stop_if_idle(
        "athr_source",
        generation=1,
        reason="orphaned_attempt",
    )

    assert stopped.running is False
    assert stopped.snapshotting is False
    assert manager.stop_count == 3
    assert len(archive_calls) == 1
    assert supervisor.has_pending_archive_work() is False
    assert all(event[0] != "thaw" for event in manager.events)

    replacement = supervisor.acquire(
        _spec(),
        thread_id="athr_source",
        owner="attempt:2",
        activation_generation=2,
        has_settled_context=False,
        manifest=fresh_members,
        expected_container_id="container-shared",
    )

    assert replacement.token
    assert supervisor.status("athr_source").running is True
    assert supervisor.status("athr_source").snapshotting is False
    supervisor.abandon("athr_source", replacement.token, lost=False)


def test_failed_initial_archive_before_marker_thaws_without_stopping() -> None:
    manager = _Manager()
    prepared: list[bool] = []
    archived: list[object] = []
    supervisor = SharedChatSessionSupervisor(
        manager,
        stopped_archive_prepare_hook=lambda _spec, inspection: prepared.append(inspection.running),
        stopped_archive_hook=lambda *args: archived.append(args),
    )
    lease = supervisor.acquire(
        _spec(),
        thread_id="athr_source",
        owner="attempt:1",
        activation_generation=1,
        has_settled_context=True,
        manifest=_members(),
    )
    initial_failure = RuntimeError("initial capture failed before marker")

    with pytest.raises(RuntimeError, match="before marker") as raised:
        supervisor.capture_initial_archive(
            "athr_source",
            lease.token,
            capture=lambda *_args: (_ for _ in ()).throw(initial_failure),
        )

    status = supervisor.status("athr_source")
    assert raised.value is initial_failure
    assert prepared == []
    assert archived == []
    assert manager.stop_count == 0
    assert status.running is True
    assert status.snapshotting is False
    assert manager.events[0] == ("freeze", (0, 1))
    assert manager.events[1][0] == "thaw"


@pytest.mark.parametrize("entrypoint", ["operation", "terminal"])
def test_failed_start_marker_completion_is_retried_by_same_mutation_entrypoint(
    entrypoint: str,
) -> None:
    manager = _Manager()
    completions = 0

    def complete(_spec, inspection) -> None:
        nonlocal completions
        assert inspection.running is True
        completions += 1
        if completions == 1:
            raise RuntimeError("marker clear failed")

    supervisor = SharedChatSessionSupervisor(
        manager,
        starting_archive_prepare_hook=lambda _spec, inspection: (
            None if not inspection.running else pytest.fail("start fence requires stopped state")
        ),
        started_archive_hook=complete,
    )
    supervisor.register_manifest(_spec(), _members())
    manager.stop(_spec(), release_networks=False)
    opened = (
        supervisor.operation_opened if entrypoint == "operation" else supervisor.terminal_opened
    )

    with pytest.raises(RuntimeError, match="marker clear failed"):
        opened("athr_source")

    token, descriptor = opened("athr_source")

    assert token
    assert descriptor.running is True
    assert completions == 2
    assert manager.stop_count == 1
    if entrypoint == "operation":
        supervisor.operation_closed("athr_source", token)
    else:
        supervisor.terminal_closed("athr_source", token)


def test_pending_final_archive_retries_after_external_bridge_removal() -> None:
    manager = _Manager()
    archive_calls = 0

    def archive(_spec, _inspection, _members) -> None:
        nonlocal archive_calls
        archive_calls += 1
        if archive_calls == 1:
            raise RuntimeError("upload response lost")

    supervisor = SharedChatSessionSupervisor(
        manager,
        stopped_archive_prepare_hook=lambda *_args: None,
        stopped_archive_hook=archive,
    )
    supervisor.register_manifest(_spec(), _members())

    with pytest.raises(RuntimeError, match="upload response lost"):
        supervisor.stop_if_idle("athr_source", generation=1)
    stopped = manager.items[_spec().container_name]
    assert stopped.running is False
    assert stopped.networks == ("agent-net",)
    manager.items[_spec().container_name] = replace(stopped, networks=())

    descriptor = supervisor.stop_if_idle("athr_source", generation=1)

    assert descriptor.running is False
    assert archive_calls == 2
    assert manager.items[_spec().container_name].networks == ()


def test_direct_orphan_archive_retry_clears_the_admission_fence() -> None:
    manager = _Manager()
    spec = _spec()
    manager.items[spec.container_name] = manager._inspection(spec, running=True)
    manager.read_running_final_archive_marker = (  # type: ignore[method-assign]
        lambda *_args, **_kwargs: b"durable-pending-marker"
    )
    archive_calls = 0

    def archive(_spec, _inspection, _members) -> None:
        nonlocal archive_calls
        archive_calls += 1
        if archive_calls == 1:
            raise RuntimeError("upload failed")

    supervisor = SharedChatSessionSupervisor(manager, stopped_archive_hook=archive)

    descriptors = supervisor.reconcile()

    assert [item.thread_id for item in descriptors] == [spec.thread_id]
    assert archive_calls == 1
    with pytest.raises(SessionBusy, match="final archive"):
        supervisor.operation_opened(spec.thread_id)

    stopped = supervisor.stop_if_idle(spec.thread_id, generation=spec.generation)
    token, _ = supervisor.operation_opened(spec.thread_id)

    assert stopped.running is False
    assert archive_calls == 2
    supervisor.operation_closed(spec.thread_id, token)


def test_ambiguous_post_start_failure_remains_pending_until_exact_stop_retry() -> None:
    class UncertainStartManager(_Manager):
        def __init__(self) -> None:
            super().__init__()
            self.fail_start_once = True
            self.fail_stop_once = True

        def start(self, spec, **kwargs):
            if self.fail_start_once:
                self.fail_start_once = False
                current = self.items[spec.container_name]
                callback = kwargs.get("before_stopped_start")
                assert callback is not None and current.running is False
                callback(current)
                self.items[spec.container_name] = replace(
                    current,
                    running=True,
                    status=ChatSandboxStatus.RUNNING,
                )
                raise SandboxError("guest network proof failed")
            return super().start(spec, **kwargs)

        def stop(self, spec, **kwargs):
            if self.fail_stop_once:
                self.fail_stop_once = False
                raise SandboxError("fail-stop response lost")
            return super().stop(spec, **kwargs)

    manager = UncertainStartManager()
    archived: list[tuple] = []
    supervisor = SharedChatSessionSupervisor(
        manager,
        starting_archive_prepare_hook=lambda *_args: None,
        started_archive_hook=lambda *_args: None,
        stopped_archive_prepare_hook=lambda *_args: None,
        stopped_archive_hook=lambda *args: archived.append(args),
    )
    supervisor.register_manifest(_spec(), _members())
    manager.items[_spec().container_name] = manager._inspection(_spec(), running=False)

    with pytest.raises(SandboxError, match="fail-stop response lost"):
        supervisor.acquire(
            _spec(),
            thread_id="athr_source",
            owner="attempt:1",
            activation_generation=1,
            has_settled_context=True,
            manifest=_members(),
            expected_container_id="container-shared",
        )
    with pytest.raises(SessionBusy, match="final archive retry"):
        supervisor.acquire(
            _spec(),
            thread_id="athr_source",
            owner="attempt:2",
            activation_generation=2,
            has_settled_context=True,
            manifest=_members(),
            expected_container_id="container-shared",
        )

    supervisor.reconcile()

    assert len(archived) == 1
    current = manager.items[_spec().container_name]
    assert current.running is False
    assert current.networks == ()


def test_external_stopped_group_with_missing_final_marker_keeps_its_bridge() -> None:
    manager = _Manager()
    spec = _spec()
    supervisor = SharedChatSessionSupervisor(
        manager,
        stopped_archive_hook=lambda *_args: (_ for _ in ()).throw(
            SandboxError("final archive marker is missing")
        ),
    )
    supervisor.register_manifest(spec, _members())
    manager.items[spec.container_name] = manager._inspection(spec, running=False)

    descriptors = supervisor.list()

    assert all(item.running is False for item in descriptors)
    assert manager.items[spec.container_name].networks == ("agent-net",)
    assert manager.stop_count == 0
    with pytest.raises(SessionBusy, match="native flush failure"):
        supervisor.operation_opened(spec.thread_id)


@pytest.mark.parametrize("generations", [{"athr_source": 0}, {"athr_source": 1, "athr_peer": 0}])
def test_delayed_admin_stop_cannot_stop_changed_members(generations):
    manager = _Manager()
    spec = _spec()
    manager.create(spec)
    supervisor = SharedChatSessionSupervisor(manager)
    supervisor.register_manifest(spec, _members(), expected_container_id="container-shared")
    with pytest.raises(SessionLeaseLost, match="activation changed"):
        supervisor.stop_if_idle(
            "athr_source",
            generation=1,
            member_activation_generations=generations,
        )
    assert manager.stop_count == 0
    stopped = supervisor.stop_if_idle(
        "athr_source",
        generation=1,
        member_activation_generations={"athr_source": 0, "athr_peer": 0},
    )
    assert not stopped.running


def test_new_activation_waits_for_an_inflight_idle_stop_then_restarts_exact_container():
    class BlockingStopManager(_Manager):
        def __init__(self) -> None:
            super().__init__()
            self.stop_entered = threading.Event()
            self.allow_stop = threading.Event()

        def stop(
            self,
            spec: ChatSandboxSpec,
            *,
            expected_container_id: str | None = None,
            release_networks: bool = True,
        ) -> ChatSandboxInspection:
            self.stop_entered.set()
            assert self.allow_stop.wait(timeout=2)
            return super().stop(
                spec,
                expected_container_id=expected_container_id,
                release_networks=release_networks,
            )

    manager = BlockingStopManager()
    factory = _TransportFactory()
    supervisor = SharedChatSessionSupervisor(manager, transport_factory=factory)
    manifest = _members()
    manager.create(_spec())
    lease = supervisor.acquire(
        _spec(),
        thread_id="athr_source",
        owner="attempt:1",
        activation_generation=1,
        has_settled_context=True,
        manifest=manifest,
        expected_container_id="container-shared",
    )
    supervisor.ensure_resident(
        "athr_source",
        lease.token,
        _launch("athr_source", native_session_id="native-source", slot=0),
    )
    supervisor.settle(
        "athr_source",
        lease.token,
        job_id="ajob_source",
        context_status="live",
    )

    stopped: list[object] = []
    errors: list[BaseException] = []

    def stop_group() -> None:
        try:
            stopped.append(supervisor.stop_if_idle("athr_source", generation=1))
        except BaseException as exc:  # pragma: no cover - assertion reports it below
            errors.append(exc)

    stop_thread = threading.Thread(target=stop_group)
    stop_thread.start()
    assert manager.stop_entered.wait(timeout=1)
    with pytest.raises(SessionBusy, match="stop"):
        supervisor.operation_opened("athr_peer")

    acquired: list[object] = []

    def wake_group() -> None:
        try:
            acquired.append(
                supervisor.acquire(
                    _spec(),
                    thread_id="athr_source",
                    owner="attempt:2",
                    activation_generation=2,
                    has_settled_context=True,
                    manifest=manifest,
                    expected_container_id="container-shared",
                )
            )
        except BaseException as exc:  # pragma: no cover - assertion reports it below
            errors.append(exc)

    wake_thread = threading.Thread(target=wake_group)
    wake_thread.start()
    wake_thread.join(timeout=0.05)
    assert wake_thread.is_alive(), "new activation bypassed the stop admission barrier"

    manager.allow_stop.set()
    stop_thread.join(timeout=1)
    wake_thread.join(timeout=1)

    assert errors == []
    assert len(stopped) == len(acquired) == 1
    assert acquired[0].container_id == "container-shared"
    assert acquired[0].context_status == "restored"
    assert manager.items[_spec().container_name].running is True


def test_settlement_ack_replays_only_for_the_same_completed_activation():
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
    supervisor.ensure_resident(
        "athr_source",
        lease.token,
        _launch("athr_source", native_session_id="native-source", slot=0),
    )
    original = supervisor.settle(
        "athr_source",
        lease.token,
        job_id="ajob_source",
        context_status="live",
    )
    assert (
        supervisor.settle(
            "athr_source",
            lease.token,
            job_id="ajob_source",
            context_status="live",
        )
        == original
    )
    assert len(factory.transports) == 1
    for token, job_id, status in [
        ("wrong-token", "ajob_source", "live"),
        (lease.token, "ajob_other", "live"),
        (lease.token, "ajob_source", "restored"),
    ]:
        with pytest.raises(SessionLeaseLost):
            supervisor.settle("athr_source", token, job_id=job_id, context_status=status)

    successor = supervisor.acquire(
        _spec(),
        thread_id="athr_source",
        owner="attempt:2",
        activation_generation=2,
        has_settled_context=True,
        manifest=_members(),
    )
    with pytest.raises(SessionLeaseLost):
        supervisor.settle(
            "athr_source",
            lease.token,
            job_id="ajob_source",
            context_status="live",
        )
    assert supervisor.status("athr_source").busy
    supervisor.abandon("athr_source", successor.token, lost=False)
    with pytest.raises(SessionLeaseLost):
        supervisor.settle(
            "athr_source",
            lease.token,
            job_id="ajob_source",
            context_status="live",
        )


def test_status_downgrades_a_dead_resident_process_without_losing_native_context():
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
    supervisor.ensure_resident(
        "athr_source",
        lease.token,
        _launch("athr_source", native_session_id="native-source", slot=0),
    )
    supervisor.settle(
        "athr_source",
        lease.token,
        job_id="ajob_source",
        context_status="live",
    )
    factory.transports[-1].alive = False

    descriptor = supervisor.status("athr_source")

    assert descriptor.resident_state == "restarting"
    assert descriptor.context_status == "live"
    assert descriptor.native_session_id == "native-source"


def test_process_fixed_config_restart_reports_restored_instead_of_hot():
    manager = _Manager()
    factory = _TransportFactory()
    supervisor = SharedChatSessionSupervisor(manager, transport_factory=factory)
    first = supervisor.acquire(
        _spec(),
        thread_id="athr_source",
        owner="attempt:1",
        activation_generation=1,
        has_settled_context=True,
        manifest=_members(),
    )
    launch = _launch("athr_source", native_session_id="native-source", slot=0)
    supervisor.ensure_resident("athr_source", first.token, launch)
    supervisor.settle(
        "athr_source",
        first.token,
        job_id="ajob_source",
        context_status="live",
    )

    second = supervisor.acquire(
        _spec(),
        thread_id="athr_source",
        owner="attempt:2",
        activation_generation=2,
        has_settled_context=True,
        manifest=_members(),
    )
    assert second.context_status == "live"

    restarted = supervisor.ensure_resident(
        "athr_source",
        second.token,
        replace(
            launch,
            model="glm-5.2",
            credential="grant-2",
            residency_generation=2,
        ),
    )

    assert restarted.process_generation == 2
    assert restarted.context_status == "restored"


def test_retry_retirement_settles_native_locator_then_restores_same_session():
    manager = _Manager()
    factory = _TransportFactory()
    supervisor = SharedChatSessionSupervisor(manager, transport_factory=factory)
    first = supervisor.acquire(
        _spec(),
        thread_id="athr_source",
        owner="attempt:1",
        activation_generation=1,
        has_settled_context=False,
        manifest=[
            replace(
                _members()[0],
                native_session_id=None,
                last_settled_job_id=None,
                context_status="new",
            ),
            _members()[1],
        ],
    )
    supervisor.ensure_resident(
        "athr_source", first.token, _launch("athr_source", native_session_id=None, slot=0)
    )
    transport = factory.transports[-1]
    transport.retire_after_turn = True
    settled = supervisor.settle(
        "athr_source", first.token, job_id="ajob_first", context_status="new"
    )
    assert not transport.alive
    assert settled.native_session_id == "native-child"
    assert settled.resident_state == "restarting"
    assert settled.last_settled_job_id == "ajob_first"
    second = supervisor.acquire(
        _spec(),
        thread_id="athr_source",
        owner="attempt:2",
        activation_generation=2,
        has_settled_context=True,
        manifest=[
            replace(
                _members()[0],
                native_session_id=settled.native_session_id,
                last_settled_job_id=settled.last_settled_job_id,
                context_status=settled.context_status,
            ),
            _members()[1],
        ],
    )
    assert second.context_status == "restored"
    restored = supervisor.ensure_resident(
        "athr_source",
        second.token,
        replace(
            _launch("athr_source", native_session_id=settled.native_session_id, slot=0),
            residency_generation=2,
        ),
    )
    assert restored.native_session_id == "native-child"
    assert restored.process_generation == 2
    assert restored.context_status == "restored"


def test_unused_mcp_token_does_not_change_live_process_compatibility():
    launch = _launch("athr_source", native_session_id="native-source", slot=0)
    transport = ResidentTransport(launch=launch, session=SimpleNamespace())

    assert transport.compatible_with(
        replace(launch, mcp_base_url="http://relay", mcp_credential="fresh-attempt-token")
    )


def test_missing_launch_locator_restores_the_known_member_instead_of_starting_fresh():
    factory = _TransportFactory()
    supervisor = SharedChatSessionSupervisor(_Manager(), transport_factory=factory)
    lease = supervisor.acquire(
        _spec(),
        thread_id="athr_source",
        owner="attempt:1",
        activation_generation=1,
        has_settled_context=True,
        manifest=_members(),
    )
    restored = supervisor.ensure_resident(
        "athr_source", lease.token, _launch("athr_source", native_session_id=None, slot=0)
    )
    assert factory.opened[-1].native_session_id == "native-source"
    assert restored.native_session_id == "native-source"
    assert restored.context_status == "restored"


@pytest.mark.parametrize("runtime", ["claude-code", "codex", "pi", "opencode", "kilo"])
def test_wrong_native_resume_is_rejected_without_replacing_the_saved_locator(runtime):
    class WrongResumeFactory(_TransportFactory):
        def open(self, spec, launch):
            transport = super().open(spec, launch)
            transport.native_session_id = "unexpected-new-session"
            return transport

    factory = WrongResumeFactory()
    supervisor = SharedChatSessionSupervisor(_Manager(), transport_factory=factory)
    lease = supervisor.acquire(
        _spec(),
        thread_id="athr_source",
        owner="attempt:1",
        activation_generation=1,
        has_settled_context=True,
        manifest=_members(),
    )
    with pytest.raises(SandboxError, match="different native session"):
        supervisor.ensure_resident(
            "athr_source",
            lease.token,
            replace(
                _launch("athr_source", native_session_id="native-source", slot=0),
                runtime_id=runtime,
            ),
        )
    assert not factory.transports[-1].alive
    assert not factory.transports[-1].turns
    assert supervisor.status("athr_source").native_session_id == "native-source"


def test_settlement_cannot_overwrite_the_previously_retained_native_session():
    factory = _TransportFactory()
    supervisor = SharedChatSessionSupervisor(_Manager(), transport_factory=factory)
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
    factory.transports[-1].native_session_id = "unexpected-new-session"
    with pytest.raises(SandboxError, match="different native session"):
        supervisor.settle(
            "athr_source",
            lease.token,
            job_id="ajob_bad",
            context_status="restored",
            native_session_id="unexpected-new-session",
        )
    descriptor = supervisor.status("athr_source")
    assert descriptor.native_session_id == "native-source"
    assert descriptor.last_settled_job_id == "ajob_source"


def test_turn_boundary_preserves_reviewed_resident_tree_and_reaps_new_processes():
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
    supervisor.complete_turn("athr_source", lease.token, turn)

    assert manager.events[-3:] == [
        ("census", 0),
        ("reap-except", 0, ((41, 400),)),
        ("normalize", 0),
    ]
    assert supervisor.status("athr_source").resident_state == "live"


def test_kata_turn_observation_is_uid_scoped_and_closes_before_platform_cleanup():
    class Manager(_Manager):
        def finish_guest_observation(self, spec, cursor):
            observation = super().finish_guest_observation(spec, cursor)
            return replace(
                observation,
                tool_marks=(
                    ToolMark(10.0, "pre", "toolu_1"),
                    ToolMark(10.1, "post", "toolu_1"),
                ),
            )

        def tool_mark_reader(self, _spec):
            raise AssertionError("Kata must not trust the agent-owned mark file")

    manager = Manager()
    manager.backend.runtime = KATA_RUNTIME
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
        prompt="run a sub-millisecond command",
        model="glm-5.1",
        reasoning_effort=None,
    )
    telemetry = supervisor.complete_turn("athr_source", lease.token, turn)

    assert telemetry.complete is True
    assert telemetry.tool_marks == (
        ToolMark(10.0, "pre", "toolu_1"),
        ToolMark(10.1, "post", "toolu_1"),
    )
    # The window is fenced by the observation that reads it back: the opening
    # census lands after the fence is taken, the closing census lands before
    # the fence is read, and the platform's own cleanup follows both so it is
    # never billed to the member.
    assert manager.events[-8:] == [
        ("census", 0),
        ("observe-begin", 11001),
        ("member-window", "register"),
        ("member-window", "sample"),
        ("observe-finish", 11001),
        ("member-window", "close"),
        ("reap-except", 0, ((41, 400),)),
        ("normalize", 0),
    ]


def test_kata_observer_failure_keeps_the_source_and_reports_a_gap():
    class Manager(_Manager):
        def finish_guest_observation(self, _spec, _cursor):
            return GuestObservation.unavailable("observer stopped")

    manager = Manager()
    manager.backend.runtime = KATA_RUNTIME
    supervisor = SharedChatSessionSupervisor(manager, transport_factory=_TransportFactory())
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

    assert telemetry.source == GUEST_OBSERVER_SOURCE
    assert telemetry.complete is False


def test_lost_shared_residency_preserves_observation_until_turn_closes():
    manager = _Manager()
    manager.backend.runtime = KATA_RUNTIME
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

    supervisor.residency_lost("athr_source", generation=1)

    assert turn.interrupted is True
    # Losing residency interrupts the member but must not close the window
    # early: the turn is still the turn, and its closing census belongs to
    # ``complete_turn`` like every other activation's.
    assert ("member-window", "close") not in manager.events

    telemetry = supervisor.complete_turn("athr_source", lease.token, turn)

    assert telemetry.complete is True
    assert manager.events[-5:] == [
        ("member-window", "sample"),
        ("observe-finish", 11001),
        ("member-window", "close"),
        ("reap-except", 0, ((41, 400),)),
        ("normalize", 0),
    ]


def test_supervisor_close_releases_an_active_turn_observation_and_member_lock():
    manager = _Manager()
    manager.backend.runtime = KATA_RUNTIME
    supervisor = SharedChatSessionSupervisor(manager, transport_factory=_TransportFactory())
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
    supervisor.start_turn(
        "athr_source",
        lease.token,
        prompt="work",
        model="glm-5.1",
        reasoning_effort=None,
    )
    member = supervisor._members["athr_source"].members["athr_source"]

    supervisor.close()

    assert manager.events[-1] == ("observe-finish", 11001)
    assert member.turn_lock.locked() is False
    assert member.activation_processes is None


def test_supervisor_close_does_not_steal_an_initialization_lock():
    manager = _Manager()
    supervisor = SharedChatSessionSupervisor(manager, transport_factory=_TransportFactory())
    supervisor.acquire(
        _spec(),
        thread_id="athr_source",
        owner="attempt:1",
        activation_generation=1,
        has_settled_context=False,
        manifest=_members(),
    )
    member = supervisor._members["athr_source"].members["athr_source"]
    member.turn_lock.acquire()

    supervisor.close()

    assert member.turn_lock.locked() is True
    member.turn_lock.release()


def test_abandon_releases_observation_when_normal_turn_cleanup_fails(monkeypatch):
    manager = _Manager()
    manager.backend.runtime = KATA_RUNTIME
    supervisor = SharedChatSessionSupervisor(manager, transport_factory=_TransportFactory())
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
    supervisor.start_turn(
        "athr_source",
        lease.token,
        prompt="work",
        model="glm-5.1",
        reasoning_effort=None,
    )

    def fail_cleanup(*_args, **_kwargs):
        raise RuntimeError("cleanup failed")

    monkeypatch.setattr(supervisor, "complete_turn", fail_cleanup)
    supervisor.abandon("athr_source", lease.token, lost=True)

    assert manager.events[-1] == ("observe-finish", 11001)


def test_shared_claude_turn_fences_and_returns_exact_tool_marks():
    """The supervisor must carry the retained hook slice to the broker."""

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
    assert manager.events[-5:] == [
        ("census", 0),
        ("marks-begin",),
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
    no event stream involved.
    """
    reported: list[tuple[str, dict]] = []
    manager = _Manager()
    manager.backend.runtime = KATA_RUNTIME
    supervisor = SharedChatSessionSupervisor(
        manager,
        transport_factory=_TransportFactory(),
        window_sample_interval_s=10.0,
        checkpoint_interval_s=0.0,
        window_checkpoint_hook=lambda thread_id, snapshot: reported.append((thread_id, snapshot)),
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

    supervisor.start_turn(
        "athr_source",
        lease.token,
        prompt="work",
        model="glm-5.1",
        reasoning_effort=None,
    )
    assert reported == []

    supervisor.sample_open_windows()

    # A checkpoint was produced and is waiting for the turn stream to forward,
    # without a single turn event having been read.
    assert len(reported) == 1
    assert reported[0][0] == "athr_source"
    assert reported[0][1]["state"] == "collecting"
    pending = supervisor.take_reported_checkpoint("athr_source")
    assert pending is not None and pending["state"] == "collecting"
    assert pending["cpu"]["capacity_cores"] == 2.0
    # Taking it is what makes forwarding happen once rather than repeatedly.
    assert supervisor.take_reported_checkpoint("athr_source") is None


def test_checkpoints_are_produced_without_a_hook_because_production_has_none():
    """The delivery gate must not depend on an observer nobody installs.

    The broker builds its supervisor without a checkpoint hook. An earlier
    version returned early when the hook was absent, so no checkpoint was ever
    produced in production while a test that passed its own hook stayed green --
    the same shape of gap as a fixture that does not match the producer.
    """
    manager = _Manager()
    manager.backend.runtime = KATA_RUNTIME
    supervisor = SharedChatSessionSupervisor(
        manager,
        transport_factory=_TransportFactory(),
        window_sample_interval_s=10.0,
        checkpoint_interval_s=0.0,
    )
    assert supervisor._window_checkpoint_hook is None

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

    supervisor.start_turn(
        "athr_source",
        lease.token,
        prompt="work",
        model="glm-5.1",
        reasoning_effort=None,
    )
    supervisor.sample_open_windows()

    pending = supervisor.take_reported_checkpoint("athr_source")
    assert pending is not None
    assert pending["state"] == "collecting"


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
        checkpoint = supervisor.take_reported_checkpoint("athr_source")
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

    A group can outlive the broker that made it, and a container that was
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


def test_broker_sub_stages_are_reported_when_this_process_is_profiling(monkeypatch, tmp_path):
    """With the broker's switch on, the container work comes back as durations."""
    monkeypatch.setenv(operation_profiling.ENV_PROFILING_ENABLED, "true")
    monkeypatch.setenv(
        operation_profiling.ENV_PROFILING_RUN,
        json.dumps(
            {
                "run_id": "broker-stages-1",
                "operations": ["attempt.startup"],
                "expires_at": (datetime.now(timezone.utc) + timedelta(minutes=15)).isoformat(),
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
            "run_id": "broker-stages-1",
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


def _enable_broker_profiling(monkeypatch) -> None:
    monkeypatch.setenv(operation_profiling.ENV_PROFILING_ENABLED, "true")
    monkeypatch.setenv(
        operation_profiling.ENV_PROFILING_RUN,
        json.dumps(
            {
                "run_id": "broker-context-1",
                "operations": ["attempt.startup"],
                "expires_at": (datetime.now(timezone.utc) + timedelta(minutes=15)).isoformat(),
                "max_operations": 10,
            }
        ),
    )
    operation_profiling.reset_cache()


def test_a_lapsed_selection_stops_the_broker_recording(monkeypatch):
    """A run that already expired must not collect container sub-stages.

    Regression: the broker checked only that its cached run existed, so it
    kept measuring after the runner had stopped.
    """
    _enable_broker_profiling(monkeypatch)
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
            "run_id": "broker-context-1",
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
    """The broker honours only the run configured here."""
    _enable_broker_profiling(monkeypatch)
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
    """With a matching, unexpired selection the broker reports its work."""
    _enable_broker_profiling(monkeypatch)
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
            "run_id": "broker-context-1",
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
    stages, and the caller read that as never having heard from the broker.
    """
    _enable_broker_profiling(monkeypatch)
    manager = _Manager()
    factory = _TransportFactory()
    supervisor = SharedChatSessionSupervisor(manager, transport_factory=factory)
    live = {
        "run_id": "broker-context-1",
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

    Regression, at the response the runner actually reads: the broker's local
    deadline lapsed while it waited for the member lock, the CLI start was
    never timed, and the lease still reported a completed collection.
    """
    _enable_broker_profiling(monkeypatch)
    live = {
        "run_id": "broker-context-1",
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
