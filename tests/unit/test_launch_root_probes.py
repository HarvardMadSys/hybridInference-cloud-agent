"""Root probes before a member's CLI starts are not repeated once settled.

Every launch asked a retained container, as root and under its install lock,
whether a downloaded runtime version is copied in, whether Kilo carries its
isolation patch and whether the MCP adapters match this host's. Each answer
is a root-owned file no member can change and only a platform exec as root
writes, so once found it holds for the container's life: it is asked once per
proven container, and a launch whose answer is known does not wait behind an
install into the same container.
"""

from __future__ import annotations

import threading
from types import SimpleNamespace
from typing import Any

import pytest

from cloud_agent_host import mcp_helpers, runtime_install
from cloud_agent_host.chat_sandbox import ChatSandboxManager, ChatSandboxSpec
from cloud_agent_host.chat_sandbox_inspect import container_inspection
from cloud_agent_host.install_locks import install_lock
from cloud_agent_host.kilo_mcp import KILO_MCP_PATCH_SHA256, KILO_MCP_VERSION
from cloud_agent_host.sandbox import ContainerBackend, SandboxError

_MARKER = f"/opt/runtimes/claude-code/2.1.300/{runtime_install.MARKER}"
_KILO_MARKER = f"/opt/runtimes/kilo/{KILO_MCP_VERSION}/mcp-isolation-patch.sha256"


def _spec() -> ChatSandboxSpec:
    return ChatSandboxSpec(
        thread_id="athr_01",
        sandbox_id="asbx_01",
        generation=3,
        runtime_id="claude-code",
        runtime_version="2.1.300",
        sandbox_image_digest="sha256:image-pinned",
    )


def _running(container_id: str) -> Any:
    return container_inspection(
        {"Id": container_id, "Name": "/chat", "State": {"Status": "running", "Running": True}}
    )


def _manager(monkeypatch, answers: list[int] | None = None):
    """A manager whose execs answer ``answers`` in order, then succeed."""
    manager = ChatSandboxManager(ContainerBackend(image="image", sandbox_namespace="ns"))
    monkeypatch.setattr(manager.backend, "image_menu", lambda: {"claude-code": ["2.1.220"]})
    execs: list[tuple[list[str], int | None]] = []
    pending = list(answers or [])

    def exec_capture(_spec, argv, **kwargs):
        execs.append((argv, kwargs.get("user_uid")))
        code = pending.pop(0) if pending else 0
        return SimpleNamespace(returncode=code, stdout=b"", stderr=b"")

    monkeypatch.setattr(manager, "exec_capture", exec_capture)
    return manager, execs


def test_a_downloaded_version_found_in_a_proven_container_is_not_probed_again(monkeypatch):
    manager, execs = _manager(monkeypatch)
    spec = _spec()
    manager._proven.record(spec, _running("container-one"))

    manager.ensure_downloaded_runtime(spec, "claude-code", "2.1.300")
    manager.ensure_downloaded_runtime(spec, "claude-code", "2.1.300")

    ((probe, uid),) = execs
    assert uid == 0
    assert probe[-2:] == [_MARKER, "2.1.300"]


def test_another_version_has_a_marker_of_its_own(monkeypatch):
    manager, execs = _manager(monkeypatch)
    spec = _spec()
    manager._proven.record(spec, _running("container-one"))

    manager.ensure_downloaded_runtime(spec, "claude-code", "2.1.300")
    manager.ensure_downloaded_runtime(spec, "claude-code", "2.1.301")
    manager.ensure_downloaded_runtime(spec, "claude-code", "2.1.301")

    assert [argv[-1] for argv, _uid in execs] == ["2.1.300", "2.1.301"]


