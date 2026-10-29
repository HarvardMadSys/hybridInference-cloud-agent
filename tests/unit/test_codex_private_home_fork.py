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
from cloud_agent_host.codex_state_index import CODEX_STATE_INDEX_BY_VERSION
from cloud_agent_host.drivers import DriverError
from cloud_agent_host.resident_transport import ResidentLaunch, ResidentTransportFactory
from cloud_agent_shared.shared_protocol import SHARED_SANDBOX_RUNTIME_PINS

CODEX = SHARED_SANDBOX_RUNTIME_PINS["codex"]


def _codex(
    thread_id: str, *, native_session_id: str | None, slot: int, version: str = CODEX
) -> ResidentLaunch:
    """A member launch of the Codex release a sandbox would run."""
    launch = _launch(thread_id, native_session_id=native_session_id, slot=slot)
    return replace(launch, runtime_id="codex", runtime_version=version)


def _indexed_home(home: Path, rows: dict[str, str]) -> None:
    """Write the Codex session index a CLI leaves beside its rollout files."""
    index = sqlite3.connect(home / "state_5.sqlite")
    index.execute("CREATE TABLE threads (id TEXT PRIMARY KEY, rollout_path TEXT NOT NULL)")
    index.executemany("INSERT INTO threads (id, rollout_path) VALUES (?, ?)", sorted(rows.items()))
    index.commit()
    index.close()


def _indexed_home_rows(home: Path, rows: dict[str, str]) -> None:
    """Add or replace rows in an index ``_indexed_home`` wrote."""
    index = sqlite3.connect(home / "state_5.sqlite")
    index.executemany("INSERT OR REPLACE INTO threads VALUES (?, ?)", sorted(rows.items()))
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
    source_launch = _codex("athr_source", native_session_id="native-source", slot=0)
    target_launch = _codex("athr_child", native_session_id=None, slot=2)
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

    factory._clone_private_member_state(_spec(), source_launch, target_launch, "codex fork child")

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
    sibling_launch = _codex("athr_sibling", native_session_id="native-sibling", slot=0)
    launch = _codex("athr_child", native_session_id="native-child", slot=2)
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
    sibling_launch = _codex("athr_sibling", native_session_id="native-sibling", slot=0)
    launch = _codex("athr_child", native_session_id="native-child", slot=2)
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
    sibling_launch = _codex("athr_sibling", native_session_id="native-sibling", slot=0)
    launch = _codex("athr_child", native_session_id="native-child", slot=2)
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
    sibling_launch = _codex("athr_sibling", native_session_id="native-sibling", slot=0)
    launch = _codex("athr_child", native_session_id="native-child", slot=2)
    factory._prepare_member_state(
        _spec(), sibling_launch, factory._member_state_dir(sibling_launch)
    )
    home = manager.local(factory._member_state_dir(launch))
    home.mkdir(parents=True)
    (home / "state_5.sqlite").write_bytes(b"not a database")

    factory._prepare_member_state(_spec(), launch, factory._member_state_dir(launch))

    assert (home / "state_5.sqlite").read_bytes() == b"not a database"


@pytest.mark.parametrize("index", ["none", "no threads table"])
def test_codex_fork_succeeds_when_the_copy_has_no_index_to_rebase(tmp_path: Path, index: str):
    """A home whose CLI wrote no session index, or not one of this shape, forks.

    This is the fork path, not the start path: the copy is what the child will
    resume from, and an index that holds no row at all cannot send the child to
    another member's home. Failing here would leave the fork journal PREPARING
    and retry the same copy forever.
    """
    _private_state_root(tmp_path)
    manager = _LocalMemberShell(tmp_path)
    factory = ResidentTransportFactory(manager)  # type: ignore[arg-type]
    source_launch = _codex("athr_source", native_session_id="native-source", slot=0)
    target_launch = _codex("athr_child", native_session_id=None, slot=2)
    source_home = factory._member_state_dir(source_launch)
    factory._prepare_member_state(_spec(), source_launch, source_home)
    source = manager.local(source_home)
    (source / "sessions").mkdir()
    (source / "sessions/rollout.jsonl").write_text("r\n")
    if index == "no threads table":
        connection = sqlite3.connect(source / "state_5.sqlite")
        connection.execute("CREATE TABLE other (x)")
        connection.commit()
        connection.close()

    factory._clone_private_member_state(_spec(), source_launch, target_launch, "native-child")

    child = manager.local(factory._member_state_dir(target_launch))
    assert (child / "sessions/rollout.jsonl").read_text() == "r\n"
    assert (child / "state_5.sqlite").exists() == (index != "none")


