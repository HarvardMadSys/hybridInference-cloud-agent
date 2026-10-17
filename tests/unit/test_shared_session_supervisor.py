"""Concurrency and capture barriers for shared live chat sandboxes."""

from __future__ import annotations

import io
import json
import logging
import os
import shutil
import stat
import subprocess
import tarfile
import tempfile
import threading
import time
from dataclasses import asdict, replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
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
from cloud_agent_host.drivers import DriverError, InputOutcome
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
from cloud_agent_host.sandbox_manager import SharedSandboxIdentityRequest, create_app
from cloud_agent_host.session_lifecycle import SessionBusy, SessionLeaseLost
from cloud_agent_host.shared_session_supervisor import (
    SharedChatSessionSupervisor,
    SharedMemberManifest,
    SharedSessionDescriptor,
    SharedSessionLease,
    archive_failure_http_detail,
)
from cloud_agent_host.tool_windows import ToolMark
from cloud_agent_shared import operation_profiling
from cloud_agent_shared.errors import SandboxArchiveBlocked
from cloud_agent_shared.sandbox_archive_settings import SandboxArchivePolicy

#: How long a call on another group may take while one group's Docker call or
#: hook is held. Only a guard against a hang: a held call waits for the test
#: longer than this (_HELD_S), so a call queued behind the supervisor-wide lock
#: still runs out of time, however late the machine runs the calling thread.
_UNBLOCKED_S = 5
_HELD_S = 10


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
        # Renewals of the host-rule verification the sweep makes while idle.
        self.host_rule_renewals = 0
        self.host_rule_renewal_failures = 0
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

    def ensure_downloaded_runtime(self, _spec: ChatSandboxSpec, runtime: str, version: str) -> None:
        """The image already holds every version these tests launch."""
        self.runtimes_ensured = [*getattr(self, "runtimes_ensured", []), (runtime, version)]

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

    def renew_host_rules(self) -> None:
        self.host_rule_renewals += 1
        if self.host_rule_renewal_failures:
            self.host_rule_renewal_failures -= 1
            raise SandboxError("nft failed: Error: Could not process rule")

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


