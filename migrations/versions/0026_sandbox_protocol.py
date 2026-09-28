"""Record which shared-sandbox protocol each retained group's image speaks.

Revision ID: 0026_sandbox_protocol
Revises: 0025_github_user_tokens

Fork and mid-turn input were offered only while the host's current sandbox
image was byte-for-byte the image a chat's container was created from. That
stood in for what Fork actually needs from the container — bundled helpers
that understand a historical anchor, which is shared-sandbox protocol v2 — but
every release builds a new image: the image is labelled with its source
commit, so its digest changes even when nothing in it did. The first
deployment after a chat was created switched off its Fork and its mid-turn
input for good, while its ordinary follow-ups kept working.

``shared_sandbox_protocol`` is the protocol the group's image declares, as the
runner creating the container attests it. It is immutable like the digest and
runtime version beside it, and the Fork gate reads it instead of asking
whether the host still runs the very same image. Null means nobody recorded
it; such a group stays forkable only from the exact image it was created from.

Existing rows are backfilled with ``v2``. A group can only be created through
a claim whose replica attested exactly protocol v2 and
``native-fork-at-anchor-v1`` (``shared_sandbox_claim_attested``, a rule older
than this schema's baseline), so every group this schema's code created speaks
v2. The exception is a row copied in from an old-chain database by
``scripts/convert_legacy_database_copy.py``, which may predate v2: a row whose
id is in ``legacy_archive.agent_sandboxes`` keeps null. The decision is made in
SQL because that conversion renders this revision offline.
"""

from __future__ import annotations

from alembic import op

revision = "0026_sandbox_protocol"
down_revision = "0025_github_user_tokens"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Add the group's image protocol, and record it for groups this schema created."""
    op.execute("""
        ALTER TABLE agent_sandboxes
            ADD COLUMN shared_sandbox_protocol TEXT
                CHECK (shared_sandbox_protocol IS NULL
                       OR length(shared_sandbox_protocol) BETWEEN 1 AND 32)
    """)
    op.execute("""
        DO $$ BEGIN
            IF to_regclass('legacy_archive.agent_sandboxes') IS NULL THEN
                UPDATE agent_sandboxes SET shared_sandbox_protocol = 'v2';
            ELSE
                UPDATE agent_sandboxes g SET shared_sandbox_protocol = 'v2'
                WHERE NOT EXISTS (
                    SELECT 1 FROM legacy_archive.agent_sandboxes l WHERE l.id = g.id
                );
            END IF;
        END $$
    """)


def downgrade() -> None:
    """Drop it; Fork again needs the host to run the group's exact image."""
    op.execute("ALTER TABLE agent_sandboxes DROP COLUMN IF EXISTS shared_sandbox_protocol")
