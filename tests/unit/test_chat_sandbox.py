"""Contract tests for Docker-owned restartable chat containers."""

from __future__ import annotations

import inspect
import io
import json
import os
import shutil
import signal
import subprocess
import sys
import time
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest

from cloud_agent_host import chat_sandbox as chat_sandbox_mod
from cloud_agent_host.chat_sandbox import (
    ACTIVATION_TMPFS,
    ARCHIVE_IMPORT_TIMEOUT_PER_GIB_S,
    CHAT_BOOTSTRAP_NETWORK,
    CHAT_DISK_LIMIT_LABEL,
    CHAT_IMAGE_DIGEST_LABEL,
    CHAT_NAMESPACE_LABEL,
    CHAT_OFFLINE_DNS,
    CHAT_SANDBOX_ID_LABEL,
    CHAT_SANDBOX_LABEL,
    CHAT_STATE_DIR,
    CHAT_THREAD_LABEL,
    CHAT_WORKDIR,
    FINAL_ARCHIVE_MARKER_NAME,
    INITIAL_ARCHIVE_MARKER_ROOT,
    MEMBER_TMPFS_ROOT,
    MEMBER_UID_BASE,
    TURN_NETWORK_PROOF_S,
    ChatSandboxInspection,
    ChatSandboxManager,
    ChatSandboxSpec,
    ChatSandboxStatus,
    RetainedChatBackend,
    RetainedChatLost,
    SandboxActionFacts,
    _RetainedTerminalProcess,
    claude_member_config_dir,
)
from cloud_agent_host.guest_observer import (
    GUEST_OBSERVER_BINARY,
    GUEST_OBSERVER_ROOT,
    GUEST_OBSERVER_ROTATE_BYTES,
    GUEST_OBSERVER_VERSION,
    GuestObservation,
    GuestObserverController,
    GuestObserverCursor,
    GuestObserverSnapshot,
    GuestObserverStatus,
)
from cloud_agent_host.runtimes import RuntimeMCPConfig, get_runtime, registered_runtimes
from cloud_agent_host.sandbox import KATA_RUNTIME, ContainerBackend, SandboxError, SandboxSpec


def _spec() -> ChatSandboxSpec:
    return ChatSandboxSpec(
        thread_id="athr_01",
        sandbox_id="asbx_01",
        generation=3,
        runtime_id="claude-code",
        runtime_version="2.1.220",
        sandbox_image_digest="sha256:image-pinned",
    )


def test_archive_export_is_stopped_exact_id_and_metadata_excludes_secrets(monkeypatch, tmp_path):
    spec = _spec()
    manager = ChatSandboxManager(ContainerBackend(image="image", sandbox_namespace="deployment-a"))
    labels = {
        **spec.docker_labels(),
        CHAT_NAMESPACE_LABEL: "deployment-a",
        "custom-secret": "hidden",
    }
    raw = json.loads(_inspect_payload(spec, status="exited", labels=labels))[0]
    raw["Config"].update(
        {"Env": ["AWS_SECRET_ACCESS_KEY=hidden"], "User": "10001:10001", "WorkingDir": "/workspace"}
    )
    raw["HostConfig"].update({"Binds": ["/host/secret:/secret"], "Memory": 1024, "Runtime": "kata"})
    calls = []

    def run(argv, *, timeout):
        calls.append(argv)
        assert argv[1:3] == ["container", "inspect"]
        return SimpleNamespace(returncode=0, stdout=json.dumps([raw]), stderr="")

    class ExportProcess:
        returncode = 0

        def __init__(self, argv, *, stdout, stderr):
            calls.append(argv)
            stdout.write(b"complete Docker filesystem tar")

        def poll(self):
            return 0

    monkeypatch.setattr(manager, "_run", run)
    monkeypatch.setattr(chat_sandbox_mod.subprocess, "Popen", ExportProcess)
    with (tmp_path / "rootfs.tar").open("wb") as output:
        metadata = manager.export_stopped_archive(
            spec, container_id="sha256:container-one", output=output, max_bytes=1024
        )
    assert [manager.backend.docker_binary, "container", "export", "sha256:container-one"] in calls
    assert metadata["Config"]["Image"] == spec.sandbox_image_digest
    assert metadata["HostConfig"] == {"Memory": 1024, "Runtime": "kata"}
    assert "Env" not in metadata["Config"]
    assert "custom-secret" not in metadata["Config"]["Labels"]
    assert "hidden" not in json.dumps(metadata)
    assert "Mounts" not in metadata and "NetworkSettings" not in metadata
    assert not any("exec" in command or "start" in command for command in calls)


@pytest.mark.parametrize(
    "change",
    ["running", "paused", "created", "wrong-id", "wrong-generation", "foreign", "volume"],
)
def test_archive_fences_reject_nonstopped_unowned_or_changed_containers(monkeypatch, change):
    spec = _spec()
    manager = ChatSandboxManager(ContainerBackend(image="image", sandbox_namespace="deployment-a"))
    labels = {**spec.docker_labels(), CHAT_NAMESPACE_LABEL: "deployment-a"}
    if change == "wrong-generation":
        labels[chat_sandbox_mod.CHAT_GENERATION_LABEL] = "100"
    if change == "foreign":
        labels[CHAT_NAMESPACE_LABEL] = "foreign"
    payload = _inspect_payload(
        spec,
        labels=labels,
        status="created" if change == "created" else "exited",
        running=change == "running",
        paused=change == "paused",
        mounts=[{"Type": "volume", "Destination": "/agent-state"}] if change == "volume" else [],
    )
    monkeypatch.setattr(
        manager,
        "_run",
        lambda *_args, **_kwargs: SimpleNamespace(returncode=0, stdout=payload, stderr=""),
    )
    with pytest.raises(SandboxError):
        manager.require_stopped_archive(
            spec, container_id="replacement" if change == "wrong-id" else "sha256:container-one"
        )


def test_archived_removal_never_stops_an_external_start_before_removal(monkeypatch):
    spec = _spec()
    manager = ChatSandboxManager(ContainerBackend(image="image", sandbox_namespace="deployment-a"))
    labels = {**spec.docker_labels(), CHAT_NAMESPACE_LABEL: "deployment-a"}
    calls = []

    def run(argv, *, timeout):
        calls.append(argv)
        if argv[1:3] == ["container", "inspect"]:
            return SimpleNamespace(
                returncode=0,
                stdout=_inspect_payload(spec, status="exited", labels=labels),
                stderr="",
            )
        assert argv == [manager.backend.docker_binary, "container", "rm", "sha256:container-one"]
        # Docker refuses because an administrator started the exact original
        # after the stopped inspection. No generic stop may make it removable.
        return SimpleNamespace(returncode=1, stdout="", stderr="container is running")

    monkeypatch.setattr(manager, "_run", run)
    monkeypatch.setattr(
        manager, "stop", lambda *_args: pytest.fail("archive must never stop a new start")
    )
    with pytest.raises(SandboxError, match="removal failed"):
        manager.remove_stopped_archived(spec, container_id="sha256:container-one")
    assert not any("stop" in argv or "--force" in argv for argv in calls)


def test_archived_removal_rejects_start_at_second_identity_fence(monkeypatch):
    spec = _spec()
    manager = ChatSandboxManager(ContainerBackend(image="image", sandbox_namespace="deployment-a"))
    labels = {**spec.docker_labels(), CHAT_NAMESPACE_LABEL: "deployment-a"}
    calls = []

    def run(argv, *, timeout):
        calls.append(argv)
        assert argv[1:3] == ["container", "inspect"]
        return SimpleNamespace(
            returncode=0,
            stdout=_inspect_payload(spec, status="exited", labels=labels, running=len(calls) > 1),
            stderr="",
        )

    monkeypatch.setattr(manager, "_run", run)
    with pytest.raises(SandboxError, match="stopped or dead container"):
        manager.remove_stopped_archived(spec, container_id="sha256:container-one")
    assert len(calls) == 2


def test_dead_container_exports_and_removes_without_force(monkeypatch, tmp_path):
    spec = _spec()
    manager = ChatSandboxManager(ContainerBackend(image="image", sandbox_namespace="deployment-a"))
    labels = {**spec.docker_labels(), CHAT_NAMESPACE_LABEL: "deployment-a"}
    calls = []
    removed = False

    def run(argv, *, timeout):
        nonlocal removed
        calls.append(argv)
        if argv[1:3] == ["container", "inspect"]:
            if removed:
                return SimpleNamespace(returncode=1, stdout="", stderr="No such container")
            return SimpleNamespace(
                returncode=0,
                stdout=_inspect_payload(spec, status="dead", labels=labels, networks=()),
                stderr="",
            )
        assert argv == [manager.backend.docker_binary, "container", "rm", "sha256:container-one"]
        removed = True
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    class ExportProcess:
        returncode = 0

        def __init__(self, argv, *, stdout, stderr):
            calls.append(argv)
            stdout.write(b"dead container filesystem")

        def poll(self):
            return 0

        def kill(self):
            raise AssertionError("export must finish")

        def wait(self, timeout):
            return 0

    monkeypatch.setattr(manager, "_run", run)
    monkeypatch.setattr(chat_sandbox_mod.subprocess, "Popen", ExportProcess)
    with (tmp_path / "rootfs.tar").open("wb") as output:
        metadata = manager.export_stopped_archive(
            spec, container_id="sha256:container-one", output=output, max_bytes=1024
        )
    assert metadata["Id"] == "sha256:container-one"
    manager.remove_stopped_archived(spec, container_id="sha256:container-one")
    assert [manager.backend.docker_binary, "container", "export", "sha256:container-one"] in calls
    assert [manager.backend.docker_binary, "container", "rm", "sha256:container-one"] in calls
    assert not any("--force" in argv or "stop" in argv for argv in calls)


def test_stop_accepts_a_running_container_that_lands_dead(monkeypatch):
    spec = _spec()
    manager = ChatSandboxManager(ContainerBackend(image="image", sandbox_namespace="deployment-a"))
    labels = {**spec.docker_labels(), CHAT_NAMESPACE_LABEL: "deployment-a"}
    calls = []
    inspections = {"count": 0}

    def run(argv, *, timeout):
        calls.append(list(argv))
        if argv[1:3] == ["container", "inspect"]:
            inspections["count"] += 1
            running = inspections["count"] == 1
            status = "running" if running else "dead"
            return SimpleNamespace(
                returncode=0,
                stdout=_inspect_payload(spec, status=status, running=running, labels=labels),
                stderr="",
            )
        assert argv[1] == "stop"
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(manager, "_run", run)
    stopped = manager.stop(spec, release_networks=False)
    assert stopped.status is ChatSandboxStatus.DEAD
    assert stopped.running is False
    assert not any("--force" in argv or argv[1:3] == ["container", "rm"] for argv in calls)


def test_archive_export_rejects_oversize_and_changed_identity_after_export(monkeypatch, tmp_path):
    spec = _spec()
    manager = ChatSandboxManager(ContainerBackend(image="image", sandbox_namespace="deployment-a"))
    labels = {**spec.docker_labels(), CHAT_NAMESPACE_LABEL: "deployment-a"}
    inspections = 0

    def run(argv, *, timeout):
        nonlocal inspections
        inspections += 1
        payload = json.loads(_inspect_payload(spec, status="exited", labels=labels))
        if inspections > 2:
            payload[0]["Id"] = "replacement"
        return SimpleNamespace(returncode=0, stdout=json.dumps(payload), stderr="")

    class ExportProcess:
        returncode = 0

        def __init__(self, argv, *, stdout, stderr):
            stdout.write(b"export data")

        def poll(self):
            return 0

    monkeypatch.setattr(manager, "_run", run)
    monkeypatch.setattr(chat_sandbox_mod.subprocess, "Popen", ExportProcess)
    with (
        (tmp_path / "rootfs.tar").open("wb") as output,
        pytest.raises(SandboxError, match="size limit"),
    ):
        manager.export_stopped_archive(
            spec, container_id="sha256:container-one", output=output, max_bytes=1
        )
    inspections = 0
    with (
        (tmp_path / "rootfs.tar").open("wb") as output,
        pytest.raises(SandboxError, match="durable sandbox record"),
    ):
        manager.export_stopped_archive(
            spec, container_id="sha256:container-one", output=output, max_bytes=1024
        )


@pytest.fixture(autouse=True)
def _plain_group_network(monkeypatch):
    """Keep these tests on one fixed bridge; the group network is tested elsewhere.

    Everything here is about exec, terminals, observers and ownership. Their
    containers sit on ``agent-net`` and the group network's own lifecycle
    (creation, relays, host exceptions, release, pruning, the in-guest check)
    is covered against a model of Docker in ``test_chat_group_network.py``.
    """
    monkeypatch.setattr(ChatSandboxManager, "network_for", lambda _self, _spec: "agent-net")
    monkeypatch.setattr(ChatSandboxManager, "require_host_rules", lambda _self: None)
    monkeypatch.setattr(
        ChatSandboxManager, "_ensure_group_network", lambda _self, _spec: "agent-net"
    )
    monkeypatch.setattr(
        ChatSandboxManager,
        "_require_group_network",
        lambda self, spec, _current, *_snapshot: self._require(spec),
    )
    monkeypatch.setattr(ChatSandboxManager, "_sync_kata_guest_network", lambda *_args: None)
    monkeypatch.setattr(
        ChatSandboxManager,
        "_verify_guest_network",
        lambda *_args, **_kwargs: None,
    )
    monkeypatch.setattr(
        ChatSandboxManager,
        "_release_sandbox_networks",
        lambda self, spec, _current, *, expected_container_id: self._require_exact(
            spec, expected_container_id
        ),
    )


def _observer_snapshot_payload(snapshot: GuestObserverSnapshot) -> bytes:
    status = snapshot.status
    return json.dumps(
        {
            "status": {
                "version": GUEST_OBSERVER_VERSION,
                "ready": True,
                "pid": status.pid,
                "poll_seq": status.poll_seq,
                "event_seq": status.event_seq,
                "lost": status.lost,
                "journal_dropped": status.journal_dropped,
                "journal_bytes": status.journal_bytes,
                "journal_dev": status.journal_dev,
                "journal_ino": status.journal_ino,
            },
            "journal_size": snapshot.journal_size,
            "guest_mono_ns": snapshot.guest_mono_ns,
        },
        separators=(",", ":"),
    ).encode()


