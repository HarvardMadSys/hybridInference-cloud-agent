"""Contract tests for Docker-owned restartable chat containers."""

from __future__ import annotations

import json
import subprocess
from dataclasses import replace
from types import SimpleNamespace

import pytest

from cloud_agent_host.chat_sandbox import (
    ACTIVATION_TMPFS,
    CHAT_BOOTSTRAP_NETWORK,
    CHAT_DNS_UPSTREAM,
    CHAT_IMAGE_DIGEST_LABEL,
    CHAT_NAMESPACE_LABEL,
    CHAT_NETWORK_BASE_LABEL,
    CHAT_NETWORK_INHIBIT_IPV4_OPTION,
    CHAT_NETWORK_LABEL,
    CHAT_SANDBOX_ID_LABEL,
    CHAT_SANDBOX_LABEL,
    CHAT_STATE_DIR,
    CHAT_THREAD_LABEL,
    CHAT_WORKDIR,
    MEMBER_TMPFS_ROOT,
    MEMBER_UID_BASE,
    ChatSandboxInspection,
    ChatSandboxManager,
    ChatSandboxSpec,
    ChatSandboxStatus,
    RetainedChatBackend,
    SandboxActionFacts,
    _RetainedTerminalProcess,
)
from cloud_agent_host.egress import EgressPolicy, EgressTier
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
from cloud_agent_host.sandbox import KATA_RUNTIME, ContainerBackend, SandboxError


def _spec() -> ChatSandboxSpec:
    return ChatSandboxSpec(
        thread_id="athr_01",
        sandbox_id="asbx_01",
        generation=3,
        runtime_id="claude-code",
        runtime_version="2.1.220",
        sandbox_image_digest="sha256:image-pinned",
    )


