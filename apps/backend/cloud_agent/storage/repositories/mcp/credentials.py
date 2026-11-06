"""A connection's credentials, client registration, token refresh and OAuth flows."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

from cloud_agent.storage.repositories.mcp._common import McpRepositoryBase

if TYPE_CHECKING:
    from collections.abc import Mapping


class CredentialsMixin(McpRepositoryBase):
    """Store credentials, refresh tokens and run OAuth flows."""

    @staticmethod
    async def _lock_oauth_flow(conn: Any, state_hash: str) -> bool:
        return bool(
            await conn.fetchval(
                "SELECT 1 FROM agent_mcp_oauth_flows WHERE state_hash = $1 FOR UPDATE",
                state_hash,
            )
        )

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