def _inspect_payload(
    spec: ChatSandboxSpec,
    *,
    status: str = "created",
    running: bool = False,
    paused: bool = False,
    labels: dict[str, str] | None = None,
    mounts: list[dict] | None = None,
    networks: tuple[str, ...] = ("agent-net",),
    sandbox_runtime: str = "",
) -> str:
    return json.dumps(
        [
            {
                "Id": "sha256:container-one",
                "Name": f"/{spec.container_name}",
                "Image": "sha256:image-one",
                "Config": {
                    "Image": spec.sandbox_image_digest or "sha256:image-one",
                    "Labels": labels or spec.docker_labels(),
                },
                "State": {"Status": status, "Running": running, "Paused": paused},
                "HostConfig": {"Runtime": sandbox_runtime},
                "Mounts": mounts or [],
                "NetworkSettings": {"Networks": {network: {} for network in networks}},
            }
        ]
    )


def test_create_command_has_stable_identity_and_no_chat_state_mount_or_secret():
    spec = _spec()
    manager = ChatSandboxManager(ContainerBackend(image="registry/agent:1"))

    command = manager.build_create_command(spec)
    joined = " ".join(command)

    assert command[:2] == ["docker", "create"]
    assert command[command.index("--name") + 1] == spec.container_name
    assert "--rm" not in command
    assert "--mount" not in command
    assert "--volume" not in command
    assert "-v" not in command
    assert "--env" not in command
    assert CHAT_WORKDIR in command
    assert CHAT_STATE_DIR not in command
    assert f"{CHAT_SANDBOX_LABEL}=true" in joined
    assert f"{CHAT_NAMESPACE_LABEL}=default" in joined
    assert f"{CHAT_THREAD_LABEL}=athr_01" in joined
    assert f"{CHAT_SANDBOX_ID_LABEL}=asbx_01" in joined
    assert f"{CHAT_IMAGE_DIGEST_LABEL}=sha256:image-pinned" in joined
    assert command[-3] == "sha256:image-pinned"
    assert f"{ACTIVATION_TMPFS}:rw,nosuid,nodev,noexec" in joined
    assert f"--network {CHAT_BOOTSTRAP_NETWORK}" in joined
    assert f"--dns {CHAT_OFFLINE_DNS}" in joined
    assert "--cap-drop ALL" in joined
    assert "--security-opt no-new-privileges" in joined
    assert "--pids-limit 512" in joined
    assert "sleep 2147483647 & child=$!" in command[-1]
    assert "--storage-opt" not in command
    assert CHAT_DISK_LIMIT_LABEL not in spec.docker_labels()


def test_a_requested_disk_quota_is_not_dropped_when_docker_refuses_it(monkeypatch):
    spec = replace(_spec(), disk_limit_mib=20480)
    manager = ChatSandboxManager(ContainerBackend(image="img"))
    monkeypatch.setattr(manager, "inspect", lambda _name: None)
    creates = []

    def run(argv, **_kwargs):
        creates.append(argv)
        return SimpleNamespace(
            returncode=125,
            stdout="",
            stderr="--storage-opt is supported only for overlay over xfs with pquota",
        )

    monkeypatch.setattr(manager, "_run", run)

    with pytest.raises(SandboxError, match="xfs with pquota"):
        manager.create(spec)

    assert len(creates) == 1
    assert creates[0][creates[0].index("--storage-opt") + 1] == "size=20480M"
    assert spec.docker_labels()[CHAT_DISK_LIMIT_LABEL] == "20480"


@pytest.mark.parametrize("persisted_disk", [None, "0", "8192"])
@pytest.mark.parametrize("requested_disk", [0, 8192])
def test_retained_disk_policy_must_match_even_when_a_legacy_label_is_absent(
    persisted_disk, requested_disk
):
    spec = replace(_spec(), disk_limit_mib=requested_disk)
    labels = spec.docker_labels()
    labels.pop(CHAT_DISK_LIMIT_LABEL, None)
    if persisted_disk is not None:
        labels[CHAT_DISK_LIMIT_LABEL] = persisted_disk
    inspection = ChatSandboxInspection(
        container_id="container-one",
        name=spec.container_name,
        status=ChatSandboxStatus.STOPPED,
        running=False,
        image=spec.sandbox_image_digest or "sha256:image-pinned",
        labels=labels,
        mounts=(),
        networks=(),
    )
    manager = ChatSandboxManager(ContainerBackend(image="img"))

    if int(persisted_disk or 0) == requested_disk:
        manager._verify_identity(inspection, spec)
    else:
        with pytest.raises(SandboxError, match="outside this chat/generation"):
            manager._verify_identity(inspection, spec)


def test_kata_create_command_reserves_a_root_only_guest_observer_journal():
    spec = _spec()
    manager = ChatSandboxManager(
        ContainerBackend(
            image="registry/agent:1",
            runtime=KATA_RUNTIME,
        )
    )

    command = manager.build_create_command(spec)
    joined = " ".join(command)

    assert f"{GUEST_OBSERVER_ROOT}:rw,nosuid,nodev,noexec,mode=0700,uid=0,gid=0" in joined
    assert "--cap-drop ALL" in joined
    assert "--cap-add BPF" in joined
    assert "--security-opt no-new-privileges" in joined
    assert command[command.index("--runtime") + 1] == KATA_RUNTIME


def test_finishing_guest_observation_flushes_after_the_turn_end_fence():
    closing = GuestObserverSnapshot(
        status=GuestObserverStatus(91, 20, 8, 0, 0, 0, 7, 19),
        journal_size=0,
        guest_mono_ns=2_000_000_000,
    )
    flushed = GuestObserverSnapshot(
        status=GuestObserverStatus(91, 22, 8, 0, 0, 0, 7, 19),
        journal_size=0,
        guest_mono_ns=2_050_000_000,
    )
    waits: list[int | None] = []

    def capture(argv, _timeout_s):
        if GUEST_OBSERVER_BINARY in argv:
            wanted = int(argv[-2])
            waits.append(None if wanted < 0 else wanted)
            snapshot = closing if wanted < 0 else flushed
            return SimpleNamespace(
                returncode=0,
                stdout=_observer_snapshot_payload(snapshot),
                stderr=b"",
            )
        return SimpleNamespace(returncode=0, stdout=b"", stderr=b"")

    controller = GuestObserverController(
        capture=capture,
        privileged=lambda *_args: SimpleNamespace(returncode=0, stdout=b"", stderr=b""),
    )
    cursor = GuestObserverCursor(
        uid=MEMBER_UID_BASE,
        offset=0,
        poll_seq=3,
        observer_pid=91,
        event_seq=7,
        journal_dev=7,
        journal_ino=19,
        guest_mono_ns=1_000_000_000,
        available=True,
    )

    observation = controller.finish(cursor)

    assert observation.complete is True
    # The first post-turn publication can have completed its poll before the
    # closing status was read.  Crossing the following publication proves a
    # full drain happened after that read.
    assert waits == [None, 21]


def test_stale_guest_observer_cleanup_only_signals_the_trusted_binary():
    calls: list[tuple[list[str], bool]] = []

    def privileged(argv, detach, _timeout_s):
        calls.append((list(argv), detach))
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    ready = GuestObserverSnapshot(
        status=GuestObserverStatus(91, 1, 0, 0, 0, 0, 7, 19),
        journal_size=0,
        guest_mono_ns=1,
    )
    controller = GuestObserverController(
        capture=lambda *_args: SimpleNamespace(
            returncode=0,
            stdout=_observer_snapshot_payload(ready),
            stderr=b"",
        ),
        privileged=privileged,
    )

    controller.start()

    cleanup_argv, cleanup_detached = calls[0]
    assert cleanup_detached is False
    assert cleanup_argv[-1] == GUEST_OBSERVER_BINARY
    compile(cleanup_argv[2], "<guest-observer-cleanup>", "exec")
    assert "os.path.samefile" in cleanup_argv[2]
    assert "stale guest observer did not exit" in cleanup_argv[2]
    assert calls[1] == ([GUEST_OBSERVER_BINARY], True)


def test_guest_observer_snapshot_verifies_the_status_pid_binary(monkeypatch):
    manager = ChatSandboxManager(ContainerBackend(image="registry/agent:1", runtime=KATA_RUNTIME))
    calls: list[list[str]] = []
    payload = json.dumps(
        {
            "status": {
                "version": GUEST_OBSERVER_VERSION,
                "ready": True,
                "pid": 91,
                "poll_seq": 22,
                "event_seq": 8,
                "lost": 0,
                "journal_dropped": 0,
                "journal_bytes": 0,
                "journal_dev": 7,
                "journal_ino": 19,
            },
            "journal_size": 0,
            "guest_mono_ns": 2_050_000_000,
        }
    ).encode()

    def capture(_spec, argv, **_kwargs):
        calls.append(list(argv))
        return SimpleNamespace(returncode=0, stdout=payload, stderr=b"")

    monkeypatch.setattr(manager, "exec_capture", capture)

    snapshot = manager._guest_observer_snapshot(_spec(), after_poll_seq=21)

    assert snapshot.status.poll_seq == 22
    command = calls[0]
    compile(command[2], "<guest-observer-snapshot>", "exec")
    assert command[-3:] == [GUEST_OBSERVER_BINARY, "21", "3.0"]
    assert "os.path.samefile" in command[2]


def test_short_guest_journal_read_is_unavailable_not_a_false_complete_census():
    snapshot = GuestObserverSnapshot(
        status=GuestObserverStatus(91, 21, 8, 0, 0, 10, 7, 19),
        journal_size=10,
        guest_mono_ns=2_050_000_000,
    )

    def capture(argv, _timeout_s):
        stdout = _observer_snapshot_payload(snapshot) if GUEST_OBSERVER_BINARY in argv else b"short"
        return SimpleNamespace(returncode=0, stdout=stdout, stderr=b"")

    controller = GuestObserverController(
        capture=capture,
        privileged=lambda *_args: SimpleNamespace(returncode=0, stdout=b"", stderr=b""),
    )
    cursor = GuestObserverCursor(
        uid=MEMBER_UID_BASE,
        offset=0,
        observer_pid=91,
        event_seq=7,
        journal_dev=7,
        journal_ino=19,
        guest_mono_ns=1_000_000_000,
        available=True,
    )

    observation = controller.finish(cursor)

    assert observation.source is None
    assert observation.complete is False
    assert "published fence" in (observation.detail or "")


def test_retained_guest_journal_rotates_only_without_active_turns(monkeypatch):
    manager = ChatSandboxManager(ContainerBackend(image="registry/agent:1", runtime=KATA_RUNTIME))

    class Controller:
        def __init__(self):
            self.pid = 91
            self.size = 0
            self.starts = 0
            self.restart_choices: list[bool] = []

        def snapshot(self, **_kwargs):
            return GuestObserverSnapshot(
                status=GuestObserverStatus(self.pid, 1, 0, 0, 0, self.size),
                journal_size=self.size,
                guest_mono_ns=1,
            )

        def start(self):
            self.starts += 1
            self.pid += 1
            self.size = 0

        def rotate_if_needed(self, *, threshold):
            if self.size < threshold:
                return False
            self.start()
            return True

        def begin(self, *, user_uid, restart_if_missing=True, measurement_id=None):
            self.restart_choices.append(restart_if_missing)
            return GuestObserverCursor(
                uid=user_uid,
                observer_pid=self.pid,
                offset=self.size,
                available=True,
                measurement_id=measurement_id,
            )

        def finish(self, _cursor):
            return GuestObservation(
                source="kata-guest-ebpf-v1",
                complete=True,
                lost_events=0,
                truncated=False,
            )

    controller = Controller()
    monkeypatch.setattr(manager, "_guest_observer_controller", lambda _spec: controller)

    first = manager.begin_guest_observation(_spec(), user_uid=MEMBER_UID_BASE)
    controller.size = GUEST_OBSERVER_ROTATE_BYTES
    second = manager.begin_guest_observation(_spec(), user_uid=MEMBER_UID_BASE + 1)
    assert controller.starts == 0

    manager.finish_guest_observation(_spec(), first)
    manager.finish_guest_observation(_spec(), second)
    third = manager.begin_guest_observation(_spec(), user_uid=MEMBER_UID_BASE)

    assert controller.starts == 1
    assert controller.restart_choices == [True, False, True]
    assert third.observer_pid == 92


def test_retained_tool_marks_are_read_as_the_shared_state_owner(monkeypatch):
    """Capabilityless guest root cannot traverse the 2770 agent-state tree."""
    manager = ChatSandboxManager(ContainerBackend(image="img"))
    calls: list[dict[str, object]] = []

    def exec_capture(_spec, argv, **kwargs):
        calls.append({"argv": list(argv), **kwargs})
        stdout = b"0\n" if len(calls) == 1 else b"100.0 pre toolu_1\n"
        return SimpleNamespace(returncode=0, stdout=stdout, stderr=b"")

    monkeypatch.setattr(manager, "exec_capture", exec_capture)

    reader = manager.tool_mark_reader(_spec())
    assert [mark.tool_use_id for mark in reader.read()] == ["toolu_1"]
    assert [(call["user_uid"], call["user_gid"]) for call in calls] == [
        (manager.backend.uid, manager.backend.gid),
        (manager.backend.uid, manager.backend.gid),
    ]


def test_kata_never_reads_the_agent_writable_tool_mark_file(monkeypatch):
    manager = ChatSandboxManager(ContainerBackend(image="img", runtime=KATA_RUNTIME))
    monkeypatch.setattr(
        manager,
        "exec_capture",
        lambda *_args, **_kwargs: pytest.fail("Kata mark file must not be read"),
    )

    assert manager.tool_mark_reader(_spec()).read() == []


def test_retained_tool_hooks_are_installed_inside_docker_owned_state(monkeypatch, tmp_path):
    manager = ChatSandboxManager(ContainerBackend(image="img"))
    captured: dict[str, object] = {}

    def exec_capture(_spec, argv, **kwargs):
        captured["argv"] = list(argv)
        captured["input"] = kwargs["input_bytes"]
        captured["uid"] = kwargs["user_uid"]
        return SimpleNamespace(returncode=0, stdout=b"", stderr=b"")

    monkeypatch.setattr(manager, "exec_capture", exec_capture)
    update = {"hooks": {"PreToolUse": [{"hooks": []}]}}

    manager.install_tool_hooks(_spec(), update, slot_id=0, thread_id="athr_01")

    argv = captured["argv"]
    assert isinstance(argv, list)
    compile(argv[2], "<retained-tool-hooks>", "exec")
    assert argv[-2:] == [claude_member_config_dir(0, "athr_01"), "/agent-state/tool-marks.log"]
    assert json.loads(captured["input"]) == update
    assert captured["uid"] == MEMBER_UID_BASE
    assert "fcntl.flock" in argv[2]
    assert "os.O_APPEND" in argv[2]

    config = tmp_path / "member-home" / ".claude"
    config.mkdir(parents=True)
    (config / "settings.json").write_text(json.dumps({"model": "keep-me"}))
    marks = tmp_path / "agent-state" / "tool-marks.log"
    marks.parent.mkdir()
    subprocess.run(
        ["python3", "-c", argv[2], str(config), str(marks)],
        input=captured["input"],
        check=True,
    )
    installed = json.loads((config / "settings.json").read_text())
    assert installed["model"] == "keep-me"
    assert installed["hooks"] == update["hooks"]
    assert config.stat().st_mode & 0o777 == 0o700
    assert (config / "settings.json").stat().st_mode & 0o777 == 0o600
    assert (config / ".freeinference-hooks.lock").stat().st_mode & 0o777 == 0o600
    assert marks.exists()
    assert marks.stat().st_mode & 0o777 == 0o660


