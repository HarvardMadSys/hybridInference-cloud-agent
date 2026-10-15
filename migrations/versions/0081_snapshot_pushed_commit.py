"""Keep the commit a snapshot pushed apart from the one it published.

Revision ID: 0081_snapshot_pushed_commit
Revises: 0080_user_ssh_servers

The publisher records the commit it is about to push before the push, so that
when this publication fails after the push, the group's next one, cut from the
same base, builds on that commit instead of finding the branch moved
(``own_pushes``). Until now the commit was kept in ``published_commit_sha``,
which every other reader takes as a published commit and so had to filter on
``state = 'published'``. It now has its own column.

The upgrade moves the commits of snapshots that were never published into it;
a published snapshot keeps its published commit. The downgrade moves them back.
Neither changes ``state``, so no publication or background notice fires.
"""

from alembic import op

revision = "0081_snapshot_pushed_commit"
down_revision = "0080_user_ssh_servers"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Add the pushed commit, and move each unpublished snapshot's commit into it."""
    op.execute("ALTER TABLE agent_sandbox_snapshots ADD COLUMN pushed_commit_sha TEXT")
    op.execute(
        """
        UPDATE agent_sandbox_snapshots
        SET pushed_commit_sha = published_commit_sha, published_commit_sha = NULL
        WHERE state <> 'published' AND published_commit_sha IS NOT NULL
        """
    )


def downgrade() -> None:
    """Move the unpublished snapshots' commits back, then drop the column."""
    op.execute(
        """
        UPDATE agent_sandbox_snapshots
        SET published_commit_sha = pushed_commit_sha
        WHERE state <> 'published' AND pushed_commit_sha IS NOT NULL
        """
    )
    op.execute("ALTER TABLE agent_sandbox_snapshots DROP COLUMN pushed_commit_sha")