@pytest.mark.parametrize(
    ("child_row", "reason"),
    [
        # What the earlier "nothing to rebase" case was: an index that has rows,
        # none of them the child's. That is the copy the child cannot resume.
        (None, "has no row"),
        ("/outside/home/rollout.jsonl", "not a file in this home"),
        ("{source}/sessions/never-written.jsonl", "not a file in this home"),
        ("{child}/sessions/../../escape.jsonl", "not a file in this home"),
    ],
)
def test_codex_fork_refuses_a_copy_the_child_cannot_resume_its_own_thread_from(
    tmp_path: Path, child_row: str | None, reason: str
):
    """The general rewrite is not enough: the child's own row is what it resumes."""
    _private_state_root(tmp_path)
    manager = _LocalMemberShell(tmp_path)
    factory = ResidentTransportFactory(manager)  # type: ignore[arg-type]
    source_launch = _codex("athr_source", native_session_id="native-source", slot=0)
    target_launch = _codex("athr_child", native_session_id=None, slot=2)
    source_home = factory._member_state_dir(source_launch)
    factory._prepare_member_state(_spec(), source_launch, source_home)
    source = manager.local(source_home)
    child_home = manager.local(factory._member_state_dir(target_launch))
    (source / "sessions").mkdir()
    (source / "sessions/rollout.jsonl").write_text("r\n")
    rows = {"native-source": str(source / "sessions/rollout.jsonl")}
    if child_row is not None:
        rows["native-child"] = child_row.format(source=source, child=child_home)
    _indexed_home(source, rows)

    with pytest.raises(DriverError, match=reason):
        factory._clone_private_member_state(_spec(), source_launch, target_launch, "native-child")

    # Nothing was committed: the child's copy is exactly the source's.
    assert _paths(child_home) == rows


def test_codex_fork_child_id_reaches_the_index_only_as_a_value(tmp_path: Path):
    """The id is the CLI's; quoting, globbing or SQL in it must mean nothing."""
    _private_state_root(tmp_path)
    manager = _LocalMemberShell(tmp_path)
    factory = ResidentTransportFactory(manager)  # type: ignore[arg-type]
    source_launch = _codex("athr_source", native_session_id="native-source", slot=0)
    target_launch = _codex("athr_child", native_session_id=None, slot=2)
    source_home = factory._member_state_dir(source_launch)
    factory._prepare_member_state(_spec(), source_launch, source_home)
    source = manager.local(source_home)
    (source / "sessions").mkdir()
    (source / "sessions/child.jsonl").write_text("child\n")
    hostile = "c' OR '1'='1\"; $(touch pwned) `touch pwned` *; --"
    _indexed_home(source, {hostile: str(source / "sessions/child.jsonl")})

    factory._clone_private_member_state(_spec(), source_launch, target_launch, hostile)

    child = manager.local(factory._member_state_dir(target_launch))
    assert _paths(child) == {hostile: str(child / "sessions/child.jsonl")}
    assert not list(tmp_path.rglob("pwned"))
    # A NUL is the one byte an argv entry cannot carry; it is refused, not cut.
    with pytest.raises(DriverError, match="cannot name an index row"):
        factory._clone_private_member_state(_spec(), source_launch, target_launch, "a\0b")


def test_codex_rollout_rebase_failure_rolls_back_target_index(tmp_path: Path):
    _private_state_root(tmp_path)
    manager = _LocalMemberShell(tmp_path)
    factory = ResidentTransportFactory(manager)  # type: ignore[arg-type]
    source_launch = _codex("athr_source", native_session_id="native-source", slot=0)
    target_launch = _codex("athr_child", native_session_id=None, slot=2)
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
        factory._clone_private_member_state(_spec(), source_launch, target_launch, "first")

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
    source_launch = _codex("athr_source", native_session_id="native-source", slot=0)
    target_launch = _codex("athr_child", native_session_id=None, slot=2)
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
        factory._clone_private_member_state(_spec(), source_launch, target_launch, "native-source")

    assert (source_dir / "state_5.sqlite").read_bytes() == index_before
    child = manager.local(factory._member_state_dir(target_launch))
    assert _paths(child) == {"native-source": str(source_dir / "sessions/rollout.jsonl")}


