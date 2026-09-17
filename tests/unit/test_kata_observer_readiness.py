"""Regressions reproduced on Kata 3.32, beyond a mocked kernel-name check."""

import json
import logging
import os
import subprocess
import sys
from types import SimpleNamespace

import pytest

from cloud_agent_host import chat_sandbox
from cloud_agent_host.chat_sandbox import (
    GUEST_OBSERVER_PROBE_ATTEMPTS,
    MEMBER_REAPER_TARGET,
    MEMBER_UID_BASE,
    ChatSandboxInspection,
    ChatSandboxManager,
    ChatSandboxSpec,
    ChatSandboxStatus,
)
from cloud_agent_host.guest_observer import GUEST_OBSERVER_BINARY
from cloud_agent_host.sandbox import KATA_RUNTIME, ContainerBackend, SandboxError


def _manager():
    return ChatSandboxManager(
        ContainerBackend(image="test-image", runtime=KATA_RUNTIME, network="none")
    )


@pytest.mark.parametrize("actual_runtime", ["runc", "", "io.containerd.kata.v2"])
def test_privileged_exec_checks_the_container_runtime_and_uses_its_id(monkeypatch, actual_runtime):
    manager = _manager()
    spec = ChatSandboxSpec(
        thread_id="thread", sandbox_id="sandbox", generation=1, runtime_id="claude-code"
    )
    calls = []

    def run(argv, **kwargs):
        calls.append(argv)
        if argv[1:3] == ["container", "inspect"]:
            return SimpleNamespace(
                returncode=0,
                stderr="",
                stdout=json.dumps(
                    [
                        {
                            "Id": "inspected-container-id",
                            "Name": "/" + spec.container_name,
                            "Config": {"Labels": spec.docker_labels()},
                            "HostConfig": {"Runtime": actual_runtime},
                            "State": {"Running": True, "Status": "running"},
                        }
                    ]
                ),
            )
        return SimpleNamespace(returncode=0, stdout="ok", stderr="")

    monkeypatch.setattr(manager, "_run", run)
    if actual_runtime == KATA_RUNTIME:
        manager._kata_privileged_exec(spec, [GUEST_OBSERVER_BINARY, "--self-test"])
        assert "inspected-container-id" in calls[-1]
        assert spec.container_name not in calls[-1]
    else:
        with pytest.raises(SandboxError, match="inspected Kata container"):
            manager._kata_privileged_exec(spec, [GUEST_OBSERVER_BINARY, "--self-test"])
        assert not any(call[1] == "exec" for call in calls)


def test_kata_normal_exec_explicitly_sets_no_new_privileges():
    manager = _manager()
    spec = ChatSandboxSpec(
        thread_id="thread", sandbox_id="sandbox", generation=1, runtime_id="claude-code"
    )
    command = manager.build_exec_command(spec, ["sh", "-c", "id"], user_uid=11001)
    assert command[command.index(spec.container_name) + 1 :] == [
        "/usr/bin/setpriv",
        "--no-new-privs",
        "--",
        "/bin/sh",
        "-c",
        'exec "$@" </dev/null',
        "sh",
        "sh",
        "-c",
        "id",
    ]
    assert "--privileged" not in command
    create = manager.build_create_command(spec)
    caps = {create[i + 1] for i, token in enumerate(create) if token == "--cap-add"}
    assert caps == {
        "BPF",
        "PERFMON",
        "SETUID",
        "SETGID",
        "CHOWN",
        "DAC_OVERRIDE",
        "KILL",
        "SYS_PTRACE",
    }


