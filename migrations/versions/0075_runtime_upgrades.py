"""Store agent CLI updates an admin asked for, and each host's progress.

Revision ID: 0075_runtime_upgrades
Revises: 0074_host_removal_pending

``agent_runner_policy.runtime_upgrades`` holds one record per runtime: the
version every host should download, the instant its dependencies resolve at,
whether new chats have been switched to it yet, and the download of the version
they use until then. Each heartbeat answer carries the versions to hold, and
each host reports how far it has got with each in
``agent_runner_hosts.runtime_downloads``. Both are additive and empty by
default, so a control plane or host from before this revision ignores them.
"""

from alembic import op

revision = "0075_runtime_upgrades"
down_revision = "0074_host_removal_pending"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Add the targets to the policy singleton and the reports to each host."""
    op.execute(
        """
        ALTER TABLE agent_runner_policy
            ADD COLUMN runtime_upgrades JSONB NOT NULL DEFAULT '{}'::jsonb,
            ADD CONSTRAINT runner_policy_runtime_upgrades_object
                CHECK (jsonb_typeof(runtime_upgrades) = 'object');

        ALTER TABLE agent_runner_hosts
            ADD COLUMN runtime_downloads JSONB,
            ADD CONSTRAINT runner_hosts_runtime_downloads_array
                CHECK (runtime_downloads IS NULL OR jsonb_typeof(runtime_downloads) = 'array')
        """
    )


def downgrade() -> None:
    """Forget the targets and reports; downloaded images stay on the hosts."""
    op.execute(
        """
        ALTER TABLE agent_runner_hosts
            DROP CONSTRAINT runner_hosts_runtime_downloads_array,
            DROP COLUMN runtime_downloads;

        ALTER TABLE agent_runner_policy
            DROP CONSTRAINT runner_policy_runtime_upgrades_object,
            DROP COLUMN runtime_upgrades
        """
    )
