"""External inference provisioning for approved accounts.

When an administrator enables it (Admin → Settings → Accounts), an approved
account is invited to set a password on the deployment's connected inference
service, which issues that account a scoped API key there. This service only
tracks the invitation lifecycle; it never sees the password or the key.

- ``agent_account_provisioning``: one row per account, written by the
  provisioning worker (and by the operator backfill command). ``pending``
  until the invitation mail is sent, ``invited`` until the service reports
  the account provisioned, ``provisioned`` while the scoped key lives, and
  ``revoked`` once access here ended and the key was revoked there.
- ``agent_account_provisioning_activation``: a single-row marker recording
  when provisioning was first enabled, so only accounts approved after
  activation are invited automatically. Existing accounts are invited only
  through the explicit backfill command.

Both tables are new: the previous release does not read them while it serves
beside this one.

Revision ID: 0116_account_provisioning
Revises: 0115_mcp_retire_dead_locations
"""

from __future__ import annotations

from alembic import op

revision = "0116_account_provisioning"
down_revision = "0115_mcp_retire_dead_locations"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Create the provisioning ledger and the activation marker."""
    op.execute(
        """
        CREATE TABLE agent_account_provisioning (
            account_id TEXT PRIMARY KEY,
            state TEXT NOT NULL DEFAULT 'pending'
                CHECK (state IN ('pending', 'invited', 'provisioned', 'revoked')),
            email TEXT,
            attempts INTEGER NOT NULL DEFAULT 0,
            last_error TEXT,
            invited_at TIMESTAMPTZ,
            provisioned_at TIMESTAMPTZ,
            revoked_at TIMESTAMPTZ,
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
        )
        """
    )
    op.execute(
        """
        CREATE TABLE agent_account_provisioning_activation (
            id INTEGER PRIMARY KEY CHECK (id = 1),
            activated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
        )
        """
    )


def downgrade() -> None:
    """Drop both tables; nothing else reads them."""
    op.execute("DROP TABLE agent_account_provisioning_activation")
    op.execute("DROP TABLE agent_account_provisioning")
