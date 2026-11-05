"""Add Teams: members, single-use invitations, join requests and their audit.

The first step of the Team and shared-workspace design
(``docs/agents/specs/2026-10-15-team-shared-workspace-design.zh-CN.md``). It
records who belongs to which team and how they got there; no thread, sandbox
or environment refers to a team yet, so nothing here changes what anyone can
run or read.

**One owner, enforced by the database.** ``agent_teams.owner_user_id`` is the
only record of who owns a team. A deferred composite foreign key ties it to an
*active* membership of the same team while the team is not deleted: the
generated ``owner_membership_state`` is ``'active'`` for a live team and NULL
for a deleted one, and a NULL column exempts the row from the check. Creating a
team, transferring it, or letting a member leave all commit only if the owner
still names an active member, whichever order the writes ran in.

**Departures keep the row.** Leaving or being removed sets the membership's
state and bumps ``access_generation``; a later rejoin reuses the row and bumps
it again. Later steps key execution fences to that generation, so it must never
go backwards and never restart at 1 for a returning member.

**Invitations store only a hash.** The 32-byte link token is returned once when
the invitation is created; the table keeps its SHA-256. One account can claim
an invitation, which creates that account's join request; an owner or admin's decision
is recorded on the request.

Revision ID: 0079_teams
Revises: 0078_environment_snapshots
"""

from alembic import op

revision = "0079_teams"
down_revision = "0078_environment_snapshots"
branch_labels = None
depends_on = None