def test_abandon_before_first_native_locator_keeps_member_stopped():
    manager = _Manager()
    supervisor = SharedChatSessionSupervisor(manager)
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

    descriptor = supervisor.abandon("athr_source", lease.token, lost=False)

    assert descriptor.native_session_id is None
    assert descriptor.resident_state == "stopped"


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
def test_sandbox_manager_reconcile_blocks_an_unmarked_running_orphan_before_accepting_requests(
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


class _FinalMarkerManager(_Manager):
    """Keep one final marker with the exact running/stopped writer contract."""

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


class _NoUploadArchiveService:
    """An enabled archive service that fails the test if anything uploads."""

    def __init__(self, _config) -> None:
        return

    def start(self) -> None:
        return

    def close(self) -> None:
        return

    def status(self):
        return {"enabled": True, "running": True}

    def queue_final(self, *_args, **_kwargs):
        raise AssertionError("unknown native history must never upload")


def _enabled_archive_policy() -> SandboxArchivePolicy:
    return SandboxArchivePolicy(
        enabled=True,
        region="us-east-1",
        kms_key_arn="arn:aws:kms:us-east-1:123456789012:key/12345678-1234-1234-1234-123456789abc",
        expected_bucket_owner="123456789012",
        revision=1,
    )


@pytest.mark.parametrize("enabled", [False, True], ids=["disabled", "enabled"])
@pytest.mark.parametrize("running", [True, False])
def test_reconcile_finishes_a_markerless_orphan_without_upload(
    running: bool,
    enabled: bool,
) -> None:
    """A crash, reboot or stop behind the broker's back must not strand the chat.

    With archives enabled the unknown native history is never archived: the
    controller records a skipped final and the group starts again.
    """
    manager = _FinalMarkerManager()
    spec = _spec()
    manager.items[spec.container_name] = manager._inspection(spec, running=running)
    controller = SandboxArchiveController(
        manager,  # type: ignore[arg-type]
        namespace="pool-east",
        service_factory=_NoUploadArchiveService,  # type: ignore[arg-type]
    )
    controller.start()
    policy = (
        _enabled_archive_policy() if enabled else SandboxArchivePolicy(enabled=False, revision=1)
    )
    controller.apply(policy.as_dict())
    supervisor = SharedChatSessionSupervisor(
        manager,
        stopped_archive_prepare_hook=controller.prepare_final,
        stopped_archive_invalidate_hook=controller.invalidate_final,
        starting_archive_prepare_hook=controller.prepare_start,
        started_archive_hook=controller.complete_start,
        markerless_final_prepare_hook=controller.prepare_markerless_final,
        stopped_archive_hook=controller.queue_final,
    )

    descriptors = supervisor.reconcile()

    assert descriptors[0].running is False
    assert manager.items[spec.container_name].networks == ()
    assert manager.stop_count == 2
    assert manager.running_marker_writes == int(running)
    assert manager.stopped_marker_writes == int(not running)
    assert manager.marker is not None
    expected_state = b'"state":"skipped"' if enabled else b'"state":"pending"'
    assert expected_state in manager.marker
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

    # Without an invalidate hook nothing can record a skip, so it stays fenced.
    with pytest.raises(SandboxError, match="skip could not be persisted"):
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
            assert allow_archive.wait(timeout=_HELD_S)

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
    assert status_done.wait(timeout=_UNBLOCKED_S), "archive I/O held the supervisor-wide lock"
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


def test_targeted_listing_refreshes_all_members_without_host_reconciliation(monkeypatch):
    manager = _Manager()
    supervisor = SharedChatSessionSupervisor(manager)
    supervisor.register_manifest(_spec(), _members())
    supervisor.terminal_opened("athr_peer")
    supervisor.terminal_opened("athr_peer")
    supervisor.operation_opened("athr_source")
    original = manager.items[_spec().container_name]
    manager.items[_spec().container_name] = replace(
        original,
        running=False,
        status=ChatSandboxStatus.STOPPED,
    )
    inspected: list[str] = []

    def inspect(container_name):
        inspected.append(container_name)
        return manager.items[container_name]

    def inventory():
        raise SandboxError("unrelated sandbox inventory failed")

    monkeypatch.setattr(manager, "inspect", inspect)
    monkeypatch.setattr(manager, "list", inventory)

    descriptors = supervisor.list_for_thread("athr_peer")

    assert inspected == [_spec().container_name]
    assert {row.thread_id for row in descriptors} == {"athr_source", "athr_peer"}
    assert sum(row.terminal_count for row in descriptors) == 2
    assert sum(row.operation_count for row in descriptors) == 1
    assert all(row.running is False and row.group_busy for row in descriptors)
    assert manager.stop_count == 0
    assert manager.pruned_specs == []
    with pytest.raises(SandboxError, match="unrelated sandbox inventory failed"):
        supervisor.list()


def test_a_restarted_supervisor_knows_a_group_by_its_origin_until_a_manifest_names_the_rest():
    """The chats a group-level request, such as an SSH push, can be addressed to.

    After a restart the container's labels name only the origin chat. A
    manifest then names the chats that hold slots, and drops an origin that
    gave its slot up, as a deleted origin chat does.
    """
    manager = _Manager()
    SharedChatSessionSupervisor(manager).register_manifest(_spec(), _members())
    manager.items[_spec().container_name] = manager._inspection(_spec(), running=False)

    restarted = SharedChatSessionSupervisor(manager)
    restarted.reconcile()

    assert restarted.owns("athr_source")
    assert not restarted.owns("athr_peer")

    restarted.register_manifest(_spec(), [_members()[1]])

    assert restarted.owns("athr_peer")
    assert not restarted.owns("athr_source")


def test_targeted_listing_unknown_member_does_not_discover_or_inspect_containers(monkeypatch):
    manager = _Manager()
    supervisor = SharedChatSessionSupervisor(manager)
    monkeypatch.setattr(manager, "list", lambda: pytest.fail("unexpected inventory"))
    monkeypatch.setattr(manager, "inspect", lambda _name: pytest.fail("unexpected inspection"))

    with pytest.raises(SandboxError, match="no shared sandbox membership"):
        supervisor.list_for_thread("athr_unknown")


@pytest.mark.parametrize("missing", [False, True])
def test_targeted_listing_marks_missing_or_replaced_container_lost(missing):
    manager = _Manager()
    supervisor = SharedChatSessionSupervisor(manager)
    supervisor.register_manifest(_spec(), _members())
    original = manager.items[_spec().container_name]
    if missing:
        manager.items.pop(_spec().container_name)
    else:
        manager.items[_spec().container_name] = replace(
            original,
            container_id="container-replacement",
        )

    with pytest.raises(RetainedChatLost, match="missing" if missing else "identity changed"):
        supervisor.list_for_thread("athr_peer")

    group = supervisor._members["athr_source"]
    assert group.inspection == original
    assert group.container_lost is True
    assert {member.context_status for member in group.members.values()} == {"lost"}
    assert manager.stop_count == 0


def test_targeted_listing_rejects_changed_immutable_labels_without_accepting_inspection():
    manager = _Manager()
    supervisor = SharedChatSessionSupervisor(manager)
    supervisor.register_manifest(_spec(), _members())
    original = manager.items[_spec().container_name]
    manager.items[_spec().container_name] = replace(
        original,
        labels=replace(_spec(), generation=2).docker_labels(),
        running=False,
        status=ChatSandboxStatus.STOPPED,
    )

    with pytest.raises(SandboxError, match="immutable labels changed"):
        supervisor.list_for_thread("athr_source")

    assert supervisor._members["athr_source"].inspection == original
    assert manager.stop_count == 0


def test_targeted_listing_inspection_does_not_block_another_groups_status(monkeypatch):
    manager = _Manager()
    supervisor = SharedChatSessionSupervisor(manager)
    supervisor.register_manifest(_spec(), _members())
    other = replace(_spec(), thread_id="athr_other", sandbox_id="asbx_other")
    supervisor.register_manifest(
        other,
        [replace(_members()[0], thread_id=other.thread_id)],
    )
    inspect_entered = threading.Event()
    allow_inspect = threading.Event()
    status_done = threading.Event()
    errors: list[BaseException] = []

    def inspect(container_name):
        if container_name == _spec().container_name:
            inspect_entered.set()
            assert allow_inspect.wait(timeout=_HELD_S)
        return manager.items[container_name]

    monkeypatch.setattr(manager, "inspect", inspect)

    def targeted_listing():
        try:
            supervisor.list_for_thread("athr_source")
        except BaseException as exc:  # pragma: no cover - asserted below
            errors.append(exc)

    def other_status():
        try:
            supervisor.status(other.thread_id)
        except BaseException as exc:  # pragma: no cover - asserted below
            errors.append(exc)
        finally:
            status_done.set()

    listing_thread = threading.Thread(target=targeted_listing)
    status_thread = threading.Thread(target=other_status)
    listing_thread.start()
    try:
        assert inspect_entered.wait(timeout=1)
        status_thread.start()
        assert status_done.wait(timeout=_UNBLOCKED_S), "inspection held the supervisor-wide lock"
    finally:
        allow_inspect.set()
        listing_thread.join(timeout=2)
        if status_thread.ident is not None:
            status_thread.join(timeout=2)

    assert not listing_thread.is_alive()
    assert not status_thread.is_alive()
    assert errors == []


def test_targeted_listing_rejects_a_stopped_observation_after_concurrent_start(monkeypatch):
    manager = _Manager()
    spec = _spec()
    manager.items[spec.container_name] = manager._inspection(spec, running=False)
    supervisor = SharedChatSessionSupervisor(manager)
    supervisor.register_manifest(spec, _members())
    group = supervisor._members[spec.thread_id]
    original_generation = group.lifecycle_generation
    inspect_entered = threading.Event()
    allow_inspect = threading.Event()
    errors: list[BaseException] = []

    def inspect(container_name):
        observed = manager.items[container_name]
        if not inspect_entered.is_set():
            inspect_entered.set()
            assert allow_inspect.wait(timeout=2)
        return observed

    monkeypatch.setattr(manager, "inspect", inspect)

    def targeted_listing():
        try:
            supervisor.list_for_thread(spec.thread_id)
        except BaseException as exc:
            errors.append(exc)

    listing_thread = threading.Thread(target=targeted_listing)
    listing_thread.start()
    try:
        assert inspect_entered.wait(timeout=1)
        token, opened = supervisor.terminal_opened("athr_peer")
        assert opened.running is True
    finally:
        allow_inspect.set()
        listing_thread.join(timeout=2)

    assert not listing_thread.is_alive()
    assert len(errors) == 1
    assert isinstance(errors[0], SessionLeaseLost)
    assert group.lifecycle_generation == original_generation
    assert group.inspection.running is True
    assert manager.items[spec.container_name].running is True
    supervisor.terminal_closed("athr_peer", token)


@pytest.mark.parametrize(
    "changed_fence",
    ["group_index", "member_index", "member", "lifecycle", "spec", "container"],
)
def test_targeted_listing_rejects_a_changed_fence_during_inspection(monkeypatch, changed_fence):
    manager = _Manager()
    supervisor = SharedChatSessionSupervisor(manager)
    supervisor.register_manifest(_spec(), _members())
    group = supervisor._members["athr_source"]
    original = group.inspection
    stale = replace(original, running=False, status=ChatSandboxStatus.STOPPED)

    def inspect(_container_name):
        with supervisor._lock:
            if changed_fence == "group_index":
                supervisor._groups.pop(_spec().sandbox_id)
            elif changed_fence == "member_index":
                supervisor._members.pop("athr_source")
            elif changed_fence == "member":
                group.members["athr_source"] = replace(group.members["athr_source"])
            elif changed_fence == "lifecycle":
                group.lifecycle_generation += 1
            elif changed_fence == "spec":
                group.spec = replace(group.spec, generation=2)
            else:
                group.inspection = replace(original, container_id="container-successor")
        return stale

    monkeypatch.setattr(manager, "inspect", inspect)

    with pytest.raises(SessionLeaseLost, match="lifecycle changed during observation"):
        supervisor.list_for_thread("athr_source")

    assert group.inspection.running is True
    assert group.container_lost is False
    assert manager.stop_count == 0


def _gated_inspect(manager, monkeypatch, container_name):
    """Hold the next inspect of ``container_name`` until the test releases it."""
    entered = threading.Event()
    release = threading.Event()
    inspect = manager.inspect

    def gated(name):
        observed = inspect(name)
        if name == container_name and not entered.is_set():
            entered.set()
            assert release.wait(timeout=_HELD_S)
        return observed

    monkeypatch.setattr(manager, "inspect", gated)
    return entered, release


def _in_thread(call):
    """Run ``call`` on a thread; return the thread and its result holder."""
    outcome: dict[str, Any] = {}

    def run():
        try:
            outcome["value"] = call()
        except BaseException as exc:  # pragma: no cover - asserted by callers
            outcome["error"] = exc

    thread = threading.Thread(target=run)
    thread.start()
    return thread, outcome


@pytest.mark.parametrize("call", ["status", "acquire", "snapshot", "initial_archive"])
def test_a_member_inspection_does_not_block_another_groups_status(monkeypatch, call):
    """Docker inspect runs outside the supervisor-wide lock.

    Every terminal request resolves its member through ``status``; holding the
    lock across its inspect stalled all other groups' lifecycle calls, and the
    sandbox manager's event loop with them, for as long as Docker took to answer.
    """
    manager = _Manager()
    supervisor = SharedChatSessionSupervisor(manager, transport_factory=_TransportFactory())
    supervisor.register_manifest(_spec(), _members())
    other = replace(_spec(), thread_id="athr_other", sandbox_id="asbx_other")
    supervisor.register_manifest(other, [replace(_members()[0], thread_id=other.thread_id)])
    lease = None
    if call == "initial_archive":
        lease = supervisor.acquire(
            _spec(),
            thread_id="athr_source",
            owner="attempt:1",
            activation_generation=1,
            has_settled_context=False,
            manifest=_members(),
        )
    entered, release = _gated_inspect(manager, monkeypatch, _spec().container_name)

    def blocked():
        if call == "status":
            return supervisor.status("athr_source")
        if call == "snapshot":
            return supervisor.capture_snapshot("athr_source", capture=lambda _backend: {})
        if call == "initial_archive":
            assert lease is not None
            return supervisor.capture_initial_archive(
                "athr_source", lease.token, capture=lambda *_args: {}
            )
        return supervisor.acquire(
            _spec(),
            thread_id="athr_source",
            owner="attempt:1",
            activation_generation=1,
            has_settled_context=True,
            manifest=_members(),
        )

    blocked_thread, blocked_outcome = _in_thread(blocked)
    try:
        assert entered.wait(timeout=1)
        status_thread, status_outcome = _in_thread(lambda: supervisor.status(other.thread_id))
        status_thread.join(timeout=_UNBLOCKED_S)
        assert not status_thread.is_alive(), "an inspection held the supervisor-wide lock"
    finally:
        release.set()
        blocked_thread.join(timeout=2)

    assert not blocked_thread.is_alive()
    assert "error" not in blocked_outcome
    assert "error" not in status_outcome


def test_retiring_a_cli_at_settlement_does_not_hold_the_supervisor_lock():
    """A CLI that must retire after its turn is closed when its member settles.

    The close waits for the CLI to flush its native history and, when it does
    not exit in time, for a Docker exec to kill it: seconds, up to that exec's
    timeout. Holding the supervisor-wide lock across it stalled every other
    group's lifecycle calls, the host listing among them. The member's own next
    process still waits for the close, so it never resumes unflushed history.
    """
    _manager, supervisor, lease = _question_activation()
    factory = supervisor._transport_factory
    other = replace(_spec(), thread_id="athr_other", sandbox_id="asbx_other")
    supervisor.register_manifest(other, [replace(_members()[0], thread_id=other.thread_id)])
    retiring = factory.transports[0]
    retiring.retire_after_turn = True
    entered = threading.Event()
    release = threading.Event()

    def flushing_close() -> None:
        entered.set()
        assert release.wait(timeout=_HELD_S)
        retiring.alive = False

    retiring.close = flushing_close
    settle_thread, settle_outcome = _in_thread(
        lambda: supervisor.settle(
            "athr_source", lease.token, job_id="ajob_next", context_status="live"
        )
    )
    resume_thread = None
    try:
        assert entered.wait(timeout=1)
        status_thread, status_outcome = _in_thread(lambda: supervisor.status(other.thread_id))
        status_thread.join(timeout=_UNBLOCKED_S)
        assert not status_thread.is_alive(), "a retiring CLI's close held the supervisor lock"
        assert "error" not in status_outcome
        nxt = supervisor.acquire(
            _spec(),
            thread_id="athr_source",
            owner="attempt:2",
            activation_generation=2,
            has_settled_context=True,
            manifest=_members(),
        )
        resume = replace(
            _launch("athr_source", native_session_id="native-source", slot=0),
            credential="grant-2",
            residency_generation=2,
        )
        resume_thread, resume_outcome = _in_thread(
            lambda: supervisor.ensure_resident("athr_source", nxt.token, resume)
        )
        resume_thread.join(timeout=0.3)
        assert resume_thread.is_alive(), "the next process started before the old one closed"
        assert len(factory.opened) == 1
    finally:
        release.set()
        settle_thread.join(timeout=2)
        if resume_thread is not None:
            resume_thread.join(timeout=2)

    assert not settle_thread.is_alive()
    settled = settle_outcome["value"]
    assert settled.busy is False
    assert settled.resident_state == "restarting"
    assert settled.native_session_id == "native-source"
    assert resume_thread is not None and not resume_thread.is_alive()
    assert resume_outcome["value"].resident_action == "resume"
    assert len(factory.opened) == 2


def test_a_retiring_cli_that_fails_to_close_still_settles_its_member():
    """The settlement's native locator is recorded before the CLI retires.

    A close that cannot confirm the CLI's exit, such as a Docker exec kill
    timing out, used to fail the settlement after recording it, with the lease
    still held. The runner's retry then found no live transport to settle, so
    a turn that had completed ended with its chat's context lost. The next
    process of the member reaps its slot before it starts.
    """
    _manager, supervisor, lease = _question_activation()
    retiring = supervisor._transport_factory.transports[0]
    retiring.retire_after_turn = True

    def failing_close() -> None:
        retiring.alive = False
        raise SandboxError("resident process cleanup failed: Docker exec timed out")

    retiring.close = failing_close

    settled = supervisor.settle(
        "athr_source", lease.token, job_id="ajob_next", context_status="live"
    )

    assert settled.busy is False
    assert settled.resident_state == "restarting"
    assert settled.context_status == "live"
    assert settled.native_session_id == "native-source"
    assert settled.last_settled_job_id == "ajob_next"
    replayed = supervisor.settle(
        "athr_source", lease.token, job_id="ajob_next", context_status="live"
    )
    assert replayed == settled


def test_environment_preparation_reads_docker_outside_the_supervisor_lock(monkeypatch):
    """Preparation inspects the container and reads its receipt without the lock.

    Every turn with an environment binding prepares first, and so does every
    terminal that requires its environment. Holding the supervisor-wide lock
    across those reads stalled every other group for as long as Docker took.
    """
    manager = _Manager()
    supervisor = SharedChatSessionSupervisor(manager)
    supervisor.register_manifest(_spec(), _members())
    other = replace(_spec(), thread_id="athr_other", sandbox_id="asbx_other")
    supervisor.register_manifest(other, [replace(_members()[0], thread_id=other.thread_id)])
    inspect = manager.inspect
    locked_inspections: list[str] = []

    def observed_inspect(name):
        if supervisor._lock._is_owned():
            locked_inspections.append(name)
        return inspect(name)

    monkeypatch.setattr(manager, "inspect", observed_inspect)
    entered = threading.Event()
    release = threading.Event()

    def receipt(backend, binding_id):
        # EnvironmentPreparation.status inspects the container on a cache miss.
        backend.manager.inspect(backend.spec.container_name)
        entered.set()
        assert release.wait(timeout=_HELD_S)
        return {"binding_id": binding_id, "ready": True}

    preparer = SimpleNamespace(status=receipt)
    environment = SimpleNamespace(binding_id="binding-1")
    preparing, outcome = _in_thread(
        lambda: supervisor.prepare_environment("athr_source", environment, preparer)
    )
    try:
        assert entered.wait(timeout=1)
        status_thread, status_outcome = _in_thread(lambda: supervisor.status(other.thread_id))
        status_thread.join(timeout=_UNBLOCKED_S)
        assert not status_thread.is_alive(), "preparation held the supervisor-wide lock"
    finally:
        release.set()
        preparing.join(timeout=2)

    assert not preparing.is_alive()
    assert outcome == {"value": {"binding_id": "binding-1", "ready": True}}
    assert "error" not in status_outcome
    assert locked_inspections == []


@pytest.mark.parametrize("call", ["snapshot", "initial_archive"])
def test_a_capture_that_finds_its_container_missing_lifts_its_barrier(call):
    manager = _Manager()
    supervisor = SharedChatSessionSupervisor(manager)
    lease = supervisor.acquire(
        _spec(),
        thread_id="athr_source",
        owner="attempt:1",
        activation_generation=1,
        has_settled_context=False,
        manifest=_members(),
    )
    if call == "snapshot":
        supervisor.abandon("athr_source", lease.token, lost=False)
    group = supervisor._members["athr_source"]
    del manager.items[_spec().container_name]

    with pytest.raises(RetainedChatLost, match="missing"):
        if call == "snapshot":
            supervisor.capture_snapshot("athr_source", capture=lambda _backend: {})
        else:
            supervisor.capture_initial_archive(
                "athr_source", lease.token, capture=lambda *_args: {}
            )

    assert group.snapshotting is False
    assert manager.events == []


def test_status_keeps_a_concurrent_starts_newer_observation(monkeypatch):
    """A stopped observation taken before a start never replaces the start's."""
    manager = _Manager()
    spec = _spec()
    manager.items[spec.container_name] = manager._inspection(spec, running=False)
    supervisor = SharedChatSessionSupervisor(manager)
    supervisor.register_manifest(spec, _members())
    group = supervisor._members[spec.thread_id]
    entered, release = _gated_inspect(manager, monkeypatch, spec.container_name)

    status_thread, outcome = _in_thread(lambda: supervisor.status(spec.thread_id))
    try:
        assert entered.wait(timeout=1)
        token, opened = supervisor.terminal_opened("athr_peer")
        assert opened.running is True
    finally:
        release.set()
        status_thread.join(timeout=2)

    assert not status_thread.is_alive()
    assert "error" not in outcome
    assert outcome["value"].running is True
    assert group.inspection.running is True
    supervisor._arm_idle_if_quiescent(group)
    supervisor.terminal_closed("athr_peer", token)
    assert group.idle_deadline is not None


def test_creating_a_group_does_not_hold_the_supervisor_lock():
    """Docker create runs outside the lock; only that sandbox's callers wait."""
    creating = threading.Event()
    proceed = threading.Event()

    class Manager(_Manager):
        create_calls = 0

        def create(self, spec, *, facts=None, observer=None):
            self.create_calls += 1
            inspection = super().create(spec, facts=facts, observer=observer)
            if spec.sandbox_id == _spec().sandbox_id:
                creating.set()
                assert proceed.wait(timeout=_HELD_S)
            return inspection

    manager = Manager()
    supervisor = SharedChatSessionSupervisor(manager)
    other = replace(_spec(), thread_id="athr_other", sandbox_id="asbx_other")
    supervisor.register_manifest(other, [replace(_members()[0], thread_id=other.thread_id)])

    first, first_outcome = _in_thread(lambda: supervisor.register_manifest(_spec(), _members()))
    second = None
    try:
        assert creating.wait(timeout=2)
        # Other groups are answered while this one is being created.
        status_thread, status_outcome = _in_thread(lambda: supervisor.status(other.thread_id))
        status_thread.join(timeout=_UNBLOCKED_S)
        assert not status_thread.is_alive(), "Docker create held the supervisor-wide lock"
        assert "error" not in status_outcome
        # Host reconciliation sees the new container but leaves it to its
        # registration rather than adopting (and stopping) it as an orphan.
        supervisor.reconcile()
        assert supervisor.owns("athr_source") is False
        # A second registration of the same sandbox waits for the first.
        second, second_outcome = _in_thread(
            lambda: supervisor.register_manifest(_spec(), _members())
        )
        second.join(timeout=0.3)
        assert second.is_alive()
    finally:
        proceed.set()
        first.join(timeout=2)
        if second is not None:
            second.join(timeout=2)

    assert "error" not in first_outcome
    assert "error" not in second_outcome
    assert manager.create_calls == 2  # the other group, and this one once
    assert manager.stop_count == 0
    assert supervisor.owns("athr_source") and supervisor.owns("athr_peer")
    assert supervisor._creating == set()


def test_a_failed_group_creation_lets_the_next_registration_try_again():
    attempts = []

    class Manager(_Manager):
        def create(self, spec, *, facts=None, observer=None):
            attempts.append(spec.sandbox_id)
            if len(attempts) == 1:
                raise SandboxError("docker create failed")
            return super().create(spec, facts=facts, observer=observer)

    supervisor = SharedChatSessionSupervisor(Manager())

    with pytest.raises(SandboxError, match="docker create failed"):
        supervisor.register_manifest(_spec(), _members())
    assert supervisor._creating == set()
    supervisor.register_manifest(_spec(), _members())

    assert attempts == [_spec().sandbox_id, _spec().sandbox_id]
    assert supervisor.owns("athr_source")


def test_a_sandbox_still_being_created_is_never_proved_absent():
    """Creation runs outside the lock, before its group is installed or Docker names it."""
    creating = threading.Event()
    proceed = threading.Event()

    class Manager(_Manager):
        def create(self, spec, *, facts=None, observer=None):
            creating.set()
            assert proceed.wait(timeout=5)
            return super().create(spec, facts=facts, observer=observer)

    supervisor = SharedChatSessionSupervisor(Manager())
    registration, outcome = _in_thread(lambda: supervisor.register_manifest(_spec(), _members()))
    try:
        assert creating.wait(timeout=2)
        absent = supervisor.admin_deletion_target_absent(
            _spec().thread_id,
            sandbox_id=_spec().sandbox_id,
            container_id="container-not-created-yet",
        )
    finally:
        proceed.set()
        registration.join(timeout=2)

    assert absent is False
    assert "error" not in outcome


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

    descriptors = supervisor.list()

    assert {(row.container_id, row.context_status) for row in descriptors} == {
        ("container-shared", "lost")
    }
    group = supervisor._members["athr_source"]
    assert group.inspection.container_id == "container-shared"
    assert group.container_lost is True
    assert manager.stop_count == 0
    assert archived == []
    with pytest.raises(RetainedChatLost, match="identity was lost"):
        supervisor.stop_if_idle("athr_source", generation=1)


@pytest.mark.asyncio
async def test_a_lost_group_does_not_fail_the_host_listing_of_the_others():
    manager = _Manager()
    other = replace(_spec(), thread_id="athr_other", sandbox_id="asbx_other")
    supervisor = SharedChatSessionSupervisor(manager)
    supervisor.register_manifest(_spec(), _members())
    supervisor.register_manifest(other, [replace(_members()[0], thread_id=other.thread_id)])
    original = manager.items[_spec().container_name]
    manager.items[_spec().container_name] = replace(original, container_id="container-replacement")

    # The first listing finds the replacement; later ones find a group already lost.
    for _ in range(2):
        rows = {row.thread_id: row for row in supervisor.list()}
        assert {thread_id: row.context_status for thread_id, row in rows.items()} == {
            "athr_source": "lost",
            "athr_peer": "lost",
            "athr_other": "live",
        }
        assert rows["athr_source"].container_id == "container-shared"
    assert supervisor._members["athr_source"].container_lost is True
    assert manager.pruned_specs == [_spec(), other]

    app = create_app(
        backend=ProcessBackend(acknowledged_unsafe=True),
        token="sandbox-manager-secret",
        shared_session_supervisor=supervisor,
    )
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://sandbox_manager"
    ) as client:
        listed = await client.get(
            "/sandboxes", headers={"X-Agent-Workspace-Token": "sandbox-manager-secret"}
        )

    assert listed.status_code == 200, listed.text
    assert {row["sandbox_id"] for row in listed.json()["sandboxes"]} == {
        "asbx_shared",
        "asbx_other",
    }
    stopped = supervisor.stop_if_idle(other.thread_id, generation=other.generation)
    assert stopped.running is False