def test_foreign_namespace_is_not_inspected_during_sandbox_manager_startup(monkeypatch) -> None:
    manager = ChatSandboxManager(ContainerBackend(image="img", sandbox_namespace="this-stack"))

    def run(argv, **_kwargs):
        if argv[1:3] == ["container", "ls"]:
            return SimpleNamespace(
                returncode=0,
                stdout=json.dumps({"id": "foreign-container", "namespace": "sibling-stack"}),
                stderr="",
            )
        if argv[1:3] == ["container", "inspect"]:
            raise AssertionError("foreign inspect would hang on a wedged Kata shim")
        raise AssertionError(argv)

    monkeypatch.setattr(manager, "_run", run)

    assert manager.list() == []


@pytest.mark.parametrize("namespace", ["this-stack", ""])
@pytest.mark.parametrize("inspect_fails", [False, True])
def test_listing_keeps_owned_containers_and_leaves_unlabelled_ones(
    monkeypatch, namespace, inspect_fails
):
    """Ownership is the namespace label; an unlabelled container is not adopted.

    Containers without one predate the label and the single-network change,
    and are left for the one-time cleanup rather than inspected and claimed.
    """
    manager = ChatSandboxManager(ContainerBackend(image="img", sandbox_namespace="this-stack"))
    spec = _spec()
    labels = spec.docker_labels()
    if namespace:
        labels[CHAT_NAMESPACE_LABEL] = namespace

    def run(argv, **_kwargs):
        if argv[1:3] == ["container", "ls"]:
            return SimpleNamespace(
                returncode=0,
                stdout=json.dumps({"id": "owned-container", "namespace": namespace}),
                stderr="",
            )
        assert argv[1:] == ["container", "inspect", "owned-container"]
        if inspect_fails:
            raise SandboxError("owned inspection failed")
        return SimpleNamespace(
            returncode=0,
            stdout=_inspect_payload(spec, labels=labels, networks=("agent-net",)),
            stderr="",
        )

    monkeypatch.setattr(manager, "_run", run)
    if not namespace:
        assert manager.list() == []
    elif inspect_fails:
        with pytest.raises(SandboxError, match="owned inspection failed"):
            manager.list()
    else:
        assert len(manager.list()) == 1


def test_retained_storage_preflight_reads_daemon_root_without_mounting_it(monkeypatch):
    manager = ChatSandboxManager(ContainerBackend(image="img"))
    calls: list[list[str]] = []

    def fake_run(argv, **_kwargs):
        calls.append(list(argv))
        return SimpleNamespace(returncode=0, stdout="/mnt/agent-docker\n", stderr="")

    monkeypatch.setattr("cloud_agent_host.chat_sandbox.subprocess.run", fake_run)

    assert manager.require_docker_data_root("/mnt/agent-docker") == "/mnt/agent-docker"
    assert calls == [["docker", "info", "--format", "{{.DockerRootDir}}"]]

    with pytest.raises(SandboxError, match="expected retained storage"):
        manager.require_docker_data_root("/var/lib/docker")


def test_container_name_is_stable_but_does_not_embed_thread_or_sandbox_id():
    first = _spec().container_name
    second = _spec().container_name

    assert first == second
    assert first.startswith("freeinference-chat-")
    assert "athr_01" not in first
    assert "asbx_01" not in first


def test_unsafe_legacy_extra_args_cannot_add_a_host_mount():
    manager = ChatSandboxManager(
        ContainerBackend(
            image="registry/agent:1",
            extra_args=["--mount=type=bind,source=/host,target=/workspace"],
        )
    )

    with pytest.raises(SandboxError, match="unsafe for retained chats"):
        manager.build_create_command(_spec())


@pytest.mark.parametrize(
    "args",
    [
        ["--cpus", "16"],
        ["--cpu-quota=-1"],
        ["--memory=32g"],
        ["-m", "32g"],
        ["-m32g"],
        ["-c1024"],
        ["--memory-swap=-1"],
        ["--storage-opt", "size=100G"],
        ["-eTOKEN=secret"],
        ["-v/host:/workspace"],
    ],
)
def test_extra_args_cannot_override_the_configured_resource_limits(args):
    manager = ChatSandboxManager(ContainerBackend(image="img", extra_args=args))

    with pytest.raises(SandboxError, match="unsafe for retained chats"):
        manager.build_create_command(_spec())


@pytest.mark.parametrize(
    ("args", "option"),
    [
        # Short and combined spellings of a refused long option.
        (["-u", "0"], "--user"),
        (["-u0"], "--user"),
        (["-itu0"], "--user"),
        (["-lorg.freeinference.chat-namespace=other"], "--label"),
        (["-p", "8080:80"], "--publish"),
        (["-P"], "--publish-all"),
        # Docker's hidden alias.
        (["--net", "host"], "--network"),
        (["--net=host"], "--network"),
        # Options that undo the profile, the state, the network or a limit.
        (["--security-opt", "seccomp=unconfined"], "--security-opt"),
        (["--security-opt=no-new-privileges=false"], "--security-opt"),
        (["--tmpfs", "/workspace"], "--tmpfs"),
        (["--read-only"], "--read-only"),
        (["--runtime", "runc"], "--runtime"),
        (["--userns=host"], "--userns"),
        (["--cgroupns=host"], "--cgroupns"),
        (["--sysctl", "net.ipv4.ip_forward=1"], "--sysctl"),
        (["--group-add", "0"], "--group-add"),
        (["--init=false"], "--init"),
        (["--restart", "always"], "--restart"),
        (["--label-file", "/etc/labels"], "--label-file"),
        (["--dns", "8.8.8.8"], "--dns"),
        (["--add-host", "agent-gateway:203.0.113.9"], "--add-host"),
        (["--pids-limit", "-1"], "--pids-limit"),
    ],
)
def test_extra_args_are_refused_in_every_spelling_docker_accepts(args, option):
    """The arguments follow the platform's own, so whichever spelling came last would win."""
    manager = ChatSandboxManager(ContainerBackend(image="img", extra_args=args))

    with pytest.raises(SandboxError, match="unsafe for retained chats") as refused:
        manager.build_create_command(_spec())
    assert repr(option) in str(refused.value)


def test_a_refused_extra_arg_is_named_without_its_value():
    manager = ChatSandboxManager(ContainerBackend(image="img", extra_args=["-vsecret-host-dir:/x"]))

    with pytest.raises(SandboxError, match="'--volume'") as refused:
        manager.build_create_command(_spec())
    assert "secret-host-dir" not in str(refused.value)


def test_extra_args_that_only_tune_the_container_still_apply():
    args = [
        "--ulimit",
        "nofile=1024:2048",
        "--log-opt",
        "max-size=10m",
        "--shm-size=256m",
        "--device-write-bps",
        "/dev/sda:50mb",
        "--blkio-weight",
        "300",
        "--oom-score-adj",
        "-500",
    ]
    manager = ChatSandboxManager(ContainerBackend(image="img", extra_args=args))

    command = manager.build_create_command(_spec())

    entrypoint = command.index("--entrypoint")
    assert command[entrypoint - len(args) : entrypoint] == args


@pytest.mark.parametrize(
    ("tmpfs", "mounts"),
    [
        ({CHAT_WORKDIR: "rw"}, []),
        ({f"{CHAT_STATE_DIR}/": "rw"}, []),
        ({}, [{"Type": "tmpfs", "Destination": f"{CHAT_WORKDIR}/node_modules"}]),
    ],
)
def test_inspect_rejects_a_tmpfs_over_chat_state(monkeypatch, tmpfs, mounts):
    """Docker lists ``--tmpfs`` in HostConfig, not Mounts; either one empties the state."""
    spec = _spec()
    manager = ChatSandboxManager(ContainerBackend(image="img"))
    payload = json.loads(_inspect_payload(spec, mounts=mounts))
    payload[0]["HostConfig"]["Tmpfs"] = {f"{ACTIVATION_TMPFS}": "rw,noexec", **tmpfs}
    monkeypatch.setattr(
        "cloud_agent_host.chat_sandbox.subprocess.run",
        lambda *_args, **_kwargs: SimpleNamespace(
            returncode=0, stdout=json.dumps(payload), stderr=""
        ),
    )

    with pytest.raises(SandboxError, match="not Docker-layer owned"):
        manager.inspect(spec.container_name)


def test_inspect_accepts_the_platforms_own_tmpfs_mounts(monkeypatch):
    spec = _spec()
    manager = ChatSandboxManager(ContainerBackend(image="img"))
    payload = json.loads(_inspect_payload(spec))
    payload[0]["HostConfig"]["Tmpfs"] = {
        ACTIVATION_TMPFS: "rw,noexec",
        f"{MEMBER_TMPFS_ROOT}/0": "rw,noexec",
        GUEST_OBSERVER_ROOT: "rw,noexec",
    }
    monkeypatch.setattr(
        "cloud_agent_host.chat_sandbox.subprocess.run",
        lambda *_args, **_kwargs: SimpleNamespace(
            returncode=0, stdout=json.dumps(payload), stderr=""
        ),
    )

    inspection = manager.inspect(spec.container_name)

    assert inspection is not None
    assert ACTIVATION_TMPFS in inspection.tmpfs


def test_create_refuses_an_image_override_that_would_make_the_pin_a_lie():
    manager = ChatSandboxManager(ContainerBackend(image="registry/agent:latest"))

    with pytest.raises(SandboxError, match="immutable digest"):
        manager.build_create_command(_spec(), image="sha256:different")


def test_inspect_rejects_a_bind_or_volume_even_when_labels_match(monkeypatch):
    spec = _spec()
    manager = ChatSandboxManager(ContainerBackend(image="img"))
    payload = _inspect_payload(
        spec,
        mounts=[{"Type": "bind", "Source": "/host", "Destination": CHAT_WORKDIR}],
    )
    monkeypatch.setattr(
        "cloud_agent_host.chat_sandbox.subprocess.run",
        lambda *_args, **_kwargs: SimpleNamespace(returncode=0, stdout=payload, stderr=""),
    )

    with pytest.raises(SandboxError, match="forbidden bind mount"):
        manager.inspect(spec.container_name)


def test_existing_container_must_have_been_created_from_the_pinned_image(monkeypatch):
    spec = _spec()
    manager = ChatSandboxManager(ContainerBackend(image="img"))
    payload = json.loads(_inspect_payload(spec))
    payload[0]["Config"]["Image"] = "sha256:different"
    monkeypatch.setattr(
        "cloud_agent_host.chat_sandbox.subprocess.run",
        lambda *_args, **_kwargs: SimpleNamespace(
            returncode=0,
            stdout=json.dumps(payload),
            stderr="",
        ),
    )

    with pytest.raises(SandboxError, match="outside this chat"):
        manager.create(spec)


def test_create_start_stop_retains_the_same_container(monkeypatch):
    spec = _spec()
    manager = ChatSandboxManager(ContainerBackend(image="img"))
    state: dict[str, object] = {"exists": False, "status": "created", "running": False}
    calls: list[list[str]] = []

    def fake_run(argv, **_kwargs):
        calls.append(list(argv))
        if argv[1:3] == ["container", "inspect"]:
            if not state["exists"]:
                return SimpleNamespace(
                    returncode=1,
                    stdout="",
                    stderr="Error: No such container",
                )
            return SimpleNamespace(
                returncode=0,
                stdout=_inspect_payload(
                    spec,
                    status=str(state["status"]),
                    running=bool(state["running"]),
                ),
                stderr="",
            )
        if argv[1] == "create":
            state["exists"] = True
            return SimpleNamespace(returncode=0, stdout="sha256:container-one\n", stderr="")
        if argv[1] == "start":
            state.update(status="running", running=True)
            return SimpleNamespace(returncode=0, stdout=spec.container_name, stderr="")
        if argv[1] == "stop":
            state.update(status="exited", running=False)
            return SimpleNamespace(returncode=0, stdout=spec.container_name, stderr="")
        raise AssertionError(argv)

    monkeypatch.setattr("cloud_agent_host.chat_sandbox.subprocess.run", fake_run)

    created = manager.create(spec)
    started = manager.start(spec)
    stopped = manager.stop(spec)

    assert created.container_id == started.container_id == stopped.container_id
    assert created.status is ChatSandboxStatus.CREATED
    assert started.status is ChatSandboxStatus.RUNNING
    assert stopped.status is ChatSandboxStatus.STOPPED
    assert not any(argv[1:3] in (["rm", "--force"], ["container", "rm"]) for argv in calls)


@pytest.mark.parametrize("operation", ["start", "stop"])
def test_lifecycle_command_never_adopts_a_same_name_post_command_replacement(
    monkeypatch,
    operation,
):
    spec = _spec()
    manager = ChatSandboxManager(ContainerBackend(image="img"))
    command_completed = False

    def inspection(container_id: str, *, running: bool) -> str:
        payload = json.loads(
            _inspect_payload(
                spec,
                status="running" if running else "exited",
                running=running,
            )
        )
        payload[0]["Id"] = container_id
        return json.dumps(payload)

    def fake_run(argv, **_kwargs):
        nonlocal command_completed
        if argv[1:3] == ["container", "inspect"]:
            if command_completed:
                return SimpleNamespace(
                    returncode=0,
                    stdout=inspection("sha256:replacement", running=operation == "stop"),
                    stderr="",
                )
            return SimpleNamespace(
                returncode=0,
                stdout=inspection("sha256:container-one", running=operation == "stop"),
                stderr="",
            )
        if argv[1] == operation:
            assert argv[-1] == "sha256:container-one"
            command_completed = True
            return SimpleNamespace(returncode=0, stdout="", stderr="")
        raise AssertionError(argv)

    monkeypatch.setattr("cloud_agent_host.chat_sandbox.subprocess.run", fake_run)

    with pytest.raises(RetainedChatLost, match="identity changed during"):
        getattr(manager, operation)(spec)


