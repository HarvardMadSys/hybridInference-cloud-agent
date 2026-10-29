"""Discarded prefetch work stops before another file, chunk, or Git command."""

from __future__ import annotations

import io
import os
import pathlib
import subprocess
import sys
import threading
import time
from unittest.mock import Mock

import pytest
from tests.unit.test_agent_runner_worktree import _assert_ended, _child_started, _slow_git
from tests.unit.test_runner_host_settings import _JOB
from tests.unit.test_runner_source_prefetch import Source, _prefetch_threads, _run_turn

from cloud_agent_host import runner
from cloud_agent_host.host_runtime import LAUNCH_STARTED, LaunchResult, TurnPool
from cloud_agent_shared.git import source_archive, source_cancellation
from cloud_agent_shared.git.source_cancellation import SourcePreparationCancelled


def _checkout(tmp_path):
    (tmp_path / ".git").mkdir()
    (tmp_path / "payload").write_bytes(b"x" * (128 * 1024))
    return tmp_path


@pytest.mark.parametrize("stage", ["traversal", "metadata", "copy"])
def test_archive_cancellation_stops_before_the_next_entry_or_chunk(tmp_path, monkeypatch, stage):
    root = _checkout(tmp_path)
    stop = threading.Event()
    consumed: list[str] = []
    out = io.BytesIO()
    if stage == "traversal":

        def entries(_root, **kwargs):
            yield str(root / ".git")
            stop.set()
            yield str(root / "payload")
            consumed.append("walked after cancellation")

        monkeypatch.setattr(source_archive, "_entries", entries)
    elif stage == "metadata":
        # The builder stats each walked path with os.lstat. Whichever of the
        # two top-level entries the directory lists first is cancelled after;
        # a stat of the other one is the violation, in either listing order.
        lstat = source_archive.os.lstat
        first: list[pathlib.Path] = []

        def metadata(path, *args, **kwargs):
            result = lstat(path, *args, **kwargs)
            entry = pathlib.Path(os.fsdecode(path))
            if entry.parent == root and entry.name in {".git", "payload"}:
                if not first:
                    first.append(entry)
                    stop.set()
                elif entry != first[0]:
                    consumed.append("statted after cancellation")
            return result

        monkeypatch.setattr(source_archive.os, "lstat", metadata)
    else:

        class StopOnContent(io.BytesIO):
            def write(self, data):
                if data == b"x" * (16 * 1024):
                    consumed.append("content chunk")
                    stop.set()
                return super().write(data)

        out = StopOnContent()

    with pytest.raises(SourcePreparationCancelled, match="abandoned"):
        source_archive.build_source_archive(str(root), out, uid=1000, gid=1000, stop=stop)

    assert consumed == (["content chunk"] if stage == "copy" else [])
    if stage != "copy":
        assert out.getvalue() == b""


def test_an_already_cancelled_archive_does_not_scan_the_checkout(tmp_path, monkeypatch):
    stop = threading.Event()
    stop.set()
    entries = Mock(side_effect=AssertionError("cancelled archive must not traverse"))
    monkeypatch.setattr(source_archive, "_entries", entries)
    with pytest.raises(SourcePreparationCancelled):
        source_archive.build_source_archive(str(tmp_path), io.BytesIO(), uid=1, gid=1, stop=stop)
    entries.assert_not_called()


def test_cancel_during_enumeration_does_not_read_a_whole_large_directory(tmp_path, monkeypatch):
    root = _checkout(tmp_path)
    for index in range(10000):
        (root / f"tiny-{index}").touch()
    stop = threading.Event()
    scandir = source_cancellation.os.scandir
    consumed = 0

    class Scan:
        def __init__(self, directory):
            self.entries = scandir(directory)

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            self.close()

        def close(self):
            self.entries.close()

        def __iter__(self):
            return self

        def __next__(self):
            nonlocal consumed
            entry = next(self.entries)
            consumed += 1
            if consumed == 200:
                stop.set()
            return entry

    monkeypatch.setattr(source_cancellation.os, "scandir", Scan)
    with pytest.raises(SourcePreparationCancelled):
        source_archive.build_source_archive(str(root), io.BytesIO(), uid=1, gid=1, stop=stop)
    assert consumed == 200, "os.walk consumed the whole directory before noticing cancellation"


def test_cancellable_walk_streams_every_entry_and_never_follows_symlinks(tmp_path):
    root = _checkout(tmp_path)
    (root / "z").mkdir()
    (root / "a").mkdir()
    (root / "z" / "second").touch()
    (root / "a" / "first").touch()
    (root / "directory-link").symlink_to(root / "z", target_is_directory=True)
    expected = set()
    for directory, directories, files in source_cancellation.os.walk(root, followlinks=False):
        expected.update(os.path.join(directory, name) for name in directories)
        expected.update(os.path.join(directory, name) for name in files)
    assert set(source_archive._entries(str(root), stop=threading.Event())) == expected


