"""Concurrency and capture barriers for shared live chat sandboxes."""

from __future__ import annotations

import io
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
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from httpx import ASGITransport, AsyncClient
from tests.support.owners import launch_connection

from cloud_agent_host.chat_sandbox import (
    MEMBER_UID_BASE,
    ChatSandboxInspection,
    ChatSandboxManager,
    ChatSandboxSpec,
    ChatSandboxStatus,
    RetainedChatFailStopped,
    RetainedChatLost,
    claude_member_config_dir,
)
from cloud_agent_host.chat_sandbox_markers import (
    FINAL_ARCHIVE_MARKER_NAME,
    INITIAL_ARCHIVE_MARKER_ROOT,
)
from cloud_agent_host.drivers import DriverError, InputOutcome
from cloud_agent_host.guest_observer import (
    GuestObservation,
    GuestObserverCursor,
)
from cloud_agent_host.resident_transport import (
    ResidentLaunch,
    ResidentTransportFactory,
)
from cloud_agent_host.sandbox import ProcessBackend, SandboxError
from cloud_agent_host.sandbox_archive_runtime import SandboxArchiveController
from cloud_agent_host.sandbox_manager import SharedSandboxIdentityRequest, create_app
from cloud_agent_host.session_lifecycle import SessionBusy, SessionLeaseLost
from cloud_agent_host.shared_session_supervisor import (
    SharedChatSessionSupervisor,
    SharedMemberManifest,
    SharedSessionDescriptor,
)
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
            docker_binary="docker",
            sandbox_namespace="test",
        )
        self.items: dict[str, ChatSandboxInspection] = {}
        self.events: list[object] = []
        self.starts: list[str] = []
        # Network proofs made without a start; each start makes its own.
        self.network_proofs: list[str] = []
        self.stop_count = 0
        self.host_rule_failures = 0
        self.host_rule_checks = 0
        self.host_rule_running: list[list[ChatSandboxSpec]] = []
        self.host_rule_renewals = 0
        self.host_rule_renewal_failures = 0
        self.pruned_specs: list[ChatSandboxSpec] = []
        self.pruned_observed: list[ChatSandboxInspection] | None = None
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
        with operation_profiling.substage(observer, "container_create"):
            inspection = self.items.setdefault(spec.container_name, self._inspection(spec))
        if created and facts is not None:
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

    def _run(self, *_args, **_kwargs):
        return SimpleNamespace(
            returncode=0, stdout="\n".join(f"{x.name}\t" for x in self.items.values() if x.running)
        )

    def ensure_downloaded_runtime(self, _spec: ChatSandboxSpec, runtime: str, version: str) -> None:
        """The image already holds every version these tests launch."""
        self.runtimes_ensured = [*getattr(self, "runtimes_ensured", []), (runtime, version)]

    @property
    def network(self) -> _Manager:
        return self

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

    def require_network(self, spec: ChatSandboxSpec, *, observer: Any = None) -> None:
        del observer
        self.network_proofs.append(spec.container_name)

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

    def prune_orphan_networks(
        self, specs: list[ChatSandboxSpec], observed: list[ChatSandboxInspection] | None = None
    ) -> None:
        self.pruned_specs = specs
        self.pruned_observed = observed

    def renew_host_rules(self) -> None:
        self.host_rule_renewals += 1
        if self.host_rule_renewal_failures:
            self.host_rule_renewal_failures -= 1
            raise SandboxError("nft failed: Error: Could not process rule")

    def verify_host_rules(self, running: Any = ()) -> None:
        self.host_rule_checks += 1
        self.host_rule_running.append(list(running))
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
        credential="provider-key",
        slot_id=slot,
        residency_generation=slot + 1,
        **launch_connection("claude-code"),
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


