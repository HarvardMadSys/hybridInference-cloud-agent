"""Few modules write a sandbox group or its memberships, and fewer over time.

``agent_sandboxes`` and ``agent_thread_sandbox_memberships`` were written from
eleven storage modules. The repositories' statements now live in
``storage/_sql/sandbox_transitions.py``, executed by the repository methods
that always ran them, and the ``_sql`` helpers that wrote these tables keep
their own. The number of modules issuing an ``UPDATE`` or ``INSERT INTO``
against either table only goes down.
"""

from __future__ import annotations

import re
from pathlib import Path

from tests.support.sql_writes import writes

CONTROL_PLANE = Path(__file__).resolve().parents[2] / "apps/backend/cloud_agent"
TABLES = ("agent_sandboxes", "agent_thread_sandbox_memberships")

#: Modules that write either table. Lower it when one stops; never raise it.
BUDGET = 4


def test_few_modules_write_the_sandbox_tables() -> None:
    modules = {write.module for table in TABLES for write in writes(table)}
    assert len(modules) <= BUDGET, f"modules writing {TABLES}: {sorted(modules)}"
    assert len(modules) == BUDGET, (
        f"only {len(modules)} modules write these tables now; lower BUDGET to match"
    )
    assert "_sql/sandbox_transitions.py" in modules


def test_nothing_outside_storage_writes_the_sandbox_tables() -> None:
    statement = re.compile(rf"\b(UPDATE|INSERT\s+INTO)\s+({'|'.join(TABLES)})\b", re.IGNORECASE)
    outside = [
        path.relative_to(CONTROL_PLANE).as_posix()
        for path in sorted(CONTROL_PLANE.rglob("*.py"))
        if "storage" not in path.relative_to(CONTROL_PLANE).parts
        and statement.search(path.read_text(encoding="utf-8"))
    ]
    assert outside == []
