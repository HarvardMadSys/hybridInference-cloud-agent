"""Postgres persistence for generic MCP connections and fenced invocations."""

from __future__ import annotations

import asyncio
import json
from typing import TYPE_CHECKING, Any

from cloud_agent.logging_utils import get_logger
from cloud_agent.mcp.errors import McpConnectionCreateConflict
from cloud_agent.mcp.models import Capability, CapabilityKind
from cloud_agent.storage._sql.mcp import ensure_mcp_bindings, mcp_binding_row
from cloud_agent.storage._sql.mcp_residency import lock_mcp_residency, residency_fence_sql
from cloud_agent.storage._sql.rows import load_json as _load_json

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

    import asyncpg

logger = get_logger(__name__)

_CREATE_REQUIRED = ("user_id", "label", "transport", "location", "auth_kind", "auth_config")
_CREATE_OPTIONAL = (
    "url",
    "launch_spec",
    "template",
    "connector_id",
    "account_label",
    "request_timeout_s",
    "reuse_pending",
    "access_token_ciphertext",
    "auth_config_patch",
    "sealed_launch_spec",
)


def _remembered_rule_matches(intent: Any, snapshot: Any) -> bool:
    """Bind a remembered permission to the current stored capability definition."""
    if snapshot is None or any(
        snapshot[key] != intent[key] for key in ("config_revision", "capability_revision")
    ):
        return False
    surfaces = {
        "tool": "tools",
        "prompt": "prompts",
        "resource": "resources",
        "resource_template": "resource_templates",
    }
    kind = intent["capability_kind"]
    if kind not in surfaces:
        return False
    rule = _load_json(intent["always_allow_rule"])
    if not isinstance(rule, dict) or rule.get("policy") != "allow":
        return False
    for entry in _load_json(snapshot[surfaces[kind]]) or []:
        if not isinstance(entry, dict) or entry.get("name") != intent["capability_name"]:
            continue
        schema = entry.get("input_schema")
        capability = Capability(
            kind=CapabilityKind(kind),
            name=str(entry["name"]),
            description=str(entry.get("description") or ""),
            input_schema=schema if isinstance(schema, dict) else None,
        )
        return rule.get("fingerprint") == capability.fingerprint()
    return False


def _create_kwargs(item: Mapping[str, Any]) -> dict[str, Any]:
    """The insert arguments present on one batch draft."""
    missing = [key for key in _CREATE_REQUIRED if key not in item]
    if missing:
        joined = ", ".join(missing)
        raise TypeError(f"MCP connection draft is missing {joined}")
    kwargs = {key: item[key] for key in _CREATE_REQUIRED}
    kwargs.update({key: item[key] for key in _CREATE_OPTIONAL if key in item})
    return kwargs


def fence_params(
    *,
    config_revision: int | None = None,
    authorization_epoch: int | None = None,
    capability_revision: int | None = None,
    policy_revision: int | None = None,
    attempt_id: int | None = None,
    lease_generation: int | None = None,
) -> list[Any]:
    """The values :func:`_invocation_fence_sql` numbered, in its order.

    Kept as one pair so the clauses and their arguments cannot drift apart: a
    fence whose ``$n`` refers to the wrong value silently matches the wrong
    rows, which is worse than not checking at all because it looks like it does.
    """
    values: list[Any] = []
    for value in (config_revision, authorization_epoch, capability_revision, policy_revision):
        if value is not None:
            values.append(int(value))
    if attempt_id is not None:
        values.append(attempt_id)
    if lease_generation is not None:
        values.append(lease_generation)
    return values


def _invocation_fence_sql(
    *,
    first_index: int,
    config_revision: int | None = None,
    authorization_epoch: int | None = None,
    capability_revision: int | None = None,
    policy_revision: int | None = None,
    attempt_id: int | None = None,
    lease_generation: int | None = None,
) -> str:
    """The ``AND`` clauses binding an approval to the state it was given for.

    Every clause is optional so a caller that has nothing to compare against --
    a migration, a repair script -- can still address the row. The API never
    omits them.
    """
    clauses: list[str] = []
    index = first_index
    for column, value in (
        ("config_revision", config_revision),
        ("authorization_epoch", authorization_epoch),
        ("capability_revision", capability_revision),
        ("policy_revision", policy_revision),
    ):
        if value is not None:
            clauses.append(f" AND {column} = ${index}")
            index += 1
    if attempt_id is not None:
        # A job may have no attempt recorded, and ``= NULL`` is never true.
        clauses.append(f" AND attempt_id IS NOT DISTINCT FROM ${index}")
        index += 1
    if lease_generation is not None:
        clauses.append(f" AND lease_generation = ${index}")
        index += 1
    return "".join(clauses)


