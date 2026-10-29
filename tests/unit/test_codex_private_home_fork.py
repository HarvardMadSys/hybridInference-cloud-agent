"""Focused regression coverage for Codex private-home fork materialization."""

from __future__ import annotations

import sqlite3
import stat
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest
from test_shared_session_supervisor import (
    _assert_private,
    _launch,
    _LocalMemberShell,
    _Manager,
    _private_state_root,
    _spec,
)

from cloud_agent_host.chat_sandbox import MEMBER_UID_BASE
from cloud_agent_host.drivers import DriverError
from cloud_agent_host.resident_transport import ResidentTransportFactory


def test_codex_fork_rebases_indexed_rollouts_into_the_child_home(tmp_path: Path):
    runtime_id = "codex"
    _private_state_root(tmp_path)
    manager = _LocalMemberShell(tmp_path)
    factory = ResidentTransportFactory(manager)  # type: ignore[arg-type]
    source_launch = replace(
        _launch("athr_source", native_session_id="native-source", slot=0), runtime_id=runtime_id
    )
    target_launch = replace(
        _launch("athr_child", native_session_id=None, slot=2), runtime_id=runtime_id
    )
    source_home = factory._member_state_dir(source_launch)
    factory._prepare_member_state(_spec(), source_launch, source_home)
    source = manager.local(source_home)
    (source / "sessions").mkdir()
    (source / "sessions/rollout.jsonl").write_text('{"id":"native-source"}\n')
    if runtime_id == "codex":
        index = sqlite3.connect(source / "state_5.sqlite")
        index.execute("CREATE TABLE threads (id TEXT PRIMARY KEY, rollout_path TEXT NOT NULL)")
        index.executemany(
            "INSERT INTO threads (id, rollout_path) VALUES (?, ?)",
            [
                ("native-source", str(source / "sessions/rollout.jsonl")),
                ("codex fork child", str(source / "sessions/child-rollout.jsonl")),
                ("unrelated", "/outside/source/home/rollout.jsonl"),
                ("prefix collision", str(source) + "-sibling/rollout.jsonl"),
                (
                    "already target-rooted",
                    str(
                        manager.local(factory._member_state_dir(target_launch))
                        / "sessions/target.jsonl"
                    ),
                ),
            ],
        )
        index.commit()
        index.close()
        (source / "sessions/child-rollout.jsonl").write_text("child\n")
        (source / "state_5.sqlite").chmod(0o600)
        source_index_before = (source / "state_5.sqlite").read_bytes()
    (source / ".hidden").write_text("dotfile")
    (source / "tmp").mkdir(exist_ok=True)
    (source / "tmp/scratch").write_text("never copied")
    (source / f"{runtime_id}/state/{runtime_id}/locks").mkdir(parents=True)
    (source / f"{runtime_id}/state/{runtime_id}/locks/server.lock").write_text("pid")
    manager.calls.clear()

    factory._clone_private_member_state(_spec(), source_launch, target_launch)

    child = manager.local(factory._member_state_dir(target_launch))
    _assert_private(child)
    # The CLI's own files keep its umask; the closed home is what protects them.
    assert stat.S_IMODE(source.stat().st_mode) == 0o700
    assert manager.calls == [("exec", None), ("pipe", MEMBER_UID_BASE + 0, MEMBER_UID_BASE + 2)]
    assert manager.pipe_identity == (
        MEMBER_UID_BASE + 0,
        MEMBER_UID_BASE + 2,
        manager.backend.gid,
    )
    assert (child / "sessions/rollout.jsonl").read_text() == '{"id":"native-source"}\n'
    assert (source / "state_5.sqlite").read_bytes() == source_index_before
    child_index = sqlite3.connect(f"file:{child / 'state_5.sqlite'}?mode=ro", uri=True)
    paths = dict(child_index.execute("SELECT id, rollout_path FROM threads"))
    assert child_index.execute("PRAGMA integrity_check").fetchone() == ("ok",)
    child_index.close()
    assert paths["native-source"] == str(child / "sessions/rollout.jsonl")
    assert paths["codex fork child"] == str(child / "sessions/child-rollout.jsonl")
    assert paths["unrelated"] == "/outside/source/home/rollout.jsonl"
    assert paths["prefix collision"] == str(source) + "-sibling/rollout.jsonl"
    assert paths["already target-rooted"] == str(
        manager.local(factory._member_state_dir(target_launch)) / "sessions/target.jsonl"
    )
    assert Path(paths["native-source"]).read_text() == '{"id":"native-source"}\n'
    assert Path(paths["codex fork child"]).read_text() == "child\n"
    Path(paths["codex fork child"]).write_text("child changed\n")
    assert (source / "sessions/child-rollout.jsonl").read_text() == "child\n"
    assert (child / ".hidden").read_text() == "dotfile"
    assert list((child / "tmp").iterdir()) == []
    assert (child / ".freeinference-member-state-v1").exists()
    locks = child / f"{runtime_id}/state/{runtime_id}/locks"
    assert locks.exists() is (runtime_id == "codex")
    assert (source / "tmp/scratch").exists(), "the source home is read, never changed"


