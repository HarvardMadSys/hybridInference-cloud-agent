"""Static guards for the retained-session online migration sequence."""

import ast
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
ENV = REPO / "migrations/env.py"
SCHEMA = REPO / "migrations/versions/0023_native_turn_anchors.py"
BACKFILL = REPO / "migrations/versions/0024_native_turn_rollout_guard.py"
DISPOSITION = REPO / "migrations/versions/0025_retained_followup_gate.py"


def test_every_revision_identifier_fits_alembic_version_num() -> None:
    """The baseline declares ``alembic_version.version_num`` as VARCHAR(32)."""
    too_long: list[tuple[str, str]] = []
    for migration in sorted((REPO / "migrations/versions").glob("*.py")):
        tree = ast.parse(migration.read_text(), filename=str(migration))
        for node in tree.body:
            if not isinstance(node, ast.Assign):
                continue
            if not any(
                isinstance(target, ast.Name) and target.id == "revision" for target in node.targets
            ):
                continue
            if isinstance(node.value, ast.Constant) and isinstance(node.value.value, str):
                revision = node.value.value
                if len(revision) > 32:
                    too_long.append((migration.name, revision))
            break

    assert not too_long, f"revision identifiers exceed alembic_version VARCHAR(32): {too_long}"


def test_native_turn_schema_commits_before_the_backfill() -> None:
    env = ENV.read_text()
    schema = SCHEMA.read_text()
    backfill = BACKFILL.read_text()

    assert "transaction_per_migration=True" in env
    assert "SET LOCAL lock_timeout" in schema
    assert "assign_agent_job_native_turn_no" in schema
    assert "WITH RECURSIVE native_jobs" not in schema
    assert "CREATE UNIQUE INDEX" not in schema
    assert "WITH RECURSIVE native_jobs" in backfill
    assert "autocommit_block" in backfill
    assert "DROP INDEX CONCURRENTLY IF EXISTS idx_agent_jobs_native_turn" in backfill
    assert "CREATE UNIQUE INDEX CONCURRENTLY" in backfill


def test_disposable_rows_remain_fail_closed_across_the_parallel_cutover() -> None:
    migration = DISPOSITION.read_text()

    assert "retained_followup_eligible" in migration
    assert "SET DEFAULT TRUE" not in migration
    assert "UPDATE agent_jobs" not in migration
    assert "SET NOT NULL" not in migration
