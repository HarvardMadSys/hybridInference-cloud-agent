"""Find the storage statements that write a table, by reading the source.

A statement is a string expression in ``cloud_agent/storage``: a literal, an
f-string, or a ``+`` concatenation of them, with an f-string's replacement
fields read as ``{}``. Its *anchor* is the module-level constant it is
assigned to, or else the function it is written in, so a statement can be
named in a test without a line number.
"""

from __future__ import annotations

import ast
import re
from dataclasses import dataclass
from pathlib import Path

STORAGE = Path(__file__).resolve().parents[2] / "apps/backend/cloud_agent/storage"


@dataclass(frozen=True, slots=True)
class Write:
    """One ``UPDATE`` or ``INSERT INTO`` of a table, and the columns it sets."""

    #: The module's path under ``cloud_agent/storage``, e.g. ``_sql/hosts.py``.
    module: str
    anchor: str
    #: ``UPDATE`` or ``INSERT``.
    verb: str
    #: The asked-for columns the statement sets.
    columns: frozenset[str]


def _text(node: ast.expr) -> str:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.JoinedStr):
        return "".join(
            value.value if isinstance(value, ast.Constant) else "{}" for value in node.values
        )
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
        return _text(node.left) + _text(node.right)
    return "{}"


def _statements(tree: ast.Module) -> list[tuple[str, str]]:
    """Every string expression of ``tree`` with its anchor, outermost only."""
    parents = {child: node for node in ast.walk(tree) for child in ast.iter_child_nodes(node)}
    found: list[tuple[str, str]] = []
    seen: set[int] = set()
    for node in ast.walk(tree):
        is_string = isinstance(node, ast.JoinedStr) or (
            isinstance(node, ast.Constant) and isinstance(node.value, str)
        )
        if not is_string or isinstance(parents.get(node), ast.JoinedStr):
            continue
        root: ast.AST = node
        while isinstance(parents.get(root), ast.BinOp):
            root = parents[root]
        if id(root) in seen:
            continue
        seen.add(id(root))
        parent = parents.get(root)
        if (
            isinstance(parent, ast.Assign)
            and parents.get(parent) is tree
            and isinstance(parent.targets[0], ast.Name)
        ):
            anchor = parent.targets[0].id
        else:
            scope = parent
            while scope is not None and not isinstance(
                scope, ast.FunctionDef | ast.AsyncFunctionDef
            ):
                scope = parents.get(scope)
            anchor = scope.name if scope is not None else "<module>"
        found.append((anchor, _text(root)))  # type: ignore[arg-type]
    return found


def writes(table: str, columns: tuple[str, ...] = ()) -> list[Write]:
    """Every statement in storage that inserts into or updates ``table``.

    ``columns`` are the columns to report: for an ``UPDATE``, the ones its
    ``SET`` assigns unqualified; for an ``INSERT``, the ones its column list
    names (a column it leaves out takes its default).
    """
    verb = re.compile(rf"\b(UPDATE|INSERT\s+INTO)\s+{table}\b", re.IGNORECASE)
    assigned = re.compile(r"(?<![\w.])(" + "|".join(columns) + r")\s*=") if columns else None
    found: list[Write] = []
    for path in sorted(STORAGE.rglob("*.py")):
        module = path.relative_to(STORAGE).as_posix()
        for anchor, text in _statements(ast.parse(path.read_text(encoding="utf-8"))):
            for match in verb.finditer(text):
                rest = text[match.end() :]
                if match.group(1).upper().startswith("INSERT"):
                    names = rest[: rest.find(")")]
                    written = {c for c in columns if re.search(rf"\b{c}\b", names)}
                    found.append(Write(module, anchor, "INSERT", frozenset(written)))
                    continue
                set_clause = re.search(r"\bSET\b", rest)
                body = rest[set_clause.end() :] if set_clause else ""
                end = re.search(r"\b(WHERE|FROM|RETURNING)\b", body)
                body = body[: end.start()] if end else body
                written = set(assigned.findall(body)) if assigned else set()
                found.append(Write(module, anchor, "UPDATE", frozenset(written)))
    return found