def _isolated_policy() -> EgressPolicy:
    return EgressPolicy(
        tiers={"setup": EgressTier.TRUSTED, "agent": EgressTier.PLATFORM_ONLY},
        networks={
            EgressTier.TRUSTED: "setup-backplane",
            EgressTier.PLATFORM_ONLY: "agent-backplane",
        },
        proxies={EgressTier.TRUSTED: "http://package-proxy:3128"},
        no_proxy=("agent-gateway", "agent-mcp", "localhost", "127.0.0.1"),
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
    manager = ChatSandboxManager(ContainerBackend(image="registry/agent:1", network="agent-net"))

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
    assert f"--dns {CHAT_DNS_UPSTREAM}" in joined
    assert "--cap-drop ALL" in joined
    assert "--security-opt no-new-privileges" in joined
    assert "--pids-limit 512" in joined
    assert "sleep 2147483647 & child=$!" in command[-1]


def test_kata_create_command_reserves_a_root_only_guest_observer_journal():
    spec = _spec()
    manager = ChatSandboxManager(
        ContainerBackend(
            image="registry/agent:1",
            network="agent-net",
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
    manager = ChatSandboxManager(
        ContainerBackend(image="registry/agent:1", network="agent-net", runtime=KATA_RUNTIME)
    )
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
    manager = ChatSandboxManager(
        ContainerBackend(image="registry/agent:1", network="agent-net", runtime=KATA_RUNTIME)
    )

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
    manager = ChatSandboxManager(ContainerBackend(image="img", network="agent-net"))
    calls: list[dict[str, object]] = []

    def exec_capture(_spec, argv, **kwargs):
        calls.append({"argv": list(argv), **kwargs})
        stdout = b"0\n" if len(calls) == 1 else b"100.0 pre toolu_1\n"
        return SimpleNamespace(returncode=0, stdout=stdout, stderr=b"")

    monkeypatch.setattr(manager, "exec_capture", exec_capture)

    reader = manager.tool_mark_reader(_spec())
    assert [mark[2] for mark in reader.read()] == ["toolu_1"]
    assert [(call["user_uid"], call["user_gid"]) for call in calls] == [
        (manager.backend.uid, manager.backend.gid),
        (manager.backend.uid, manager.backend.gid),
    ]


def test_kata_never_reads_the_agent_writable_tool_mark_file(monkeypatch):
    manager = ChatSandboxManager(
        ContainerBackend(image="img", network="agent-net", runtime=KATA_RUNTIME)
    )
    monkeypatch.setattr(
        manager,
        "exec_capture",
        lambda *_args, **_kwargs: pytest.fail("Kata mark file must not be read"),
    )

    assert manager.tool_mark_reader(_spec()).read() == []


def test_retained_tool_hooks_are_installed_inside_docker_owned_state(monkeypatch, tmp_path):
    manager = ChatSandboxManager(ContainerBackend(image="img", network="agent-net"))
    captured: dict[str, object] = {}

    def exec_capture(_spec, argv, **kwargs):
        captured["argv"] = list(argv)
        captured["input"] = kwargs["input_bytes"]
        return SimpleNamespace(returncode=0, stdout=b"", stderr=b"")

    monkeypatch.setattr(manager, "exec_capture", exec_capture)
    update = {"hooks": {"PreToolUse": [{"hooks": []}]}}

    manager.install_tool_hooks(_spec(), update)

    argv = captured["argv"]
    assert isinstance(argv, list)
    compile(argv[2], "<retained-tool-hooks>", "exec")
    assert argv[-2:] == ["/agent-state/claude", "/agent-state/tool-marks.log"]
    assert json.loads(captured["input"]) == update
    assert "fcntl.flock" in argv[2]
    assert "os.O_APPEND" in argv[2]

    config = tmp_path / "agent-state" / "claude"
    config.mkdir(parents=True)
    (config / "settings.json").write_text(json.dumps({"model": "keep-me"}))
    marks = tmp_path / "agent-state" / "tool-marks.log"
    subprocess.run(
        ["python3", "-c", argv[2], str(config), str(marks)],
        input=captured["input"],
        check=True,
    )
    installed = json.loads((config / "settings.json").read_text())
    assert installed["model"] == "keep-me"
    assert installed["hooks"] == update["hooks"]
    assert marks.exists()
    assert marks.stat().st_mode & 0o777 == 0o660


def test_closed_egress_uses_a_distinct_private_network_for_each_chat():
    manager = ChatSandboxManager(
        ContainerBackend(image="registry/agent:1", network="unused", egress=_isolated_policy())
    )
    first = _spec()
    second = ChatSandboxSpec(
        thread_id="athr_02",
        sandbox_id="asbx_02",
        generation=1,
        runtime_id="claude-code",
    )

    first_agent = manager.network_for_phase(first, "agent")
    first_setup = manager.network_for_phase(first, "setup")

    assert first_agent.startswith("freeinference-chat-net-")
    assert first_agent != "agent-backplane"
    assert first_setup != "setup-backplane"
    assert first_agent != first_setup
    assert first_agent != manager.network_for_phase(second, "agent")
    assert first_agent != manager.network_for_phase(replace(first, generation=4), "agent")
    command = manager.build_create_command(first, network_phase="agent")
    assert command[command.index("--network") + 1] == CHAT_BOOTSTRAP_NETWORK


def test_trusted_agent_phase_copies_both_platform_relays_and_the_proxy():
    policy = _isolated_policy()
    policy.tiers["agent"] = EgressTier.TRUSTED
    manager = ChatSandboxManager(ContainerBackend(image="img", network="unused", egress=policy))

    assert manager._service_aliases_for_phase("agent") == (
        "agent-gateway",
        "agent-mcp",
        "package-proxy",
    )


def test_trusted_setup_phase_copies_only_the_registry_proxy():
    policy = _isolated_policy()
    manager = ChatSandboxManager(ContainerBackend(image="img", network="unused", egress=policy))

    assert manager._service_aliases_for_phase("setup") == ("package-proxy",)


def test_first_activation_replaces_the_dormant_bootstrap_bridge_before_start(monkeypatch):
    spec = _spec()
    manager = ChatSandboxManager(
        ContainerBackend(image="img", network="unused", egress=_isolated_policy())
    )
    expected = manager.network_for_phase(spec, "setup")
    current = ChatSandboxInspection(
        container_id="sandbox-container",
        name=spec.container_name,
        status=ChatSandboxStatus.CREATED,
        running=False,
        image=spec.sandbox_image_digest or "img",
        labels=spec.docker_labels(),
        mounts=(),
        networks=(CHAT_BOOTSTRAP_NETWORK,),
    )
    calls: list[list[str]] = []

    def run(argv, **_kwargs):
        nonlocal current
        calls.append(list(argv))
        if argv[1:3] == ["network", "connect"]:
            current = replace(
                current,
                networks=tuple(sorted({*current.networks, expected})),
            )
        elif argv[1:3] == ["network", "disconnect"]:
            current = replace(current, networks=(expected,))
        else:
            raise AssertionError(argv)
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(manager, "_ensure_phase_network", lambda *_args: expected)
    monkeypatch.setattr(manager, "_require", lambda _spec: current)
    monkeypatch.setattr(manager, "_run", run)

    moved = manager._require_phase_network(spec, current, "setup")

    assert moved.networks == (expected,)
    assert ["docker", "network", "connect", expected, current.container_id] in calls
    assert [
        "docker",
        "network",
        "disconnect",
        "--force",
        CHAT_BOOTSTRAP_NETWORK,
        current.container_id,
    ] in calls


def test_daemon_wide_discovery_never_adopts_a_sibling_stack() -> None:
    manager = ChatSandboxManager(
        ContainerBackend(
            image="img",
            network="unused",
            egress=_isolated_policy(),
            sandbox_namespace="staging-host-stack",
        )
    )
    spec = _spec()

    def inspection(namespace: str | None, networks: tuple[str, ...]) -> ChatSandboxInspection:
        labels = spec.docker_labels()
        if namespace is not None:
            labels[CHAT_NAMESPACE_LABEL] = namespace
        return ChatSandboxInspection(
            container_id="container",
            name=spec.container_name,
            status=ChatSandboxStatus.STOPPED,
            running=False,
            image=spec.sandbox_image_digest or "img",
            labels=labels,
            mounts=(),
            networks=networks,
        )

    assert manager._belongs_to_namespace(inspection("staging-host-stack", ()))
    assert not manager._belongs_to_namespace(
        inspection("production-host-stack", ("agent-backplane",))
    )
    # The pre-namespace rollout can safely adopt only a container still
    # attached to this stack's configured discovery backplane.
    assert manager._belongs_to_namespace(inspection(None, ("agent-backplane",)))
    assert not manager._belongs_to_namespace(inspection(None, ()))


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
def test_listing_keeps_owned_and_legacy_containers_and_their_failures(
    monkeypatch, namespace, inspect_fails
):
    manager = ChatSandboxManager(
        ContainerBackend(image="img", network="agent-net", sandbox_namespace="this-stack")
    )
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
    if inspect_fails:
        with pytest.raises(SandboxError, match="owned inspection failed"):
            manager.list()
    else:
        assert len(manager.list()) == 1


def test_private_network_contains_only_the_discovered_fixed_services(monkeypatch):
    spec = _spec()
    manager = ChatSandboxManager(
        ContainerBackend(image="img", network="unused", egress=_isolated_policy())
    )
    expected = manager.network_for_phase(spec, "agent")
    created = False
    connected: list[tuple[str, str, list[str]]] = []

    def inspect_network(name):
        if name == expected and created:
            return {
                "Name": expected,
                "Driver": "bridge",
                "Scope": "local",
                "Internal": True,
                "Options": {CHAT_NETWORK_INHIBIT_IPV4_OPTION: "true"},
                "Labels": manager._network_labels(spec, "agent"),
                "IPAM": {"Config": [{"Subnet": "10.240.0.0/29"}]},
                "Containers": {
                    "gateway-container": {},
                    "mcp-container": {},
                },
            }
        if name == expected:
            return None
        raise AssertionError(name)

    def run(argv, **_kwargs):
        nonlocal created
        if argv[1:3] == ["network", "create"]:
            assert "--internal" in argv
            assert f"{CHAT_NETWORK_INHIBIT_IPV4_OPTION}=true" in argv
            assert f"{CHAT_NETWORK_LABEL}=true" in argv
            assert f"{CHAT_NETWORK_BASE_LABEL}=agent-backplane" in argv
            assert argv[argv.index("--subnet") + 1].endswith("/29")
            created = True
            return SimpleNamespace(returncode=0, stdout=f"{expected}\n", stderr="")
        if argv[1:3] == ["container", "inspect"]:
            return SimpleNamespace(returncode=1, stdout="", stderr="No such container")
        raise AssertionError(argv)

    monkeypatch.setattr(manager, "_inspect_network", inspect_network)
    monkeypatch.setattr(
        manager,
        "_service_owners",
        lambda base, aliases: {
            "gateway-container": ["agent-gateway"],
            "mcp-container": ["agent-mcp"],
        },
    )
    monkeypatch.setattr(
        manager,
        "_ensure_service_connection",
        lambda *, network, container_id, aliases: connected.append(
            (network, container_id, aliases)
        ),
    )
    monkeypatch.setattr(manager, "_run", run)

    assert manager._ensure_phase_network(spec, "agent") == expected
    assert connected == [
        (expected, "gateway-container", ["agent-gateway"]),
        (expected, "mcp-container", ["agent-mcp"]),
    ]


def test_private_network_rejects_a_host_addressable_internal_bridge() -> None:
    spec = _spec()
    manager = ChatSandboxManager(
        ContainerBackend(image="img", network="unused", egress=_isolated_policy())
    )
    network = {
        "Name": manager.network_for_phase(spec, "agent"),
        "Driver": "bridge",
        "Scope": "local",
        "Internal": True,
        "Labels": manager._network_labels(spec, "agent"),
        "IPAM": {"Config": [{"Subnet": "10.240.0.0/29"}]},
        "Containers": {},
    }

    with pytest.raises(SandboxError, match="host-isolated"):
        manager._verify_phase_network(network, spec, "agent")


def test_private_network_allocator_retries_a_racing_subnet_claim(monkeypatch):
    spec = _spec()
    manager = ChatSandboxManager(
        ContainerBackend(image="img", network="unused", egress=_isolated_policy())
    )
    expected = manager.network_for_phase(spec, "agent")
    network: dict | None = None
    attempted: list[str] = []

    monkeypatch.setattr(manager, "_service_owners", lambda *_args: {})
    monkeypatch.setattr(manager, "_reconcile_phase_members", lambda **_kwargs: None)

    def inspect_network(name):
        assert name == expected
        return network

    def run(argv, **_kwargs):
        nonlocal network
        assert argv[1:3] == ["network", "create"]
        subnet = argv[argv.index("--subnet") + 1]
        attempted.append(subnet)
        if len(attempted) == 1:
            return SimpleNamespace(
                returncode=1,
                stdout="",
                stderr="Pool overlaps with other one on this address space",
            )
        network = {
            "Name": expected,
            "Driver": "bridge",
            "Scope": "local",
            "Internal": True,
            "Options": {CHAT_NETWORK_INHIBIT_IPV4_OPTION: "true"},
            "Labels": manager._network_labels(spec, "agent"),
            "IPAM": {"Config": [{"Subnet": subnet}]},
            "Containers": {},
        }
        return SimpleNamespace(returncode=0, stdout=f"{expected}\n", stderr="")

    monkeypatch.setattr(manager, "_inspect_network", inspect_network)
    monkeypatch.setattr(manager, "_run", run)

    assert manager._ensure_phase_network(spec, "agent") == expected
    assert len(attempted) == 2
    assert attempted[0] != attempted[1]
    assert all(value.endswith("/29") for value in attempted)


@pytest.mark.parametrize("pool", ["10.240.0.1/16", "2001:db8::/64", "8.8.8.0/24", "10.0.0.0/30"])
def test_private_network_pool_must_be_canonical_private_ipv4_and_hold_a_slash_29(pool):
    with pytest.raises(SandboxError, match="AGENT_EGRESS_CHAT_NETWORK_POOL"):
        ChatSandboxManager(ContainerBackend(image="img", chat_network_pool=pool))


def test_idle_stop_releases_private_network_and_wake_recreates_agent_phase(monkeypatch):
    spec = _spec()
    manager = ChatSandboxManager(
        ContainerBackend(image="img", network="unused", egress=_isolated_policy())
    )
    expected = manager.network_for_phase(spec, "agent")
    current = ChatSandboxInspection(
        container_id="sandbox-container",
        name=spec.container_name,
        status=ChatSandboxStatus.STOPPED,
        running=False,
        image=spec.sandbox_image_digest or "img",
        labels=spec.docker_labels(),
        mounts=(),
        networks=(expected,),
    )
    network_exists = True
    endpoints = {
        "sandbox-container": set(),
        "gateway-container": {"agent-gateway"},
        "mcp-container": {"agent-mcp"},
    }
    calls: list[list[str]] = []

    def inspect_network(name):
        assert name == expected
        if not network_exists:
            return None
        return {
            "Name": expected,
            "Driver": "bridge",
            "Scope": "local",
            "Internal": True,
            "Options": {CHAT_NETWORK_INHIBIT_IPV4_OPTION: "true"},
            "Labels": manager._network_labels(spec, "agent"),
            "IPAM": {"Config": [{"Subnet": "10.240.0.0/29"}]},
            "Containers": {container_id: {} for container_id in endpoints},
        }

    def run(argv, **_kwargs):
        nonlocal current, network_exists
        calls.append(list(argv))
        if argv[1:3] == ["network", "disconnect"]:
            container_id = argv[-1]
            endpoints.pop(container_id, None)
            if container_id == current.container_id:
                current = replace(current, networks=())
        elif argv[1:3] == ["network", "rm"]:
            assert not endpoints
            network_exists = False
        elif argv[1:3] == ["network", "connect"]:
            current = replace(current, networks=(expected,))
            network_exists = True
        elif argv[1] == "start":
            current = replace(
                current,
                running=True,
                status=ChatSandboxStatus.RUNNING,
            )
        else:
            raise AssertionError(argv)
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(manager, "_inspect_network", inspect_network)
    monkeypatch.setattr(
        manager,
        "_network_aliases_for",
        lambda ids, _net: {cid: endpoints[cid] for cid in ids if endpoints.get(cid)},
    )
    monkeypatch.setattr(manager, "inspect", lambda _name: current)
    monkeypatch.setattr(manager, "_require", lambda _spec: current)
    monkeypatch.setattr(manager, "_ensure_phase_network", lambda *_args: expected)
    monkeypatch.setattr(manager, "_run", run)

    stopped = manager.stop(spec)
    assert stopped.networks == ()
    assert network_exists is False

    started = manager.start(spec, network_phase="agent")
    assert started.running is True
    assert started.networks == (expected,)
    assert ["docker", "network", "rm", expected] in calls
    assert ["docker", "network", "connect", expected, current.container_id] in calls


def test_wake_reconnects_a_same_name_bridge_when_docker_retains_its_old_id(monkeypatch):
    spec = _spec()
    manager = ChatSandboxManager(
        ContainerBackend(image="img", network="unused", egress=_isolated_policy())
    )
    expected = manager.network_for_phase(spec, "agent")
    current = ChatSandboxInspection(
        container_id="sandbox-container",
        name=spec.container_name,
        status=ChatSandboxStatus.STOPPED,
        running=False,
        image=spec.sandbox_image_digest or "img",
        labels=spec.docker_labels(),
        mounts=(),
        networks=(expected,),
        network_ids=((expected, "deleted-network-id"),),
    )
    calls: list[list[str]] = []

    def run(argv, **_kwargs):
        nonlocal current
        calls.append(list(argv))
        assert argv[1:3] == ["network", "connect"]
        current = replace(current, network_ids=((expected, ""),))
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(manager, "_ensure_phase_network", lambda *_args: expected)
    monkeypatch.setattr(manager, "_inspect_network", lambda _name: {"Id": "replacement-id"})
    monkeypatch.setattr(manager, "_require", lambda _spec: current)
    monkeypatch.setattr(manager, "_run", run)

    refreshed = manager._require_phase_network(spec, current, "agent")

    assert refreshed.network_ids == ((expected, ""),)
    assert calls == [["docker", "network", "connect", expected, current.container_id]]


def test_agent_transition_removes_the_no_longer_reachable_setup_bridge(monkeypatch):
    spec = _spec()
    manager = ChatSandboxManager(
        ContainerBackend(image="img", network="unused", egress=_isolated_policy())
    )
    setup = manager.network_for_phase(spec, "setup")
    agent = manager.network_for_phase(spec, "agent")
    current = ChatSandboxInspection(
        container_id="sandbox-container",
        name=spec.container_name,
        status=ChatSandboxStatus.RUNNING,
        running=True,
        image=spec.sandbox_image_digest or "img",
        labels=spec.docker_labels(),
        mounts=(),
        networks=(setup,),
    )
    setup_exists = True
    endpoints = {
        "sandbox-container": set(),
        "proxy-container": {"package-proxy"},
    }
    calls: list[list[str]] = []

    def inspect_network(name):
        if name != setup or not setup_exists:
            return None
        return {
            "Name": setup,
            "Driver": "bridge",
            "Scope": "local",
            "Internal": True,
            "Labels": manager._network_labels(spec, "setup"),
            "IPAM": {"Config": [{"Subnet": "10.240.0.0/29"}]},
            "Containers": {container_id: {} for container_id in endpoints},
        }

    def run(argv, **_kwargs):
        nonlocal current, setup_exists
        calls.append(list(argv))
        if argv[1:3] == ["network", "connect"]:
            current = replace(current, networks=tuple(sorted({*current.networks, agent})))
        elif argv[1:3] == ["network", "disconnect"]:
            container_id = argv[-1]
            endpoints.pop(container_id, None)
            if container_id == current.container_id:
                current = replace(current, networks=(agent,))
        elif argv[1:3] == ["network", "rm"]:
            setup_exists = False
        else:
            raise AssertionError(argv)
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(
        manager, "_ensure_phase_network", lambda _spec, phase: agent if phase == "agent" else setup
    )
    monkeypatch.setattr(manager, "_inspect_network", inspect_network)
    monkeypatch.setattr(
        manager,
        "_network_aliases_for",
        lambda ids, _net: {cid: endpoints[cid] for cid in ids if endpoints.get(cid)},
    )
    monkeypatch.setattr(manager, "inspect", lambda _name: current)
    monkeypatch.setattr(manager, "_require", lambda _spec: current)
    monkeypatch.setattr(manager, "_run", run)

    transitioned = manager.transition_to_agent_network(spec)

    assert transitioned.networks == (agent,)
    assert setup_exists is False
    assert ["docker", "network", "rm", setup] in calls


def test_agent_transition_preserves_a_bridge_shared_by_both_phases(monkeypatch):
    spec = _spec()
    policy = EgressPolicy(
        tiers={"setup": EgressTier.PLATFORM_ONLY, "agent": EgressTier.PLATFORM_ONLY},
        networks={EgressTier.PLATFORM_ONLY: "agent-backplane"},
        no_proxy=("agent-gateway", "agent-mcp", "localhost", "127.0.0.1"),
    )
    manager = ChatSandboxManager(ContainerBackend(image="img", network="unused", egress=policy))
    shared = manager.network_for_phase(spec, "agent")
    assert manager.network_for_phase(spec, "setup") == shared
    current = ChatSandboxInspection(
        container_id="sandbox-container",
        name=spec.container_name,
        status=ChatSandboxStatus.RUNNING,
        running=True,
        image=spec.sandbox_image_digest or "img",
        labels=spec.docker_labels(),
        mounts=(),
        networks=(shared,),
    )

    monkeypatch.setattr(manager, "_ensure_phase_network", lambda *_args: shared)
    monkeypatch.setattr(manager, "_require", lambda _spec: current)
    monkeypatch.setattr(
        manager,
        "_release_owned_network",
        lambda *_args: pytest.fail("the active shared phase bridge must be preserved"),
    )

    assert manager.transition_to_agent_network(spec) == current


def test_broker_startup_prunes_a_phase_network_left_before_container_create(monkeypatch):
    spec = _spec()
    manager = ChatSandboxManager(
        ContainerBackend(image="img", network="unused", egress=_isolated_policy())
    )
    orphan = manager.network_for_phase(spec, "setup")
    network_exists = True
    endpoints = {"proxy-container": {"package-proxy"}}
    calls: list[list[str]] = []

    def inspect_network(name):
        assert name in {"network-id", orphan}
        if not network_exists:
            return None
        return {
            "Id": "network-id",
            "Name": orphan,
            "Created": "1970-01-01T00:00:00Z",
            "Driver": "bridge",
            "Scope": "local",
            "Internal": True,
            "Labels": manager._network_labels(spec, "setup"),
            "IPAM": {"Config": [{"Subnet": "10.240.0.0/29"}]},
            "Containers": {container_id: {} for container_id in endpoints},
        }

    def run(argv, **_kwargs):
        nonlocal network_exists
        calls.append(list(argv))
        if argv[1:3] == ["network", "ls"]:
            return SimpleNamespace(returncode=0, stdout="network-id\n", stderr="")
        if argv[1:3] == ["network", "disconnect"]:
            endpoints.pop(argv[-1], None)
            return SimpleNamespace(returncode=0, stdout="", stderr="")
        if argv[1:3] == ["network", "rm"]:
            network_exists = False
            return SimpleNamespace(returncode=0, stdout="", stderr="")
        raise AssertionError(argv)

    monkeypatch.setattr(manager, "_inspect_network", inspect_network)
    monkeypatch.setattr(
        manager,
        "_network_aliases_for",
        lambda ids, _net: {cid: endpoints[cid] for cid in ids if endpoints.get(cid)},
    )
    monkeypatch.setattr(manager, "_run", run)

    manager.prune_orphan_networks([])

    assert network_exists is False
    assert ["docker", "network", "rm", "network-id"] in calls


def test_ownerless_prune_pins_the_old_id_across_a_same_name_replacement(monkeypatch):
    spec = _spec()
    manager = ChatSandboxManager(
        ContainerBackend(image="img", network="unused", egress=_isolated_policy())
    )
    orphan = manager.network_for_phase(spec, "setup")
    old = {
        "Id": "old-network-id",
        "Name": orphan,
        "Created": "1970-01-01T00:00:00Z",
        "Driver": "bridge",
        "Internal": True,
        "Labels": manager._network_labels(spec, "setup"),
        "Containers": {"proxy-container": {}},
    }
    replacement = {
        **old,
        "Id": "replacement-network-id",
        "Created": "2999-01-01T00:00:00Z",
    }
    replaced = False
    calls: list[list[str]] = []

    def inspect_network(name):
        if name == "listed-network-id":
            return old
        if name == "old-network-id":
            return None if replaced else old
        if name == orphan:
            return replacement if replaced else old
        return None

    def run(argv, **_kwargs):
        nonlocal replaced
        calls.append(list(argv))
        if argv[1:3] == ["network", "ls"]:
            return SimpleNamespace(returncode=0, stdout="listed-network-id\n", stderr="")
        if argv[1:3] == ["network", "disconnect"]:
            assert argv[-2:] == ["old-network-id", "proxy-container"]
            replaced = True
            return SimpleNamespace(returncode=1, stdout="", stderr="network not found")
        if argv[1:3] == ["network", "rm"]:
            assert argv[-1] == "old-network-id"
            return SimpleNamespace(returncode=1, stdout="", stderr="network not found")
        raise AssertionError(argv)

    monkeypatch.setattr(manager, "_inspect_network", inspect_network)
    monkeypatch.setattr(
        manager,
        "_network_aliases_for",
        lambda ids, _network: {cid: {"package-proxy"} for cid in ids},
    )
    monkeypatch.setattr(manager, "_run", run)

    manager.prune_orphan_networks([])

    assert replaced is True
    assert all(orphan not in call[3:] for call in calls if call[1:2] == ["network"])


def test_ownerless_prune_restores_relays_after_a_late_sandbox_attach(monkeypatch):
    spec = _spec()
    manager = ChatSandboxManager(
        ContainerBackend(image="img", network="unused", egress=_isolated_policy())
    )
    orphan = manager.network_for_phase(spec, "agent")
    network_id = "agent-network-id"
    relay_id = "relay-container"
    sandbox_id = "sha256:container-one"
    relay_attached = True
    sandbox_attached = False

    def network():
        containers = {}
        if relay_attached:
            containers[relay_id] = {}
        if sandbox_attached:
            containers[sandbox_id] = {}
        return {
            "Id": network_id,
            "Name": orphan,
            "Created": "1970-01-01T00:00:00Z",
            "Driver": "bridge",
            "Internal": True,
            "Labels": manager._network_labels(spec, "agent"),
            "Containers": containers,
        }

    def aliases(container_id, _network):
        if container_id == relay_id and relay_attached:
            return {"agent-gateway", "agent-mcp"}
        return None

    def run(argv, **_kwargs):
        nonlocal relay_attached, sandbox_attached
        if argv[1:3] == ["network", "ls"]:
            return SimpleNamespace(returncode=0, stdout=f"{network_id}\n", stderr="")
        if argv[1:3] == ["network", "disconnect"]:
            assert argv[-2:] == [network_id, relay_id]
            relay_attached = False
            return SimpleNamespace(returncode=0, stdout="", stderr="")
        if argv[1:3] == ["network", "rm"]:
            sandbox_attached = True
            return SimpleNamespace(returncode=1, stdout="", stderr="active endpoints")
        if argv[1:3] == ["network", "connect"]:
            assert argv[-2:] == [orphan, relay_id]
            relay_attached = True
            return SimpleNamespace(returncode=0, stdout="", stderr="")
        raise AssertionError(argv)

    monkeypatch.setattr(manager, "_inspect_network", lambda _name: network())
    monkeypatch.setattr(
        manager,
        "_network_aliases_for",
        lambda ids, network: {
            cid: found for cid in ids if (found := aliases(cid, network)) is not None
        },
    )
    monkeypatch.setattr(
        manager,
        "_service_owners",
        lambda _base, _aliases: {relay_id: ["agent-gateway", "agent-mcp"]},
    )
    monkeypatch.setattr(manager, "_run", run)

    manager.prune_orphan_networks([])

    assert sandbox_attached is True
    assert relay_attached is True


def test_orphan_prune_preserves_a_young_bridge_before_its_container_appears(monkeypatch):
    spec = _spec()
    manager = ChatSandboxManager(
        ContainerBackend(image="img", network="unused", egress=_isolated_policy())
    )
    pending = manager.network_for_phase(spec, "setup")
    network = {
        "Name": pending,
        "Created": "2999-01-01T00:00:00Z",
        "Driver": "bridge",
        "Internal": True,
        "Labels": manager._network_labels(spec, "setup"),
        "Containers": {},
    }

    monkeypatch.setattr(
        manager,
        "_inspect_network",
        lambda name: network if name in {"network-id", pending} else None,
    )
    monkeypatch.setattr(
        manager,
        "_run",
        lambda argv, **_kwargs: (
            SimpleNamespace(returncode=0, stdout="network-id\n", stderr="")
            if argv[1:3] == ["network", "ls"]
            else pytest.fail(f"young in-flight network must not be mutated: {argv}")
        ),
    )

    manager.prune_orphan_networks([])


def test_reconcile_preserves_a_young_unattached_phase_bridge(monkeypatch):
    spec = _spec()
    manager = ChatSandboxManager(
        ContainerBackend(image="img", network="unused", egress=_isolated_policy())
    )
    setup = manager.network_for_phase(spec, "setup")
    pending = manager.network_for_phase(spec, "agent")
    current = ChatSandboxInspection(
        container_id="sha256:container-one",
        name=spec.container_name,
        status=ChatSandboxStatus.RUNNING,
        running=True,
        image=spec.sandbox_image_digest,
        labels=spec.docker_labels(),
        mounts=(),
        networks=(setup,),
    )
    network = {
        "Name": pending,
        "Created": "2999-01-01T00:00:00Z",
        "Driver": "bridge",
        "Internal": True,
        "Labels": manager._network_labels(spec, "agent"),
        "Containers": {},
    }

    monkeypatch.setattr(
        manager,
        "_inspect_network",
        lambda name: network if name in {"network-id", pending} else None,
    )
    monkeypatch.setattr(manager, "inspect", lambda _name: current)
    monkeypatch.setattr(
        manager,
        "_run",
        lambda argv, **_kwargs: (
            SimpleNamespace(returncode=0, stdout="network-id\n", stderr="")
            if argv[1:3] == ["network", "ls"]
            else pytest.fail(f"in-flight phase network must not be mutated: {argv}")
        ),
    )

    manager.prune_orphan_networks([spec])


def test_reconcile_reclaims_a_stale_unattached_phase_bridge(monkeypatch):
    spec = _spec()
    manager = ChatSandboxManager(
        ContainerBackend(image="img", network="unused", egress=_isolated_policy())
    )
    setup = manager.network_for_phase(spec, "setup")
    abandoned = manager.network_for_phase(spec, "agent")
    current = ChatSandboxInspection(
        container_id="sha256:container-one",
        name=spec.container_name,
        status=ChatSandboxStatus.RUNNING,
        running=True,
        image=spec.sandbox_image_digest,
        labels=spec.docker_labels(),
        mounts=(),
        networks=(setup,),
    )
    network = {
        "Name": abandoned,
        "Created": "1970-01-01T00:00:00Z",
        "Driver": "bridge",
        "Internal": True,
        "Labels": manager._network_labels(spec, "agent"),
        "Containers": {},
    }
    released: list[tuple[str, ChatSandboxSpec, str]] = []

    monkeypatch.setattr(
        manager,
        "_inspect_network",
        lambda name: network if name in {"network-id", abandoned} else None,
    )
    monkeypatch.setattr(manager, "inspect", lambda _name: current)
    monkeypatch.setattr(
        manager,
        "_release_owned_network",
        lambda name, owned_spec, phase, **_kwargs: released.append((name, owned_spec, phase)),
    )
    monkeypatch.setattr(
        manager,
        "_run",
        lambda argv, **_kwargs: (
            SimpleNamespace(returncode=0, stdout="network-id\n", stderr="")
            if argv[1:3] == ["network", "ls"]
            else pytest.fail(f"unexpected Docker mutation: {argv}")
        ),
    )

    manager.prune_orphan_networks([spec])

    assert released == [(abandoned, spec, "agent")]


def test_stale_prune_never_disconnects_a_sandbox_that_attached_during_recheck(
    monkeypatch,
):
    spec = _spec()
    manager = ChatSandboxManager(
        ContainerBackend(image="img", network="unused", egress=_isolated_policy())
    )
    setup = manager.network_for_phase(spec, "setup")
    pending = manager.network_for_phase(spec, "agent")
    before = ChatSandboxInspection(
        container_id="sha256:container-one",
        name=spec.container_name,
        status=ChatSandboxStatus.RUNNING,
        running=True,
        image=spec.sandbox_image_digest,
        labels=spec.docker_labels(),
        mounts=(),
        networks=(setup,),
    )
    attached = replace(before, networks=(pending,))
    network = {
        "Name": pending,
        "Created": "1970-01-01T00:00:00Z",
        "Driver": "bridge",
        "Internal": True,
        "Labels": manager._network_labels(spec, "agent"),
        "Containers": {before.container_id: {}},
    }
    inspections = iter((before, attached))

    monkeypatch.setattr(
        manager,
        "_inspect_network",
        lambda name: network if name in {"network-id", pending} else None,
    )
    monkeypatch.setattr(manager, "inspect", lambda _name: next(inspections))
    monkeypatch.setattr(
        manager,
        "_run",
        lambda argv, **_kwargs: (
            SimpleNamespace(returncode=0, stdout="network-id\n", stderr="")
            if argv[1:3] == ["network", "ls"]
            else pytest.fail(f"newly attached sandbox must not be mutated: {argv}")
        ),
    )

    manager.prune_orphan_networks([spec])


def test_stale_prune_ignores_a_same_name_replacement_network(monkeypatch):
    spec = _spec()
    manager = ChatSandboxManager(
        ContainerBackend(image="img", network="unused", egress=_isolated_policy())
    )
    setup = manager.network_for_phase(spec, "setup")
    pending = manager.network_for_phase(spec, "agent")
    current = ChatSandboxInspection(
        container_id="sha256:container-one",
        name=spec.container_name,
        status=ChatSandboxStatus.RUNNING,
        running=True,
        image=spec.sandbox_image_digest,
        labels=spec.docker_labels(),
        mounts=(),
        networks=(setup,),
    )
    old = {
        "Id": "old-network-id",
        "Name": pending,
        "Created": "1970-01-01T00:00:00Z",
        "Driver": "bridge",
        "Internal": True,
        "Labels": manager._network_labels(spec, "agent"),
        "Containers": {},
    }
    replacement = {
        **old,
        "Id": "replacement-network-id",
        "Created": "2999-01-01T00:00:00Z",
    }

    monkeypatch.setattr(
        manager,
        "_inspect_network",
        lambda name: old if name == "listed-network-id" else replacement,
    )
    monkeypatch.setattr(manager, "inspect", lambda _name: current)
    monkeypatch.setattr(
        manager,
        "_run",
        lambda argv, **_kwargs: (
            SimpleNamespace(returncode=0, stdout="listed-network-id\n", stderr="")
            if argv[1:3] == ["network", "ls"]
            else pytest.fail(f"replacement network must not be mutated: {argv}")
        ),
    )

    manager.prune_orphan_networks([spec])


def test_stale_prune_restores_relays_when_a_sandbox_attaches_before_remove(
    monkeypatch,
):
    spec = _spec()
    manager = ChatSandboxManager(
        ContainerBackend(image="img", network="unused", egress=_isolated_policy())
    )
    setup = manager.network_for_phase(spec, "setup")
    pending = manager.network_for_phase(spec, "agent")
    sandbox_id = "sha256:container-one"
    network_id = "agent-network-id"
    relay_id = "relay-container"
    sandbox_attached = False
    relay_attached = True
    before = ChatSandboxInspection(
        container_id=sandbox_id,
        name=spec.container_name,
        status=ChatSandboxStatus.RUNNING,
        running=True,
        image=spec.sandbox_image_digest,
        labels=spec.docker_labels(),
        mounts=(),
        networks=(setup,),
    )

    def network():
        containers = {}
        if relay_attached:
            containers[relay_id] = {}
        if sandbox_attached:
            containers[sandbox_id] = {}
        return {
            "Id": network_id,
            "Name": pending,
            "Created": "1970-01-01T00:00:00Z",
            "Driver": "bridge",
            "Scope": "local",
            "Internal": True,
            "Options": {CHAT_NETWORK_INHIBIT_IPV4_OPTION: "true"},
            "Labels": manager._network_labels(spec, "agent"),
            "IPAM": {"Config": [{"Subnet": "10.240.0.0/29"}]},
            "Containers": containers,
        }

    def inspect(_name):
        if not sandbox_attached:
            return before
        return replace(
            before,
            networks=(pending,),
            network_ids=((pending, network_id),),
        )

    def aliases(container_id, _network):
        if container_id == relay_id and relay_attached:
            return {"agent-gateway", "agent-mcp"}
        return None

    def run(argv, **_kwargs):
        nonlocal relay_attached, sandbox_attached
        if argv[1:3] == ["network", "ls"]:
            return SimpleNamespace(returncode=0, stdout=f"{network_id}\n", stderr="")
        if argv[1:3] == ["network", "disconnect"]:
            assert argv[-2:] == [network_id, relay_id]
            relay_attached = False
            return SimpleNamespace(returncode=0, stdout="", stderr="")
        if argv[1:3] == ["network", "rm"]:
            sandbox_attached = True
            return SimpleNamespace(returncode=1, stdout="", stderr="active endpoints")
        if argv[1:3] == ["network", "connect"]:
            assert argv[-2:] == [pending, relay_id]
            relay_attached = True
            return SimpleNamespace(returncode=0, stdout="", stderr="")
        raise AssertionError(argv)

    monkeypatch.setattr(manager, "_inspect_network", lambda _name: network())
    monkeypatch.setattr(manager, "inspect", inspect)
    monkeypatch.setattr(
        manager,
        "_network_aliases_for",
        lambda ids, network: {
            cid: found for cid in ids if (found := aliases(cid, network)) is not None
        },
    )
    monkeypatch.setattr(
        manager,
        "_service_owners",
        lambda _base, _aliases: {relay_id: ["agent-gateway", "agent-mcp"]},
    )
    monkeypatch.setattr(manager, "_reconcile_phase_members", lambda **_kwargs: None)
    monkeypatch.setattr(manager, "_run", run)

    manager.prune_orphan_networks([spec])

    assert sandbox_attached is True
    assert relay_attached is True


def test_reconcile_preserves_the_private_network_attached_to_an_active_chat(monkeypatch):
    spec = _spec()
    manager = ChatSandboxManager(
        ContainerBackend(image="img", network="unused", egress=_isolated_policy())
    )
    active = manager.network_for_phase(spec, "agent")
    current = ChatSandboxInspection(
        container_id="sha256:container-one",
        name=spec.container_name,
        status=ChatSandboxStatus.RUNNING,
        running=True,
        image=spec.sandbox_image_digest,
        labels=spec.docker_labels(),
        mounts=(),
        networks=(active,),
    )

    monkeypatch.setattr(
        manager,
        "_inspect_network",
        lambda name: (
            {
                "Name": active,
                "Driver": "bridge",
                "Internal": True,
                "Labels": manager._network_labels(spec, "agent"),
                "Containers": {"sha256:container-one": {}},
            }
            if name in {"network-id", active}
            else None
        ),
    )
    monkeypatch.setattr(manager, "inspect", lambda _name: current)
    monkeypatch.setattr(
        manager,
        "_run",
        lambda argv, **_kwargs: (
            SimpleNamespace(
                returncode=0,
                stdout="network-id\n",
                stderr="",
            )
            if argv[1:3] == ["network", "ls"]
            else pytest.fail(f"active network must not be mutated: {argv}")
        ),
    )

    manager.prune_orphan_networks([spec])


def test_stopped_pre_upgrade_chat_moves_off_the_shared_backplane(monkeypatch):
    spec = _spec()
    manager = ChatSandboxManager(
        ContainerBackend(image="img", network="unused", egress=_isolated_policy())
    )
    expected = manager.network_for_phase(spec, "agent")
    networks = {"agent-backplane"}
    current = ChatSandboxInspection(
        container_id="sha256:container-one",
        name=spec.container_name,
        status=ChatSandboxStatus.STOPPED,
        running=False,
        image=spec.sandbox_image_digest or "img",
        labels=spec.docker_labels(),
        mounts=(),
        networks=("agent-backplane",),
    )
    calls: list[list[str]] = []

    monkeypatch.setattr(manager, "_ensure_phase_network", lambda *_args: expected)
    monkeypatch.setattr(
        manager,
        "_require",
        lambda _spec: replace(current, networks=tuple(sorted(networks))),
    )

    def run(argv, **_kwargs):
        calls.append(list(argv))
        if argv[1:3] == ["network", "connect"]:
            networks.add(expected)
        elif argv[1:3] == ["network", "disconnect"]:
            networks.remove("agent-backplane")
        else:
            raise AssertionError(argv)
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(manager, "_run", run)

    moved = manager._require_phase_network(spec, current, "agent")

    assert moved.networks == (expected,)
    assert ["docker", "network", "connect", expected, current.container_id] in calls
    assert [
        "docker",
        "network",
        "disconnect",
        "--force",
        "agent-backplane",
        current.container_id,
    ] in calls


def test_running_chat_never_hot_migrates_from_the_shared_backplane(monkeypatch):
    spec = _spec()
    manager = ChatSandboxManager(
        ContainerBackend(image="img", network="unused", egress=_isolated_policy())
    )
    current = ChatSandboxInspection(
        container_id="sha256:container-one",
        name=spec.container_name,
        status=ChatSandboxStatus.RUNNING,
        running=True,
        image=spec.sandbox_image_digest or "img",
        labels=spec.docker_labels(),
        mounts=(),
        networks=("agent-backplane",),
    )
    monkeypatch.setattr(
        manager,
        "_ensure_phase_network",
        lambda *_args: pytest.fail("shared backplane must fail before migration"),
    )

    with pytest.raises(SandboxError, match="shared/obsolete"):
        manager._require_phase_network(spec, current, "agent")


def test_running_pre_generation_private_bridge_migrates_without_shared_fallback(
    monkeypatch,
):
    spec = _spec()
    manager = ChatSandboxManager(
        ContainerBackend(image="img", network="unused", egress=_isolated_policy())
    )
    expected = manager.network_for_phase(spec, "agent")
    legacy = "freeinference-chat-net-legacy-digest"
    networks = {legacy}
    current = ChatSandboxInspection(
        container_id="sha256:container-one",
        name=spec.container_name,
        status=ChatSandboxStatus.RUNNING,
        running=True,
        image=spec.sandbox_image_digest or "img",
        labels=spec.docker_labels(),
        mounts=(),
        networks=(legacy,),
    )
    released: list[str] = []

    monkeypatch.setattr(
        manager,
        "_inspect_network",
        lambda name: (
            {
                "Name": legacy,
                "Driver": "bridge",
                "Internal": True,
                "Labels": manager._network_labels(spec, "agent"),
            }
            if name == legacy
            else None
        ),
    )
    monkeypatch.setattr(manager, "_ensure_phase_network", lambda *_args: expected)
    monkeypatch.setattr(
        manager,
        "_require",
        lambda _spec: replace(current, networks=tuple(sorted(networks))),
    )
    monkeypatch.setattr(
        manager,
        "_release_owned_network",
        lambda name, *_args, **_kwargs: released.append(name),
    )

    def run(argv, **_kwargs):
        if argv[1:3] == ["network", "connect"]:
            networks.add(expected)
        elif argv[1:3] == ["network", "disconnect"]:
            networks.remove(legacy)
        else:
            raise AssertionError(argv)
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(manager, "_run", run)

    moved = manager._require_phase_network(spec, current, "agent")

    assert moved.running is True
    assert moved.networks == (expected,)
    assert released == [legacy]


def test_retained_storage_preflight_reads_daemon_root_without_mounting_it(monkeypatch):
    manager = ChatSandboxManager(ContainerBackend(image="img", network="agent-net"))
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
            network="agent-net",
            extra_args=["--mount=type=bind,source=/host,target=/workspace"],
        )
    )

    with pytest.raises(SandboxError, match="unsafe for retained chats"):
        manager.build_create_command(_spec())


def test_create_refuses_an_image_override_that_would_make_the_pin_a_lie():
    manager = ChatSandboxManager(ContainerBackend(image="registry/agent:latest", network="agent"))

    with pytest.raises(SandboxError, match="immutable digest"):
        manager.build_create_command(_spec(), image="sha256:different")


def test_inspect_rejects_a_bind_or_volume_even_when_labels_match(monkeypatch):
    spec = _spec()
    manager = ChatSandboxManager(ContainerBackend(image="img", network="agent-net"))
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
    manager = ChatSandboxManager(ContainerBackend(image="img", network="agent-net"))
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
    manager = ChatSandboxManager(ContainerBackend(image="img", network="agent-net"))
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
    manager = ChatSandboxManager(ContainerBackend(image="img", network="agent-net"))
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
    manager = ChatSandboxManager(ContainerBackend(image="img", network="agent-net"))
    payload = _inspect_payload(spec, status="running", running=True, paused=True)
    monkeypatch.setattr(
        "cloud_agent_host.chat_sandbox.subprocess.run",
        lambda *_args, **_kwargs: SimpleNamespace(returncode=0, stdout=payload, stderr=""),
    )

    with pytest.raises(SandboxError, match="paused"):
        manager.stop(spec)


def test_exec_is_bound_to_the_inspected_container_and_workspace(monkeypatch):
    spec = _spec()
    manager = ChatSandboxManager(ContainerBackend(image="img", network="agent-net"))
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
    manager = ChatSandboxManager(ContainerBackend(image="img", network="agent-net"))
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
    manager = ChatSandboxManager(ContainerBackend(image="img", network="agent-net"))
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
    assert script.count("-path /agent-state/claude/sessions") == 2
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
    manager = ChatSandboxManager(ContainerBackend(image="img", network="agent-net"))
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
    manager = ChatSandboxManager(ContainerBackend(image="img", network="agent-net"))
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
    manager = ChatSandboxManager(ContainerBackend(image="img", network="agent-net"))
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
    manager = ChatSandboxManager(ContainerBackend(image="img", network="agent-net"))
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
    manager = ChatSandboxManager(ContainerBackend(image="img", network="agent-net"))
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
    manager = ChatSandboxManager(ContainerBackend(image="img", network="agent-net"))
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

    command, kwargs = popen_calls[0]
    script = command[-3]
    assert "--interactive" in command
    assert "--tty" in command
    assert 'printf "%s\\n" "$$" > "$pidfile"' in script
    assert "/bin/sh +m -i" in script
    assert "umask 0002" in script
    assert "setsid" not in script
    assert " &" not in script
    assert kwargs["start_new_session"] is True


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
    manager = ChatSandboxManager(ContainerBackend(image="img", network="agent-net"))
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
    assert 'grep -Fqx -- "$pidfile"' in script
    assert 'kill -KILL -"$pid"' in script
    assert "pkill" not in script


def test_unsettled_retry_reset_targets_only_the_two_container_owned_roots(monkeypatch):
    spec = _spec()
    manager = ChatSandboxManager(ContainerBackend(image="img", network="agent-net"))
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
    manager = ChatSandboxManager(ContainerBackend(image="img", network="agent-net"))
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


def test_retained_backend_reports_the_cli_version_pinned_in_its_container(monkeypatch):
    spec = _spec()
    manager = ChatSandboxManager(ContainerBackend(image="img", network="agent-net"))
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


@pytest.mark.parametrize("sandbox_runtime", [None, KATA_RUNTIME])
def test_setup_network_is_replaced_by_agent_network_before_agent_exec(monkeypatch, sandbox_runtime):
    spec = _spec()
    policy = EgressPolicy(
        tiers={"setup": EgressTier.TRUSTED, "agent": EgressTier.PLATFORM_ONLY},
        networks={
            EgressTier.TRUSTED: "setup-net",
            EgressTier.PLATFORM_ONLY: "agent-net",
        },
    )
    manager = ChatSandboxManager(
        ContainerBackend(image="img", runtime=sandbox_runtime, network="unused", egress=policy)
    )
    networks = {"setup-net"}
    running = True
    calls: list[list[str]] = []

    def fake_run(argv, **_kwargs):
        nonlocal running
        calls.append(list(argv))
        if argv[1:3] == ["container", "inspect"]:
            return SimpleNamespace(
                returncode=0,
                stdout=_inspect_payload(
                    spec,
                    status="running" if running else "exited",
                    running=running,
                    networks=tuple(networks),
                    sandbox_runtime=sandbox_runtime or "runc",
                ),
                stderr="",
            )
        if argv[1:3] == ["network", "connect"]:
            assert running is (sandbox_runtime != KATA_RUNTIME)
            networks.add("agent-net")
            return SimpleNamespace(returncode=0, stdout="", stderr="")
        if argv[1:3] == ["network", "disconnect"]:
            networks.remove("setup-net")
            return SimpleNamespace(returncode=0, stdout="", stderr="")
        if argv[1] in {"stop", "start"}:
            running = argv[1] == "start"
            return SimpleNamespace(returncode=0, stdout="", stderr="")
        raise AssertionError(argv)

    monkeypatch.setattr("cloud_agent_host.chat_sandbox.subprocess.run", fake_run)

    create_command = manager.build_create_command(spec)
    transitioned = manager.transition_to_agent_network(spec)

    assert create_command[create_command.index("--network") + 1] == CHAT_BOOTSTRAP_NETWORK
    assert transitioned.networks == ("agent-net",)
    assert transitioned.running
    assert sum(call[1] == "stop" for call in calls) == int(sandbox_runtime == KATA_RUNTIME)
    assert sum(call[1] == "start" for call in calls) == int(sandbox_runtime == KATA_RUNTIME)
    assert ["docker", "network", "connect", "agent-net", "sha256:container-one"] in calls
    assert [
        "docker",
        "network",
        "disconnect",
        "--force",
        "setup-net",
        "sha256:container-one",
    ] in calls


@pytest.mark.parametrize(
    "field,value,message",
    [
        ("thread_id", "", "thread_id"),
        ("sandbox_id", "", "sandbox_id"),
        ("generation", 0, "generation"),
        ("stop_timeout_s", 0, "stop_timeout_s"),
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


def test_backplane_peers_are_inspected_in_one_call(monkeypatch):
    """One `docker inspect` for the whole backplane, not one per container.

    Every reconcile resolves the trusted service aliases, and it used to fork a
    process per container on the egress backplane — so a reconcile's cost grew
    with the size of the backplane for no reason. Measured on the deployment
    host: twelve milliseconds per `container inspect`, and seventeen for seven
    ids in a single call.
    """
    manager = ChatSandboxManager(
        ContainerBackend(image="img", network="unused", egress=_isolated_policy())
    )
    base = manager.backend.network_for_phase("agent")
    peers = {"gw": "agent-gateway", "mcp": "agent-mcp", "other-a": "x", "other-b": "y"}

    monkeypatch.setattr(
        manager,
        "_inspect_network",
        lambda name: {"Name": name, "Containers": {cid: {} for cid in peers}},
    )

    calls: list[list[str]] = []

    def run(argv, **_kwargs):
        calls.append(list(argv))
        assert argv[1:3] == ["container", "inspect"]
        requested = argv[3:]
        return SimpleNamespace(
            returncode=0,
            stdout=json.dumps(
                [
                    {
                        "Id": cid,
                        "Name": f"/{cid}",
                        "State": {"Running": True},
                        "Config": {"Hostname": cid},
                        "NetworkSettings": {"Networks": {base: {"Aliases": [peers[cid]]}}},
                    }
                    for cid in requested
                ]
            ),
            stderr="",
        )

    monkeypatch.setattr(manager, "_run", run)

    owners = manager._service_owners(base, ("agent-gateway", "agent-mcp"))

    assert owners == {"gw": ["agent-gateway"], "mcp": ["agent-mcp"]}
    assert len(calls) == 1, f"one call for four peers, got {len(calls)}"
    assert sorted(calls[0][3:]) == ["gw", "mcp", "other-a", "other-b"]


def test_a_peer_that_vanished_mid_reconcile_is_simply_absent(monkeypatch):
    """Docker exits non-zero when one id is missing but still answers for the rest.

    Compose replacement can remove an endpoint between the network inspection
    and this one. The batched call must read that partial answer rather than
    discard it on the exit code — the exact-one check below is what turns the
    race into a retryable failure.
    """
    manager = ChatSandboxManager(
        ContainerBackend(image="img", network="unused", egress=_isolated_policy())
    )
    base = manager.backend.network_for_phase("agent")

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
                        "NetworkSettings": {"Networks": {base: {"Aliases": ["agent-gateway"]}}},
                    }
                ]
            ),
            stderr="Error: No such container: gone",
        ),
    )

    assert manager._service_owners(base, ("agent-gateway",)) == {"gw": ["agent-gateway"]}


