"""SQL rules for environment runs: the setup tests and prebuilds a Host runs.

An environment run reserves one slot on its Host while it is live: its lease
is current and it started less than ``ENV_RUN_MAX_AGE`` ago. Cancelled, it
keeps the slot until its cleanup is confirmed or it stops being live. A run
that is no longer live is stale: it reserves nothing, but its Host takes no
new work until it confirms the run's cleanup, and the reaper fails it.
"""

from __future__ import annotations

# The longest one environment run may hold a slot, however often it renews
# its lease.
ENV_RUN_MAX_AGE = "INTERVAL '30 minutes'"


def _columns(run: str) -> str:
    return f"{run}." if run else ""


def env_run_live(run: str = "") -> str:
    """SQL: whether environment run ``run`` has a current lease and is not too old, in parentheses.

    ``run`` is the alias its columns are qualified with; by default they are
    not qualified.
    """
    c = _columns(run)
    return f"({c}lease_expires_at>NOW() AND {c}started_at>NOW()-{ENV_RUN_MAX_AGE})"


def env_run_stale(run: str = "") -> str:
    """SQL: whether environment run ``run`` is no longer live, in parentheses."""
    c = _columns(run)
    return f"({c}lease_expires_at<=NOW() OR {c}started_at<=NOW()-{ENV_RUN_MAX_AGE})"


def env_run_holds_slot(run: str = "") -> str:
    """SQL: whether environment run ``run`` reserves a slot on its Host, in parentheses."""
    return f"(NOT {_columns(run)}cleanup_complete AND {env_run_live(run)})"


def env_run_blocks_host(run: str = "") -> str:
    """SQL: whether environment run ``run`` keeps its Host from taking new work, in parentheses."""
    return f"(NOT {_columns(run)}cleanup_complete AND {env_run_stale(run)})"


def env_runs_holding_slots(host: str) -> str:
    """SQL: how many slots environment runs reserve on ``host``, a scalar subquery."""
    return (
        "(SELECT COUNT(*) FROM agent_environment_runs r "
        f"WHERE r.host={host} AND {env_run_holds_slot('r')})"
    )
