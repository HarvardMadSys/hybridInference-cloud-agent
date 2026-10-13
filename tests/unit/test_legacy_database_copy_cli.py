"""Evidence failures cannot be discovered only after the database was changed."""

from __future__ import annotations

import sys

from scripts import convert_legacy_database_copy as migration


def test_existing_report_refuses_before_connecting(tmp_path, monkeypatch):
    report = tmp_path / "report.json"
    report.write_text("existing evidence")
    monkeypatch.setenv(migration.DSN_ENV, "postgresql://unused/unused")
    monkeypatch.setattr(
        sys,
        "argv",
        ["convert", "--database", "agent_legacy_copy_test", "--apply", "--report", str(report)],
    )

    def forbidden(*args, **kwargs):
        raise AssertionError("must not connect when evidence cannot be reserved")

    monkeypatch.setattr(migration.psycopg, "connect", forbidden)
    assert migration.main() == 1
    assert report.read_text() == "existing evidence"


def test_the_rendered_migrations_run_inside_one_transaction():
    """The conversion runs every revision's offline SQL in its one transaction.

    PostgreSQL refuses a ``CONCURRENTLY`` index build inside a transaction
    block, so a revision that emits one offline fails every conversion. Only
    the database tier ran the conversion, so the rendering is checked here too.
    """
    rendered = migration.migration_sql(migration.BASELINE + ":head")

    assert "CONCURRENTLY" not in rendered.upper()
