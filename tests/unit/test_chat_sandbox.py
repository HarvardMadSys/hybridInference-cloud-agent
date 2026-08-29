"""Contract tests for Docker-owned restartable chat containers."""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from cloud_agent_host.chat_sandbox import (
    ACTIVATION_TMPFS,
    CHAT_IMAGE_DIGEST_LABEL,
    CHAT_SANDBOX_ID_LABEL,
    CHAT_SANDBOX_LABEL,
    CHAT_STATE_DIR,
    CHAT_THREAD_LABEL,
    CHAT_WORKDIR,
    ChatSandboxManager,
    ChatSandboxSpec,
    ChatSandboxStatus,
    RetainedChatBackend,
    _RetainedTerminalProcess,
)
from cloud_agent_host.egress import EgressPolicy, EgressTier
from cloud_agent_host.sandbox import ContainerBackend, SandboxError


def _spec() -> ChatSandboxSpec:
    return ChatSandboxSpec(
        thread_id="athr_01",
        sandbox_id="asbx_01",
        generation=3,
        runtime_id="claude-code",
        runtime_version="2.1.220",
        sandbox_image_digest="sha256:image-pinned",
    )


def _inspect_payload(
    spec: ChatSandboxSpec,
    *,
    status: str = "created",
    running: bool = False,
    paused: bool = False,
    labels: dict[str, str] | None = None,
    mounts: list[dict] | None = None,
    networks: tuple[str, ...] = ("agent-net",),
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
    assert f"{CHAT_THREAD_LABEL}=athr_01" in joined
    assert f"{CHAT_SANDBOX_ID_LABEL}=asbx_01" in joined
    assert f"{CHAT_IMAGE_DIGEST_LABEL}=sha256:image-pinned" in joined
    assert command[-3] == "sha256:image-pinned"
    assert f"{ACTIVATION_TMPFS}:rw,nosuid,nodev,noexec" in joined
    assert "--network agent-net" in joined
    assert "--cap-drop ALL" in joined
    assert "--security-opt no-new-privileges" in joined
    assert "--pids-limit 512" in joined


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


def test_retained_terminal_keeps_the_shell_in_the_docker_tty_process_group(monkeypatch):
    spec = _spec()
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


def test_setup_network_is_replaced_by_agent_network_before_agent_exec(monkeypatch):
    spec = _spec()
    policy = EgressPolicy(
        tiers={"setup": EgressTier.TRUSTED, "agent": EgressTier.PLATFORM_ONLY},
        networks={
            EgressTier.TRUSTED: "setup-net",
            EgressTier.PLATFORM_ONLY: "agent-net",
        },
    )
    manager = ChatSandboxManager(ContainerBackend(image="img", network="unused", egress=policy))
    networks = {"setup-net"}
    calls: list[list[str]] = []

    def fake_run(argv, **_kwargs):
        calls.append(list(argv))
        if argv[1:3] == ["container", "inspect"]:
            return SimpleNamespace(
                returncode=0,
                stdout=_inspect_payload(
                    spec,
                    status="running",
                    running=True,
                    networks=tuple(networks),
                ),
                stderr="",
            )
        if argv[1:3] == ["network", "connect"]:
            networks.add("agent-net")
            return SimpleNamespace(returncode=0, stdout="", stderr="")
        if argv[1:3] == ["network", "disconnect"]:
            networks.remove("setup-net")
            return SimpleNamespace(returncode=0, stdout="", stderr="")
        raise AssertionError(argv)

    monkeypatch.setattr("cloud_agent_host.chat_sandbox.subprocess.run", fake_run)

    create_command = manager.build_create_command(spec)
    transitioned = manager.transition_to_agent_network(spec)

    assert create_command[create_command.index("--network") + 1] == "setup-net"
    assert transitioned.networks == ("agent-net",)
    assert ["docker", "network", "connect", "agent-net", "sha256:container-one"] in calls
    assert ["docker", "network", "disconnect", "setup-net", "sha256:container-one"] in calls


def test_commit_uses_dockers_pause_barrier_and_returns_the_image_id(monkeypatch):
    spec = _spec()
    manager = ChatSandboxManager(ContainerBackend(image="img", network="agent-net"))
    payload = _inspect_payload(spec, status="exited", running=False)
    calls: list[list[str]] = []

    def fake_run(argv, **_kwargs):
        calls.append(list(argv))
        if argv[1:3] == ["container", "inspect"]:
            return SimpleNamespace(returncode=0, stdout=payload, stderr="")
        return SimpleNamespace(
            returncode=0,
            stdout=(
                f"Flag --pause has been deprecated, and enabled by default.\nsha256:{'f' * 64}\n"
            ),
            stderr="",
        )

    monkeypatch.setattr("cloud_agent_host.chat_sandbox.subprocess.run", fake_run)

    assert manager.commit(spec, "freeinference-fork:operation-1") == f"sha256:{'f' * 64}"
    assert calls[-1] == [
        "docker",
        "commit",
        "--pause=true",
        "sha256:container-one",
        "freeinference-fork:operation-1",
    ]


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