def test_an_existing_name_with_other_ownership_is_never_adopted(monkeypatch):
    spec = _spec()
    manager = ChatSandboxManager(ContainerBackend(image="img"))
    wrong = spec.docker_labels()
    wrong[CHAT_THREAD_LABEL] = "athr_other"
    payload = _inspect_payload(spec, labels=wrong)
    monkeypatch.setattr(
        "cloud_agent_host.chat_sandbox.subprocess.run",
        lambda *_args, **_kwargs: SimpleNamespace(returncode=0, stdout=payload, stderr=""),
    )

    with pytest.raises(SandboxError, match="outside this chat/generation"):
        manager.create(spec)


def test_start_refuses_paused_state_instead_of_calling_it_stopped(monkeypatch):
    spec = _spec()
    manager = ChatSandboxManager(ContainerBackend(image="img"))
    payload = _inspect_payload(spec, status="running", running=True, paused=True)
    monkeypatch.setattr(
        "cloud_agent_host.chat_sandbox.subprocess.run",
        lambda *_args, **_kwargs: SimpleNamespace(returncode=0, stdout=payload, stderr=""),
    )

    with pytest.raises(SandboxError, match="paused"):
        manager.stop(spec)


def test_exec_is_bound_to_the_inspected_container_and_workspace(monkeypatch):
    spec = _spec()
    manager = ChatSandboxManager(ContainerBackend(image="img"))
    payload = _inspect_payload(spec, status="running", running=True)
    calls: list[list[str]] = []

    def fake_run(argv, **_kwargs):
        calls.append(list(argv))
        if argv[1:3] == ["container", "inspect"]:
            return SimpleNamespace(returncode=0, stdout=payload, stderr="")
        if argv[1] == "exec":
            return SimpleNamespace(returncode=0, stdout=b"ok\n", stderr=b"")
        raise AssertionError(argv)

    monkeypatch.setattr("cloud_agent_host.chat_sandbox.subprocess.run", fake_run)

    result = manager.exec_capture(
        spec,
        ["git", "status", "--short"],
        env={"SAFE": "1"},
    )

    assert result.stdout == b"ok\n"
    exec_command = calls[-1]
    assert exec_command[:2] == ["docker", "exec"]
    assert exec_command[exec_command.index("--workdir") + 1] == CHAT_WORKDIR
    assert exec_command[-4:] == ["sha256:container-one", "git", "status", "--short"]


_MEMBER_SECRETS = {
    "ANTHROPIC_API_KEY": "sk-member-inference-secret",
    "AGENT_MCP_TOKEN": "mcp-member-relay-secret",
    "GIT_CONFIG_VALUE_0": "Authorization: Bearer git-secret",
}


def test_exec_names_member_credentials_without_putting_values_on_the_client_argv():
    manager = ChatSandboxManager(ContainerBackend(image="img"))
    env = {
        **_MEMBER_SECRETS,
        "PATH": "/sandbox/bin",
        "HOME": "/home/agent-member-0",
        "XDG_CONFIG_HOME": "/agent-state/opencode-members/x/opencode/config",
        "https_proxy": "http://sandbox-proxy",
    }

    command = manager.build_exec_command(_spec(), ["true"], env=env)
    client_env = manager.exec_client_env(env)

    for key, secret in _MEMBER_SECRETS.items():
        assert not any(secret in token for token in command)
        assert command[command.index(key) - 1] == "--env"
        assert client_env[key] == secret
    # The client's own executable lookup, config, and proxy are never
    # replaced by a sandbox value; those names alone keep KEY=VALUE.
    for key in ("PATH", "HOME", "XDG_CONFIG_HOME", "https_proxy"):
        assert f"{key}={env[key]}" in command
        assert client_env.get(key) == os.environ.get(key)


def test_exec_delivers_named_values_through_the_docker_client_environment(monkeypatch, tmp_path):
    """Run a stand-in Docker CLI that resolves ``--env KEY`` as Docker does."""
    seen_argv = tmp_path / "argv.json"
    docker = tmp_path / "docker"
    docker.write_text(
        f"#!{sys.executable}\n"
        "import json, os, sys\n"
        f"open({str(seen_argv)!r}, 'w').write(json.dumps(sys.argv))\n"
        "args = sys.argv[2:]\n"
        "env = {}\n"
        "while args[0].startswith('-'):\n"
        "    flag = args.pop(0)\n"
        "    if flag == '--env':\n"
        "        key, sep, value = args.pop(0).partition('=')\n"
        "        if not sep:\n"
        "            value = os.environ[key]\n"
        "        env[key] = value\n"
        "    elif flag in ('--user', '--workdir'):\n"
        "        args.pop(0)\n"
        "args.pop(0)\n"
        "os.execve(args[0], args, env)\n"
    )
    docker.chmod(0o755)
    spec = _spec()
    manager = ChatSandboxManager(ContainerBackend(image="img", docker_binary=str(docker)))
    current = ChatSandboxInspection(
        container_id="sha256:container-one",
        name=spec.container_name,
        status=ChatSandboxStatus.RUNNING,
        running=True,
        image="img",
        labels=spec.docker_labels(),
        mounts=(),
        networks=("agent-net",),
    )
    monkeypatch.setattr(manager, "_require", lambda _spec: current)

    result = manager.exec_capture(
        spec,
        [sys.executable, "-c", "import json, os; print(json.dumps(dict(os.environ)))"],
        env={**_MEMBER_SECRETS, "PATH": "/sandbox/bin"},
    )

    assert result.returncode == 0, result.stderr
    delivered = json.loads(result.stdout)
    assert {key: delivered[key] for key in _MEMBER_SECRETS} == _MEMBER_SECRETS
    assert delivered["PATH"] == "/sandbox/bin"
    argv = json.loads(seen_argv.read_text())
    assert not any(secret in token for token in argv for secret in _MEMBER_SECRETS.values())


def _archive_export_failure(monkeypatch, detail: str) -> tuple[ChatSandboxManager, ChatSandboxSpec]:
    spec = _spec()
    manager = ChatSandboxManager(ContainerBackend(image="img"))
    current = ChatSandboxInspection(
        container_id="sha256:container-one",
        name=spec.container_name,
        status=ChatSandboxStatus.STOPPED,
        running=False,
        image="img",
        labels=spec.docker_labels(),
        mounts=(),
        networks=("agent-net",),
    )
    monkeypatch.setattr(manager, "_require", lambda _spec: current)
    real_popen = subprocess.Popen

    def fake_popen(_argv, **kwargs):
        return real_popen(
            [
                sys.executable,
                "-c",
                f"import sys; sys.stderr.write({detail!r}); raise SystemExit(1)",
            ],
            **kwargs,
        )

    monkeypatch.setattr(chat_sandbox_mod.subprocess, "Popen", fake_popen)
    return manager, spec


def test_export_archive_normalizes_the_exact_missing_source_path(monkeypatch):
    path = "/home/agent/.freeinference-platform"
    detail = (
        "Error response from daemon: Could not find the file "
        f"{path} in container sha256:container-one"
    )
    manager, spec = _archive_export_failure(monkeypatch, detail)

    with pytest.raises(FileNotFoundError, match="freeinference-platform"):
        manager.export_archive(
            spec,
            path,
            io.BytesIO(),
            max_bytes=1024,
            expected_container_id="sha256:container-one",
        )


@pytest.mark.parametrize("prefix", ["", "Error response from daemon: "])
def test_optional_stopped_final_marker_accepts_exact_docker_path_absence(monkeypatch, prefix):
    path = f"{INITIAL_ARCHIVE_MARKER_ROOT}/{FINAL_ARCHIVE_MARKER_NAME}"
    detail = f"{prefix}Could not find the file {path} in container sha256:container-one"
    manager, spec = _archive_export_failure(monkeypatch, detail)

    assert (
        manager.read_stopped_final_archive_marker_optional(
            spec,
            expected_container_id="sha256:container-one",
        )
        is None
    )
    with pytest.raises(FileNotFoundError):
        manager.read_final_archive_marker(spec, expected_container_id="sha256:container-one")


@pytest.mark.parametrize(
    "detail",
    [
        "Error response from daemon: No such container: sha256:container-one",
        "Error response from daemon: permission denied",
        (
            "Error response from daemon: Could not find the file /workspace in container "
            "sha256:container-one"
        ),
        (
            "Error response from daemon: Could not find the file "
            f"{INITIAL_ARCHIVE_MARKER_ROOT}/{FINAL_ARCHIVE_MARKER_NAME} "
            "in container sha256:another-container"
        ),
    ],
)
def test_optional_stopped_final_marker_keeps_near_miss_daemon_errors_fatal(monkeypatch, detail):
    manager, spec = _archive_export_failure(monkeypatch, detail)

    with pytest.raises(SandboxError, match="archive export failed"):
        manager.read_stopped_final_archive_marker_optional(
            spec,
            expected_container_id="sha256:container-one",
        )


@pytest.mark.parametrize(
    "failure",
    [
        FileNotFoundError("docker"),
        FileNotFoundError(2, "No such file or directory", "/tmp/archive-marker-copy"),
    ],
)
def test_optional_stopped_final_marker_preserves_unrelated_missing_files(monkeypatch, failure):
    manager, spec = _archive_export_failure(monkeypatch, "")

    def export(*_args, **_kwargs):
        raise failure

    monkeypatch.setattr(manager, "export_archive", export)

    with pytest.raises(FileNotFoundError) as caught:
        manager.read_stopped_final_archive_marker_optional(
            spec,
            expected_container_id="sha256:container-one",
        )
    assert caught.value is failure


@pytest.mark.parametrize(
    "detail",
    [
        "Error response from daemon: No such container: sha256:container-one",
        "Error response from daemon: permission denied",
        (
            "Error response from daemon: Could not find the file /workspace in container "
            "sha256:container-one"
        ),
        (
            "Error response from daemon: Could not find the file "
            "/home/agent/.freeinference-platform in container sha256:another-container"
        ),
    ],
)
def test_export_archive_keeps_near_miss_daemon_errors_fatal(monkeypatch, detail):
    manager, spec = _archive_export_failure(monkeypatch, detail)

    with pytest.raises(SandboxError, match="archive export failed"):
        manager.export_archive(
            spec,
            "/home/agent/.freeinference-platform",
            io.BytesIO(),
            max_bytes=1024,
            expected_container_id="sha256:container-one",
        )


def test_member_process_wrapper_preserves_the_cli_argv(monkeypatch):
    spec = ChatSandboxSpec(
        thread_id="athr_shared",
        sandbox_id="asbx_shared",
        generation=1,
        runtime_id="claude-code",
        shared_live=True,
        resident_slot_capacity=2,
    )
    manager = ChatSandboxManager(ContainerBackend(image="img"))
    payload = _inspect_payload(spec, status="running", running=True)
    popen_calls: list[tuple[list[str], dict]] = []

    class Process:
        stdin = None
        stdout = None
        stderr = None

        def poll(self):
            return None

    monkeypatch.setattr(
        "cloud_agent_host.chat_sandbox.subprocess.run",
        lambda *_args, **_kwargs: SimpleNamespace(returncode=0, stdout=payload, stderr=""),
    )

    def fake_popen(argv, **kwargs):
        popen_calls.append((list(argv), kwargs))
        return Process()

    monkeypatch.setattr("cloud_agent_host.chat_sandbox.subprocess.Popen", fake_popen)

    pidfile = f"{MEMBER_TMPFS_ROOT}/0/resident-{'a' * 32}.pid"
    manager.spawn(
        spec,
        ["claude", "--model", "local-demo-model"],
        live_input=True,
        user_uid=MEMBER_UID_BASE,
        user_gid=manager.backend.gid,
        process_pidfile=pidfile,
    )

    command, _kwargs = popen_calls[0]
    wrapper_at = command.index("/bin/sh")
    wrapper = command[wrapper_at + 2]
    assert 'started=$(set -- $fields; printf "%s" "${20:-}")' in wrapper
    assert 'exec "$@"' in wrapper
    assert "shift 20" not in wrapper
    assert command[-4:] == [pidfile, "claude", "--model", "local-demo-model"]


@pytest.mark.parametrize("name", registered_runtimes())
def test_every_runtime_credential_reaches_a_resident_exec_by_name_only(monkeypatch, name):
    """Each runtime's live grant and MCP token stay off the resident client's argv.

    A resident exec lives for the whole residency, and every host user can read
    its argv. This drives each runtime's real live environment through
    ``spawn``, so a credential handed over under a name the Docker client reads
    for itself, which therefore still travels by value, fails here.
    """
    grant, token = "agr.grant-sentinel", "mcp-token-sentinel"
    runtime = get_runtime(name)
    arguments = {
        "workdir": CHAT_WORKDIR,
        "model": "glm-5.1",
        "gateway_base_url": "http://agent-gateway:8080",
        "credential": grant,
        "provides_isolation": True,
        "mcp_config": RuntimeMCPConfig(
            server_ids=("server",), base_url="http://agent-mcp:8000", credential=token
        ),
        "session_state_dir": CHAT_STATE_DIR,
        "activation_dir": f"{ACTIVATION_TMPFS}/{name}",
    }
    accepted = inspect.signature(runtime.prepare_live).parameters
    _argv, env = runtime.prepare_live(
        **{key: value for key, value in arguments.items() if key in accepted}
    )
    carrying = {key for key, value in env.items() if grant in value or token in value}
    assert carrying, f"{name} handed over no credential at all"
    spec = ChatSandboxSpec(
        thread_id="athr_shared",
        sandbox_id="asbx_shared",
        generation=1,
        runtime_id=name,
        shared_live=True,
        resident_slot_capacity=2,
    )
    manager = ChatSandboxManager(ContainerBackend(image="img"))
    payload = _inspect_payload(spec, status="running", running=True)
    popen_calls: list[tuple[list[str], dict]] = []

    class Process:
        stdin = None
        stdout = None
        stderr = None

        def poll(self):
            return None

    monkeypatch.setattr(
        "cloud_agent_host.chat_sandbox.subprocess.run",
        lambda *_args, **_kwargs: SimpleNamespace(returncode=0, stdout=payload, stderr=""),
    )

    def fake_popen(argv, **kwargs):
        popen_calls.append((list(argv), kwargs))
        return Process()

    monkeypatch.setattr("cloud_agent_host.chat_sandbox.subprocess.Popen", fake_popen)

    manager.spawn(
        spec,
        ["agent-cli"],
        env=env,
        live_input=True,
        user_uid=MEMBER_UID_BASE,
        user_gid=manager.backend.gid,
        process_pidfile=f"{MEMBER_TMPFS_ROOT}/0/resident-{'a' * 32}.pid",
    )

    command, kwargs = popen_calls[0]
    assert not any(grant in part or token in part for part in command)
    for key in carrying:
        assert command[command.index(key) - 1] == "--env"
        assert kwargs["env"][key] == env[key]