def test_an_unproven_or_replaced_container_is_probed_again(monkeypatch):
    manager, execs = _manager(monkeypatch)
    spec = _spec()

    manager.ensure_downloaded_runtime(spec, "claude-code", "2.1.300")
    manager.ensure_downloaded_runtime(spec, "claude-code", "2.1.300")
    assert len(execs) == 2, "without a proven container there is nothing to remember it by"

    manager._proven.record(spec, _running("container-one"))
    manager.ensure_downloaded_runtime(spec, "claude-code", "2.1.300")
    manager._proven.forget(spec)
    manager._proven.record(spec, _running("container-two"))
    manager.ensure_downloaded_runtime(spec, "claude-code", "2.1.300")
    manager.ensure_downloaded_runtime(spec, "claude-code", "2.1.300")

    assert len(execs) == 4


def test_a_version_the_image_holds_is_neither_probed_nor_remembered(monkeypatch):
    manager, execs = _manager(monkeypatch)
    spec = _spec()
    proven = manager._proven.record(spec, _running("container-one"))

    manager.ensure_downloaded_runtime(spec, "claude-code", "2.1.220")

    assert execs == []
    assert manager._proven._helpers == set()
    assert not manager._proven.holds_helper(proven, "/opt/runtimes/claude-code/2.1.220")


def test_only_a_copy_that_succeeded_is_remembered(monkeypatch):
    """The copy itself is ``test_runtime_downloads``'s; here it fails once, then succeeds."""
    manager, _execs = _manager(monkeypatch)
    spec = _spec()
    manager._proven.record(spec, _running("container-one"))
    copies: list[str] = []

    def copy(_manager, _spec, runtime_id, version):
        copies.append(version)
        if len(copies) == 1:
            raise SandboxError(f"could not activate {runtime_id} {version} in this chat")

    monkeypatch.setattr(runtime_install, "ensure_downloaded_runtime", copy)

    with pytest.raises(SandboxError, match="could not activate"):
        manager.ensure_downloaded_runtime(spec, "claude-code", "2.1.300")
    manager.ensure_downloaded_runtime(spec, "claude-code", "2.1.300")
    manager.ensure_downloaded_runtime(spec, "claude-code", "2.1.300")

    assert copies == ["2.1.300", "2.1.300"]


def test_a_known_copy_does_not_wait_behind_an_install_into_its_container(monkeypatch):
    """Runtime and MCP installs hold the container's install lock for minutes."""
    manager, execs = _manager(monkeypatch)
    spec = _spec()
    proven = manager._proven.record(spec, _running("container-one"))
    monkeypatch.setattr(manager, "_require", lambda _spec: proven)
    manager.ensure_downloaded_runtime(spec, "claude-code", "2.1.300")
    manager.ensure_kilo_mcp_runtime(spec, KILO_MCP_VERSION)
    manager.ensure_mcp_helpers(spec, "kilo")
    settled = len(execs)

    with install_lock(spec.container_name):
        launch = threading.Thread(
            target=lambda: (
                manager.ensure_downloaded_runtime(spec, "claude-code", "2.1.300"),
                manager.ensure_kilo_mcp_runtime(spec, KILO_MCP_VERSION),
                manager.ensure_mcp_helpers(spec, "kilo"),
                manager.ensure_mcp_helpers(spec, "claude-code"),
            ),
            daemon=True,
        )
        launch.start()
        launch.join(5)
        assert not launch.is_alive(), "a settled answer waited for the install lock"
    assert len(execs) == settled


def test_kilos_patch_marker_is_probed_once_per_proven_container(monkeypatch):
    manager, execs = _manager(monkeypatch)
    spec = _spec()
    manager._proven.record(spec, _running("container-one"))

    manager.ensure_kilo_mcp_runtime(spec, KILO_MCP_VERSION)
    manager.ensure_kilo_mcp_runtime(spec, KILO_MCP_VERSION)

    ((probe, uid),) = execs
    assert uid == 0
    assert probe[-2:] == [_KILO_MARKER, KILO_MCP_PATCH_SHA256]


