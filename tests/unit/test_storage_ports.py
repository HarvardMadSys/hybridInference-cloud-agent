"""The storage ports describe the repositories, and nothing else does.

Three properties:

* every public repository method is claimed by exactly one port, so a
  capability cannot quietly belong to nobody;
* every admin read model is described by the port named after it, method for
  method: they share method names (``detail``), so they are paired by class
  rather than inventoried by name;
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
#: The admin read models and their ports, checked pairwise rather than by name.
ADMIN_REPOSITORIES_DIR = REPOSITORIES_DIR / "admin"
ADMIN_PORTS = PORTS_DIR / "admin_queries.py"

#: Repository methods with no port, because the port only mirrored its
#: repository (structural-refactor spec, section 7.3). The assembly names the
#: repository class, and each caller declares the narrow Protocol it uses.
UNPORTED: set[str] = {
    "get_model_preference",
    "prune_notification_changes",
    "recent_requests",
}


def _method_names(path: Path, *, protocols: bool = False) -> set[str]:
    """The method inventory of a capability's protocol or repository."""
    tree = ast.parse(path.read_text())
    classes = [n for n in tree.body if isinstance(n, ast.ClassDef)]
    if protocols:
        classes = [
            node
            for node in classes
            if any(isinstance(base, ast.Name) and base.id == "Protocol" for base in node.bases)
        ]
        assert classes, f"{path}: expected at least one protocol"
    else:
        assert len(classes) == 1, f"{path}: expected one class, found {len(classes)}"
    return {
        node.name
        for class_node in classes
        for node in class_node.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    }


#: Shared vocabulary beside the ports, with no protocol of its own.
NOT_PORTS = {"__init__.py", "errors.py"}


def _port_files() -> list[Path]:
    files = sorted(p for p in PORTS_DIR.glob("*.py") if p.name not in NOT_PORTS)
    assert files, f"no capability ports found under {PORTS_DIR}"
    return files


def _inventoried_port_files() -> list[Path]:
    """The ports whose methods are claimed by name; the admin ports are paired instead."""
    return [path for path in _port_files() if path != ADMIN_PORTS]


def _repository_files() -> list[Path]:
    """One module per capability; together, the whole storage implementation."""
    assert REPOSITORIES_DIR.is_dir(), REPOSITORIES_DIR
    files = sorted(p for p in REPOSITORIES_DIR.glob("*.py") if p.name != "__init__.py")
    assert files, f"no repositories found under {REPOSITORIES_DIR}"
    return files


def _repository_packages() -> list[Path]:
    """Repositories split into a package of mixins, one module per concern."""
    return sorted(
        path
        for path in REPOSITORIES_DIR.iterdir()
        if path.is_dir() and path != ADMIN_REPOSITORIES_DIR and (path / "__init__.py").is_file()
    )


def _package_method_names(package: Path) -> set[str]:
    """The method inventory of a repository package: its class and every mixin."""
    return {
        node.name
        for path in sorted(package.glob("*.py"))
        for class_node in ast.parse(path.read_text()).body
        if isinstance(class_node, ast.ClassDef)
        for node in class_node.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    }


def _implementations() -> set[str]:
    """Every method a port can be satisfied by.

    One entry per name, and the repositories are the only source: with the
    store deleted there is no second implementation for a port to match
    against by accident.
    """
    found: set[str] = set()
    inventories = [_method_names(path) for path in _repository_files()]
    inventories += [_package_method_names(path) for path in _repository_packages()]
    for names in inventories:
        for name in names:
            assert name not in found or name == "__init__", f"{name}: two repositories define it"
            found.add(name)
    return found


def test_every_public_repository_method_is_claimed_once() -> None:
    """Nothing is left behind, and nothing is claimed by two ports."""
    store = _implementations()
    public = {name for name in store if not name.startswith("_")}
    claimed: dict[str, str] = {}
    for path in _inventoried_port_files():
        for name in _method_names(path, protocols=True):
            assert name not in claimed, f"{name}: both {claimed[name]} and {path.name}"
            claimed[name] = path.name
    assert sorted(public - set(claimed)) == sorted(UNPORTED)
    assert not set(claimed) - public


def _public_methods(node: ast.ClassDef) -> set[str]:
    """Methods a caller can call; a property is an attribute, not a method."""
    return {
        child.name
        for child in node.body
        if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef))
        and not child.name.startswith("_")
        and not any(
            isinstance(decorator, ast.Name) and decorator.id == "property"
            for decorator in child.decorator_list
        )
    }


def test_every_admin_read_model_is_described_by_its_port() -> None:
    """``PostgresAdminXQueries`` implements ``AdminXQueries``, and nothing is left over."""
    protocols = {
        node.name: _public_methods(node)
        for node in ast.parse(ADMIN_PORTS.read_text()).body
        if isinstance(node, ast.ClassDef)
        and any(isinstance(base, ast.Name) and base.id == "Protocol" for base in node.bases)
    }
    implemented: set[str] = set()
    modules = sorted(p for p in ADMIN_REPOSITORIES_DIR.glob("*.py") if p.name != "__init__.py")
    assert modules, f"no admin read models found under {ADMIN_REPOSITORIES_DIR}"
    for path in modules:
        classes = [n for n in ast.parse(path.read_text()).body if isinstance(n, ast.ClassDef)]
        assert len(classes) == 1, f"{path}: expected one class, found {len(classes)}"
        port = classes[0].name.removeprefix("Postgres")
        assert port != classes[0].name, f"{path}: {port} is not a Postgres implementation"
        assert port in protocols, f"{path}: no port {port} in {ADMIN_PORTS.name}"
        assert _public_methods(classes[0]) == protocols[port], f"{path}: differs from {port}"
        implemented.add(port)
    assert implemented == set(protocols), (
        f"ports with no implementation: {set(protocols) - implemented}"
    )


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