def test_member_permission_normalization_prunes_platform_and_sibling_provider_homes(
    monkeypatch,
):
    spec = ChatSandboxSpec(
        thread_id="athr_shared",
        sandbox_id="asbx_shared",
        generation=1,
        runtime_id="claude-code",
        shared_live=True,
        resident_slot_capacity=2,
    )
    manager = ChatSandboxManager(ContainerBackend(image="img"))
    calls: list[tuple[list[str], dict]] = []

    def fake_exec(_spec, argv, **kwargs):
        calls.append((list(argv), kwargs))
        return SimpleNamespace(returncode=0, stdout=b"", stderr=b"")

    monkeypatch.setattr(manager, "exec_capture", fake_exec)

    manager.normalize_member_files(
        spec,
        slot_id=0,
        user_uid=MEMBER_UID_BASE,
        user_gid=manager.backend.gid,
    )

    script = calls[0][0][2]
    assert script.count("-path /agent-state/.platform") == 2
    assert script.count("-path /agent-state/claude ") == 2
    assert "normalize_dirs /agent-state/claude/projects" in script
    assert "normalize_files /agent-state/claude/projects" in script
    assert "/home/agent-member-" not in script
    assert script.count("-path /agent-state/codex-members") == 2
    assert script.count("-path /agent-state/opencode-members") == 2
    assert script.count("-path /agent-state/kilo-members") == 2
    # The trusted collections hold other members' 0700 homes; a traversal
    # into them would fail on the first one it cannot open.
    for runtime_id in ("codex", "opencode", "kilo"):
        assert script.count(f"-path /agent-state/{runtime_id}-homes ") == 2
    # A member's provider home is never widened, not even by its owner: the
    # pass no longer enters the collections at all.
    assert "-members/" not in script
    assert "-homes/" not in script
    assert '"$member"' not in script
    assert calls[0][1]["user_uid"] == MEMBER_UID_BASE


def _running(spec: ChatSandboxSpec) -> ChatSandboxInspection:
    return ChatSandboxInspection(
        container_id="sha256:container-one",
        name=spec.container_name,
        status=ChatSandboxStatus.RUNNING,
        running=True,
        image="img",
        labels=spec.docker_labels(),
        mounts=(),
        networks=("agent-net",),
    )


def test_a_member_pipe_joins_two_member_execs_without_a_shared_file(monkeypatch, tmp_path):
    """One UID's stdout reaches another UID's stdin through this host only."""
    seen = tmp_path / "execs.jsonl"
    docker = tmp_path / "docker"
    docker.write_text(
        f"#!{sys.executable}\n"
        "import json, os, sys\n"
        f"open({str(seen)!r}, 'a').write(json.dumps(sys.argv[1:]) + '\\n')\n"
        "args = sys.argv[2:]\n"
        "while args[0].startswith('-'):\n"
        "    if args.pop(0) in ('--user', '--workdir', '--env'):\n"
        "        args.pop(0)\n"
        "args.pop(0)\n"
        "os.execvp(args[0], args)\n"
    )
    docker.chmod(0o755)
    spec = replace(_spec(), shared_live=True, resident_slot_capacity=4)
    manager = ChatSandboxManager(ContainerBackend(image="img", docker_binary=str(docker)))
    monkeypatch.setattr(manager, "_require", lambda _spec: _running(spec))
    received = tmp_path / "received"

    source, target = manager.pipe_member_exec(
        spec,
        source_argv=["/bin/sh", "-c", "printf 'private state'; echo note >&2"],
        source_uid=MEMBER_UID_BASE,
        target_argv=["/bin/sh", "-c", 'cat > "$1"', "sh", str(received)],
        target_uid=MEMBER_UID_BASE + 2,
        user_gid=manager.backend.gid,
        timeout_s=30,
    )

    assert (source.returncode, target.returncode) == (0, 0)
    assert source.stderr == b"note\n"
    assert received.read_text() == "private state"
    # Both execs start before either finishes, so the order they were logged
    # in is not the order they were spawned in; tell them apart by user.
    execs = {
        argv[argv.index("--user") + 1]: argv
        for argv in (json.loads(line) for line in seen.read_text().splitlines())
    }
    assert set(execs) == {f"{MEMBER_UID_BASE}:10001", f"{MEMBER_UID_BASE + 2}:10001"}
    source_exec = execs[f"{MEMBER_UID_BASE}:10001"]
    target_exec = execs[f"{MEMBER_UID_BASE + 2}:10001"]
    assert "--interactive" not in source_exec and "--interactive" in target_exec
    assert source_exec[source_exec.index("--workdir") + 2] == "sha256:container-one"


def test_a_failed_member_pipe_reports_each_side(monkeypatch, tmp_path):
    docker = tmp_path / "docker"
    docker.write_text(
        f"#!{sys.executable}\n"
        "import os, sys\n"
        "args = sys.argv[2:]\n"
        "while args[0].startswith('-'):\n"
        "    if args.pop(0) in ('--user', '--workdir', '--env'):\n"
        "        args.pop(0)\n"
        "args.pop(0)\n"
        "os.execvp(args[0], args)\n"
    )
    docker.chmod(0o755)
    spec = replace(_spec(), shared_live=True, resident_slot_capacity=4)
    manager = ChatSandboxManager(ContainerBackend(image="img", docker_binary=str(docker)))
    monkeypatch.setattr(manager, "_require", lambda _spec: _running(spec))

    source, target = manager.pipe_member_exec(
        spec,
        source_argv=["/bin/sh", "-c", "yes"],
        source_uid=MEMBER_UID_BASE,
        target_argv=["/bin/sh", "-c", "echo refused >&2; exit 3"],
        target_uid=MEMBER_UID_BASE + 1,
        user_gid=manager.backend.gid,
        timeout_s=30,
    )

    assert target.returncode == 3 and target.stderr == b"refused\n"
    assert source.returncode != 0, "a closed pipe ends the source instead of hanging"


def test_unsettled_reset_has_each_member_clear_its_private_homes_first(monkeypatch):
    spec = replace(_spec(), shared_live=True, resident_slot_capacity=4)
    manager = ChatSandboxManager(ContainerBackend(image="img"))
    calls: list[tuple[list[str], dict]] = []
    answers = iter(
        [
            SimpleNamespace(returncode=75, stdout=b"11001\n11003\n", stderr=b""),
            SimpleNamespace(returncode=0, stdout=b"", stderr=b""),
            SimpleNamespace(returncode=0, stdout=b"", stderr=b""),
            SimpleNamespace(returncode=0, stdout=b"", stderr=b""),
        ]
    )

    def fake_exec(_spec, argv, **kwargs):
        calls.append((list(argv), kwargs))
        return next(answers)

    monkeypatch.setattr(manager, "exec_capture", fake_exec)

    manager.reset_unsettled_state(spec)

    assert [kwargs.get("user_uid") for _argv, kwargs in calls] == [None, 11001, 11003, None]
    assert calls[0][0] == calls[3][0], "the roots are cleared only once no member home is left"
    assert "-members" in calls[1][0][2] and '-uid "$uid"' in calls[1][0][2]
    assert "/agent-state/codex-homes" in calls[0][0][2] and "-homes" in calls[1][0][2]


def test_private_member_collections_are_secured_as_the_sandbox_user(monkeypatch):
    spec = replace(_spec(), shared_live=True, resident_slot_capacity=4)
    manager = ChatSandboxManager(ContainerBackend(image="img"))
    calls: list[tuple[list[str], dict]] = []

    def fake_exec(_spec, argv, **kwargs):
        calls.append((list(argv), kwargs))
        return SimpleNamespace(returncode=0, stdout=b"", stderr=b"")

    monkeypatch.setattr(manager, "exec_capture", fake_exec)

    manager.ensure_private_member_collections(spec)

    ((argv, kwargs),) = calls
    # No user override: the sandbox user owns /agent-state and is no member.
    assert kwargs.get("user_uid") is None and kwargs.get("user_gid") is None
    assert argv[4:] == [
        CHAT_STATE_DIR,
        "/agent-state/codex-homes",
        "/agent-state/opencode-homes",
        "/agent-state/kilo-homes",
    ]
    assert 'chmod +t -- "$root"' in argv[2] and "mkdir -m 3770" in argv[2]


@pytest.mark.parametrize("owners", [b"0\n", b"10001\n", b"11005\n", b"not-a-uid\n"])
def test_unsettled_reset_refuses_a_home_no_member_slot_owns(monkeypatch, owners):
    spec = replace(_spec(), shared_live=True, resident_slot_capacity=4)
    manager = ChatSandboxManager(ContainerBackend(image="img"))
    calls: list[dict] = []

    def fake_exec(_spec, _argv, **kwargs):
        calls.append(kwargs)
        return SimpleNamespace(returncode=75, stdout=owners, stderr=b"")

    monkeypatch.setattr(manager, "exec_capture", fake_exec)

    with pytest.raises(SandboxError, match="private member home"):
        manager.reset_unsettled_state(spec)
    assert len(calls) == 1


def test_member_replacement_reaps_pidfile_group_and_every_same_uid_leftover(monkeypatch):
    spec = ChatSandboxSpec(
        thread_id="athr_shared",
        sandbox_id="asbx_shared",
        generation=1,
        runtime_id="claude-code",
        shared_live=True,
        resident_slot_capacity=2,
    )
    manager = ChatSandboxManager(ContainerBackend(image="img"))
    calls: list[tuple[list[str], dict]] = []

    def fake_exec(_spec, argv, **kwargs):
        calls.append((list(argv), kwargs))
        return SimpleNamespace(returncode=0, stdout=b"", stderr=b"")

    monkeypatch.setattr(manager, "exec_capture", fake_exec)

    manager.reap_member_processes(
        spec,
        slot_id=0,
        user_uid=MEMBER_UID_BASE,
        user_gid=manager.backend.gid,
    )

    assert len(calls) == 3
    assert "stat -c %u:%g:%a" in calls[0][0][2]
    assert calls[1][0] == [
        "/usr/local/bin/member-process-reaper",
        "reap",
        str(MEMBER_UID_BASE),
    ]
    assert calls[1][1]["input_bytes"] == b""
    assert calls[1][1]["user_uid"] == MEMBER_UID_BASE
    assert "resident-*.pid" in calls[2][0][2]


def test_member_turn_census_is_pid_reuse_fenced_and_canonical(monkeypatch):
    spec = ChatSandboxSpec(
        thread_id="athr_shared",
        sandbox_id="asbx_shared",
        generation=1,
        runtime_id="claude-code",
        shared_live=True,
        resident_slot_capacity=2,
    )
    manager = ChatSandboxManager(ContainerBackend(image="img"))
    calls: list[tuple[list[str], dict]] = []

    def fake_exec(_spec, argv, **kwargs):
        calls.append((list(argv), kwargs))
        if kwargs.get("input_bytes") is None:
            return SimpleNamespace(returncode=0, stdout=b"42 500\n41 400\n", stderr=b"")
        return SimpleNamespace(returncode=0, stdout=b"", stderr=b"")

    monkeypatch.setattr(manager, "exec_capture", fake_exec)

    baseline = manager.capture_member_processes(
        spec,
        slot_id=0,
        user_uid=MEMBER_UID_BASE,
        user_gid=manager.backend.gid,
    )
    manager.reap_member_processes_except(
        spec,
        slot_id=0,
        user_uid=MEMBER_UID_BASE,
        user_gid=manager.backend.gid,
        baseline=baseline,
    )

    assert baseline == ((41, 400), (42, 500))
    assert calls[3][1]["input_bytes"] == b"41 400\n42 500\n"
    assert calls[1][0] == [
        "/usr/local/bin/member-process-reaper",
        "capture",
        str(MEMBER_UID_BASE),
    ]
    assert calls[3][0] == [
        "/usr/local/bin/member-process-reaper",
        "reap",
        str(MEMBER_UID_BASE),
    ]

    with pytest.raises(SandboxError, match="malformed"):
        manager._parse_member_processes(b"41 nope\n")


def test_pre_upgrade_container_receives_the_root_owned_process_reaper(monkeypatch, tmp_path):
    spec = _spec()
    manager = ChatSandboxManager(ContainerBackend(image="img"))
    source = tmp_path / "member-process-reaper"
    source.write_text("#!/usr/bin/env python3\n")
    monkeypatch.setenv("AGENT_MEMBER_PROCESS_REAPER_SOURCE", str(source))
    monkeypatch.setattr(
        manager,
        "_require",
        lambda _spec: ChatSandboxInspection(
            container_id="old-container-id",
            name=spec.container_name,
            status=ChatSandboxStatus.RUNNING,
            running=True,
            image="old-image",
            labels=spec.docker_labels(),
            mounts=(),
            networks=("agent-net",),
        ),
    )
    exec_calls: list[tuple[list[str], dict]] = []
    docker_calls: list[list[str]] = []

    def fake_exec(_spec, argv, **kwargs):
        exec_calls.append((list(argv), kwargs))
        return SimpleNamespace(
            returncode=1 if len(exec_calls) == 1 else 0,
            stdout=b"",
            stderr=b"",
        )

    def fake_run(argv, **_kwargs):
        docker_calls.append(list(argv))
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(manager, "exec_capture", fake_exec)
    monkeypatch.setattr(manager, "_run", fake_run)

    manager.ensure_member_process_reaper(spec)

    assert len(docker_calls) == 1
    assert docker_calls[0][:3] == ["docker", "cp", str(source)]
    assert docker_calls[0][3].startswith(
        "old-container-id:/usr/local/bin/member-process-reaper.install-"
    )
    assert "chown 0:0" in exec_calls[1][0][2]
    assert "chmod 0555" in exec_calls[1][0][2]
    assert exec_calls[1][1]["user_uid"] == 0


