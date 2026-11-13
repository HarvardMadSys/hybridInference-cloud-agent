"""The old schema is converted with real constraints and complete rollback."""

from __future__ import annotations

import contextlib
import os
import time
import uuid
from pathlib import Path

import psycopg
import pytest
from psycopg import sql
from scripts import convert_legacy_database_copy as migration

pytestmark = pytest.mark.dbtest
FIXTURE = Path(__file__).resolve().parents[2] / "fixtures/legacy_schema/0025.sql"
#: How long the server may take to end a closed session's process.
SESSION_EXIT_TIMEOUT_S = 30.0


@contextlib.contextmanager
def session(name, options):
    """Connect to the copy; when the block ends, wait until the server has let go too.

    Closing a connection does not wait for the server process behind it to
    exit, and the audit refuses while any other client of the copy is still
    listed in pg_stat_activity. Without this wait the next session's audit can
    count the one just closed, as it did on dev CI on 2026-11-12.
    """
    conn = psycopg.connect(dbname=name, **options)
    pid = conn.info.backend_pid
    try:
        with conn:
            yield conn
    finally:
        conn.close()  # also when commit failed, which leaves it open
        wait_until_disconnected(name, options, pid)


def wait_until_disconnected(name, options, pid):
    # Watched from another database, so watching adds no client of the copy.
    # Each autocommitted statement reads pg_stat_activity afresh.
    with psycopg.connect(dbname="postgres", autocommit=True, **options) as watcher:
        deadline = time.monotonic() + SESSION_EXIT_TIMEOUT_S
        while watcher.execute(
            "SELECT 1 FROM pg_stat_activity WHERE pid = %s AND datname = %s", (pid, name)
        ).fetchone():
            if time.monotonic() > deadline:
                pytest.fail(
                    f"the server process {pid} of a closed session was still connected to "
                    f"{name} after {SESSION_EXIT_TIMEOUT_S:g} s"
                )
            time.sleep(0.01)


@pytest.fixture()
def copied_database():
    options = {
        "host": os.environ.get("TEST_DB_HOST", "localhost"),
        "port": os.environ.get("TEST_DB_PORT", "5432"),
        "user": os.environ.get("TEST_DB_USER", "postgres"),
        "password": os.environ.get("TEST_DB_PASSWORD", "postgres"),
    }
    name = "agent_legacy_copy_" + uuid.uuid4().hex
    with psycopg.connect(dbname="postgres", autocommit=True, **options) as admin:
        admin.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(name)))
        try:
            with session(name, options) as conn:
                conn.execute(FIXTURE.read_text())
                conn.execute("SET search_path=public")
                conn.execute("INSERT INTO agent_runner_policy(id) VALUES (true)")
                conn.execute(
                    "INSERT INTO alembic_version VALUES (%s)", (migration.SOURCE_REVISION,)
                )
                conn.execute(
                    "INSERT INTO agent_runner_hosts(host,credential_hash) VALUES ('host','credential-hash')"
                )
                conn.execute(
                    "INSERT INTO agent_threads(id,user_id,repo,title) VALUES ('t','owner','repo','title')"
                )
                conn.execute(
                    "INSERT INTO agent_jobs(id,thread_id,user_id,turn_no,repo,task_prompt,runtime,model,state) "
                    "VALUES ('j','t','owner',1,'repo','original prompt','codex','model','failed')"
                )
                conn.execute(
                    "INSERT INTO agent_attempts(id,job_id,attempt_no,lease_owner,lease_generation,"
                    "lease_expires_at,status,finished_at,continuity_mode) "
                    "VALUES (20,'j',1,'worker',1,now(),'finished',now(),'replay')"
                )
                conn.execute("UPDATE agent_jobs SET current_attempt_id=20 WHERE id='j'")
                conn.execute(
                    "INSERT INTO agent_job_artifacts(job_id,attempt_id,kind,content) "
                    "VALUES ('j',20,'log','original artifact')"
                )
                conn.execute("SELECT setval('agent_attempts_id_seq',1000,true)")
            yield name, options
        finally:
            admin.execute(sql.SQL("DROP DATABASE {} WITH (FORCE)").format(sql.Identifier(name)))


def test_conversion_keeps_source_and_parent_identity_and_sequence(copied_database):
    name, options = copied_database
    with session(name, options) as conn:
        before = migration.fingerprints(conn, "public")
        report = migration.convert(conn, name)
        assert report["archive_unchanged"]
        assert migration.fingerprints(conn, migration.ARCHIVE) == before
        assert conn.execute(
            "SELECT thread_id,continuity_mode FROM agent_attempts WHERE id=20"
        ).fetchone() == ("t", None)
        assert conn.execute(
            "SELECT continuity_mode FROM legacy_archive.agent_attempts WHERE id=20"
        ).fetchone() == ("replay",)
        assert conn.execute(
            "SELECT content FROM legacy_archive.agent_job_artifacts"
        ).fetchone() == ("original artifact",)
        assert conn.execute(
            "SELECT current_attempt_id FROM agent_jobs WHERE id='j'"
        ).fetchone() == (20,)
        assert conn.execute(
            "SELECT credential_hash FROM agent_runner_hosts WHERE host='host'"
        ).fetchone() == ("credential-hash",)
        assert conn.execute("SELECT nextval('agent_attempts_id_seq')").fetchone() == (1001,)
        assert conn.execute("SELECT count(*) FROM agent_identity_namespace").fetchone() == (0,)
        assert (
            conn.execute("SELECT version_num FROM alembic_version").fetchone()[0]
            != migration.SOURCE_REVISION
        )
    with (
        session(name, options) as conn,
        pytest.raises(migration.ConversionError, match="already exists"),
    ):
        migration.audit(conn, name)


def test_cross_owner_row_rolls_back_archive_and_entire_schema(copied_database):
    name, options = copied_database
    with session(name, options) as conn:
        conn.execute("UPDATE agent_jobs SET user_id='another-owner'")
    with session(name, options) as conn:
        before = migration.fingerprints(conn, "public")
    with (
        pytest.raises(psycopg.errors.ForeignKeyViolation),
        session(name, options) as conn,
    ):
        migration.convert(conn, name)
    with session(name, options) as conn:
        assert conn.execute("SELECT to_regnamespace('legacy_archive')").fetchone() == (None,)
        assert migration.fingerprints(conn, "public") == before


#: Each guard's own refusal: a lingering client must not stand in for another.
GUARD_REFUSALS = {
    "wrong-name": "explicitly named agent_legacy_copy_",
    "active-job": "nonterminal jobs",
    "other-client": "other clients",
}


@pytest.mark.parametrize("reason", ["wrong-name", "active-job", "other-client"])
def test_live_source_guards_refuse_before_writes(copied_database, reason):
    name, options = copied_database
    extra = None
    if reason == "active-job":
        with session(name, options) as conn:
            conn.execute("UPDATE agent_jobs SET state='queued'")
    if reason == "other-client":
        extra = psycopg.connect(dbname=name, **options)
    try:
        with session(name, options) as conn:
            with pytest.raises(migration.ConversionError, match=GUARD_REFUSALS[reason]):
                migration.audit(conn, "production" if reason == "wrong-name" else name)
            assert conn.execute("SELECT to_regnamespace('legacy_archive')").fetchone() == (None,)
    finally:
        if extra:
            extra.close()
