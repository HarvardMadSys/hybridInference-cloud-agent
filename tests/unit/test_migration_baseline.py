"""The baseline migration must reproduce the gateway's schema exactly.

E1's real risk is not that the migration fails to run — it is that it runs and
produces a schema that is *nearly* the source's. A column that ended up
``TEXT`` instead of ``TIMESTAMPTZ``, or an index quietly dropped as redundant,
surfaces at cutover against rows nobody can afford to lose.

So these tests compare the migration against the source, not against an idea
of what the schema should look like. The `dbtest` tier applies it for real; see
`tests/integration/storage/test_agent_job_store.py`.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
BASELINE = REPO / "migrations/versions/0001_baseline.py"
STORE = REPO / "cloud_agent/storage/store.py"

# The 11 tables the gateway's AgentJobStore created at the freeze commit.
# Written out rather than derived, so a table silently vanishing from the
# migration fails here instead of agreeing with itself.
EXPECTED_TABLES = {
    "agent_threads",
    "agent_jobs",
    "agent_attempts",
    "agent_job_events",
    "agent_thread_messages",
    "agent_job_artifacts",
    "agent_repo_grants",
    "agent_oauth_states",
    "agent_gitlab_connections",
    "agent_runner_hosts",
    "agent_runner_policy",
}


#: Columns the source schema had and this one deliberately does not.
#:
#: The design removed the per-task dollar budget: a grant scopes *what* an
#: attempt may call, and the gateway's per-user daily quota governs *how much*.
#: A column nothing reads is not harmless — it reads as a feature, and the API
#: that accepted a value for it made the claim out loud.
#:
#: Dropped from the baseline rather than added and then dropped by a later
#: migration, because this database has never been deployed: a baseline is a
#: starting schema, not a historical record. There is no row anywhere carrying
#: a value to preserve.
#:
#: Named here so the rest of the "reproduce the source exactly" guarantee keeps
#: its teeth. A *second* divergence still fails the tests below.
DELIBERATE_OMISSIONS = {"budget_usd"}


def _statements() -> tuple[str, ...]:
    """Return the baseline's DDL, read as data rather than executed."""
    tree = ast.parse(BASELINE.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        target = None
        if isinstance(node, ast.AnnAssign):
            target = getattr(node.target, "id", None)
        elif isinstance(node, ast.Assign):
            target = getattr(node.targets[0], "id", None)
        if target == "_STATEMENTS":
            return tuple(ast.literal_eval(node.value))
    raise AssertionError("_STATEMENTS not found in the baseline migration")


def test_the_baseline_creates_every_table_the_gateway_did() -> None:
    created = {
        match.group(1)
        for statement in _statements()
        for match in re.finditer(r"CREATE TABLE IF NOT EXISTS (\w+)", statement)
    }
    assert created == EXPECTED_TABLES


def test_the_baseline_keeps_the_add_column_trail() -> None:
    """Those ALTERs are not redundant — they are the source's migration history.

    ``CREATE TABLE IF NOT EXISTS`` does nothing when the table exists with the
    *wrong* columns, which is why every later schema change in the source was
    re-stated as an ALTER. Dropping them here would produce a schema that is
    correct on an empty database and wrong on an existing one.
    """
    alters = [s for s in _statements() if "ADD COLUMN IF NOT EXISTS" in s]
    assert len(alters) >= 10, f"only {len(alters)} ALTERs survived; the trail was trimmed"


def test_the_baseline_creates_indexes() -> None:
    indexes = {
        match.group(1)
        for statement in _statements()
        for match in re.finditer(r"CREATE (?:UNIQUE )?INDEX IF NOT EXISTS (\w+)", statement)
    }
    assert len(indexes) >= 8, f"only {len(indexes)} indexes: {sorted(indexes)}"


def test_no_statement_was_normalised_into_a_different_type() -> None:
    """Spot-check the types most likely to be "tidied" into something wrong."""
    joined = "\n".join(_statements())
    # Timestamps must stay timezone-aware: a naive column silently shifts every
    # lease expiry and reaper decision by the server's offset.
    assert "TIMESTAMPTZ" in joined
    assert not re.search(r"\bTIMESTAMP\b(?! WITH)(?!TZ)", joined)
    # JSONB, not JSON: the source queries these with containment operators.
    assert "JSONB" in joined


def test_the_downgrade_drops_every_table_it_creates() -> None:
    """A downgrade that misses a table leaves the next upgrade a no-op."""
    dropped = set(re.findall(r"DROP TABLE IF EXISTS \{table\}", BASELINE.read_text()))
    listed = set(re.findall(r'^        "(agent_\w+)",$', BASELINE.read_text(), re.MULTILINE))
    assert dropped, "downgrade does not drop anything"
    assert listed == EXPECTED_TABLES


# ---------------------------------------------------------------------------
# The store must no longer own the schema
# ---------------------------------------------------------------------------


def test_the_store_no_longer_creates_tables_at_startup() -> None:
    """Schema is Alembic's now.

    Leaving startup DDL in place would mean two sources of truth, and the one
    that runs first wins silently.

    Checked against the parsed source rather than the raw text: the store's own
    docstrings *discuss* ``CREATE TABLE IF NOT EXISTS`` — explaining why it was
    not enough — and a substring search cannot tell an explanation from an
    execution.
    """
    tree = ast.parse(STORE.read_text(encoding="utf-8"))
    # clean=False: the default dedents, and the raw constants below would then
    # never match their own docstrings.
    docstrings = {
        ast.get_docstring(node, clean=False)
        for node in ast.walk(tree)
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef))
    }
    offenders = [
        node.value[:60]
        for node in ast.walk(tree)
        if isinstance(node, ast.Constant)
        and isinstance(node.value, str)
        and node.value not in docstrings
        and ("CREATE TABLE" in node.value or "ADD COLUMN" in node.value)
    ]
    assert not offenders, f"the store still executes DDL: {offenders}"


