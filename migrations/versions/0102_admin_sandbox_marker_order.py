"""Mark a membership's sandbox group only while holding the group's row.

Revision 0097's triggers mark a changed group in ``agent_admin_sandbox_changes``.
A write to ``agent_sandboxes`` already holds the group's row when its trigger
marks it. A membership write marked the group straight after the membership,
before the group. Settling a turn, abandoning an activation, ending, releasing
or reaping an attempt and handing a failed restore back write a membership and
then its group, so each held the marker while it waited for the group. A writer
holding the group, such as the admin terminal check or a workspace start,
waited for that marker in its own trigger, and Postgres aborted one of the two.

The membership trigger now locks the group's row before the marker, in the
mode a group write takes: membership, then group, then marker, the order
:mod:`cloud_agent.storage` documents. A writer that already holds the group
takes nothing new. Only the trigger function changes. No table is locked, and
the previous release runs the same statements under the new function.

Revision ID: 0102_admin_sandbox_marker_order
Revises: 0101_notification_state_deltas
"""

from __future__ import annotations

from alembic import op

revision = "0102_admin_sandbox_marker_order"
down_revision = "0101_notification_state_deltas"
branch_labels = None
depends_on = None

_GROUP_FIRST = """
CREATE OR REPLACE FUNCTION agent_track_admin_membership_change()
RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF TG_OP='UPDATE' AND
        (NEW.sandbox_id,NEW.resident_slot_id,NEW.active_attempt_id) IS NOT DISTINCT FROM
        (OLD.sandbox_id,OLD.resident_slot_id,OLD.active_attempt_id) THEN
        RETURN NULL;
    END IF;
    -- The group before its marker. FOR NO KEY UPDATE conflicts with every
    -- write to the group and with this lock in another membership write, so
    -- a transaction holding a group's marker also holds the group.
    PERFORM 1 FROM agent_sandboxes
    WHERE id IN (OLD.sandbox_id,NEW.sandbox_id)
    ORDER BY id
    FOR NO KEY UPDATE;
    IF TG_OP<>'INSERT' THEN
        PERFORM agent_mark_admin_sandbox_change('sandbox',OLD.sandbox_id);
    END IF;
    IF TG_OP<>'DELETE' THEN
        PERFORM agent_mark_admin_sandbox_change('sandbox',NEW.sandbox_id);
    END IF;
    RETURN NULL;
END $$
"""

_MARKER_FIRST = """
CREATE OR REPLACE FUNCTION agent_track_admin_membership_change()
RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF TG_OP='UPDATE' AND
        (NEW.sandbox_id,NEW.resident_slot_id,NEW.active_attempt_id) IS NOT DISTINCT FROM
        (OLD.sandbox_id,OLD.resident_slot_id,OLD.active_attempt_id) THEN
        RETURN NULL;
    END IF;
    IF TG_OP<>'INSERT' THEN
        PERFORM agent_mark_admin_sandbox_change('sandbox',OLD.sandbox_id);
    END IF;
    IF TG_OP<>'DELETE' THEN
        PERFORM agent_mark_admin_sandbox_change('sandbox',NEW.sandbox_id);
    END IF;
    RETURN NULL;
END $$
"""


def upgrade() -> None:
    """Take the group before its marker."""
    op.execute(_GROUP_FIRST)


def downgrade() -> None:
    """Restore revision 0097's trigger, which marks the group without locking it."""
    op.execute(_MARKER_FIRST)