def test_codex_rollout_rebase_rejects_parent_traversal(tmp_path: Path):
    _private_state_root(tmp_path)
    manager = _LocalMemberShell(tmp_path)
    factory = ResidentTransportFactory(manager)  # type: ignore[arg-type]
    source_launch = _codex("athr_source", native_session_id="native-source", slot=0)
    target_launch = _codex("athr_child", native_session_id=None, slot=2)
    source_home = factory._member_state_dir(source_launch)
    factory._prepare_member_state(_spec(), source_launch, source_home)
    source = manager.local(source_home)
    (source / "sessions").mkdir()
    (source / "sessions/rollout.jsonl").write_text("source rollout\n")
    escaped = f"{source}/../outside/rollout.jsonl"
    _indexed_home(source, {"escape": escaped})
    source_index_before = (source / "state_5.sqlite").read_bytes()

    with pytest.raises(DriverError, match="state could not be materialized"):
        factory._clone_private_member_state(_spec(), source_launch, target_launch, "escape")

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
    source_launch = _codex("athr_source", native_session_id="native-source", slot=0)
    target_launch = _codex("athr_child", native_session_id=None, slot=2)
    source_home = factory._member_state_dir(source_launch)
    factory._prepare_member_state(_spec(), source_launch, source_home)
    source = manager.local(source_home)
    (tmp_path / "elsewhere").mkdir()
    (tmp_path / "elsewhere/rollout.jsonl").write_text("kept\n")
    (source / "sessions").symlink_to(tmp_path / "elsewhere")
    _indexed_home(source, {"native-source": str(source / "sessions/rollout.jsonl")})
    source_index_before = (source / "state_5.sqlite").read_bytes()

    with pytest.raises(DriverError, match="state could not be materialized"):
        factory._clone_private_member_state(_spec(), source_launch, target_launch, "native-source")

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

    def clone(_spec, _source, _target, child):
        nonlocal clone_attempts
        assert child == "native-child"
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


def test_the_pinned_codex_release_has_its_session_index_on_record():
    """A pin bump must say which file the new release indexes its rollouts in.

    Without an entry the rebase cannot run: every fork with the new release is
    refused, and a member forked by an earlier release is never repaired. A
    stale entry is worse -- the rebase finds no such file and calls that
    nothing to do -- so the entry is checked, not copied forward.
    """
    assert CODEX in CODEX_STATE_INDEX_BY_VERSION, (
        f"Codex {CODEX} is pinned in SHARED_SANDBOX_RUNTIME_PINS "
        "(apps/backend/cloud_agent_shared/shared_protocol.py) but has no entry in "
        "CODEX_STATE_INDEX_BY_VERSION (apps/backend/cloud_agent_host/codex_state_index.py). "
        f"Start Codex {CODEX} with an empty CODEX_HOME, run one turn, and add the "
        "state_*.sqlite file whose threads table holds that rollout's rollout_path."
    )
    assert all(
        name.endswith(".sqlite") and "/" not in name
        for name in CODEX_STATE_INDEX_BY_VERSION.values()
    )


def test_an_unknown_codex_release_is_not_forked_and_its_start_leaves_the_index(
    tmp_path: Path, monkeypatch
):
    """A release with no index on record is never guessed at."""
    _private_state_root(tmp_path)
    manager = _LocalMemberShell(tmp_path)
    factory = ResidentTransportFactory(manager)  # type: ignore[arg-type]
    unknown = "0.0.0-unreviewed"
    assert unknown not in CODEX_STATE_INDEX_BY_VERSION
    sibling = _codex("athr_sibling", native_session_id="native-sibling", slot=0)
    launch = _codex("athr_child", native_session_id="native-child", slot=2, version=unknown)
    sibling_home = manager.local(factory._member_state_dir(sibling))
    home_dir = factory._member_state_dir(launch)
    home = manager.local(home_dir)
    (home / "sessions").mkdir(parents=True)
    (home / "sessions/rollout.jsonl").write_text("r\n")
    _indexed_home(home, {"native-child": str(sibling_home / "sessions/rollout.jsonl")})
    before = (home / "state_5.sqlite").read_bytes()

    # A member's own start still never fails over its index.
    factory._prepare_member_state(_spec(), launch, home_dir)
    assert (home / "state_5.sqlite").read_bytes() == before

    # A fork is refused before the CLI makes a child, and says where to look.
    events: list[str] = []
    source = SimpleNamespace(
        native_session_id="native-child",
        launch=launch,
        session=SimpleNamespace(fork_native=lambda **_kwargs: events.append("fork") or "x"),
        close=lambda: events.append("close"),
    )
    monkeypatch.setattr(factory, "_read_fork_journal", lambda *_args: None)
    monkeypatch.setattr(factory, "_write_fork_journal", lambda *_args: events.append("journal"))
    target = _codex("athr_grandchild", native_session_id=None, slot=3, version=unknown)
    with pytest.raises(DriverError, match="CODEX_STATE_INDEX_BY_VERSION"):
        factory.fork_native(_spec(), source, "fork-unknown", target, 1)
    assert events == []
    with pytest.raises(DriverError, match="CODEX_STATE_INDEX_BY_VERSION"):
        factory._clone_private_member_state(_spec(), launch, target, "native-child")