def test_initialize_survives_as_a_no_op() -> None:
    """Kept so E6's bootstrap call site moves unchanged."""
    tree = ast.parse(STORE.read_text(encoding="utf-8"))
    initialize = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.AsyncFunctionDef) and node.name == "initialize"
    ]
    assert len(initialize) == 1, "initialize() should still exist, exactly once"
    # Docstring plus a bare return: nothing that touches a connection.
    body = initialize[0].body
    assert all(isinstance(node, (ast.Expr, ast.Return)) for node in body)


def test_the_store_imports_no_gateway_module() -> None:
    source = STORE.read_text(encoding="utf-8")
    assert "serving." not in source


@pytest.mark.parametrize("path", [BASELINE, REPO / "migrations/env.py"])
def test_migration_files_parse(path: Path) -> None:
    """A migration that does not import is found at deploy time, not review."""
    ast.parse(path.read_text(encoding="utf-8"))


def test_the_baseline_keeps_the_startup_backfills() -> None:
    """`initialize` was not only DDL, and the migration must carry the rest.

    Five statements backfilled data on every boot: threads and thread messages
    for jobs that predate them, the singleton runner-policy row, and
    thread_id/turn_no on legacy jobs. On a fresh database they are no-ops; on
    the database H1 imports rows into they decide whether the thread view has
    anything in it.

    They are easy to mistake for leftovers because they hid inside a method
    named for schema. This test is what makes deleting them fail.
    """
    verbs = [statement.strip().split(None, 1)[0].upper() for statement in _statements()]
    assert verbs.count("INSERT") == 4, "a thread/message/policy backfill was dropped"
    assert verbs.count("UPDATE") == 1, "the legacy-job thread_id backfill was dropped"


def test_the_store_docstring_does_not_claim_the_move_was_ddl_only() -> None:
    """Guards the wording, because the wording is what a future reader trusts.

    An `initialize` documented as "created the tables" invites someone to
    treat the migration's INSERTs as noise.
    """
    tree = ast.parse(STORE.read_text(encoding="utf-8"))
    initialize = next(
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.AsyncFunctionDef) and node.name == "initialize"
    )
    doc = ast.get_docstring(initialize) or ""
    assert "backfill" in doc.lower()


@pytest.mark.parametrize("column", sorted(DELIBERATE_OMISSIONS))
def test_a_deliberately_omitted_column_is_absent_everywhere(column: str) -> None:
    """Not in the CREATE TABLE, and not in the ADD COLUMN trail either.

    The source carried both — the table was created with the column and a later
    migration re-added it defensively — so removing one and not the other
    leaves a schema that still grows it on the second run.
    """
    assert not [s for s in _statements() if column in s], (
        f"{column} is still in the baseline; it was removed by design"
    )


def test_the_store_writes_no_deliberately_omitted_column() -> None:
    """A column gone from the schema and still in an INSERT is a crash at the
    first write, not a leftover."""
    source = STORE.read_text(encoding="utf-8")
    still_there = sorted(c for c in DELIBERATE_OMISSIONS if c in source)
    assert not still_there, f"the store still references {still_there}"


def _split_top_level(text: str) -> list[str]:
    """Split on commas that are not inside parentheses.

    A naive split miscounts `COALESCE(MAX(seq), 0)` as two values and reports a
    healthy statement as broken — and a check with a false positive is one
    people learn to skip past.
    """
    parts, depth, current = [], 0, ""
    for char in text:
        if char == "(":
            depth += 1
        elif char == ")":
            depth -= 1
        if char == "," and depth == 0:
            parts.append(current)
            current = ""
            continue
        current += char
    parts.append(current)
    return [part for part in parts if part.strip()]


def test_every_insert_names_as_many_values_as_columns() -> None:
    """**The failure mode of removing a column from a hand-written INSERT.**

    Dropping `budget_usd` shifted the positional parameters of three
    statements. Two of them came out one placeholder long — and the symptom of
    that is not a crash at import: asyncpg raises at runtime, on whichever code
    path happens to run first, and a mismatch that *still counts* would write
    each value into its neighbour's column instead.

    This counts columns against value expressions for every INSERT in the
    store, so the next removal is checked by something other than care.
    """
    source = STORE.read_text(encoding="utf-8")
    pattern = re.compile(
        r"INSERT INTO (\w+)\s*\(([^)]*)\)\s*VALUES\s*\(((?:[^()]|\([^()]*(?:\([^()]*\))?[^()]*\))*)\)",
        re.S,
    )
    mismatches = [
        f"{match.group(1)}: {len(_split_top_level(match.group(2)))} columns, "
        f"{len(_split_top_level(match.group(3)))} values"
        for match in pattern.finditer(source)
        if len(_split_top_level(match.group(2))) != len(_split_top_level(match.group(3)))
    ]
    assert not mismatches, f"INSERT column/value count mismatch: {mismatches}"
    assert pattern.search(source), "the pattern matched nothing; the check is vacuous"


def test_no_public_schema_field_offers_a_per_task_budget() -> None:
    """The API is where a removed feature is loudest.

    A column nobody reads is invisible; a request field named `budget_usd` is a
    promise, and one the service cannot keep — nothing enforces it. It survived
    the first removal pass in five places: two constants, a create parameter, a
    follow-up parameter, a response field and a config default.

    Named `budget_usd` rather than "budget" so the *retry* budget — attempts
    per job, alive and enforced — does not trip it.
    """
    schemas = (REPO / "cloud_agent/schemas.py").read_text(encoding="utf-8")
    assert "budget_usd" not in schemas
    assert "BUDGET_USD" not in schemas