def test_orphan_stop_accepts_group_level_zero_activation_fence() -> None:
    manager = _Manager()
    spec = _spec()
    supervisor = SharedChatSessionSupervisor(manager)
    lease = supervisor.acquire(
        spec,
        thread_id="athr_source",
        owner="attempt:1",
        activation_generation=1,
        has_settled_context=False,
        manifest=_members(),
    )
    issued_member_fence = {"athr_source": 1, "athr_peer": 0}

    with pytest.raises(SessionLeaseLost, match="group-level stop"):
        supervisor.stop_if_idle(
            "athr_source",
            generation=spec.generation,
            activation_generation=0,
            reason="idle",
            member_activation_generations=issued_member_fence,
        )
    assert manager.stop_count == 0

    stopped = supervisor.stop_if_idle(
        "athr_source",
        generation=spec.generation,
        activation_generation=0,
        reason="orphaned_attempt",
        member_activation_generations=issued_member_fence,
    )

    assert stopped.running is False
    assert manager.stop_count == 1
    duplicate = supervisor.stop_if_idle(
        "athr_source",
        generation=spec.generation,
        activation_generation=0,
        reason="orphaned_attempt",
        member_activation_generations=issued_member_fence,
    )
    assert duplicate.running is False
    assert manager.stop_count == 1

    replacement = supervisor.acquire(
        spec,
        thread_id="athr_source",
        owner="attempt:2",
        activation_generation=2,
        has_settled_context=False,
        manifest=_members(),
        expected_container_id="container-shared",
    )
    assert replacement.token
    with pytest.raises(SessionLeaseLost, match="member activation changed"):
        supervisor.stop_if_idle(
            "athr_source",
            generation=spec.generation,
            activation_generation=0,
            reason="orphaned_attempt",
            member_activation_generations=issued_member_fence,
        )
    assert manager.stop_count == 1
    supervisor.abandon("athr_source", replacement.token, lost=False)
    # The original broker lease was cleared as part of the orphan stop.
    with pytest.raises(SessionLeaseLost):
        supervisor.abandon("athr_source", lease.token, lost=False)


def test_lost_archive_restart_replay_does_not_reopen_or_repeat_destructive_work() -> None:
    class MarkedManager(_Manager):
        def __init__(self) -> None:
            super().__init__()
            self.final_marker: bytes | None = None
            self.docker_stop_calls = 0

        def read_running_final_archive_marker(self, _spec, **_kwargs):
            return self.final_marker

        def read_stopped_final_archive_marker_optional(self, _spec, **_kwargs):
            return self.final_marker

        def stop(self, spec, **kwargs):
            if self.items[spec.container_name].running:
                self.docker_stop_calls += 1
            return super().stop(spec, **kwargs)

    manager = MarkedManager()
    spec = _spec()
    manager.items[spec.container_name] = manager._inspection(spec, running=True)
    lost_member = SharedMemberManifest(
        thread_id="athr_source",
        native_session_id="native-source",
        context_status="lost",
        activation_generation=1,
        process_generation=4,
        residency_generation=2,
        resident_state="lost",
        resident_slot_id=0,
        last_settled_job_id="ajob_source",
    )
    final_uploads: list[tuple[SharedMemberManifest, ...]] = []

    def prepare(_spec, inspection):
        assert inspection.running is True
        manager.final_marker = b"pending-final-upload"

    def archive(_spec, inspection, members):
        assert inspection.running is False
        if manager.final_marker == b"uploaded-final-receipt":
            return
        assert manager.final_marker == b"pending-final-upload"
        final_uploads.append(members)
        manager.final_marker = b"uploaded-final-receipt"

    supervisor = SharedChatSessionSupervisor(
        manager,
        stopped_archive_prepare_hook=prepare,
        stopped_archive_hook=archive,
    )
    supervisor.register_manifest(spec, [lost_member], expected_container_id="container-shared")
    issued_stop = {
        "thread_id": "athr_source",
        "generation": spec.generation,
        "activation_generation": 1,
        "reason": "lost",
        "member_activation_generations": {"athr_source": 1},
    }

    stopped = supervisor.stop_if_idle(
        "athr_source",
        generation=issued_stop["generation"],
        activation_generation=issued_stop["activation_generation"],
        reason=issued_stop["reason"],
        member_activation_generations=issued_stop["member_activation_generations"],
    )
    assert stopped.running is False
    assert manager.docker_stop_calls == 1
    assert len(final_uploads) == 1
    assert final_uploads[0][0].context_status == "lost"

    # The broker died after Docker stop and final upload, before the control
    # plane received its stopped observation. A replacement probes the durable
    # upload marker and completes cleanup without another Docker stop/upload.
    replacement = SharedChatSessionSupervisor(
        manager,
        stopped_archive_prepare_hook=prepare,
        stopped_archive_hook=archive,
    )
    descriptors = replacement.reconcile()
    assert len(descriptors) == 1 and descriptors[0].running is False
    replacement.register_manifest(spec, [lost_member], expected_container_id="container-shared")
    replacement.register_manifest(
        spec,
        [replace(lost_member, context_status="live", resident_state="live")],
        expected_container_id="container-shared",
    )
    assert replacement.status("athr_source").context_status == "lost"
    with pytest.raises(RetainedChatLost, match="context was lost"):
        replacement.acquire(
            spec,
            thread_id="athr_source",
            owner="attempt:2",
            activation_generation=2,
            has_settled_context=True,
            manifest=[replace(lost_member, context_status="live", resident_state="live")],
            expected_container_id="container-shared",
        )

    # A delayed copy of the original stop is harmless after restart too.
    duplicate = replacement.stop_if_idle(
        "athr_source",
        generation=issued_stop["generation"],
        activation_generation=issued_stop["activation_generation"],
        reason=issued_stop["reason"],
        member_activation_generations=issued_stop["member_activation_generations"],
    )
    assert duplicate.running is False
    assert manager.docker_stop_calls == 1
    assert len(final_uploads) == 1


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


