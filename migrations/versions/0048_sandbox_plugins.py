"""Keep sandbox plugin selections for the lifetime of their chat.

Revision ID: 0048_sandbox_plugins
Revises: 0047_user_memory
"""

from alembic import op

revision = "0048_sandbox_plugins"
down_revision = "0047_user_memory"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Existing chats continue with an empty plugin selection."""
    op.execute("ALTER TABLE agent_threads ADD COLUMN plugins JSONB NOT NULL DEFAULT '[]'::jsonb")
    op.execute(
        "ALTER TABLE agent_threads ADD CONSTRAINT ck_agent_threads_plugins_array "
        "CHECK (jsonb_typeof(plugins) = 'array')"
    )


def downgrade() -> None:
    """Drop the thread plugin selections."""
    op.execute("ALTER TABLE agent_threads DROP COLUMN plugins")
