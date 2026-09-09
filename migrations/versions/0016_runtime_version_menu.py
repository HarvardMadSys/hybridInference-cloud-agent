"""Let an admin choose which agent CLI version a deployment runs.

Until now the version was decided when the sandbox image was built and could
not be changed without rebuilding it. 0007 made *what ran* a durable record;
this makes *what will run* a decision, and the two halves need one column each.

``agent_runner_hosts.runtime_versions_available`` is the menu — every version
this host's image actually holds, keyed by runtime id, beside the
``runtime_versions`` default 0007 already stores. The distinction is the whole
mechanism: the control plane cannot serve a version the image does not contain,
so the menu is the ceiling on what an admin may pick, and a host that stops
reporting a version stops being asked to run it.

``agent_runner_policy.runtime_versions`` is the selection — ``{runtime_id:
version}``, set from the admin page. It sits on the singleton policy row beside
``active_host`` because it is the same kind of fact (a deployment-wide
scheduling decision an operator makes) and because the claim already reads that
row under ``FOR SHARE``: the selection costs no extra read and inherits the lock
ordering documented on ``set_active_runner_host``.

An absent entry means "use the host's default", which is what every deployment
gets until someone chooses otherwise. Both columns are nullable and additive: a
host that has not upgraded keeps heartbeating, and a claim against a row written
before this migration resolves exactly as it did before.

Revision ID: 0016_runtime_version_menu
Revises: 0015_execution_profiles
"""

from __future__ import annotations

from alembic import op

revision = "0016_runtime_version_menu"
down_revision = "0015_execution_profiles"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Add the per-host menu and the deployment's selection."""
    op.execute(
        "ALTER TABLE agent_runner_hosts "
        "ADD COLUMN IF NOT EXISTS runtime_versions_available JSONB"
    )
    op.execute(
        "ALTER TABLE agent_runner_policy ADD COLUMN IF NOT EXISTS runtime_versions JSONB"
    )


def downgrade() -> None:
    """Drop them. Selections are lost; every job falls back to the default."""
    op.execute("ALTER TABLE agent_runner_policy DROP COLUMN IF EXISTS runtime_versions")
    op.execute(
        "ALTER TABLE agent_runner_hosts DROP COLUMN IF EXISTS runtime_versions_available"
    )