def test_reconcile_hands_the_network_prune_the_listing_it_just_read():
    """Whose bridge each retained container is on comes from that listing, not a re-read."""
    manager, supervisor = _running_group()

    supervisor.reconcile()

    assert manager.pruned_specs == [_spec()]
    assert manager.pruned_observed == manager.list()


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
            before_stopped_start=None,
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


def test_the_sweep_names_its_running_groups_to_the_host_rule_check():
    """A table the check reinstalls comes back without any group's relay or SSH exceptions.

    A firewall reload flushes the host's ruleset; a group still running behind
    the reinstalled table stays cut off from its MCP relay and SSH servers until
    its bridge is synced. Only the sweep knows which groups run, so it names them.
    """
    manager, supervisor = _running_group()

    assert supervisor.guard_network() == []

    assert manager.host_rule_running == [[_spec()]]


def test_one_failed_check_is_retried_before_anything_is_stopped():
    manager, supervisor = _running_group()
    manager.host_rule_failures = 1

    assert supervisor.guard_network() == []
    assert manager.host_rule_checks == 2
    assert manager.stop_count == 0


def test_while_nothing_runs_the_sweep_starts_no_guard_helper():
    """The running-container query proves that nothing remains behind the rules."""
    manager = _Manager()
    supervisor = SharedChatSessionSupervisor(manager)

    for _ in range(120):
        assert supervisor.guard_network() == []
    assert manager.host_rule_checks == 0
    assert manager.host_rule_renewals == 0
    assert manager.stop_count == 0


def test_a_running_group_without_an_active_turn_keeps_every_guard_check():
    """Background processes still require the boundary after the turn ends."""
    manager, supervisor = _running_group()
    group = supervisor._members["athr_source"]
    assert group.inspection.running
    assert not any(member.current_turn for member in group.members.values())

    for _ in range(3):
        assert supervisor.guard_network() == []

    assert manager.host_rule_checks == 3
    assert manager.host_rule_running == [[_spec()]] * 3
    assert manager.host_rule_renewals == 0
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


