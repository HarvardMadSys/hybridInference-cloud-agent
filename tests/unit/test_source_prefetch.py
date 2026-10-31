"""A new group handed a snapshot no longer waits for the source archive.

The group chooses its installation snapshot by the checkout's dependency
manifests before it is created. That choice used to wait for the whole
prepared source, archive included, so the archive was built before the
snapshot load and the boot instead of beside them. The manifests are now
published as soon as Git lists them. What must still hold: a preparation that
fails, before the listing or after it, fails the turn at its checkout as
before; one that is discarded stops and leaves nothing behind; and the turn
imports exactly what was prepared.
"""

from __future__ import annotations

import os
import threading
import time
from dataclasses import replace
from types import SimpleNamespace
from typing import Any, BinaryIO
from unittest.mock import Mock

import pytest
from tests.unit.test_runner_host_settings import _JOB, _stamp

from cloud_agent_host import runner as runner_mod
from cloud_agent_host.chat_sandbox import CHAT_WORKDIR
from cloud_agent_host.runner import WorktreeError
from cloud_agent_host.runtimes import get_runtime
from cloud_agent_host.sandbox import ContainerBackend
from cloud_agent_host.source_prefetch import SourcePrefetch
from cloud_agent_shared.environment import BoundEnvironment
from cloud_agent_shared.environment_snapshots import (
    EnvironmentSnapshotRef,
    owner_hash,
    snapshot_object_key,
)
from cloud_agent_shared.git.source_cancellation import (
    SourcePreparationCancelled,
    check_source_stop,
)

_BASE = "c" * 40
_ARCHIVE = b"normalized source tar"
_LOCKFILES = {"uv.lock": "d" * 40}
_IMAGE = "ghcr.io/harvardmadsys/cloud-agent-sandbox@sha256:" + "a" * 64
_OWNER = owner_hash("alice")
_SNAPSHOT = EnvironmentSnapshotRef.model_validate(
    {
        "snapshot_id": "5" * 32,
        "owner": _OWNER,
        "object_key": snapshot_object_key(_OWNER, "5" * 32),
        "sha256": "e" * 64,
        "size_bytes": 10,
        "base_image": _IMAGE,
        "source_sha": _BASE,
        "lockfiles": _LOCKFILES,
        "built_at": "2026-10-15T00:00:00Z",
    }
)
_OFFERED_JOB = replace(
    _JOB,
    environment=BoundEnvironment(
        binding_id="task-binding", setup_script="install", setup_fingerprint="f" * 64
    ),
    environment_snapshot=_SNAPSHOT,
)


class _Archive:
    """An archive step that holds until the test releases it, or is told to stop."""

    def __init__(self, *, error: BaseException | None = None) -> None:
        self.error = error
        self.started = threading.Event()
        self.release = threading.Event()
        self.finished = threading.Event()
        self.outs: list[BinaryIO] = []

    def build(self, out: BinaryIO, stop: threading.Event) -> None:
        self.outs.append(out)
        self.started.set()
        deadline = time.monotonic() + 5
        while not self.release.wait(0.01):
            check_source_stop(stop)
            assert time.monotonic() < deadline, "the test never let the archive finish"
        if self.error is not None:
            raise self.error
        out.write(_ARCHIVE)
        self.finished.set()


class _Scripted(SourcePrefetch):
    """The prefetch's lifecycle with each step scripted."""

    def __init__(
        self,
        archive: _Archive,
        *,
        git_lists: bool = True,
        clone_error: BaseException | None = None,
    ) -> None:
        self.archive = archive
        self.git_lists = git_lists
        self.clone_error = clone_error
        self.workdirs: list[str] = []
        super().__init__()

    def _check_out(self, workdir: str, stop: threading.Event) -> str:
        self.workdirs.append(workdir)
        if self.clone_error is not None:
            raise self.clone_error
        return _BASE

    def _list_lockfiles(self, workdir: str, stop: threading.Event) -> dict[str, str] | None:
        return dict(_LOCKFILES) if self.git_lists else None

    def _build_archive(self, workdir: str, out: BinaryIO, stop: threading.Event) -> None:
        self.archive.build(out, stop)


