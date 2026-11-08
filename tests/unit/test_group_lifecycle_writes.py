"""A sandbox group's lifecycle is assigned only by its own transitions.

``GroupLifecycleState`` (``supervisor/group_lifecycle_state.py``) is changed
only through its ``*_locked`` transitions, which check each change against
the A2 spec's preconditions and invariants. An assignment anywhere else would
skip those checks, so this scan of ``apps/backend/cloud_agent_host`` allows
none outside the two lifecycle modules: no assignment (plain, augmented or
annotated) and no ``setattr`` of

* a field of the state, through a group's ``life`` or a name ``life``;
* a group's ``life`` itself;
* any attribute under one of the flag names the state replaced, whatever
  object it is on;

and no ``_Group(...)`` keyword under one of those flag names. Constructing a
state (``GroupLifecycleState(...)``) is not an assignment and stays allowed.
"""

from __future__ import annotations

import ast
import dataclasses
from pathlib import Path

from cloud_agent_host.supervisor.group_lifecycle_state import GroupLifecycleState

REPO = Path(__file__).resolve().parents[2]
HOST = REPO / "apps" / "backend" / "cloud_agent_host"
LIFECYCLE_MODULES = frozenset(
    {
        HOST / "supervisor" / "group_lifecycle.py",
        HOST / "supervisor" / "group_lifecycle_state.py",
    }
)

#: The fields of the state.
STATE_FIELDS = frozenset(field.name for field in dataclasses.fields(GroupLifecycleState))
#: The ``_Group`` fields the state replaced (A2 spec section 2).
OLD_FIELDS = frozenset(
    {
        "starting",
        "stopping",
        "snapshotting",
        "lifecycle_check",
        "start_failed",
        "container_lost",
        "initial_archive_preservation_pending",
        "stop_archive_pending",
        "stop_archive_blocked",
        "stop_archive_members",
        "orphan_finalization_pending",
        "orphan_marker_unreadable",
        "start_archive_completion_pending",
        "start_seq",
        "run_unfenced",
        "sealed_seq",
        "archive_request_id",
        "lifecycle_generation",
    }
)


def _is_life(node: ast.expr) -> bool:
    """Whether ``node`` names a lifecycle state: ``<x>.life`` or ``life``."""
    return (isinstance(node, ast.Attribute) and node.attr == "life") or (
        isinstance(node, ast.Name) and node.id == "life"
    )


def _forbidden_target(target: ast.expr) -> str | None:
    for node in ast.walk(target):
        if not isinstance(node, ast.Attribute) or not isinstance(node.ctx, ast.Store):
            continue
        if node.attr in OLD_FIELDS:
            return node.attr
        if node.attr == "life":
            return "life"
        if node.attr in STATE_FIELDS and _is_life(node.value):
            return f"life.{node.attr}"
    return None


def _forbidden_call(call: ast.Call) -> str | None:
    func = call.func
    name = func.id if isinstance(func, ast.Name) else None
    if name == "setattr" and len(call.args) >= 2:
        target, attribute = call.args[0], call.args[1]
        literal = attribute.value if isinstance(attribute, ast.Constant) else None
        if _is_life(target):
            return f"setattr(life, {literal!r})"
        if literal in OLD_FIELDS or literal == "life":
            return f"setattr(..., {literal!r})"
    if name == "_Group":
        for keyword in call.keywords:
            if keyword.arg in OLD_FIELDS:
                return f"_Group({keyword.arg}=...)"
    return None


def lifecycle_writes(path: Path) -> list[str]:
    """Each statement in ``path`` that assigns lifecycle state, as ``line: what``."""
    found: list[str] = []
    for node in ast.walk(ast.parse(path.read_text(), filename=str(path))):
        targets: list[ast.expr] = []
        if isinstance(node, ast.Assign):
            targets = node.targets
        elif isinstance(node, (ast.AugAssign, ast.AnnAssign)):
            targets = [node.target]
        for target in targets:
            if (what := _forbidden_target(target)) is not None:
                found.append(f"{node.lineno}: {what}")
        if isinstance(node, ast.Call) and (what := _forbidden_call(node)) is not None:
            found.append(f"{node.lineno}: {what}")
    return found


def test_only_the_lifecycle_modules_assign_a_groups_lifecycle() -> None:
    offenders = {
        str(path.relative_to(REPO)): writes
        for path in sorted(HOST.rglob("*.py"))
        if path not in LIFECYCLE_MODULES and (writes := lifecycle_writes(path))
    }
    assert offenders == {}, (
        "assign a group's lifecycle only through a GroupLifecycleState transition"
    )


def test_the_scan_finds_each_kind_of_write(tmp_path: Path) -> None:
    source = tmp_path / "offender.py"
    source.write_text(
        "group.stopping = True\n"
        "group.life.stop = None\n"
        "life.generation += 1\n"
        "group.life = other\n"
        "setattr(group, 'snapshotting', False)\n"
        "setattr(group.life, name, value)\n"
        "_Group(spec=spec, sealed_seq=3)\n"
        "worker.stop = event\n"
        "GroupLifecycleState(condition, archives_finals=True, generation=2)\n"
    )
    assert lifecycle_writes(source) == [
        "1: stopping",
        "2: life.stop",
        "3: life.generation",
        "4: life",
        "5: setattr(..., 'snapshotting')",
        "6: setattr(life, None)",
        "7: _Group(sealed_seq=...)",
    ]