@pytest.mark.parametrize("stage", ["clone", "branch", "lockfiles"])
def test_discard_between_prefetch_stages_never_starts_the_next_one(monkeypatch, stage):
    started = threading.Event()
    stages: list[str] = []

    def step(name, stop):
        stages.append(name)
        if name == stage:
            started.set()
            assert stop.wait(5), "prefetch was never discarded"

    def clone(*, stop, **kwargs):
        step("clone", stop)
        return "c" * 40

    def branch(*args, stop):
        step("branch", stop)

    def lockfiles(*args, stop):
        step("lockfiles", stop)
        return {}

    monkeypatch.setattr(runner, "prepare_worktree", clone)
    monkeypatch.setattr(runner, "checkout_output_branch", branch)
    monkeypatch.setattr(runner, "tracked_lockfiles", lockfiles)
    archive = Mock(side_effect=AssertionError("discard must precede archive construction"))
    monkeypatch.setattr(runner, "build_source_archive", archive)
    source = runner._SourcePrefetch(_JOB, uid=1000, gid=1000, max_bytes=1 << 20)
    assert started.wait(5)

    source.discard()

    assert source._done.is_set()
    assert not source._thread.is_alive()
    assert (
        stages
        == ["clone", "branch", "lockfiles"][: ["clone", "branch", "lockfiles"].index(stage) + 1]
    )
    archive.assert_not_called()


@pytest.mark.parametrize("stage", ["branch", "lockfiles", "temporary"])
def test_post_fetch_git_cancellation_kills_descendants(tmp_path, monkeypatch, stage):
    child_pid = _slow_git(tmp_path, monkeypatch)
    stop = threading.Event()

    def cancel():
        deadline = time.monotonic() + 5
        while not _child_started(child_pid) and time.monotonic() < deadline:
            time.sleep(0.01)
        stop.set()

    canceller = threading.Thread(target=cancel)
    canceller.start()
    started = time.monotonic()
    try:
        with pytest.raises(SourcePreparationCancelled):
            if stage == "branch":
                runner.checkout_output_branch(str(tmp_path), "test", stop=stop)
            elif stage == "lockfiles":
                runner.tracked_lockfiles(str(tmp_path), stop=stop)
            else:
                runner.prepare_temporary_worktree(workdir=str(tmp_path), stop=stop)
    finally:
        canceller.join(timeout=5)
    assert time.monotonic() - started < 5
    assert _child_started(child_pid)
    _assert_ended(child_pid)


def _archive_failure_child():
    """Real turn cleanup + archive copying, with slow local I/O and a failed broker."""
    from cloud_agent_host.session_supervisor_client import SessionSupervisorError

    monkeypatch = pytest.MonkeyPatch()
    copying = threading.Event()
    original_copy = source_archive.tarfile.copyfileobj

    def slow_copy(src, dst, length=None, exception=OSError, bufsize=None):
        class SlowReader:
            def read(self, size):
                copying.set()
                time.sleep(0.02)
                return src.read(size)

        return original_copy(SlowReader(), dst, length, exception, bufsize)

    class RealArchiveSource(Source):
        def prepare_worktree(self, *, workdir, **kwargs):
            self.workdirs.append(workdir)
            root = pathlib.Path(workdir)
            (root / ".git").mkdir()
            (root / "large").write_bytes(b"x" * (16 * 1024 * 1024))
            return "c" * 40

        def build_source_archive(self, workdir, archive, **kwargs):
            self.archives.append(archive)
            return source_archive.build_source_archive(workdir, archive, **kwargs)

    def refuse_broker():
        assert copying.wait(5)
        raise SessionSupervisorError(503, "broker unavailable")

    monkeypatch.setattr(source_archive.tarfile, "copyfileobj", slow_copy)
    source = RealArchiveSource()
    turn = _run_turn(monkeypatch, source, before_acquire=refuse_broker)
    assert turn.result == 2
    assert source.archives and all(archive.closed for archive in source.archives)
    assert not any(pathlib.Path(path).exists() for path in source.workdirs)
    assert _prefetch_threads() == []
    print("turn failed; prefetch stopped; checkout and spool cleaned", flush=True)


def test_archive_discard_allows_normal_child_exit_and_turn_pool_reap():
    command = (
        "from tests.unit.test_source_cancellation import _archive_failure_child; "
        "_archive_failure_child()"
    )
    process = subprocess.Popen(
        [sys.executable, "-c", command], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True
    )
    pool = TurnPool(launch=lambda job: LaunchResult(outcome=LAUNCH_STARTED, handle=process))
    pool.launch({"job_id": "cancelled-source-test", "attempt_id": 1})
    assert pool.running() == 1
    try:
        stdout, stderr = process.communicate(timeout=10)
    finally:
        if process.poll() is None:
            process.kill()
            process.communicate(timeout=5)
    assert process.returncode == 0, stderr
    assert "checkout and spool cleaned" in stdout
    assert len(pool.reap()) == 1
    assert pool.running() == 0
    assert pool.may_claim(1)
