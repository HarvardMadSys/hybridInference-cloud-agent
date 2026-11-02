"""Store the deployment-wide retained-sandbox archive policy."""

from alembic import op

revision = "0042_sandbox_archive_settings"
down_revision = "0041_sandbox_disk_limit"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Add one revisioned policy to the existing runner-policy singleton."""
    op.execute(
        """
        ALTER TABLE agent_runner_policy
            ADD COLUMN sandbox_archive_settings JSONB NOT NULL DEFAULT
                '{
                    "enabled": false,
                    "region": "",
                    "kms_key_arn": "",
                    "expected_bucket_owner": "",
                    "max_archive_bytes": 2147483648,
                    "multipart_threshold_bytes": 8388608,
                    "multipart_part_bytes": 8388608
                }'::jsonb,
            ADD COLUMN sandbox_archive_revision BIGINT NOT NULL DEFAULT 0
                CHECK (sandbox_archive_revision >= 0),
            ADD COLUMN sandbox_archive_updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            ADD CONSTRAINT runner_policy_sandbox_archive_settings_object
                CHECK (jsonb_typeof(sandbox_archive_settings) = 'object')
        ;

        ALTER TABLE agent_runner_hosts
            ADD COLUMN sandbox_archive_settings_applied_revision BIGINT
                CHECK (sandbox_archive_settings_applied_revision >= 0)
        """
    )


def downgrade() -> None:
    """Remove archive policy storage without touching any archived objects."""
    op.execute(
        """
        ALTER TABLE agent_runner_hosts
            DROP COLUMN sandbox_archive_settings_applied_revision;

        ALTER TABLE agent_runner_policy
            DROP CONSTRAINT runner_policy_sandbox_archive_settings_object,
            DROP COLUMN sandbox_archive_updated_at,
            DROP COLUMN sandbox_archive_revision,
            DROP COLUMN sandbox_archive_settings
        """
    )
