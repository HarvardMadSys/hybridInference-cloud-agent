"""A new chat's source is cloned while its container starts, not after.

Booting the guest and cloning the repository were back to back on every new
chat, though only the import needs the container. What has to hold now that
they overlap: the clone really runs during the activation, the turn imports
exactly what was prepared, a failed clone fails the turn at its checkout as
before, a turn that stops early leaves nothing behind, and a follow-up, whose
workspace already holds the repository, clones nothing.
"""

from __future__ import annotations

import io
import os
import tarfile
import threading
from dataclasses import replace
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any
from unittest.mock import Mock

import pytest
from tests.unit.test_agent_runner_worktree import remote as remote
from tests.unit.test_runner_host_settings import _JOB, _stamp

from cloud_agent_host import runner as runner_mod
from cloud_agent_host.chat_sandbox import CHAT_WORKDIR
from cloud_agent_host.runner import WorktreeError
from cloud_agent_host.runtimes import get_runtime
from cloud_agent_host.sandbox import ContainerBackend, SandboxError
from cloud_agent_host.session_supervisor_client import SessionSupervisorError

if TYPE_CHECKING:
    from collections.abc import Callable

_BASE = "c" * 40
_ARCHIVE = b"normalized source tar"


class Source:
    """The host-side clone, scripted: what it did, and when it may finish."""

    def __init__(self, *, error: Exception | None = None, hold: bool = False) -> None:
        self.error = error
        self.started = threading.Event()
        self.release = threading.Event()
        if not hold:
            self.release.set()
        self.workdirs: list[str] = []
        self.archives: list[Any] = []
        self.limits: list[int] = []

    def prepare_worktree(self, *, workdir: str, **_kwargs: Any) -> str:
        self.workdirs.append(workdir)
        self.started.set()
        assert self.release.wait(5), "the test never let the clone finish"
        if self.error is not None:
            raise self.error
        return _BASE

    def build_source_archive(self, _source: str, archive: Any, **kwargs: Any) -> None:
        self.limits.append(kwargs["max_bytes"])
        self.archives.append(archive)
        archive.write(_ARCHIVE)


def _prefetch_threads() -> list[threading.Thread]:
    return [thread for thread in threading.enumerate() if thread.name == "source-prefetch"]


def _run_turn(
    monkeypatch,
    source: Source,
    *,
    has_context: bool = False,
    before_acquire: Callable[[], None] | None = None,
) -> SimpleNamespace:
    """Drive one shared turn with the clone scripted and everything else mocked."""
    monkeypatch.setattr(runner_mod, "prepare_worktree", source.prepare_worktree)
    monkeypatch.setattr(runner_mod, "checkout_output_branch", lambda *_args: None)
    monkeypatch.setattr(runner_mod, "build_source_archive", source.build_source_archive)
    backend = ContainerBackend(image="img")
    monkeypatch.setattr(backend, "version_stamp", _stamp)
    imported: list[tuple[Any, str, bytes]] = []
    manager = Mock()
    manager.backend = backend
    manager.import_archive.side_effect = lambda spec, path, archive: imported.append(
        (spec, path, archive.read())
    )
    retained = Mock()
    retained.sandbox_metadata.return_value = {}
    retained.has_binary.return_value = True
    monkeypatch.setattr(runner_mod, "ChatSandboxManager", lambda _backend: manager)
    monkeypatch.setattr(runner_mod, "RetainedChatBackend", lambda *_args, **_kwargs: retained)
    monkeypatch.setattr(runner_mod, "_resident_base_ready", lambda *_args: True)
    monkeypatch.setattr(runner_mod, "runtime_version_unavailable", lambda *_args: None)
    control = Mock(spec=runner_mod.ControlPlane)
    control.mark_chat_sandbox_running.return_value = {}
    control.reserve_chat_sandbox.side_effect = lambda seed: {
        **seed,
        "thread_id": _JOB.thread_id,
        "generation": 1,
        "activation_generation": 3,
        "resident_slot_id": 0,
        **(
            {"last_settled_job_id": "ajob_0", "native_session_id": "native-1"}
            if has_context
            else {}
        ),
    }
    supervisor = Mock()
    lease = {
        "token": "lease",
        "container_id": "same-container",
        "activation_generation": 3,
        "context_status": "live" if has_context else "new",
        "resident_state": "live",
    }

    def acquire(*_args: Any, **_kwargs: Any) -> dict[str, Any]:
        if before_acquire is not None:
            before_acquire()
        return lease

    supervisor.acquire_shared.side_effect = acquire
    supervisor.ensure_shared_resident.return_value = {
        "context_status": "live" if has_context else "new",
        "process_generation": 1,
    }
    supervisor.settle_shared.return_value = {"native_session_id": "native-1"}
    monkeypatch.setattr(runner_mod, "_session_supervisor_from_env", lambda: supervisor)
    monkeypatch.setattr(
        runner_mod, "_stream_shared_agent_turn", lambda **_kwargs: (0, "", [], None)
    )
    monkeypatch.setenv("AGENT_SHARED_RESIDENT_CAPACITY", "8")
    monkeypatch.setenv("AGENT_CHAT_IDLE_TIMEOUT_S", "1800")
    finish = Mock()
    result = runner_mod._run_shared_live_attempt(
        runtime=get_runtime("claude-code"),
        job=replace(_JOB),
        control=control,
        heart=SimpleNamespace(),
        backend=backend,
        host="host-a",
        gateway_base_url="http://gateway",
        agent_timeout_s=30,
        finish_attempt=finish,
        host_settings={"source_import_max_mib": 64},
    )
    return SimpleNamespace(result=result, finish=finish, imported=imported, supervisor=supervisor)