@pytest.mark.parametrize("payload", [None, b"", b"input\x00\n"])
def test_kata_exec_delivers_eof_and_preserves_input_and_literal_arguments(monkeypatch, payload):
    manager = _manager()
    spec = ChatSandboxSpec(
        thread_id="thread", sandbox_id="sandbox", generation=1, runtime_id="claude-code"
    )
    literal = "spaces ' \" $HOME $(false) `false`\nnext line"
    real_run = subprocess.run
    monkeypatch.setattr(manager, "_require", lambda spec: SimpleNamespace(running=True))

    def docker_exec(argv, **kwargs):
        assert ("--interactive" in argv) == bool(payload)
        guest_argv = argv[argv.index(spec.container_name) + 1 :]
        assert guest_argv[:3] == ["/usr/bin/setpriv", "--no-new-privs", "--"]
        guest_argv = guest_argv[3:]
        if payload:
            return real_run(guest_argv, input=kwargs["input"], capture_output=True, timeout=2)
        # Model Kata's pipe whose writer stays open even without attached
        # input. Execute the actual wrapper: reading it must still reach EOF.
        read_fd, write_fd = os.pipe()
        try:
            return real_run(guest_argv, stdin=read_fd, capture_output=True, timeout=2)
        finally:
            os.close(read_fd)
            os.close(write_fd)

    monkeypatch.setattr("cloud_agent_host.chat_sandbox.subprocess.run", docker_exec)
    result = manager.exec_capture(
        spec,
        [
            sys.executable,
            "-c",
            "import json,sys; print(json.dumps([sys.stdin.buffer.read().hex(),sys.argv[1]]))",
            literal,
        ],
        input_bytes=payload,
    )
    assert result.returncode == 0
    assert json.loads(result.stdout) == [(payload or b"").hex(), literal]


@pytest.mark.parametrize(
    "failure",
    [
        None,
        "observer",
        "seccomp",
        "nnp",
        "effective",
        "member_cleanup",
        "marker",
        "marker_transport",
    ],
)
def test_readiness_requires_events_and_member_isolation_and_cleans_up(monkeypatch, failure):
    manager = _manager()
    calls = []
    privileged_calls = []
    cleanup_calls = []

    def run(argv, **kwargs):
        calls.append(argv)
        return SimpleNamespace(returncode=0, stdout="probe-id", stderr="")

    def capture(spec, argv, **kwargs):
        if argv[0] == MEMBER_REAPER_TARGET:
            cleanup_calls.append((argv, kwargs))
            return SimpleNamespace(
                returncode=int(failure == "member_cleanup"), stdout=b"", stderr=b"cleanup failed"
            )
        if argv[0] == "python3":
            status = {"uid": 10001, "nnp": 1, "seccomp": 2, "effective": 0, "permitted": 0}
            if failure in {"seccomp", "nnp", "effective"}:
                status[failure] = 0 if failure != "effective" else 1
            return SimpleNamespace(returncode=0, stdout=json.dumps(status).encode(), stderr=b"")
        return SimpleNamespace(
            returncode=int(failure == "marker"), stdout=b"freeinference-tool-mark-v1\n", stderr=b""
        )

    def privileged(spec, argv, **kwargs):
        privileged_calls.append(argv)
        return SimpleNamespace(
            returncode=int(failure == "observer"),
            stdout=(
                "freeinference-guest-observer-v2\n"
                '{"window":{"cpu":true,"storage_io":true,"pss":true,"counters":7}}\n'
            ),
            stderr="no lifecycle events",
        )

    monkeypatch.setattr(manager, "_run", run)
    monkeypatch.setattr(manager, "exec_capture", capture)
    monkeypatch.setattr(manager, "_kata_privileged_exec", privileged)
    # `marker_transport` is a race the probe now retries; the suite does not
    # need to sit out the interval to see the verdict.
    monkeypatch.setattr(chat_sandbox.time, "sleep", lambda _seconds: None)
    monkeypatch.setattr(
        manager, "begin_guest_observation", lambda spec, user_uid: SimpleNamespace(available=True)
    )
    monkeypatch.setattr(
        manager,
        "finish_guest_observation",
        lambda spec, cursor: SimpleNamespace(
            complete=True,
            lost_events=0,
            truncated=False,
            detail=None,
            tool_marks=()
            if failure == "marker_transport"
            else ((1.0, "pre", "preflight"), (2.0, "post", "preflight")),
        ),
    )
    if failure is None:
        manager.probe_guest_observer()
    else:
        with pytest.raises(SandboxError):
            manager.probe_guest_observer()
    assert calls[0][calls[0].index("--network") + 1] == "none"
    assert calls[-1][1:3] == ["rm", "--force"]
    created = [call for call in calls if call[1:2] == ["create"]]
    assert calls[-1][-1] == created[-1][created[-1].index("--name") + 1]
    if failure not in {"seccomp", "nnp", "effective"}:
        assert cleanup_calls[0][0] == [MEMBER_REAPER_TARGET, "reap", str(MEMBER_UID_BASE)]
        assert cleanup_calls[0][1]["input_bytes"] == b""
        assert cleanup_calls[0][1]["user_uid"] == MEMBER_UID_BASE
    if failure in {"seccomp", "nnp", "effective", "member_cleanup"}:
        assert privileged_calls == []
    else:
        # A lost race is retried on a sandbox of its own, so the self-test runs
        # once per attempt; everything else answers the same way every time and
        # is asked exactly once.
        probes = GUEST_OBSERVER_PROBE_ATTEMPTS if failure == "marker_transport" else 1
        assert privileged_calls == [[GUEST_OBSERVER_BINARY, "--self-test"]] * probes
        assert len(created) == probes


