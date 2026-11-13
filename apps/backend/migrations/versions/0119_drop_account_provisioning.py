"""Withdraw inference provisioning: its two tables and its stored settings.

``0116_account_provisioning`` added a ledger of invitations to a connected
inference service, where each approved account was given a key of its own to
paste into a model connection. A deployment now provides models through the
model relay instead (``0118_operator_model_connections``): its one key stays
in the control plane and no account needs one elsewhere. The worker, the
backfill command and the settings are gone from this release, so nothing
reads these tables.

The four ``accounts.inference_provisioning_*`` settings are removed from the
stored document as well. One is a secret, the token the inference service
required of this one, and a secret nothing reads should not stay stored.

A previous release beside this one stops provisioning: it reads the setting
as off, and a pass that is off touches neither table. Keys already issued at
the inference service are not revoked by this; that service still holds them.

Revision ID: 0119_drop_account_provisioning
Revises: 0118_operator_model_connections
"""

from __future__ import annotations

from alembic import op

revision = "0119_drop_account_provisioning"
down_revision = "0118_operator_model_connections"
branch_labels = None
depends_on = None

_SETTINGS = (
    "accounts.inference_provisioning_enabled",
    "accounts.inference_provisioning_api_url",
    "accounts.inference_provisioning_web_url",
    "accounts.inference_provisioning_dispatch_token",
)


def upgrade() -> None:
    """Drop the ledger and the activation marker, and forget the settings."""
    op.execute("DROP TABLE agent_account_provisioning_activation")
    op.execute("DROP TABLE agent_account_provisioning")
    keys = ", ".join(f"'{key}'" for key in _SETTINGS)
    # A new revision, so that a running process adopts the document without them.
    op.execute(
        f"""
        UPDATE agent_settings
        SET "values" = "values" - ARRAY[{keys}]::text[],
            revision = revision + 1,
            updated_at = NOW()
        WHERE "values" <> "values" - ARRAY[{keys}]::text[]
        """
    )


def downgrade() -> None:
    """Put the two tables back, empty; the settings are not restored."""
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