class PostgresMcpConnectionRepository:
    """Per-user connections, session selections, and MCP execution state."""

    def __init__(self, pool: asyncpg.Pool) -> None:
        """Use the control plane pool."""
        self._pool = pool

    @staticmethod
    async def _lock_connection(conn: Any, connection_id: int) -> bool:
        return bool(
            await conn.fetchval(
                "SELECT id FROM agent_mcp_connections WHERE id = $1 FOR UPDATE",
                connection_id,
            )
        )

    @staticmethod
    async def _lock_invocation(conn: Any, invocation_id: int) -> bool:
        return bool(
            await conn.fetchval(
                "SELECT id FROM agent_mcp_invocations WHERE id = $1 FOR UPDATE",
                invocation_id,
            )
        )

    @staticmethod
    async def _lock_oauth_flow(conn: Any, state_hash: str) -> bool:
        return bool(
            await conn.fetchval(
                "SELECT 1 FROM agent_mcp_oauth_flows WHERE state_hash = $1 FOR UPDATE",
                state_hash,
            )
        )

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
    def _mcp_connection(row: Any) -> dict[str, Any]:
        """Decode one connection row into plain Python values."""
        record = dict(row)
        for key in ("launch_spec", "auth_config"):
            record[key] = _load_json(record.get(key)) or {}
        record["scopes"] = list(record.get("scopes") or [])
        return record

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
        connector_id: int | None = None,
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
                    "connector_id": connector_id,
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
        connector_id: int | None = None,
        account_label: str | None = None,
        request_timeout_s: float | None = None,
        reuse_pending: bool = False,
        access_token_ciphertext: str | None = None,
        auth_config_patch: Mapping[str, Any] | None = None,
        sealed_launch_spec: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Insert one connection on ``conn`` and store any credential with it."""
        if connector_id is not None:
            if location != "user-connector":
                raise McpConnectionCreateConflict(
                    "a connector can only serve a user-connector connection"
                )
            owned_connector = await conn.fetchval(
                """
                SELECT id FROM agent_mcp_connectors
                WHERE id = $1 AND user_id = $2 AND status <> 'revoked'
                FOR SHARE
                """,
                connector_id,
                user_id,
            )
            if owned_connector is None:
                raise McpConnectionCreateConflict("that connector is no longer available")

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
                  AND connector_id IS NOT DISTINCT FROM $6::bigint
                  AND account_label IS NOT DISTINCT FROM $7::text
                  AND request_timeout_s IS NOT DISTINCT FROM $8::double precision
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
                connector_id,
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
                 auth_kind, auth_config, connector_id, account_label, status,
                 request_timeout_s, access_token_ciphertext, credential_version)
            VALUES ($1, $2, $3, $4, $5, $6, $7::jsonb, $8, $9::jsonb, $10, $11, 'draft',
                    $12, $13, CASE WHEN $13::text IS NULL THEN 1 ELSE 2 END)
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
            connector_id,
            account_label,
            request_timeout_s,
            access_token_ciphertext,
        )
        inserted_id = int(row["id"])
        if connector_id is not None:
            await conn.execute(
                """
                INSERT INTO agent_mcp_connector_connections (connector_id, connection_id)
                VALUES ($1, $2)
                """,
                connector_id,
                inserted_id,
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
                    connector_id = CASE
                        WHEN COALESCE($5, location) = 'user-connector' THEN connector_id
                        ELSE NULL
                    END,
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
            if row is not None:
                # Keep the reverse authorization exactly aligned with the
                # connection's one current route, including location edits
                # that remove that route.
                await conn.execute(
                    "DELETE FROM agent_mcp_connector_connections "
                    "WHERE connection_id = $1 AND connector_id IS DISTINCT FROM $2::bigint",
                    connection_id,
                    row["connector_id"],
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
                    connector_id = NULL,
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
                await conn.execute(
                    "DELETE FROM agent_mcp_connector_connections WHERE connection_id = $1",
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

    async def set_mcp_credential(
        self,
        *,
        connection_id: int,
        expected_authorization_epoch: int,
        access_token_ciphertext: str | None,
        refresh_token_ciphertext: str | None = None,
        client_secret_ciphertext: str | None = None,
        expires_at: Any = None,
        scopes: list[str] | None = None,
        issuer: str | None = None,
        resource: str | None = None,
        refresh_rotates: bool = False,
        client_auth_method: str | None = None,
        registration: str | None = None,
        token_endpoint: str | None = None,
        revocation_endpoint: str | None = None,
        registration_endpoint: str | None = None,
        auth_config_patch: Mapping[str, Any] | None = None,
        bump_epoch: bool = False,
        clear_refresh: bool = False,
        expected_oauth_generation: int | None = None,
        expected_config_revision: int | None = None,
        expected_refresh_generation: int | None = None,
        reset_status: bool = False,
        expected_credential_version: int | None = None,
    ) -> bool:
        """Write a credential, conditional on the state the flow started in.

        The epoch condition is what makes a late OAuth callback harmless: a
        flow that started before a disconnect carries the epoch it started
        under, and by the time it returns the row has moved past it, so the
        write matches no row instead of restoring a credential the owner
        removed.

        ``expected_oauth_generation`` and ``expected_config_revision`` close the
        window the epoch alone leaves open. A callback spends real time on the
        network between "this flow is still the current one" and "write what it
        returned", and in that window a *newer* flow can finish: the epoch is
        unchanged, because a re-authorization is not a change of target, so the
        late write would land on top of the newer token while the row still
        records the newer generation. Checking both here makes the two writes
        race for one row in a single statement, which is the only place a race
        can be settled.

        ``expected_refresh_generation`` is the same fence for the automatic
        refresh, which is the one caller that cannot see a newer flow finish:
        it holds a lease, spends the same time on the network, and its token
        response is worthless the moment that lease is superseded. Requiring the
        generation *and* a lease that has not expired means a slow refresher
        writes nothing rather than reviving the credential its successor is
        replacing -- and the successful write clears the lease, because the
        refresher that lands is done holding it.

        ``clear_refresh`` means the *previous* refresh token is gone -- an
        issuer that rotates invalidates it. It is not a request to discard the
        one being written: a rotating issuer returns a new refresh token in the
        same response, and writing NULL over it, which is what this column did,
        leaves every connection unable to renew once its access token expires.

        ``bump_epoch`` is for a *manual* replacement, where the identity behind
        the credential may be a different account and every approval made
        against the old one has to be re-made. A normal refresh keeps the epoch
        and only moves the credential version.

        ``reset_status`` publishes a new authorization's draft state with its
        credential, so a later discovery cannot be overwritten by the callback.

        ``expected_credential_version`` is the version the refresh claimed. The
        generation and lease fences cannot see an authorization callback that
        completes while the refresh is on the network: the callback moves none
        of them, so without this the refresh would write the old grant's
        tokens over the new one.
        """
        async with self._pool.acquire() as conn, conn.transaction():
            if not await self._lock_connection(conn, connection_id):
                return False
            updated = await conn.fetchval(
                """
                UPDATE agent_mcp_connections SET
                    access_token_ciphertext = $3,
                    refresh_token_ciphertext = CASE
                        WHEN $5::text IS NOT NULL THEN $5::text
                        WHEN $4::boolean THEN NULL
                        ELSE refresh_token_ciphertext END,
                    client_secret_ciphertext = COALESCE($6, client_secret_ciphertext),
                    expires_at = $7,
                    scopes = COALESCE($8, scopes),
                    issuer = COALESCE($9, issuer),
                    resource = COALESCE($10, resource),
                    refresh_rotates = $11,
                    client_auth_method = COALESCE($12, client_auth_method),
                    registration = COALESCE($13, registration),
                    token_endpoint = COALESCE($14, token_endpoint),
                    revocation_endpoint = COALESCE($15, revocation_endpoint),
                    registration_endpoint = COALESCE($16, registration_endpoint),
                    auth_config = auth_config || $17::jsonb,
                    credential_version = credential_version + 1,
                    authorization_epoch = authorization_epoch + CASE WHEN $18::boolean THEN 1 ELSE 0 END,
                    refresh_lease_expires_at = CASE
                        WHEN $21::bigint IS NULL THEN refresh_lease_expires_at
                        ELSE NULL END,
                    status = CASE WHEN $22::boolean THEN 'draft' ELSE status END,
                    error_kind = CASE WHEN $22::boolean THEN 'none' ELSE error_kind END,
                    error_detail = CASE WHEN $22::boolean THEN NULL ELSE error_detail END,
                    last_checked_at = CASE WHEN $22::boolean THEN NULL ELSE last_checked_at END,
                    updated_at = NOW()
                WHERE id = $1 AND authorization_epoch = $2 AND deleted_at IS NULL
                  AND oauth_generation = COALESCE($19::bigint, oauth_generation)
                  AND config_revision = COALESCE($20::bigint, config_revision)
                  AND ($21::bigint IS NULL
                       OR (refresh_generation = $21 AND refresh_lease_expires_at > clock_timestamp()))
                  AND credential_version = COALESCE($23::bigint, credential_version)
                RETURNING id
                """,
                connection_id,
                expected_authorization_epoch,
                access_token_ciphertext,
                clear_refresh,
                refresh_token_ciphertext,
                client_secret_ciphertext,
                expires_at,
                list(scopes) if scopes is not None else None,
                issuer,
                resource,
                refresh_rotates,
                client_auth_method,
                registration,
                token_endpoint,
                revocation_endpoint,
                registration_endpoint,
                json.dumps(dict(auth_config_patch or {})),
                bump_epoch,
                expected_oauth_generation,
                expected_config_revision,
                expected_refresh_generation,
                reset_status,
                expected_credential_version,
            )
        return updated is not None

    async def set_mcp_client_registration(
        self,
        *,
        connection_id: int,
        user_id: str,
        client_id: str,
        client_secret_ciphertext: str | None,
    ) -> bool:
        """Record the OAuth client a connection authorizes as.

        The target is unchanged, so neither the config revision nor the
        authorization epoch moves: a client id says *who is asking*, not *what
        is being reached*. The OAuth generation does move, because an
        authorization already in flight was started by the previous client and
        its callback must not complete the flow that replaces it.

        A secret belongs to one client. Naming the same client again without
        one keeps it; naming another client drops it, so a public client can
        be adopted and the previous client's secret is never sent as the new
        one's.
        """
        async with self._pool.acquire() as conn:
            updated = await conn.fetchval(
                """
                UPDATE agent_mcp_connections SET
                    auth_config = auth_config || jsonb_build_object('client_id', $3::text),
                    client_secret_ciphertext = CASE
                        WHEN $4::text IS NOT NULL THEN $4::text
                        WHEN auth_config->>'client_id' IS DISTINCT FROM $3::text THEN NULL
                        ELSE client_secret_ciphertext END,
                    oauth_generation = oauth_generation + 1,
                    updated_at = NOW()
                WHERE id = $1 AND user_id = $2 AND deleted_at IS NULL
                RETURNING id
                """,
                connection_id,
                user_id,
                client_id,
                client_secret_ciphertext,
            )
        return updated is not None

    async def claim_mcp_refresh(
        self,
        *,
        connection_id: int,
        expected_credential_version: int,
        lease_seconds: float,
    ) -> dict[str, Any] | None:
        """Take exclusive ownership of refreshing one connection's credential.

        Two conditions, and they answer different questions. The version says
        *the row I read is still the row I read* -- a credential written since
        then means this caller would be spending a token that has already been
        replaced. The lease says *nobody else is on the network with it right
        now*, which the version cannot: bumping a version is a write, not a
        lock, and it is visible the instant it commits, so a second caller that
        reads afterwards claims the row while the first is still waiting on the
        token endpoint. A rotating authorization server invalidates the first
        refresh token the moment the second call uses it, so that is the
        connection destroyed rather than merely refreshed twice.

        The lease is also what keeps the property the version bump was chosen
        for: a refresher that dies mid-flight leaves a lease that expires, not a
        lock somebody has to clear, and the row is claimable again at the *new*
        generation. That retry is bounded by ``lease_seconds`` instead of
        immediate, which is the whole difference between the two mechanisms.

        The generation is bumped here and returned to the caller, which hands it
        back to :meth:`set_mcp_credential`. A claim that was superseded while it
        was on the network therefore cannot write its answer over its
        successor's, and the lease that successor holds is untouched.
        """
        if lease_seconds <= 0:
            raise ValueError("an MCP refresh lease must be positive")
        async with self._pool.acquire() as conn, conn.transaction():
            if not await self._lock_connection(conn, connection_id):
                return None
            row = await conn.fetchrow(
                f"""
                UPDATE agent_mcp_connections
                SET refresh_generation = refresh_generation + 1,
                    refresh_lease_expires_at = clock_timestamp() + make_interval(secs => $3),
                    updated_at = NOW()
                WHERE id = $1 AND credential_version = $2 AND deleted_at IS NULL
                  AND (refresh_lease_expires_at IS NULL OR refresh_lease_expires_at <= clock_timestamp())
                RETURNING {self._MCP_CONNECTION_COLUMNS}
                """,
                connection_id,
                expected_credential_version,
                lease_seconds,
            )
        return self._mcp_connection(row) if row is not None else None

    async def release_mcp_refresh_claim(
        self, *, connection_id: int, refresh_generation: int
    ) -> bool:
        """Give up a refresh lease early, after an attempt that wrote nothing.

        Called on the failure paths only. A refused or unreachable token
        endpoint rotated nothing, so holding the lease would only make the owner
        wait out a timer for a refresh that never happened.

        Fenced on the generation it was issued under: a lease that has since
        been superseded belongs to the worker now holding it, and a late
        releaser must not hand the row to a third caller while that one is still
        on the network.
        """
        async with self._pool.acquire() as conn, conn.transaction():
            if not await self._lock_connection(conn, connection_id):
                return False
            updated = await conn.fetchval(
                """
                UPDATE agent_mcp_connections
                SET refresh_lease_expires_at = NULL, updated_at = NOW()
                WHERE id = $1 AND refresh_generation = $2 AND deleted_at IS NULL
                  AND refresh_lease_expires_at > clock_timestamp()
                RETURNING id
                """,
                connection_id,
                refresh_generation,
            )
        return updated is not None

    async def get_mcp_capabilities(self, *, connection_id: int) -> dict[str, Any] | None:
        """The stored capability snapshot for one connection."""
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT * FROM agent_mcp_capabilities WHERE connection_id = $1",
                connection_id,
            )
        if row is None:
            return None
        record = dict(row)
        for key in ("tools", "resources", "resource_templates", "prompts", "support"):
            record[key] = _load_json(record.get(key)) or ({} if key == "support" else [])
        return record

    async def save_mcp_capabilities(
        self,
        *,
        connection_id: int,
        config_revision: int,
        protocol_version: str,
        server_name: str,
        server_version: str,
        tools: list[dict[str, Any]],
        resources: list[dict[str, Any]],
        resource_templates: list[dict[str, Any]],
        prompts: list[dict[str, Any]],
        support: dict[str, Any],
        partial: bool = False,
        expected_authorization_epoch: int | None = None,
        expected_capability_revision: int | None = None,
        expected_credential_version: int | None = None,
    ) -> int:
        """Store a discovery result and return the new capability revision.

        The revision is incremented in the same statement that writes the
        snapshot, so a binding's fence can never be checked against a snapshot
        written by a different discovery. ``partial`` records a discovery that
        succeeded on some surfaces and failed on others: the connection is
        usable, and the interface says which part is missing rather than
        presenting a short catalogue as the whole truth.
        """
        async with self._pool.acquire() as conn, conn.transaction():
            revision = await conn.fetchval(
                """
                UPDATE agent_mcp_connections
                SET capability_revision = capability_revision + 1, updated_at = NOW()
                WHERE id = $1 AND deleted_at IS NULL
                  AND config_revision = $2
                  AND ($3::bigint IS NULL OR authorization_epoch = $3)
                  AND ($4::bigint IS NULL OR capability_revision = $4)
                  AND ($5::bigint IS NULL OR credential_version = $5)
                RETURNING capability_revision
                """,
                connection_id,
                config_revision,
                expected_authorization_epoch,
                expected_capability_revision,
                expected_credential_version,
            )
            if revision is None:
                return 0
            await conn.execute(
                """
                INSERT INTO agent_mcp_capabilities
                    (connection_id, capability_revision, config_revision, protocol_version,
                     server_name, server_version, tools, resources, resource_templates,
                     prompts, support, discovered_at)
                VALUES ($1, $2, $3, $4, $5, $6, $7::jsonb, $8::jsonb, $9::jsonb,
                        $10::jsonb, $11::jsonb, NOW())
                ON CONFLICT (connection_id) DO UPDATE SET
                    capability_revision = EXCLUDED.capability_revision,
                    config_revision = EXCLUDED.config_revision,
                    protocol_version = EXCLUDED.protocol_version,
                    server_name = EXCLUDED.server_name,
                    server_version = EXCLUDED.server_version,
                    tools = EXCLUDED.tools,
                    resources = EXCLUDED.resources,
                    resource_templates = EXCLUDED.resource_templates,
                    prompts = EXCLUDED.prompts,
                    support = EXCLUDED.support,
                    discovered_at = NOW()
                """,
                connection_id,
                revision,
                config_revision,
                protocol_version,
                server_name,
                server_version,
                json.dumps(tools),
                json.dumps(resources),
                json.dumps(resource_templates),
                json.dumps(prompts),
                json.dumps({**support, "partial": partial}),
            )
        return int(revision)

    async def delete_mcp_capabilities(self, *, connection_id: int) -> None:
        """Drop a snapshot, used when discovery proves the target changed."""
        async with self._pool.acquire() as conn:
            await conn.execute(
                "DELETE FROM agent_mcp_capabilities WHERE connection_id = $1", connection_id
            )

    async def create_mcp_oauth_flow(
        self,
        *,
        state_hash: str,
        user_id: str,
        connection_id: int,
        config_revision: int,
        authorization_epoch: int,
        expected_oauth_generation: int,
        code_verifier_ciphertext: str,
        client_id: str,
        client_secret_ciphertext: str | None,
        client_auth_method: str,
        registration: str,
        issuer: str,
        resource: str,
        redirect_uri: str,
        scopes: list[str],
        authorization_endpoint: str,
        token_endpoint: str,
        revocation_endpoint: str | None,
        registration_endpoint: str | None,
        expires_at: Any,
    ) -> int | None:
        """Record one started authorization and return its generation.

        The new generation is allocated in the same statement that fences the
        connection on the config revision, authorization epoch, and OAuth
        generation the caller read. The flow row stores that returned value.
        A stale start matches nothing: it does not move the generation and it
        does not delete a newer pending flow. ``None`` means the fence missed.
        """
        async with self._pool.acquire() as conn, conn.transaction():
            generation = await conn.fetchval(
                """
                UPDATE agent_mcp_connections
                SET oauth_generation = oauth_generation + 1, updated_at = NOW()
                WHERE id = $1 AND user_id = $2 AND deleted_at IS NULL
                  AND config_revision = $3
                  AND authorization_epoch = $4
                  AND oauth_generation = $5
                RETURNING oauth_generation
                """,
                connection_id,
                user_id,
                config_revision,
                authorization_epoch,
                expected_oauth_generation,
            )
            if generation is None:
                return None
            await conn.execute(
                "DELETE FROM agent_mcp_oauth_flows WHERE connection_id = $1 AND consumed_at IS NULL",
                connection_id,
            )
            await conn.execute(
                """
                INSERT INTO agent_mcp_oauth_flows
                    (state_hash, user_id, connection_id, config_revision,
                     authorization_epoch, oauth_generation, code_verifier_ciphertext,
                     client_id, client_secret_ciphertext, client_auth_method, registration,
                     issuer, resource, redirect_uri, scopes, authorization_endpoint,
                     token_endpoint, revocation_endpoint, registration_endpoint, expires_at)
                VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13, $14,
                        $15, $16, $17, $18, $19, $20)
                """,
                state_hash,
                user_id,
                connection_id,
                config_revision,
                authorization_epoch,
                int(generation),
                code_verifier_ciphertext,
                client_id,
                client_secret_ciphertext,
                client_auth_method,
                registration,
                issuer,
                resource,
                redirect_uri,
                list(scopes),
                authorization_endpoint,
                token_endpoint,
                revocation_endpoint,
                registration_endpoint,
                expires_at,
            )
        return int(generation)

    async def consume_mcp_oauth_flow(
        self, *, state_hash: str, user_id: str
    ) -> dict[str, Any] | None:
        """Claim one of ``user_id``'s flows exactly once, returning it, or ``None``.

        The ``consumed_at IS NULL`` predicate is inside the ``UPDATE``, so two
        callbacks racing on the same state cannot both proceed: the second
        matches no row. Marking it consumed before the token exchange is
        deliberate -- a callback that fails after this point must be restarted
        by the user, which is preferable to a state value that can be replayed.

        The owner is part of the match, as it is for the source-control state.
        The state alone identifies a flow, not who may finish it: a browser
        signed in as somebody else must not complete it, or that person's
        provider grant would be stored on the starter's connection. A flow
        presented by another account is left untouched for its owner.
        """
        async with self._pool.acquire() as conn, conn.transaction():
            if not await self._lock_oauth_flow(conn, state_hash):
                return None
            row = await conn.fetchrow(
                """
                UPDATE agent_mcp_oauth_flows
                SET consumed_at = NOW()
                WHERE state_hash = $1 AND user_id = $2
                  AND consumed_at IS NULL AND expires_at > clock_timestamp()
                RETURNING *
                """,
                state_hash,
                user_id,
            )
        if row is None:
            return None
        record = dict(row)
        record["scopes"] = list(record.get("scopes") or [])
        return record

    async def delete_mcp_oauth_flows(self, *, connection_id: int) -> int:
        """Drop every unconsumed flow for one connection."""
        async with self._pool.acquire() as conn:
            deleted = await conn.fetch(
                "DELETE FROM agent_mcp_oauth_flows WHERE connection_id = $1 AND consumed_at IS NULL "
                "RETURNING state_hash",
                connection_id,
            )
        return len(deleted)

    async def list_mcp_bindings(self, *, thread_id: str) -> list[dict[str, Any]]:
        """Ensure default bindings for the owner's servers and return all overrides."""
        async with self._pool.acquire() as conn, conn.transaction():
            await ensure_mcp_bindings(conn, thread_id=thread_id)
            rows = await conn.fetch(
                """
                SELECT b.thread_id, b.connection_id, b.alias, b.enabled, b.selection_pending,
                       b.config_revision, b.authorization_epoch, b.capability_revision,
                       b.policy_revision, b.rules, b.resource_rules, b.prompt_rules,
                       c.label, c.transport, c.location, c.status AS connection_status,
                       c.error_kind, c.error_detail, c.deleted_at
                FROM agent_mcp_thread_bindings b
                JOIN agent_mcp_connections c ON c.id = b.connection_id
                WHERE b.thread_id = $1
                ORDER BY b.alias, b.connection_id
                """,
                thread_id,
            )
        bindings: list[dict[str, Any]] = []
        for row in rows:
            record = dict(row)
            for key in ("rules", "resource_rules", "prompt_rules"):
                record[key] = _load_json(record.get(key)) or {}
            bindings.append(record)
        return bindings

    async def replace_mcp_bindings(
        self, *, thread_id: str, bindings: list[dict[str, Any]], merge: bool = False
    ) -> None:
        """Replace a conversation's selection wholesale.

        With merge=True only named servers change. Otherwise unmentioned
        existing bindings become disabled opt-outs instead of disappearing.
        """
        async with self._pool.acquire() as conn, conn.transaction():
            # Serialize replacements even when the old selection is empty.
            await conn.fetchval("SELECT id FROM agent_threads WHERE id = $1 FOR UPDATE", thread_id)
            previous = {
                int(row["connection_id"]): mcp_binding_row(row)
                for row in await conn.fetch(
                    "SELECT * FROM agent_mcp_thread_bindings WHERE thread_id = $1 FOR UPDATE",
                    thread_id,
                )
            }
            revisions: dict[int, int] = {}
            supplied = {int(binding["connection_id"]) for binding in bindings}
            # An omitted server is explicitly disabled, never deleted: otherwise
            # the next automatic sync would re-enable a user-removed server.
            bindings = [
                *bindings,
                *[
                    old if merge else {**old, "enabled": False}
                    for connection_id, old in previous.items()
                    if connection_id not in supplied
                ],
            ]
            changed: set[int] = set()
            for binding in bindings:
                connection_id = int(binding["connection_id"])
                old = previous.get(connection_id)
                unchanged = old is not None and all(
                    old[key] == binding.get(key, default)
                    for key, default in (
                        ("alias", ""),
                        ("enabled", True),
                        ("selection_pending", False),
                        ("config_revision", 1),
                        ("authorization_epoch", 1),
                        ("capability_revision", 1),
                        ("rules", {}),
                        ("resource_rules", {}),
                        ("prompt_rules", {}),
                    )
                )
                revisions[connection_id] = (
                    int(old["policy_revision"])
                    if unchanged and old is not None
                    else int(await conn.fetchval("SELECT nextval('agent_mcp_policy_revision_seq')"))
                )
                if not unchanged:
                    changed.add(connection_id)
            if changed:
                # Revocation is visible to both the waiting relay and the UI.
                # A call already dispatching keeps its outcome; it is never replayed.
                await conn.execute(
                    f"UPDATE agent_mcp_invocations SET {self._EXPIRED} "
                    "WHERE thread_id = $1 AND connection_id = ANY($2::bigint[]) "
                    "AND status IN ('pending', 'approved')",
                    thread_id,
                    list(changed),
                )
            await conn.execute(
                "DELETE FROM agent_mcp_thread_bindings WHERE thread_id = $1", thread_id
            )
            for binding in bindings:
                await conn.execute(
                    """
                    INSERT INTO agent_mcp_thread_bindings
                        (thread_id, connection_id, alias, enabled, selection_pending,
                         config_revision, authorization_epoch, capability_revision,
                         policy_revision, rules, resource_rules, prompt_rules)
                    VALUES ($1, $2, $3, $4, $12, $5, $6, $7, $8, $9::jsonb, $10::jsonb, $11::jsonb)
                    """,
                    thread_id,
                    int(binding["connection_id"]),
                    str(binding["alias"]),
                    bool(binding.get("enabled", True)),
                    int(binding["config_revision"]),
                    int(binding["authorization_epoch"]),
                    int(binding["capability_revision"]),
                    revisions[int(binding["connection_id"])],
                    json.dumps(binding.get("rules") or {}),
                    json.dumps(binding.get("resource_rules") or {}),
                    json.dumps(binding.get("prompt_rules") or {}),
                    bool(binding.get("selection_pending", False)),
                )

    async def create_mcp_invocation(
        self,
        *,
        job_id: str,
        attempt_id: int | None,
        lease_generation: int,
        thread_id: str,
        connection_id: int,
        capability_name: str,
        arguments_digest: str,
        arguments_ciphertext: str | None,
        config_revision: int,
        authorization_epoch: int,
        capability_revision: int,
        policy_revision: int,
        expires_at: Any,
        capability_kind: str = "tool",
        residency_generation: int | None = None,
    ) -> dict[str, Any]:
        """Record a call that is waiting for its owner."""
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                INSERT INTO agent_mcp_invocations
                    (job_id, attempt_id, lease_generation, thread_id, connection_id,
                     capability_name, capability_kind, arguments_digest,
                     arguments_ciphertext, config_revision, authorization_epoch,
                     capability_revision, policy_revision, expires_at, residency_generation)
                VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13, $14, $15)
                RETURNING *
                """,
                job_id,
                attempt_id,
                lease_generation,
                thread_id,
                connection_id,
                capability_name,
                capability_kind,
                arguments_digest,
                arguments_ciphertext,
                config_revision,
                authorization_epoch,
                capability_revision,
                policy_revision,
                expires_at,
                residency_generation,
            )
        return dict(row)

    async def get_mcp_invocation(self, *, invocation_id: int) -> dict[str, Any] | None:
        """One invocation by id."""
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT * FROM agent_mcp_invocations WHERE id = $1", invocation_id
            )
        return dict(row) if row is not None else None

    async def list_mcp_invocations(
        self,
        *,
        job_id: str | None = None,
        thread_id: str | None = None,
        statuses: list[str] | None = None,
        user_id: str | None = None,
        limit: int = 50,
        before_id: int | None = None,
    ) -> list[dict[str, Any]]:
        """Invocations filtered by job, thread, state or owner, newest first.

        ``user_id`` scopes through the job rather than a column on the
        invocation, because an invocation belongs to whoever owns the job that
        made it. Filtering the *list* is not a convenience: the rows carry the
        decrypted arguments, so a listing that does not scope by owner hands one
        user another user's tool call bodies. Callers that serve a request must
        pass it. Owner-scoped reads omit deleted conversations; unscoped
        internal audit reads retain their invocation history.

        ``before_id`` continues strictly below the previous page's last id,
        so new calls cannot shift rows between pages. A pending state filter
        omits calls whose deadline has passed, even before the expiry worker
        persists that transition. Reads never change invocation state.
        """
        clauses: list[str] = []
        params: list[Any] = []
        if user_id is not None:
            params.append(user_id)
            clauses.append(
                "job_id IN (SELECT j.id FROM agent_jobs j "
                "JOIN agent_threads t ON t.id = j.thread_id "
                f"WHERE j.user_id = ${len(params)} AND t.deleted_at IS NULL)"
            )
        if job_id is not None:
            params.append(job_id)
            clauses.append(f"job_id = ${len(params)}")
        if thread_id is not None:
            params.append(thread_id)
            clauses.append(f"thread_id = ${len(params)}")
        if statuses:
            params.append(list(statuses))
            clauses.append(f"status = ANY(${len(params)})")
            if "pending" in statuses:
                clauses.append("(status <> 'pending' OR expires_at > clock_timestamp())")
        if before_id is not None:
            params.append(before_id)
            clauses.append(f"id < ${len(params)}")
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        params.append(max(1, min(limit, 200)))
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(
                f"""
                SELECT * FROM agent_mcp_invocations {where}
                ORDER BY id DESC LIMIT ${len(params)}
                """,
                *params,
            )
        return [dict(row) for row in rows]

    async def decide_mcp_invocation(
        self,
        *,
        invocation_id: int,
        approve: bool,
        decided_by: str,
        always_allow_rule: dict[str, Any] | None = None,
    ) -> dict[str, Any] | None:
        """Approve or deny one pending call.

        Only a pending call may be decided, enforced in the predicate rather
        than by reading first: an approval that arrives after the call expired
        must not revive it, and a check-then-write would let exactly that race
        through.
        """
        if always_allow_rule is not None and not approve:
            raise ValueError("Always allow requires approval")
        target = "approved" if approve else "denied"
        async with self._pool.acquire() as conn, conn.transaction():
            if not await self._lock_invocation(conn, invocation_id):
                return None
            row = await conn.fetchrow(
                """
                UPDATE agent_mcp_invocations
                SET status = $2, decided_at = NOW(), decided_by = $3,
                    always_allow_rule = $4::jsonb,
                    always_allow_status = CASE WHEN $4::jsonb IS NULL THEN NULL ELSE 'pending' END
                WHERE id = $1 AND status = 'pending' AND expires_at > clock_timestamp()
                RETURNING *
                """,
                invocation_id,
                target,
                decided_by,
                json.dumps(always_allow_rule) if always_allow_rule is not None else None,
            )
            if row is None:
                # Distinguish "already decided" from "too late" for the caller,
                # without changing either outcome.
                await conn.execute(
                    f"UPDATE agent_mcp_invocations SET {self._EXPIRED} "
                    "WHERE id = $1 AND status = 'pending' "
                    "AND expires_at <= clock_timestamp()",
                    invocation_id,
                )
        if row is not None and not approve:
            # A denial may release another remembered decision on this binding.
            # Save now, before the agent's next call enters the approval queue.
            await self._flush_remembered_approvals(row)
        return dict(row) if row is not None else None

    async def claim_mcp_invocation(
        self,
        *,
        invocation_id: int,
        config_revision: int | None = None,
        authorization_epoch: int | None = None,
        capability_revision: int | None = None,
        policy_revision: int | None = None,
        attempt_id: int | None = None,
        lease_generation: int | None = None,
        residency_generation: int | None = None,
    ) -> dict[str, Any] | None:
        """Take exclusive ownership of dispatching one approved call.

        The final ``UPDATE ... WHERE status = 'approved'`` is the
        mutual-exclusion point. Two workers may both arrive; one matches the
        row, the other matches nothing and must not send. ``dispatch_generation``
        is bumped so that a worker whose lease was superseded can be refused by
        the upstream side even if it still holds this record.

        The fence is in the same predicate, for the same reason the status is:
        the window between "this approval is still valid" and "I am dispatching
        it" is exactly where a connection gets repointed, and a check performed
        before the ``UPDATE`` is a check that races with it. Requiring the
        versions here means an approval whose target moved is not merely
        discouraged but unmatchable.
        """
        fence = _invocation_fence_sql(
            config_revision=config_revision,
            authorization_epoch=authorization_epoch,
            capability_revision=capability_revision,
            policy_revision=policy_revision,
            attempt_id=attempt_id,
            lease_generation=lease_generation,
            first_index=2,
        )
        # A waiting approval is valid only while the live database still
        # describes the target, policy and attempt the owner approved.
        live_fence = (
            ""
            if attempt_id is None
            else f"""
            AND EXISTS (
                SELECT 1 FROM agent_jobs j
                JOIN agent_attempts a ON a.id = j.current_attempt_id
                JOIN agent_mcp_thread_bindings b ON b.thread_id = j.thread_id
                JOIN agent_mcp_connections c ON c.id = b.connection_id
                WHERE j.id = agent_mcp_invocations.job_id
                  AND j.state = 'running' AND a.status = 'running'
                  AND a.id = agent_mcp_invocations.attempt_id
                  AND a.lease_generation = agent_mcp_invocations.lease_generation
                  AND a.lease_expires_at > clock_timestamp()
                  AND {residency_fence_sql("agent_mcp_invocations.residency_generation")}
                  AND b.connection_id = agent_mcp_invocations.connection_id AND b.enabled
                  AND b.policy_revision = agent_mcp_invocations.policy_revision
                  AND c.user_id = j.user_id AND c.deleted_at IS NULL AND c.enabled
                  AND c.config_revision = agent_mcp_invocations.config_revision
                  AND c.authorization_epoch = agent_mcp_invocations.authorization_epoch
                  AND c.capability_revision = agent_mcp_invocations.capability_revision
            )
        """
        )
        async with self._pool.acquire() as conn, conn.transaction():
            target = await conn.fetchrow(
                """
                SELECT job_id, attempt_id, lease_generation, thread_id, connection_id,
                       residency_generation
                FROM agent_mcp_invocations WHERE id = $1
                """,
                invocation_id,
            )
            if target is None or target["residency_generation"] != residency_generation:
                return None
            if residency_generation is not None and attempt_id is None:
                return None
            if attempt_id is not None:
                if target["attempt_id"] != attempt_id or (
                    lease_generation is not None
                    and int(target["lease_generation"]) != lease_generation
                ):
                    return None
                # Follow the execution lock order and hold every row whose live
                # state authorizes dispatch.  The UPDATE below then observes a
                # fresh wall clock and a stable set of revisions after any wait.
                if not await conn.fetchval(
                    "SELECT id FROM agent_attempts WHERE id = $1 FOR SHARE",
                    target["attempt_id"],
                ):
                    return None
                if not await conn.fetchval(
                    "SELECT id FROM agent_threads WHERE id = $1 FOR SHARE",
                    target["thread_id"],
                ):
                    return None
                if not await conn.fetchval(
                    "SELECT id FROM agent_jobs WHERE id = $1 FOR SHARE",
                    target["job_id"],
                ):
                    return None
                if not await lock_mcp_residency(
                    conn,
                    job_id=target["job_id"],
                    attempt_id=attempt_id,
                    generation=residency_generation,
                ):
                    return None
                if not await conn.fetchval(
                    "SELECT id FROM agent_mcp_connections WHERE id = $1 FOR SHARE",
                    target["connection_id"],
                ):
                    return None
                if not await conn.fetchval(
                    "SELECT connection_id FROM agent_mcp_thread_bindings "
                    "WHERE thread_id = $1 AND connection_id = $2 FOR SHARE",
                    target["thread_id"],
                    target["connection_id"],
                ):
                    return None
            locked_target = await conn.fetchrow(
                """
                SELECT job_id, attempt_id, lease_generation, thread_id, connection_id,
                       residency_generation
                FROM agent_mcp_invocations WHERE id = $1 FOR UPDATE
                """,
                invocation_id,
            )
            if locked_target is None or any(
                locked_target[column] != target[column]
                for column in (
                    "job_id",
                    "attempt_id",
                    "lease_generation",
                    "thread_id",
                    "connection_id",
                    "residency_generation",
                )
            ):
                return None
            row = await conn.fetchrow(
                f"""
                UPDATE agent_mcp_invocations
                SET status = 'dispatching', dispatch_generation = dispatch_generation + 1
                WHERE id = $1 AND status = 'approved' AND expires_at > clock_timestamp(){fence}{live_fence}
                RETURNING *
                """,
                invocation_id,
                *fence_params(
                    config_revision=config_revision,
                    authorization_epoch=authorization_epoch,
                    capability_revision=capability_revision,
                    policy_revision=policy_revision,
                    attempt_id=attempt_id,
                    lease_generation=lease_generation,
                ),
            )
        if row is not None:
            try:
                await self._flush_remembered_approvals(row)
            except asyncio.CancelledError:
                # The caller has not received the committed claim yet, so its
                # normal dispatch cleanup cannot know which record it owns.
                await asyncio.shield(
                    self.finish_mcp_invocation(
                        invocation_id=int(row["id"]),
                        dispatch_generation=int(row["dispatch_generation"]),
                        status="outcome_unknown",
                        result_summary="Dispatcher cancelled after claiming the approval.",
                    )
                )
                raise
        return dict(row) if row is not None else None

    async def _flush_remembered_approvals(self, row: Any) -> None:
        """Attempt a prompt save without losing an already committed dispatch claim."""
        try:
            async with asyncio.timeout(5.0):
                await self.apply_pending_mcp_allow_rules(
                    thread_id=str(row["thread_id"]), connection_id=int(row["connection_id"])
                )
        except Exception:
            logger.warning("agent_mcp_remembered_approval_failed", exc_info=True)

    async def apply_pending_mcp_allow_rules(
        self, *, thread_id: str | None = None, connection_id: int | None = None
    ) -> int:
        """Persist remembered approvals after held calls leave the dispatch queue.

        Intents live with their decisions, surviving client and process restarts.
        The thread lock serializes permission edits; connection and binding locks
        keep their fences stable. Eligible intents for one binding are merged
        before moving its policy revision, preserving other rules and defaults.
        """
        async with self._pool.acquire() as conn:
            groups = await conn.fetch(
                """
                SELECT DISTINCT thread_id, connection_id FROM agent_mcp_invocations
                WHERE always_allow_status = 'pending'
                  AND ($1::text IS NULL OR thread_id = $1)
                  AND ($2::bigint IS NULL OR connection_id = $2)
                ORDER BY thread_id, connection_id
                """,
                thread_id,
                connection_id,
            )
        applied = 0
        for group in groups:
            applied += await self._apply_mcp_allow_group(
                str(group["thread_id"]), int(group["connection_id"])
            )
        return applied

    async def _apply_mcp_allow_group(self, thread_id: str, connection_id: int) -> int:
        """Recheck fingerprints, ownership and revisions before saving one binding."""
        async with self._pool.acquire() as conn, conn.transaction():
            thread = await conn.fetchrow(
                "SELECT * FROM agent_threads WHERE id = $1 FOR UPDATE SKIP LOCKED", thread_id
            )
            if thread is None:
                # Permission edits and other workers may own the conversation.
                # Leave its durable intents for a later pass and service others.
                return 0
            connection = await conn.fetchrow(
                "SELECT * FROM agent_mcp_connections WHERE id = $1 FOR SHARE", connection_id
            )
            binding = await conn.fetchrow(
                "SELECT * FROM agent_mcp_thread_bindings "
                "WHERE thread_id = $1 AND connection_id = $2 FOR UPDATE",
                thread_id,
                connection_id,
            )
            snapshot = await conn.fetchrow(
                "SELECT * FROM agent_mcp_capabilities WHERE connection_id = $1 FOR SHARE",
                connection_id,
            )
            intents = await conn.fetch(
                "SELECT * FROM agent_mcp_invocations "
                "WHERE thread_id = $1 AND connection_id = $2 "
                "AND always_allow_status = 'pending' ORDER BY id FOR UPDATE",
                thread_id,
                connection_id,
            )
            valid = []
            for intent in intents:
                live = (
                    thread is not None
                    and thread["deleted_at"] is None
                    and connection is not None
                    and connection["deleted_at"] is None
                    and connection["enabled"]
                    and binding is not None
                    and binding["enabled"]
                    and thread["user_id"] == connection["user_id"] == intent["decided_by"]
                    and all(
                        connection[key] == intent[key]
                        for key in ("config_revision", "authorization_epoch", "capability_revision")
                    )
                    and binding["policy_revision"] == intent["policy_revision"]
                    and _remembered_rule_matches(intent, snapshot)
                )
                if live:
                    valid.append(intent)
                else:
                    await conn.execute(
                        "UPDATE agent_mcp_invocations SET always_allow_status = 'discarded' "
                        "WHERE id = $1",
                        intent["id"],
                    )
            if not valid or await conn.fetchval(
                "SELECT EXISTS (SELECT 1 FROM agent_mcp_invocations "
                "WHERE thread_id = $1 AND connection_id = $2 "
                "AND status IN ('pending', 'approved') AND expires_at > clock_timestamp())",
                thread_id,
                connection_id,
            ):
                return 0
            assert binding is not None
            merged = mcp_binding_row(binding)
            keys = {
                "tool": "rules",
                "prompt": "prompt_rules",
                "resource": "resource_rules",
                "resource_template": "resource_rules",
            }
            for intent in valid:
                merged[keys[intent["capability_kind"]]][intent["capability_name"]] = _load_json(
                    intent["always_allow_rule"]
                )
            await conn.execute(
                """
                UPDATE agent_mcp_thread_bindings
                SET rules = $3::jsonb, resource_rules = $4::jsonb, prompt_rules = $5::jsonb,
                    policy_revision = nextval('agent_mcp_policy_revision_seq'), updated_at = NOW()
                WHERE thread_id = $1 AND connection_id = $2
                """,
                thread_id,
                connection_id,
                json.dumps(merged["rules"]),
                json.dumps(merged["resource_rules"]),
                json.dumps(merged["prompt_rules"]),
            )
            await conn.execute(
                "UPDATE agent_mcp_invocations SET always_allow_status = 'applied' "
                "WHERE id = ANY($1::bigint[])",
                [intent["id"] for intent in valid],
            )
            return len(valid)

    async def finish_mcp_invocation(
        self,
        *,
        invocation_id: int,
        dispatch_generation: int,
        status: str,
        result_summary: str | None = None,
    ) -> bool:
        """Record the outcome of a dispatched call, fenced by generation."""
        if status not in ("completed", "failed", "outcome_unknown"):
            raise ValueError(f"invalid terminal invocation status: {status!r}")
        async with self._pool.acquire() as conn:
            updated = await conn.fetchval(
                """
                UPDATE agent_mcp_invocations
                SET status = $3, result_summary = $4
                WHERE id = $1 AND status = 'dispatching' AND dispatch_generation = $2
                RETURNING id
                """,
                invocation_id,
                dispatch_generation,
                status,
                result_summary,
            )
        return updated is not None

    async def find_approved_mcp_invocation(
        self,
        *,
        job_id: str,
        connection_id: int,
        capability_name: str,
        arguments_digest: str,
        capability_kind: str = "tool",
        config_revision: int | None = None,
        authorization_epoch: int | None = None,
        capability_revision: int | None = None,
        policy_revision: int | None = None,
        attempt_id: int | None = None,
        lease_generation: int | None = None,
        residency_generation: int | None = None,
    ) -> dict[str, Any] | None:
        """The oldest approval of one call that is still valid for it.

        Matching on the argument digest as well as the name is what makes an
        approval an approval *of something*: a session-wide "allow" is a rule,
        while this is one decision about one call, and reusing it for different
        arguments would turn a single confirmation into a standing grant.

        The fence is part of the lookup, not a check after it. An approval was
        given for a target, a catalogue and a session *as they were*, and the
        owner agreed to that call and nothing else -- so once the connection is
        repointed, the catalogue grows or the job changes hands, the approval
        describes something that no longer exists and must not be found at all.
        """
        fence = _invocation_fence_sql(
            config_revision=config_revision,
            authorization_epoch=authorization_epoch,
            capability_revision=capability_revision,
            policy_revision=policy_revision,
            attempt_id=attempt_id,
            lease_generation=lease_generation,
            first_index=7,
        )
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow(
                f"""
                SELECT * FROM agent_mcp_invocations
                WHERE job_id = $1 AND connection_id = $2 AND capability_name = $3
                  AND arguments_digest = $4 AND capability_kind = $5
                  AND residency_generation IS NOT DISTINCT FROM $6::bigint
                  AND status = 'approved' AND expires_at > clock_timestamp(){fence}
                ORDER BY id
                LIMIT 1
                """,
                job_id,
                connection_id,
                capability_name,
                arguments_digest,
                capability_kind,
                residency_generation,
                *fence_params(
                    config_revision=config_revision,
                    authorization_epoch=authorization_epoch,
                    capability_revision=capability_revision,
                    policy_revision=policy_revision,
                    attempt_id=attempt_id,
                    lease_generation=lease_generation,
                ),
            )
        return dict(row) if row is not None else None

    async def expire_mcp_invocations(self) -> int:
        """Expire every pending or approved call whose deadline has passed."""
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(
                f"""
                WITH expired AS MATERIALIZED (
                    SELECT id FROM agent_mcp_invocations
                    WHERE status IN ('pending', 'approved')
                      AND expires_at <= clock_timestamp()
                    FOR UPDATE SKIP LOCKED
                )
                UPDATE agent_mcp_invocations AS calls SET {self._EXPIRED}
                FROM expired WHERE calls.id = expired.id RETURNING calls.id
                """
            )
        return len(rows)

    async def open_mcp_transport_session(
        self,
        *,
        handle: str,
        connection_id: int,
        member: str,
        job_id: str | None,
        attempt_id: int | None,
        owner_lease_generation: int,
        protocol_version: str,
        upstream_session_id: str | None,
        config_revision: int,
        authorization_epoch: int,
    ) -> None:
        """Record one opened session under its opaque handle."""
        async with self._pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO agent_mcp_transport_sessions
                    (handle, connection_id, member, job_id, attempt_id,
                     owner_lease_generation, protocol_version, upstream_session_id,
                     config_revision, authorization_epoch)
                VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10)
                ON CONFLICT (handle) DO UPDATE SET
                    owner_lease_generation = EXCLUDED.owner_lease_generation,
                    protocol_version = EXCLUDED.protocol_version,
                    upstream_session_id = EXCLUDED.upstream_session_id,
                    config_revision = EXCLUDED.config_revision,
                    authorization_epoch = EXCLUDED.authorization_epoch,
                    last_used_at = NOW()
                """,
                handle,
                connection_id,
                member,
                job_id,
                attempt_id,
                owner_lease_generation,
                protocol_version,
                upstream_session_id,
                config_revision,
                authorization_epoch,
            )

    async def close_mcp_transport_session(self, *, handle: str) -> bool:
        """Forget one session. ``False`` when it was already gone."""
        async with self._pool.acquire() as conn:
            deleted = await conn.fetchval(
                "DELETE FROM agent_mcp_transport_sessions WHERE handle = $1 RETURNING handle",
                handle,
            )
        return deleted is not None

    async def reap_mcp_transport_sessions(
        self, *, connection_id: int, member: str, keep_handle: str
    ) -> list[dict[str, Any]]:
        """Delete every other session this member holds on this connection.

        The design's "explicitly rebuild the protocol session when the owner is
        superseded" is this: a takeover produces a new key, and the old session
        -- a socket the platform will never drive again -- is closed and its
        record removed rather than left for the reaper to find. Returned rather
        than counted so the caller can close the transports it still holds.
        """
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(
                """
                DELETE FROM agent_mcp_transport_sessions
                WHERE connection_id = $1 AND member = $2 AND handle <> $3
                RETURNING handle, owner_lease_generation
                """,
                connection_id,
                member,
                keep_handle,
            )
        return [dict(row) for row in rows]

    async def list_mcp_transport_sessions(self, *, connection_id: int) -> list[dict[str, Any]]:
        """Every recorded session for one connection, newest first."""
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT handle, member, job_id, attempt_id, owner_lease_generation,
                       protocol_version, config_revision, authorization_epoch,
                       created_at, last_used_at
                FROM agent_mcp_transport_sessions
                WHERE connection_id = $1
                ORDER BY last_used_at DESC, handle
                """,
                connection_id,
            )
        return [dict(row) for row in rows]

    async def touch_mcp_transport_session(self, *, handle: str) -> bool:
        """Move a session's last-used timestamp, so a diagnosis can see it live."""
        async with self._pool.acquire() as conn:
            updated = await conn.fetchval(
                "UPDATE agent_mcp_transport_sessions SET last_used_at = NOW() "
                "WHERE handle = $1 RETURNING handle",
                handle,
            )
        return updated is not None

    async def create_mcp_connector(
        self, *, user_id: str, name: str, registration_hash: str
    ) -> dict[str, Any]:
        """Register a user-run connector in the pending state."""
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                INSERT INTO agent_mcp_connectors (user_id, name, registration_hash)
                VALUES ($1, $2, $3)
                RETURNING id, user_id, name, status, mounts, created_at, last_seen_at
                """,
                user_id,
                name,
                registration_hash,
            )
        return dict(row)

    async def list_mcp_connectors(self, *, user_id: str) -> list[dict[str, Any]]:
        """Every connector this user registered."""
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT id, user_id, name, status, mounts, created_at, last_seen_at, revoked_at
                FROM agent_mcp_connectors WHERE user_id = $1 ORDER BY id
                """,
                user_id,
            )
        return [dict(row) for row in rows]

    async def set_mcp_connector_status(self, *, connector_id: int, status: str) -> bool:
        """Record a non-terminal heartbeat status; revocation uses the atomic path."""
        if status not in ("pending", "online", "offline"):
            raise ValueError(f"invalid connector status: {status!r}")
        async with self._pool.acquire() as conn:
            updated = await conn.fetchval(
                """
                UPDATE agent_mcp_connectors
                SET status = $2,
                    last_seen_at = CASE WHEN $2 = 'online' THEN NOW() ELSE last_seen_at END
                WHERE id = $1 AND status <> 'revoked'
                RETURNING id
                """,
                connector_id,
                status,
            )
        return updated is not None

    async def update_mcp_connector(
        self,
        *,
        connector_id: int,
        user_id: str,
        name: str | None = None,
        mounts: list[str] | None = None,
    ) -> dict[str, Any] | None:
        """Rename a connector or change the resources it may reach.

        Neither edit moves the connections' versions on its own: ``mounts`` is
        what the connector is allowed to see on the user's machine, and widening
        it is a decision the connector makes locally, while the platform's
        record of it is advisory. Binding a *connection* to the connector is the
        security-relevant edit, and that is where the versions move.
        """
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                UPDATE agent_mcp_connectors
                SET name = COALESCE($3, name),
                    mounts = COALESCE($4, mounts)
                WHERE id = $1 AND user_id = $2 AND status <> 'revoked'
                RETURNING id, user_id, name, status, mounts, created_at, last_seen_at, revoked_at
                """,
                connector_id,
                user_id,
                name,
                list(mounts) if mounts is not None else None,
            )
        return dict(row) if row is not None else None

    async def bind_mcp_connection_connector(
        self,
        *,
        connection_id: int,
        user_id: str,
        connector_id: int,
    ) -> dict[str, Any] | None:
        """Route one connection through one connector, moving both versions.

        ``connector`` is on the design's list of security-relevant configuration:
        it decides *where* a local process runs, so choosing it is as much a
        change of target as editing the command. Both revisions move, which
        invalidates every session binding approved against the previous answer,
        and the connector's authorized set is written in the same transaction so
        the two cannot disagree about what it may serve. Every previous
        authorization row for the connection is removed in that transaction
        before the new pair is inserted.
        """
        async with self._pool.acquire() as conn, conn.transaction():
            owned_connector = await conn.fetchval(
                """
                SELECT id FROM agent_mcp_connectors
                WHERE id = $1 AND user_id = $2 AND status <> 'revoked'
                FOR SHARE
                """,
                connector_id,
                user_id,
            )
            if owned_connector is None:
                return None
            row = await conn.fetchrow(
                f"""
                UPDATE agent_mcp_connections c
                SET connector_id = $3,
                    config_revision = c.config_revision + 1,
                    authorization_epoch = c.authorization_epoch + 1,
                    updated_at = NOW()
                WHERE c.id = $1 AND c.user_id = $2 AND c.deleted_at IS NULL
                  AND c.location = 'user-connector'
                RETURNING {self._MCP_CONNECTION_COLUMNS}
                """,
                connection_id,
                user_id,
                connector_id,
            )
            if row is None:
                return None
            await conn.execute(
                "DELETE FROM agent_mcp_connector_connections WHERE connection_id = $1",
                connection_id,
            )
            await conn.execute(
                """
                INSERT INTO agent_mcp_connector_connections (connector_id, connection_id)
                VALUES ($1, $2) ON CONFLICT DO NOTHING
                """,
                connector_id,
                connection_id,
            )
        return self._mcp_connection(row)

    async def unbind_mcp_connection_connector(
        self, *, connection_id: int, user_id: str, connector_id: int
    ) -> dict[str, Any] | None:
        """Stop routing one connection through the connector the caller named.

        ``connector_id`` is matched in the same statement that clears the
        route. A request that names connector A must not detach a connection
        currently bound to B, and only that pair's authorization row is removed.
        The same version movement as binding applies when the pair matches: a
        connection that has moved back to "nowhere to run" is not the one a
        session was approved against.
        """
        async with self._pool.acquire() as conn, conn.transaction():
            row = await conn.fetchrow(
                f"""
                UPDATE agent_mcp_connections c
                SET connector_id = NULL,
                    config_revision = c.config_revision + 1,
                    authorization_epoch = c.authorization_epoch + 1,
                    updated_at = NOW()
                WHERE c.id = $1 AND c.user_id = $2 AND c.connector_id = $3
                RETURNING {self._MCP_CONNECTION_COLUMNS}
                """,
                connection_id,
                user_id,
                connector_id,
            )
            if row is None:
                return None
            await conn.execute(
                """
                DELETE FROM agent_mcp_connector_connections
                WHERE connector_id = $1 AND connection_id = $2
                """,
                connector_id,
                connection_id,
            )
        return self._mcp_connection(row)

    async def revoke_mcp_connector(self, *, connector_id: int, user_id: str) -> bool:
        """Revoke a connector and detach everything that routed through it.

        One transaction, because the two halves are the same decision: a
        connector marked revoked while its connections still name it would leave
        jobs resolving to a target the owner has withdrawn, and each detached
        connection has its versions moved so the sessions approved against it
        stop working rather than quietly pointing somewhere else.
        """
        async with self._pool.acquire() as conn, conn.transaction():
            revoked = await conn.fetchval(
                """
                UPDATE agent_mcp_connectors
                SET status = 'revoked', revoked_at = NOW()
                WHERE id = $1 AND user_id = $2 AND status <> 'revoked'
                RETURNING id
                """,
                connector_id,
                user_id,
            )
            if revoked is None:
                return False
            await conn.execute(
                """
                UPDATE agent_mcp_connections
                SET connector_id = NULL,
                    config_revision = config_revision + 1,
                    authorization_epoch = authorization_epoch + 1,
                    updated_at = NOW()
                WHERE connector_id = $1
                """,
                connector_id,
            )
            await conn.execute(
                "DELETE FROM agent_mcp_connector_connections WHERE connector_id = $1",
                connector_id,
            )
        return True

    async def list_mcp_connector_connections(self, *, connector_id: int) -> list[int]:
        """The connection ids one connector may serve."""
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT connection_id FROM agent_mcp_connector_connections WHERE connector_id = $1",
                connector_id,
            )
        return [int(row["connection_id"]) for row in rows]

    async def set_mcp_launch_spec(
        self,
        *,
        connection_id: int,
        expected_authorization_epoch: int,
        launch_spec: dict[str, Any],
    ) -> bool:
        """Store encrypted launch secrets only while the connection identity is current."""
        async with self._pool.acquire() as conn:
            updated = await conn.fetchval(
                """
                UPDATE agent_mcp_connections SET launch_spec = $3::jsonb, updated_at = NOW()
                WHERE id = $1 AND authorization_epoch = $2 AND deleted_at IS NULL
                RETURNING id
                """,
                connection_id,
                expected_authorization_epoch,
                json.dumps(launch_spec),
            )
        return updated is not None

    async def expire_mcp_invocation(self, *, invocation_id: int) -> bool:
        """Expire one undispatched call when its waiting client or authorization is gone."""
        async with self._pool.acquire() as conn:
            updated = await conn.fetchval(
                f"UPDATE agent_mcp_invocations SET {self._EXPIRED} "
                "WHERE id = $1 AND status IN ('pending', 'approved') RETURNING id",
                invocation_id,
            )
        return updated is not None
