"""Accounts, sessions and model connections for server tests without a database.

Owner routes admit a request on an account session and the account's current
permissions, and every turn runs on one of the owner's model connections. A
server test over an in-memory store installs these three pieces:

- :class:`FakeOwners`, the account directory (``app.state.owners``), whose
  answers a test sets per account: permissions, an unavailable subject, or an
  outage;
- :class:`SessionAccounts`, the part of the account store a session lookup
  reads, so a real ``cals1.`` bearer or cookie authenticates through the real
  dependencies;
- :class:`FakeConnections`, the part of the connection store admission reads,
  holding one verified connection per owner by default.
"""

from __future__ import annotations

import contextlib
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from cloud_agent.accounts import tokens
from cloud_agent.accounts.directory import OwnerDirectoryError
from cloud_agent.agent_access import AgentAccessDecision
from cloud_agent.application.model_connections import model_connection_cipher
from cloud_agent.credential_cipher import CredentialCipherError
from cloud_agent.domain.accounts import AccountRecord
from cloud_agent.ports.accounts import SessionOwner
from cloud_agent_shared.model_connections import (
    MODEL_CONNECTION_CAPABILITY,
    PROTOCOLS,
    ModelConnectionAccess,
    runtime_protocol,
)

#: The account key session hashes are derived under in these tests.
SESSION_SECRET = "owner-session-secret-for-tests-0123456789"

#: The connection every owner has unless a test removes it.
CONNECTION_ID = "mconn_" + "0" * 32
#: The model that connection lists and has verified for every Agent.
CONNECTION_MODEL = "provider-model"
#: Everything that connection lists, each verified on every API: the model ids
#: server tests have always submitted, so a case about something else names a
#: model the owner's connection serves.
CONNECTION_MODELS = (CONNECTION_MODEL, "glm-5.1", "qwen-next", "m")
#: The connection's provider key: plainly a test value.
TEST_PROVIDER_KEY = "sk-test-owner-provider-key-0001"


def use_session_secret(monkeypatch: Any) -> None:
    """Make account session hashing usable in this test."""
    monkeypatch.setenv("AGENT_LOCAL_SESSION_SECRET", SESSION_SECRET)


def account_record(
    user_id: str,
    *,
    admin: bool = False,
    approved: bool = True,
    status: str = "active",
    email: str | None = None,
) -> AccountRecord:
    """An account in the state a test names: verified, and approved unless told otherwise."""
    return AccountRecord(
        id=user_id,
        email=email if email is not None else f"{user_id}@example.org",
        email_verified=True,
        status=status,  # type: ignore[arg-type]
        is_admin=admin,
        version=1,
        created_at=datetime(2026, 1, 1, tzinfo=UTC),
        admission_status="approved" if approved else "pending",  # type: ignore[arg-type]
        purpose="",
        review_reason=None,
        submitted_at=None,
        reviewed_at=None,
        identity_verified=True,
        email_proven=True,
    )


@dataclass
class FakeOwners:
    """The account directory, answering from what a test set.

    ``permissions`` maps an account to what it holds; an account absent from
    it holds ``agent.use``. ``unavailable`` accounts are refused as a subject
    that cannot sign in, and ``outage`` makes every read fail as an unreadable
    store does.
    """

    permissions: dict[str, list[str]] = field(default_factory=dict)
    unavailable: set[str] = field(default_factory=set)
    emails: dict[str, str] = field(default_factory=dict)
    outage: bool = False
    asked: list[str] = field(default_factory=list)

    def grant(self, user_id: str, *permissions: str) -> None:
        """Set exactly what ``user_id`` holds now."""
        self.permissions[user_id] = list(permissions)

    def _check(self, user_id: str) -> None:
        self.asked.append(user_id)
        if self.outage:
            raise OwnerDirectoryError("account store unavailable")
        if user_id in self.unavailable:
            raise OwnerDirectoryError(
                "account unavailable", status_code=403, error_type="subject_unavailable"
            )

    async def agent_access(self, user_id: str) -> AgentAccessDecision:
        """The account's current permissions."""
        self._check(user_id)
        held = self.permissions.get(user_id, ["agent.use"])
        return AgentAccessDecision(user_id=user_id, allowed=bool(held), permissions=held)

    async def user_email(self, user_id: str) -> str:
        """The account's email."""
        self._check(user_id)
        return self.emails.get(user_id, f"{user_id}@example.org")


class SessionAccounts:
    """The account-store reads a session lookup makes, in memory.

    :meth:`sign_in` returns a bearer of the session's real shape; the real
    ``get_current_user`` hashes it under the test secret and finds the account
    here. ``browser_state`` answers ``None``, so the token is read as a single
    account's session.
    """

    def __init__(self) -> None:
        """Start with no account and no session."""
        self.accounts: dict[str, AccountRecord] = {}
        self._sessions: dict[bytes, str] = {}

    def add(self, user_id: str, **record: Any) -> AccountRecord:
        """Create (or replace) ``user_id``'s account without signing it in."""
        self.accounts[user_id] = account_record(user_id, **record)
        return self.accounts[user_id]

    def sign_in(self, user_id: str, **record: Any) -> str:
        """Create (or replace) ``user_id``'s account and return a live session for it."""
        self.accounts[user_id] = account_record(user_id, **record)
        token = tokens.new_session_token()
        self._sessions[tokens.session_hash(token)] = user_id
        return token

    def revoke_all(self) -> None:
        """End every session, as a sign-out or a password change does."""
        self._sessions.clear()

    async def browser_state(self, _browser_hash: bytes) -> None:
        """No browser holds these sessions."""
        return None

    async def session_owner(self, session_hash: bytes) -> SessionOwner | None:
        """The live session ``session_hash`` names, with its account as it is now."""
        user_id = self._sessions.get(session_hash)
        if user_id is None or user_id not in self.accounts:
            return None
        return SessionOwner(session_id="sess-" + user_id, account=self.accounts[user_id])

    async def get_account(self, user_id: str) -> AccountRecord | None:
        """The account as it is now."""
        return self.accounts.get(user_id)


