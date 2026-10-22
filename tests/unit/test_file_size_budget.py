"""No backend module grows past 1,500 lines, and none already past it grows.

The file-size half of the complexity ratchet (#771). A module listed below was
over the limit when the limit arrived. Its entry is the most it may hold, so
new code goes somewhere else, and shrinking it is how the list empties: lower
the entry when a module shrinks, and remove it once the module is back under
the limit, where the limit itself then holds it.
"""

from __future__ import annotations

from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
LIMIT = 1500

#: Line counts of the modules over the limit when it arrived.
BASELINE = {
    "apps/backend/cloud_agent_host/shared_session_supervisor.py": 5405,
    "apps/backend/cloud_agent_host/runner.py": 5034,
    "apps/backend/cloud_agent_host/chat_sandbox.py": 4194,
    "apps/backend/cloud_agent_host/sandbox_manager.py": 4050,
    "apps/backend/cloud_agent/storage/repositories/compute_resources.py": 3856,
    "apps/backend/cloud_agent/storage/repositories/sandbox.py": 3480,
    "apps/backend/cloud_agent_host/drivers.py": 3052,
    "apps/backend/cloud_agent/api/worker.py": 2363,
    "apps/backend/cloud_agent/storage/repositories/host_pool.py": 2287,
    "apps/backend/cloud_agent/application/compute_reconcile.py": 2259,
    "apps/backend/cloud_agent/api/mcp_connections.py": 2220,
    "apps/backend/cloud_agent/storage/repositories/attempt_commands.py": 2041,
    "apps/backend/cloud_agent/storage/repositories/mcp.py": 1974,
    "apps/backend/cloud_agent/storage/repositories/environments.py": 1931,
    "apps/backend/cloud_agent/api/mcp_relay.py": 1801,
    "apps/backend/cloud_agent/storage/repositories/snapshot.py": 1701,
    "apps/backend/cloud_agent_host/host_capacity.py": 1636,
    "apps/backend/cloud_agent_host/chat_sandbox_network.py": 1595,
}


def _modules() -> dict[str, int]:
    """Every module under ``apps/backend`` and its line count, as ``wc -l`` counts them."""
    return {
        path.relative_to(REPO).as_posix(): path.read_bytes().count(b"\n")
        for path in (REPO / "apps" / "backend").rglob("*.py")
    }


def test_no_module_grows_past_its_budget() -> None:
    over = {
        name: (lines, BASELINE.get(name, LIMIT))
        for name, lines in _modules().items()
        if lines > BASELINE.get(name, LIMIT)
    }
    assert not over, (
        f"over budget (lines, budget); move the new code to a module of its own: {over}"
    )


def test_the_baseline_lists_only_modules_still_over_the_limit() -> None:
    modules = _modules()
    stale = {name: modules.get(name) for name in BASELINE if modules.get(name, 0) <= LIMIT}
    assert not stale, f"back under the limit or gone; remove them from BASELINE: {stale}"
