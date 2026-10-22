"""Repository composition uses public primitives, not peer implementations."""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
REPOSITORIES = REPO_ROOT / "apps" / "backend" / "cloud_agent" / "storage" / "repositories"
REPOSITORY_PACKAGE = "cloud_agent.storage.repositories"


def _forbidden_peer_imports(source: str) -> list[str]:
    """Concrete classes and private names are implementation coupling."""
    found = []
    for node in ast.walk(ast.parse(source)):
        if not isinstance(node, ast.ImportFrom) or node.module is None:
            continue
        if not (
            node.module == REPOSITORY_PACKAGE or node.module.startswith(f"{REPOSITORY_PACKAGE}.")
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
    for path in sorted(REPOSITORIES.glob("*.py")):
        if path.name == "__init__.py":
            continue
        forbidden = _forbidden_peer_imports(path.read_text(encoding="utf-8"))
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