def test_codex_rollout_rebase_failure_rolls_back_target_index(tmp_path: Path):
    _private_state_root(tmp_path)
    manager = _LocalMemberShell(tmp_path)
    factory = ResidentTransportFactory(manager)  # type: ignore[arg-type]
    source_launch = replace(
        _launch("athr_source", native_session_id="native-source", slot=0), runtime_id="codex"
    )
    target_launch = replace(
        _launch("athr_child", native_session_id=None, slot=2), runtime_id="codex"
    )
    source_dir = manager.local(factory._member_state_dir(source_launch))
    factory._prepare_member_state(_spec(), source_launch, factory._member_state_dir(source_launch))
    (source_dir / "sessions").mkdir()
    rollout = source_dir / "sessions/rollout.jsonl"
    rollout.write_text("source rollout\n")
    index_path = source_dir / "state_5.sqlite"
    index = sqlite3.connect(index_path)
    index.execute("CREATE TABLE threads (id TEXT PRIMARY KEY, rollout_path TEXT NOT NULL)")
    index.executemany(
        "INSERT INTO threads (id, rollout_path) VALUES (?, ?)",
        [("first", str(rollout)), ("second", str(rollout))],
    )
    index.execute(
        "CREATE TRIGGER reject_second_path BEFORE UPDATE OF rollout_path ON threads "
        "WHEN OLD.id = 'second' BEGIN SELECT RAISE(ABORT, 'injected rebase failure'); END"
    )
    index.commit()
    index.close()
    index_before = index_path.read_bytes()

    with pytest.raises(DriverError, match="state could not be materialized"):
        factory._clone_private_member_state(_spec(), source_launch, target_launch)

    assert index_path.read_bytes() == index_before
    child_db = manager.local(factory._member_state_dir(target_launch)) / "state_5.sqlite"
    child_index = sqlite3.connect(child_db)
    paths = dict(child_index.execute("SELECT id, rollout_path FROM threads"))
    child_index.close()
    assert paths == {"first": str(rollout), "second": str(rollout)}
    assert rollout.read_text() == "source rollout\n"


def test_codex_rollout_rebase_rejects_parent_traversal(tmp_path: Path):
    _private_state_root(tmp_path)
    manager = _LocalMemberShell(tmp_path)
    factory = ResidentTransportFactory(manager)  # type: ignore[arg-type]
    source_launch = replace(
        _launch("athr_source", native_session_id="native-source", slot=0), runtime_id="codex"
    )
    target_launch = replace(
        _launch("athr_child", native_session_id=None, slot=2), runtime_id="codex"
    )
    factory._prepare_member_state(_spec(), source_launch, factory._member_state_dir(source_launch))
    source = manager.local(factory._member_state_dir(source_launch))
    (source / "sessions").mkdir()
    (source / "sessions/rollout.jsonl").write_text("source rollout\n")
    escaped = str(source / "../outside/rollout.jsonl")
    index_path = source / "state_5.sqlite"
    index = sqlite3.connect(index_path)
    index.execute("CREATE TABLE threads (id TEXT PRIMARY KEY, rollout_path TEXT NOT NULL)")
    index.execute("INSERT INTO threads (id, rollout_path) VALUES (?, ?)", ("escape", escaped))
    index.commit()
    index.close()
    source_index_before = index_path.read_bytes()

    with pytest.raises(DriverError, match="state could not be materialized"):
        factory._clone_private_member_state(_spec(), source_launch, target_launch)

    assert index_path.read_bytes() == source_index_before
    child_index = sqlite3.connect(
        manager.local(factory._member_state_dir(target_launch)) / "state_5.sqlite"
    )
    assert child_index.execute(
        "SELECT rollout_path FROM threads WHERE id = 'escape'"
    ).fetchone() == (escaped,)
    child_index.close()


