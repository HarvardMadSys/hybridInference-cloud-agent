"""Production and test modules have separate shrink-only file-size budgets.

The file-size half of the complexity ratchet (#771). A module listed below was
over the limit when the limit arrived. Its entry is the most it may hold, so
new code goes somewhere else, and shrinking it is how the list empties: lower
the entry when a module shrinks, and remove it once the module is back under
the limit, where the limit itself then holds it.

Tests have a deliberately wider 2,000-line limit: route and state-machine
cases need readable arrange/assert blocks, but no test module needs unbounded
growth. Their exact baseline is checked as well as enforced, so a split must
lower or remove its entry instead of leaving spare room behind.
"""

from __future__ import annotations

from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
LIMIT = 1500
TEST_LIMIT = 2000

#: Line counts of the modules over the limit when it arrived.
BASELINE = {
    "apps/backend/cloud_agent_host/shared_session_supervisor.py": 5380,
    "apps/backend/cloud_agent_host/runner.py": 4981,
    "apps/backend/cloud_agent_host/chat_sandbox.py": 4115,
    "apps/backend/cloud_agent_host/sandbox_manager.py": 3958,
    "apps/backend/cloud_agent/storage/repositories/compute_resources.py": 3856,
    "apps/backend/cloud_agent/storage/repositories/sandbox.py": 3480,
    "apps/backend/cloud_agent_host/drivers.py": 3052,
    "apps/backend/cloud_agent/api/worker.py": 2358,
    "apps/backend/cloud_agent/storage/repositories/host_pool.py": 2287,
    "apps/backend/cloud_agent/application/compute_reconcile.py": 2259,
    "apps/backend/cloud_agent/api/mcp_connections.py": 2220,
    "apps/backend/cloud_agent/storage/repositories/attempt_commands.py": 2041,
    "apps/backend/cloud_agent/storage/repositories/mcp.py": 1974,
    "apps/backend/cloud_agent/storage/repositories/environments.py": 1931,
    "apps/backend/cloud_agent/api/mcp_relay.py": 1798,
    "apps/backend/cloud_agent/storage/repositories/snapshot.py": 1673,
    "apps/backend/cloud_agent_host/host_capacity.py": 1636,
    "apps/backend/cloud_agent_host/chat_sandbox_network.py": 1595,
}

#: Exact line counts of test modules over their calibrated limit after #894's
#: first behavior-focused splits. Unlike the older production baseline, these
#: entries become stale on any shrink so they can only move downward.
TEST_BASELINE = {
    "tests/integration/servers/test_pending_message_queue.py": 2420,
    "tests/integration/storage/test_agent_job_store.py": 5966,
    "tests/integration/storage/test_compute_resources.py": 3862,
    "tests/integration/storage/test_goal_store.py": 2133,
    "tests/integration/storage/test_host_pool_store.py": 3431,
    "tests/integration/storage/test_mcp_connection_store.py": 3182,
    "tests/servers/test_admin_compute.py": 2101,
    "tests/servers/test_agent_jobs_api.py": 6328,
    "tests/unit/test_agent_publish_worker.py": 2080,
    "tests/unit/test_agent_runner.py": 2735,
    "tests/unit/test_agent_sandbox_manager.py": 3957,
    "tests/unit/test_chat_sandbox.py": 3190,
    "tests/unit/test_compute_providers.py": 2002,
    "tests/unit/test_compute_reconcile.py": 2291,
    "tests/unit/test_deploy_config.py": 2071,
    "tests/unit/test_host_pool_api.py": 3543,
    "tests/unit/test_mcp_connections_api.py": 3079,
    "tests/unit/test_retained_deploy.py": 2236,
    "tests/unit/test_runner_host_service.py": 2939,
    "tests/unit/test_shared_session_supervisor.py": 5362,
}


def _modules() -> dict[str, int]:
    """Every module under ``apps/backend`` and its line count, as ``wc -l`` counts them."""
    return {
        path.relative_to(REPO).as_posix(): path.read_bytes().count(b"\n")
        for path in (REPO / "apps" / "backend").rglob("*.py")
    }


def _test_modules() -> dict[str, int]:
    """Every Python test/support module and its physical line count."""
    return {
        path.relative_to(REPO).as_posix(): path.read_bytes().count(b"\n")
        for path in (REPO / "tests").rglob("*.py")
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


def test_no_test_module_grows_past_its_budget() -> None:
    over = {
        name: (lines, TEST_BASELINE.get(name, TEST_LIMIT))
        for name, lines in _test_modules().items()
        if lines > TEST_BASELINE.get(name, TEST_LIMIT)
    }
    assert not over, (
        f"test module over budget (lines, budget); split by behavior without weakening it: {over}"
    )


def test_the_test_baseline_is_exact_and_only_lists_offenders() -> None:
    modules = _test_modules()
    stale = {
        name: (modules.get(name), budget)
        for name, budget in TEST_BASELINE.items()
        if modules.get(name) != budget or budget <= TEST_LIMIT
    }
    assert not stale, (
        "test baseline is stale (lines, budget); lower it after a shrink, or remove the "
        f"entry once it reaches {TEST_LIMIT}: {stale}"
    )
