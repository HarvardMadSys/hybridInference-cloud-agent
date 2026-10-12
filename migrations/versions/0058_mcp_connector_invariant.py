"""Repair and enforce singular MCP connector authorization.

Revision ID: 0058_mcp_connector_invariant
Revises: 0057_admin_sandbox_trajectory
"""

from alembic import op

revision = "0058_mcp_connector_invariant"
down_revision = "0057_admin_sandbox_trajectory"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Make the connection route and reverse authorization agree exactly."""
    # Application writes lock a connector before its connections and then
    # their mapping. Taking table locks in the same order both gives this data
    # repair a stable snapshot and avoids introducing a migration-only
    # lock-order inversion.
    op.execute(
        """
        LOCK TABLE agent_mcp_connectors,
                   agent_mcp_connections,
                   agent_mcp_connector_connections
        IN SHARE ROW EXCLUSIVE MODE
        """
    )
    op.execute(
        """
        DELETE FROM agent_mcp_connector_connections AS mapping
        WHERE NOT EXISTS (
            SELECT 1
            FROM agent_mcp_connections AS connection
            JOIN agent_mcp_connectors AS connector
              ON connector.id = connection.connector_id
            WHERE connection.id = mapping.connection_id
              AND connection.connector_id = mapping.connector_id
              AND connection.deleted_at IS NULL
              AND connection.location = 'user-connector'
              AND connector.user_id = connection.user_id
              AND connector.status <> 'revoked'
        )
        """
    )
    op.execute(
        """
        UPDATE agent_mcp_connections AS connection
        SET connector_id = NULL,
            config_revision = connection.config_revision + 1,
            authorization_epoch = connection.authorization_epoch + 1,
            updated_at = clock_timestamp()
        WHERE connection.connector_id IS NOT NULL
          AND NOT EXISTS (
              SELECT 1
              FROM agent_mcp_connectors AS connector
              WHERE connector.id = connection.connector_id
                AND connection.deleted_at IS NULL
                AND connection.location = 'user-connector'
                AND connector.user_id = connection.user_id
                AND connector.status <> 'revoked'
          )
        """
    )
    op.execute(
        """
        INSERT INTO agent_mcp_connector_connections (connector_id, connection_id)
        SELECT connection.connector_id, connection.id
        FROM agent_mcp_connections AS connection
        JOIN agent_mcp_connectors AS connector
          ON connector.id = connection.connector_id
        WHERE connection.deleted_at IS NULL
          AND connection.location = 'user-connector'
          AND connector.user_id = connection.user_id
          AND connector.status <> 'revoked'
        ON CONFLICT (connector_id, connection_id) DO NOTHING
        """
    )
    op.execute(
        """
        ALTER TABLE agent_mcp_connector_connections
        ADD CONSTRAINT uq_agent_mcp_connector_connections_connection
        UNIQUE (connection_id)
        """
    )


def downgrade() -> None:
    """Remove the guard without resurrecting stale connector authority."""
    op.execute(
        """
        ALTER TABLE agent_mcp_connector_connections
        DROP CONSTRAINT IF EXISTS uq_agent_mcp_connector_connections_connection
        """
    )