def test_codex_rollout_rebase_rejects_symlink_escape_after_partial_update(
    tmp_path: Path, monkeypatch
):
    _private_state_root(tmp_path)
    manager = _LocalMemberShell(tmp_path)
    factory = ResidentTransportFactory(manager)  # type: ignore[arg-type]
    source_launch = replace(
        _launch("athr_source", native_session_id="native-source", slot=0), runtime_id="codex"
    )
    control_launch = replace(
        _launch("athr_control", native_session_id=None, slot=1), runtime_id="codex"
    )
    target_launch = replace(
        _launch("athr_child", native_session_id=None, slot=2), runtime_id="codex"
    )
    factory._prepare_member_state(_spec(), source_launch, factory._member_state_dir(source_launch))
    source = manager.local(factory._member_state_dir(source_launch))
    (source / "sessions").mkdir()
    rollout = source / "sessions/rollout.jsonl"
    rollout.write_text("valid internal rollout\n")

    index_path = source / "state_5.sqlite"
    index = sqlite3.connect(index_path)
    index.execute("CREATE TABLE threads (id TEXT PRIMARY KEY, rollout_path TEXT NOT NULL)")
    index.execute(
        "INSERT INTO threads (id, rollout_path) VALUES (?, ?)", ("internal", str(rollout))
    )
    index.commit()
    index.close()

    # A normal in-home path remains forkable; the escape below must not make
    # the containment check reject legitimate rollouts in general.
    factory._clone_private_member_state(_spec(), source_launch, control_launch)
    control = manager.local(factory._member_state_dir(control_launch))
    control_index = sqlite3.connect(f"file:{control / 'state_5.sqlite'}?mode=ro", uri=True)
    control_path = control_index.execute(
        "SELECT rollout_path FROM threads WHERE id = 'internal'"
    ).fetchone()[0]
    control_index.close()
    assert control_path == str(control / "sessions/rollout.jsonl")
    assert Path(control_path).read_text() == "valid internal rollout\n"

    external_dir = tmp_path / "outside-child-home"
    external_dir.mkdir()
    external_rollout = external_dir / "rollout.jsonl"
    external_rollout.write_text("must remain untouched\n")
    (source / "sessions/escape").symlink_to(external_dir, target_is_directory=True)
    escaped_path = str(source / "sessions/escape/rollout.jsonl")
    index = sqlite3.connect(index_path)
    index.execute(
        "INSERT INTO threads (id, rollout_path) VALUES (?, ?)", ("symlink-escape", escaped_path)
    )
    index.commit()
    assert [row[0] for row in index.execute("SELECT id, rollout_path FROM threads")] == [
        "internal",
        "symlink-escape",
    ]
    index.close()
    source_index_before = index_path.read_bytes()
    external_before = external_rollout.read_bytes()

    journal_writes: list[dict[str, object]] = []
    monkeypatch.setattr(factory, "_read_fork_journal", lambda *_args: None)
    monkeypatch.setattr(
        factory,
        "_write_fork_journal",
        lambda _spec, _operation_id, entry: journal_writes.append(dict(entry)),
    )
    monkeypatch.setattr(factory, "_codex_fork_children", lambda *_args, **_kwargs: set())
    manager.normalize_member_files = lambda *_args, **_kwargs: None
    lifecycle: list[str] = []
    fork_source = SimpleNamespace(
        native_session_id="native-source",
        launch=source_launch,
        session=SimpleNamespace(
            fork_native=lambda **_kwargs: lifecycle.append("fork") or "native-child"
        ),
        close=lambda: lifecycle.append("close"),
    )

    with pytest.raises(DriverError, match="state could not be materialized"):
        factory.fork_native(
            _spec(), fork_source, "fork-symlink-escape", target_launch, source_native_turn_no=1
        )

    # The child fork fails closed after the native child exists: the journal
    # stays PREPARING, and the method returns no partial-success child id.
    assert [entry["state"] for entry in journal_writes] == ["preparing"]
    assert lifecycle == ["fork", "close"]
    assert index_path.read_bytes() == source_index_before
    assert rollout.read_text() == "valid internal rollout\n"
    assert (source / "sessions/escape").is_symlink()
    assert (source / "sessions/escape").resolve() == external_dir
    assert external_rollout.read_bytes() == external_before

    child = manager.local(factory._member_state_dir(target_launch))
    child_symlink_rollout = child / "sessions/escape/rollout.jsonl"
    assert child_symlink_rollout.is_relative_to(child)
    assert child_symlink_rollout.resolve() == external_rollout
    child_index = sqlite3.connect(f"file:{child / 'state_5.sqlite'}?mode=ro", uri=True)
    paths = dict(child_index.execute("SELECT id, rollout_path FROM threads"))
    assert child_index.execute("PRAGMA integrity_check").fetchone() == ("ok",)
    child_index.close()
    # The valid first row was updated before the symlink failure, but closing
    # the uncommitted SQLite transaction restores both source-rooted paths.
    assert paths == {"internal": str(rollout), "symlink-escape": escaped_path}


