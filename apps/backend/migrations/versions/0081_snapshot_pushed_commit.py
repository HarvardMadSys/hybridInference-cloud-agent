"""Keep the commit a snapshot pushed apart from the one it published.

Revision ID: 0081_snapshot_pushed_commit
Revises: 0080_user_ssh_servers

The publisher records the commit it is about to push before the push, so that
when this publication fails after the push, the group's next one, cut from the
same base, builds on that commit instead of finding the branch moved
(``own_pushes``). Until now the commit was kept in ``published_commit_sha``,
which every other reader takes as a published commit and so had to filter on
``state = 'published'``. It now has its own column.

The upgrade copies the commit of each snapshot that was never published into
it. It does not clear ``published_commit_sha``: ``deploy.sh`` migrates before
it replaces the API, and the release still running reads and writes its pushes
there until then. The claim of the next publication therefore reads both
columns for a snapshot that was never published. The downgrade copies the new
column back. Neither changes ``state``, so no publication or background notice
fires.
"""

from alembic import op

revision = "0081_snapshot_pushed_commit"
down_revision = "0080_user_ssh_servers"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Add the pushed commit, and copy each unpublished snapshot's commit into it."""
    op.execute("ALTER TABLE agent_sandbox_snapshots ADD COLUMN pushed_commit_sha TEXT")
    op.execute(
        """
        UPDATE agent_sandbox_snapshots
        SET pushed_commit_sha = published_commit_sha
        WHERE state <> 'published' AND published_commit_sha IS NOT NULL
        """
    )


def downgrade() -> None:
    """Copy the unpublished snapshots' commits back, then drop the column."""
    op.execute(
        """
        UPDATE agent_sandbox_snapshots
        SET published_commit_sha = pushed_commit_sha
        WHERE state <> 'published' AND pushed_commit_sha IS NOT NULL
        """
    )
    op.execute("ALTER TABLE agent_sandbox_snapshots DROP COLUMN pushed_commit_sha")
