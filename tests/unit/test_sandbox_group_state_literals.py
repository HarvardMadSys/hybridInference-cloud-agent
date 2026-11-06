"""Storage SQL names sandbox group states through ``domain/sandbox_group.py``.

``agent_sandboxes.state`` and ``deletion_state`` are compared in dozens of
statements. Each one spells a value through :class:`GroupState` or
:class:`DeletionState` (``{GroupState.COLD.sql}``, ``sql_list(...)``), so a
value has one definition and a misspelled one fails at import, not in a
query that silently matches nothing.

The scan reads every string in ``cloud_agent/storage`` and counts a comparison
of a quoted literal with a group's ``state`` or ``deletion_state``: any
``deletion_state`` (only ``agent_sandboxes`` has one), a ``state`` qualified by
an alias the same string binds to ``agent_sandboxes`` or by ``g`` (the alias
every shared fragment assumes), or an unqualified ``state`` in a string whose
only table is ``agent_sandboxes``. It does not see a fragment that names the
group through another alias it does not bind, nor the ``CASE`` arms of an
assignment. The count only goes down.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
STORAGE = REPO / "apps/backend/cloud_agent/storage"

#: Inline literals left. Lower it when one goes; never raise it.
BUDGET = 0

_COMPARISON = re.compile(
    r"(?<![\w.])(?:(?P<alias>\w+)\.)?(?P<column>state|deletion_state)\s*"
    r"(?:=|<>|!=|\bNOT\s+IN\b|\bIN\b)\s*(?:\(\s*'|')",
    re.IGNORECASE,
)
_TABLE = re.compile(
    r"\b(?:FROM|JOIN|UPDATE|INTO)\s+(\w+)(?=(?:\s+(?:AS\s+)?(\w+))?)", re.IGNORECASE
)
#: Words that may follow a table name where an alias would stand.
_NOT_ALIAS = frozenset(
    {
        "and",
        "as",
        "cross",
        "default",
        "for",
        "full",
        "group",
        "inner",
        "join",
        "lateral",
        "left",
        "limit",
        "natural",
        "on",
        "order",
        "returning",
        "right",
        "select",
        "set",
        "union",
        "using",
        "values",
        "where",
    }
)


def _strings(tree: ast.AST) -> list[tuple[int, str]]:
    """Every string of the module, an f-string's literal parts joined."""
    found: list[tuple[int, str]] = []
    parts: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.JoinedStr):
            text = "".join(
                value.value if isinstance(value, ast.Constant) else "{}" for value in node.values
            )
            parts.update(id(value) for value in node.values)
            found.append((node.lineno, text))
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str) and id(node) not in parts:
            found.append((node.lineno, node.value))
    return found


def group_state_literals(text: str) -> list[str]:
    """The inline group-state comparisons in one SQL string."""
    tables: dict[str, set[str]] = {}
    for match in _TABLE.finditer(text):
        aliases = tables.setdefault(match.group(1).lower(), set())
        alias = match.group(2)
        if alias and alias.lower() not in _NOT_ALIAS:
            aliases.add(alias)
    group_aliases = tables.get("agent_sandboxes", set()) | {"g"}
    only_groups = set(tables) == {"agent_sandboxes"}
    hits = []
    for match in _COMPARISON.finditer(text):
        alias, column = match.group("alias"), match.group("column").lower()
        if (
            column == "deletion_state"
            or (alias is not None and alias in group_aliases)
            or (alias is None and only_groups)
        ):
            hits.append(match.group(0))
    return hits


def _inline_literals(root: Path = STORAGE) -> list[str]:
    """``path:line: text`` for every inline group-state literal under ``root``."""
    found = []
    for path in sorted(root.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for lineno, text in _strings(tree):
            found.extend(
                f"{path.relative_to(REPO)}:{lineno}: {hit}" for hit in group_state_literals(text)
            )
    return found


def test_the_scan_recognizes_group_state_literals() -> None:
    """Each form the scan counts is counted, and other tables' states are not."""
    assert group_state_literals("UPDATE agent_sandboxes SET state = 'cold' WHERE id = $1")
    assert group_state_literals("g.state IN ('cold', 'restoring')")
    assert group_state_literals("SELECT 1 FROM agent_sandboxes WHERE deletion_state <> 'deleted'")
    assert group_state_literals(
        "FROM touched JOIN agent_sandboxes s ON s.id = touched.id AND s.state = 'running'"
    )
    assert not group_state_literals("g.state = {GroupState.COLD.sql}")
    assert not group_state_literals("UPDATE agent_jobs SET state = 'running' WHERE id = $1")
    assert not group_state_literals(
        "FROM agent_jobs j JOIN agent_sandboxes g ON TRUE WHERE j.state = 'running'"
    )
    assert not group_state_literals("m.resident_state = 'stopped'")


def test_storage_spells_no_group_state_inline() -> None:
    """The inline literals left in storage SQL stay within the budget."""
    found = _inline_literals()
    assert len(found) <= BUDGET, "name the value through cloud_agent.domain.sandbox_group:\n" + (
        "\n".join(found)
    )
