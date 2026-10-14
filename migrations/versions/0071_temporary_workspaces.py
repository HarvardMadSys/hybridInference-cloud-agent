"""Let a conversation start without a repository.

Revision ID: 0071_temporary_workspaces
Revises: 0070_memory_cutover

A temporary workspace is a conversation whose sandbox starts from an empty
directory instead of a clone, and whose turns are never published. A null
``agent_threads.repo`` is what marks one; there is no second column that would
have to agree with it. Its environment binding carries only the owner's global
variables, so ``agent_environment_bindings.repo`` is null for it too.

Turns read the repository from their conversation (``0003_thread_level_config``),
so ``agent_jobs`` has no column to change.

Downgrade refuses while a temporary workspace exists. Restoring ``NOT NULL``
would mean deleting those conversations, and they are their owners' work, not a
schema detail: delete them first.
"""

from alembic import op

revision = "0071_temporary_workspaces"
down_revision = "0070_memory_cutover"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Allow a null repository on conversations and their environment bindings."""
    op.execute("ALTER TABLE agent_threads ALTER COLUMN repo DROP NOT NULL")
    op.execute("ALTER TABLE agent_environment_bindings ALTER COLUMN repo DROP NOT NULL")


def downgrade() -> None:
    """Require a repository again; refuse while a temporary workspace exists."""
    op.execute("LOCK TABLE agent_threads IN ACCESS EXCLUSIVE MODE")
    op.execute("LOCK TABLE agent_environment_bindings IN ACCESS EXCLUSIVE MODE")
    op.execute("""
        DO $$ BEGIN
            IF EXISTS (SELECT 1 FROM agent_threads WHERE repo IS NULL)
               OR EXISTS (SELECT 1 FROM agent_environment_bindings WHERE repo IS NULL) THEN
                RAISE EXCEPTION 'Delete temporary workspace conversations before downgrading; '
                    'the previous schema requires every conversation to name a repository';
            END IF;
        END $$
    """)
    op.execute("ALTER TABLE agent_environment_bindings ALTER COLUMN repo SET NOT NULL")
    op.execute("ALTER TABLE agent_threads ALTER COLUMN repo SET NOT NULL")
