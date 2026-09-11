"""The storage ports describe the store, and nothing else describes the store.

Three properties, each of which has already been violated once by a draft of
this refactor somewhere else:

* every port method exists on ``AgentJobStore`` with the same signature, so a
  port cannot describe an operation the store does not have;
* every public store method is claimed by exactly one port, so a capability
  cannot quietly belong to nobody;
* no port imports ``cloud_agent.storage``, so the dependency keeps pointing
  inward.

A runtime ``isinstance`` check would prove much less than this: a
``runtime_checkable`` Protocol only tests that a member *exists*, not that it
takes the same arguments. The comparison here is on the parsed signature.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
PORTS_DIR = REPO_ROOT / "cloud_agent" / "ports"
STORE_PATH = REPO_ROOT / "cloud_agent" / "storage" / "store.py"

#: ``initialize`` is the store's own lifecycle call, not a capability any caller
#: asks a port for. It leaves with the startup path in stage 3.
UNPORTED = {"initialize"}


def _methods(path: Path, class_name: str | None = None) -> dict[str, tuple[str, str, str]]:
    """name -> (async-or-not, arguments, return annotation, docstring)."""
    tree = ast.parse(path.read_text())
    classes = [n for n in tree.body if isinstance(n, ast.ClassDef)]
    if class_name is not None:
        classes = [c for c in classes if c.name == class_name]
    assert len(classes) == 1, f"{path}: expected one class, found {len(classes)}"
    found: dict[str, tuple[str, str, str, str]] = {}
    for node in classes[0].body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            found[node.name] = (
                type(node).__name__,
                ast.unparse(node.args),
                ast.unparse(node.returns) if node.returns else "",
                ast.get_docstring(node, clean=True) or "",
            )
    return found


def _port_files() -> list[Path]:
    return sorted(p for p in PORTS_DIR.glob("*.py") if p.name != "__init__.py")


def test_the_ports_directory_is_not_empty() -> None:
    """A scan that silently matches nothing would pass every test below."""
    assert len(_port_files()) == 16


@pytest.mark.parametrize("path", _port_files(), ids=lambda p: p.stem)
def test_every_port_method_matches_the_store(path: Path) -> None:
    """Same name, same async-ness, same signature, same documented contract."""
    store = _methods(STORE_PATH, "AgentJobStore")
    port = _methods(path)
    missing = sorted(name for name in port if name not in store)
    assert not missing, f"{path.name}: not on AgentJobStore: {missing}"
    for name, described in port.items():
        actual = store[name]
        assert described[0] == actual[0], f"{path.name}.{name}: async-ness differs"
        assert described[1] == actual[1], f"{path.name}.{name}: arguments differ"
        assert described[2] == actual[2], f"{path.name}.{name}: return annotation differs"
        assert described[3] == actual[3], f"{path.name}.{name}: docstring differs"


def test_every_public_store_method_is_claimed_once() -> None:
    """Nothing is left behind, and nothing is claimed by two ports."""
    store = _methods(STORE_PATH, "AgentJobStore")
    public = {name for name in store if not name.startswith("_")}
    claimed: dict[str, str] = {}
    for path in _port_files():
        for name in _methods(path):
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


def test_the_ports_cover_every_capability_the_callers_reach_for() -> None:
    """Start from the call sites, not from the ports.

    The three tests above read the ports and check them against the store. This
    one goes the other way: it collects every attribute call in the control
    plane whose name is a public store method, and requires that each name
    belongs to a port. A capability a caller actually uses, and no port
    declares, is a hole in the boundary that no amount of reading the ports
    would reveal.

    The scan is name-based, so it over-collects: another class with a method of
    the same name is counted too. That direction is safe here -- a false
    positive can only make this test stricter, never more permissive.
    """
    store = _methods(STORE_PATH, "AgentJobStore")
    public = {name for name in store if not name.startswith("_")}
    claimed = {name for path in _port_files() for name in _methods(path)}

    reached: dict[str, set[str]] = {}
    for path in sorted((REPO_ROOT / "cloud_agent").rglob("*.py")):
        if path == STORE_PATH:
            continue
        for node in ast.walk(ast.parse(path.read_text())):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr in public
            ):
                reached.setdefault(node.func.attr, set()).add(str(path.relative_to(REPO_ROOT)))

    assert reached, "the scan found no call sites at all, so it proves nothing"
    uncovered = {
        name: sorted(files)
        for name, files in reached.items()
        if name not in claimed and name not in UNPORTED
    }
    assert not uncovered, f"called but claimed by no port: {uncovered}"
