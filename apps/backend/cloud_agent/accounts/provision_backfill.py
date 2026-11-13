"""Queue inference invitations for accounts approved before activation.

Provisioning invites only accounts approved after it was first switched on
(Admin → Settings → Accounts). This command is the explicit, resumable way
to invite the accounts approved earlier: it queues provisioning rows, and
the provisioning worker mints and mails them on its next passes.

    python -m cloud_agent.accounts.provision_backfill            # dry run
    python -m cloud_agent.accounts.provision_backfill --apply    # queue all
    python -m cloud_agent.accounts.provision_backfill --apply --account-id <id>

A dry run writes nothing and prints how many eligible accounts (active,
approved, verified, with an email address) would be queued. Re-running with
``--apply`` queues nobody twice: accounts already invited, provisioned or
revoked are left as they stand.
"""

from __future__ import annotations

import argparse
import asyncio
import os


class BackfillRefused(RuntimeError):
    """The backfill cannot run as asked."""


async def _run(args: argparse.Namespace) -> str:
    import asyncpg

    from cloud_agent.accounts.config import (
        check_local_configuration,
        refuse_retired_configuration,
    )
    from cloud_agent.deployment.identity_namespace import ensure_local_namespace
    from cloud_agent.storage.assembly import Storage

    refuse_retired_configuration()
    check_local_configuration()
    dsn = os.environ.get("AGENT_DATABASE_URL")
    if not dsn:
        raise BackfillRefused("AGENT_DATABASE_URL must be set.")
    pool = await asyncpg.create_pool(dsn, min_size=1, max_size=2)
    try:
        await ensure_local_namespace(pool)
        repo = Storage(pool).account_provisioning
        if args.account_id:
            ids = [args.account_id]
        else:
            ids = await repo.eligible_account_ids()
        if not args.apply:
            return f"dry run: {len(ids)} eligible account(s) would be queued"
        queued = await repo.enqueue_accounts(ids)
        return f"queued {queued} account(s); the provisioning worker mails them"
    finally:
        await pool.close()


def main() -> None:
    """Queue inference invitations for earlier approvals, or report the count."""
    parser = argparse.ArgumentParser(
        description="Queue inference invitations for accounts approved before provisioning began."
    )
    parser.add_argument("--apply", action="store_true", help="Queue the rows (default: dry run)")
    parser.add_argument("--account-id", help="Queue only this account")
    args = parser.parse_args()
    try:
        print(asyncio.run(_run(args)))
    except BackfillRefused as exc:
        parser.exit(1, f"refused: {exc}\n")


if __name__ == "__main__":
    main()
