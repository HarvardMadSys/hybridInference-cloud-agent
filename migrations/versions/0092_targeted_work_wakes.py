"""Name the one host that can take the work a notification announces.

``agent_work_available`` carried no payload, so every queue change woke every
waiting host to claim. A retained chat's turns can only run on the host that
holds its group's writable layer, so for those turns every other host's claim
was wasted: at least two per turn, one when it is queued and one when it ends.

The notification now names that host when the turn's conversation belongs to a
group only it may take: one that is provisioning, starting, running or stopped
there. Otherwise it stays empty, which still wakes every host: a conversation
with no group yet, a cold group any restore-capable host may take, or one being
restored. The work a turn's end releases is its own conversation's next turn,
so the same host is named for it.

The payload only narrows who is woken first. A stopped group whose host has
been silent a long time may be handed over to another host, and that host, like
any host not woken, still claims when its wait times out.

Revision ID: 0092_targeted_work_wakes
Revises: 0091_env_queued_runs_index
"""

from __future__ import annotations

from alembic import op

revision = "0092_targeted_work_wakes"
down_revision = "0091_env_queued_runs_index"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Notify with the host a retained conversation's turns are pinned to."""
    op.execute(
        """
        CREATE OR REPLACE FUNCTION agent_notify_work_available() RETURNS trigger
        LANGUAGE plpgsql AS $$
        BEGIN
            PERFORM pg_notify(
                'agent_work_available',
                COALESCE(
                    (
                        SELECT g.host
                        FROM agent_thread_sandbox_memberships m
                        JOIN agent_sandboxes g ON g.id = m.sandbox_id
                        WHERE m.thread_id = NEW.thread_id
                          AND g.state IN ('provisioning', 'starting', 'running', 'stopped')
                    ),
                    ''
                )
            );
            RETURN NULL;
        END;
        $$
        """
    )


def downgrade() -> None:
    """Notify every host again, with no payload."""
    op.execute(
        """
        CREATE OR REPLACE FUNCTION agent_notify_work_available() RETURNS trigger
        LANGUAGE plpgsql AS $$
        BEGIN
            PERFORM pg_notify('agent_work_available', '');
            RETURN NULL;
        END;
        $$
        """
    )
