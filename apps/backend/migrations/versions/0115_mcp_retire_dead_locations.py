"""An MCP connection runs in one of two places: ``remote`` or ``sandbox``.

``managed-worker`` and ``user-connector`` never had a runtime behind them: no
managed worker shipped, the routes that registered a user's machine were
retired, and the relay refused both at use. This revision moves every row
still stored at either one to ``sandbox``, with its launch exactly as stored,
and narrows the location check to the two values that run.

A moved row is a program. The sandbox runs it when its launch passes the
sandbox's rules (``npx`` or ``uvx``, one exactly pinned package, no mounts or
working directory) and leaves it out otherwise, as it does any other program;
its owner sees it as one and can edit or replace it. Its configuration
revision and authorization epoch move, as a location edit moves them, so
nothing approved for where it was carries over to where it is now. A failing
diagnosis written for the old location (it needed a worker or connector) is
cleared rather than shown on a program it no longer describes. No row is
deleted, and no launch, credential or connector link is changed.

The connector tables and ``agent_mcp_connections.connector_id`` stay, unread
by this release: the previous release still selects and maintains them while
it serves beside this one. A later revision drops them.

While the previous release serves, it could still try to store a connection
at a removed location. The narrowed check refuses that write, which could only
have stored a connection that never runs. The table is locked first so that no
such row lands between the move and the check.

A downgrade restores the four-location check, under the same name, so
``0111_mcp_sandbox_location``'s downgrade still finds it. Moved rows stay
``sandbox``: the previous release reads them as programs.

Revision ID: 0115_mcp_retire_dead_locations
Revises: 0114_admin_audit_attribution
"""

from __future__ import annotations

from alembic import op

revision = "0115_mcp_retire_dead_locations"
down_revision = "0114_admin_audit_attribution"
branch_labels = None
depends_on = None

_CONSTRAINT = "agent_mcp_connections_location_check"


def _replace_check(locations: str) -> None:
    op.execute(f"ALTER TABLE agent_mcp_connections DROP CONSTRAINT {_CONSTRAINT}")
    op.execute(
        f"ALTER TABLE agent_mcp_connections ADD CONSTRAINT {_CONSTRAINT} "
        f"CHECK (location IN {locations})"
    )


def upgrade() -> None:
    """Move every dead-location row to ``sandbox``, then admit only the two."""
    op.execute("LOCK TABLE agent_mcp_connections IN SHARE ROW EXCLUSIVE MODE")
    # SET reads the row as it was, so each CASE sees the stored status.
    op.execute(
        """
        UPDATE agent_mcp_connections
        SET location = 'sandbox',
            status = CASE WHEN status = 'error' THEN 'draft' ELSE status END,
            error_kind = 'none',
            error_detail = NULL,
            config_revision = config_revision + 1,
            authorization_epoch = authorization_epoch + 1,
            updated_at = clock_timestamp()
        WHERE location IN ('managed-worker', 'user-connector')
        """
    )
    _replace_check("('remote', 'sandbox')")


def downgrade() -> None:
    """Admit the two removed locations again; moved rows stay ``sandbox``."""
    _replace_check("('remote', 'managed-worker', 'user-connector', 'sandbox')")
