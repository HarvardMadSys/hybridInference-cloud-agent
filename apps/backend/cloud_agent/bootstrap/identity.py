"""The account system, checked and bound before anything else runs.

Startup refuses retired Gateway configuration, checks the account secret and
the credential key model connections are stored under, then binds or checks
the database's account namespace. The lifespan then installs the account
directory every owner check reads; see :mod:`cloud_agent.accounts.directory`.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Coroutine

    import asyncpg
    from fastapi import FastAPI

    from cloud_agent.storage.assembly import Storage


async def bind_identity_source(pool: asyncpg.Pool) -> None:
    """Refuse a configuration or database the account system cannot serve.

    Raises:
        RetiredConfiguration: A Gateway-era variable is still set.
        LocalAuthNotConfigured: No usable account secret or credential
            encryption key.
        IdentityNamespaceError: The database's owners are not accounts.
    """
    from cloud_agent.accounts.config import (
        check_local_configuration,
        refuse_retired_configuration,
    )
    from cloud_agent.identity_namespace import ensure_local_namespace

    refuse_retired_configuration()
    check_local_configuration()
    _check_model_connection_key()
    await ensure_local_namespace(pool)
    await _announce_setup(pool)


async def _announce_setup(pool: asyncpg.Pool) -> None:
    """Log the first-run setup code while no administrator exists.

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


def _check_model_connection_key() -> None:
    """Refuse to start where owners could not store a model connection.

    Owners' own connections are the only way they run anything, and the
    connection keys are stored encrypted under this deployment's credential
    key. Without it every task would fail.

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
            "AGENT_SOURCE_CONTROL_ENCRYPTION_KEY must be set: "
            "personal model connection keys are stored encrypted under it"
        ) from None


def attach_owner_directory(app: FastAPI, storage: Storage) -> None:
    """Answer identity and admission from this database."""
    from cloud_agent.accounts.directory import LocalOwnerDirectory

    app.state.owners = LocalOwnerDirectory(storage.local_accounts)


def account_tasks(
    storage: Storage, *, mailer: Any = None
) -> list[tuple[str, Coroutine[Any, Any, None]]]:
    """The account system's background loops: keeping ended access ended, and account mail.

    Administrator mail goes through ``mailer``, the application's account
    mailer (``app.state.account_mailer``) when one is installed, so it leaves
    by the same transport as every other account message; otherwise the
    transport the settings select.
    """
    from cloud_agent.accounts.delivery import DeliveryMailer
    from cloud_agent.workers.account_access import run_account_access_enforcement
    from cloud_agent.workers.account_broadcasts import run_account_broadcasts

    return [
        ("account_access_task", run_account_access_enforcement(storage)),
        ("account_broadcast_task", run_account_broadcasts(storage, mailer or DeliveryMailer())),
    ]


__all__ = ["account_tasks", "attach_owner_directory", "bind_identity_source"]