def _imported(prefetch: SourcePrefetch) -> tuple[str, list[bytes]]:
    imported: list[bytes] = []
    manager = Mock()
    manager.import_archive.side_effect = lambda _spec, path, archive: imported.append(
        archive.read() if path == CHAT_WORKDIR else b"elsewhere"
    )
    return prefetch.import_into(manager, Mock()), imported


def test_the_manifests_are_answered_before_the_archive_is_built():
    archive = _Archive()
    prefetch = _Scripted(archive)

    assert prefetch.lockfiles() == _LOCKFILES
    assert not archive.finished.is_set() and not prefetch._done.is_set()

    archive.release.set()
    assert _imported(prefetch) == (_BASE, [_ARCHIVE])
    assert archive.outs[0].closed
    assert not os.path.exists(prefetch.workdirs[0])


def test_a_clone_that_fails_answers_no_manifests_and_fails_the_import():
    error = WorktreeError("could not fetch o/n@" + "a" * 40 + ": not found")
    archive = _Archive()
    prefetch = _Scripted(archive, clone_error=error)

    assert prefetch.lockfiles() is None
    with pytest.raises(WorktreeError) as raised:
        _imported(prefetch)
    assert raised.value is error
    assert not archive.started.is_set()


def test_an_archive_that_fails_after_the_listing_fails_the_import_as_before():
    """The manifests were good; the source was not, and the turn learns it at its checkout."""
    error = WorktreeError("source archive exceeds this host's retained import size limit")
    archive = _Archive(error=error)
    prefetch = _Scripted(archive)

    assert prefetch.lockfiles() == _LOCKFILES
    archive.release.set()
    with pytest.raises(WorktreeError) as raised:
        _imported(prefetch)

    assert raised.value is error
    assert archive.outs[0].closed, "the failed archive's spool is closed"
    assert prefetch.lockfiles() is None, "asked after the failure, as before: nothing"
    assert not os.path.exists(prefetch.workdirs[0])


def test_a_discard_while_the_archive_is_built_stops_it_and_leaves_nothing():
    archive = _Archive()
    prefetch = _Scripted(archive)
    assert prefetch.lockfiles() == _LOCKFILES
    assert archive.started.wait(5)

    prefetch.discard()

    assert not prefetch._thread.is_alive()
    assert prefetch._done.is_set()
    assert not archive.finished.is_set()
    assert archive.outs[0].closed
    assert not os.path.exists(prefetch.workdirs[0])
    with pytest.raises(SourcePreparationCancelled):
        _imported(prefetch)


def test_manifests_git_cannot_list_answer_none_and_the_source_still_imports():
    archive = _Archive()
    archive.release.set()
    prefetch = _Scripted(archive, git_lists=False)

    assert prefetch.lockfiles() is None
    assert _imported(prefetch) == (_BASE, [_ARCHIVE])