def test_backend_refuses_claim_readiness_when_observer_probe_fails(monkeypatch):
    backend = _manager().backend
    monkeypatch.setattr(
        "cloud_agent_host.sandbox.subprocess.run", lambda *a, **kw: SimpleNamespace(returncode=0)
    )
    monkeypatch.setattr(backend, "_probe_kernel_boundary", lambda: None)

    def refuse(self):
        raise SandboxError("no real lifecycle events")

    monkeypatch.setattr(ChatSandboxManager, "probe_guest_observer", refuse)
    with pytest.raises(SandboxError, match="no real lifecycle events"):
        backend.preflight()


@pytest.mark.parametrize(
    "address,alias,valid",
    [
        ("10.240.0.2/29", "agent-gateway", True),
        ("", "agent-gateway", False),
        ("not-an-ip", "agent-gateway", False),
        ("10.240.0.2/29", "agent-gateway\nforged", False),
    ],
)
def test_fixed_aliases_use_the_current_bridge_endpoint(monkeypatch, address, alias, valid):
    manager = _manager()
    monkeypatch.setattr(manager, "_service_aliases_for_phase", lambda phase: (alias,))
    monkeypatch.setattr(manager, "_service_owners", lambda base, aliases: {"relay-id": [alias]})
    monkeypatch.setattr(
        manager,
        "_inspect_network",
        lambda network: {
            "Containers": {
                "relay-id": {"IPv4Address": address},
                "unrelated": {"IPv4Address": "10.240.0.3/29"},
            }
        },
    )
    if valid:
        assert manager.fixed_service_addresses("agent", "private-bridge") == {
            "agent-gateway": "10.240.0.2"
        }
    else:
        with pytest.raises(SandboxError):
            manager.fixed_service_addresses("agent", "private-bridge")


def test_hosts_refresh_replaces_setup_aliases_and_is_idempotent(monkeypatch, tmp_path):
    manager = _manager()
    spec = ChatSandboxSpec(
        thread_id="thread", sandbox_id="sandbox", generation=1, runtime_id="claude-code"
    )
    current = ChatSandboxInspection(
        container_id="container-id",
        name=spec.container_name,
        status=ChatSandboxStatus.RUNNING,
        running=True,
        image="image",
        labels=spec.docker_labels(),
        mounts=(),
        networks=("agent-net",),
        sandbox_runtime=KATA_RUNTIME,
    )
    monkeypatch.setattr(manager, "network_for_phase", lambda spec, phase: phase + "-net")
    hosts = tmp_path / "hosts"
    hosts.write_text(
        "127.0.0.1 localhost\n# BEGIN FREEINFERENCE RELAYS\n10.1.0.2 old-proxy\n# END FREEINFERENCE RELAYS\n"
    )
    hosts.chmod(0o644)
    addresses = {"agent-gateway": "10.2.0.2", "agent-mcp": "10.2.0.3"}
    monkeypatch.setattr(manager, "fixed_service_addresses", lambda phase, network: addresses)
    actual_open, actual_stat = os.open, os.fstat
    monkeypatch.setattr(
        os, "open", lambda path, flags: actual_open(hosts if path == "/etc/hosts" else path, flags)
    )
    monkeypatch.setattr(
        os, "fstat", lambda fd: SimpleNamespace(st_uid=0, st_mode=actual_stat(fd).st_mode)
    )

    def run(spec, argv, **kwargs):
        monkeypatch.setattr("sys.argv", ["-c", argv[3]])
        exec(compile(argv[2], "<fixed-service-hosts>", "exec"), {})
        return SimpleNamespace(returncode=0, stderr="")

    monkeypatch.setattr(manager, "_kata_privileged_exec", run)
    manager._sync_kata_service_hosts(spec, current)
    first = hosts.read_text()
    assert "127.0.0.1 localhost" in first
    assert "old-proxy" not in first
    assert "10.2.0.2 agent-gateway" in first
    manager._sync_kata_service_hosts(spec, current)
    assert hosts.read_text() == first
    addresses["agent-gateway"] = "10.3.0.2"
    manager._sync_kata_service_hosts(spec, current)
    assert "10.2.0.2" not in hosts.read_text()
    assert "10.3.0.2 agent-gateway" in hosts.read_text()