def test_a_new_chat_clones_while_its_container_starts(monkeypatch):
    source = Source()
    # Booting the guest takes seconds; the clone must already be under way.
    seen_during_activation: list[bool] = []

    turn = _run_turn(
        monkeypatch,
        source,
        before_acquire=lambda: seen_during_activation.append(source.started.wait(5)),
    )

    assert seen_during_activation == [True]
    assert turn.result == 0
    [(_spec, path, content)] = turn.imported
    assert (path, content) == (CHAT_WORKDIR, _ARCHIVE), "the import is what was prepared"
    assert turn.finish.call_args.kwargs["base_sha"] == _BASE
    assert source.limits == [64 * 1024 * 1024], "the turn's import limit reaches the archive"
    assert source.archives[0].closed
    assert not os.path.exists(source.workdirs[0]), "the clone's directory is removed"


def test_a_failed_clone_fails_the_turn_at_its_checkout(monkeypatch):
    """The same failure, from the same stage, as when the clone came after the boot."""
    source = Source(error=WorktreeError("could not fetch o/n@" + "a" * 40 + ": not found"))

    turn = _run_turn(monkeypatch, source)

    assert turn.result == 2
    assert turn.supervisor.acquire_shared.called
    assert turn.imported == []
    status, detail = turn.finish.call_args.args[:2]
    assert status == "failed"
    assert "could not fetch o/n@" in detail


@pytest.mark.parametrize("clone_finished_first", [True, False])
def test_a_turn_that_stops_before_its_checkout_leaves_nothing_behind(
    monkeypatch, clone_finished_first
):
    """Whether the clone was done or still running when the turn gave up."""
    source = Source(hold=not clone_finished_first)

    def stop() -> None:
        assert source.started.wait(5)
        if clone_finished_first:
            for thread in _prefetch_threads():
                thread.join(5)
        raise SessionSupervisorError(503, "shared Session Supervisor is unavailable")

    turn = _run_turn(monkeypatch, source, before_acquire=stop)
    source.release.set()
    for thread in _prefetch_threads():
        thread.join(5)

    assert turn.result == 2
    assert turn.imported == []
    assert [archive.closed for archive in source.archives] == [True]
    assert not os.path.exists(source.workdirs[0])


def test_a_follow_up_clones_nothing(monkeypatch):
    source = Source()

    turn = _run_turn(monkeypatch, source, has_context=True)

    assert turn.result == 0
    assert source.workdirs == []
    assert turn.imported == []


def test_a_discarded_source_cannot_be_imported(monkeypatch):
    source = Source()
    monkeypatch.setattr(runner_mod, "prepare_worktree", source.prepare_worktree)
    monkeypatch.setattr(runner_mod, "checkout_output_branch", lambda *_args: None)
    monkeypatch.setattr(runner_mod, "build_source_archive", source.build_source_archive)
    prefetch = runner_mod._prefetch_retained_source(job=_JOB, uid=1000, gid=1000, max_bytes=1)
    for thread in _prefetch_threads():
        thread.join(5)

    prefetch.discard()

    assert source.archives[0].closed
    with pytest.raises(SandboxError, match="already discarded"):
        prefetch.import_into(Mock(), Mock())


def test_the_real_clone_and_archive_run_on_the_prefetch_thread(monkeypatch, remote):
    """No scripted clone: real git against a local origin, imported as a real tar."""
    base, repo, first, _head = remote
    monkeypatch.setattr(runner_mod, "repo_clone_base_url", lambda **_kwargs: base)
    prefetch = runner_mod._prefetch_retained_source(
        job=replace(_JOB, repo=repo, base_sha=first), uid=1000, gid=1000
    )
    imported: list[bytes] = []
    manager = Mock()
    manager.import_archive.side_effect = lambda _spec, _path, archive: imported.append(
        archive.read()
    )

    assert prefetch.import_into(manager, Mock()) == first

    with tarfile.open(fileobj=io.BytesIO(imported[0])) as archive:
        names = archive.getnames()
    assert any(name.endswith("hello.py") for name in names)
    assert not any(name.endswith("second.txt") for name in names), "the pinned base, not the tip"
