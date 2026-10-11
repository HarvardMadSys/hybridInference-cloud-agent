"""Contract tests for Docker-owned restartable chat containers."""

from __future__ import annotations

import json
import os
import shutil
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
    MEMBER_TMPFS_ROOT,
    MEMBER_UID_BASE,
    TURN_NETWORK_PROOF_S,
    ChatSandboxInspection,
    ChatSandboxManager,
    ChatSandboxSpec,
    ChatSandboxStatus,
    RetainedChatBackend,
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


@pytest.fixture(autouse=True)
def _plain_group_network(monkeypatch):
    """Keep these tests on one fixed bridge; the group network is tested elsewhere.

    Everything here is about exec, terminals, observers and ownership. Their
    containers sit on ``agent-net`` and the group network's own lifecycle
    (creation, relays, host exceptions, release, pruning, the in-guest check)
    is covered against a model of Docker in ``test_chat_group_network.py``.
    """
    monkeypatch.setattr(ChatSandboxManager, "network_for", lambda _self, _spec: "agent-net")
    monkeypatch.setattr(
        ChatSandboxManager, "_ensure_group_network", lambda _self, _spec: "agent-net"
    )
    monkeypatch.setattr(
        ChatSandboxManager,
        "_require_group_network",
        lambda self, spec, _current, *_snapshot: self._require(spec),
    )
    monkeypatch.setattr(ChatSandboxManager, "_sync_kata_guest_network", lambda *_args: None)
    monkeypatch.setattr(ChatSandboxManager, "_verify_guest_network", lambda *_args: None)
    monkeypatch.setattr(
        ChatSandboxManager,
        "_release_sandbox_networks",
        lambda self, spec, _current: self._require(spec),
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


def test_foreign_namespace_is_not_inspected_during_broker_startup(monkeypatch) -> None:
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
    assert exec_command[-4:] == [spec.container_name, "git", "status", "--short"]


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
    assert 'stat -c %u "$member"' in script
    assert 'normalize_dirs "$member"' in script
    assert calls[0][1]["user_uid"] == MEMBER_UID_BASE


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


def _local_shared_writer_manager(monkeypatch, tmp_path, owned_processes):
    """Run the generated scripts with census traversal confined to owned PIDs."""
    manager = ChatSandboxManager(ContainerBackend(image="img", uid=os.getuid(), gid=os.getgid()))

    def local_exec(_spec, argv, **kwargs):
        assert kwargs["user_uid"] == os.getuid()
        command = list(argv)
        targets = " ".join(f"/proc/{process.pid}" for process in owned_processes)
        assert targets
        command[2] = command[2].replace("/proc/[0-9]*", targets)
        assert "/proc/[0-9]*" not in command[2]
        command = [argument.replace(ACTIVATION_TMPFS, str(tmp_path)) for argument in command]
        return subprocess.run(
            command,
            input=kwargs.get("input_bytes"),
            capture_output=True,
            timeout=5,
            check=False,
        )

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


def test_a_backend_without_a_proof_checks_the_bridge_before_every_command(monkeypatch):
    """The broker's own backends, and a turn whose broker did not say it checked."""
    backend, checks = _proving_backend(monkeypatch, proved_at=None)

    for _ in range(3):
        assert backend.spawn(_git_probe()) == "process"

    assert len(checks) == 3


def test_a_turn_relies_on_its_activation_proof_for_a_minute(monkeypatch):
    """The broker proved the bridge when it acquired the turn; repeating it adds nothing."""
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
        manager,
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
        # Everything except the container creation itself: the network, the
        # inspect that follows it (absent before the create, present after),
        # and the ownership checks that run on both.
        made = False

        def _inspect(_name):
            return created if made else None

        def _run(*_args, **_kwargs):
            nonlocal made
            made = True
            return SimpleNamespace(returncode=0, stdout="", stderr="")

        monkeypatch.setattr(manager, "_ensure_group_network", lambda *_args: None)
        monkeypatch.setattr(manager, "inspect", _inspect)
        monkeypatch.setattr(manager, "_require_group_network", lambda *_args: created)
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