def test_codex_rebase_failure_leaves_preparing_journal_for_idempotent_recovery(monkeypatch):
    manager = _Manager()
    factory = ResidentTransportFactory(manager)  # type: ignore[arg-type]
    spec = replace(_spec(), runtime_id="codex", runtime_version="0.145.0")
    source_launch = replace(
        _launch("athr_source", native_session_id="native-source", slot=0),
        runtime_id="codex",
        runtime_version="0.145.0",
    )
    target_launch = replace(
        _launch("athr_child", native_session_id=None, slot=2),
        runtime_id="codex",
        runtime_version="0.145.0",
    )
    lifecycle: list[str] = []
    source = SimpleNamespace(
        native_session_id="native-source",
        launch=source_launch,
        session=SimpleNamespace(
            fork_native=lambda **_kwargs: lifecycle.append("fork") or "native-child"
        ),
        close=lambda: lifecycle.append("close"),
    )
    journal: dict[str, object] = {}
    writes: list[dict[str, object]] = []
    scans = 0
    clone_attempts = 0

    def read_journal(_spec, _operation_id):
        return dict(journal) if journal else None

    def write_journal(_spec, _operation_id, value):
        journal.clear()
        journal.update(value)
        writes.append(dict(value))

    def child_inventory(*_args, **_kwargs):
        nonlocal scans
        scans += 1
        return set() if scans == 1 else {"native-child"}

    def clone(_spec, _source, _target):
        nonlocal clone_attempts
        clone_attempts += 1
        if clone_attempts == 1:
            raise DriverError("Codex fork state could not be materialized for the child")

    monkeypatch.setattr(factory, "_read_fork_journal", read_journal)
    monkeypatch.setattr(factory, "_write_fork_journal", write_journal)
    monkeypatch.setattr(factory, "_codex_fork_children", child_inventory)
    monkeypatch.setattr(factory, "_clone_private_member_state", clone)

    with pytest.raises(DriverError, match="could not be materialized"):
        factory.fork_native(spec, source, "fork-rebase-failure", target_launch, 2)

    assert [entry["state"] for entry in writes] == ["preparing"]
    assert lifecycle == ["fork", "close"]

    child_id = factory.fork_native(spec, source, "fork-rebase-failure", target_launch, 2)

    assert child_id == "native-child"
    assert lifecycle == ["fork", "close", "close"]
    assert clone_attempts == 2
    assert [entry["state"] for entry in writes] == ["preparing", "ready"]
