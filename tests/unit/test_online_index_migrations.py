"""Online index migrations remain resumable across autocommit boundaries."""

from __future__ import annotations

import runpy
from contextlib import nullcontext
from pathlib import Path
from types import SimpleNamespace

import pytest

_ROOT = Path(__file__).resolve().parents[2]
_MIGRATIONS = (
    "0066_admin_job_listing_index.py",
    "0085_terminal_resume_index.py",
    "0091_env_queued_runs_index.py",
    "0094_thread_message_pages.py",
    "0095_memory_retention.py",
    "0100_memory_archive_pages.py",
)


@pytest.mark.parametrize("filename", _MIGRATIONS)
def test_online_index_downgrade_can_retry_after_an_interrupted_drop(filename: str) -> None:
    """Every autocommitted drop tolerates a prior attempt having completed it."""
    migration = runpy.run_path(str(_ROOT / "migrations" / "versions" / filename))
    statements: list[str] = []
    downgrade = migration["downgrade"]
    downgrade.__globals__["context"] = SimpleNamespace(is_offline_mode=lambda: False)
    downgrade.__globals__["op"] = SimpleNamespace(
        execute=statements.append,
        get_context=lambda: SimpleNamespace(autocommit_block=nullcontext),
    )

    downgrade()

    drops = [
        statement for statement in statements if statement.startswith("DROP INDEX CONCURRENTLY")
    ]
    assert drops
    assert all(" IF EXISTS " in statement for statement in drops)
