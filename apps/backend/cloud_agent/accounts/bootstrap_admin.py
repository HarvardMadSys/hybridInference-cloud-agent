"""Grant the first local administrator, with the deployment's own database credential.

There is no public bootstrap endpoint and no "first account to sign up is the
administrator": whoever can run this holds ``AGENT_DATABASE_URL`` and the
local session secret, which is the deployment's authority, not an account's.

The account must already exist and have verified its email. Find its id with
``--find-email``, then grant::

    python -m cloud_agent.accounts.bootstrap_admin --find-email admin@example.org
    python -m cloud_agent.accounts.bootstrap_admin --account-id <id> --approve

``--approve`` also approves the account's Agent application in its own
audited step, because administration is granted only to an approved account
and the first administrator has nobody to approve them. Without it, an
unapproved account is refused. Both steps are the same audited, versioned
actions the administration API applies, recorded with the actor
``deployment-bootstrap``.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from cloud_agent.ports.accounts import LocalAccountRepository

ACTOR = "deployment-bootstrap"


class BootstrapRefused(RuntimeError):
    """The account cannot be made an administrator as asked."""


async def grant_first_admin(
    accounts: LocalAccountRepository, account_id: str, *, approve: bool
) -> list[str]:
    """Approve (if asked) and grant administration; the actions applied, in order.

    Raises:
        BootstrapRefused: Unknown, unverified or suspended account, or not
            approved without ``approve``.
    """
    record = await accounts.get_account(account_id)
    if record is None:
        raise BootstrapRefused("No local account has that id.")
    if not record.email_verified or record.status != "active":
        raise BootstrapRefused("The account must be active and have verified its email.")
    if record.is_admin:
        return []
    applied: list[str] = []
    if record.admission_status != "approved":
        if not approve:
            raise BootstrapRefused(
                "The account is not approved for Agent use; pass --approve to approve it too."
            )
        result = await accounts.apply_admin_action(
            account_id=account_id,
            action="approve",
            reason=None,
            expected_version=record.version,
            actor_id=ACTOR,
            actor_account_id=None,
            admin_ip="local",
        )
        record = result.account
        applied.append("approve")
    await accounts.apply_admin_action(
        account_id=account_id,
        action="grant_admin",
        reason=None,
        expected_version=record.version,
        actor_id=ACTOR,
        actor_account_id=None,
        admin_ip="local",
    )
    applied.append("grant_admin")
    return applied


async def _run(args: argparse.Namespace) -> str:
    import asyncpg

    from cloud_agent.accounts.config import check_local_configuration, local_mode
    from cloud_agent.application.accounts import normalize_email
    from cloud_agent.identity_namespace import ensure_local_namespace
    from cloud_agent.storage.assembly import Storage

    if not local_mode():
        raise BootstrapRefused("AGENT_AUTH_MODE=local must be set.")
    check_local_configuration()
    dsn = os.environ.get("AGENT_DATABASE_URL")
    if not dsn:
        raise BootstrapRefused("AGENT_DATABASE_URL must be set.")
    pool = await asyncpg.create_pool(dsn, min_size=1, max_size=1)
    try:
        await ensure_local_namespace(pool)
        accounts = Storage(pool).local_accounts
        if args.find_email:
            login = await accounts.password_login(normalize_email(args.find_email)[1])
            if login is None:
                raise BootstrapRefused("No local account uses that email.")
            return login.account.id
        applied = await grant_first_admin(accounts, args.account_id, approve=args.approve)
        return "already an administrator" if not applied else "applied: " + ", ".join(applied)
    finally:
        await pool.close()


def main() -> None:
    """Find an account, or grant it administration."""
    parser = argparse.ArgumentParser(description="Grant the first local administrator.")
    target = parser.add_mutually_exclusive_group(required=True)
    target.add_argument("--account-id", help="The verified account to make an administrator")
    target.add_argument("--find-email", help="Print the id of the account using this email")
    parser.add_argument(
        "--approve", action="store_true", help="Also approve the account's Agent application"
    )
    args = parser.parse_args()
    try:
        print(asyncio.run(_run(args)))
    except BootstrapRefused as exc:
        print(f"refused: {exc}", file=sys.stderr)
        raise SystemExit(2) from None


if __name__ == "__main__":
    main()
