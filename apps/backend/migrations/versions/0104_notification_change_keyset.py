"""Align notification deltas with their kind/XID keyset.

Revision ID: 0104_notification_change_keyset
Revises: 0103_terminal_resume_keyset
"""

from __future__ import annotations

from alembic import context, op

revision = "0104_notification_change_keyset"
down_revision = "0103_terminal_resume_keyset"
branch_labels = None
depends_on = None

_PREVIOUS_INDEX = "idx_agent_notification_changes_owner_xid"
_KEYSET_INDEX = "idx_agent_notification_changes_owner_kind_xid"
_PREVIOUS_DEFINITION = (
    f"{_PREVIOUS_INDEX} ON agent_notification_changes (user_id,change_xid,kind,entity_id)"
)
_KEYSET_DEFINITION = (
    f"{_KEYSET_INDEX} ON agent_notification_changes (user_id,kind,change_xid,entity_id)"
)


def _replace(*, create: str, remove: str, definition: str) -> None:
    """Build the replacement before retiring the usable predecessor."""
    if context.is_offline_mode():
        op.execute(f"DROP INDEX IF EXISTS {create}")
        op.execute(f"CREATE INDEX {definition}")
        op.execute(f"DROP INDEX IF EXISTS {remove}")
        return
    with op.get_context().autocommit_block():
        op.execute(f"DROP INDEX CONCURRENTLY IF EXISTS {create}")
        op.execute(f"CREATE INDEX CONCURRENTLY {definition}")
        op.execute(f"DROP INDEX CONCURRENTLY IF EXISTS {remove}")


def upgrade() -> None:
    """Support kind-first, transaction-keyed notification pages."""
    _replace(
        create=_KEYSET_INDEX,
        remove=_PREVIOUS_INDEX,
        definition=_KEYSET_DEFINITION,
    )


def downgrade() -> None:
    """Restore the original owner/XID notification index."""
    _replace(
        create=_PREVIOUS_INDEX,
        remove=_KEYSET_INDEX,
        definition=_PREVIOUS_DEFINITION,
    )