def test_closing_a_terminal_does_not_arm_the_idle_deadline_of_a_lost_group():
    """A lost group still reads as running, but nothing will ever stop it again.

    Armed, the idle sweep would pick it on every pass and fail each time.
    """
    manager = _Manager()
    supervisor = SharedChatSessionSupervisor(manager, idle_timeout_s=30, clock=lambda: 100.0)
    supervisor.register_manifest(_spec(), _members())
    token, _ = supervisor.terminal_opened("athr_source")
    manager.items.pop(_spec().container_name)
    with pytest.raises(RetainedChatLost, match="missing"):
        supervisor.list_for_thread("athr_peer")
    group = supervisor._members["athr_source"]
    assert group.container_lost is True
    assert group.inspection.running is True

    supervisor.terminal_closed("athr_source", token, last_activity_monotonic=90.0)

    assert group.idle_deadline is None
    assert supervisor.sweep() == []


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
                ["python3", "-I", "-c", calls[slot][0][3], str(config), str(projects)], check=True
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
        subprocess.run(["python3", "-I", "-c", call[3], str(config), str(projects)], check=True)
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
        ["python3", "-I", "-c", calls[0][3], str(config), str(projects)], capture_output=True
    )

    assert result.returncode != 0 and b"RuntimeError" in result.stderr, result.stderr
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


def _waits_without_sleeping(monkeypatch, supervisor) -> list[float]:
    """Advance the clock by each wait instead of sleeping; answer the waits made."""
    now = [1000.0]
    waits: list[float] = []

    def wait(timeout: float | None = None) -> bool:
        assert timeout is not None
        waits.append(timeout)
        now[0] += timeout
        return False

    monkeypatch.setattr(time, "monotonic", lambda: now[0])
    monkeypatch.setattr(supervisor._condition, "wait", wait)
    return waits


@pytest.mark.parametrize(
    ("transitions", "message"),
    [
        (("snapshotting",), "shared workspace snapshot is in progress"),
        (("starting",), "shared workspace start is in progress"),
        (("stopping",), "shared workspace stop is in progress"),
        (("stopping", "starting", "snapshotting"), "shared workspace snapshot is in progress"),
        (("stopping", "starting"), "shared workspace start is in progress"),
    ],
)
def test_a_prompt_waits_out_a_group_transition_and_names_it_at_the_deadline(
    monkeypatch, transitions, message
):
    """A prompt waits up to 30 s, in half-second slices, for the group to settle."""
    manager = _Manager()
    supervisor = SharedChatSessionSupervisor(manager)
    register = supervisor.register_manifest

    def register_then_transition(*args, **kwargs):
        registered = register(*args, **kwargs)
        group = supervisor._groups[_spec().sandbox_id]
        for transition in transitions:
            setattr(group, transition, True)
        return registered

    monkeypatch.setattr(supervisor, "register_manifest", register_then_transition)
    waits = _waits_without_sleeping(monkeypatch, supervisor)

    with pytest.raises(SessionBusy, match=rf"^{message}$"):
        supervisor.acquire(
            _spec(),
            thread_id="athr_source",
            owner="attempt:1",
            activation_generation=1,
            has_settled_context=True,
            manifest=_members(),
        )

    assert waits == [0.5] * 60


@pytest.mark.parametrize(
    ("opener", "kind"), [("operation_opened", "operation"), ("terminal_opened", "terminal")]
)
def test_a_reference_names_its_kind_when_membership_changes_before_admission(
    monkeypatch, opener, kind
):
    manager = _Manager()
    supervisor = SharedChatSessionSupervisor(manager)
    supervisor.register_manifest(_spec(), _members())

    def rejoin(group):
        group.members["athr_peer"] = replace(group.members["athr_peer"])

    monkeypatch.setattr(supervisor, "_recover_start_archive_completion", rejoin)

    with pytest.raises(
        SessionLeaseLost, match=rf"^shared membership changed before {kind} admission$"
    ):
        getattr(supervisor, opener)("athr_peer")


@pytest.mark.parametrize(
    ("opener", "kind"), [("operation_opened", "operation"), ("terminal_opened", "terminal")]
)
def test_a_reference_names_its_kind_when_the_lifecycle_changes_during_admission(
    monkeypatch, opener, kind
):
    manager = _Manager()
    supervisor = SharedChatSessionSupervisor(manager)
    supervisor.register_manifest(_spec(), _members())
    ensure_running = supervisor._ensure_running

    def ensure_running_then_restart(group, **kwargs):
        ensure_running(group, **kwargs)
        group.lifecycle_generation += 1

    monkeypatch.setattr(supervisor, "_ensure_running", ensure_running_then_restart)

    with pytest.raises(
        SessionLeaseLost, match=rf"^shared sandbox lifecycle changed during {kind} admission$"
    ):
        getattr(supervisor, opener)("athr_peer")


