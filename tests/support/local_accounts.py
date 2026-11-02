"""Shared setup for local-account database tests: a pool and ready-made accounts."""

from __future__ import annotations

import os
from typing import TYPE_CHECKING, Any

import asyncpg
import pytest
from tests.support.db import reset_agent_tables
from tests.support.migrations import apply_migrations

from cloud_agent.accounts import tokens
from cloud_agent.accounts.passwords import hash_password

if TYPE_CHECKING:
    from cloud_agent.storage.assembly import Storage

LOCAL_SECRET = "local-session-secret-for-tests-0123456789"
PASSWORD = "Correct1Horse"


def db_config() -> dict[str, Any]:
    """The per-worker test database; anything not named ``*_test_*`` is refused."""
    base = os.getenv("TEST_DB_NAME", "hybridinference_test_db")
    worker_id = os.environ.get("PYTEST_XDIST_WORKER", "master")
    name = base if worker_id == "master" else f"{base}_{worker_id}"
    if "_test_" not in name:
        pytest.fail(f"SAFETY: TEST_DB_NAME='{name}' is not a test database.")
    return {
        "host": os.getenv("TEST_DB_HOST", "localhost"),
        "port": int(os.getenv("TEST_DB_PORT", "5432")),
        "database": name,
        "user": os.getenv("TEST_DB_USER", "postgres"),
        "password": os.getenv("TEST_DB_PASSWORD", "postgres"),
    }


async def open_pool(max_size: int = 8) -> asyncpg.Pool:
    """A pool over the emptied per-worker database at head."""
    config = db_config()
    try:
        pool = await asyncpg.create_pool(**config, min_size=1, max_size=max_size)
    except Exception as exc:  # pragma: no cover - the tier's precondition
        pytest.fail(f"PostgreSQL is required for the dbtest tier: {exc}")
    apply_migrations(config)
    async with pool.acquire() as conn:
        await reset_agent_tables(conn)
    return pool


async def close_pool(pool: asyncpg.Pool) -> None:
    """Empty the database again and close."""
    async with pool.acquire() as conn:
        await reset_agent_tables(conn)
    await pool.close()


async def make_account(
    storage: Storage,
    email: str,
    *,
    verified: bool = True,
    admission: str | None = None,
    admin: bool = False,
    password: str = PASSWORD,
    account_id: str | None = None,
) -> str:
    """Create an account in the state a test needs, through the repository where it can."""
    account_id = account_id or tokens.new_account_id()
    created = await storage.local_accounts.create_account(
        account_id=account_id,
        email=email,
        email_normalized=email.lower(),
        password_hash=await hash_password(password),
    )
    assert created
    if verified:
        token = tokens.new_email_token()
        await storage.local_accounts.issue_email_token(
            account_id=account_id,
            purpose="verify_email",
            token_hash=tokens.email_token_hash(token),
            ttl_s=60,
        )
        assert await storage.local_accounts.verify_email(token_hash=tokens.email_token_hash(token))
    if admission in ("pending", "approved"):
        await storage.local_accounts.submit_application(account_id=account_id, purpose="test")
    if admission == "approved":
        record = await storage.local_accounts.get_account(account_id)
        assert record is not None
        await storage.local_accounts.apply_admin_action(
            account_id=account_id,
            action="approve",
            reason=None,
            expected_version=record.version,
            actor_id="admin-token",
            actor_account_id=None,
            admin_ip="test",
        )
    if admin:
        record = await storage.local_accounts.get_account(account_id)
        assert record is not None
        await storage.local_accounts.apply_admin_action(
            account_id=account_id,
            action="grant_admin",
            reason=None,
            expected_version=record.version,
            actor_id="admin-token",
            actor_account_id=None,
            admin_ip="test",
        )
    return account_id


async def act(storage: Storage, account_id: str, action: str, **kwargs: Any) -> Any:
    """Apply an administrator action at the account's current version."""
    record = await storage.local_accounts.get_account(account_id)
    assert record is not None
    return await storage.local_accounts.apply_admin_action(
        account_id=account_id,
        action=action,
        reason=kwargs.pop("reason", None),
        expected_version=kwargs.pop("expected_version", record.version),
        actor_id=kwargs.pop("actor_id", "admin-token"),
        actor_account_id=kwargs.pop("actor_account_id", None),
        admin_ip="test",
    )