def test_shared_writer_freeze_runs_as_each_owner_without_cap_kill(monkeypatch):
    spec = ChatSandboxSpec(
        thread_id="athr_shared",
        sandbox_id="asbx_shared",
        generation=1,
        runtime_id="claude-code",
        shared_live=True,
        resident_slot_capacity=3,
    )
    manager = ChatSandboxManager(ContainerBackend(image="img"))
    monkeypatch.setattr(manager, "_operation_writer_uids", lambda _spec: [])
    calls: list[tuple[list[str], dict]] = []
    receipts = {
        manager.backend.uid: b"31 10001 300\n",
        MEMBER_UID_BASE: b"41 11001 400\n",
        MEMBER_UID_BASE + 1: b"51 11002 500\n",
    }

    def fake_exec(_spec, argv, **kwargs):
        calls.append((list(argv), kwargs))
        if kwargs.get("input_bytes") is not None:
            return SimpleNamespace(returncode=0, stdout=b"", stderr=b"")
        return SimpleNamespace(
            returncode=0,
            stdout=receipts[kwargs["user_uid"]],
            stderr=b"",
        )

    monkeypatch.setattr(manager, "exec_capture", fake_exec)

    manifest = manager.freeze_shared_writers(
        spec,
        slot_ids=[1, 0, 1],
        token="a" * 32,
    )

    assert manifest == f"{ACTIVATION_TMPFS}/snapshot-{'a' * 32}.pids"
    assert [call[1]["user_uid"] for call in calls] == [10001, 11001, 11002, 10001]
    assert all(call[1]["user_uid"] != 0 for call in calls)
    assert calls[-1][1]["input_bytes"] == (b"31 10001 300\n41 11001 400\n51 11002 500\n")
    census_script = calls[0][0][2]
    assert '[ "$pid" != "$$" ]' in census_script
    assert "kill -STOP" in census_script
    assert "kill -CONT" in census_script


def test_shared_writer_freeze_replacement_runs_exact_id_cleanup(monkeypatch):
    spec = ChatSandboxSpec(
        thread_id="athr_shared",
        sandbox_id="asbx_shared",
        generation=1,
        runtime_id="claude-code",
        shared_live=True,
        resident_slot_capacity=2,
    )
    manager = ChatSandboxManager(ContainerBackend(image="img"))
    monkeypatch.setattr(manager, "_operation_writer_uids", lambda _spec: [])
    exact_id = "sha256:original"
    exact_checks = 0
    calls: list[tuple[list[str], dict]] = []

    def require_exact(_spec, container_id):
        nonlocal exact_checks
        assert container_id == exact_id
        exact_checks += 1
        if exact_checks == 2:
            raise RetainedChatLost("same-name replacement")
        return SimpleNamespace(container_id=exact_id, running=True)

    def fake_exec(_spec, argv, **kwargs):
        calls.append((list(argv), kwargs))
        if kwargs.get("input_bytes") is not None:
            return SimpleNamespace(returncode=0, stdout=b"", stderr=b"")
        if 'expected_uid="$1"; committed=0' in argv[2]:
            uid = kwargs["user_uid"]
            return SimpleNamespace(
                returncode=0,
                stdout=f"{31 if uid == manager.backend.uid else 41} {uid} 300\n".encode(),
                stderr=b"",
            )
        return SimpleNamespace(returncode=0, stdout=b"", stderr=b"")

    monkeypatch.setattr(manager, "_require_exact", require_exact)
    monkeypatch.setattr(manager, "exec_capture", fake_exec)

    with pytest.raises(RetainedChatLost, match="replacement"):
        manager.freeze_shared_writers(
            spec,
            slot_ids=[0],
            token="c" * 32,
            expected_container_id=exact_id,
        )

    assert exact_checks == 2
    assert all(call[1]["expected_container_id"] == exact_id for call in calls)
    assert len(calls) == 5
    assert all("kill -CONT" in call[0][2] for call in calls[-2:])


def test_shared_writer_thaw_reads_manifest_and_resumes_as_each_owner(monkeypatch):
    spec = ChatSandboxSpec(
        thread_id="athr_shared",
        sandbox_id="asbx_shared",
        generation=1,
        runtime_id="claude-code",
        shared_live=True,
        resident_slot_capacity=2,
    )
    manager = ChatSandboxManager(ContainerBackend(image="img"))
    monkeypatch.setattr(manager, "_operation_writer_uids", lambda _spec: [])
    calls: list[tuple[list[str], dict]] = []
    manifest = f"{ACTIVATION_TMPFS}/snapshot-{'b' * 32}.pids"

    def fake_exec(_spec, argv, **kwargs):
        calls.append((list(argv), kwargs))
        if argv[2] == 'cat -- "$1"':
            return SimpleNamespace(
                returncode=0,
                stdout=b"31 10001 300\n41 11001 400\n",
                stderr=b"",
            )
        return SimpleNamespace(returncode=0, stdout=b"", stderr=b"")

    monkeypatch.setattr(manager, "exec_capture", fake_exec)

    manager.thaw_shared_writers(spec, slot_ids=[0], manifest=manifest)

    assert [call[1]["user_uid"] for call in calls] == [10001, 10001, 11001, 10001]
    assert all(call[1]["user_uid"] != 0 for call in calls)
    assert calls[1][0][-1] == "31:300"
    assert calls[2][0][-1] == "41:400"
    assert "kill -CONT" in calls[1][0][2]
    assert calls[-1][0][2] == 'rm -f -- "$1"'


@pytest.mark.parametrize("dead_state", ["Z", "X", "x"])
@pytest.mark.parametrize("has_stopped_worker", [False, True])
def test_shared_writer_census_rejects_exiting_nonleaders_even_with_matching_counts(
    tmp_path, dead_state, has_stopped_worker
):
    # An exiting nonleader can be replaced while /proc is read, preserving
    # both thread counts but hiding a live writer from the observed task list.
    proc = tmp_path / "100"
    states = {100: "Z", 101: dead_state}
    if has_stopped_worker:
        states[102] = "T"
    for tid, state in states.items():
        task = proc / "task" / str(tid)
        task.mkdir(parents=True)
        (task / "stat").write_text(f"{tid} (writer) {state}\n")
    (proc / "status").write_text(f"Threads:\t{len(states)}\n")

    result = subprocess.run(
        [
            "/bin/sh",
            "-c",
            ChatSandboxManager._shared_writer_task_state_script() + 'writer_state "$1"',
            "sh",
            str(proc),
        ],
        capture_output=True,
        check=True,
        timeout=5,
    )

    assert result.stdout == b"unknown"


def _local_shared_writer_command(argv, tmp_path, owned_processes):
    """Confine a generated script's census traversal to owned PIDs."""
    command = list(argv)
    targets = " ".join(f"/proc/{process.pid}" for process in owned_processes)
    assert targets
    command[2] = command[2].replace("/proc/[0-9]*", targets)
    assert "/proc/[0-9]*" not in command[2]
    return [argument.replace(ACTIVATION_TMPFS, str(tmp_path)) for argument in command]


def _local_shared_writer_manager(monkeypatch, tmp_path, owned_processes):
    """Run the generated scripts with census traversal confined to owned PIDs."""
    manager = ChatSandboxManager(ContainerBackend(image="img", uid=os.getuid(), gid=os.getgid()))

    def local_exec(_spec, argv, **kwargs):
        assert kwargs["user_uid"] == os.getuid()
        return subprocess.run(
            _local_shared_writer_command(argv, tmp_path, owned_processes),
            input=kwargs.get("input_bytes"),
            capture_output=True,
            timeout=5,
            check=False,
        )

    monkeypatch.setattr(manager, "_operation_writer_uids", lambda _spec: [])
    monkeypatch.setattr(manager, "exec_capture", local_exec)
    return manager


@pytest.fixture
def owned_shared_writer_processes():
    processes = []
    yield processes
    for process in processes:
        process.kill()
        process.wait(timeout=5)


def _shared_writer_process_state(pid):
    return Path(f"/proc/{pid}/stat").read_text().rsplit(") ", 1)[1].split()[0]


@pytest.fixture
def zombie_leader_writer_binary(tmp_path):
    compiler = shutil.which("cc")
    if compiler is None:
        pytest.skip("pthread writer fixture requires a C compiler")
    source = tmp_path / "zombie-leader-writer.c"
    source.write_text(
        """
#include <pthread.h>
#include <stdio.h>
#include <unistd.h>

static void *writer(void *path) {
    FILE *stream = fopen(path, "w");
    if (!stream) return NULL;
    for (;;) {
        fputs("x", stream);
        fflush(stream);
        usleep(10000);
    }
}

int main(int argc, char **argv) {
    pthread_t thread;
    if (argc != 2 || pthread_create(&thread, NULL, writer, argv[1])) return 1;
    pthread_exit(NULL);
}
"""
    )
    executable = tmp_path / "zombie-leader-writer"
    subprocess.run(
        [compiler, "-pthread", str(source), "-o", str(executable)],
        capture_output=True,
        check=True,
        timeout=5,
    )
    return executable


def _spawn_zombie_leader_writer(executable, writes_path, owned_processes):
    process = subprocess.Popen([str(executable), str(writes_path)])
    owned_processes.append(process)
    deadline = time.monotonic() + 2
    while time.monotonic() < deadline:
        if (
            _shared_writer_process_state(process.pid) == "Z"
            and writes_path.exists()
            and writes_path.stat().st_size > 0
        ):
            return process
        time.sleep(0.01)
    pytest.fail("pthread fixture did not reach a zombie leader with a live writer")


def _wait_for_shared_writer_growth(writes_path, previous_size):
    deadline = time.monotonic() + 2
    while time.monotonic() < deadline:
        if writes_path.stat().st_size > previous_size:
            return
        time.sleep(0.01)
    pytest.fail("the live writer did not resume")


@pytest.mark.skipif(sys.platform != "linux", reason="writer census uses Linux /proc")
def test_shared_writer_freeze_and_exact_thaw_ignore_an_owned_zombie(
    monkeypatch, tmp_path, owned_shared_writer_processes
):
    zombie = subprocess.Popen([sys.executable, "-c", "pass"])
    owned_shared_writer_processes.append(zombie)
    os.waitid(os.P_PID, zombie.pid, os.WEXITED | os.WNOWAIT)
    assert _shared_writer_process_state(zombie.pid) == "Z"
    manager = _local_shared_writer_manager(monkeypatch, tmp_path, owned_shared_writer_processes)

    manifest = manager.freeze_shared_writers(_spec(), slot_ids=[], token="c" * 32)

    assert (tmp_path / Path(manifest).name).read_bytes() == b""
    manager.thaw_shared_writers(_spec(), slot_ids=[], manifest=manifest)
    assert _shared_writer_process_state(zombie.pid) == "Z"


@pytest.mark.skipif(sys.platform != "linux", reason="writer census uses Linux /proc")
def test_shared_writer_freeze_and_thaw_keep_live_writers_fenced_beside_zombies(
    monkeypatch, tmp_path, owned_shared_writer_processes
):
    zombie = subprocess.Popen([sys.executable, "-c", "pass"])
    writer = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    owned_shared_writer_processes.extend([zombie, writer])
    os.waitid(os.P_PID, zombie.pid, os.WEXITED | os.WNOWAIT)
    manager = _local_shared_writer_manager(monkeypatch, tmp_path, owned_shared_writer_processes)

    manifest = manager.freeze_shared_writers(_spec(), slot_ids=[], token="d" * 32)

    receipt = manager._parse_frozen_processes(
        (tmp_path / Path(manifest).name).read_bytes(), allowed_uids={os.getuid()}
    )
    assert [(pid, uid) for pid, uid, _started in receipt] == [(writer.pid, os.getuid())]
    assert _shared_writer_process_state(writer.pid) == "T"
    manager.thaw_shared_writers(_spec(), slot_ids=[], manifest=manifest)
    assert _shared_writer_process_state(writer.pid) not in {"T", "t"}
    assert _shared_writer_process_state(zombie.pid) == "Z"
    assert list(tmp_path.glob("snapshot-*")) == []


@pytest.mark.skipif(sys.platform != "linux", reason="writer census uses Linux /proc")
def test_shared_writer_exact_thaw_still_rejects_a_new_live_writer(
    monkeypatch, tmp_path, owned_shared_writer_processes
):
    writer = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    owned_shared_writer_processes.append(writer)
    manager = _local_shared_writer_manager(monkeypatch, tmp_path, owned_shared_writer_processes)
    manifest = manager.freeze_shared_writers(_spec(), slot_ids=[], token="e" * 32)
    new_writer = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    owned_shared_writer_processes.append(new_writer)

    with pytest.raises(SandboxError, match="writer census changed"):
        manager.thaw_shared_writers(_spec(), slot_ids=[], manifest=manifest)

    assert _shared_writer_process_state(writer.pid) not in {"T", "t"}
    assert not (tmp_path / Path(manifest).name).exists()


@pytest.mark.skipif(sys.platform != "linux", reason="writer census uses Linux /proc")
def test_shared_writer_freeze_and_thaw_fence_live_threads_behind_a_zombie_leader(
    monkeypatch, tmp_path, owned_shared_writer_processes, zombie_leader_writer_binary
):
    writes_path = tmp_path / "thread-writes"
    writer = _spawn_zombie_leader_writer(
        zombie_leader_writer_binary, writes_path, owned_shared_writer_processes
    )
    manager = _local_shared_writer_manager(monkeypatch, tmp_path, owned_shared_writer_processes)

    manifest = manager.freeze_shared_writers(_spec(), slot_ids=[], token="f" * 32)

    receipt = manager._parse_frozen_processes(
        (tmp_path / Path(manifest).name).read_bytes(), allowed_uids={os.getuid()}
    )
    assert [(pid, uid) for pid, uid, _started in receipt] == [(writer.pid, os.getuid())]
    task_states = {
        task_stat.read_text().rsplit(") ", 1)[1].split()[0]
        for task_stat in Path(f"/proc/{writer.pid}/task").glob("*/stat")
    }
    assert task_states == {"Z", "T"}
    frozen_size = writes_path.stat().st_size
    time.sleep(0.05)
    assert writes_path.stat().st_size == frozen_size

    manager.thaw_shared_writers(_spec(), slot_ids=[], manifest=manifest)

    _wait_for_shared_writer_growth(writes_path, frozen_size)
    assert _shared_writer_process_state(writer.pid) == "Z"


@pytest.mark.skipif(sys.platform != "linux", reason="writer census uses Linux /proc")
def test_shared_writer_exact_thaw_rejects_a_new_zombie_leader_with_live_threads(
    monkeypatch, tmp_path, owned_shared_writer_processes, zombie_leader_writer_binary
):
    writer = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    owned_shared_writer_processes.append(writer)
    manager = _local_shared_writer_manager(monkeypatch, tmp_path, owned_shared_writer_processes)
    manifest = manager.freeze_shared_writers(_spec(), slot_ids=[], token="1" * 32)
    _spawn_zombie_leader_writer(
        zombie_leader_writer_binary, tmp_path / "new-thread-writes", owned_shared_writer_processes
    )

    with pytest.raises(SandboxError, match="writer census changed"):
        manager.thaw_shared_writers(_spec(), slot_ids=[], manifest=manifest)

    assert _shared_writer_process_state(writer.pid) not in {"T", "t"}
    assert not (tmp_path / Path(manifest).name).exists()


