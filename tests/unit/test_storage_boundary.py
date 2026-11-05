"""Callers use storage ports instead of private pools or concrete repositories.

Three properties:

* nothing outside the storage package reaches for a pool it was not handed,
  which is how the admin read models were built before the assembly existed;
* the Postgres implementations are named in exactly one place. A caller that
  imports one is a caller that cannot be given a different implementation, and
  the point of the ports is that it can;
* callers cannot import the repository modules, including their helper functions
  and constants. Shared policy and vocabulary belong outside persistence;
* nor anything else in the storage package but the assembly: its SQL helpers,
  its row decoders and its deadlock retry are implementation too.

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
STORAGE_PACKAGE = "cloud_agent.storage"
#: What a caller outside storage is handed: the assembly, and nothing behind it.
STORAGE_ENTRY = "cloud_agent.storage.assembly"

#: Callers outside storage that still import a storage module, and why each one
#: has not moved yet. An entry is a debt to pay down, never a way to add one.
STORAGE_IMPORT_EXCEPTIONS = {
    # The heartbeat's settings-import policy, called only by the host-pool
    # repository, decodes that row's JSONB column with the store's own decoder.
    # It left the repository as policy; its decoder did not come with it.
    ("cloud_agent/host_settings_import.py", "cloud_agent.storage._sql.rows"),
    ("cloud_agent/host_settings_import.py", "cloud_agent.storage._sql.rows.load_json"),
}

#: Where a concrete Postgres implementation may be named. The assembly, because
#: assembling them is what it is for; the repositories, because they are them.
ALLOWED_CONCRETE = {
    STORAGE / "assembly.py",
    *(STORAGE / "repositories").rglob("*.py"),
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


def _is_storage_internal(target: str) -> bool:
    """Anything in the storage package except the package itself and the assembly."""
    return (
        matches_package(target, STORAGE_PACKAGE)
        and target != STORAGE_PACKAGE
        and not matches_package(target, STORAGE_ENTRY)
    )


def test_storage_modules_are_imported_only_inside_storage() -> None:
    """A caller is handed the assembly; every other storage module is implementation."""
    offenders = []
    for path in _python_files(PRODUCTION):
        if STORAGE in path.parents:
            continue
        relative = path.relative_to(PRODUCTION.parent).as_posix()
        targets = file_imports(path, root=PRODUCTION, package_name="cloud_agent")
        forbidden = sorted(
            target
            for target in targets
            if _is_storage_internal(target) and (relative, target) not in STORAGE_IMPORT_EXCEPTIONS
        )
        if forbidden:
            offenders.append(f"{path.relative_to(REPO_ROOT)}: {forbidden}")
    assert not offenders, f"a caller imports a storage module other than the assembly: {offenders}"


def test_every_storage_import_exception_is_still_needed() -> None:
    """An exception whose import is gone is removed, so the list only shrinks."""
    stale = []
    for relative, target in sorted(STORAGE_IMPORT_EXCEPTIONS):
        path = PRODUCTION.parent / relative
        if not path.exists() or target not in file_imports(
            path, root=PRODUCTION, package_name="cloud_agent"
        ):
            stale.append((relative, target))
    assert not stale, f"storage import exceptions no longer needed: {stale}"


@pytest.mark.parametrize(
    "source",
    [
        "from cloud_agent.storage.admin_usage_queries import UsageQueryTooLarge",
        "from cloud_agent.storage._sql.rows import load_json",
        "from cloud_agent.storage import deadlocks",
        "from ..storage.dsn import agent_dsn",
        "import cloud_agent.storage.notification_state",
        "if TYPE_CHECKING:\n    from cloud_agent.storage.repositories.admin.usage import Queries",
        "def flow():\n    from ..storage import _sql",
    ],
)
def test_storage_import_scanner_sees_every_storage_module(source: str) -> None:
    """Negative controls: every storage module but the assembly is refused."""
    targets = imported_modules(source, package="cloud_agent.application")
    assert any(_is_storage_internal(target) for target in targets)


@pytest.mark.parametrize(
    "source",
    [
        "from cloud_agent.storage.assembly import Storage",
        "if TYPE_CHECKING:\n    from ..storage.assembly import Storage",
        "from cloud_agent.storage import assembly",
        "import cloud_agent.storage",
        "from cloud_agent.ports.admin_queries import UsageQueryTooLarge",
        "import cloud_agent.storage_extra",
    ],
)
def test_storage_import_scanner_allows_the_assembly(source: str) -> None:
    """The assembly is the storage package's one entry point."""
    targets = imported_modules(source, package="cloud_agent.application")
    assert not any(_is_storage_internal(target) for target in targets)


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
