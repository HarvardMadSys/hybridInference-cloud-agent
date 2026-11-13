"""Record the fork helper version in each retained group's own image.

Revision ID: 0026_fork_helper_version
Revises: 0025_github_user_tokens

Fork and mid-turn input were offered only while the host's current sandbox
image was byte-for-byte the image a chat's container was created from. That
stood in for what Fork actually needs from the container — bundled helpers
that understand a historical anchor, which is claude-session-fork version 2 — but
every release builds a new image: the image is labelled with its source
commit, so its digest changes even when nothing in it did. The first
deployment after a chat was created switched off its Fork and its mid-turn
input for good, while its ordinary follow-ups kept working.

``fork_helper_version`` is the claude-session-fork version the image declares, as the
runner creating the container attests it. It is immutable like the digest and
runtime version beside it, and the Fork gate reads it instead of asking
whether the host still runs the very same image. Null means nobody recorded
it; such a group stays forkable only from the exact image it was created from.

Existing rows are backfilled with ``2``. Since before this schema's baseline,
creating a group required the at-anchor helper: initially it was attested by
shared-sandbox protocol v2 and ``native-fork-at-anchor-v1``; after #351 it is
attested directly by the helper's version-2 label. The helper's behavior did
not change when that label was introduced. The exception is a row copied in
from an old-chain database by ``scripts/convert_legacy_database_copy.py``,
which may predate historical Fork: a row whose id is in
``legacy_archive.agent_sandboxes`` keeps null. The decision is made in SQL
because that conversion renders this revision offline. The value is literal
historical evidence, never a reference to a future runtime's helper pin.
"""

from __future__ import annotations

from alembic import op

revision = "0026_fork_helper_version"
down_revision = "0025_github_user_tokens"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Record the fork helper for groups created under this schema's claim gate."""
    op.execute("""
        ALTER TABLE agent_sandboxes
            ADD COLUMN fork_helper_version TEXT
                CHECK (fork_helper_version IS NULL
                       OR length(fork_helper_version) BETWEEN 1 AND 32)
    """)
    op.execute("""
        DO $$ BEGIN
            IF to_regclass('legacy_archive.agent_sandboxes') IS NULL THEN
                UPDATE agent_sandboxes SET fork_helper_version = '2';
            ELSE
                UPDATE agent_sandboxes g SET fork_helper_version = '2'
                WHERE NOT EXISTS (
                    SELECT 1 FROM legacy_archive.agent_sandboxes l WHERE l.id = g.id
                );
            END IF;
        END $$
    """)


def downgrade() -> None:
    """Drop it; Fork again needs the host to run the group's exact image."""
    op.execute("ALTER TABLE agent_sandboxes DROP COLUMN IF EXISTS fork_helper_version")