@pytest.mark.skipif(sys.platform != "linux", reason="writer census uses Linux /proc")
@pytest.mark.parametrize("failure_phase", ["freeze", "manifest"])
def test_shared_writer_freeze_failure_resumes_live_threads_behind_a_zombie_leader(
    monkeypatch,
    tmp_path,
    owned_shared_writer_processes,
    zombie_leader_writer_binary,
    failure_phase,
):
    writes_path = tmp_path / "thread-writes"
    _spawn_zombie_leader_writer(
        zombie_leader_writer_binary, writes_path, owned_shared_writer_processes
    )
    manager = _local_shared_writer_manager(monkeypatch, tmp_path, owned_shared_writer_processes)
    local_exec = manager.exec_capture

    def failing_exec(spec, argv, **kwargs):
        if failure_phase == "freeze" and "resume_receipt()" in argv[2]:
            argv = list(argv)
            argv[2] = argv[2].replace("committed=1", "exit 45")
        if failure_phase == "manifest" and kwargs.get("input_bytes") is not None:
            return SimpleNamespace(returncode=1, stdout=b"", stderr=b"injected manifest failure")
        return local_exec(spec, argv, **kwargs)

    monkeypatch.setattr(manager, "exec_capture", failing_exec)

    with pytest.raises(SandboxError, match=f"writer {failure_phase} failed"):
        manager.freeze_shared_writers(_spec(), slot_ids=[], token="2" * 32)

    _wait_for_shared_writer_growth(writes_path, writes_path.stat().st_size)


def _after_census_stops_a_new_writer(argv, command):
    """Run ``command`` in the guest census right after it stops an unseen writer."""
    anchor = 'receipt="$receipt $item"; stable=0;;'
    assert argv[2].count(anchor) == 1
    script = argv[2].replace(anchor, f'receipt="$receipt $item"; stable=0; {command};;')
    return [*argv[:2], script, *argv[3:]]


def _wait_for_shared_writer_state(pid, state):
    # A stop signal takes effect once its target next runs, not when it is sent.
    deadline = time.monotonic() + 2
    while _shared_writer_process_state(pid) != state:
        assert time.monotonic() < deadline, f"process {pid} did not reach state {state}"
        time.sleep(0.01)


@pytest.mark.skipif(sys.platform != "linux", reason="writer census uses Linux /proc")
@pytest.mark.parametrize("census_end", ["committed", "killed"])
def test_shared_writer_freeze_failure_resumes_a_census_whose_reply_was_lost(
    monkeypatch, tmp_path, owned_shared_writer_processes, census_end
):
    # The guest census has stopped the writer, then either committed its
    # receipt or died before its trap could run; the host never sees a reply.
    writer = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    owned_shared_writer_processes.append(writer)
    manager = _local_shared_writer_manager(monkeypatch, tmp_path, owned_shared_writer_processes)
    local_exec = manager.exec_capture

    def lose_census_reply(spec, argv, **kwargs):
        if "resume_receipt()" not in argv[2]:
            return local_exec(spec, argv, **kwargs)
        if census_end == "killed":
            argv = _after_census_stops_a_new_writer(argv, "kill -KILL $$")
        census = local_exec(spec, argv, **kwargs)
        assert census.returncode == (0 if census_end == "committed" else -signal.SIGKILL)
        _wait_for_shared_writer_state(writer.pid, "T")
        raise SandboxError("chat sandbox exec failed: reply lost")

    monkeypatch.setattr(manager, "exec_capture", lose_census_reply)

    with pytest.raises(SandboxError, match="reply lost"):
        manager.freeze_shared_writers(_spec(), slot_ids=[], token="3" * 32)

    assert _shared_writer_process_state(writer.pid) not in {"T", "t"}
    assert list(tmp_path.glob("snapshot-*")) == []