_AUDIT_ACTIONS = (
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


def upgrade() -> None:
    """Create the team tables, their constraints and their lookup indexes."""
    op.execute(
        """
        CREATE TABLE agent_teams (
            id TEXT PRIMARY KEY,
            name TEXT NOT NULL,
            owner_user_id TEXT NOT NULL,
            state TEXT NOT NULL DEFAULT 'active' CHECK (state IN ('active', 'deleted')),
            revision BIGINT NOT NULL DEFAULT 1 CHECK (revision > 0),
            created_by_user_id TEXT NOT NULL,
            create_request_key TEXT NOT NULL,
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            deleted_at TIMESTAMPTZ,
            owner_membership_state TEXT GENERATED ALWAYS AS (
                CASE WHEN state = 'deleted' THEN NULL ELSE 'active' END
            ) STORED,
            CONSTRAINT ck_agent_teams_name
                CHECK (char_length(name) BETWEEN 1 AND 80 AND name = btrim(name)),
            CONSTRAINT ck_agent_teams_request_key
                CHECK (char_length(create_request_key) BETWEEN 1 AND 128),
            CONSTRAINT ck_agent_teams_deleted CHECK ((state = 'deleted') = (deleted_at IS NOT NULL)),
            CONSTRAINT uq_agent_teams_create_request UNIQUE (created_by_user_id, create_request_key)
        )
        """
    )
    op.execute(
        """
        CREATE TABLE agent_team_members (
            team_id TEXT NOT NULL REFERENCES agent_teams (id),
            user_id TEXT NOT NULL,
            role TEXT NOT NULL DEFAULT 'member' CHECK (role IN ('admin', 'member')),
            state TEXT NOT NULL DEFAULT 'active' CHECK (state IN ('active', 'left', 'removed')),
            access_generation BIGINT NOT NULL DEFAULT 1 CHECK (access_generation > 0),
            joined_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            departed_at TIMESTAMPTZ,
            updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            PRIMARY KEY (team_id, user_id),
            CONSTRAINT uq_agent_team_members_state UNIQUE (team_id, user_id, state),
            CONSTRAINT ck_agent_team_members_departed
                CHECK ((state = 'active') = (departed_at IS NULL))
        )
        """
    )
    op.execute(
        "CREATE INDEX idx_agent_team_members_user ON agent_team_members (user_id) "
        "WHERE state = 'active'"
    )
    op.execute(
        """
        ALTER TABLE agent_teams ADD CONSTRAINT fk_agent_teams_owner_active_member
            FOREIGN KEY (id, owner_user_id, owner_membership_state)
            REFERENCES agent_team_members (team_id, user_id, state)
            DEFERRABLE INITIALLY DEFERRED
        """
    )
    op.execute(
        """
        CREATE TABLE agent_team_invitations (
            id TEXT PRIMARY KEY,
            team_id TEXT NOT NULL REFERENCES agent_teams (id),
            created_by_user_id TEXT NOT NULL,
            request_key TEXT NOT NULL,
            token_hash BYTEA NOT NULL UNIQUE CHECK (octet_length(token_hash) = 32),
            state TEXT NOT NULL DEFAULT 'open' CHECK (state IN ('open', 'claimed', 'revoked')),
            expires_at TIMESTAMPTZ NOT NULL,
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            revoked_at TIMESTAMPTZ,
            CONSTRAINT uq_agent_team_invitations_team UNIQUE (team_id, id),
            CONSTRAINT uq_agent_team_invitations_request
                UNIQUE (team_id, created_by_user_id, request_key),
            CONSTRAINT ck_agent_team_invitations_request_key
                CHECK (char_length(request_key) BETWEEN 1 AND 128),
            CONSTRAINT ck_agent_team_invitations_expiry CHECK (expires_at > created_at),
            CONSTRAINT ck_agent_team_invitations_revoked
                CHECK ((state = 'revoked') = (revoked_at IS NOT NULL))
        )
        """
    )
    op.execute(
        "CREATE INDEX idx_agent_team_invitations_team "
        "ON agent_team_invitations (team_id, created_at DESC)"
    )
    op.execute(
        """
        CREATE TABLE agent_team_join_requests (
            id TEXT PRIMARY KEY,
            team_id TEXT NOT NULL,
            invitation_id TEXT NOT NULL UNIQUE,
            user_id TEXT NOT NULL,
            state TEXT NOT NULL DEFAULT 'pending'
                CHECK (state IN ('pending', 'approved', 'declined', 'cancelled', 'expired')),
            decided_by_user_id TEXT,
            decided_at TIMESTAMPTZ,
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            CONSTRAINT fk_agent_team_join_requests_invitation
                FOREIGN KEY (team_id, invitation_id)
                REFERENCES agent_team_invitations (team_id, id),
            CONSTRAINT ck_agent_team_join_requests_decided
                CHECK ((state = 'pending') = (decided_at IS NULL)),
            CONSTRAINT ck_agent_team_join_requests_decider
                CHECK (state NOT IN ('approved', 'declined') OR decided_by_user_id IS NOT NULL)
        )
        """
    )
    op.execute(
        "CREATE UNIQUE INDEX uq_agent_team_join_requests_pending "
        "ON agent_team_join_requests (team_id, user_id) WHERE state = 'pending'"
    )
    op.execute(
        "CREATE INDEX idx_agent_team_join_requests_user "
        "ON agent_team_join_requests (user_id, created_at DESC)"
    )
    actions = ", ".join(f"'{action}'" for action in _AUDIT_ACTIONS)
    op.execute(
        f"""
        CREATE TABLE agent_team_audit_events (
            id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
            team_id TEXT NOT NULL REFERENCES agent_teams (id),
            actor_user_id TEXT NOT NULL,
            action TEXT NOT NULL CHECK (action IN ({actions})),
            target_user_id TEXT,
            target_id TEXT,
            details JSONB NOT NULL DEFAULT '{{}}'::jsonb,
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
        )
        """
    )
    op.execute(
        "CREATE INDEX idx_agent_team_audit_events_team ON agent_team_audit_events (team_id, id DESC)"
    )


def downgrade() -> None:
    """Remove every team table this revision added."""
    op.execute("DROP TABLE agent_team_audit_events")
    op.execute("DROP TABLE agent_team_join_requests")
    op.execute("DROP TABLE agent_team_invitations")
    op.execute("ALTER TABLE agent_teams DROP CONSTRAINT fk_agent_teams_owner_active_member")
    op.execute("DROP TABLE agent_team_members")
    op.execute("DROP TABLE agent_teams")
