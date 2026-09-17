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
STORAGE = REPO / "cloud_agent/storage"


def _storage_source() -> str:
    """Every line of the storage layer, as one text to scan.

    This read the single ``store.py``. The store is gone and its statements
    live across ``storage/_sql`` and ``storage/repositories``, so the scan
    follows them: the same assertions over strictly more files than before,
    which is what keeps "the schema has one owner" checkable after the split.
    """
    return "\n".join(path.read_text(encoding="utf-8") for path in sorted(STORAGE.rglob("*.py")))


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


def _revision_pair(path: Path) -> tuple[str, str | None]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    values: dict[str, object] = {}
    for node in tree.body:
        if isinstance(node, ast.Assign) and getattr(node.targets[0], "id", None) in (
            "revision",
            "down_revision",
        ):
            values[node.targets[0].id] = ast.literal_eval(node.value)
    return str(values["revision"]), values.get("down_revision")  # type: ignore[return-value]


def test_the_chain_is_linear_and_rooted_at_the_rewritten_baseline() -> None:
    """One root, one head, and every revision revises the one before it."""
    pairs = dict(map(_revision_pair, VERSIONS.glob("*.py")))
    assert "0001_schema_baseline" in pairs and pairs["0001_schema_baseline"] is None
    roots = [rev for rev, parent in pairs.items() if parent is None]
    assert roots == ["0001_schema_baseline"], roots
    parents = [parent for parent in pairs.values() if parent is not None]
    assert len(parents) == len(set(parents)), "a revision is revised twice"
    assert all(parent in pairs for parent in parents), "a down_revision names no file"
    heads = [rev for rev in pairs if rev not in parents]
    assert len(heads) == 1, heads
    assert all(len(rev) <= 32 for rev in pairs)


def test_the_revision_identifier_fits_alembic_version_num() -> None:
    """``alembic_version.version_num`` is VARCHAR(32)."""
    assert len(str(_module_constant("revision"))) <= 32


def _tables_created_by_every_revision() -> set[str]:
    """Every table the whole migration chain creates.

    Read from the revision files rather than from a list maintained here: the
    schema has one owner, and a table added by a later revision is still a
    table the store may name. Checking only the baseline made every migration
    that follows it a hole in this test.
    """
    created: set[str] = set()
    for path in sorted(VERSIONS.glob("*.py")):
        created.update(re.findall(r"CREATE TABLE (\w+)", path.read_text(encoding="utf-8")))
    return created


def test_the_baseline_creates_the_tables_it_declares() -> None:
    """The baseline is exactly the set it says it is, and nothing drifted in."""
    created = set()
    for statement in _literal("_STATEMENTS"):
        match = re.search(r"CREATE TABLE (\w+)", statement)
        if match:
            created.add(match.group(1))
    assert created == EXPECTED_TABLES


def test_every_table_the_store_names_is_created_by_a_revision() -> None:
    """A store query against a table no revision creates fails in production only."""
    created = _tables_created_by_every_revision()
    store_source = _storage_source()
    named_by_store = set(re.findall(r"\b(agent_[a-z_]+)\b", store_source))
    tables_in_store = {name for name in named_by_store if name in created or name.endswith("s")}
    missing = {
        name
        for name in tables_in_store
        if name not in created and re.search(rf"(FROM|INTO|UPDATE|JOIN) {name}\b", store_source)
    }
    assert not missing, f"the store names tables no revision creates: {sorted(missing)}"


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
    store = _storage_source()
    for residue in (
        "agent_thread_sandboxes",
        "agent_job_artifacts",
        "included_job_ids ",
        "retained_followup_eligible",
    ):
        assert residue not in store, residue
    # The sandbox ``mode`` column went with the topology it selected, and no
    # table in the rewritten schema has a bare ``mode`` column, so an
    # alias-qualified one in the store's SQL can only be a leftover. This is
    # the class the Postgres tier caught and this tier did not: a query that
    # arrived from a branch landing after the rewrite, still naming a column
    # the rewrite deleted. (``routes.py``'s ``body.mode`` is a request field,
    # not SQL, which is why this reads the storage layer alone.)
    leftover = re.search(r"\b\w+\.mode\b", store)
    assert leftover is None, f"the storage layer names a dropped column: {leftover.group(0)}"


def test_the_storage_layer_executes_no_ddl() -> None:
    """The schema has one owner. A repository that creates tables is two owners."""
    source = _storage_source()
    offenders = [
        line.strip()
        for line in source.splitlines()
        if re.search(r"\b(CREATE TABLE|ALTER TABLE|CREATE INDEX|DROP TABLE)\b", line)
        and not line.strip().startswith("#")
    ]
    assert not offenders, f"the storage layer still executes DDL: {offenders}"


def test_the_storage_layer_imports_no_gateway_module() -> None:
    """The gateway is reached over HTTP only (AGENTS.md rule 5)."""
    assert "serving." not in _storage_source()


@pytest.mark.parametrize("path", sorted(VERSIONS.glob("*.py")), ids=lambda p: p.name)
def test_migration_files_parse(path: Path) -> None:
    """A migration that does not parse fails the deploy, not the test suite."""
    ast.parse(path.read_text(encoding="utf-8"))
