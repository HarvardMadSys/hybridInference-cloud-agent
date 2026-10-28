"""SQL that lets other statements honour a local account's current admission."""

from __future__ import annotations


def local_access_ended(user_column: str) -> str:
    """A condition true while ``user_column``'s local account has no Agent use.

    The same rule as ``permissions_for``: suspended, unverified, or not
    approved. Always false in Gateway mode, whose database holds no local
    account, so adding it to a statement changes nothing there. In local mode
    it makes the statement itself the fence: a residency an attempt admitted
    before a revocation cannot be established or extended after it commits.

    Args:
        user_column: A trusted SQL expression naming the owner's id.
    """
    return f"""EXISTS (
        SELECT 1 FROM agent_local_accounts la
        JOIN agent_local_admissions lm ON lm.account_id = la.id
        JOIN agent_local_password_logins ll ON ll.account_id = la.id
        WHERE la.id = {user_column}
          AND (la.status <> 'active' OR lm.status <> 'approved'
               OR ll.email_verified_at IS NULL)
    )"""


__all__ = ["local_access_ended"]