@pytest.mark.asyncio
async def test_a_group_changed_while_it_is_reconciled_does_not_fail_the_host_listing():
    """Reconciliation reads a group's archive marker outside the lock, under a check.

    A container that stopped on its own keeps its bridge, so the next listing
    reads its marker to classify that stop. Another call can change the group
    meanwhile: here a targeted listing finds the container removed and marks
    the group lost. The check then refused the marker's result, and the
    refusal failed the whole host listing, so GET /sandboxes answered 409 and
    the heartbeat carried no group's observation.
    """
    manager = _Manager()
    other = replace(_spec(), thread_id="athr_other", sandbox_id="asbx_other")
    supervisor = SharedChatSessionSupervisor(manager)
    supervisor.register_manifest(_spec(), _members())
    supervisor.register_manifest(other, [replace(_members()[0], thread_id=other.thread_id)])
    name = _spec().container_name
    manager.items[name] = replace(
        manager.items[name], running=False, status=ChatSandboxStatus.STOPPED
    )
    read_marker = manager.read_stopped_final_archive_marker_optional
    concurrent: list[BaseException] = []

    def removed_while_read(spec, *, expected_container_id):
        if spec.sandbox_id == _spec().sandbox_id and name in manager.items:
            del manager.items[name]
            try:
                supervisor.list_for_thread("athr_source")
            except RetainedChatLost as exc:
                concurrent.append(exc)
        return read_marker(spec, expected_container_id=expected_container_id)

    manager.read_stopped_final_archive_marker_optional = removed_while_read
    app = create_app(
        backend=ProcessBackend(acknowledged_unsafe=True),
        token="sandbox-manager-secret",
        shared_session_supervisor=supervisor,
    )
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://sandbox_manager"
    ) as client:
        listed = await client.get(
            "/sandboxes", headers={"X-Agent-Workspace-Token": "sandbox-manager-secret"}
        )

    assert len(concurrent) == 1
    assert listed.status_code == 200, listed.text
    groups = {row["sandbox_id"]: row for row in listed.json()["sandboxes"]}
    assert set(groups) == {"asbx_shared", "asbx_other"}
    assert {member["context_status"] for member in groups["asbx_shared"]["members"]} == {"lost"}
    assert groups["asbx_other"]["running"] is True
    assert manager.pruned_specs == [_spec(), other]
    assert supervisor._members["athr_source"].container_lost is True
    rows = {row.thread_id: row.context_status for row in supervisor.list()}
    assert rows == {"athr_source": "lost", "athr_peer": "lost", "athr_other": "live"}
    assert supervisor.stop_if_idle(other.thread_id, generation=other.generation).running is False


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


def test_externally_stopped_group_under_enabled_archives_skips_and_continues():
    """Docker stopped this group without the broker, e.g. a node move or reboot."""
    manager = _FinalMarkerManager()
    spec = _spec()
    manager.items[spec.container_name] = manager._inspection(spec, running=True)
    controller = SandboxArchiveController(
        manager,  # type: ignore[arg-type]
        namespace="pool-east",
        service_factory=_NoUploadArchiveService,  # type: ignore[arg-type]
    )
    controller.start()
    controller.apply(_enabled_archive_policy().as_dict())
    supervisor = SharedChatSessionSupervisor(
        manager,
        stopped_archive_prepare_hook=controller.prepare_final,
        stopped_archive_invalidate_hook=controller.invalidate_final,
        starting_archive_prepare_hook=controller.prepare_start,
        started_archive_hook=controller.complete_start,
        markerless_final_prepare_hook=controller.prepare_markerless_final,
        stopped_archive_hook=controller.queue_final,
    )
    supervisor.register_manifest(spec, _members(), expected_container_id="container-shared")
    manager.items[spec.container_name] = manager._inspection(spec, running=False)

    descriptors = supervisor.list()

    assert all(item.running is False for item in descriptors)
    assert manager.marker is not None and b'"state":"skipped"' in manager.marker
    assert manager.items[spec.container_name].networks == ()
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
    controller.close()


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
    assert asdict(first)["network_verified"] is True, "what the sandbox manager endpoint sends"
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


def test_network_fail_stop_final_archive_log_omits_provider_details(
    caplog: pytest.LogCaptureFixture,
) -> None:
    class ProviderError(RuntimeError):
        def __init__(self, unsafe: str) -> None:
            super().__init__(unsafe)
            self.response = {
                "Error": {"Code": "AccessDenied", "Message": unsafe},
                "ResponseMetadata": {
                    "HTTPStatusCode": 403,
                    "HTTPHeaders": {"authorization": unsafe},
                },
            }

    unsafe = "https://archive.invalid/object?credential=network-fail-stop-secret"

    def archive(*_args) -> None:
        try:
            raise ProviderError(unsafe)
        except ProviderError as exc:
            raise SandboxError("final archive could not be stored") from exc

    manager, supervisor = _running_group(stopped_archive_hook=archive)
    manager.host_rule_failures = 2

    assert supervisor.guard_network() == []

    assert unsafe not in caplog.text
    assert any(
        getattr(record, "event", None) == "shared_chat_network_final_archive_failed"
        and getattr(record, "detail", None)
        == "archive failure (phase=fail_stop_final, type=ProviderError, "
        "code=AccessDenied, status=403)"
        for record in caplog.records
    )


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


def test_while_nothing_runs_the_rules_are_renewed_not_read_every_pass():
    """An idle host keeps its verification fresh so the next start does not pay for it.

    Nothing is read back every pass and nothing is stopped: the renewal is the
    guard's own cached ensure, which installs and reads back only once the
    last verification is old enough to need it.
    """
    manager = _Manager()
    supervisor = SharedChatSessionSupervisor(manager)

    assert supervisor.guard_network() == []
    assert manager.host_rule_checks == 0
    assert manager.host_rule_renewals == 1
    assert manager.stop_count == 0


def test_an_idle_renewal_that_fails_is_logged_once_and_stops_nothing(caplog):
    manager = _Manager()
    supervisor = SharedChatSessionSupervisor(manager)
    manager.host_rule_renewal_failures = 2

    with caplog.at_level(logging.WARNING):
        assert supervisor.guard_network() == []
        assert supervisor.guard_network() == []
        assert supervisor.guard_network() == []
        supervisor.guard_network()
        manager.host_rule_renewal_failures = 1
        supervisor.guard_network()

    warnings = [
        record
        for record in caplog.records
        if getattr(record, "event", None) == "shared_chat_idle_host_rules_unavailable"
    ]
    assert len(warnings) == 2, "once per failing streak, not once per pass"
    assert manager.host_rule_renewals == 5
    assert manager.stop_count == 0


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


@pytest.mark.parametrize("kind", ["terminal", "operation"])
def test_a_token_still_closes_after_a_manifest_drops_its_opener(kind):
    """A deleted Fork child's PTY or workspace request can outlive its membership.

    A later Fork reclaims the deleted chat's resident slot, so every manifest
    from then on omits the chat, and the host drops the member while its
    token is still open. Closing the token must still release the group;
    otherwise it never goes idle and every publication snapshot is refused.
    """
    now = [1000.0]
    manager = _Manager()
    supervisor = SharedChatSessionSupervisor(manager, idle_timeout_s=60, clock=lambda: now[0])
    origin, child = _members()
    supervisor.register_manifest(_spec(), [origin, child])
    if kind == "terminal":
        token, _ = supervisor.terminal_opened("athr_peer")

        def close():
            return supervisor.terminal_closed("athr_peer", token, last_activity_monotonic=now[0])
    else:
        token, _ = supervisor.operation_opened("athr_peer")

        def close():
            return supervisor.operation_closed("athr_peer", token)

    supervisor.register_manifest(_spec(), [origin])
    assert not supervisor.owns("athr_peer")

    assert close() is None

    [descriptor] = supervisor.list()
    assert descriptor.group_busy is False
    assert descriptor.idle_deadline_monotonic == 1060.0
    with pytest.raises(SandboxError, match="no shared sandbox membership"):
        close()
    assert supervisor.capture_snapshot("athr_source", capture=lambda _backend: {"patch": ""}) == {
        "patch": ""
    }
    now[0] += 60
    assert [stopped.running for stopped in supervisor.sweep()] == [False]
    assert manager.items[_spec().container_name].running is False


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
        # after sandbox manager/container restart would do, against legacy state.
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
        ("codex", "codex-homes"),
        ("opencode", "opencode-homes"),
        ("kilo", "kilo-homes"),
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


#: The image's sandbox user and group, which own /agent-state.
_SANDBOX_UID = 10001
_SETPRIV = shutil.which("setpriv")


