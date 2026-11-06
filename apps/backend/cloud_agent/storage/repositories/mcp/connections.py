"""A user's MCP connections: create, read, update and disconnect."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

from cloud_agent.storage.repositories.mcp._common import McpRepositoryBase

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

    import asyncpg


_CREATE_REQUIRED = ("user_id", "label", "transport", "location", "auth_kind", "auth_config")


_CREATE_OPTIONAL = (
    "url",
    "launch_spec",
    "template",
    "account_label",
    "request_timeout_s",
    "reuse_pending",
    "access_token_ciphertext",
    "auth_config_patch",
    "sealed_launch_spec",
)


def _create_kwargs(item: Mapping[str, Any]) -> dict[str, Any]:
    """The insert arguments present on one batch draft."""
    missing = [key for key in _CREATE_REQUIRED if key not in item]
    if missing:
        joined = ", ".join(missing)
        raise TypeError(f"MCP connection draft is missing {joined}")
    kwargs = {key: item[key] for key in _CREATE_REQUIRED}
    kwargs.update({key: item[key] for key in _CREATE_OPTIONAL if key in item})
    return kwargs


class ConnectionsMixin(McpRepositoryBase):
    """Create, read, change and disconnect connections."""

    async def list_mcp_connections(
        self, *, user_id: str, include_deleted: bool = False
    ) -> list[dict[str, Any]]:
        """Every connection this user owns, newest label order stable."""
        clause = "" if include_deleted else " AND deleted_at IS NULL"
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(
                f"""
                SELECT {self._MCP_CONNECTION_COLUMNS}
                FROM agent_mcp_connections
                WHERE user_id = $1{clause}
                ORDER BY label, id
                """,
                user_id,
            )
        return [self._mcp_connection(row) for row in rows]

    async def get_mcp_connection(
        self, *, connection_id: int, user_id: str | None = None
    ) -> dict[str, Any] | None:
        """One connection by immutable id, optionally checked against its owner."""
        async with self._pool.acquire() as conn:
            if user_id is None:
                row = await conn.fetchrow(
                    f"SELECT {self._MCP_CONNECTION_COLUMNS} FROM agent_mcp_connections WHERE id = $1",
                    connection_id,
                )
            else:
                row = await conn.fetchrow(
                    f"SELECT {self._MCP_CONNECTION_COLUMNS} FROM agent_mcp_connections "
                    "WHERE id = $1 AND user_id = $2",
                    connection_id,
                    user_id,
                )
        return self._mcp_connection(row) if row is not None else None

    async def create_mcp_connection(
        self,
        *,
        user_id: str,
        label: str,
        transport: str,
        location: str,
        url: str | None,
        launch_spec: dict[str, Any] | None,
        auth_kind: str,
        auth_config: dict[str, Any],
        template: str | None = None,
        account_label: str | None = None,
        request_timeout_s: float | None = None,
        reuse_pending: bool = False,
        access_token_ciphertext: str | None = None,
        auth_config_patch: Mapping[str, Any] | None = None,
        sealed_launch_spec: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Insert a connection and return it, at revision one.

        New rows start ``draft`` rather than ``active``: nothing has been tested
        and no capability has been discovered, and a connection that reads as
        active before either has happened is a lie the Integrations page would
        repeat.

        ``reuse_pending`` resumes an identical remote setup without credentials
        instead of inserting another row. Existing credentials, settings, and
        approvals are never changed; authenticated accounts remain independent.

        ``access_token_ciphertext`` and ``sealed_launch_spec`` are written in
        the same transaction as the insert. A rejected write raises
        :class:`~cloud_agent.mcp.errors.McpCredentialWriteRejected` and leaves
        no row.
        """
        rows = await self.create_mcp_connections(
            connections=[
                {
                    "user_id": user_id,
                    "label": label,
                    "transport": transport,
                    "location": location,
                    "url": url,
                    "launch_spec": launch_spec,
                    "auth_kind": auth_kind,
                    "auth_config": auth_config,
                    "template": template,
                    "account_label": account_label,
                    "request_timeout_s": request_timeout_s,
                    "reuse_pending": reuse_pending,
                    "access_token_ciphertext": access_token_ciphertext,
                    "auth_config_patch": auth_config_patch,
                    "sealed_launch_spec": sealed_launch_spec,
                }
            ]
        )
        return rows[0]

    async def create_mcp_connections(
        self, *, connections: Sequence[Mapping[str, Any]]
    ) -> list[dict[str, Any]]:
        """Insert every draft, with its credentials, in one transaction.

        One rejected credential write or a later insert failure rolls the whole
        batch back. Callers encrypt before this call, so a missing key never
        reaches it.
        """
        async with self._pool.acquire() as conn, conn.transaction():
            return [
                await self._insert_mcp_connection(conn, **_create_kwargs(item))
                for item in connections
            ]

    async def _insert_mcp_connection(
        self,
        conn: asyncpg.Connection,
        *,
        user_id: str,
        label: str,
        transport: str,
        location: str,
        url: str | None,
        launch_spec: dict[str, Any] | None,
        auth_kind: str,
        auth_config: dict[str, Any],
        template: str | None = None,
        account_label: str | None = None,
        request_timeout_s: float | None = None,
        reuse_pending: bool = False,
        access_token_ciphertext: str | None = None,
        auth_config_patch: Mapping[str, Any] | None = None,
        sealed_launch_spec: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Insert one connection on ``conn`` and store any credential with it."""
        if (
            reuse_pending
            and location == "remote"
            and transport in {"streamable-http", "http-sse"}
            and url is not None
            and launch_spec is None
            and auth_kind in {"none", "oauth"}
            and not auth_config
            and access_token_ciphertext is None
            and sealed_launch_spec is None
        ):
            # Serialize setup retries across API workers, even before the
            # first row exists. The owner scope also keeps tenants apart.
            await conn.execute(
                "SELECT pg_advisory_xact_lock(hashtextextended('agent-mcp-setup:' || $1, 0))",
                user_id,
            )
            existing = await conn.fetchrow(
                f"""
                SELECT {self._MCP_CONNECTION_COLUMNS}
                FROM agent_mcp_connections
                WHERE user_id = $1 AND label = $2 AND transport = $3
                  AND location = $4 AND url = $5 AND launch_spec IS NULL
                  AND account_label IS NOT DISTINCT FROM $6::text
                  AND request_timeout_s IS NOT DISTINCT FROM $7::double precision
                  AND auth_kind IN ('none', 'oauth')
                  AND access_token_ciphertext IS NULL AND refresh_token_ciphertext IS NULL
                  AND deleted_at IS NULL AND status <> 'disabled'
                ORDER BY id LIMIT 1 FOR UPDATE
                """,
                user_id,
                label,
                transport,
                location,
                url,
                account_label,
                request_timeout_s,
            )
            if existing is not None:
                return self._mcp_connection(existing)
        # Credentials and sealed environment values belong to the INSERT
        # itself. Besides avoiding an intermediate credential-less row, this
        # means a database rejection of any part of the draft aborts the whole
        # batch at its insertion point.
        stored_auth_config = dict(auth_config)
        if access_token_ciphertext is not None:
            stored_auth_config.update(dict(auth_config_patch or {}))
        stored_launch_spec = (
            dict(sealed_launch_spec) if sealed_launch_spec is not None else launch_spec
        )
        row = await conn.fetchrow(
            f"""
            INSERT INTO agent_mcp_connections
                (user_id, label, template, transport, location, url, launch_spec,
                 auth_kind, auth_config, account_label, status,
                 request_timeout_s, access_token_ciphertext, credential_version)
            VALUES ($1, $2, $3, $4, $5, $6, $7::jsonb, $8, $9::jsonb, $10, 'draft',
                    $11, $12, CASE WHEN $12::text IS NULL THEN 1 ELSE 2 END)
            RETURNING {self._MCP_CONNECTION_COLUMNS}
            """,
            user_id,
            label,
            template,
            transport,
            location,
            url,
            json.dumps(stored_launch_spec) if stored_launch_spec is not None else None,
            auth_kind,
            json.dumps(stored_auth_config),
            account_label,
            request_timeout_s,
            access_token_ciphertext,
        )
        return self._mcp_connection(row)

    async def update_mcp_connection(
        self,
        *,
        connection_id: int,
        user_id: str,
        expected_config_revision: int,
        label: str | None = None,
        transport: str | None = None,
        location: str | None = None,
        url: str | None = None,
        launch_spec: dict[str, Any] | None = None,
        auth_kind: str | None = None,
        auth_config: dict[str, Any] | None = None,
        account_label: str | None = None,
        request_timeout_s: float | None = None,
        reset_status: bool = False,
    ) -> dict[str, Any] | None:
        """Apply a configuration edit, refusing a stale revision.

        Every field that decides *what is reached* -- transport, location,
        endpoint, launch spec, authentication -- moves both the config revision
        and the authorization epoch, which invalidates the thread bindings that
        were approved against the previous target. A label edit moves neither:
        renaming a connection is not a reason to make every session re-approve
        the tools it already has.

        Target changes reset diagnosis and remove the old discovery and pending
        OAuth flows in the same transaction as the edit. A change of address,
        transport, location or authentication kind also clears the stored
        credential and the OAuth state behind it, so the next request cannot
        carry a token or secret to a target it was not issued for.
        """
        security_relevant = any(
            value is not None
            for value in (transport, location, url, launch_spec, auth_kind, auth_config)
        )
        # A credential belongs to the address and the kind of authentication
        # it was issued for, so an edit that changes either drops it, with the
        # OAuth issuer, resource, endpoints and client it would be refreshed
        # and re-authorized through. Compared with the stored value -- SET sees
        # the row as it was -- so re-saving an unchanged address keeps it.
        retargeted = (
            "(($6::boolean AND url IS DISTINCT FROM $7)"
            " OR transport IS DISTINCT FROM COALESCE($4, transport)"
            " OR location IS DISTINCT FROM COALESCE($5, location)"
            " OR auth_kind IS DISTINCT FROM COALESCE($10, auth_kind))"
        )
        async with self._pool.acquire() as conn, conn.transaction():
            row = await conn.fetchrow(
                f"""
                UPDATE agent_mcp_connections SET
                    label = COALESCE($3, label),
                    transport = COALESCE($4, transport),
                    location = COALESCE($5, location),
                    url = CASE WHEN $6::boolean THEN $7 ELSE url END,
                    launch_spec = CASE WHEN $8::boolean THEN $9::jsonb ELSE launch_spec END,
                    auth_kind = COALESCE($10, auth_kind),
                    auth_config = CASE
                        WHEN $11::boolean THEN $12::jsonb
                        WHEN {retargeted}
                            THEN auth_config - 'client_id' - 'managed_github_oauth' - 'hint'
                        ELSE auth_config END,
                    account_label = COALESCE($13, account_label),
                    request_timeout_s = COALESCE($17, request_timeout_s),
                    status = CASE WHEN $14::boolean THEN 'draft' ELSE status END,
                    error_kind = CASE WHEN $14::boolean THEN 'none' ELSE error_kind END,
                    error_detail = CASE WHEN $14::boolean THEN NULL ELSE error_detail END,
                    access_token_ciphertext = CASE
                        WHEN {retargeted} THEN NULL ELSE access_token_ciphertext END,
                    refresh_token_ciphertext = CASE
                        WHEN {retargeted} THEN NULL ELSE refresh_token_ciphertext END,
                    client_secret_ciphertext = CASE
                        WHEN {retargeted} THEN NULL ELSE client_secret_ciphertext END,
                    expires_at = CASE WHEN {retargeted} THEN NULL ELSE expires_at END,
                    scopes = CASE WHEN {retargeted} THEN NULL ELSE scopes END,
                    issuer = CASE WHEN {retargeted} THEN NULL ELSE issuer END,
                    resource = CASE WHEN {retargeted} THEN NULL ELSE resource END,
                    refresh_rotates = CASE WHEN {retargeted} THEN FALSE ELSE refresh_rotates END,
                    token_endpoint = CASE WHEN {retargeted} THEN NULL ELSE token_endpoint END,
                    revocation_endpoint = CASE
                        WHEN {retargeted} THEN NULL ELSE revocation_endpoint END,
                    registration_endpoint = CASE
                        WHEN {retargeted} THEN NULL ELSE registration_endpoint END,
                    client_auth_method = CASE
                        WHEN {retargeted} THEN NULL ELSE client_auth_method END,
                    registration = CASE WHEN {retargeted} THEN NULL ELSE registration END,
                    credential_version = credential_version
                        + CASE WHEN {retargeted} THEN 1 ELSE 0 END,
                    oauth_generation = oauth_generation
                        + CASE WHEN {retargeted} THEN 1 ELSE 0 END,
                    config_revision = config_revision + CASE WHEN $15::boolean THEN 1 ELSE 0 END,
                    authorization_epoch = authorization_epoch + CASE WHEN $15::boolean THEN 1 ELSE 0 END,
                    updated_at = NOW()
                WHERE id = $1 AND user_id = $2 AND config_revision = $16 AND deleted_at IS NULL
                RETURNING {self._MCP_CONNECTION_COLUMNS}
                """,
                connection_id,
                user_id,
                label,
                transport,
                location,
                url is not None,
                url,
                launch_spec is not None,
                json.dumps(launch_spec) if launch_spec is not None else None,
                auth_kind,
                auth_config is not None,
                json.dumps(auth_config) if auth_config is not None else None,
                account_label,
                reset_status or security_relevant,
                security_relevant,
                expected_config_revision,
                request_timeout_s,
            )
            if row is not None and security_relevant:
                # Keep the connection row locked until old-target state is gone.
                # New discovery and authorization both lock this row to publish,
                # so neither can be deleted by cleanup after the edit commits.
                await conn.execute(
                    "DELETE FROM agent_mcp_capabilities WHERE connection_id = $1",
                    connection_id,
                )
                await conn.execute(
                    "DELETE FROM agent_mcp_oauth_flows WHERE connection_id = $1 AND consumed_at IS NULL",
                    connection_id,
                )
        return self._mcp_connection(row) if row is not None else None

    async def disconnect_mcp_connection(self, *, connection_id: int, user_id: str) -> bool:
        """Soft-disconnect one connection: tombstone it and bump the epoch.

        The row is kept so that the bindings pointing at it can say *this was
        disconnected* rather than *this never existed*, and so a session that
        was authorized against it fails the epoch check instead of quietly
        resolving to nothing. The stored secret is cleared in the same
        statement, because a tombstone that still holds a token is not a
        disconnect.
        """
        async with self._pool.acquire() as conn, conn.transaction():
            row = await conn.fetchrow(
                """
                UPDATE agent_mcp_connections SET
                    deleted_at = NOW(),
                    status = 'disabled',
                    access_token_ciphertext = NULL,
                    refresh_token_ciphertext = NULL,
                    client_secret_ciphertext = NULL,
                    expires_at = NULL,
                    authorization_epoch = authorization_epoch + 1,
                    credential_version = credential_version + 1,
                    oauth_generation = oauth_generation + 1,
                    updated_at = NOW()
                WHERE id = $1 AND user_id = $2 AND deleted_at IS NULL
                RETURNING id
                """,
                connection_id,
                user_id,
            )
            if row is not None:
                await conn.execute(
                    "DELETE FROM agent_mcp_oauth_flows WHERE connection_id = $1 AND consumed_at IS NULL",
                    connection_id,
                )
        return row is not None

    async def set_mcp_connection_enabled(
        self, *, connection_id: int, user_id: str, enabled: bool
    ) -> dict[str, Any] | None:
        """Toggle account-wide availability without deleting credentials or tool choices."""
        async with self._pool.acquire() as conn, conn.transaction():
            row = await conn.fetchrow(
                f"""
                UPDATE agent_mcp_connections
                SET enabled = $3,
                    authorization_epoch = authorization_epoch + (enabled IS DISTINCT FROM $3)::int,
                    updated_at = NOW()
                WHERE id = $1 AND user_id = $2 AND deleted_at IS NULL
                RETURNING {self._MCP_CONNECTION_COLUMNS}
                """,
                connection_id,
                user_id,
                enabled,
            )
            if row is not None and not enabled:
                await conn.execute(
                    f"UPDATE agent_mcp_invocations SET {self._EXPIRED} "
                    "WHERE connection_id = $1 AND status IN ('pending', 'approved')",
                    connection_id,
                )
        return self._mcp_connection(row) if row is not None else None

    async def set_mcp_connection_status(
        self,
        *,
        connection_id: int,
        status: str,
        error_kind: str = "none",
        error_detail: str | None = None,
        expected_config_revision: int | None = None,
        expected_authorization_epoch: int | None = None,
        expected_capability_revision: int | None = None,
        expected_credential_version: int | None = None,
    ) -> bool:
        """Record what the last test or discovery found."""
        async with self._pool.acquire() as conn:
            updated = await conn.fetchval(
                """
                UPDATE agent_mcp_connections SET
                    status = $2, error_kind = $3, error_detail = $4,
                    last_checked_at = NOW(), updated_at = NOW()
                WHERE id = $1 AND deleted_at IS NULL
                  AND ($5::bigint IS NULL OR config_revision = $5)
                  AND ($6::bigint IS NULL OR authorization_epoch = $6)
                  AND ($7::bigint IS NULL OR capability_revision = $7)
                  AND ($8::bigint IS NULL OR credential_version = $8)
                RETURNING id
                """,
                connection_id,
                status,
                error_kind,
                error_detail,
                expected_config_revision,
                expected_authorization_epoch,
                expected_capability_revision,
                expected_credential_version,
            )
        return updated is not None