@pytest.mark.skipif(sys.platform != "linux", reason="writer census uses Linux /proc")
def test_shared_writer_freeze_failure_kills_a_census_still_running_before_resuming(
    monkeypatch, tmp_path, owned_shared_writer_processes
):
    writer = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    owned_shared_writer_processes.append(writer)
    manager = _local_shared_writer_manager(monkeypatch, tmp_path, owned_shared_writer_processes)
    local_exec = manager.exec_capture
    censuses = []

    def abandon_census(spec, argv, **kwargs):
        if "resume_receipt()" not in argv[2]:
            return local_exec(spec, argv, **kwargs)
        # The guest census is descheduled mid-scan, and the host gives up on it.
        argv = _after_census_stops_a_new_writer(argv, "kill -STOP $$")
        census = subprocess.Popen(
            _local_shared_writer_command(argv, tmp_path, owned_shared_writer_processes),
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        censuses.append(census)
        owned_shared_writer_processes.append(census)
        _wait_for_shared_writer_state(census.pid, "T")
        _wait_for_shared_writer_state(writer.pid, "T")
        raise SandboxError("chat sandbox exec failed: connection reset")

    monkeypatch.setattr(manager, "exec_capture", abandon_census)

    with pytest.raises(SandboxError, match="connection reset"):
        manager.freeze_shared_writers(_spec(), slot_ids=[], token="4" * 32)

    (census,) = censuses
    # Scheduled again, a census left alive would stop the writer once more.
    os.kill(census.pid, signal.SIGCONT)
    assert census.wait(timeout=5) == -signal.SIGKILL
    assert _shared_writer_process_state(writer.pid) not in {"T", "t"}


@pytest.mark.skipif(sys.platform != "linux", reason="writer census uses Linux /proc")
def test_shared_writer_census_past_its_deadline_resumes_through_its_own_trap(
    monkeypatch, tmp_path, owned_shared_writer_processes
):
    writer = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    owned_shared_writer_processes.append(writer)
    manager = _local_shared_writer_manager(monkeypatch, tmp_path, owned_shared_writer_processes)
    local_exec = manager.exec_capture
    monkeypatch.setattr(chat_sandbox_mod, "_SHARED_WRITER_CENSUS_BUDGET_S", 0)
    censuses = []

    def census_only(spec, argv, **kwargs):
        # Nothing the host sends after the census reaches the guest.
        if "resume_receipt()" not in argv[2]:
            raise SandboxError("chat sandbox exec failed: connection refused")
        censuses.append(local_exec(spec, argv, **kwargs))
        return censuses[-1]

    monkeypatch.setattr(manager, "exec_capture", census_only)

    with pytest.raises(SandboxError, match="writer freeze failed"):
        manager.freeze_shared_writers(_spec(), slot_ids=[], token="5" * 32)

    assert [census.returncode for census in censuses] == [47]
    assert _shared_writer_process_state(writer.pid) not in {"T", "t"}


def test_shared_writer_thaw_detects_replacement_after_exact_id_cleanup(monkeypatch):
    spec = ChatSandboxSpec(
        thread_id="athr_shared",
        sandbox_id="asbx_shared",
        generation=1,
        runtime_id="claude-code",
        shared_live=True,
        resident_slot_capacity=2,
    )
    manager = ChatSandboxManager(ContainerBackend(image="img"))
    monkeypatch.setattr(manager, "_operation_writer_uids", lambda _spec: [])
    exact_id = "sha256:original"
    exact_checks = 0
    calls: list[tuple[list[str], dict]] = []
    manifest = f"{ACTIVATION_TMPFS}/snapshot-{'d' * 32}.pids"

    def require_exact(_spec, container_id):
        nonlocal exact_checks
        assert container_id == exact_id
        exact_checks += 1
        if exact_checks == 2:
            raise RetainedChatLost("same-name replacement")
        return SimpleNamespace(container_id=exact_id, running=True)

    def fake_exec(_spec, argv, **kwargs):
        calls.append((list(argv), kwargs))
        if argv[2] == 'cat -- "$1"':
            return SimpleNamespace(
                returncode=0,
                stdout=b"31 10001 300\n41 11001 400\n",
                stderr=b"",
            )
        return SimpleNamespace(returncode=0, stdout=b"", stderr=b"")

    monkeypatch.setattr(manager, "_require_exact", require_exact)
    monkeypatch.setattr(manager, "exec_capture", fake_exec)

    with pytest.raises(RetainedChatLost, match="replacement"):
        manager.thaw_shared_writers(
            spec,
            slot_ids=[0],
            manifest=manifest,
            expected_container_id=exact_id,
        )

    assert exact_checks == 2
    assert all(call[1]["expected_container_id"] == exact_id for call in calls)
    assert len(calls) == 4
    assert "kill -CONT" in calls[1][0][2]
    assert "kill -CONT" in calls[2][0][2]
    assert calls[-1][0][2] == 'rm -f -- "$1"'


@pytest.mark.parametrize(
    "payload",
    [
        b"1 10001 300\n",
        b"31 0 300\n",
        b"31 10001 0\n",
        b"31 10001 300 extra\n",
        b"31 10001 300\n31 10001 301\n",
        "31 10001 300\N{SNOWMAN}\n".encode(),
    ],
)
def test_shared_writer_manifest_rejects_values_outside_the_identity_fence(payload):
    with pytest.raises(SandboxError, match="snapshot writer census"):
        ChatSandboxManager._parse_frozen_processes(payload, allowed_uids={10001})


def test_retained_terminal_keeps_the_shell_in_the_docker_tty_process_group(
    monkeypatch,
):
    spec = ChatSandboxSpec(
        thread_id="athr_01",
        sandbox_id="asbx_01",
        generation=3,
        runtime_id="claude-code",
    )
    manager = ChatSandboxManager(ContainerBackend(image="img"))
    payload = _inspect_payload(spec, status="running", running=True)
    popen_calls: list[tuple[list[str], dict]] = []

    class Process:
        stdout = None

        def poll(self):
            return 0

        def wait(self, timeout=None):
            return 0

    monkeypatch.setattr(
        "cloud_agent_host.chat_sandbox.subprocess.run",
        lambda *_args, **_kwargs: SimpleNamespace(returncode=0, stdout=payload, stderr=""),
    )

    def fake_popen(argv, **kwargs):
        popen_calls.append((list(argv), kwargs))
        return Process()

    monkeypatch.setattr("cloud_agent_host.chat_sandbox.subprocess.Popen", fake_popen)

    terminal = manager.spawn_terminal(spec, rows=31, cols=101)
    assert terminal.wait() == 0
    assert terminal.shell == "bash"

    command, kwargs = popen_calls[0]
    script, _name, pidfile, rcfile, bashrc = command[command.index("-c") + 1 :]
    assert "--interactive" in command
    assert "--tty" in command
    assert 'printf "%s\\n" "$$" > "$pidfile"' in script
    # bash replaces the wrapper, so it leads the group the pidfile names.
    assert 'exec bash --rcfile "$rcfile" -i' in script
    assert "/bin/sh +m -i" in script
    assert "trap '' TSTP TTIN TTOU" in script
    assert "trap : INT QUIT" in script
    assert "umask 0002" in script
    assert "setsid" not in script
    assert " &" not in script
    assert pidfile.startswith(f"{ACTIVATION_TMPFS}/terminal-") and pidfile.endswith(".pid")
    assert rcfile == pidfile.removesuffix(".pid") + ".bashrc"
    assert 'PROMPT_COMMAND="set +m${PROMPT_COMMAND:+;$PROMPT_COMMAND}"' in bashrc
    assert f"trap 'rm -f -- {pidfile}' EXIT" in bashrc
    assert kwargs["start_new_session"] is True
    # The Docker CLI sizes the exec from its stdout, so stdout must be the PTY.
    assert kwargs["stdin"] == kwargs["stdout"] == kwargs["stderr"]
    assert isinstance(kwargs["stdout"], int)


def test_retained_terminal_signal_must_be_confirmed_while_client_is_live():
    spec = _spec()
    calls: list[list[str]] = []

    class Process:
        stdout = None

        def poll(self):
            return None

    class Manager:
        def exec_capture(self, _spec, argv, **_kwargs):
            calls.append(list(argv))
            return SimpleNamespace(returncode=3)

    terminal = _RetainedTerminalProcess(
        Process(),  # type: ignore[arg-type]
        manager=Manager(),  # type: ignore[arg-type]
        spec=spec,
        pidfile=f"{ACTIVATION_TMPFS}/terminal-test.pid",
        input_fd=None,
    )

    with pytest.raises(SandboxError, match="terminal cannot receive STOP"):
        terminal.suspend()

    script = calls[0][2]
    assert "while [ ! -r" in script
    assert "sleep 0.05" in script
    assert "/bin/kill" not in script
    assert "|| true" not in script


def test_stale_terminal_reaper_matches_pidfile_and_wrapper_argv(monkeypatch):
    spec = _spec()
    manager = ChatSandboxManager(ContainerBackend(image="img"))
    payload = _inspect_payload(spec, status="running", running=True)
    calls: list[list[str]] = []

    def fake_run(argv, **_kwargs):
        calls.append(list(argv))
        if argv[1:3] == ["container", "inspect"]:
            return SimpleNamespace(returncode=0, stdout=payload, stderr="")
        if argv[1] == "exec":
            return SimpleNamespace(returncode=0, stdout=b"", stderr=b"")
        raise AssertionError(argv)

    monkeypatch.setattr("cloud_agent_host.chat_sandbox.subprocess.run", fake_run)

    manager.reap_stale_terminal_processes(spec)

    script = calls[-1][-1]
    assert f"{ACTIVATION_TMPFS}/terminal-*.pid" in script
    # The sh wrapper's argv names the pidfile; an exec'd bash names its rcfile.
    assert 'rcfile="${pidfile%.pid}.bashrc"' in script
    assert 'grep -Fqx -e "$pidfile" -e "$rcfile"' in script
    assert 'kill -KILL -"$pid"' in script
    assert "pkill" not in script


def test_unsettled_retry_reset_targets_only_the_two_container_owned_roots(monkeypatch):
    spec = _spec()
    manager = ChatSandboxManager(ContainerBackend(image="img"))
    payload = _inspect_payload(spec, status="running", running=True)
    calls: list[list[str]] = []

    def fake_run(argv, **_kwargs):
        calls.append(list(argv))
        if argv[1:3] == ["container", "inspect"]:
            return SimpleNamespace(returncode=0, stdout=payload, stderr="")
        if argv[1] == "exec":
            return SimpleNamespace(returncode=0, stdout=b"", stderr=b"")
        raise AssertionError(argv)

    monkeypatch.setattr("cloud_agent_host.chat_sandbox.subprocess.run", fake_run)

    manager.reset_unsettled_state(spec)

    command = calls[-1]
    assert command[command.index("--workdir") + 1] == "/"
    script = command[-1]
    assert CHAT_WORKDIR in script
    assert CHAT_STATE_DIR in script
    assert "/host" not in script


def test_base_bundle_import_uses_activation_tmpfs_and_preserves_git_state(monkeypatch):
    spec = _spec()
    manager = ChatSandboxManager(ContainerBackend(image="img"))
    payload = _inspect_payload(spec, status="running", running=True)
    calls: list[tuple[list[str], dict]] = []

    def fake_run(argv, **kwargs):
        calls.append((list(argv), kwargs))
        if argv[1:3] == ["container", "inspect"]:
            return SimpleNamespace(returncode=0, stdout=payload, stderr="")
        if argv[1] == "exec":
            return SimpleNamespace(returncode=0, stdout=b"", stderr=b"")
        raise AssertionError(argv)

    monkeypatch.setattr("cloud_agent_host.chat_sandbox.subprocess.run", fake_run)
    commit = "a" * 40

    manager.import_base_bundle(spec, b"bundle bytes", commit=commit, env={"SAFE": "1"})

    command, kwargs = calls[-1]
    assert kwargs["input"] == b"bundle bytes"
    assert command[command.index("--workdir") + 1] == CHAT_WORKDIR
    assert ACTIVATION_TMPFS in command[-4]
    assert "git bundle unbundle" in command[-4]
    assert "git reset" not in command[-4]
    assert "git checkout" not in command[-4]
    assert command[-3:] == ["sh", commit, f"{commit}^{{commit}}"]


def test_archive_import_streams_a_file_to_docker_cp(monkeypatch, tmp_path):
    spec = _spec()
    manager = ChatSandboxManager(ContainerBackend(image="img"))
    payload = _inspect_payload(spec, status="running", running=True)
    calls: list[tuple[list[str], dict]] = []

    def fake_run(argv, **kwargs):
        calls.append((list(argv), kwargs))
        if argv[1:3] == ["container", "inspect"]:
            return SimpleNamespace(returncode=0, stdout=payload, stderr="")
        if argv[1] == "cp":
            return SimpleNamespace(returncode=0, stdout=b"", stderr=b"")
        raise AssertionError(argv)

    monkeypatch.setattr("cloud_agent_host.chat_sandbox.subprocess.run", fake_run)
    archive_path = tmp_path / "checkout.tar"
    archive_path.write_bytes(b"\0" * 1024)
    # Sparse, so the test asserts the timeout for a 3 GiB archive without
    # writing one.
    with archive_path.open("r+b") as archive:
        archive.truncate(3 << 30)
        manager.import_archive(spec, CHAT_WORKDIR, archive)

        command, kwargs = calls[-1]
        assert command[-2:] == ["-", f"{spec.container_name}:{CHAT_WORKDIR}"]
        assert kwargs["stdin"] is archive
        assert kwargs["input"] is None
        assert kwargs["timeout"] == 3 * ARCHIVE_IMPORT_TIMEOUT_PER_GIB_S

    manager.import_archive(spec, CHAT_WORKDIR, b"small tar")

    _command, kwargs = calls[-1]
    assert kwargs["input"] == b"small tar"
    assert kwargs["stdin"] is None
    assert kwargs["timeout"] == ARCHIVE_IMPORT_TIMEOUT_PER_GIB_S


def test_retained_backend_reports_the_cli_version_pinned_in_its_container(monkeypatch):
    spec = _spec()
    manager = ChatSandboxManager(ContainerBackend(image="img"))
    monkeypatch.setattr(
        manager.backend,
        "version_stamp",
        lambda: {
            "sandbox_image_digest": "sha256:new-runner-image",
            "runtime_versions": {"claude-code": "2.1.999"},
            "runtime_versions_available": {"claude-code": ["2.1.999"]},
        },
    )

    stamp = RetainedChatBackend(manager, spec).version_stamp()

    assert stamp["runtime_versions"]["claude-code"] == "2.1.220"
    assert stamp["runtime_versions_available"]["claude-code"] == ["2.1.220"]
    assert stamp["sandbox_image_digest"] == "sha256:image-pinned"


def _proving_backend(
    monkeypatch, *, proved_at: float | None, failures: list[Exception] | None = None
) -> tuple[RetainedChatBackend, list[object]]:
    """A retained backend whose bridge checks are recorded, and fail if told to."""
    manager = ChatSandboxManager(ContainerBackend(image="img"))
    checks: list[object] = []

    def require_network(_spec: ChatSandboxSpec, *, observer: object = None) -> None:
        checks.append(observer)
        if failures:
            raise failures.pop(0)

    monkeypatch.setattr(manager, "require_network", require_network)
    monkeypatch.setattr(manager, "spawn", lambda *_args, **_kwargs: "process")
    return RetainedChatBackend(manager, _spec(), network_proved_at=proved_at), checks


def _git_probe() -> SandboxSpec:
    return SandboxSpec(argv=["git", "cat-file", "-e", "HEAD"], workdir=CHAT_WORKDIR)


def test_killing_a_bounded_command_signals_only_its_own_process_group(monkeypatch):
    """A setup script, patch build or owner command shares the group's container.

    Killing it on a timeout or an oversized output must end that command's own
    process group, found through its receipt: ``docker stop`` would end every
    sibling member's turn in the same container.
    """
    spec = _spec()
    manager = ChatSandboxManager(ContainerBackend(image="img"))
    payload = _inspect_payload(spec, status="running", running=True)
    docker: list[list[str]] = []
    clients: list[SimpleNamespace] = []

    def fake_run(argv, **_kwargs):
        docker.append(list(argv))
        if argv[1:3] == ["container", "inspect"]:
            return SimpleNamespace(returncode=0, stdout=payload, stderr="")
        return SimpleNamespace(returncode=0, stdout=b"", stderr=b"")

    def fake_popen(argv, **_kwargs):
        docker.append(list(argv))
        client = SimpleNamespace(
            stdin=None, stdout=None, stderr=None, killed=False, poll=lambda: None
        )
        client.kill = lambda: setattr(client, "killed", True)
        clients.append(client)
        return client

    monkeypatch.setattr("cloud_agent_host.chat_sandbox.subprocess.run", fake_run)
    monkeypatch.setattr("cloud_agent_host.chat_sandbox.subprocess.Popen", fake_popen)
    monkeypatch.setattr(manager, "require_network", lambda *_args, **_kwargs: None)
    backend = RetainedChatBackend(manager, spec)

    process = backend.spawn(SandboxSpec(argv=["/bin/sh", "-c", "sleep 30"], workdir=CHAT_WORKDIR))
    process.kill()

    receipt = next(part for part in docker[1] if part.startswith(f"{ACTIVATION_TMPFS}/command-"))
    cleanup = docker[-1]
    assert cleanup[:2] == ["docker", "exec"] and cleanup[-1] == receipt
    assert not any(argv[1] in {"stop", "kill", "rm", "pause", "restart"} for argv in docker)
    assert clients[0].killed is True


def test_a_backend_without_a_proof_checks_the_bridge_before_every_command(monkeypatch):
    """The sandbox manager's own backends, and a turn whose sandbox manager did not say it checked."""
    backend, checks = _proving_backend(monkeypatch, proved_at=None)

    for _ in range(3):
        assert backend.spawn(_git_probe()) == "process"

    assert len(checks) == 3


def test_a_turn_relies_on_its_activation_proof_for_a_minute(monkeypatch):
    """The sandbox manager proved the bridge when it acquired the turn; repeating it adds nothing."""
    clock = {"now": 1000.0}
    monkeypatch.setattr(chat_sandbox_mod, "time", SimpleNamespace(monotonic=lambda: clock["now"]))
    backend, checks = _proving_backend(monkeypatch, proved_at=1000.0)

    clock["now"] = 1000.0 + TURN_NETWORK_PROOF_S - 1
    backend.spawn(_git_probe())
    backend.require_network(observer="agent_network")
    assert checks == []

    # A minute on, as after a long setup script, the turn checks for itself.
    clock["now"] = 1000.0 + TURN_NETWORK_PROOF_S
    backend.require_network(observer="agent_network")
    assert checks == ["agent_network"]

    # That check stands for the next minute in turn.
    clock["now"] += TURN_NETWORK_PROOF_S - 1
    backend.spawn(_git_probe())
    assert checks == ["agent_network"]
    clock["now"] += 1
    backend.spawn(_git_probe())
    assert checks == ["agent_network", None]


def test_a_failed_check_is_no_proof(monkeypatch):
    """Fail closed: nothing runs, and the next command checks again."""
    clock = {"now": 1000.0 + TURN_NETWORK_PROOF_S}
    monkeypatch.setattr(chat_sandbox_mod, "time", SimpleNamespace(monotonic=lambda: clock["now"]))
    backend, checks = _proving_backend(
        monkeypatch,
        proved_at=1000.0,
        failures=[SandboxError("relay endpoint changed during group network reconciliation")],
    )

    with pytest.raises(SandboxError, match="relay endpoint changed"):
        backend.spawn(_git_probe())
    assert backend.spawn(_git_probe()) == "process"
    assert len(checks) == 2


@pytest.mark.parametrize(
    "field,value,message",
    [
        ("thread_id", "", "thread_id"),
        ("sandbox_id", "", "sandbox_id"),
        ("generation", 0, "generation"),
        ("stop_timeout_s", 0, "stop_timeout_s"),
        ("disk_limit_mib", -1, "disk_limit_mib"),
        ("disk_limit_mib", 16777217, "disk_limit_mib"),
        ("disk_limit_mib", True, "disk_limit_mib"),
        ("disk_limit_mib", 1.5, "disk_limit_mib"),
    ],
)
def test_spec_rejects_invalid_identity_or_stop_budget(field, value, message):
    kwargs = {
        "thread_id": "athr_01",
        "sandbox_id": "asbx_01",
        "generation": 1,
        "runtime_id": "claude-code",
        "stop_timeout_s": 10,
    }
    kwargs[field] = value

    with pytest.raises(ValueError, match=message):
        ChatSandboxSpec(**kwargs)


def test_a_peer_that_vanished_mid_reconcile_is_simply_absent(monkeypatch):
    """Docker exits non-zero when one id is missing but still answers for the rest.

    Compose replacement can remove an endpoint between the network inspection
    and this one. The batched call must read that partial answer rather than
    discard it on the exit code — the exact-one check is what turns the race
    into a retryable failure.
    """
    from cloud_agent_shared.egress import network_from_env

    network = network_from_env(
        {
            "AGENT_SANDBOX_NAMESPACE": "default",
            "AGENT_EGRESS_CHAT_NETWORK_POOL": "10.240.0.0/16",
            "AGENT_EGRESS_DNS_SERVER": "1.1.1.1",
            "AGENT_RELAY_NETWORK": "agent-relays",
            "AGENT_GATEWAY_URL": "http://agent-gateway:8080",
        }
    )
    manager = ChatSandboxManager(ContainerBackend(image="img", sandbox_network=network))

    monkeypatch.setattr(
        manager._network_lifecycle,
        "_inspect_network",
        lambda name: {"Name": name, "Containers": {"gw": {}, "gone": {}}},
    )
    monkeypatch.setattr(
        manager,
        "_run",
        lambda argv, **_kwargs: SimpleNamespace(
            returncode=1,
            stdout=json.dumps(
                [
                    {
                        "Id": "gw",
                        "Name": "/gw",
                        "State": {"Running": True},
                        "NetworkSettings": {
                            "Networks": {"agent-relays": {"Aliases": ["agent-gateway"]}}
                        },
                    }
                ]
            ),
            stderr="Error: No such container: gone",
        ),
    )

    assert manager._relay_owners() == {"gw": ["agent-gateway"]}


class TestSandboxActionFacts:
    """The container action is what classifies the startup path."""

    def test_create_outranks_the_start_that_follows_it(self):
        facts = SandboxActionFacts()

        facts.note("create")
        facts.note("reuse")

        assert facts.container_action == "create"

    def test_a_later_action_overwrites_an_earlier_weaker_one(self):
        facts = SandboxActionFacts()

        facts.note("reuse")
        facts.note("start")

        assert facts.container_action == "start"

    def test_an_untouched_activation_reports_unknown(self):
        assert SandboxActionFacts().container_action == "unknown"

    def test_a_real_creation_reports_the_action_the_classifier_reads(self, monkeypatch):
        """The manager's own literal and the runner's classifier must agree.

        These two halves were individually correct and jointly broken: the
        manager noted ``"created"`` while the classifier and the strongest-fact
        guard both tested ``"create"``. So the guard never fired, the ``start``
        that always follows a create overwrote the fact, and every brand-new
        retained group was measured as ``resume_stopped`` -- two of the four
        declared paths collapsed into one. A unit test of the guard cannot see
        that, because it supplies the spelling the classifier wants, so this one
        drives the real ``create`` and hands the result to the real classifier.
        """
        from cloud_agent_host.runner import _classify_startup_path

        spec = _spec()
        manager = ChatSandboxManager(ContainerBackend(image="img"))
        created = ChatSandboxInspection(
            container_id="container-new",
            name=spec.container_name,
            status=ChatSandboxStatus.RUNNING,
            running=True,
            image=spec.sandbox_image_digest or "sha256:image",
            labels=spec.docker_labels(),
            mounts=(),
            networks=("agent-net",),
        )
        # Everything except the container creation itself: the inspect that
        # follows it (absent before the create, present after) and the
        # ownership checks that run on both.
        made = False

        def _inspect(_name):
            return created if made else None

        def _run(*_args, **_kwargs):
            nonlocal made
            made = True
            return SimpleNamespace(returncode=0, stdout="", stderr="")

        monkeypatch.setattr(manager, "inspect", _inspect)
        monkeypatch.setattr(manager, "_verify_identity", lambda *_args: None)
        monkeypatch.setattr(manager, "_run", _run)

        facts = SandboxActionFacts()
        manager.create(spec, facts=facts)
        # The start that follows a creation, which used to overwrite the fact.
        facts.note("start")

        assert facts.container_action == "create", (
            "a container this attempt created must say so in the word the "
            "classifier recognises, not a near-miss spelling"
        )
        assert _classify_startup_path(facts.container_action, "unknown") == "new_group"