class _LocalMemberShell:
    """Run the real member scripts on this host, with /agent-state under a temp root.

    Every path a script touches arrives as an argument, so rebasing the
    ``/agent-state`` arguments is enough to exercise the actual shell. The
    member pipe is a real pipe between the two local processes. With
    ``as_uids`` (root only) each command runs as the UID the sandbox would
    run it as, so the kernel's own permission checks are under test.
    """

    def __init__(self, root: Path, *, as_uids: bool = False) -> None:
        self.root = root
        self.as_uids = as_uids
        uid, gid = (_SANDBOX_UID, _SANDBOX_UID) if as_uids else (os.getuid(), os.getgid())
        self.backend = SimpleNamespace(uid=uid, gid=gid)
        self.calls: list[tuple[object, ...]] = []

    def local(self, path: str) -> Path:
        return Path(f"{self.root}{path}")

    def run_as(self, uid: int | None, gid: int | None, argv: list[str]) -> list[str]:
        argv = [str(self.local(item)) if item.startswith("/agent-state") else item for item in argv]
        if not self.as_uids:
            return argv
        assert _SETPRIV is not None
        uid = self.backend.uid if uid is None else uid
        gid = self.backend.gid if gid is None else gid
        return [_SETPRIV, f"--reuid={uid}", f"--regid={gid}", "--clear-groups", "--", *argv]

    def exec_capture(self, _spec, argv, **kwargs):
        self.calls.append(("exec", kwargs.get("user_uid")))
        return subprocess.run(
            self.run_as(kwargs.get("user_uid"), kwargs.get("user_gid"), argv),
            capture_output=True,
            timeout=30,
            check=False,
        )

    def ensure_private_member_collections(self, spec):
        # The production script, through this shell's exec.
        ChatSandboxManager.ensure_private_member_collections(self, spec)  # type: ignore[arg-type]

    def pipe_member_exec(
        self, _spec, *, source_argv, source_uid, target_argv, target_uid, user_gid, timeout_s
    ):
        self.calls.append(("pipe", source_uid, target_uid))
        source = subprocess.Popen(
            self.run_as(source_uid, user_gid, source_argv),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        assert source.stdout is not None and source.stderr is not None
        target = subprocess.run(
            self.run_as(target_uid, user_gid, target_argv),
            stdin=source.stdout,
            capture_output=True,
            timeout=timeout_s,
            check=False,
        )
        source.stdout.close()
        stderr = source.stderr.read()
        return subprocess.CompletedProcess(source_argv, source.wait(), b"", stderr), target


def _private_state_root(tmp_path: Path) -> Path:
    """Mirror the image: a setgid, group-writable /agent-state."""
    state = tmp_path / "agent-state"
    state.mkdir()
    state.chmod(0o2770)
    (state / "config.toml").write_text("model = 'bootstrap'\n")
    (state / "config.toml").chmod(0o664)
    return state


def _assert_private(home: Path) -> None:
    assert stat.S_IMODE(home.stat().st_mode) == 0o700, oct(home.stat().st_mode)
    for path in home.rglob("*"):
        assert path.lstat().st_mode & 0o077 == 0, (path, oct(path.lstat().st_mode))


def _assert_trusted(state: Path, collection: Path, uid: int) -> None:
    assert state.stat().st_mode & stat.S_ISVTX, "no member may rename a collection away"
    info = collection.lstat()
    assert stat.S_ISDIR(info.st_mode) and info.st_uid == uid
    assert stat.S_IMODE(info.st_mode) == 0o3770, oct(info.st_mode)


@pytest.mark.parametrize("runtime_id", ["codex", "opencode", "kilo"])
def test_a_member_provider_home_is_private_to_its_member(tmp_path: Path, runtime_id: str):
    """A sibling UID shares the group, so group bits would hand it this home."""
    state = _private_state_root(tmp_path)
    manager = _LocalMemberShell(tmp_path)
    factory = ResidentTransportFactory(manager)  # type: ignore[arg-type]
    launch = replace(_launch("athr_member", native_session_id=None, slot=1), runtime_id=runtime_id)
    home = factory._member_state_dir(launch)

    factory._prepare_member_state(_spec(), launch, home)
    factory._prepare_member_state(_spec(), launch, home)

    _assert_private(manager.local(home))
    _assert_trusted(state, manager.local(home).parent, os.getuid())
    # The collection is secured once, as the sandbox user, then only checked.
    assert manager.calls == [
        ("exec", MEMBER_UID_BASE + 1),
        ("exec", None),
        ("exec", MEMBER_UID_BASE + 1),
        ("exec", MEMBER_UID_BASE + 1),
    ]
    if runtime_id == "codex":
        assert (manager.local(home) / "config.toml").read_text() == "model = 'bootstrap'\n"


def test_an_earlier_releases_home_moves_into_the_trusted_collection(tmp_path: Path):
    _private_state_root(tmp_path)
    manager = _LocalMemberShell(tmp_path)
    factory = ResidentTransportFactory(manager)  # type: ignore[arg-type]
    launch = replace(_launch("athr_member", native_session_id=None, slot=1), runtime_id="opencode")
    legacy = manager.local(factory._legacy_member_state_dir(launch))
    (legacy / "opencode/data").mkdir(parents=True)
    (legacy / "opencode/data/opencode.db").write_text("native session")
    (legacy / "opencode/data/opencode.db").chmod(0o660)
    for directory in (legacy.parent, legacy, legacy / "opencode", legacy / "opencode/data"):
        directory.chmod(0o2770)
    (legacy / ".freeinference-member-state-v1").write_text("")

    factory._prepare_member_state(_spec(), launch, factory._member_state_dir(launch))

    home = manager.local(factory._member_state_dir(launch))
    assert not legacy.exists()
    assert (home / "opencode/data/opencode.db").read_text() == "native session"
    _assert_private(home)


def test_a_symlink_at_a_members_home_is_refused_not_adopted(tmp_path: Path):
    _private_state_root(tmp_path)
    manager = _LocalMemberShell(tmp_path)
    factory = ResidentTransportFactory(manager)  # type: ignore[arg-type]
    launch = replace(_launch("athr_member", native_session_id=None, slot=1), runtime_id="codex")
    manager.ensure_private_member_collections(_spec())
    home = manager.local(factory._member_state_dir(launch))
    planted = tmp_path / "sibling-owned"
    planted.mkdir()
    planted.chmod(0o777)
    home.symlink_to(planted, target_is_directory=True)

    with pytest.raises(DriverError, match="not owned by this member"):
        factory._prepare_member_state(_spec(), launch, factory._member_state_dir(launch))

    assert list(planted.iterdir()) == []


def test_a_squatted_collection_name_is_moved_aside_and_replaced(tmp_path: Path):
    state = _private_state_root(tmp_path)
    manager = _LocalMemberShell(tmp_path)
    elsewhere = tmp_path / "sibling-tree"
    elsewhere.mkdir()
    (state / "codex-homes").symlink_to(elsewhere, target_is_directory=True)

    manager.ensure_private_member_collections(_spec())

    for runtime_id in ("codex", "opencode", "kilo"):
        _assert_trusted(state, state / f"{runtime_id}-homes", os.getuid())
    assert [path.name for path in state.glob("codex-homes.refused-*")]
    assert list(elsewhere.iterdir()) == []


@pytest.mark.parametrize("runtime_id", ["codex", "opencode"])
def test_fork_copies_a_private_home_without_widening_either_side(tmp_path: Path, runtime_id: str):
    _private_state_root(tmp_path)
    manager = _LocalMemberShell(tmp_path)
    factory = ResidentTransportFactory(manager)  # type: ignore[arg-type]
    source_launch = replace(
        _launch("athr_source", native_session_id="native-source", slot=0), runtime_id=runtime_id
    )
    target_launch = replace(
        _launch("athr_child", native_session_id=None, slot=2), runtime_id=runtime_id
    )
    source_home = factory._member_state_dir(source_launch)
    factory._prepare_member_state(_spec(), source_launch, source_home)
    source = manager.local(source_home)
    (source / "sessions").mkdir()
    (source / "sessions/rollout.jsonl").write_text('{"id":"native-source"}\n')
    (source / ".hidden").write_text("dotfile")
    (source / "tmp").mkdir(exist_ok=True)
    (source / "tmp/scratch").write_text("never copied")
    (source / f"{runtime_id}/state/{runtime_id}/locks").mkdir(parents=True)
    (source / f"{runtime_id}/state/{runtime_id}/locks/server.lock").write_text("pid")
    manager.calls.clear()

    factory._clone_private_member_state(_spec(), source_launch, target_launch)

    child = manager.local(factory._member_state_dir(target_launch))
    _assert_private(child)
    # The CLI's own files keep its umask; the closed home is what protects them.
    assert stat.S_IMODE(source.stat().st_mode) == 0o700
    assert manager.calls == [("exec", None), ("pipe", MEMBER_UID_BASE + 0, MEMBER_UID_BASE + 2)]
    assert (child / "sessions/rollout.jsonl").read_text() == '{"id":"native-source"}\n'
    assert (child / ".hidden").read_text() == "dotfile"
    assert list((child / "tmp").iterdir()) == []
    assert (child / ".freeinference-member-state-v1").exists()
    locks = child / f"{runtime_id}/state/{runtime_id}/locks"
    assert locks.exists() is (runtime_id == "codex")
    assert (source / "tmp/scratch").exists(), "the source home is read, never changed"


@pytest.fixture
def _multi_uid_state():
    """A /agent-state the image's own UIDs can reach, for kernel-checked tests."""
    if os.geteuid() != 0 or _SETPRIV is None:
        pytest.skip("switching to the sandbox and member UIDs needs root and setpriv")
    base = Path(tempfile.mkdtemp(prefix="member-homes-"))
    try:
        base.chmod(0o755)
        state = base / "agent-state"
        state.mkdir()
        os.chown(state, _SANDBOX_UID, _SANDBOX_UID)
        state.chmod(0o2770)
        yield base, state
    finally:
        shutil.rmtree(base)


def _as_member(manager: _LocalMemberShell, uid: int, script: str, *args: str):
    return subprocess.run(
        manager.run_as(uid, _SANDBOX_UID, ["/bin/sh", "-c", script, "sh", *args]),
        capture_output=True,
        timeout=30,
        check=False,
    )


def test_a_sibling_cannot_swap_a_members_home_before_its_cli_starts(_multi_uid_state):
    """Codex review P1: a 0700 home is not enough in a group-writable collection.

    A sibling could rename the home away and put its own tree at the same path
    between the owner check and the CLI's start. With the sandbox user owning a
    sticky collection inside a sticky /agent-state, the kernel refuses each step.
    """
    base, state = _multi_uid_state
    manager = _LocalMemberShell(base, as_uids=True)
    factory = ResidentTransportFactory(manager)  # type: ignore[arg-type]
    victim = replace(_launch("athr_victim", native_session_id=None, slot=1), runtime_id="codex")
    factory._prepare_member_state(_spec(), victim, factory._member_state_dir(victim))
    home = manager.local(factory._member_state_dir(victim))
    collection = home.parent
    _assert_trusted(state, collection, _SANDBOX_UID)
    assert home.stat().st_uid == MEMBER_UID_BASE + 1
    sibling = MEMBER_UID_BASE

    renamed_home = _as_member(manager, sibling, 'mv -T -- "$1" "$1.away"', str(home))
    renamed_collection = _as_member(manager, sibling, 'mv -T -- "$1" "$1.away"', str(collection))
    removed_home = _as_member(manager, sibling, 'rm -rf -- "$1"', str(home))
    unsticky = _as_member(manager, sibling, 'chmod -t -- "$1"', str(state))

    for attempt in (renamed_home, renamed_collection, removed_home, unsticky):
        assert attempt.returncode != 0
        assert b"ermission denied" in attempt.stderr or b"not permitted" in attempt.stderr
    assert home.is_dir() and home.stat().st_uid == MEMBER_UID_BASE + 1
    _assert_trusted(state, collection, _SANDBOX_UID)
    # The sibling can still make its own home, and the victim restarts cleanly.
    own = replace(_launch("athr_sibling", native_session_id=None, slot=0), runtime_id="codex")
    factory._prepare_member_state(_spec(), own, factory._member_state_dir(own))
    factory._prepare_member_state(_spec(), victim, factory._member_state_dir(victim))


@pytest.mark.parametrize("planted", ["sibling_directory", "sibling_file_inside"])
def test_a_legacy_home_a_sibling_replaced_is_refused_after_the_move(_multi_uid_state, planted):
    """The legacy collection is still swappable, so its entry is judged after the move."""
    base, _state = _multi_uid_state
    manager = _LocalMemberShell(base, as_uids=True)
    factory = ResidentTransportFactory(manager)  # type: ignore[arg-type]
    victim = replace(_launch("athr_victim", native_session_id=None, slot=1), runtime_id="codex")
    legacy = manager.local(factory._legacy_member_state_dir(victim))
    sibling = MEMBER_UID_BASE
    # An earlier release's member-created, group-writable collection.
    made = _as_member(manager, sibling, 'umask 0007; mkdir -p -- "$1"', str(legacy.parent))
    assert made.returncode == 0, made.stderr
    if planted == "sibling_directory":
        script = 'umask 0; mkdir -- "$1"; printf evil > "$1/config.toml"'
        result = _as_member(manager, sibling, script, str(legacy))
    else:
        result = _as_member(manager, MEMBER_UID_BASE + 1, 'umask 0007; mkdir -- "$1"', str(legacy))
        assert result.returncode == 0, result.stderr
        script = 'umask 0; printf evil > "$1/config.toml"'
        result = _as_member(manager, sibling, script, str(legacy))
    assert result.returncode == 0, result.stderr

    with pytest.raises(DriverError, match=r"not owned by this member|another UID"):
        factory._prepare_member_state(_spec(), victim, factory._member_state_dir(victim))

    # Codex review P1 on #568: the next start must not find the refused tree at
    # the home's path, take it for an already migrated home, and adopt it.
    home = manager.local(factory._member_state_dir(victim))
    try:
        factory._prepare_member_state(_spec(), victim, factory._member_state_dir(victim))
    except DriverError:
        pass  # Still refused is safe; adopting the planted tree is not.
    else:
        assert home.stat().st_uid == MEMBER_UID_BASE + 1
        assert not (home / "config.toml").exists(), "the sibling's config was adopted"


def _poison_legacy_home(manager: _LocalMemberShell, factory, victim) -> None:
    """An earlier release's group-writable home a sibling put its config into."""
    legacy = manager.local(factory._legacy_member_state_dir(victim))
    sibling = MEMBER_UID_BASE
    made = _as_member(manager, sibling, 'umask 0007; mkdir -p -- "$1"', str(legacy.parent))
    assert made.returncode == 0, made.stderr
    made = _as_member(manager, victim_uid(victim), 'umask 0007; mkdir -- "$1"', str(legacy))
    assert made.returncode == 0, made.stderr
    made = _as_member(manager, sibling, 'umask 0; printf evil > "$1/config.toml"', str(legacy))
    assert made.returncode == 0, made.stderr


def victim_uid(launch) -> int:
    return MEMBER_UID_BASE + launch.slot_id


def _never_adopted(manager: _LocalMemberShell, factory, victim) -> None:
    """Start again: refused is safe; a home with a sibling's file in it is not."""
    home = manager.local(factory._member_state_dir(victim))
    try:
        factory._prepare_member_state(_spec(), victim, factory._member_state_dir(victim))
    except DriverError as exc:
        assert "another UID" in str(exc) or "not owned by this member" in str(exc)
        return
    assert home.stat().st_uid == victim_uid(victim)
    owners = {path.lstat().st_uid for path in [home, *home.rglob("*")]}
    assert owners == {victim_uid(victim)}, "a sibling-owned entry was adopted"
    config = home / "config.toml"
    assert not config.exists() or config.read_text() != "evil"


def test_a_sibling_holding_every_guessable_quarantine_name_gets_nothing_adopted(
    _multi_uid_state,
):
    """Codex review P1 on #568: the set-aside name must not decide adoption.

    A sibling may create entries in the sticky collection. It takes the name
    the previous release used for the set-aside home, for a whole range of
    PIDs, plus other obvious spellings; the poisoned home is still never
    adopted, on this start or the next.
    """
    base, _state = _multi_uid_state
    manager = _LocalMemberShell(base, as_uids=True)
    factory = ResidentTransportFactory(manager)  # type: ignore[arg-type]
    victim = replace(_launch("athr_victim", native_session_id=None, slot=1), runtime_id="codex")
    manager.ensure_private_member_collections(_spec())
    _poison_legacy_home(manager, factory, victim)
    home = manager.local(factory._member_state_dir(victim))
    squat = (
        'for name in "$@"; do mkdir -- "$name"; : > "$name/keep"; done; '
        'i=1; while [ "$i" -le 2048 ]; do mkdir -- "$0.refused-$i"; i=$((i + 1)); done'
    )
    guessed = [f"{home}.refused", f"{home}.refused-", f"{home}.refused-0", f"{home}.old"]
    squatted = subprocess.run(
        manager.run_as(
            MEMBER_UID_BASE, _SANDBOX_UID, ["/bin/sh", "-c", squat, str(home), *guessed]
        ),
        capture_output=True,
        timeout=60,
        check=False,
    )
    assert squatted.returncode == 0, squatted.stderr

    with pytest.raises(DriverError, match="another UID"):
        factory._prepare_member_state(_spec(), victim, factory._member_state_dir(victim))

    _never_adopted(manager, factory, victim)
    _never_adopted(manager, factory, victim)


def test_a_poisoned_home_is_refused_on_every_start_when_it_cannot_be_set_aside(
    _multi_uid_state, monkeypatch
):
    """Adoption never depends on the set-aside rename succeeding."""
    base, _state = _multi_uid_state
    manager = _LocalMemberShell(base, as_uids=True)
    factory = ResidentTransportFactory(manager)  # type: ignore[arg-type]
    victim = replace(_launch("athr_victim", native_session_id=None, slot=1), runtime_id="codex")
    manager.ensure_private_member_collections(_spec())
    _poison_legacy_home(manager, factory, victim)
    home = manager.local(factory._member_state_dir(victim))
    # Every name mktemp offers is one the sibling already holds.
    stubs = base / "stubs"
    stubs.mkdir()
    stub = stubs / "mktemp"
    stub.write_text('#!/bin/sh\nfor last; do :; done\nprintf "%staken\\n" "${last%%XXXX*}"\n')
    stub.chmod(0o755)
    monkeypatch.setenv("PATH", f"{stubs}:{os.environ['PATH']}")
    taken = _as_member(
        manager, MEMBER_UID_BASE, 'mkdir -- "$1"; : > "$1/keep"', f"{home}.refused-taken"
    )
    assert taken.returncode == 0, taken.stderr

    for _start in range(3):
        with pytest.raises(DriverError, match="another UID"):
            factory._prepare_member_state(_spec(), victim, factory._member_state_dir(victim))
        # The poisoned tree is still at the home's path, and still refused.
        assert (home / "config.toml").read_text() == "evil"


def test_a_clean_home_and_a_fork_copy_pass_the_every_start_check(_multi_uid_state):
    """Every legitimate writer is the member itself, so nothing honest is refused."""
    base, _state = _multi_uid_state
    manager = _LocalMemberShell(base, as_uids=True)
    factory = ResidentTransportFactory(manager)  # type: ignore[arg-type]
    source = replace(
        _launch("athr_source", native_session_id="native-source", slot=0), runtime_id="opencode"
    )
    child = replace(_launch("athr_child", native_session_id=None, slot=2), runtime_id="opencode")
    factory._prepare_member_state(_spec(), source, factory._member_state_dir(source))
    source_home = manager.local(factory._member_state_dir(source))
    written = _as_member(
        manager,
        victim_uid(source),
        'mkdir -p -- "$1/opencode/data"; printf session > "$1/opencode/data/opencode.db"',
        str(source_home),
    )
    assert written.returncode == 0, written.stderr
    factory._prepare_member_state(_spec(), source, factory._member_state_dir(source))

    factory._clone_private_member_state(_spec(), source, child)
    factory._prepare_member_state(_spec(), child, factory._member_state_dir(child))

    child_home = manager.local(factory._member_state_dir(child))
    assert {path.lstat().st_uid for path in [child_home, *child_home.rglob("*")]} == {
        victim_uid(child)
    }
    assert (child_home / "opencode/data/opencode.db").read_text() == "session"


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
async def test_sandbox_manager_fork_materializes_reserved_target_and_replays_stale_manifest(
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
        token="sandbox-manager-secret",
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
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://sandbox_manager"
    ) as client:
        first = await client.post(
            "/shared-sandboxes/athr_source/fork",
            headers={"X-Agent-Workspace-Token": "sandbox-manager-secret"},
            json=body,
        )
        assert first.status_code == 200, first.text
        # Files/Git hydration can register the still-pending database manifest
        # after the host has created the child but before receipt finalization.
        registered = await client.post(
            "/shared-sandboxes/athr_source/register",
            headers={"X-Agent-Workspace-Token": "sandbox-manager-secret"},
            json=SharedSandboxIdentityRequest.model_validate(body).model_dump(),
        )
        assert registered.status_code == 200, registered.text
        # The control plane can retry before storing the receipt, so the
        # durable target manifest still contains no native session identity.
        body["source_launch"]["residency_generation"] = 10
        body["target_launch"]["residency_generation"] = 11
        retried = await client.post(
            "/shared-sandboxes/athr_source/fork",
            headers={"X-Agent-Workspace-Token": "sandbox-manager-secret"},
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
            headers={"X-Agent-Workspace-Token": "sandbox-manager-secret"},
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


def test_a_failed_stop_docker_cannot_inspect_leaves_the_group_unlost():
    """Only Docker's "no such container" loses a group, not a daemon that cannot answer.

    ``docker stop`` times out and so does the inspection that follows it. The
    container never went anywhere: once Docker answers again the group keeps
    its contexts, lists, and stops.
    """

    class DaemonDown(_Manager):
        def __init__(self) -> None:
            super().__init__()
            self.down = False

        def inspect(self, container_name):
            if self.down:
                raise SandboxError("Docker lifecycle command failed: timed out after 30 seconds")
            return super().inspect(container_name)

        def stop(self, spec, **kwargs):
            if self.stop_count == 0:
                self.stop_count += 1
                self.down = True
                raise SandboxError("Docker lifecycle command failed: timed out")
            return super().stop(spec, **kwargs)

    manager = DaemonDown()
    supervisor = SharedChatSessionSupervisor(manager)
    supervisor.register_manifest(_spec(), _members())

    with pytest.raises(SandboxError, match="timed out"):
        supervisor.stop_if_idle("athr_source", generation=1)
    manager.down = False

    group = supervisor._members["athr_source"]
    assert group.container_lost is False
    assert group.stopping is False
    assert group.idle_deadline is not None
    assert {row.context_status for row in supervisor.list()} == {"live"}
    stopped = supervisor.stop_if_idle("athr_source", generation=1)
    assert stopped.running is False
    assert manager.stop_count == 2
    assert manager.items[_spec().container_name].running is False


def test_a_group_lost_in_a_failed_stop_is_never_armed_to_stop_again():
    """A container that really went away during its stop leaves a lost group, not an idle one.

    Its last observation still says running, but a lost group is never stopped
    again. Armed, it was the sweep's candidate on every pass, and the loss it
    raised ended each pass before the groups after it.
    """
    now = 100.0

    class VanishOnStop(_Manager):
        def stop(self, spec, **kwargs):
            if spec.sandbox_id == _spec().sandbox_id:
                # Docker now answers "no such container" for it.
                self.items.pop(spec.container_name)
                raise SandboxError("docker stop failed")
            return super().stop(spec, **kwargs)

    manager = VanishOnStop()
    supervisor = SharedChatSessionSupervisor(manager, idle_timeout_s=30, clock=lambda: now)
    other = replace(_spec(), thread_id="athr_other", sandbox_id="asbx_other")
    supervisor.register_manifest(_spec(), _members())
    supervisor.register_manifest(other, [replace(_members()[0], thread_id=other.thread_id)])
    with pytest.raises(SandboxError, match="docker stop failed"):
        supervisor.stop_if_idle("athr_source", generation=1)
    lost = supervisor._members["athr_source"]
    assert lost.container_lost is True
    assert lost.idle_deadline is None
    token, _ = supervisor.operation_opened(other.thread_id)
    supervisor.operation_closed(other.thread_id, token)
    now = 1000.0

    assert [row.thread_id for row in supervisor.sweep()] == [other.thread_id]
    assert manager.items[other.container_name].running is False


def test_one_group_the_sweep_cannot_stop_does_not_hold_up_the_others(caplog):
    now = 100.0
    unsafe = "https://daemon.invalid/stop?token=sweep-secret"

    class StopFails(_Manager):
        def stop(self, spec, **kwargs):
            if spec.sandbox_id == _spec().sandbox_id:
                raise SandboxError(f"docker stop failed: {unsafe}")
            return super().stop(spec, **kwargs)

    manager = StopFails()
    supervisor = SharedChatSessionSupervisor(manager, idle_timeout_s=30, clock=lambda: now)
    other = replace(_spec(), thread_id="athr_other", sandbox_id="asbx_other")
    supervisor.register_manifest(_spec(), _members())
    supervisor.register_manifest(other, [replace(_members()[0], thread_id=other.thread_id)])
    for thread_id in ("athr_source", other.thread_id):
        token, _ = supervisor.operation_opened(thread_id)
        supervisor.operation_closed(thread_id, token)
    now = 1000.0

    with caplog.at_level(logging.WARNING):
        stopped = supervisor.sweep()

    assert [row.thread_id for row in stopped] == [other.thread_id]
    assert manager.items[_spec().container_name].running is True
    assert manager.items[other.container_name].running is False
    assert any(
        getattr(record, "event", None) == "shared_chat_idle_stop_failed"
        and getattr(record, "error_type", None) == "SandboxError"
        for record in caplog.records
    )
    assert unsafe not in caplog.text


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

    skipped_markers: list[bytes] = []

    def invalidate(_spec, inspection) -> None:
        assert inspection.running is True
        events.append("invalidate")
        if fail_invalidate:
            raise RuntimeError("skipped marker write failed")
        skipped_markers.append(b'{"state":"skipped"}')

    def read_stopped_marker(_spec, *, expected_container_id):
        del expected_container_id
        # Like the controller, a recorded skip stays readable after the stop.
        return skipped_markers[-1] if skipped_markers else None

    manager.read_stopped_final_archive_marker_optional = read_stopped_marker  # type: ignore[method-assign]

    def archive(_spec, inspection, _members) -> None:
        assert inspection.running is False
        if skipped_markers:
            # The controller reads the skipped marker and uploads nothing.
            events.append("archive:skipped")
            return
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


def test_shutdown_flush_failure_skips_the_final_archive_and_finishes_the_stop() -> None:
    events: list[str] = []
    manager, supervisor = _archive_shutdown_group(
        events=events,
        fail_transport_close=True,
    )

    supervisor.quiesce()
    supervisor.stop_all_for_shutdown()

    # The invalidate hook records a skipped final before Docker stop and never
    # prepares an archive. The final hook still runs to finish the lifecycle.
    assert events == [
        "transport.close:0",
        "transport.close:1",
        "invalidate",
        "stop:retain",
        "archive:skipped",
        "stop:release",
    ]
    current = manager.items[_spec().container_name]
    assert current.running is False
    assert current.networks == ()
    group = supervisor._members["athr_source"]
    assert group.stop_archive_pending is False
    assert group.stop_archive_blocked is False


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

    # Once Docker stop works, the recorded skip lets the stop finish.
    manager.stop = original_stop  # type: ignore[method-assign]
    supervisor.stop_all_for_shutdown()
    assert manager.items[_spec().container_name].running is False
    assert manager.items[_spec().container_name].networks == ()
    assert supervisor._members["athr_source"].stop_archive_blocked is False


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
    stopped = supervisor.guard_network()

    assert [item.thread_id for item in stopped] == ["athr_source"]
    current = manager.items[_spec().container_name]
    assert current.running is False
    assert current.networks == ()
    assert supervisor._members["athr_source"].stop_archive_blocked is False


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


def test_network_guard_prepare_failure_exact_stops_and_skips_the_upload() -> None:
    manager = _Manager()
    spec = _spec()
    manager.items[spec.container_name] = manager._inspection(spec, running=True)
    events: list[str] = []

    def prepare(_spec, inspection) -> None:
        assert inspection.running is True
        events.append("prepare")
        raise SandboxError("marker preparation failed")

    skipped: list[bytes] = []

    def invalidate(_spec, inspection) -> None:
        events.append("invalidate:running" if inspection.running else "invalidate:stopped")
        skipped.append(b'{"state":"skipped"}')

    def read_stopped_marker(_spec, *, expected_container_id):
        del expected_container_id
        return skipped[-1] if skipped else None

    manager.read_stopped_final_archive_marker_optional = read_stopped_marker  # type: ignore[method-assign]

    def archive(*_args) -> None:
        # The controller reads the skipped marker and uploads nothing.
        events.append("archive:skipped")

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

    stopped = supervisor.guard_network()

    assert [item.thread_id for item in stopped] == [spec.thread_id]
    current = manager.items[spec.container_name]
    assert current.running is False
    assert current.networks == ()
    # Phase two finds the completed skip and only releases what remains.
    assert events == ["prepare", "invalidate:running", "archive:skipped", "archive:skipped"]
    assert supervisor._members[spec.thread_id].stop_archive_blocked is False


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


def test_durable_flush_skip_survives_stop_failure_and_replacement_controller() -> None:
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
            raise AssertionError("skipped native history must never upload")

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
    assert b'"state":"skipped"' in manager.final_marker
    assert manager.items[_spec().container_name].running is True
    # Until an exact stop completes, this broker still fences the group.
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

    # The durable skip lets the replacement finish the stop without upload and
    # release the bridge, so the chat can start and resume again.
    assert descriptors[0].running is False
    assert manager.items[_spec().container_name].networks == ()
    assert all(service.final_calls == 0 for service in services)
    group = replacement._members[_spec().thread_id]
    assert group.stop_archive_pending is False
    assert group.stop_archive_blocked is False
    lease = replacement.acquire(
        _spec(),
        thread_id="athr_source",
        owner="attempt:2",
        activation_generation=2,
        has_settled_context=True,
        manifest=_members(),
        expected_container_id="container-shared",
    )
    assert lease.token
    assert manager.items[_spec().container_name].running is True
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


class _BlockingStartManager(_Manager):
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


def test_initial_archive_waits_out_a_concurrent_start_and_workspace_request() -> None:
    """The chat page reads the workspace while a new chat prepares (staging 409)."""
    manager = _BlockingStartManager()
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
    opening = threading.Thread(
        target=lambda: opened.append(supervisor.operation_opened("athr_peer"))
    )
    opening.start()
    assert manager.start_entered.wait(timeout=1)
    captured = threading.Event()

    def capture(*_args):
        captured.set()
        return {"enabled": True, "stored": True}

    capturing, outcome = _in_thread(
        lambda: supervisor.capture_initial_archive("athr_source", lease.token, capture=capture)
    )
    try:
        # Neither the start nor the workspace request it admits fails the archive.
        capturing.join(timeout=0.3)
        assert capturing.is_alive()
        manager.start_release.set()
        opening.join(timeout=2)
        assert not opening.is_alive() and opened
        capturing.join(timeout=0.3)
        assert capturing.is_alive(), "the archive captured while a request was still running"
        assert not captured.is_set()
        token, _ = opened[0]
        supervisor.operation_closed("athr_peer", token)
        capturing.join(timeout=2)
    finally:
        manager.start_release.set()

    assert not capturing.is_alive()
    assert "error" not in outcome
    assert captured.is_set()
    assert supervisor._members["athr_source"].snapshotting is False


class _HeldStartManager(_Manager):
    """Hold every start once armed, including a running group's bridge check."""

    def __init__(self) -> None:
        super().__init__()
        self.hold_start = False
        self.start_entered = threading.Event()
        self.start_release = threading.Event()

    def start(self, spec, **kwargs):
        if self.hold_start:
            self.start_entered.set()
            assert self.start_release.wait(timeout=2)
        return super().start(spec, **kwargs)


def test_initial_archive_waits_out_a_start_that_begins_under_its_barrier(monkeypatch) -> None:
    """A request admitted before the barrier may begin its bridge check under it."""
    manager = _HeldStartManager()
    supervisor = SharedChatSessionSupervisor(manager)
    lease = supervisor.acquire(
        _spec(),
        thread_id="athr_source",
        owner="attempt:1",
        activation_generation=1,
        has_settled_context=False,
        manifest=_members(),
    )
    running, _ = supervisor.operation_opened("athr_peer")
    group = supervisor._members["athr_source"]
    # A second read passes its first admission, then is held before its check.
    entered, release = _gated_inspect(manager, monkeypatch, _spec().container_name)
    manager.hold_start = True
    opening, _ = _in_thread(lambda: supervisor.operation_opened("athr_peer"))
    assert entered.wait(timeout=1)
    captured = threading.Event()

    def capture(*_args):
        captured.set()
        return {"enabled": True, "stored": True}

    capturing, outcome = _in_thread(
        lambda: supervisor.capture_initial_archive("athr_source", lease.token, capture=capture)
    )
    try:
        deadline = time.monotonic() + 1
        while not group.snapshotting and time.monotonic() < deadline:
            time.sleep(0.01)
        assert group.snapshotting
        release.set()
        assert manager.start_entered.wait(timeout=1)
        assert group.starting
        supervisor.operation_closed("athr_peer", running)
        capturing.join(timeout=0.6)
        assert capturing.is_alive(), f"the archive did not wait for the start: {outcome}"
        assert not captured.is_set()
    finally:
        release.set()
        manager.start_release.set()
        opening.join(timeout=2)
        capturing.join(timeout=2)

    assert not capturing.is_alive()
    assert "error" not in outcome
    assert captured.is_set()
    assert group.snapshotting is False


def test_initial_archive_reports_busy_once_a_request_outlasts_its_wait() -> None:
    manager = _Manager()
    supervisor = SharedChatSessionSupervisor(manager)
    supervisor.initial_archive_wait_s = 0.3
    lease = supervisor.acquire(
        _spec(),
        thread_id="athr_source",
        owner="attempt:1",
        activation_generation=1,
        has_settled_context=False,
        manifest=_members(),
    )
    token, _ = supervisor.operation_opened("athr_peer")
    captured: list[bool] = []

    with pytest.raises(SessionBusy, match="active mutations"):
        supervisor.capture_initial_archive(
            "athr_source", lease.token, capture=lambda *_args: captured.append(True) or {}
        )

    group = supervisor._members["athr_source"]
    assert captured == []
    assert group.snapshotting is False
    # The barrier is gone: the request finishes, and the next attempt captures.
    supervisor.operation_closed("athr_peer", token)
    supervisor.capture_initial_archive(
        "athr_source", lease.token, capture=lambda *_args: captured.append(True) or {}
    )
    assert captured == [True]


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
    assert archive_failure_http_detail(raised.value) == (
        "archive failure (phase=initial, type=RuntimeError)"
    )
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


def test_failed_final_upload_keeps_the_stopped_bridge_and_never_thaws(
    caplog: pytest.LogCaptureFixture,
) -> None:
    manager = _Manager()
    prepared: list[bool] = []

    class ProviderError(RuntimeError):
        def __init__(self, *, code: str, status: int, unsafe: str) -> None:
            super().__init__(unsafe)
            self.response = {
                "Error": {"Code": code, "Message": unsafe},
                "ResponseMetadata": {
                    "HTTPStatusCode": status,
                    "HTTPHeaders": {"authorization": unsafe},
                },
                "Body": unsafe,
            }

    initial_unsafe = "https://initial.invalid/private-key?credential=initial-secret"
    final_unsafe = "https://final.invalid/object-key?credential=final-secret"
    final_failure = RuntimeError("final upload failed")
    final_failure.__cause__ = ProviderError(
        code="SlowDown",
        status=503,
        unsafe=final_unsafe,
    )

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
    initial_failure.__cause__ = ProviderError(
        code="AccessDenied",
        status=403,
        unsafe=initial_unsafe,
    )

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
    safe_detail = archive_failure_http_detail(raised.value)
    assert safe_detail == (
        "archive failure (phase=initial, type=ProviderError, code=AccessDenied, status=403); "
        "archive failure (phase=fail_stop_final, type=ProviderError, code=SlowDown, status=503)"
    )
    assert initial_unsafe not in safe_detail
    assert final_unsafe not in safe_detail
    assert initial_unsafe not in caplog.text
    assert final_unsafe not in caplog.text
    assert initial_unsafe not in " ".join(getattr(raised.value, "__notes__", ()))
    assert final_unsafe not in " ".join(getattr(raised.value, "__notes__", ()))
    assert any(
        getattr(record, "detail", None)
        == "archive failure (phase=fail_stop_final, type=ProviderError, "
        "code=SlowDown, status=503)"
        for record in caplog.records
    )
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
    supervisor.register_manifest(
        _spec(),
        [
            replace(_members()[0], resident_state="restarting"),
            replace(_members()[1], context_status="lost", resident_state="lost"),
        ],
    )
    assert supervisor.status("athr_source").resident_state == "restarting"
    assert supervisor.status("athr_peer").resident_state == "lost"

    descriptor = supervisor.stop_if_idle("athr_source", generation=1)

    assert descriptor.running is False
    assert descriptor.resident_state == "stopped"
    assert supervisor.status("athr_peer").resident_state == "lost"
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


def test_a_residency_mcp_credential_lets_a_probe_borrow_the_live_process():
    """The process keeps its residency's credential; a probe carries none."""
    launch = replace(
        _launch("athr_source", native_session_id="native-source", slot=0),
        mcp_server_ids=(":platform:memory", "github"),
        mcp_base_url="http://relay",
        mcp_credential="amr.residency-1",
    )
    transport = ResidentTransport(launch=launch, session=SimpleNamespace())

    assert transport.compatible_with(replace(launch, mcp_credential=""))
    assert transport.compatible_with(launch)
    # What the CLI fixed at start still has to match.
    assert not transport.compatible_with(replace(launch, mcp_credential="amr.residency-2"))
    assert not transport.compatible_with(
        replace(launch, mcp_credential="", mcp_server_ids=(":platform:memory",))
    )
    assert not transport.compatible_with(
        replace(launch, mcp_credential="", mcp_base_url="http://other-relay")
    )


def test_a_per_attempt_mcp_token_still_needs_a_new_process():
    """A process holding one turn's token cannot serve the next turn's tools."""
    launch = replace(
        _launch("athr_source", native_session_id="native-source", slot=0),
        mcp_server_ids=(":platform:memory",),
        mcp_base_url="http://relay",
        mcp_credential="ajt.turn-1",
    )
    transport = ResidentTransport(launch=launch, session=SimpleNamespace())

    assert not transport.compatible_with(replace(launch, mcp_credential=""))
    assert not transport.compatible_with(replace(launch, mcp_credential="ajt.turn-2"))


def test_github_token_rotation_or_removal_requires_a_new_resident_process():
    launch = replace(
        _launch("athr_source", native_session_id="native-source", slot=0),
        github_token="first-token",
        github_repo="owner/repo",
    )
    transport = ResidentTransport(launch=launch, session=SimpleNamespace())

    assert transport.compatible_with(replace(launch, credential="renewed-grant"))
    assert not transport.compatible_with(replace(launch, github_token="renewed-token"))
    assert not transport.compatible_with(replace(launch, github_token=None))
    assert not transport.compatible_with(replace(launch, github_repo="owner/other"))


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


@pytest.mark.parametrize("turn", ["never_started", "aborted"])
def test_a_reaped_activation_ends_with_its_residency_so_its_group_can_idle(turn):
    """A runner that dies mid-activation never settles or abandons its member.

    The control plane reaps the attempt and closes the residency its process
    ran on. The lease the dead activation holds ends with that process, as an
    abandon would end it; otherwise a sibling's group never goes idle and its
    publication snapshot is refused as an active mutation. A turn the dead
    runner had started was already aborted when its stream dropped.
    """
    now = [1000.0]
    manager = _Manager()
    supervisor = SharedChatSessionSupervisor(
        manager, transport_factory=_TransportFactory(), idle_timeout_s=60, clock=lambda: now[0]
    )
    dead = supervisor.acquire(
        _spec(),
        thread_id="athr_source",
        owner="attempt:1",
        activation_generation=1,
        has_settled_context=True,
        manifest=_members(),
    )
    supervisor.ensure_resident(
        "athr_source", dead.token, _launch("athr_source", native_session_id="native-source", slot=0)
    )
    if turn == "aborted":
        started = supervisor.start_turn(
            "athr_source", dead.token, prompt="work", model="glm-5.1", reasoning_effort=None
        )
        supervisor.abort_turn("athr_source", dead.token, started)
    peer = supervisor.acquire(
        _spec(),
        thread_id="athr_peer",
        owner="attempt:7",
        activation_generation=1,
        has_settled_context=True,
        manifest=_members(),
    )
    supervisor.ensure_resident(
        "athr_peer", peer.token, _launch("athr_peer", native_session_id="native-peer", slot=1)
    )
    supervisor.settle("athr_peer", peer.token, job_id="ajob_peer", context_status="live")

    ended = supervisor.residency_lost("athr_source", generation=1)

    assert ended.busy is False
    assert ended.group_busy is False
    assert ended.context_status == ended.resident_state == "lost"
    assert ended.residency_generation == 0
    assert ended.idle_deadline_monotonic == 1060.0
    with pytest.raises(SessionLeaseLost):
        supervisor.settle("athr_source", dead.token, job_id="ajob_dead", context_status="live")
    assert supervisor.capture_snapshot("athr_peer", capture=lambda _backend: {"patch": ""}) == {
        "patch": ""
    }
    now[0] += 60
    assert [stopped.running for stopped in supervisor.sweep()] == [False]
    assert manager.items[_spec().container_name].running is False


def test_a_loss_its_activation_rotated_past_meanwhile_leaves_the_new_process_running():
    """A loss names the generation its beat sampled, which can be rotated past meanwhile.

    The loss closes that generation's process and then ends the activation
    that took it on. Before it does, the same activation can rotate to a newer
    residency and start the replacement, as a Fork rotates its source's. Ending
    the activation then tore the replacement down and marked its context lost.
    """
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
    first = _launch("athr_source", native_session_id="native-source", slot=0)
    supervisor.ensure_resident("athr_source", lease.token, first)
    old = factory.transports[0]
    rotated = []

    def rotate_as_the_old_process_closes() -> None:
        # The loss closes the old process outside the supervisor's lock, and
        # only then ends the activation; the rotation lands in between.
        type(old).close(old)
        rotated.append(
            supervisor.ensure_resident(
                "athr_source",
                lease.token,
                replace(first, credential="grant-2", residency_generation=2),
            )
        )

    old.close = rotate_as_the_old_process_closes

    lost = supervisor.residency_lost("athr_source", generation=1)

    assert [replacement.residency_generation for replacement in rotated] == [2]
    assert old.alive is False
    assert factory.transports[1].alive is True
    assert lost.residency_generation == 2
    assert lost.context_status != "lost"
    settled = supervisor.settle(
        "athr_source", lease.token, job_id="ajob_next", context_status="restored"
    )
    assert settled.busy is False
    assert settled.context_status == "restored"


def test_a_residency_its_activation_is_replacing_leaves_that_activation_running():
    """A host beat can name the generation a live activation is rotating away from.

    The activation found the member's process unfit for its turn (here, a new
    model) before taking it on, and asked the control plane for a new
    residency, so the old generation is no longer current: a beat sampled just
    before reports it lost. That closes the old process only. The activation
    keeps its lease and starts the replacement under its new generation.
    """
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
    old_launch = _launch("athr_source", native_session_id="native-source", slot=0)
    supervisor.ensure_resident("athr_source", first.token, old_launch)
    supervisor.settle("athr_source", first.token, job_id="ajob_source", context_status="live")
    lease = supervisor.acquire(
        _spec(),
        thread_id="athr_source",
        owner="attempt:2",
        activation_generation=2,
        has_settled_context=True,
        manifest=_members(),
    )
    probe = replace(old_launch, model="glm-5.2", credential=None)
    with pytest.raises(SandboxError, match="requires a renewable credential"):
        supervisor.ensure_resident("athr_source", lease.token, probe)

    lost = supervisor.residency_lost("athr_source", generation=1)

    assert lost.busy is True
    assert lost.context_status == "live"
    assert factory.transports[0].alive is False
    replacement = supervisor.ensure_resident(
        "athr_source", lease.token, replace(probe, credential="grant-2", residency_generation=2)
    )
    assert replacement.residency_generation == 2
    assert replacement.resident_action == "resume"
    settled = supervisor.settle(
        "athr_source", lease.token, job_id="ajob_next", context_status="restored"
    )
    assert settled.busy is False
    assert settled.context_status == "restored"


def _hung_turn_with_a_live_sibling(now: list[float]):
    """A member's turn is running for a runner that hangs; its Fork sibling has settled."""
    manager = _Manager()
    supervisor = SharedChatSessionSupervisor(
        manager, transport_factory=_TransportFactory(), idle_timeout_s=60, clock=lambda: now[0]
    )
    hung = supervisor.acquire(
        _spec(),
        thread_id="athr_source",
        owner="attempt:1",
        activation_generation=1,
        has_settled_context=True,
        manifest=_members(),
    )
    supervisor.ensure_resident(
        "athr_source", hung.token, _launch("athr_source", native_session_id="native-source", slot=0)
    )
    turn = supervisor.start_turn(
        "athr_source", hung.token, prompt="work", model="glm-5.1", reasoning_effort=None
    )
    peer = supervisor.acquire(
        _spec(),
        thread_id="athr_peer",
        owner="attempt:7",
        activation_generation=1,
        has_settled_context=True,
        manifest=_members(),
    )
    supervisor.ensure_resident(
        "athr_peer", peer.token, _launch("athr_peer", native_session_id="native-peer", slot=1)
    )
    supervisor.settle("athr_peer", peer.token, job_id="ajob_peer", context_status="live")
    return manager, supervisor, hung, turn


def test_a_loss_during_its_turn_ends_the_activation_once_its_stream_closes_the_turn():
    """A hung runner still holds its turn stream when the reaper closes its residency.

    The loss closes the process and leaves the running turn to that stream,
    which still answers for what the turn did. Nothing else will release the
    lease afterwards: the runner never settles or abandons, and the member's
    residency generation is gone, so no host beat names it again. The
    activation therefore ends with its turn, as an abandon would end it.
    """
    now = [1000.0]
    _manager, supervisor, hung, turn = _hung_turn_with_a_live_sibling(now)

    lost = supervisor.residency_lost("athr_source", generation=1)

    assert lost.busy is True
    assert turn.interrupted is True
    # The stream ends the turn once the closed process has ended it.
    supervisor.complete_turn("athr_source", hung.token, turn)

    ended = supervisor.status("athr_source")
    assert ended.busy is False
    assert ended.group_busy is False
    assert ended.context_status == ended.resident_state == "lost"
    assert ended.idle_deadline_monotonic == 1060.0
    with pytest.raises(SessionLeaseLost):
        supervisor.settle("athr_source", hung.token, job_id="ajob_hung", context_status="live")
    assert supervisor.capture_snapshot("athr_peer", capture=lambda _backend: {"patch": ""}) == {
        "patch": ""
    }


def test_a_lost_turn_whose_stream_never_closes_it_is_closed_by_the_sweep():
    """A stream to a runner that stopped reading never reaches its turn's end.

    Its frames wait on a socket the runner no longer drains, so the turn the
    loss interrupted stays open with its member lock and lease. Once the
    stream has had ample time, the sweep closes that turn's boundary on its
    behalf, which ends the activation; a stream that wakes later finds it gone.
    """
    now = [1000.0]
    manager, supervisor, hung, turn = _hung_turn_with_a_live_sibling(now)
    member = supervisor._members["athr_source"].members["athr_source"]
    supervisor.residency_lost("athr_source", generation=1)

    supervisor.sweep()
    assert supervisor.status("athr_source").busy is True
    now[0] += 60

    assert supervisor.sweep() == []

    ended = supervisor.status("athr_source")
    assert ended.busy is False
    assert ended.group_busy is False
    assert ended.context_status == ended.resident_state == "lost"
    assert member.turn_lock.locked() is False
    assert manager.events[-2:] == [("reap-except", 0, ((41, 400),)), ("normalize", 0)]
    with pytest.raises(SessionLeaseLost):
        supervisor.complete_turn("athr_source", hung.token, turn)
    now[0] += 60
    assert [stopped.running for stopped in supervisor.sweep()] == [False]


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


def test_abandon_releases_the_member_lock_of_the_turn_it_detaches(monkeypatch):
    """With normal cleanup failed, abandon owns the attached turn's release."""
    supervisor = SharedChatSessionSupervisor(_Manager(), transport_factory=_TransportFactory())
    lease = supervisor.acquire(
        _spec(),
        thread_id="athr_source",
        owner="attempt:1",
        activation_generation=1,
        has_settled_context=True,
        manifest=_members(),
    )
    launch = _launch("athr_source", native_session_id="native-source", slot=0)
    supervisor.ensure_resident("athr_source", lease.token, launch)
    supervisor.start_turn(
        "athr_source", lease.token, prompt="work", model="glm-5.1", reasoning_effort=None
    )
    member = supervisor._members["athr_source"].members["athr_source"]
    assert member.turn_lock.locked() is True

    def fail_cleanup(*_args, **_kwargs):
        raise RuntimeError("cleanup failed")

    monkeypatch.setattr(supervisor, "complete_turn", fail_cleanup)
    supervisor.abandon("athr_source", lease.token, lost=False)

    assert member.turn_lock.locked() is False
    monkeypatch.undo()
    successor = supervisor.acquire(
        _spec(),
        thread_id="athr_source",
        owner="attempt:2",
        activation_generation=2,
        has_settled_context=True,
        manifest=_members(),
    )
    supervisor.ensure_resident("athr_source", successor.token, launch)
    supervisor.start_turn(
        "athr_source", successor.token, prompt="again", model="glm-5.1", reasoning_effort=None
    )


def test_a_turn_detached_by_complete_turn_releases_its_lock_even_if_it_then_fails():
    """Once ``complete_turn`` detaches the turn, the member lock is its to release."""

    class FlakyTransport(_Transport):
        failing = False

        @property
        def alive(self) -> bool:
            if self.failing:
                raise RuntimeError("process table unavailable")
            return True

        @alive.setter
        def alive(self, _value: bool) -> None:
            return

    class Factory(_TransportFactory):
        def open(self, spec, launch):
            transport = FlakyTransport(launch)
            self.transports.append(transport)
            return transport

    factory = Factory()
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
    turn = supervisor.start_turn(
        "athr_source", lease.token, prompt="work", model="glm-5.1", reasoning_effort=None
    )
    member = supervisor._members["athr_source"].members["athr_source"]
    factory.transports[0].failing = True

    with pytest.raises(RuntimeError, match="process table"):
        supervisor.complete_turn("athr_source", lease.token, turn)

    assert member.current_turn is None
    assert member.turn_lock.locked() is False


def test_abandon_leaves_a_resident_start_its_own_member_lock():
    """A slow resident start keeps its exclusion, and its process, from abandon.

    ``threading.Lock`` has no owner. Abandon released the member lock whenever
    it was held, so a runner that gave up on a slow resident start freed the
    lock that start still held: the start then installed a live process over
    the abandoned member, and its own release raised ``RuntimeError``.
    """
    entered = threading.Event()
    proceed = threading.Event()

    class SlowFactory(_TransportFactory):
        def open(self, spec, launch):
            entered.set()
            assert proceed.wait(5)
            return super().open(spec, launch)

    factory = SlowFactory()
    supervisor = SharedChatSessionSupervisor(_Manager(), transport_factory=factory)
    lease = supervisor.acquire(
        _spec(),
        thread_id="athr_source",
        owner="attempt:1",
        activation_generation=1,
        has_settled_context=True,
        manifest=_members(),
    )
    member = supervisor._members["athr_source"].members["athr_source"]
    errors: list[BaseException] = []

    def start() -> None:
        try:
            supervisor.ensure_resident(
                "athr_source",
                lease.token,
                _launch("athr_source", native_session_id="native-source", slot=0),
            )
        except BaseException as exc:  # pragma: no cover - asserted below
            errors.append(exc)

    starter = threading.Thread(target=start)
    starter.start()
    assert entered.wait(5)

    abandoned = supervisor.abandon("athr_source", lease.token, lost=True)

    assert member.turn_lock.locked() is True
    assert abandoned.resident_state == "lost"
    proceed.set()
    starter.join(5)
    assert not starter.is_alive()
    assert [type(error) for error in errors] == [SessionLeaseLost]
    assert member.turn_lock.locked() is False
    assert member.transport is None
    assert (member.context_status, member.resident_state) == ("lost", "lost")
    assert [transport.alive for transport in factory.transports] == [False]


def test_abandon_leaves_a_turn_in_admission_its_own_member_lock():
    """A turn still being admitted releases its own lock and is refused."""
    admitting = threading.Event()
    proceed = threading.Event()

    class SlowCensus(_Manager):
        def capture_member_processes(self, spec, **kwargs):
            admitting.set()
            assert proceed.wait(5)
            return super().capture_member_processes(spec, **kwargs)

    factory = _TransportFactory()
    supervisor = SharedChatSessionSupervisor(SlowCensus(), transport_factory=factory)
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
    member = supervisor._members["athr_source"].members["athr_source"]
    errors: list[BaseException] = []

    def admit() -> None:
        try:
            supervisor.start_turn(
                "athr_source", lease.token, prompt="work", model="glm-5.1", reasoning_effort=None
            )
        except BaseException as exc:  # pragma: no cover - asserted below
            errors.append(exc)

    admission = threading.Thread(target=admit)
    admission.start()
    assert admitting.wait(5)

    supervisor.abandon("athr_source", lease.token, lost=False)

    assert member.turn_lock.locked() is True
    proceed.set()
    admission.join(5)
    assert not admission.is_alive()
    assert [type(error) for error in errors] == [SessionLeaseLost]
    assert member.turn_lock.locked() is False
    assert member.current_turn is None


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

    The sandbox manager builds its supervisor without a checkpoint hook. An earlier
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
                "expires_at": (datetime.now(timezone.utc) + timedelta(minutes=15)).isoformat(),
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


@pytest.mark.parametrize(
    "operation", ["idle_stop", "begin_archive", "commit_archive", "reconcile_marker"]
)
def test_lifecycle_io_leaves_other_groups_available(monkeypatch, operation):
    """A slow lifecycle check must fence its group without stalling siblings."""
    manager = _Manager()
    supervisor = SharedChatSessionSupervisor(manager)
    spec = _spec()
    supervisor.register_manifest(spec, _members())
    other = replace(spec, thread_id="athr_other", sandbox_id="asbx_other")
    supervisor.register_manifest(other, [replace(_members()[0], thread_id=other.thread_id)])
    entered = threading.Event()
    release = threading.Event()

    def gate():
        entered.set()
        assert release.wait(timeout=_HELD_S)

    if operation == "idle_stop":
        inspect = manager.inspect

        def inspect_slow(name):
            observed = inspect(name)
            if name == spec.container_name and not entered.is_set():
                gate()
            return observed

        monkeypatch.setattr(manager, "inspect", inspect_slow)

        def call():
            return supervisor.stop_if_idle(spec.thread_id, generation=spec.generation)
    elif operation in {"begin_archive", "commit_archive"}:
        manager.items[spec.container_name] = replace(
            manager.items[spec.container_name], running=False, status=ChatSandboxStatus.STOPPED
        )

        def require_stopped(owned_spec, *, container_id):
            current = manager.items[owned_spec.container_name]
            assert current.container_id == container_id
            assert not current.running
            if operation == "begin_archive":
                gate()
            return current

        monkeypatch.setattr(manager, "require_stopped_archive", require_stopped, raising=False)
        kwargs = {
            "request_id": "delete-1",
            "generation": spec.generation,
            "container_id": "container-shared",
        }
        if operation == "begin_archive":

            def call():
                return supervisor.begin_archive(spec.thread_id, **kwargs)
        else:
            supervisor.begin_archive(spec.thread_id, **kwargs)

            def remove(owned_spec, *, container_id):
                assert manager.items[owned_spec.container_name].container_id == container_id
                gate()
                manager.items.pop(owned_spec.container_name)

            monkeypatch.setattr(manager, "remove_stopped_archived", remove, raising=False)

            def call():
                return supervisor.commit_archive(
                    spec, request_id="delete-1", container_id="container-shared"
                )
    else:
        orphan = replace(spec, thread_id="athr_orphan", sandbox_id="asbx_orphan")
        manager.items[orphan.container_name] = manager._inspection(orphan)

        def read_marker(owned_spec, **_kwargs):
            if owned_spec == orphan:
                gate()
            return None

        monkeypatch.setattr(manager, "read_running_final_archive_marker", read_marker)
        call = supervisor.reconcile

    worker, outcome = _in_thread(call)
    other_worker = None
    try:
        assert entered.wait(timeout=2)
        other_worker, other_outcome = _in_thread(lambda: supervisor.status(other.thread_id))
        other_worker.join(timeout=_UNBLOCKED_S)
        assert not other_worker.is_alive(), "lifecycle I/O held the supervisor-wide lock"
        assert "error" not in other_outcome
        if operation != "reconcile_marker":
            with pytest.raises(SessionBusy):
                supervisor.operation_opened(spec.thread_id)
    finally:
        release.set()
        worker.join(timeout=5)
        if other_worker is not None:
            other_worker.join(timeout=5)
    assert not worker.is_alive()
    assert "error" not in outcome


@pytest.mark.parametrize("operation", ["idle_stop", "begin_archive", "reconcile_marker"])
@pytest.mark.parametrize("fence", ["index", "lifecycle", "spec", "container"])
def test_lifecycle_check_rejects_superseded_observations(monkeypatch, caplog, operation, fence):
    """An observation cannot commit to a group whose authority changed.

    Reconciliation rejects it for that group alone: it logs the refusal and
    goes on with the host's other groups rather than failing their listing.
    """
    manager = _Manager()
    supervisor = SharedChatSessionSupervisor(manager)
    spec = _spec()
    supervisor.register_manifest(spec, _members())
    group = supervisor._members[spec.thread_id]
    observed = replace(group.inspection, running=False, status=ChatSandboxStatus.STOPPED)

    def changed(*_args, **_kwargs):
        with supervisor._lock:
            if fence == "index":
                supervisor._groups.pop(spec.sandbox_id)
            elif fence == "lifecycle":
                group.lifecycle_generation += 1
            elif fence == "spec":
                group.spec = replace(spec, generation=spec.generation + 1)
            else:
                group.inspection = replace(group.inspection, container_id="replacement")
        return observed

    if operation == "idle_stop":
        monkeypatch.setattr(manager, "inspect", changed)

        def call():
            return supervisor.stop_if_idle(spec.thread_id, generation=spec.generation)
    elif operation == "reconcile_marker":
        group.orphan_marker_unreadable = True
        monkeypatch.setattr(manager, "read_running_final_archive_marker", changed)
        call = supervisor.reconcile
    else:
        monkeypatch.setattr(manager, "require_stopped_archive", changed, raising=False)

        def call():
            return supervisor.begin_archive(
                spec.thread_id,
                request_id="delete-1",
                generation=spec.generation,
                container_id=observed.container_id,
            )

    if operation == "reconcile_marker":
        with caplog.at_level(logging.WARNING):
            call()
        refusals = [
            str(getattr(record, "detail", ""))
            for record in caplog.records
            if getattr(record, "event", None) == "shared_sandbox_reconcile_group_failed"
        ]
        assert len(refusals) == 1
        assert "lifecycle changed" in refusals[0]
    else:
        with pytest.raises(SessionLeaseLost, match="lifecycle changed"):
            call()
    assert manager.stop_count == 0
    assert group.inspection.running is True
    assert group.archive_request_id is None


@pytest.mark.parametrize("operation", ["idle_stop", "begin_archive"])
def test_failed_lifecycle_proof_releases_only_its_temporary_reservation(monkeypatch, operation):
    manager = _Manager()
    supervisor = SharedChatSessionSupervisor(manager)
    spec = _spec()
    supervisor.register_manifest(spec, _members())
    group = supervisor._members[spec.thread_id]

    def failed(*_args, **_kwargs):
        raise SandboxError("Docker did not answer")

    with monkeypatch.context() as patch:
        if operation == "idle_stop":
            patch.setattr(manager, "inspect", failed)
            with pytest.raises(SandboxError, match="did not answer"):
                supervisor.stop_if_idle(spec.thread_id, generation=spec.generation)
        else:
            patch.setattr(manager, "require_stopped_archive", failed, raising=False)
            with pytest.raises(SandboxError, match="did not answer"):
                supervisor.begin_archive(
                    spec.thread_id,
                    request_id="delete-1",
                    generation=spec.generation,
                    container_id=group.inspection.container_id,
                )
    assert group.lifecycle_check is None
    assert group.archive_request_id is None
    assert not group.stopping
    token, descriptor = supervisor.operation_opened(spec.thread_id)
    assert descriptor.running
    supervisor.operation_closed(spec.thread_id, token)


def test_absence_proof_rechecks_registration_after_slow_docker_reads(monkeypatch):
    manager = _Manager()
    supervisor = SharedChatSessionSupervisor(manager)
    spec = _spec()
    entered, release = _gated_inspect(manager, monkeypatch, spec.container_name)
    worker, outcome = _in_thread(
        lambda: supervisor.admin_deletion_target_absent(
            spec.thread_id, sandbox_id=spec.sandbox_id, container_id="container-shared"
        )
    )
    try:
        assert entered.wait(timeout=2)
        supervisor.register_manifest(spec, _members())
    finally:
        release.set()
        worker.join(timeout=5)
    assert not worker.is_alive()
    assert outcome == {"value": False}


@pytest.mark.parametrize("replacement", ["stable_name", "renamed_original"])
def test_deletion_proves_both_container_identities_absent_before_forgetting_group(
    monkeypatch, replacement
):
    manager = _Manager()
    supervisor = SharedChatSessionSupervisor(manager)
    spec = _spec()
    supervisor.register_manifest(spec, _members())
    group = supervisor._members[spec.thread_id]
    manager.items[spec.container_name] = replace(
        group.inspection, running=False, status=ChatSandboxStatus.STOPPED
    )

    def require_stopped(owned_spec, *, container_id):
        current = manager.items[owned_spec.container_name]
        assert current.container_id == container_id
        return current

    def remove(owned_spec, *, container_id):
        original = manager.items.pop(owned_spec.container_name)
        if replacement == "stable_name":
            manager.items[owned_spec.container_name] = replace(original, container_id="replacement")
        else:
            manager.items[container_id] = replace(original, name="renamed")

    monkeypatch.setattr(manager, "require_stopped_archive", require_stopped, raising=False)
    monkeypatch.setattr(manager, "remove_stopped_archived", remove, raising=False)
    supervisor.begin_archive(
        spec.thread_id,
        request_id="delete-1",
        generation=spec.generation,
        container_id=group.inspection.container_id,
    )
    with pytest.raises(RetainedChatLost, match="identity changed"):
        supervisor.commit_archive(
            spec, request_id="delete-1", container_id=group.inspection.container_id
        )
    assert supervisor._groups[spec.sandbox_id] is group
    assert supervisor._members[spec.thread_id] is group
    assert group.archive_request_id == "delete-1"
    assert group.stopping
    assert group.lifecycle_check is None


def test_refused_busy_deletion_does_not_invalidate_active_lifecycle(monkeypatch):
    manager = _Manager()
    supervisor = SharedChatSessionSupervisor(manager)
    spec = _spec()
    supervisor.register_manifest(spec, _members())
    token, _descriptor = supervisor.operation_opened(spec.thread_id)
    group = supervisor._members[spec.thread_id]
    generation = group.lifecycle_generation

    def inspect(*_args):
        pytest.fail("a refused busy deletion must not perform Docker I/O")

    monkeypatch.setattr(manager, "inspect", inspect)
    with pytest.raises(SessionBusy, match="idle group"):
        supervisor.commit_archive(
            spec, request_id="delete-1", container_id=group.inspection.container_id
        )
    assert group.lifecycle_generation == generation
    assert group.lifecycle_check is None
    assert group.archive_request_id is None
    supervisor.operation_closed(spec.thread_id, token)


class _RecheckingStartManager(_Manager):
    """Hold the bridge recheck a concurrent workspace request performs."""

    def __init__(self) -> None:
        super().__init__()
        self.block_start = False
        self.start_entered = threading.Event()
        self.start_release = threading.Event()

    def start(self, spec, **kwargs):
        if self.block_start:
            self.start_entered.set()
            assert self.start_release.wait(timeout=5)
        expected = kwargs.get("expected_container_id")
        if expected is not None and self.items[spec.container_name].container_id != expected:
            raise RetainedChatLost("chat sandbox container identity changed before start")
        return super().start(spec, **kwargs)


def _rechecking_activation():
    manager = _RecheckingStartManager()
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
    launch = _launch("athr_source", native_session_id="native-source", slot=0)
    supervisor.ensure_resident("athr_source", lease.token, launch)
    manager.block_start = True
    return manager, supervisor, factory, lease, launch


@pytest.mark.parametrize("call", ["resident", "turn", "late_turn"])
def test_prompt_start_waits_for_a_workspace_bridge_recheck(monkeypatch, call):
    """A files/terminal request must not destroy an admitted but unsent turn."""
    manager, supervisor, factory, lease, launch = _rechecking_activation()
    waited = threading.Event()
    wait = supervisor._condition.wait

    def waiting(*args, **kwargs):
        waited.set()
        return wait(*args, **kwargs)

    monkeypatch.setattr(supervisor._condition, "wait", waiting)
    reading, read_outcome = _in_thread(lambda: supervisor.operation_opened("athr_peer"))
    if call == "late_turn":
        # Let the first admission check pass, then race the process census.
        manager.start_release.set()
        reading.join(timeout=2)
        supervisor.operation_closed("athr_peer", read_outcome["value"][0])
        manager.start_entered.clear()
        manager.start_release.clear()
        census = manager.capture_member_processes

        def capture(*args, **kwargs):
            nonlocal reading, read_outcome
            reading, read_outcome = _in_thread(lambda: supervisor.operation_opened("athr_peer"))
            assert manager.start_entered.wait(timeout=2)
            return census(*args, **kwargs)

        monkeypatch.setattr(manager, "capture_member_processes", capture)
    else:
        assert manager.start_entered.wait(timeout=2)

    def activate():
        if call == "resident":
            return supervisor.ensure_resident("athr_source", lease.token, launch)
        return supervisor.start_turn(
            "athr_source",
            lease.token,
            prompt="read PR state",
            model="glm-5.1",
            reasoning_effort=None,
        )

    activating, outcome = _in_thread(activate)
    try:
        assert waited.wait(timeout=1), outcome
        assert factory.transports[0].turns == []
    finally:
        manager.start_release.set()
        reading.join(timeout=2)
        activating.join(timeout=2)
    assert not reading.is_alive() and not activating.is_alive()
    assert "error" not in read_outcome
    assert "error" not in outcome
    assert len(factory.transports[0].turns) == (0 if call == "resident" else 1)
    supervisor.operation_closed("athr_peer", read_outcome["value"][0])


@pytest.mark.parametrize("invalidate", ["lease", "container"])
def test_prompt_wait_rechecks_the_lease_and_container_before_dispatch(monkeypatch, invalidate):
    manager, supervisor, factory, lease, _launch_spec = _rechecking_activation()
    waited = threading.Event()
    wait = supervisor._condition.wait

    def waiting(*args, **kwargs):
        waited.set()
        return wait(*args, **kwargs)

    monkeypatch.setattr(supervisor._condition, "wait", waiting)
    reading, _read_outcome = _in_thread(lambda: supervisor.operation_opened("athr_peer"))
    assert manager.start_entered.wait(timeout=2)
    activating, outcome = _in_thread(
        lambda: supervisor.start_turn(
            "athr_source",
            lease.token,
            prompt="must not send",
            model="glm-5.1",
            reasoning_effort=None,
        )
    )
    try:
        assert waited.wait(timeout=1), outcome
        if invalidate == "lease":
            supervisor.abandon("athr_source", lease.token, lost=False)
        else:
            current = manager.items[_spec().container_name]
            manager.items[_spec().container_name] = replace(current, container_id="replacement")
    finally:
        manager.start_release.set()
        reading.join(timeout=2)
        activating.join(timeout=2)
    assert not reading.is_alive() and not activating.is_alive()
    assert isinstance(
        outcome.get("error"), SessionLeaseLost if invalidate == "lease" else RetainedChatLost
    )
    assert factory.transports[0].turns == []


def test_prompt_wait_does_not_accept_a_failed_bridge_recheck(monkeypatch):
    manager, supervisor, factory, lease, _launch_spec = _rechecking_activation()
    waited = threading.Event()
    wait = supervisor._condition.wait
    start = manager.start

    def waiting(*args, **kwargs):
        waited.set()
        return wait(*args, **kwargs)

    def fail_start(*args, **kwargs):
        start(*args, **kwargs)
        raise SandboxError("bridge recheck failed")

    monkeypatch.setattr(supervisor._condition, "wait", waiting)
    monkeypatch.setattr(manager, "start", fail_start)
    reading, read_outcome = _in_thread(lambda: supervisor.operation_opened("athr_peer"))
    assert manager.start_entered.wait(timeout=2)
    activating, outcome = _in_thread(
        lambda: supervisor.start_turn(
            "athr_source",
            lease.token,
            prompt="must not send",
            model="glm-5.1",
            reasoning_effort=None,
        )
    )
    try:
        assert waited.wait(timeout=1), outcome
    finally:
        manager.start_release.set()
        reading.join(timeout=2)
        activating.join(timeout=2)
    assert not reading.is_alive() and not activating.is_alive()
    assert isinstance(read_outcome.get("error"), SandboxError)
    assert isinstance(outcome.get("error"), SandboxError)
    assert factory.transports[0].turns == []
    # A fresh, successful check lifts the failed-proof fence.
    monkeypatch.setattr(manager, "start", start)
    token, _ = supervisor.operation_opened("athr_peer")
    supervisor.operation_closed("athr_peer", token)
    supervisor.start_turn(
        "athr_source", lease.token, prompt="now verified", model="glm-5.1", reasoning_effort=None
    )
    assert len(factory.transports[0].turns) == 1


def test_failed_idle_stop_keeps_its_automatic_retry_deadline(monkeypatch):
    """A temporary inspection reservation must not suppress the stop retry."""
    manager = _Manager()
    now = {"value": 100.0}
    supervisor = SharedChatSessionSupervisor(manager, clock=lambda: now["value"])
    spec = _spec()
    supervisor.register_manifest(spec, _members())
    group = supervisor._members[spec.thread_id]
    group.idle_deadline = now["value"]
    stop = manager.stop

    def fail_stop(*_args, **_kwargs):
        raise SandboxError("Docker stop is temporarily unavailable")

    monkeypatch.setattr(manager, "stop", fail_stop)
    with pytest.raises(SandboxError, match="temporarily unavailable"):
        supervisor.stop_if_idle(spec.thread_id, generation=spec.generation)
    assert group.lifecycle_check is None
    assert group.stopping is False
    assert group.idle_deadline == now["value"] + supervisor.idle_timeout_s
    monkeypatch.setattr(manager, "stop", stop)
    now["value"] += supervisor.idle_timeout_s + 1
    stopped = supervisor.sweep()
    assert len(stopped) == 1
    assert stopped[0].running is False


@pytest.mark.parametrize("entrypoint", ["operation", "terminal"])
@pytest.mark.parametrize("race", ["before", "after", "membership", "container", "failure"])
def test_workspace_admission_waits_for_bridge_rechecks(monkeypatch, entrypoint, race):
    """Files and PTYs wait at both admission boundaries, then recheck identity."""
    manager, supervisor, _factory, _lease, _launch_spec = _rechecking_activation()
    group = supervisor._members["athr_source"]
    opened = getattr(supervisor, f"{entrypoint}_opened")
    closed = getattr(supervisor, f"{entrypoint}_closed")
    waited = threading.Event()
    wait = supervisor._condition.wait
    rechecks = []

    def waiting(*args, **kwargs):
        waited.set()
        return wait(*args, **kwargs)

    def begin_recheck():
        manager.block_start = True
        rechecks.append(_in_thread(lambda: supervisor._start_group(group)))
        assert manager.start_entered.wait(timeout=2)

    monkeypatch.setattr(supervisor._condition, "wait", waiting)
    if race == "before":
        begin_recheck()
    else:
        # Start the competing check after this request's own check, before it
        # can register its operation/PTY token. No sleep-based race timing.
        manager.block_start = False
        ensure_running = supervisor._ensure_running
        start = manager.start

        def ensure_then_recheck(current_group):
            ensure_running(current_group)
            if race == "failure":

                def fail_start(*args, **kwargs):
                    start(*args, **kwargs)
                    raise SandboxError("bridge recheck failed")

                monkeypatch.setattr(manager, "start", fail_start)
            begin_recheck()

        monkeypatch.setattr(supervisor, "_ensure_running", ensure_then_recheck)

    opening, outcome = _in_thread(lambda: opened("athr_source"))
    try:
        assert waited.wait(timeout=1), outcome
        assert group.operation_tokens == {} and group.terminal_tokens == {}
        if race == "membership":
            with supervisor._lock:
                group.members["athr_source"] = replace(group.members["athr_source"])
        elif race == "container":
            current = manager.items[_spec().container_name]
            manager.items[_spec().container_name] = replace(current, container_id="replacement")
    finally:
        manager.start_release.set()
        opening.join(timeout=2)
        for checking, _ in rechecks:
            checking.join(timeout=2)
    assert not opening.is_alive()
    assert all(not checking.is_alive() for checking, _ in rechecks)
    if race in {"before", "after"}:
        assert "error" not in outcome
        assert all("error" not in result for _, result in rechecks)
        token, descriptor = outcome["value"]
        assert descriptor.running is True
        closed("athr_source", token)
    else:
        expected_error = {
            "membership": SessionLeaseLost,
            "container": RetainedChatLost,
            "failure": SandboxError,
        }[race]
        assert isinstance(outcome.get("error"), expected_error), outcome
    assert group.operation_tokens == {} and group.terminal_tokens == {}