def _stopped_on(manager: ChatSandboxManager, spec: ChatSandboxSpec, network: str):
    """One retained container that stopped while attached to ``network``."""
    inspection = ChatSandboxInspection(
        container_id="c0ffee",
        name=spec.container_name,
        status=ChatSandboxStatus.STOPPED,
        running=False,
        image="registry/agent:1",
        labels={CHAT_SANDBOX_ID_LABEL: spec.sandbox_id},
        mounts=(),
        networks=(network,),
    )
    manager._require = lambda _spec: inspection  # type: ignore[method-assign]
    return inspection


def test_a_pruned_chat_network_is_released_rather_than_failing_the_reconcile():
    """The bridge is gone; its stale endpoint must not fail the whole broker.

    The chat-private network is pruned on its own grace timer, so a container
    that stopped before that timer can outlive it and keep an endpoint record
    naming a network the daemon no longer has. Reading that as an unknown
    egress network failed this sandbox's stop, and because the supervisor stops
    every retained sandbox while reconciling at startup, the workspace broker
    exited instead of serving.
    """
    spec = _spec()
    policy = _isolated_policy()
    manager = ChatSandboxManager(ContainerBackend(image="img", network="unused", egress=policy))
    inspection = _stopped_on(manager, spec, "freeinference-chat-net-deadbeefdeadbeefdeadbeef")
    disconnected: list[tuple[str, str]] = []
    manager._inspect_network = lambda _name: None  # type: ignore[method-assign]
    manager._disconnect_endpoint = (  # type: ignore[method-assign]
        lambda network, container_id, **_: disconnected.append((network, container_id))
    )

    assert manager._release_sandbox_networks(spec, inspection) is inspection
    assert disconnected == [
        ("freeinference-chat-net-deadbeefdeadbeefdeadbeef", "c0ffee"),
    ]


