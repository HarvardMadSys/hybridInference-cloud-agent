"""Port for a user's MCP connections, selections, and execution state."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Protocol

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence


class McpConnectionRepository(Protocol):
    """Persistence operations needed by the MCP control plane."""

    async def list_mcp_connections(
        self, *, user_id: str, include_deleted: bool = False
    ) -> list[dict[str, Any]]:
        """Every connection this user owns, newest label order stable."""
        ...

    async def get_mcp_connection(
        self, *, connection_id: int, user_id: str | None = None
    ) -> dict[str, Any] | None:
        """One connection by immutable id, optionally checked against its owner."""
        ...

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
        ``McpCredentialWriteRejected`` and leaves no row.
        """
        ...

    async def create_mcp_connections(
        self, *, connections: Sequence[Mapping[str, Any]]
    ) -> list[dict[str, Any]]:
        """Insert every draft, with its credentials, in one transaction.

        One rejected credential write or a later insert failure rolls the whole
        batch back. Callers encrypt before this call, so a missing key never
        reaches it.
        """
        ...

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
        OAuth flows in the same transaction as the edit.
        """
        ...

    async def disconnect_mcp_connection(self, *, connection_id: int, user_id: str) -> bool:
        """Soft-disconnect one connection: tombstone it and bump the epoch.

        The row is kept so that the bindings pointing at it can say *this was
        disconnected* rather than *this never existed*, and so a session that
        was authorized against it fails the epoch check instead of quietly
        resolving to nothing. The stored secret is cleared in the same
        statement, because a tombstone that still holds a token is not a
        disconnect.
        """
        ...

    async def set_mcp_connection_enabled(
        self, *, connection_id: int, user_id: str, enabled: bool
    ) -> dict[str, Any] | None:
        """Toggle account-wide availability without deleting credentials or tool choices."""
        ...

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
        ...

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
        """
        ...

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
        """
        ...

    async def claim_mcp_refresh(
        self, *, connection_id: int, expected_credential_version: int, lease_seconds: float
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
        ...

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
        ...

    async def get_mcp_capabilities(self, *, connection_id: int) -> dict[str, Any] | None:
        """The stored capability snapshot for one connection."""
        ...

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
        ...

    async def delete_mcp_capabilities(self, *, connection_id: int) -> None:
        """Drop a snapshot, used when discovery proves the target changed."""
        ...

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
        ...

    async def consume_mcp_oauth_flow(
        self, *, state_hash: str, user_id: str
    ) -> dict[str, Any] | None:
        """Claim one of ``user_id``'s flows exactly once, returning it, or ``None``.

        The ``consumed_at IS NULL`` predicate is inside the ``UPDATE``, so two
        callbacks racing on the same state cannot both proceed: the second
        matches no row. Marking it consumed before the token exchange is
        deliberate -- a callback that fails after this point must be restarted
        by the user, which is preferable to a state value that can be replayed.

        The owner is part of the match: a flow presented by a browser signed in
        as another account matches nothing and is left for its owner.
        """
        ...

    async def delete_mcp_oauth_flows(self, *, connection_id: int) -> int:
        """Drop every unconsumed flow for one connection."""
        ...

    async def list_mcp_bindings(self, *, thread_id: str) -> list[dict[str, Any]]:
        """Ensure default bindings for the owner's servers and return all overrides."""
        ...

    async def replace_mcp_bindings(
        self, *, thread_id: str, bindings: list[dict[str, Any]], merge: bool = False
    ) -> None:
        """Replace a conversation's selection wholesale.

        With merge=True only named servers change. Otherwise unmentioned
        existing bindings become disabled opt-outs instead of disappearing.
        """
        ...

    async def bump_mcp_binding_policy_revision(self, *, thread_id: str, connection_id: int) -> int:
        """Move a binding's policy revision, invalidating held approvals."""
        ...

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
    ) -> dict[str, Any]:
        """Record a call that is waiting for its owner."""
        ...

    async def get_mcp_invocation(self, *, invocation_id: int) -> dict[str, Any] | None:
        """One invocation by id."""
        ...

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
        ...

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
        ...

    async def apply_pending_mcp_allow_rules(
        self, *, thread_id: str | None = None, connection_id: int | None = None
    ) -> int:
        """Save queued Always allow decisions once their held calls have advanced."""
        ...

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
    ) -> dict[str, Any] | None:
        """Take exclusive ownership of dispatching one approved call.

        The single ``UPDATE ... WHERE status = 'approved'`` is the whole
        mutual-exclusion mechanism. Two workers may both arrive; one matches the
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
        ...

    async def finish_mcp_invocation(
        self,
        *,
        invocation_id: int,
        dispatch_generation: int,
        status: str,
        result_summary: str | None = None,
    ) -> bool:
        """Record the outcome of a dispatched call, fenced by generation."""
        ...

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
        ...

    async def expire_mcp_invocations(self) -> int:
        """Expire every pending or approved call whose deadline has passed."""
        ...

    async def cancel_mcp_invocations_for_attempt(self, *, attempt_id: int) -> int:
        """Expire a superseded attempt's undecided calls."""
        ...

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
        ...

    async def close_mcp_transport_session(self, *, handle: str) -> bool:
        """Forget one session. ``False`` when it was already gone."""
        ...

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
        ...

    async def list_mcp_transport_sessions(self, *, connection_id: int) -> list[dict[str, Any]]:
        """Every recorded session for one connection, newest first."""
        ...

    async def touch_mcp_transport_session(self, *, handle: str) -> bool:
        """Move a session's last-used timestamp, so a diagnosis can see it live."""
        ...

    async def create_mcp_connector(
        self, *, user_id: str, name: str, registration_hash: str
    ) -> dict[str, Any]:
        """Register a user-run connector in the pending state."""
        ...

    async def list_mcp_connectors(self, *, user_id: str) -> list[dict[str, Any]]:
        """Every connector this user registered."""
        ...

    async def set_mcp_connector_status(self, *, connector_id: int, status: str) -> bool:
        """Record a heartbeat or a status change for one connector."""
        ...

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
        ...

    async def bind_mcp_connection_connector(
        self, *, connection_id: int, user_id: str, connector_id: int
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
        ...

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
        ...

    async def revoke_mcp_connector(self, *, connector_id: int, user_id: str) -> bool:
        """Revoke a connector and detach everything that routed through it.

        One transaction, because the two halves are the same decision: a
        connector marked revoked while its connections still name it would leave
        jobs resolving to a target the owner has withdrawn, and each detached
        connection has its versions moved so the sessions approved against it
        stop working rather than quietly pointing somewhere else.
        """
        ...

    async def list_mcp_connector_connections(self, *, connector_id: int) -> list[int]:
        """The connection ids one connector may serve."""
        ...

    async def set_mcp_launch_spec(
        self,
        *,
        connection_id: int,
        expected_authorization_epoch: int,
        launch_spec: dict[str, Any],
    ) -> bool:
        """Store encrypted launch secrets only while the connection identity is current."""
        ...

    async def expire_mcp_invocation(self, *, invocation_id: int) -> bool:
        """Expire one undispatched call when its waiting client or authorization is gone."""
        ...
