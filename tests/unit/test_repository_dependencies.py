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


#: The account repository predates packages: ``accounts.py`` composes the
#: mixins of the flat ``account_*`` modules, which are one repository with it.
FLAT_MIXIN_FAMILIES = {"accounts.py": f"{REPOSITORY_PACKAGE}.account_"}


def _forbidden_peer_imports(
    source: str, *, own_package: str | None = None, family: str | None = None
) -> list[str]:
    """Concrete classes and private names are implementation coupling.

    A capability split into a package of mixins (``sandbox/``) composes its
    own modules; only imports from outside ``own_package`` are peers.
    """
    found = []
    for node in ast.walk(ast.parse(source)):
        if not isinstance(node, ast.ImportFrom):
            continue
        if node.level > 1:
            # A relative import that leaves its own package reaches a peer.
            found.extend(f"{'.' * node.level}{node.module or ''}:{n.name}" for n in node.names)
            continue
        if node.module is None:
            continue
        if not (
            node.module == REPOSITORY_PACKAGE or node.module.startswith(f"{REPOSITORY_PACKAGE}.")
        ):
            continue
        if own_package is not None and (
            node.module == own_package or node.module.startswith(f"{own_package}.")
        ):
            continue
        if family is not None and node.module.startswith(family):
            continue
        found.extend(
            name.name
            for name in node.names
            if name.name.startswith(("Postgres", "_"))
            # A split repository's building blocks are concrete classes too.
            or name.name.endswith(("Mixin", "Base"))
        )
    return found


def test_repositories_do_not_import_peer_implementations_or_private_names() -> None:
    """A repository composes through narrow public transaction primitives."""
    offenders = []
    for path in sorted(REPOSITORIES.rglob("*.py")):
        # A split repository's package __init__ may hold its class; only the
        # repositories package's own __init__ is a plain index.
        if path == REPOSITORIES / "__init__.py" or ADMIN_REPOSITORIES in path.parents:
            continue
        package = path.parent.relative_to(REPOSITORIES).parts
        own_package = ".".join((REPOSITORY_PACKAGE, *package)) if package else None
        forbidden = _forbidden_peer_imports(
            path.read_text(encoding="utf-8"),
            own_package=own_package,
            family=FLAT_MIXIN_FAMILIES.get(path.name) if not package else None,
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