#: One in-memory account store per test process, for tests whose fake storage
#: is assembled inline: they add ``local_accounts=SESSIONS`` and sign in with
#: :meth:`SessionAccounts.sign_in`. Tokens are random, so tests never share one.
SESSIONS = SessionAccounts()


def bearer(user_id: str, **record: Any) -> dict[str, str]:
    """An ``Authorization`` header carrying a live session for ``user_id``."""
    return {"Authorization": f"Bearer {SESSIONS.sign_in(user_id, **record)}"}


def connection_row(
    user_id: str,
    *,
    connection_id: str = CONNECTION_ID,
    models: tuple[str, ...] = CONNECTION_MODELS,
    enabled: bool = True,
) -> dict[str, Any]:
    """A connection whose every model is verified on every API, at revision 1."""
    supported = {"protocols": {protocol: {"status": "supported"} for protocol in PROTOCOLS}}
    row: dict[str, Any] = {
        "id": connection_id,
        "user_id": user_id,
        "name": "My provider",
        "base_url": "https://provider.example/v1",
        "models": list(models),
        "enabled": enabled,
        "deleted_at": None,
        "config_revision": 1,
        "credential_revision": 1,
        "model_checks": {model: {**supported, "revision": 1} for model in models},
    }
    # Without a connection key in this test, admission still reads the row;
    # only a claim, which hands the key over, would need it.
    with contextlib.suppress(CredentialCipherError):
        row["api_key_ciphertext"] = model_connection_cipher().encrypt(TEST_PROVIDER_KEY)
    return row


class FakeConnections:
    """The connection-store reads admission and the composer make, in memory.

    Every owner has :data:`CONNECTION_ID` unless a test removes it with
    :meth:`remove` or replaces it with :meth:`put`.
    """

    def __init__(self) -> None:
        """No connection is overridden yet."""
        self.rows: dict[tuple[str, str], dict[str, Any] | None] = {}

    def put(self, row: dict[str, Any]) -> None:
        """Store ``row`` as its owner's connection."""
        self.rows[(row["user_id"], row["id"])] = row

    def remove(self, user_id: str, connection_id: str = CONNECTION_ID) -> None:
        """``user_id`` no longer has that connection."""
        self.rows[(user_id, connection_id)] = None

    async def get_model_connection(
        self, *, user_id: str, connection_id: str
    ) -> dict[str, Any] | None:
        """The owner's connection, scoped to them."""
        key = (user_id, connection_id)
        if key in self.rows:
            return self.rows[key]
        return connection_row(user_id) if connection_id == CONNECTION_ID else None

    async def list_model_connections(self, *, user_id: str) -> list[dict[str, Any]]:
        """The owner's live connections."""
        listed = [
            row
            for (owner, _), row in self.rows.items()
            if owner == user_id and row is not None and row.get("deleted_at") is None
        ]
        if (user_id, CONNECTION_ID) not in self.rows:
            listed.insert(0, connection_row(user_id))
        return listed


def on_connection(body: dict[str, Any]) -> dict[str, Any]:
    """A job or message body that names the owner's verified connection.

    The body's model is kept, so a case about the model still tests it; a body
    naming none gets the connection's own.
    """
    return {"model": CONNECTION_MODEL, "model_connection_id": CONNECTION_ID, **body}


def connection_access(runtime: str = "claude-code", **changes: Any) -> ModelConnectionAccess:
    """What a claim hands a host for a turn on the owner's connection, speaking ``runtime``'s API."""
    fields: dict[str, Any] = {
        "connection_id": CONNECTION_ID,
        "revision": 1,
        "base_url": "https://provider.example/v1",
        "protocol": runtime_protocol(runtime) or "chat_completions",
        "api_key": TEST_PROVIDER_KEY,
    }
    return ModelConnectionAccess(**{**fields, **changes})


def launch_connection(runtime: str = "claude-code") -> dict[str, str]:
    """A member launch's connection fields, as ``model_connection_launch`` gives them."""
    return connection_access(runtime).launch_fields()


def residency_connection(runtime: str = "claude-code") -> dict[str, Any]:
    """What a residency answer carries for a turn on :func:`connection_access`."""
    access = connection_access(runtime)
    return {
        "model_connection_revision": access.revision,
        "model_connection_plan": access.plan.identity,
    }


def connection_claim(body: dict[str, Any]) -> dict[str, Any]:
    """A claim request from a host that can launch members on owner connections.

    Every turn runs on one now, so a claim from a host that cannot is handed
    back; a test about something else claims as a capable host.
    """
    attestation = dict(body.get("attestation") or {})
    capabilities = list(attestation.get("runner_capabilities") or [])
    if MODEL_CONNECTION_CAPABILITY not in capabilities:
        capabilities.append(MODEL_CONNECTION_CAPABILITY)
    return {**body, "attestation": {**attestation, "runner_capabilities": capabilities}}


__all__ = [
    "CONNECTION_ID",
    "CONNECTION_MODEL",
    "CONNECTION_MODELS",
    "SESSIONS",
    "SESSION_SECRET",
    "TEST_PROVIDER_KEY",
    "FakeConnections",
    "FakeOwners",
    "SessionAccounts",
    "account_record",
    "bearer",
    "connection_access",
    "connection_claim",
    "connection_row",
    "launch_connection",
    "on_connection",
    "residency_connection",
    "use_session_secret",
]
