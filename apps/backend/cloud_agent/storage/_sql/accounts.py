"""SQL that lets other statements honour a local account's current admission."""

from __future__ import annotations


def local_access_ended(user_column: str) -> str:
    """A condition true while ``user_column``'s local account has no Agent use.

    The same rule as ``permissions_for``, read from the shared view
    ``agent_local_account_access``: suspended or deleted, identity unverified,
    or not approved. The view is built from ``LEFT JOIN`` and ``coalesce``, so
    an account missing its admission or every identity row has no use, and
    this condition is true for it: fail closed. Always false in Gateway mode,
    whose database holds no local account, so adding it to a statement changes
    nothing there. In local mode it makes the statement itself the fence: a
    residency an attempt admitted before a revocation cannot be established or
    extended after it commits.

    Args:
        user_column: A trusted SQL expression naming the owner's id.
    """
    return f"""EXISTS (
        SELECT 1 FROM agent_local_account_access lx
        WHERE lx.account_id = {user_column} AND NOT lx.has_use
    )"""


__all__ = ["local_access_ended"]
