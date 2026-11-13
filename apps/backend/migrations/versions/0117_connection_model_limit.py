"""Raise the per-connection model cap from eight to sixteen.

A connection stores the model ids its owner chose. Changing the key or Base
URL re-verifies every one of them in that save, so the cap stays finite:
sixteen still fits in one owner's hourly check budget and in four waves of
four provider checks.

``0106_user_model_connections`` created the check inline, and Postgres named
it ``agent_model_connections_models_check``. This revision replaces that
check under the same name. The table is locked first so no row lands between
the drop and the new check. A downgrade restores the eight-model check, and
fails while any row lists more than eight models.

Revision ID: 0117_connection_model_limit
Revises: 0116_account_provisioning
"""

from __future__ import annotations

from alembic import op

revision = "0117_connection_model_limit"
down_revision = "0116_account_provisioning"
branch_labels = None
depends_on = None

_CONSTRAINT = "agent_model_connections_models_check"


def _replace_check(limit: int) -> None:
    op.execute(f"ALTER TABLE agent_model_connections DROP CONSTRAINT {_CONSTRAINT}")
    op.execute(
        f"ALTER TABLE agent_model_connections ADD CONSTRAINT {_CONSTRAINT} "
        f"CHECK (cardinality(models) BETWEEN 1 AND {limit})"
    )


def upgrade() -> None:
    """Admit up to sixteen model ids on one connection."""
    op.execute("LOCK TABLE agent_model_connections IN SHARE ROW EXCLUSIVE MODE")
    _replace_check(16)


def downgrade() -> None:
    """Restore the eight-model cap; fails while a connection lists more."""
    op.execute("LOCK TABLE agent_model_connections IN SHARE ROW EXCLUSIVE MODE")
    _replace_check(8)
