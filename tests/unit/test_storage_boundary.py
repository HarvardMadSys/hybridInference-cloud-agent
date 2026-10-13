"""Callers use storage ports instead of private pools or concrete repositories.

Three properties:

* nothing outside the storage package reaches for a pool it was not handed,
  which is how the admin read models were built before the assembly existed;
* the Postgres implementations are named in exactly one place. A caller that
  imports one is a caller that cannot be given a different implementation, and
  the point of the ports is that it can;
* callers cannot import the repository modules, including their helper functions
  and constants. Shared policy and vocabulary belong outside persistence.

The scan reads source, not imports, so a module that is never imported by a
test is still checked.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest
from tests.support.import_boundaries import file_imports, imported_modules, matches_package

REPO_ROOT = Path(__file__).resolve().parents[2]
PRODUCTION = REPO_ROOT / "apps" / "backend" / "cloud_agent"
STORAGE = PRODUCTION / "storage"
TESTS = REPO_ROOT / "tests"
REPOSITORY_PACKAGE = "cloud_agent.storage.repositories"

#: Where a concrete Postgres implementation may be named. The assembly, because
#: assembling them is what it is for; the repositories, because they are them.
ALLOWED_CONCRETE = {
    STORAGE / "assembly.py",
    *(STORAGE / "repositories").glob("*.py"),
}


def _python_files(root: Path) -> list[Path]:
    return sorted(p for p in root.rglob("*.py") if "__pycache__" not in p.parts)


def test_repository_modules_are_imported_only_inside_storage() -> None:
    """Persistence cannot become a dependency by exporting a helper or constant."""
    offenders = []
    for path in _python_files(PRODUCTION):
        if STORAGE in path.parents:
            continue
        targets = file_imports(path, root=PRODUCTION, package_name="cloud_agent")
        forbidden = sorted(
            target for target in targets if matches_package(target, REPOSITORY_PACKAGE)
        )
        if forbidden:
            offenders.append(f"{path.relative_to(REPO_ROOT)}: {forbidden}")
    assert not offenders, (
        f"a caller imports a storage implementation instead of a port: {offenders}"
    )


@pytest.mark.parametrize(
    "source",
    [
        "import cloud_agent.storage.repositories.goals as implementation",
        "from cloud_agent.storage.repositories.goal_wakeups import wakeup_backoff",
        "from cloud_agent.storage import repositories as implementation",
        "from ..storage.repositories import goals",
        "from ..storage.repositories import *",
        "from ..storage import repositories",
        "if TYPE_CHECKING:\n    from cloud_agent.storage.repositories.goals import Repository",
        "def flow():\n    from ..storage.repositories.goals import helper",
    ],
)
def test_repository_import_scanner_sees_every_import_form(source: str) -> None:
    """Negative controls: a forbidden import cannot hide behind syntax or location."""
    targets = imported_modules(source, package="cloud_agent.application")
    assert any(matches_package(target, REPOSITORY_PACKAGE) for target in targets)


@pytest.mark.parametrize(
    "source",
    [
        "from cloud_agent.ports import Goals",
        "from ..domain.profiling_control import TERMINAL_LIFECYCLES",
        "import cloud_agent.storage.repositories_extra",
        "from cloud_agent.storage import repositories_extra",
    ],
)
def test_repository_import_scanner_allows_other_modules(source: str) -> None:
    """Match the package boundary, rather than names with the same prefix."""
    targets = imported_modules(source, package="cloud_agent.application")
    assert not any(matches_package(target, REPOSITORY_PACKAGE) for target in targets)


def test_only_the_storage_package_touches_a_pool_it_was_not_handed() -> None:
    """``x._pool`` outside storage is a caller reaching past its port.

    ``self._pool`` is how every repository holds the pool it was constructed
    with, so the pattern that matters is a *different* object's: the two admin
    read models used to be built from ``_require_store(store)._pool``, which is
    exactly the shape this refuses.
    """
    offenders = []
    for path in _python_files(PRODUCTION) + _python_files(TESTS):
        if STORAGE in path.parents:
            continue
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if not (isinstance(node, ast.Attribute) and node.attr == "_pool"):
                continue
            owner = ast.unparse(node.value)
            if owner == "self":
                continue
            offenders.append(f"{path.relative_to(REPO_ROOT)}:{node.lineno}: {owner}._pool")
    assert not offenders, f"a pool is reached past its owner: {offenders}"


def test_the_postgres_implementations_are_named_in_one_place() -> None:
    """Everything else depends on a port, which is what makes it a port."""
    concrete = re.compile(r"(?<![\w.])Postgres[A-Z]\w+")
    offenders = []
    for path in _python_files(PRODUCTION):
        if path in ALLOWED_CONCRETE:
            continue
        found = sorted(set(concrete.findall(path.read_text(encoding="utf-8"))))
        if found:
            offenders.append(f"{path.relative_to(REPO_ROOT)}: {found}")
    assert not offenders, f"a caller names an implementation instead of a port: {offenders}"