def _self_test_manager(monkeypatch):
    """A manager whose privileged exec answers the observer self-test."""
    manager = _manager()
    monkeypatch.setattr(
        manager,
        "_kata_privileged_exec",
        lambda spec, argv, **kwargs: SimpleNamespace(returncode=0, stdout="", stderr=""),
    )
    return manager


def test_the_self_test_must_name_the_helper(monkeypatch):
    """An answer that is not the reviewed helper is a failed preflight."""
    manager = _self_test_manager(monkeypatch)

    with pytest.raises(SandboxError):
        manager._check_observer_self_test("some other program\n")


def test_a_guest_without_a_window_capability_is_accepted_but_reported(monkeypatch, caplog):
    """Lifecycle events still work; the missing capability is not silent."""
    manager = _self_test_manager(monkeypatch)

    with caplog.at_level(logging.WARNING):
        manager._check_observer_self_test("freeinference-guest-observer-v2\n")

    assert any(
        record.__dict__.get("event") == "guest_observer_window_capability_absent"
        for record in caplog.records
    )


def test_a_partial_window_capability_names_what_is_missing(monkeypatch, caplog):
    """A kernel that cannot read PSS must say so where an operator looks."""
    manager = _self_test_manager(monkeypatch)

    with caplog.at_level(logging.WARNING):
        manager._check_observer_self_test(
            "freeinference-guest-observer-v2\n"
            '{"window":{"cpu":true,"storage_io":true,'
            '"pss":false,"counters":3}}\n'
        )

    partial = [
        record
        for record in caplog.records
        if record.__dict__.get("event") == "guest_observer_window_capability_partial"
    ]
    assert partial and partial[0].__dict__.get("missing") == ["pss"]


def test_a_complete_capability_report_is_not_a_warning(monkeypatch, caplog):
    manager = _self_test_manager(monkeypatch)

    with caplog.at_level(logging.WARNING):
        manager._check_observer_self_test(
            "freeinference-guest-observer-v2\n"
            '{"window":{"cpu":true,"storage_io":true,'
            '"pss":true,"counters":7}}\n'
        )

    assert not [
        record
        for record in caplog.records
        if str(record.__dict__.get("event", "")).startswith("guest_observer_window")
    ]


def _probe_stubs(monkeypatch, manager, *, cursor, observations):
    """Drive one probe past every deterministic check to the observation stages.

    ``observations`` is consumed one entry per attempt, so a test can say what
    the second try sees without knowing how the probe is structured.
    """
    created = []

    def run(argv, **kwargs):
        if argv[1:2] == ["create"]:
            created.append(argv[argv.index("--name") + 1])
        return SimpleNamespace(returncode=0, stdout="probe-id", stderr="")

    def capture(spec, argv, **kwargs):
        if argv[0] == MEMBER_REAPER_TARGET:
            return SimpleNamespace(returncode=0, stdout=b"", stderr=b"")
        if argv[0] == "python3":
            status = {"uid": 10001, "nnp": 1, "seccomp": 2, "effective": 0, "permitted": 0}
            return SimpleNamespace(returncode=0, stdout=json.dumps(status).encode(), stderr=b"")
        return SimpleNamespace(returncode=0, stdout=b"freeinference-tool-mark-v1\n", stderr=b"")

    monkeypatch.setattr(manager, "_run", run)
    monkeypatch.setattr(manager, "exec_capture", capture)
    monkeypatch.setattr(
        manager,
        "_kata_privileged_exec",
        lambda spec, argv, **kwargs: SimpleNamespace(
            returncode=0,
            stdout=(
                "freeinference-guest-observer-v2\n"
                '{"window":{"cpu":true,"storage_io":true,"pss":true,"counters":7}}\n'
            ),
            stderr="",
        ),
    )
    monkeypatch.setattr(manager, "begin_guest_observation", lambda spec, user_uid: cursor)
    remaining = list(observations)
    monkeypatch.setattr(manager, "finish_guest_observation", lambda spec, cursor: remaining.pop(0))
    monkeypatch.setattr(chat_sandbox.time, "sleep", lambda _seconds: None)
    return created


