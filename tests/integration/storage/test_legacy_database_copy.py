"""The old schema is converted with real constraints and complete rollback."""

from __future__ import annotations

import os
import uuid
from pathlib import Path

import psycopg
import pytest
from psycopg import sql
from scripts import convert_legacy_database_copy as migration

pytestmark = pytest.mark.dbtest
FIXTURE = Path(__file__).resolve().parents[2] / "fixtures/legacy_schema/0025.sql"


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
            with psycopg.connect(dbname=name, **options) as conn:
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
    with psycopg.connect(dbname=name, **options) as conn:
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
        psycopg.connect(dbname=name, **options) as conn,
        pytest.raises(migration.ConversionError, match="already exists"),
    ):
        migration.audit(conn, name)


def test_cross_owner_row_rolls_back_archive_and_entire_schema(copied_database):
    name, options = copied_database
    with psycopg.connect(dbname=name, **options) as conn:
        conn.execute("UPDATE agent_jobs SET user_id='another-owner'")
    with psycopg.connect(dbname=name, **options) as conn:
        before = migration.fingerprints(conn, "public")
    with (
        pytest.raises(psycopg.errors.ForeignKeyViolation),
        psycopg.connect(dbname=name, **options) as conn,
    ):
        migration.convert(conn, name)
    with psycopg.connect(dbname=name, **options) as conn:
        assert conn.execute("SELECT to_regnamespace('legacy_archive')").fetchone() == (None,)
        assert migration.fingerprints(conn, "public") == before


@pytest.mark.parametrize("reason", ["wrong-name", "active-job", "other-client"])
def test_live_source_guards_refuse_before_writes(copied_database, reason):
    name, options = copied_database
    extra = None
    if reason == "active-job":
        with psycopg.connect(dbname=name, **options) as conn:
            conn.execute("UPDATE agent_jobs SET state='queued'")
    if reason == "other-client":
        extra = psycopg.connect(dbname=name, **options)
    try:
        with psycopg.connect(dbname=name, **options) as conn:
            with pytest.raises(migration.ConversionError):
                migration.audit(conn, "production" if reason == "wrong-name" else name)
            assert conn.execute("SELECT to_regnamespace('legacy_archive')").fetchone() == (None,)
    finally:
        if extra:
            extra.close()
