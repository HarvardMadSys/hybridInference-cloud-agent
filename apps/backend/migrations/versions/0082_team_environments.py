"""Team environments: a team's shared configuration for one repository.

Revision ID: 0082_team_environments
Revises: 0081_snapshot_pushed_commit
Create Date: 2026-10-15

An environment now belongs either to one account (``team_id`` NULL, as
before) or to a team. A team's environment has a repository, is edited by the
team's owner and admins, and is used by every active member for their own
tasks in their own containers; ``user_id`` then records who created it.

- Names are unique per account and repository for personal environments, and
  per team and repository for team ones.
- A member's repository default may name a team environment, so the default's
  foreign key no longer requires the same ``user_id``; the store checks access
  when the default is set and again when a task binds it.
- A binding of a team environment records the team and the member's access
  generation at binding time. A member who leaves, or leaves and rejoins, no
  longer runs turns on it.
- The team audit log gains environment actions.

Downgrade refuses while a team environment exists: removing the column would
silently hand each one to the member who created it.
"""

from __future__ import annotations

from alembic import op

revision = "0082_team_environments"
down_revision = "0081_snapshot_pushed_commit"
branch_labels = None
depends_on = None

_TEAM_ACTIONS = (
    "team_created",
    "team_renamed",
    "team_deleted",
    "invitation_created",
    "invitation_revoked",
    "join_requested",
    "join_approved",
    "join_declined",
    "member_left",
    "member_removed",
    "member_role_changed",
    "ownership_transferred",
)
_ENVIRONMENT_ACTIONS = (
    "environment_created",
    "environment_updated",
    "environment_archived",
)


def _action_check(actions: tuple[str, ...]) -> str:
    listed = ", ".join(f"'{action}'" for action in actions)
    return (
        "ALTER TABLE agent_team_audit_events ADD CONSTRAINT "
        f"agent_team_audit_events_action_check CHECK (action IN ({listed}))"
    )


def upgrade() -> None:
    """Let an environment belong to a team, and bindings name the team they use."""
    op.execute(
        """
        ALTER TABLE agent_environments
            ADD COLUMN team_id TEXT REFERENCES agent_teams (id),
            ADD CONSTRAINT ck_agent_environments_team_repository
                CHECK (team_id IS NULL OR repo IS NOT NULL)
        """
    )
    op.execute("DROP INDEX agent_environment_names")
    op.execute(
        "CREATE UNIQUE INDEX agent_environment_names ON agent_environments "
        "(user_id, repo, normalized_name) "
        "WHERE repo IS NOT NULL AND archived_at IS NULL AND team_id IS NULL"
    )
    op.execute(
        "CREATE UNIQUE INDEX agent_environment_team_names ON agent_environments "
        "(team_id, repo, normalized_name) WHERE team_id IS NOT NULL AND archived_at IS NULL"
    )
    op.execute("CREATE INDEX agent_environment_teams ON agent_environments (team_id, repo)")
    op.execute(
        """
        ALTER TABLE agent_repository_environment_defaults
            DROP CONSTRAINT agent_repository_environment_defaul_environment_id_user_id_fkey,
            ADD CONSTRAINT agent_repository_environment_defaults_environment_id_fkey
                FOREIGN KEY (environment_id) REFERENCES agent_environments (id)
        """
    )
    op.execute(
        """
        ALTER TABLE agent_environment_bindings
            ADD COLUMN team_id TEXT REFERENCES agent_teams (id),
            ADD COLUMN team_access_generation BIGINT,
            ADD CONSTRAINT ck_agent_environment_bindings_team_generation
                CHECK ((team_id IS NULL) = (team_access_generation IS NULL))
        """
    )
    op.execute(
        "ALTER TABLE agent_team_audit_events DROP CONSTRAINT agent_team_audit_events_action_check"
    )
    op.execute(_action_check(_TEAM_ACTIONS + _ENVIRONMENT_ACTIONS))


def downgrade() -> None:
    """Remove team ownership, refusing while any team environment exists."""
    op.execute(
        """
        DO $$
        BEGIN
            IF EXISTS (SELECT 1 FROM agent_environments WHERE team_id IS NOT NULL) THEN
                RAISE EXCEPTION 'team environments exist; archive and remove them first';
            END IF;
        END
        $$
        """
    )
    op.execute(
        "DELETE FROM agent_team_audit_events WHERE action IN "
        "('environment_created', 'environment_updated', 'environment_archived')"
    )
    op.execute(
        "ALTER TABLE agent_team_audit_events DROP CONSTRAINT agent_team_audit_events_action_check"
    )
    op.execute(_action_check(_TEAM_ACTIONS))
    op.execute(
        """
        ALTER TABLE agent_environment_bindings
            DROP CONSTRAINT ck_agent_environment_bindings_team_generation,
            DROP COLUMN team_access_generation,
            DROP COLUMN team_id
        """
    )
    op.execute(
        """
        ALTER TABLE agent_repository_environment_defaults
            DROP CONSTRAINT agent_repository_environment_defaults_environment_id_fkey,
            ADD CONSTRAINT agent_repository_environment_defaul_environment_id_user_id_fkey
                FOREIGN KEY (environment_id, user_id) REFERENCES agent_environments (id, user_id)
        """
    )
    op.execute("DROP INDEX agent_environment_teams")
    op.execute("DROP INDEX agent_environment_team_names")
    op.execute("DROP INDEX agent_environment_names")
    op.execute(
        "CREATE UNIQUE INDEX agent_environment_names ON agent_environments "
        "(user_id, repo, normalized_name) WHERE repo IS NOT NULL AND archived_at IS NULL"
    )
    op.execute(
        """
        ALTER TABLE agent_environments
            DROP CONSTRAINT ck_agent_environments_team_repository,
            DROP COLUMN team_id
        """
    )
