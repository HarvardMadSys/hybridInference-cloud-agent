"""Which identity source this process serves, bound before anything else runs.

Gateway mode (the default) is unchanged: the database is pinned to the
configured Gateway issuer and the Gateway clients are built by the lifespan.
Local mode (``AGENT_AUTH_MODE=local``) checks its secret, binds or checks the
local namespace, and replaces both Gateway clients with local stand-ins that
never make a network call; see :mod:`cloud_agent.accounts.directory`.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Coroutine

    import asyncpg
    from fastapi import FastAPI

    from cloud_agent.accounts.config import AuthMode
    from cloud_agent.storage.assembly import Storage


async def bind_identity_source(app: FastAPI, pool: asyncpg.Pool) -> AuthMode:
    """Refuse a database the configured identity source cannot own; record the mode.

    Raises:
        LocalAuthNotConfigured: Local mode without a usable secret or
            credential encryption key, or an unknown ``AGENT_AUTH_MODE``.
        IdentityNamespaceError: The database belongs to the other source.
    """
    from cloud_agent.accounts.config import auth_mode, check_local_configuration
    from cloud_agent.identity_namespace import ensure_gateway_namespace, ensure_local_namespace
    from cloud_agent.settings import setting_env

    mode = auth_mode()
    app.state.auth_mode = mode
    if mode == "local":
        check_local_configuration()
        _check_local_inference()
        await ensure_local_namespace(pool)
        await _announce_setup(pool)
        app.state.gateway_issuer = None
        return mode
    # An empty deployment must remain reachable through recovery-token setup.
    # A recorded foreign namespace still refuses startup before workers run.
    issuer = (setting_env("GATEWAY_ISSUER") or "").strip() or None
    app.state.gateway_issuer = await ensure_gateway_namespace(pool, issuer)
    return mode


async def _announce_setup(pool: asyncpg.Pool) -> None:
    """Log the first-run setup code while no local administrator exists.

    Never stops startup: an operator can still bootstrap from the command line.
    """
    from cloud_agent.accounts.setup import announce_setup_code
    from cloud_agent.logging_utils import get_logger
    from cloud_agent.storage.assembly import Storage

    try:
        await announce_setup_code(Storage(pool).local_accounts)
    except Exception:
        get_logger(__name__).warning(
            "agent_local_setup_unavailable",
            exc_info=True,
            extra={"event": "agent_local_setup_unavailable"},
        )


def _check_local_inference() -> None:
    """Refuse local mode where owners could not store a model connection.

    Local accounts have no Gateway inference: their own connections are the
    only way they run anything, and the connection keys are stored encrypted
    under this deployment's credential key. Without it every task would fail.

    Raises:
        LocalAuthNotConfigured: The credential encryption key is unusable.
    """
    from cloud_agent.accounts.config import LocalAuthNotConfigured
    from cloud_agent.application.model_connections import model_connection_cipher
    from cloud_agent.credential_cipher import CredentialCipherError

    try:
        model_connection_cipher()
    except CredentialCipherError:
        raise LocalAuthNotConfigured(
            "AGENT_SOURCE_CONTROL_ENCRYPTION_KEY must be set in local mode: "
            "personal model connection keys are stored encrypted under it"
        ) from None


def attach_local_identity(app: FastAPI, storage: Storage, *, local: bool) -> None:
    """In local mode, answer identity and admission from this database; mint no Gateway grant."""
    if not local:
        return
    from cloud_agent.accounts.directory import (
        LocalModeGateway,
        LocalModeGrantClient,
        LocalOwnerDirectory,
    )

    app.state.gateway = LocalModeGateway(LocalOwnerDirectory(storage.local_accounts))
    app.state.grants = LocalModeGrantClient()


def local_identity_tasks(
    storage: Storage, grants: Any, *, local: bool
) -> list[tuple[str, Coroutine[Any, Any, None]]]:
    """The background loops local mode adds: keeping ended access ended, and account mail."""
    if not local:
        return []
    from cloud_agent.accounts.delivery import DeliveryMailer
    from cloud_agent.workers.account_access import run_account_access_enforcement
    from cloud_agent.workers.account_broadcasts import run_account_broadcasts

    return [
        ("account_access_task", run_account_access_enforcement(storage, grants)),
        ("account_broadcast_task", run_account_broadcasts(storage, DeliveryMailer())),
    ]


__all__ = ["attach_local_identity", "bind_identity_source", "local_identity_tasks"]