def _observation(*, complete=True, marks=((1.0, "pre", "preflight"), (2.0, "post", "preflight"))):
    return SimpleNamespace(
        complete=complete, lost_events=0, truncated=False, detail=None, tool_marks=marks
    )


def test_a_lost_race_with_the_guest_is_retried_on_a_sandbox_of_its_own(monkeypatch):
    """The stages that read back from a running observer get another try.

    They race a guest that has just booted: the observer has to load its eBPF
    program, publish a first status and drain its ring before the probe can see
    the boundaries it just sent. Losing that race once is not a statement about
    the host, and it used to be treated as one -- `cmd_up` leaves a host
    advertising no deployment when the probe fails, and a runner advertising no
    deployment does not poll, so staging went quiet for 29.6 hours on
    2026-09-13 over a probe nothing retried.
    """
    manager = _manager()
    created = _probe_stubs(
        monkeypatch,
        manager,
        cursor=SimpleNamespace(available=True, detail=None),
        observations=[_observation(marks=()), _observation()],
    )

    manager.probe_guest_observer()

    assert len(created) == 2, "the second attempt must not reuse the first probe's sandbox"
    assert created[0] != created[1]


def test_a_host_that_keeps_losing_the_race_is_still_refused(monkeypatch):
    """Retrying bounds the flake; it must not paper over a broken host."""
    manager = _manager()
    created = _probe_stubs(
        monkeypatch,
        manager,
        cursor=SimpleNamespace(available=True, detail=None),
        observations=[_observation(marks=())] * GUEST_OBSERVER_PROBE_ATTEMPTS,
    )

    with pytest.raises(SandboxError, match=f"after {GUEST_OBSERVER_PROBE_ATTEMPTS} attempts"):
        manager.probe_guest_observer()

    assert len(created) == GUEST_OBSERVER_PROBE_ATTEMPTS


@pytest.mark.parametrize(
    ("observation", "expected"),
    [
        (_observation(marks=()), r"marks=\[\]"),
        (_observation(complete=False), "complete=False"),
    ],
    ids=["boundaries-never-arrived", "slice-could-not-be-read"],
)
def test_the_boundary_failure_says_which_half_failed(monkeypatch, observation, expected):
    """One sentence used to cover two unrelated failures.

    That is why the 2026-09-13 rollout could not be diagnosed from its logs: an
    incomplete slice means the journal could not be read back, while a complete
    slice carrying the wrong marks means the boundaries never arrived. They want
    opposite investigations.
    """
    manager = _manager()
    _probe_stubs(
        monkeypatch,
        manager,
        cursor=SimpleNamespace(available=True, detail=None),
        observations=[observation] * GUEST_OBSERVER_PROBE_ATTEMPTS,
    )

    with pytest.raises(SandboxError, match=expected):
        manager.probe_guest_observer()


def test_an_unavailable_cursor_carries_the_reason_it_reported(monkeypatch):
    """`begin` folds every reason into one flag, so the detail must travel."""
    manager = _manager()
    _probe_stubs(
        monkeypatch,
        manager,
        cursor=SimpleNamespace(available=False, detail="guest observer is not ready"),
        observations=[],
    )

    with pytest.raises(SandboxError, match="guest observer is not ready"):
        manager.probe_guest_observer()
