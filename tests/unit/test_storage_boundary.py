"""The boundary stays where stage 3 put it.

Three properties, each of which was true of the code at some point and is only
true *now* because something was deleted:

* the old entry points are gone -- no ``storage.store``, no ``AgentJobStore``,
  no ``get_agent_job_store`` -- and cannot come back under the same names;
* nothing outside the storage package reaches for a pool it was not handed,
  which is how the admin read models were built before the assembly existed;
* the Postgres implementations are named in exactly one place. A caller that
  imports one is a caller that cannot be given a different implementation, and
  the point of the ports is that it can.

The scan reads source, not imports, so a module that is never imported by a
test is still checked.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
PRODUCTION = REPO_ROOT / "apps" / "backend" / "cloud_agent"
STORAGE = PRODUCTION / "storage"
TESTS = REPO_ROOT / "tests"

#: Where a concrete Postgres implementation may be named. The assembly, because
#: assembling them is what it is for; the repositories, because they are them.
ALLOWED_CONCRETE = {
    STORAGE / "assembly.py",
    *(STORAGE / "repositories").glob("*.py"),
}

#: Entry points stage 3 removed. Listed by name so that re-adding one is a
#: deliberate edit to this test and not a quiet return.
RETIRED = ("AgentJobStore", "get_agent_job_store", "cloud_agent.storage.store")


def _python_files(root: Path) -> list[Path]:
    return sorted(p for p in root.rglob("*.py") if "__pycache__" not in p.parts)


@pytest.mark.parametrize("name", RETIRED)
def test_the_retired_entry_points_are_gone(name: str) -> None:
    """Neither production nor the tests still name the monolith."""
    offenders = [
        f"{path.relative_to(REPO_ROOT)}:{n}"
        for path in _python_files(PRODUCTION) + _python_files(TESTS)
        if path != Path(__file__)  # this file names them to forbid them
        for n, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1)
        if name in line
    ]
    assert not offenders, f"{name} is still named: {offenders}"


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


def test_the_assembly_offers_one_attribute_per_port() -> None:
    """A capability that exists and is unreachable is a capability nobody uses."""
    ports = {p.stem for p in (PRODUCTION / "ports").glob("*.py") if p.name != "__init__.py"}
    tree = ast.parse((STORAGE / "assembly.py").read_text(encoding="utf-8"))
    init = next(
        fn
        for cls in tree.body
        if isinstance(cls, ast.ClassDef)
        for fn in cls.body
        if isinstance(fn, ast.FunctionDef) and fn.name == "__init__"
    )
    attributes = {
        node.target.attr
        for node in ast.walk(init)
        if isinstance(node, ast.AnnAssign)
        and isinstance(node.target, ast.Attribute)
        and isinstance(node.target.value, ast.Name)
        and node.target.value.id == "self"
    }
    assert ports <= attributes, f"ports with no way to reach them: {sorted(ports - attributes)}"
    assert attributes <= ports, (
        f"assembly attributes no port declares: {sorted(attributes - ports)}"
    )
