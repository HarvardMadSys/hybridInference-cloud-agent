"""Repository composition uses public primitives, not peer implementations."""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
REPOSITORIES = REPO_ROOT / "apps" / "backend" / "cloud_agent" / "storage" / "repositories"
REPOSITORY_PACKAGE = "cloud_agent.storage.repositories"
#: The admin read models, which this guard has never read.
ADMIN_REPOSITORIES = REPOSITORIES / "admin"


def _forbidden_peer_imports(source: str, *, own_package: str | None = None) -> list[str]:
    """Concrete classes and private names are implementation coupling.

    A capability split into a package of mixins (``sandbox/``) composes its
    own modules; only imports from outside ``own_package`` are peers.
    """
    found = []
    for node in ast.walk(ast.parse(source)):
        if not isinstance(node, ast.ImportFrom) or node.module is None:
            continue
        if not (
            node.module == REPOSITORY_PACKAGE or node.module.startswith(f"{REPOSITORY_PACKAGE}.")
        ):
            continue
        if own_package is not None and (
            node.module == own_package or node.module.startswith(f"{own_package}.")
        ):
            continue
        found.extend(
            name.name
            for name in node.names
            if name.name.startswith("Postgres") or name.name.startswith("_")
        )
    return found


def test_repositories_do_not_import_peer_implementations_or_private_names() -> None:
    """A repository composes through narrow public transaction primitives."""
    offenders = []
    for path in sorted(REPOSITORIES.rglob("*.py")):
        if path.name == "__init__.py" or ADMIN_REPOSITORIES in path.parents:
            continue
        package = path.parent.relative_to(REPOSITORIES).parts
        own_package = ".".join((REPOSITORY_PACKAGE, *package)) if package else None
        forbidden = _forbidden_peer_imports(
            path.read_text(encoding="utf-8"), own_package=own_package
        )
        if forbidden:
            offenders.append(f"{path.relative_to(REPO_ROOT)}: {forbidden}")
    assert not offenders, f"repository implementation dependencies: {offenders}"


@pytest.mark.parametrize(
    ("source", "expected"),
    [
        (
            "from cloud_agent.storage.repositories.jobs import PostgresJobs",
            ["PostgresJobs"],
        ),
        (
            "from cloud_agent.storage.repositories.teams import _decode",
            ["_decode"],
        ),
        (
            "from cloud_agent.storage.repositories.jobs import insert_follow_up",
            [],
        ),
        (
            "from cloud_agent.storage.repositories.environments import lock_environment_snapshots",
            [],
        ),
    ],
)
def test_dependency_guard_targets_only_implementation_details(
    source: str, expected: list[str]
) -> None:
    """Public transaction helpers remain available for atomic composition."""
    assert _forbidden_peer_imports(source) == expected
