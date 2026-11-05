"""Host modules reach each other through public names; the old exceptions only shrink.

A host module that reads or calls another module's underscore attribute --
``manager._require(...)``, ``supervisor._lock`` -- depends on internals its
owner is free to change, and that is how security-sensitive checks end up
reimplemented or skipped by callers. This ratchet counts those accesses in
``apps/backend/cloud_agent_host`` and lets the count only go down.

The rule, kept simple on purpose: count every attribute access ``X._name``
(one leading underscore, not a dunder), except when

* ``X`` is ``self`` or ``cls``;
* ``_name`` is defined by a class in the same module -- a method, a class-level
  name, or a ``self._name = ...`` assignment -- which is how a module reaches
  its own objects (``other._x`` in ``__eq__``, a module helper over its own
  class). A :class:`typing.Protocol` does not count as defining a name: a
  protocol listing another class's private methods is what this test exists
  to catch;
* ``X`` is a module imported from outside this repository (``os._exit``).

The rule only undercounts: an access whose name happens to match one the
module defines is not counted. When the count drops, lower ``BASELINE``.
"""

from __future__ import annotations

import ast
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
HOST = REPO / "apps" / "backend" / "cloud_agent_host"
_OWN_PACKAGES = ("cloud_agent", "cloud_agent_host", "cloud_agent_shared")

#: Cross-module underscore accesses in cloud_agent_host. Only ever lower it.
BASELINE = 51


def _private(name: str) -> bool:
    return name.startswith("_") and not (name.startswith("__") and name.endswith("__"))


def _is_protocol(node: ast.ClassDef) -> bool:
    return any(
        (isinstance(base, ast.Name) and base.id == "Protocol")
        or (isinstance(base, ast.Attribute) and base.attr == "Protocol")
        for base in node.bases
    )


def _own_names(tree: ast.Module) -> set[str]:
    """Underscore attribute names a non-protocol class in this module defines."""
    names: set[str] = set()
    for cls in ast.walk(tree):
        if not isinstance(cls, ast.ClassDef) or _is_protocol(cls):
            continue
        for node in ast.walk(cls):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                names.add(node.name)
            elif isinstance(node, ast.Name) and isinstance(node.ctx, ast.Store):
                names.add(node.id)
            elif (
                isinstance(node, ast.Attribute)
                and isinstance(node.ctx, ast.Store)
                and isinstance(node.value, ast.Name)
                and node.value.id == "self"
            ):
                names.add(node.attr)
    return {name for name in names if _private(name)}


def _foreign_modules(tree: ast.Module) -> set[str]:
    """Names bound by ``import x`` of a module outside this repository."""
    bound: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name.split(".")[0] not in _OWN_PACKAGES:
                    bound.add(alias.asname or alias.name.split(".")[0])
    return bound


def _cross_module_accesses() -> list[str]:
    found: list[str] = []
    for path in sorted(HOST.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        own, foreign = _own_names(tree), _foreign_modules(tree)
        for node in ast.walk(tree):
            if not isinstance(node, ast.Attribute) or not _private(node.attr):
                continue
            base = node.value
            if isinstance(base, ast.Name) and base.id in {"self", "cls"} | foreign:
                continue
            if node.attr in own:
                continue
            where = path.relative_to(REPO).as_posix()
            found.append(f"{where}:{node.lineno}: {ast.unparse(node)}")
    return found


def test_host_modules_do_not_gain_cross_module_private_accesses() -> None:
    found = _cross_module_accesses()
    assert len(found) <= BASELINE, (
        f"{len(found)} cross-module underscore accesses, at most {BASELINE} allowed; "
        "use or add a public name on the owning object instead:\n" + "\n".join(found)
    )


def test_a_protocol_listing_private_methods_is_counted() -> None:
    tree = ast.parse(
        "from typing import Protocol\n"
        "class Containers(Protocol):\n"
        "    def _run(self) -> None: ...\n"
        "class Owner:\n"
        "    def _own(self) -> None: ...\n"
        "def use(containers, owner):\n"
        "    containers._run()\n"
        "    owner._own()\n"
    )

    assert _own_names(tree) == {"_own"}
