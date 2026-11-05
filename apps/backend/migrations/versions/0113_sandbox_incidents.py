"""Sandbox incidents: a replayed row is a no-op, and what each chat was told.

A host reports incidents (``cloud_agent_shared.sandbox_incidents``) into the
existing append-only ``agent_sandbox_events`` table, as ``incident.<kind>``
rows whose ``details`` carry the host's deterministic ``incident_id``. A host
sends a row again until a heartbeat answer confirms it, so the unique index
turns the replay into nothing.

What a chat's agent has been told is a cursor, not a time: the highest event
id told to that thread (``agent_thread_incident_cursors``). It starts when the
chat joins a sandbox, at that sandbox's newest incident or at zero, so a chat
hears of everything after it joined and nothing before: a Fork is not told its
parent's past. A trigger on the membership starts it, at insert and when the
membership moves to another sandbox, and never moves it back; existing
memberships start at their sandbox's newest incident.

A claim records the id it would tell through against its attempt
(``agent_attempt_incident_marks``); the cursor advances to it only when that
attempt's ``turn_started`` event is stored, which the runner writes once the
prompt was dispatched. A claim whose prompt never went out tells the same
incidents again.

The SQL uses ``starts_with`` rather than ``LIKE``: some callers replay
migrations through drivers that treat ``%`` as a placeholder.

Revision ID: 0113_sandbox_incidents
Revises: 0112_thread_pull_requests
"""

from alembic import op

revision = "0113_sandbox_incidents"
down_revision = "0112_thread_pull_requests"
branch_labels = None
depends_on = None

#: The newest incident of the sandbox a membership row names, or zero.
_NEWEST = """COALESCE((
    SELECT max(e.id) FROM agent_sandbox_events e
    WHERE e.sandbox_id = {sandbox} AND starts_with(e.event_type, 'incident.')
), 0)"""


def upgrade() -> None:
    """Index incident ids; add the cursor, its start and its per-attempt marks."""
    op.execute(
        """
        CREATE UNIQUE INDEX uq_agent_sandbox_events_incident
        ON agent_sandbox_events (sandbox_id, (details->>'incident_id'))
        WHERE starts_with(event_type, 'incident.')
        """
    )
    op.execute(
        """
        CREATE TABLE agent_thread_incident_cursors (
            thread_id TEXT PRIMARY KEY REFERENCES agent_threads (id) ON DELETE CASCADE,
            told_through BIGINT NOT NULL CHECK (told_through >= 0),
            updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
        )
        """
    )
    op.execute(
        """
        CREATE TABLE agent_attempt_incident_marks (
            attempt_id BIGINT PRIMARY KEY REFERENCES agent_attempts (id) ON DELETE CASCADE,
            thread_id TEXT NOT NULL REFERENCES agent_threads (id) ON DELETE CASCADE,
            through_id BIGINT NOT NULL CHECK (through_id >= 0),
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
        )
        """
    )
    op.execute(
        f"""
        CREATE FUNCTION agent_start_incident_cursor()
        RETURNS TRIGGER
        LANGUAGE plpgsql
        AS $$
        BEGIN
            INSERT INTO agent_thread_incident_cursors (thread_id, told_through)
            VALUES (NEW.thread_id, {_NEWEST.format(sandbox="NEW.sandbox_id")})
            ON CONFLICT (thread_id) DO UPDATE
            SET told_through = GREATEST(
                    agent_thread_incident_cursors.told_through, EXCLUDED.told_through
                ),
                updated_at = NOW();
            RETURN NEW;
        END;
        $$
        """
    )
    op.execute(
        """
        CREATE TRIGGER trg_agent_memberships_incident_cursor_insert
        AFTER INSERT ON agent_thread_sandbox_memberships
        FOR EACH ROW EXECUTE FUNCTION agent_start_incident_cursor()
        """
    )
    op.execute(
        """
        CREATE TRIGGER trg_agent_memberships_incident_cursor_move
        AFTER UPDATE OF sandbox_id ON agent_thread_sandbox_memberships
        FOR EACH ROW
        WHEN (OLD.sandbox_id IS DISTINCT FROM NEW.sandbox_id)
        EXECUTE FUNCTION agent_start_incident_cursor()
        """
    )
    op.execute(
        f"""
        INSERT INTO agent_thread_incident_cursors (thread_id, told_through)
        SELECT m.thread_id, {_NEWEST.format(sandbox="m.sandbox_id")}
        FROM agent_thread_sandbox_memberships m
        ON CONFLICT (thread_id) DO NOTHING
        """
    )


def downgrade() -> None:
    """Drop the cursor, its start, its marks and the index; the incident rows stay."""
    op.execute(
        "DROP TRIGGER IF EXISTS trg_agent_memberships_incident_cursor_move "
        "ON agent_thread_sandbox_memberships"
    )
    op.execute(
        "DROP TRIGGER IF EXISTS trg_agent_memberships_incident_cursor_insert "
        "ON agent_thread_sandbox_memberships"
    )
    op.execute("DROP FUNCTION IF EXISTS agent_start_incident_cursor()")
    op.execute("DROP TABLE IF EXISTS agent_attempt_incident_marks")
    op.execute("DROP TABLE IF EXISTS agent_thread_incident_cursors")
    op.execute("DROP INDEX IF EXISTS uq_agent_sandbox_events_incident")
