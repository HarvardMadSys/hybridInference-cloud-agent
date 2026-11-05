"""Sandbox incidents: a replayed row is a no-op, and what each chat was told.

A host reports incidents (``cloud_agent_shared.sandbox_incidents``) into the
existing append-only ``agent_sandbox_events`` table, as ``incident.<kind>``
rows whose ``details`` carry the host's deterministic ``incident_id``. A host
sends a row again until a heartbeat answer confirms it, so the unique index
turns the replay into nothing.

What a chat's agent has been told is a cursor, not a time: the highest event
id told to that thread (``agent_thread_incident_cursors``). A claim records
the id it would tell through against its attempt
(``agent_attempt_incident_marks``); the cursor advances to it only when that
attempt's ``started`` event is stored, so a claim that never starts tells the
same incidents again.

The predicate uses ``starts_with`` rather than ``LIKE``: some callers replay
migrations through drivers that treat ``%`` as a placeholder.

Revision ID: 0113_sandbox_incidents
Revises: 0112_thread_pull_requests
"""

from alembic import op

revision = "0113_sandbox_incidents"
down_revision = "0112_thread_pull_requests"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Index incident ids, and add the cursor and its per-attempt marks."""
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


def downgrade() -> None:
    """Drop the cursor, its marks and the index; the incident rows stay."""
    op.execute("DROP TABLE IF EXISTS agent_attempt_incident_marks")
    op.execute("DROP TABLE IF EXISTS agent_thread_incident_cursors")
    op.execute("DROP INDEX IF EXISTS uq_agent_sandbox_events_incident")
