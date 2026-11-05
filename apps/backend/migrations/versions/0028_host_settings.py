"""Let an admin set the hosts' runtime settings from the Servers page.

Seven settings — the turn time limit, the chat idle timeout, the repository
import limit, chats per sandbox, and the three terminal limits — used to be
read only from each host's ``.env``, so changing one meant editing every
machine and redeploying. ``cloud_agent_shared.host_settings`` names them and
their bounds; this adds the places their values live.

* ``agent_runner_policy.host_settings`` holds the values set for every server.
* ``agent_runner_hosts.settings_desired`` holds the values set for one server,
  which win over the global ones. Like ``slots_desired`` it is a decision about
  the named machine, so heartbeats and re-enrollment leave it alone.
* ``agent_runner_hosts.settings_applied`` and ``settings_local`` are the host's
  side: what it is applying now, and what its own environment would give it
  with no admin value. Null is a host that has never reported, which is what
  every existing row gets and what a runner predating this sends.

A setting with no admin value anywhere falls back to the host's environment,
then to the built-in default, exactly as before. The value checks — known keys,
whole numbers inside each setting's bounds — are the application's, because a
bound is a release decision and these columns outlive any one release; the
columns refuse only a document that is not an object.

Revision ID: 0028_host_settings
Revises: 0027_compute_resources
"""

from __future__ import annotations

from alembic import op

revision = "0028_host_settings"
down_revision = "0027_compute_resources"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Add the global and per-server admin values, and the host's report."""
    op.execute(
        """
        ALTER TABLE agent_runner_policy
            ADD COLUMN host_settings JSONB NOT NULL DEFAULT '{}'::jsonb
                CHECK (jsonb_typeof(host_settings) = 'object')
        """
    )
    op.execute(
        """
        ALTER TABLE agent_runner_hosts
            ADD COLUMN settings_desired JSONB NOT NULL DEFAULT '{}'::jsonb
                CHECK (jsonb_typeof(settings_desired) = 'object'),
            ADD COLUMN settings_applied JSONB
                CHECK (settings_applied IS NULL OR jsonb_typeof(settings_applied) = 'object'),
            ADD COLUMN settings_local JSONB
                CHECK (settings_local IS NULL OR jsonb_typeof(settings_local) = 'object')
        """
    )


def downgrade() -> None:
    """Drop them; every host reads its settings from its environment again."""
    op.execute(
        """
        ALTER TABLE agent_runner_hosts
            DROP COLUMN IF EXISTS settings_desired,
            DROP COLUMN IF EXISTS settings_applied,
            DROP COLUMN IF EXISTS settings_local
        """
    )
    op.execute("ALTER TABLE agent_runner_policy DROP COLUMN IF EXISTS host_settings")
