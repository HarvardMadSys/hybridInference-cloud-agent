"""The rewritten baseline is the whole schema, and the store never adds to it.

These replace the tests that pinned the old twenty-eight-revision chain. What
they hold: one revision, every table the store names, a downgrade that drops
exactly what the upgrade creates, and a store that executes no DDL of its own
— the schema has one owner.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
VERSIONS = REPO / "migrations/versions"
BASELINE = VERSIONS / "0001_schema_baseline.py"
STORE = REPO / "cloud_agent/storage/store.py"

EXPECTED_TABLES = {
    "agent_admin_audit_log",
    "agent_attempt_ledger",
    "agent_attempt_resources",
    "agent_attempts",
    "agent_gitlab_connections",
    "agent_host_enroll_tokens",
    "agent_identity_namespace",
    "agent_job_events",
    "agent_jobs",
    "agent_mcp_connections",
    "agent_native_session_edges",
    "agent_oauth_states",
    "agent_process_observations",
    "agent_repo_grants",
    "agent_runner_hosts",
    "agent_runner_policy",
    "agent_sandbox_snapshots",
    "agent_sandboxes",
    "agent_session_forks",
    "agent_snapshot_jobs",
    "agent_thread_messages",
    "agent_thread_residencies",
    "agent_thread_sandbox_memberships",
    "agent_threads",
    "agent_tool_call_resources",
}


def _literal(name: str) -> tuple[str, ...]:
    tree = ast.parse(BASELINE.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        target = None
        if isinstance(node, ast.AnnAssign):
            target = getattr(node.target, "id", None)
        elif isinstance(node, ast.Assign):
            target = getattr(node.targets[0], "id", None)
        if target == name:
            return tuple(ast.literal_eval(node.value))
    raise AssertionError(f"{name} not found in the baseline migration")


def _module_constant(name: str) -> object:
    tree = ast.parse(BASELINE.read_text(encoding="utf-8"))
    for node in tree.body:
        if isinstance(node, ast.Assign) and getattr(node.targets[0], "id", None) == name:
            return ast.literal_eval(node.value)
    raise AssertionError(f"{name} not assigned at module level")


def test_the_chain_is_exactly_one_revision() -> None:
    """A squashed baseline has nothing to revise and nothing revising it."""
    files = sorted(path.name for path in VERSIONS.glob("*.py"))
    assert files == [BASELINE.name], files
    assert _module_constant("revision") == "0001_schema_baseline"
    assert _module_constant("down_revision") is None


def test_the_revision_identifier_fits_alembic_version_num() -> None:
    """``alembic_version.version_num`` is VARCHAR(32)."""
    assert len(str(_module_constant("revision"))) <= 32


def test_the_baseline_creates_every_table_the_store_names() -> None:
    """Every ``agent_*`` table the store reads or writes is created here."""
    created = set()
    for statement in _literal("_STATEMENTS"):
        match = re.search(r"CREATE TABLE (\w+)", statement)
        if match:
            created.add(match.group(1))
    assert created == EXPECTED_TABLES
    named_by_store = set(re.findall(r"\b(agent_[a-z_]+)\b", STORE.read_text(encoding="utf-8")))
    tables_in_store = {name for name in named_by_store if name in created or name.endswith("s")}
    missing = {
        name
        for name in tables_in_store
        if name not in created
        and re.search(rf"(FROM|INTO|UPDATE|JOIN) {name}\b", STORE.read_text(encoding="utf-8"))
    }
    assert not missing, f"the store names tables the baseline does not create: {sorted(missing)}"


def test_the_downgrade_drops_exactly_what_the_upgrade_creates() -> None:
    """No table is orphaned by a downgrade, and none is dropped that was never made."""
    assert set(_literal("_TABLES")) == EXPECTED_TABLES


def test_no_statement_uses_a_softer_type_than_the_store_expects() -> None:
    """Timestamps carry a zone and documents are JSONB; the store relies on both."""
    joined = "\n".join(_literal("_STATEMENTS"))
    assert "TIMESTAMPTZ" in joined
    assert not re.search(r"\bTIMESTAMP\b(?! WITH)(?!TZ)", joined)
    assert "JSONB" in joined
    assert " JSON " not in joined and " JSON," not in joined


def test_the_legacy_shapes_are_gone() -> None:
    """The compatibility residue the rewrite exists to remove stays removed."""
    joined = "\n".join(_literal("_STATEMENTS"))
    for residue in (
        "agent_thread_sandboxes",
        "agent_job_artifacts",
        "included_job_ids",
        "retained_followup_eligible",
        "legacy_1to1",
        "CREATE TRIGGER",
        "CREATE FUNCTION",
        "'publishing'",  # a snapshot state, never a job state
    ):
        if residue == "'publishing'":
            # Allowed on the snapshot state CHECK only.
            jobs_ddl = next(s for s in _literal("_STATEMENTS") if "CREATE TABLE agent_jobs" in s)
            assert residue not in jobs_ddl
            continue
        assert residue not in joined, residue
    store = STORE.read_text(encoding="utf-8")
    for residue in (
        "agent_thread_sandboxes",
        "agent_job_artifacts",
        "included_job_ids ",
        "retained_followup_eligible",
    ):
        assert residue not in store, residue


def test_the_store_executes_no_ddl() -> None:
    """The schema has one owner. A store that creates tables is two owners."""
    source = STORE.read_text(encoding="utf-8")
    offenders = [
        line.strip()
        for line in source.splitlines()
        if re.search(r"\b(CREATE TABLE|ALTER TABLE|CREATE INDEX|DROP TABLE)\b", line)
        and not line.strip().startswith("#")
    ]
    assert not offenders, f"the store still executes DDL: {offenders}"


def test_the_store_imports_no_gateway_module() -> None:
    """The gateway is reached over HTTP only (AGENTS.md rule 5)."""
    assert "serving." not in STORE.read_text(encoding="utf-8")


@pytest.mark.parametrize("path", sorted(VERSIONS.glob("*.py")), ids=lambda p: p.name)
def test_migration_files_parse(path: Path) -> None:
    """A migration that does not parse fails the deploy, not the test suite."""
    ast.parse(path.read_text(encoding="utf-8"))