def test_codex_start_repairs_rows_rooted_in_the_earlier_collection(tmp_path: Path):
    """A home forked while homes lived in ``codex-members`` is repaired too.

    That collection is gone once its members have moved out, so the home is
    read out of the path text, as in the current collection: only a member
    home's name -- the hash of its thread -- counts, and this home's own name
    there is this home before the move.
    """
    state = _private_state_root(tmp_path)
    manager = _LocalMemberShell(tmp_path)
    factory = ResidentTransportFactory(manager)  # type: ignore[arg-type]
    launch = _codex("athr_child", native_session_id="native-child", slot=2)
    home_dir = factory._member_state_dir(launch)
    home = manager.local(home_dir)
    legacy = manager.local(factory._legacy_member_state_dir(launch)).parent
    assert legacy == state / "codex-members"
    sibling = "a" * 64
    (home / "sessions").mkdir(parents=True)
    rows = {
        "forked from a legacy sibling": f"{legacy}/{sibling}/sessions/rollout.jsonl",
        "own, before the move": f"{legacy}/{home.name}/sessions/own.jsonl",
        # None of these is a member home of this sandbox.
        "not a member home": f"{legacy}/shared/sessions/rollout.jsonl",
        "refused aside": f"{legacy}/{sibling}.refused-0123456789abcdef/x.jsonl",
        "prefix collision": f"{legacy}-old/{sibling}/sessions/rollout.jsonl",
        "another collection": f"{state}/opencode-members/{sibling}/sessions/rollout.jsonl",
    }
    _indexed_home(home, rows)

    factory._prepare_member_state(_spec(), launch, home_dir)

    assert _paths(home) == {
        **rows,
        "forked from a legacy sibling": str(home / "sessions/rollout.jsonl"),
        "own, before the move": str(home / "sessions/own.jsonl"),
    }
    # And a fork from that home now hands its child rows in the child's home.
    target = _codex("athr_grandchild", native_session_id=None, slot=3)
    (home / "sessions/own.jsonl").write_text("own\n")
    _indexed_home_rows(home, {"native-child": f"{legacy}/{home.name}/sessions/own.jsonl"})
    factory._clone_private_member_state(_spec(), launch, target, "native-child")
    grandchild = manager.local(factory._member_state_dir(target))
    assert _paths(grandchild)["native-child"] == str(grandchild / "sessions/own.jsonl")


def test_codex_start_survives_what_the_rebase_did_not_foresee(tmp_path: Path):
    """A symlink loop under a sibling row makes path resolution itself raise.

    Python reports that as ``RuntimeError``, which no ``sqlite3`` or ``OSError``
    handler catches. Uncaught in a member's own start, it would fail that start
    and every later one, over a home whose CLI never minded the loop.
    """
    _private_state_root(tmp_path)
    manager = _LocalMemberShell(tmp_path)
    factory = ResidentTransportFactory(manager)  # type: ignore[arg-type]
    sibling = _codex("athr_sibling", native_session_id="native-sibling", slot=0)
    launch = _codex("athr_child", native_session_id="native-child", slot=2)
    sibling_home = manager.local(factory._member_state_dir(sibling))
    home_dir = factory._member_state_dir(launch)
    home = manager.local(home_dir)
    home.mkdir(parents=True)
    (home / "loop").symlink_to(home / "loop")
    _indexed_home(home, {"native-child": str(sibling_home / "loop/rollout.jsonl")})
    before = (home / "state_5.sqlite").read_bytes()

    factory._prepare_member_state(_spec(), launch, home_dir)

    assert (home / "state_5.sqlite").read_bytes() == before
