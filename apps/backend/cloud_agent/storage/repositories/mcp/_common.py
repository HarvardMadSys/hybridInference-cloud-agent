"""State, columns and private helpers shared by the MCP repository's mixins."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from cloud_agent.logging_utils import get_logger
from cloud_agent.storage._sql.rows import load_json as _load_json

if TYPE_CHECKING:
    import asyncpg


logger = get_logger(__package__)


class McpRepositoryBase:
    """The pool, the connection projection and the helpers every mixin shares."""

    _pool: asyncpg.Pool

    _MCP_CONNECTION_COLUMNS = (
        "id, user_id, label, template, transport, location, url, launch_spec, "
        "auth_kind, auth_config, connector_id, status, enabled, error_kind, error_detail, "
        "access_token_ciphertext, refresh_token_ciphertext, client_secret_ciphertext, "
        "expires_at, scopes, issuer, resource, refresh_rotates, token_endpoint, "
        "revocation_endpoint, registration_endpoint, client_auth_method, "
        "registration, credential_version, config_revision, authorization_epoch, "
        "oauth_generation, capability_revision, account_label, last_checked_at, "
        "request_timeout_s, refresh_generation, refresh_lease_expires_at, "
        "created_at, updated_at, deleted_at"
    )

    #: Every way a held call expires, in one assignment. Its arguments were kept
    #: only so the owner could see what they were deciding; once nothing can be
    #: decided, the ciphertext is dropped rather than left in a shared table
    #: until the job is deleted. Settled calls keep theirs as the record of what
    #: was sent or refused.
    _EXPIRED = "status = 'expired', arguments_ciphertext = NULL"

    @staticmethod
    async def _lock_connection(conn: Any, connection_id: int) -> bool:
        return bool(
            await conn.fetchval(
                "SELECT id FROM agent_mcp_connections WHERE id = $1 FOR UPDATE",
                connection_id,
            )
        )

    @staticmethod
    def _mcp_connection(row: Any) -> dict[str, Any]:
        """Decode one connection row into plain Python values."""
        record = dict(row)
        for key in ("launch_spec", "auth_config"):
            record[key] = _load_json(record.get(key)) or {}
        record["scopes"] = list(record.get("scopes") or [])
        return record
