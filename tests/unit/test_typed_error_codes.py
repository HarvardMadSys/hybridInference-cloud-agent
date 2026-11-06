"""Callers tell refusals apart by a typed code, never by an exception's text.

An unrelated ``ValueError`` whose message happens to read ``operation_lease_lost``
must not be handled as a lost lease. Store refusals are
``cloud_agent.ports.errors.StorageConflict`` and carry ``code``; other typed
refusals are their own exception classes. So no handler under
``apps/backend/cloud_agent`` tests ``str(exc)`` of the exception it caught for
equality with a code (``==``, ``!=``) or for membership in a set of codes
(``str(exc) in {...}``), whether inline or through a name the handler binds
to it first (``code = str(exc)`` then ``code == ...`` or ``code in {...}``).
Searching a driver's message for a substring (``"name" in str(exc)``) is a
different question and stays allowed.

The scan is deliberately small: it reads one handler body at a time and follows
only plain ``name = str(exc)`` assignments made inside it.
"""

from __future__ import annotations

import ast
import pickle
from pathlib import Path

from cloud_agent.ports.errors import ConflictCode, StorageConflict

REPO = Path(__file__).resolve().parents[2]
CONTROL = REPO / "apps" / "backend" / "cloud_agent"


def _is_text_of(node: ast.expr, name: str) -> bool:
    """``str(<name>)``."""
    return (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "str"
        and len(node.args) == 1
        and isinstance(node.args[0], ast.Name)
        and node.args[0].id == name
    )


def _text_aliases(body: ast.Module, name: str) -> set[str]:
    """Names a handler binds to ``str(<name>)``: ``code = str(exc)``."""
    return {
        target.id
        for node in ast.walk(body)
        if isinstance(node, ast.Assign) and _is_text_of(node.value, name)
        for target in node.targets
        if isinstance(target, ast.Name)
    }


def _text_comparisons(source: str) -> list[int]:
    """Lines where a handler matches ``str(<caught exception>)`` against codes."""
    found: list[int] = []
    for handler in ast.walk(ast.parse(source)):
        if not isinstance(handler, ast.ExceptHandler) or handler.name is None:
            continue
        body = ast.Module(body=handler.body, type_ignores=[])
        aliases = _text_aliases(body, handler.name)

        def is_text(node: ast.expr, name: str = handler.name, aliases: set[str] = aliases) -> bool:
            return _is_text_of(node, name) or (isinstance(node, ast.Name) and node.id in aliases)

        for node in ast.walk(body):
            if not isinstance(node, ast.Compare):
                continue
            equality = any(isinstance(op, ast.Eq | ast.NotEq) for op in node.ops)
            operands = [node.left, *node.comparators]
            if is_text(node.left) or (equality and any(is_text(operand) for operand in operands)):
                found.append(node.lineno)
    return found


def test_the_scan_finds_a_text_comparison() -> None:
    source = (
        "try:\n    pass\nexcept ValueError as exc:\n"
        "    if str(exc) == 'operation_lease_lost':\n        raise\n"
        "    if str(exc) not in {'a', 'b'}:\n        raise\n"
        "    if 'constraint' in str(exc):\n        raise\n"
    )
    assert _text_comparisons(source) == [4, 6]


def test_the_scan_follows_a_name_bound_to_the_text() -> None:
    source = (
        "try:\n    pass\nexcept ValueError as exc:\n"
        "    code = str(exc)\n"
        "    if code == 'resource_retiring':\n        raise\n"
        "    if code in {'a', 'b'}:\n        raise\n"
        "    if 'constraint' in code:\n        raise\n"
        "    logger.warning('refused', extra={'reason': code})\n"
    )
    assert _text_comparisons(source) == [5, 7]


def test_no_handler_compares_exception_text() -> None:
    offenders = [
        f"{path.relative_to(REPO)}:{line}"
        for path in sorted(CONTROL.rglob("*.py"))
        for line in _text_comparisons(path.read_text())
    ]
    assert not offenders, f"compare a typed code instead of str(exc): {offenders}"


def test_a_conflict_reads_as_its_code() -> None:
    """Logs, records and tests that read ``str(exc)`` still see the code."""
    exc = StorageConflict(ConflictCode.OPERATION_LEASE_LOST)
    assert isinstance(exc, ValueError)
    assert str(exc) == "operation_lease_lost"
    assert exc.code is ConflictCode.OPERATION_LEASE_LOST
    restored = pickle.loads(pickle.dumps(exc))
    assert restored.code is ConflictCode.OPERATION_LEASE_LOST