def test_the_null_network_is_what_a_finished_release_looks_like():
    """Docker's `none` is the absence of egress, not an egress nobody owns.

    A container reports the null network once its last real endpoint is gone,
    which is the state `stop()` itself leaves behind. Reading it as an unknown
    egress network made every later stop of an already-released sandbox raise,
    and because the supervisor stops every retained sandbox while reconciling
    at startup, one such container kept the workspace broker from starting at
    all.
    """
    spec = _spec()
    manager = ChatSandboxManager(
        ContainerBackend(image="img", network="unused", egress=_isolated_policy())
    )
    inspection = _stopped_on(manager, spec, "none")
    manager._inspect_network = lambda _name: pytest.fail(  # type: ignore[method-assign]
        "the null network must not be inspected"
    )
    manager._disconnect_endpoint = lambda *_a, **_k: pytest.fail(  # type: ignore[method-assign]
        "there is no endpoint to disconnect from the null network"
    )

    assert manager._release_sandbox_networks(spec, inspection) is inspection


def test_a_live_network_this_chat_does_not_own_is_still_an_ownership_violation():
    """The repair above is about absence, and must not weaken the guard."""
    spec = _spec()
    policy = _isolated_policy()
    manager = ChatSandboxManager(ContainerBackend(image="img", network="unused", egress=policy))
    inspection = _stopped_on(manager, spec, "someone-elses-bridge")
    manager._inspect_network = lambda _name: {  # type: ignore[method-assign]
        "Internal": True,
        "Labels": {CHAT_NETWORK_LABEL: "true"},
    }

    with pytest.raises(SandboxError, match="unknown egress network"):
        manager._release_sandbox_networks(spec, inspection)


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
        manager = ChatSandboxManager(
            ContainerBackend(image="img", network="unused", egress=_isolated_policy())
        )
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

        monkeypatch.setattr(manager, "_ensure_phase_network", lambda *_args: None)
        monkeypatch.setattr(manager, "inspect", _inspect)
        monkeypatch.setattr(manager, "_require_phase_network", lambda *_args: created)
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