@pytest.mark.parametrize("terminal", [False, True])
def test_opening_a_reference_keeps_the_group_awake_under_its_own_token(terminal):
    manager = _Manager()
    supervisor = SharedChatSessionSupervisor(manager)
    supervisor.register_manifest(_spec(), _members())
    group = supervisor._groups[_spec().sandbox_id]
    group.idle_deadline = 123.0

    opener = supervisor.terminal_opened if terminal else supervisor.operation_opened
    token, _descriptor = opener("athr_peer")

    assert group.idle_deadline is None
    tokens, others = (
        (group.terminal_tokens, group.operation_tokens)
        if terminal
        else (group.operation_tokens, group.terminal_tokens)
    )
    assert tokens == {token: "athr_peer"}
    assert token not in others


def test_the_initial_archive_waits_out_a_lifecycle_check_and_names_it_at_the_deadline(
    monkeypatch,
):
    manager = _Manager()
    supervisor = SharedChatSessionSupervisor(manager, transport_factory=_TransportFactory())
    lease = supervisor.acquire(
        _spec(),
        thread_id="athr_source",
        owner="attempt:1",
        activation_generation=1,
        has_settled_context=False,
        manifest=_members(),
    )
    supervisor._groups[_spec().sandbox_id].lifecycle_check = object()
    supervisor.initial_archive_wait_s = 2.0
    waits = _waits_without_sleeping(monkeypatch, supervisor)

    with pytest.raises(SessionBusy, match=r"^shared sandbox lifecycle check is in progress$"):
        supervisor.capture_initial_archive("athr_source", lease.token, capture=lambda *_args: {})

    assert waits == [0.5] * 4


def test_the_initial_archive_waits_out_a_lifecycle_check_begun_under_its_barrier(monkeypatch):
    manager = _Manager()
    supervisor = SharedChatSessionSupervisor(manager, transport_factory=_TransportFactory())
    lease = supervisor.acquire(
        _spec(),
        thread_id="athr_source",
        owner="attempt:1",
        activation_generation=1,
        has_settled_context=False,
        manifest=_members(),
    )
    group = supervisor._groups[_spec().sandbox_id]
    admit = supervisor._require_initial_archive_admission

    def admit_then_begin_a_check(*args, **kwargs):
        admit(*args, **kwargs)
        group.lifecycle_check = object()

    monkeypatch.setattr(supervisor, "_require_initial_archive_admission", admit_then_begin_a_check)
    supervisor.initial_archive_wait_s = 2.0
    waits = _waits_without_sleeping(monkeypatch, supervisor)

    with pytest.raises(SessionBusy, match=r"^shared sandbox lifecycle check is in progress$"):
        supervisor.capture_initial_archive("athr_source", lease.token, capture=lambda *_args: {})

    assert waits == [0.5] * 4
    assert group.snapshotting is False


def test_the_initial_archive_waits_for_open_terminals_behind_its_barrier(monkeypatch):
    """A terminal admitted before the barrier is given the same bounded time to close."""
    manager = _Manager()
    supervisor = SharedChatSessionSupervisor(manager, transport_factory=_TransportFactory())
    lease = supervisor.acquire(
        _spec(),
        thread_id="athr_source",
        owner="attempt:1",
        activation_generation=1,
        has_settled_context=False,
        manifest=_members(),
    )
    group = supervisor._groups[_spec().sandbox_id]
    group.terminal_tokens["terminal-before-barrier"] = "athr_peer"
    supervisor.initial_archive_wait_s = 2.0
    waits = _waits_without_sleeping(monkeypatch, supervisor)

    with pytest.raises(SessionBusy, match=r"^shared workspace still has active mutations$"):
        supervisor.capture_initial_archive("athr_source", lease.token, capture=lambda *_args: {})

    assert waits == [0.5] * 4
    assert group.snapshotting is False


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
        assert manager.network.network_for(_spec) in inspection.networks
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