def _script_runner_steps(monkeypatch, archive: _Archive) -> list[str]:
    """The runner's own subclass, with its Git steps and archive scripted."""
    workdirs: list[str] = []

    def clone(*, workdir: str, **_kwargs: Any) -> str:
        workdirs.append(workdir)
        return _BASE

    monkeypatch.setattr(runner_mod, "prepare_worktree", clone)
    monkeypatch.setattr(runner_mod, "checkout_output_branch", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(runner_mod, "tracked_lockfiles", lambda *_args, **_kwargs: _LOCKFILES)
    monkeypatch.setattr(
        runner_mod,
        "build_source_archive",
        lambda _source, out, *, stop, **_kwargs: archive.build(out, stop),
    )
    return workdirs


def test_a_snapshot_offer_is_chosen_while_the_archive_is_still_being_built(monkeypatch):
    archive = _Archive()
    _script_runner_steps(monkeypatch, archive)
    prefetch = runner_mod._prefetch_retained_source(
        job=_OFFERED_JOB, uid=1000, gid=1000, max_bytes=1 << 20
    )

    chosen = runner_mod._choose_environment_snapshot(
        _OFFERED_JOB, SimpleNamespace(sandbox_image_digest=_IMAGE), prefetch, new_group=True
    )

    assert chosen == (_SNAPSHOT, None)
    assert not archive.finished.is_set()
    archive.release.set()
    assert _imported(prefetch) == (_BASE, [_ARCHIVE])


def test_a_new_group_offered_a_snapshot_starts_while_its_archive_is_built(monkeypatch):
    """Through the turn: the acquire that loads the snapshot does not wait for the archive."""
    archive = _Archive()
    workdirs = _script_runner_steps(monkeypatch, archive)
    backend = ContainerBackend(image="img")
    monkeypatch.setattr(
        backend, "version_stamp", lambda: {**_stamp(), "sandbox_image_digest": _IMAGE}
    )
    imported: list[tuple[str, bytes]] = []
    manager = Mock()
    manager.backend = backend
    manager.import_archive.side_effect = lambda _spec, path, out: imported.append(
        (path, out.read())
    )
    retained = Mock()
    retained.sandbox_metadata.return_value = {}
    retained.has_binary.return_value = True
    monkeypatch.setattr(runner_mod, "ChatSandboxManager", lambda _backend: manager)
    monkeypatch.setattr(runner_mod, "RetainedChatBackend", lambda *_args, **_kwargs: retained)
    monkeypatch.setattr(runner_mod, "runtime_version_unavailable", lambda *_args: None)
    control = Mock(spec=runner_mod.ControlPlane)
    control.mark_chat_sandbox_running.return_value = {}
    control.reserve_chat_sandbox.side_effect = lambda seed: {
        **seed,
        "thread_id": _JOB.thread_id,
        "generation": 1,
        "activation_generation": 3,
        "resident_slot_id": 0,
    }
    asked: list[tuple[Any, bool]] = []

    def acquire(*_args: Any, **kwargs: Any) -> dict[str, Any]:
        asked.append((kwargs["environment_snapshot"], archive.finished.is_set()))
        archive.release.set()
        return {
            "token": "lease",
            "container_id": "new-container",
            "activation_generation": 3,
            "context_status": "new",
            "resident_state": "live",
            "environment_snapshot_id": _SNAPSHOT.snapshot_id,
        }

    supervisor = Mock()
    supervisor.acquire_shared.side_effect = acquire
    supervisor.initial_archive_status.return_value = {
        "enabled": False,
        "prepared": False,
        "stored": False,
    }
    supervisor.capture_initial_archive.return_value = {"enabled": False, "stored": False}
    supervisor.prepare_environment.return_value = {}
    supervisor.ensure_shared_resident.return_value = {
        "context_status": "new",
        "process_generation": 1,
    }
    supervisor.settle_shared.return_value = {"native_session_id": "native-1"}
    monkeypatch.setattr(runner_mod, "_session_supervisor_from_env", lambda: supervisor)
    monkeypatch.setattr(
        runner_mod, "_stream_shared_agent_turn", lambda **_kwargs: (0, "", [], None, None)
    )
    monkeypatch.setenv("AGENT_SHARED_RESIDENT_CAPACITY", "8")
    monkeypatch.setenv("AGENT_CHAT_IDLE_TIMEOUT_S", "1800")
    finish = Mock()

    result = runner_mod._run_shared_live_attempt(
        runtime=get_runtime("claude-code"),
        job=_OFFERED_JOB,
        control=control,
        heart=SimpleNamespace(cancel_requested=False, lease_lost=False),
        backend=backend,
        host="host-a",
        agent_timeout_s=30,
        finish_attempt=finish,
        host_settings={"source_import_max_mib": 64},
    )

    assert asked == [(_SNAPSHOT.model_dump(mode="json"), False)]
    assert result == 0
    assert imported == [(CHAT_WORKDIR, _ARCHIVE)]
    assert finish.call_args.kwargs["base_sha"] == _BASE
    control.report_environment_snapshot.assert_called_once_with(
        snapshot_id=_SNAPSHOT.snapshot_id, outcome="used", base_sha=_BASE
    )
    assert archive.outs[0].closed
    assert not os.path.exists(workdirs[0])
