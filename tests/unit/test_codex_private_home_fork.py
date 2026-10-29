"""Focused regression coverage for Codex private-home fork materialization."""

from __future__ import annotations

import sqlite3
import stat
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest
from tests.unit.test_shared_session_supervisor import (
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


def _indexed_home(home: Path, rows: dict[str, str]) -> None:
    """Write the Codex session index a CLI leaves beside its rollout files."""
    index = sqlite3.connect(home / "state_5.sqlite")
    index.execute("CREATE TABLE threads (id TEXT PRIMARY KEY, rollout_path TEXT NOT NULL)")
    index.executemany("INSERT INTO threads (id, rollout_path) VALUES (?, ?)", sorted(rows.items()))
    index.commit()
    index.close()


def _paths(home: Path) -> dict[str, str]:
    index = sqlite3.connect(f"file:{home / 'state_5.sqlite'}?mode=ro", uri=True)
    try:
        return dict(index.execute("SELECT id, rollout_path FROM threads"))
    finally:
        index.close()


def test_codex_fork_reroots_sibling_rollouts_into_the_child_home(tmp_path: Path):
    _private_state_root(tmp_path)
    manager = _LocalMemberShell(tmp_path)
    factory = ResidentTransportFactory(manager)  # type: ignore[arg-type]
    source_launch = replace(
        _launch("athr_source", native_session_id="native-source", slot=0), runtime_id="codex"
    )
    target_launch = replace(
        _launch("athr_child", native_session_id=None, slot=2), runtime_id="codex"
    )
    source_home = factory._member_state_dir(source_launch)
    factory._prepare_member_state(_spec(), source_launch, source_home)
    source = manager.local(source_home)
    child_home = manager.local(factory._member_state_dir(target_launch))
    (source / "sessions").mkdir()
    (source / "sessions/rollout.jsonl").write_text('{"id":"native-source"}\n')
    _indexed_home(
        source,
        {
            "native-source": str(source / "sessions/rollout.jsonl"),
            "codex fork child": str(source / "sessions/child-rollout.jsonl"),
            # Neither of these is another member's home: one is outside the
            # collection, the other merely shares the prefix.
            "unrelated": "/outside/source/home/rollout.jsonl",
            "prefix collision": str(source) + "-sibling/rollout.jsonl",
            "already target-rooted": str(child_home / "sessions/target.jsonl"),
        },
    )
    (source / "sessions/child-rollout.jsonl").write_text("child\n")
    (source / "state_5.sqlite").chmod(0o600)
    source_index_before = (source / "state_5.sqlite").read_bytes()
    (source / ".hidden").write_text("dotfile")
    (source / "tmp").mkdir(exist_ok=True)
    (source / "tmp/scratch").write_text("never copied")
    (source / "codex/state/codex/locks").mkdir(parents=True)
    (source / "codex/state/codex/locks/server.lock").write_text("pid")
    manager.calls.clear()

    factory._clone_private_member_state(_spec(), source_launch, target_launch)

    child = child_home
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
    paths = _paths(child)
    assert paths["native-source"] == str(child / "sessions/rollout.jsonl")
    assert paths["codex fork child"] == str(child / "sessions/child-rollout.jsonl")
    assert paths["unrelated"] == "/outside/source/home/rollout.jsonl"
    assert paths["prefix collision"] == str(source) + "-sibling/rollout.jsonl"
    assert paths["already target-rooted"] == str(child / "sessions/target.jsonl")
    assert Path(paths["native-source"]).read_text() == '{"id":"native-source"}\n'
    assert Path(paths["codex fork child"]).read_text() == "child\n"
    Path(paths["codex fork child"]).write_text("child changed\n")
    assert (source / "sessions/child-rollout.jsonl").read_text() == "child\n"
    assert (child / ".hidden").read_text() == "dotfile"
    assert list((child / "tmp").iterdir()) == []
    assert (child / ".freeinference-member-state-v1").exists()
    # Codex keeps its project locks: unlike the HTTP runtimes, the CLI is the
    # only process in this home, so a lock naming a dead PID is not a conflict.
    assert (child / "codex/state/codex/locks").exists()
    assert (source / "tmp/scratch").exists(), "the source home is read, never changed"


def test_codex_start_repairs_a_home_forked_by_an_earlier_release(tmp_path: Path):
    """A child copied before this fix is repaired the next time it starts."""
    _private_state_root(tmp_path)
    manager = _LocalMemberShell(tmp_path)
    factory = ResidentTransportFactory(manager)  # type: ignore[arg-type]
    sibling_launch = replace(
        _launch("athr_sibling", native_session_id="native-sibling", slot=0), runtime_id="codex"
    )
    launch = replace(
        _launch("athr_child", native_session_id="native-child", slot=2), runtime_id="codex"
    )
    sibling_home = manager.local(factory._member_state_dir(sibling_launch))
    home_dir = factory._member_state_dir(launch)
    home = manager.local(home_dir)
    (home / "sessions").mkdir(parents=True)
    (home / "sessions/rollout.jsonl").write_text("rollout\n")
    # Exactly what the closed copy left behind: rows still rooted in the sibling.
    _indexed_home(
        home,
        {
            "native-child": str(sibling_home / "sessions/rollout.jsonl"),
            "kept": "/outside/home/rollout.jsonl",
        },
    )

    factory._prepare_member_state(_spec(), launch, home_dir)
    assert _paths(home)["native-child"] == str(home / "sessions/rollout.jsonl")
    assert _paths(home)["kept"] == "/outside/home/rollout.jsonl"

    # Starting again is a no-op, and "no-op" must not mean "rewritten twice".
    factory._prepare_member_state(_spec(), launch, home_dir)
    assert _paths(home)["native-child"] == str(home / "sessions/rollout.jsonl")


@pytest.mark.parametrize(
    "damage",
    ["no index", "no threads table", "no sibling rows"],
)
def test_codex_rebase_leaves_a_home_with_nothing_to_rebase_alone(tmp_path: Path, damage: str):
    """An index the CLI has not written yet is not a fork failure."""
    _private_state_root(tmp_path)
    manager = _LocalMemberShell(tmp_path)
    factory = ResidentTransportFactory(manager)  # type: ignore[arg-type]
    sibling_launch = replace(
        _launch("athr_sibling", native_session_id="native-sibling", slot=0), runtime_id="codex"
    )
    launch = replace(
        _launch("athr_child", native_session_id="native-child", slot=2), runtime_id="codex"
    )
    factory._prepare_member_state(
        _spec(), sibling_launch, factory._member_state_dir(sibling_launch)
    )
    home = manager.local(factory._member_state_dir(launch))
    home.mkdir(parents=True)
    index = home / "state_5.sqlite"
    before = None
    if damage == "no threads table":
        connection = sqlite3.connect(index)
        connection.execute("CREATE TABLE other (x)")
        connection.commit()
        connection.close()
        before = index.read_bytes()
    elif damage == "no sibling rows":
        _indexed_home(home, {"own": "/outside/home/rollout.jsonl"})
        before = index.read_bytes()

    factory._prepare_member_state(_spec(), launch, factory._member_state_dir(launch))

    if damage == "no index":
        assert not index.exists()
    else:
        # Byte-identical, not merely present: "nothing to do" must mean the
        # copy was not rewritten on its way to being left alone.
        assert index.read_bytes() == before


def test_codex_start_never_fails_over_the_index_it_cannot_repair(tmp_path: Path):
    """A member's own start has no retry, so the index may not fail it.

    Each of these is a state a real Codex home can reach -- an index the CLI is
    still writing, one that reports an integrity complaint (SQLite still serves
    it), one a member may not write, one whose rebase a trigger undoes. The CLI
    is about to read the same file and can report it far better than a start
    refusal that would repeat unchanged on every later attempt.
    """
    _private_state_root(tmp_path)
    manager = _LocalMemberShell(tmp_path)
    factory = ResidentTransportFactory(manager)  # type: ignore[arg-type]
    sibling_launch = replace(
        _launch("athr_sibling", native_session_id="native-sibling", slot=0), runtime_id="codex"
    )
    launch = replace(
        _launch("athr_child", native_session_id="native-child", slot=2), runtime_id="codex"
    )
    sibling_home = manager.local(factory._member_state_dir(sibling_launch))
    factory._prepare_member_state(
        _spec(), sibling_launch, factory._member_state_dir(sibling_launch)
    )
    home_dir = factory._member_state_dir(launch)
    home = manager.local(home_dir)
    (home / "sessions").mkdir(parents=True)
    (home / "sessions/rollout.jsonl").write_text("r\n")
    _indexed_home(home, {"native-child": str(sibling_home / "sessions/rollout.jsonl")})

    # An integrity complaint SQLite itself still serves: orphan pages.
    connection = sqlite3.connect(home / "state_5.sqlite")
    connection.execute("CREATE TABLE junk (x TEXT)")
    connection.execute("INSERT INTO junk VALUES (?)", ("y" * 5000,))
    connection.commit()
    connection.execute("PRAGMA writable_schema=ON")
    connection.execute("DELETE FROM sqlite_master WHERE name='junk'")
    connection.commit()
    connection.execute("PRAGMA writable_schema=OFF")
    connection.close()
    assert (
        sqlite3.connect(home / "state_5.sqlite").execute("PRAGMA integrity_check").fetchone()[0]
        != "ok"
    )

    # No exception: the start proceeds. The rebase declined to commit over an
    # index it cannot vouch for, so the copy is exactly as it was -- pointing at
    # the sibling -- and the CLI, which is about to read the same file, is what
    # reports the damage. What must not happen is a start refusal that would
    # repeat unchanged on every later attempt.
    factory._prepare_member_state(_spec(), launch, home_dir)

    assert (home / "sessions/rollout.jsonl").read_text() == "r\n"
    assert _paths(home)["native-child"] == str(sibling_home / "sessions/rollout.jsonl")


def test_codex_rebase_leaves_an_index_it_cannot_read_to_the_cli(tmp_path: Path):
    """The index is the CLI's own file; a copy it cannot parse is its report to make.

    Refusing here would wedge every start of the member over state the CLI is
    about to read and complain about far more precisely.
    """
    _private_state_root(tmp_path)
    manager = _LocalMemberShell(tmp_path)
    factory = ResidentTransportFactory(manager)  # type: ignore[arg-type]
    sibling_launch = replace(
        _launch("athr_sibling", native_session_id="native-sibling", slot=0), runtime_id="codex"
    )
    launch = replace(
        _launch("athr_child", native_session_id="native-child", slot=2), runtime_id="codex"
    )
    factory._prepare_member_state(
        _spec(), sibling_launch, factory._member_state_dir(sibling_launch)
    )
    home = manager.local(factory._member_state_dir(launch))
    home.mkdir(parents=True)
    (home / "state_5.sqlite").write_bytes(b"not a database")

    factory._prepare_member_state(_spec(), launch, factory._member_state_dir(launch))

    assert (home / "state_5.sqlite").read_bytes() == b"not a database"


@pytest.mark.parametrize(
    ("rows", "indexed"),
    [({}, False), ({"own": "/outside/home/rollout.jsonl"}, True)],
)
def test_codex_fork_succeeds_when_the_copy_has_nothing_to_rebase(
    tmp_path: Path, rows: dict[str, str], indexed: bool
):
    """A fork copies a home whose index has no sibling row yet, and must not fail.

    This is the fork path, not the start path: the copy is what the child will
    resume from, and an index with nothing in another member's home is already
    correct for it. Failing here would leave the fork journal PREPARING and
    retry the same copy forever.
    """
    _private_state_root(tmp_path)
    manager = _LocalMemberShell(tmp_path)
    factory = ResidentTransportFactory(manager)  # type: ignore[arg-type]
    source_launch = replace(
        _launch("athr_source", native_session_id="native-source", slot=0), runtime_id="codex"
    )
    target_launch = replace(
        _launch("athr_child", native_session_id=None, slot=2), runtime_id="codex"
    )
    source_home = factory._member_state_dir(source_launch)
    factory._prepare_member_state(_spec(), source_launch, source_home)
    source = manager.local(source_home)
    (source / "sessions").mkdir()
    (source / "sessions/rollout.jsonl").write_text("r\n")
    if indexed:
        _indexed_home(source, rows)

    factory._clone_private_member_state(_spec(), source_launch, target_launch)

    child = manager.local(factory._member_state_dir(target_launch))
    if indexed:
        assert _paths(child) == rows
    else:
        # A home whose CLI never wrote an index: the copy has none either.
        assert not (child / "state_5.sqlite").exists()


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
    assert _paths(manager.local(factory._member_state_dir(target_launch))) == {
        "first": str(rollout),
        "second": str(rollout),
    }
    assert rollout.read_text() == "source rollout\n"


def test_codex_rebase_refuses_a_sibling_rewrite_it_cannot_trust(tmp_path: Path):
    """The index is the CLI's file: a trigger can undo the rewrite behind it."""
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
    (source_dir / "sessions/rollout.jsonl").write_text("source rollout\n")
    index = sqlite3.connect(source_dir / "state_5.sqlite")
    index.execute("CREATE TABLE threads (id TEXT PRIMARY KEY, rollout_path TEXT NOT NULL)")
    index.execute(
        "INSERT INTO threads (id, rollout_path) VALUES (?, ?)",
        ("native-source", str(source_dir / "sessions/rollout.jsonl")),
    )
    index.execute(
        "CREATE TRIGGER undo_rebase AFTER UPDATE OF rollout_path ON threads "
        "BEGIN UPDATE threads SET rollout_path = '/agent-state/codex-homes/elsewhere/x.jsonl' "
        "WHERE id = NEW.id; END"
    )
    index.commit()
    index.close()
    index_before = (source_dir / "state_5.sqlite").read_bytes()

    with pytest.raises(DriverError, match="state could not be materialized"):
        factory._clone_private_member_state(_spec(), source_launch, target_launch)

    assert (source_dir / "state_5.sqlite").read_bytes() == index_before
    child = manager.local(factory._member_state_dir(target_launch))
    assert _paths(child) == {"native-source": str(source_dir / "sessions/rollout.jsonl")}


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
    source_home = factory._member_state_dir(source_launch)
    factory._prepare_member_state(_spec(), source_launch, source_home)
    source = manager.local(source_home)
    (source / "sessions").mkdir()
    (source / "sessions/rollout.jsonl").write_text("source rollout\n")
    escaped = f"{source}/../outside/rollout.jsonl"
    _indexed_home(source, {"escape": escaped})
    source_index_before = (source / "state_5.sqlite").read_bytes()

    with pytest.raises(DriverError, match="state could not be materialized"):
        factory._clone_private_member_state(_spec(), source_launch, target_launch)

    assert (source / "state_5.sqlite").read_bytes() == source_index_before
    assert _paths(manager.local(factory._member_state_dir(target_launch))) == {"escape": escaped}


def test_codex_rollout_rebase_refuses_a_symlink_out_of_the_child_home(tmp_path: Path):
    """A rollout directory that is a link to elsewhere is copied as a link.

    The unpack writes whatever the source home held, so the child's index can
    name a path that resolves outside its own home. Re-rooting that would hand
    the child a file in someone else's tree; refusing leaves the copy as it is.
    """
    _private_state_root(tmp_path)
    manager = _LocalMemberShell(tmp_path)
    factory = ResidentTransportFactory(manager)  # type: ignore[arg-type]
    source_launch = replace(
        _launch("athr_source", native_session_id="native-source", slot=0), runtime_id="codex"
    )
    target_launch = replace(
        _launch("athr_child", native_session_id=None, slot=2), runtime_id="codex"
    )
    source_home = factory._member_state_dir(source_launch)
    factory._prepare_member_state(_spec(), source_launch, source_home)
    source = manager.local(source_home)
    (tmp_path / "elsewhere").mkdir()
    (tmp_path / "elsewhere/rollout.jsonl").write_text("kept\n")
    (source / "sessions").symlink_to(tmp_path / "elsewhere")
    _indexed_home(source, {"native-source": str(source / "sessions/rollout.jsonl")})
    source_index_before = (source / "state_5.sqlite").read_bytes()

    with pytest.raises(DriverError, match="state could not be materialized"):
        factory._clone_private_member_state(_spec(), source_launch, target_launch)

    assert (source / "state_5.sqlite").read_bytes() == source_index_before
    assert (tmp_path / "elsewhere/rollout.jsonl").read_text() == "kept\n"


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
