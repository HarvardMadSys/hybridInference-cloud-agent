"""The Kilo version whose MCP isolation patch was reviewed, its fail-closed check, and its copy."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from cloud_agent_host import tree_copy
from cloud_agent_host.kilo_mcp import KILO_MCP_PATCH_SHA256, KILO_MCP_VERSION, ensure_kilo_runtime
from cloud_agent_host.runtime_images import SOURCE_LABEL, WORK_LABEL
from cloud_agent_host.sandbox import SandboxError
from cloud_agent_shared.shared_protocol import SHARED_SANDBOX_RUNTIME_PINS

NAMESPACE = "freeinference-cloud-agent-sandbox-staging"
IMAGE = "freeinference-cloud-agent-job:staging"
PREFIX = f"/opt/runtimes/kilo/{KILO_MCP_VERSION}"


def test_the_pinned_kilo_is_the_reviewed_isolation_build() -> None:
    """A Kilo pin bump must fail here until the isolation patch is reviewed against it.

    ``KILO_MCP_VERSION`` is a literal rather than the pin, so a bump cannot
    silently become the reviewed version. Review
    ``deploy/sandbox/kilo-project-config.patch`` against the new version, then
    update ``KILO_MCP_VERSION`` to match.
    """
    assert SHARED_SANDBOX_RUNTIME_PINS["kilo"] == KILO_MCP_VERSION


def test_an_unreviewed_kilo_version_is_refused_before_the_sandbox_is_touched() -> None:
    """Any other version fails closed; ``None`` stands in for a sandbox never reached."""
    with pytest.raises(SandboxError, match="no reviewed MCP isolation build"):
        ensure_kilo_runtime(None, None, f"{KILO_MCP_VERSION}.1")


# ── copying the patched build into a retained chat ────────────────────────


class FakeManager:
    """The retained-sandbox manager's surface the copy uses."""

    def __init__(self, *, activate_exit: int = 0) -> None:
        self.backend = SimpleNamespace(
            docker_binary="docker", image=IMAGE, sandbox_namespace=NAMESPACE
        )
        self.activate_exit = activate_exit
        self.execs: list[tuple[list[str], int | None]] = []
        self.runs: list[tuple[list[str], float]] = []
        self.copies: list[tuple[list[str], dict[str, Any]]] = []

    def exec_capture(self, _spec, argv, *, user_uid=None, **_kwargs):
        self.execs.append((argv, user_uid))
        if argv[0] == "/bin/sh":
            # The chat still runs the build its image shipped.
            return SimpleNamespace(returncode=1, stderr=b"")
        if argv[0] == "/usr/bin/python3":
            return SimpleNamespace(returncode=self.activate_exit, stderr=b"not protected")
        return SimpleNamespace(returncode=0, stderr=b"")

    def _require(self, _spec):
        return SimpleNamespace(container_id="chat-container")

    def _run(self, argv, *, timeout):
        self.runs.append((argv, timeout))
        return SimpleNamespace(returncode=0, stdout="source-container\n", stderr="")

    def _run_bytes(self, argv, *, timeout, **streams):
        self.copies.append((argv, streams))
        return SimpleNamespace(returncode=0, stdout=b"", stderr=b"")


@pytest.fixture
def readers(monkeypatch) -> list[Any]:
    """Each ``docker cp`` started to read out of a source container; every tree arrives."""
    started: list[Any] = []

    class Reader:
        def __init__(self, argv: list[str], **_kwargs: Any) -> None:
            self.argv = argv
            self.stdout = SimpleNamespace(close=lambda: None)
            started.append(self)

        def wait(self, timeout: float | None = None) -> int:
            return 0

    monkeypatch.setattr(tree_copy.subprocess, "Popen", Reader)
    return started


def _stage(manager: FakeManager) -> str:
    """The directory the copy made in the chat."""
    (made,) = (argv for argv, _uid in manager.execs if argv[0] == "mkdir")
    return made[-1]


def test_the_patched_build_streams_in_from_a_networkless_source_and_is_activated(readers):
    manager = FakeManager()

    ensure_kilo_runtime(manager, None, KILO_MCP_VERSION)

    # A stopped container of the deployment's image, with no network, marked
    # as a copy source and never as install work.
    assert manager.runs[0] == (
        [
            "docker",
            "create",
            "--network",
            "none",
            "--label",
            f"{WORK_LABEL}=",
            "--label",
            f"{SOURCE_LABEL}={NAMESPACE}",
            "--entrypoint",
            "/bin/true",
            IMAGE,
        ],
        60,
    )
    # Streamed from it into the stage, never held in the runner's memory.
    stage = _stage(manager)
    assert stage.startswith("/opt/runtimes/kilo/.mcp-install-")
    (reader,) = readers
    assert reader.argv == ["docker", "cp", f"source-container:{PREFIX}", "-"]
    assert manager.copies == [
        (["docker", "cp", "-", f"chat-container:{stage}"], {"stdin": reader.stdout})
    ]
    # Activated only as the reviewed build, by root.
    (activate,) = (argv for argv, _uid in manager.execs if argv[0] == "/usr/bin/python3")
    assert activate[-3:] == [f"{stage}/{KILO_MCP_VERSION}", PREFIX, KILO_MCP_PATCH_SHA256]
    assert {uid for _argv, uid in manager.execs} == {0}
    # Then the stage and the source go.
    assert manager.execs[-1][0] == ["rm", "-rf", stage]
    assert manager.runs[-1][0] == ["docker", "rm", "source-container"]


def test_a_build_that_fails_its_activation_leaves_neither_stage_nor_source(readers):
    manager = FakeManager(activate_exit=1)

    with pytest.raises(SandboxError, match="could not activate the retained Kilo MCP runtime"):
        ensure_kilo_runtime(manager, None, KILO_MCP_VERSION)

    assert manager.execs[-1][0] == ["rm", "-rf", _stage(manager)]
    assert manager.runs[-1][0] == ["docker", "rm", "source-container"]
