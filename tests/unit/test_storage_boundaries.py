"""The dependency directions the persistence boundary depends on.

A Protocol boundary is only worth having if the arrows point one way. Nothing
enforces that by itself: a single ``from cloud_agent.storage.repositories import
...`` inside a port turns the contract into a second implementation, and it will
still pass every behavioural test.

So the rules are checked here, on the parsed source rather than on a rendered
diff, because an import is a statement about architecture rather than a matter
of taste. Each rule names the failure it prevents.

Two rules cannot hold while the migration is half-done: every caller still
reaches the old monolith. They are marked ``xfail(strict=True)``, which starts
failing the day they begin to hold — the reminder to delete the marker rather
than to delete the rule.
"""

from __future__ import annotations

import ast
import pathlib

import pytest

REPO = pathlib.Path(__file__).resolve().parents[2]
AGENT = REPO / "cloud_agent"


def imported_modules(path: pathlib.Path) -> set[str]:
    """Return every module a file imports."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                found.add(alias.name)
        elif isinstance(node, ast.ImportFrom) and node.module:
            found.add(node.module)
    return found


def python_files(root: pathlib.Path) -> list[pathlib.Path]:
    """Every module under a directory."""
    return sorted(root.rglob("*.py"))


@pytest.mark.parametrize("package", ["ports", "domain"])
def test_ports_and_domain_do_not_import_storage_or_api(package: str) -> None:
    """The contract must not know how it is implemented.

    An import of the storage layer or an API module from a port would let the
    implementation define the contract, which is the arrangement this refactor
    exists to end.
    """
    offenders: list[str] = []
    for path in python_files(AGENT / package):
        for module in imported_modules(path):
            if module.startswith(
                ("cloud_agent.storage", "cloud_agent.api", "cloud_agent.bootstrap")
            ):
                offenders.append(f"{path.relative_to(REPO)} imports {module}")
    assert not offenders, offenders


def test_the_shared_sql_layer_does_not_acquire_a_connection() -> None:
    """``_sql`` helpers take a connection; they never open one.

    An operation is one transaction, and it stays that way only while the pieces
    it is built from cannot open a second connection behind its back. A helper
    that acquired one would commit separately and silently break the atomicity
    its callers depend on.
    """
    offenders: list[str] = []
    for path in python_files(AGENT / "storage" / "_sql"):
        source = path.read_text(encoding="utf-8")
        for pattern in (".acquire()", "create_pool(", ".transaction()"):
            if pattern in source:
                offenders.append(f"{path.relative_to(REPO)} contains {pattern}")
    assert not offenders, offenders


def test_no_operation_commits_its_own_transaction() -> None:
    """An operation commits once, when its scope ends.

    The alternative is a method that opens a transaction, commits partway, and
    finishes the rest outside it — which is exactly the failure the design calls
    out, because a reader can then observe half an operation. Transaction
    wrapping is done by the ``async with`` around the whole body, so nothing
    inside it has any business calling ``commit`` or ``rollback``.
    """
    offenders: list[str] = []
    for path in python_files(AGENT / "storage" / "repositories"):
        source = path.read_text(encoding="utf-8")
        for pattern in ("conn.commit(", "conn.rollback(", ".transaction() as t"):
            if pattern in source:
                offenders.append(f"{path.relative_to(REPO)} contains {pattern}")
    assert not offenders, offenders


def test_every_sql_helper_that_touches_the_database_takes_a_connection() -> None:
    """A ``_sql`` helper that runs SQL receives the caller's connection first.

    This is what keeps a cross-table operation a single commit: the helper has no
    way to open a connection of its own, so it cannot commit separately even by
    accident. Pure helpers — a JSON decoder, a cursor encoder, a query string
    builder — take no connection and are not database calls at all.
    """
    offenders: list[str] = []
    for path in python_files(AGENT / "storage" / "_sql"):
        for node in ast.parse(path.read_text(encoding="utf-8")).body:
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            body = ast.unparse(node)
            if "conn." not in body and "conn," not in body and "(conn)" not in body:
                continue
            first = [*node.args.posonlyargs, *node.args.args]
            if not first or first[0].arg != "conn":
                offenders.append(
                    f"{path.relative_to(REPO)}:{node.lineno} {node.name} uses a connection "
                    f"without taking one first"
                )
    assert not offenders, offenders


def test_no_repository_stores_a_connection_on_the_instance() -> None:
    """A repository holds a pool, never a request-scoped connection.

    Two concurrent calls on one repository share the instance. Assigning a
    connection to ``self`` would let one call observe the other's transaction.
    """
    offenders: list[str] = []
    for path in python_files(AGENT / "storage" / "repositories"):
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if isinstance(node, ast.Assign):
                targets = node.targets
            elif isinstance(node, ast.AnnAssign):
                targets = [node.target]
            else:
                continue
            for target in targets:
                if (
                    isinstance(target, ast.Attribute)
                    and isinstance(target.value, ast.Name)
                    and target.value.id == "self"
                    and target.attr != "_pool"
                ):
                    offenders.append(f"{path.relative_to(REPO)} assigns self.{target.attr}")
    assert not offenders, offenders


def test_the_pool_is_created_in_exactly_one_place() -> None:
    """One owner for the pool.

    The lifespan that creates it is the one that stops the background tasks and
    closes it. A second ``create_pool`` would be a second owner, and the two
    would close each other's connections.
    """
    owners: list[str] = []
    for path in python_files(AGENT):
        if "create_pool(" in path.read_text(encoding="utf-8"):
            owners.append(str(path.relative_to(REPO)))
    allowed = {
        "cloud_agent/app.py",
        "cloud_agent/deploy_readiness.py",
        "cloud_agent/identity_namespace.py",
    }
    unexpected = [owner for owner in owners if owner not in allowed]
    assert not unexpected, f"a new pool owner appeared: {unexpected}"


@pytest.mark.xfail(
    strict=True, reason="callers still import the monolith; delete this marker when they do not"
)
def test_no_caller_imports_the_storage_layer() -> None:
    """Business code depends on a port, not on a Postgres class.

    Pending: every caller still reaches ``cloud_agent.storage.store``. This is the
    rule the migration is working toward, and it is asserted here so that
    finishing the migration is what makes it start passing.
    """
    offenders: list[str] = []
    for path in python_files(AGENT):
        relative = path.relative_to(REPO)
        if "storage" in path.parts or path.name == "bootstrap.py":
            continue
        for module in imported_modules(path):
            if module.startswith(("cloud_agent.storage", "cloud_agent.bootstrap")):
                offenders.append(f"{relative} imports {module}")
    assert not offenders, offenders


@pytest.mark.xfail(strict=True, reason="the monolith is deleted at the end of the migration")
def test_the_old_monolith_is_gone() -> None:
    """The end state, named explicitly."""
    assert not (AGENT / "storage" / "store.py").exists()
