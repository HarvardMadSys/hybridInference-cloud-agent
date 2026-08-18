"""Name the implemented continuity rung honestly: ``warm_native``.

The measured reality (design §13): a resident turn resumes the native session
in the preserved workspace under a fresh process — §2's WARM semantics.
``hot`` stays reserved for a live process receiving injected turns, which
waits on residency-scoped credentials; recording WARM turns as ``hot`` would
make the hit-rate, the audit and the capability matrix lie together (review).

Revision ID: 0011_warm_native_mode
Revises: 0010_continuity_mode
"""

from __future__ import annotations

from alembic import op

revision = "0011_warm_native_mode"
down_revision = "0010_continuity_mode"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Admit warm_native to the continuity vocabulary."""
    op.execute(
        "ALTER TABLE agent_attempts DROP CONSTRAINT IF EXISTS "
        "agent_attempts_continuity_mode_check"
    )
    op.execute(
        "ALTER TABLE agent_attempts ADD CONSTRAINT agent_attempts_continuity_mode_check "
        "CHECK (continuity_mode IN ('hot', 'warm_native', 'cold_native', 'replay'))"
    )


def downgrade() -> None:
    """Restore the three-value vocabulary — mapping the data first.

    ``warm_native`` rows fold into ``cold_native``: it is the nearest truthful
    old-vocabulary claim (native session continuity; the workspace bonus is
    unsayable there), and mapping before re-adding the constraint is what
    keeps a downgrade with real data from failing on its own CHECK (review).
    """
    op.execute(
        "ALTER TABLE agent_attempts DROP CONSTRAINT IF EXISTS "
        "agent_attempts_continuity_mode_check"
    )
    op.execute(
        "UPDATE agent_attempts SET continuity_mode = 'cold_native' "
        "WHERE continuity_mode = 'warm_native'"
    )
    op.execute(
        "ALTER TABLE agent_attempts ADD CONSTRAINT agent_attempts_continuity_mode_check "
        "CHECK (continuity_mode IN ('hot', 'cold_native', 'replay'))"
    )
