"""The storage ports describe the repositories, and nothing else does.

Two properties:

* every public repository method is claimed by exactly one port, so a
  capability cannot quietly belong to nobody;
* no port imports ``cloud_agent.storage``, so the dependency keeps pointing
  inward.

Signature compatibility is checked by mypy at the typed repository assignments
in ``storage.assembly.Storage``.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
PORTS_DIR = REPO_ROOT / "apps" / "backend" / "cloud_agent" / "ports"
REPOSITORIES_DIR = REPO_ROOT / "apps" / "backend" / "cloud_agent" / "storage" / "repositories"

#: Nothing is left unclaimed.
UNPORTED: set[str] = set()


def _method_names(path: Path) -> set[str]:
    """The method inventory of a capability's protocol or repository."""
    tree = ast.parse(path.read_text())
    classes = [n for n in tree.body if isinstance(n, ast.ClassDef)]
    assert len(classes) == 1, f"{path}: expected one class, found {len(classes)}"
    return {
        node.name
        for node in classes[0].body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    }


def _port_files() -> list[Path]:
    files = sorted(p for p in PORTS_DIR.glob("*.py") if p.name != "__init__.py")
    assert files, f"no capability ports found under {PORTS_DIR}"
    return files


def _repository_files() -> list[Path]:
    """One module per capability; together, the whole storage implementation."""
    assert REPOSITORIES_DIR.is_dir(), REPOSITORIES_DIR
    files = sorted(p for p in REPOSITORIES_DIR.glob("*.py") if p.name != "__init__.py")
    assert files, f"no repositories found under {REPOSITORIES_DIR}"
    return files


def _implementations() -> set[str]:
    """Every method a port can be satisfied by.

    One entry per name, and the repositories are the only source: with the
    store deleted there is no second implementation for a port to match
    against by accident.
    """
    found: set[str] = set()
    for path in _repository_files():
        for name in _method_names(path):
            assert name not in found or name == "__init__", f"{name}: two repositories define it"
            found.add(name)
    return found


def test_every_public_repository_method_is_claimed_once() -> None:
    """Nothing is left behind, and nothing is claimed by two ports."""
    store = _implementations()
    public = {name for name in store if not name.startswith("_")}
    claimed: dict[str, str] = {}
    for path in _port_files():
        for name in _method_names(path):
            assert name not in claimed, f"{name}: both {claimed[name]} and {path.name}"
            claimed[name] = path.name
    assert sorted(public - set(claimed)) == sorted(UNPORTED)
    assert not set(claimed) - public


@pytest.mark.parametrize("path", _port_files(), ids=lambda p: p.stem)
def test_a_port_never_imports_storage(path: Path) -> None:
    """The dependency points inward; a port that imports storage reverses it."""
    tree = ast.parse(path.read_text())
    offenders = []
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module:
            if node.module.startswith("cloud_agent.storage"):
                offenders.append(node.module)
        elif isinstance(node, ast.Import):
            offenders += [a.name for a in node.names if a.name.startswith("cloud_agent.storage")]
    assert not offenders, f"{path.name}: imports {offenders}"