def test_an_unreviewed_kilo_version_is_refused_every_time(monkeypatch):
    manager, execs = _manager(monkeypatch)
    spec = _spec()
    manager._proven.record(spec, _running("container-one"))

    for _ in range(2):
        with pytest.raises(SandboxError, match="no reviewed MCP isolation build"):
            manager.ensure_kilo_mcp_runtime(spec, f"{KILO_MCP_VERSION}.1")
    assert execs == []


def test_mcp_adapters_found_in_place_are_not_probed_again(monkeypatch):
    manager, execs = _manager(monkeypatch)
    spec = _spec()
    proven = manager._proven.record(spec, _running("container-one"))
    monkeypatch.setattr(manager, "_require", lambda _spec: proven)

    manager.ensure_mcp_helpers(spec, "opencode")
    manager.ensure_mcp_helpers(spec, "opencode")

    assert [argv[4] for argv, _uid in execs] == [
        "/usr/local/bin/opencode-freeinference",
        "/usr/local/bin/http-agent-live-bridge",
    ]
    assert {uid for _argv, uid in execs} == {0}


def test_a_runtime_without_adapters_takes_neither_the_lock_nor_an_inspect(monkeypatch):
    manager, execs = _manager(monkeypatch)
    spec = _spec()
    monkeypatch.setattr(manager, "_require", lambda _spec: pytest.fail("nothing to install"))

    with install_lock(spec.container_name):
        manager.ensure_mcp_helpers(spec, "claude-code")
        manager.ensure_mcp_helpers(spec, "codex")

    assert execs == []


def test_an_adapter_is_remembered_with_its_content(monkeypatch):
    """A host that ships other content installs it, and forgets what was there before."""
    manager, execs = _manager(monkeypatch, answers=[0, 1, 0, 1, 0])
    spec = _spec()
    proven = manager._proven.record(spec, _running("container-one"))
    monkeypatch.setattr(manager, "_require", lambda _spec: proven)
    copies: list[list[str]] = []
    monkeypatch.setattr(
        manager,
        "_run_bytes",
        lambda argv, **_kwargs: copies.append(argv) or SimpleNamespace(returncode=0, stderr=b""),
    )
    shipped = [b"first"]

    def helpers(_runtime):
        return [mcp_helpers._Helper("/usr/local/bin/kilo-freeinference", 0o555, shipped[0])]

    monkeypatch.setattr(mcp_helpers, "_helpers", helpers)

    manager.ensure_mcp_helpers(spec, "kilo")  # found
    shipped[0] = b"second"
    manager.ensure_mcp_helpers(spec, "kilo")  # differs: copied in, moved over
    manager.ensure_mcp_helpers(spec, "kilo")  # known
    shipped[0] = b"first"
    manager.ensure_mcp_helpers(spec, "kilo")  # replaced since: probed, copied back

    assert len(execs) == 5
    assert len(copies) == 2
    (current,) = mcp_helpers._helpers("kilo")
    assert manager._proven.holds_helper(proven, current.key)
    assert [key for _id, key in manager._proven._helpers] == [current.key]


def test_a_failed_adapter_install_is_not_remembered(monkeypatch):
    manager, execs = _manager(monkeypatch, answers=[1, 1, 0])
    spec = _spec()
    proven = manager._proven.record(spec, _running("container-one"))
    monkeypatch.setattr(manager, "_require", lambda _spec: proven)
    monkeypatch.setattr(
        manager, "_run_bytes", lambda argv, **_kwargs: SimpleNamespace(returncode=0, stderr=b"")
    )
    monkeypatch.setattr(
        mcp_helpers,
        "_helpers",
        lambda _runtime: [mcp_helpers._Helper("/usr/local/bin/kilo-freeinference", 0o555, b"x")],
    )

    with pytest.raises(SandboxError, match="could not activate the kilo MCP adapter"):
        manager.ensure_mcp_helpers(spec, "kilo")
    manager.ensure_mcp_helpers(spec, "kilo")
    manager.ensure_mcp_helpers(spec, "kilo")

    assert len(execs) == 3, "probe, failed move; then found and remembered"
