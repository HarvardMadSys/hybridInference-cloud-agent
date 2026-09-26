"""A user's own GitHub token, kept usable: decrypted, and renewed before it lapses.

Which repositories a user may work on is asked of GitHub *as that user* (see
:mod:`cloud_agent.entitlement`), so every entitlement check needs a token that
still works — including the ones the publisher and the goal controller make
hours after the user last looked at the page.

An App that expires user tokens hands out eight-hour access tokens and
single-use refresh tokens. Two callers renewing at once would spend the same
refresh token, and GitHub would refuse the second: the connection both were
keeping would be gone. So a renewal is taken under a lease on the account row,
the way the MCP credential refresh is, and a caller that loses the race reads
the row again — finding either the new token or the old one, still good for a
few minutes, since renewal starts before expiry.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING, Any

from cloud_agent.github_app import UserAuthorizationRevoked
from cloud_agent.logging_utils import get_logger

if TYPE_CHECKING:
    from cloud_agent.ports import SourceControlRepository

logger = get_logger(__name__)

#: Renew this long before expiry, so a token read now still works for the
#: GitHub calls made with it.
_RENEW_BEFORE = timedelta(minutes=5)
#: A renewal that takes longer than this has died; the next caller may retry.
_LEASE_SECONDS = 30
#: How long a caller that lost the race waits for the winner's token.
_WAIT_ATTEMPTS = 10
_WAIT_INTERVAL_S = 0.3


class UserTokenUnavailable(Exception):
    """The token is being renewed elsewhere and has already lapsed. Try again."""


@dataclass(frozen=True)
class LiveUserToken:
    """A token that speaks as the user now, and the stored form it came from.

    The ciphertext identifies *which* stored token this was: if GitHub then
    refuses it, only that one is forgotten — not a newer one a reconnect or a
    renewal wrote in the meantime.
    """

    value: str
    ciphertext: str


def _aware(value: datetime | None) -> datetime | None:
    if value is not None and value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value


async def user_access_token(
    user_id: str, *, store: SourceControlRepository, app_credentials: Any
) -> LiveUserToken | None:
    """A token that speaks as this user now, or ``None`` if they must reconnect.

    ``None`` is settled: there is no stored token (a connection made before
    tokens were kept) or GitHub refused to renew it. A renewal lost to another
    caller that has not finished yet raises :class:`UserTokenUnavailable`.
    """
    account = await store.get_github_account(user_id=user_id)
    if not account or not account.get("access_token_ciphertext"):
        return None
    cipher = app_credentials.cipher
    expires = _aware(account.get("access_expires_at"))
    if expires is None or expires - _RENEW_BEFORE > datetime.now(timezone.utc):
        ciphertext = account["access_token_ciphertext"]
        return LiveUserToken(cipher.decrypt(ciphertext), ciphertext)

    claim = await store.claim_github_token_refresh(
        user_id=user_id,
        lease_seconds=_LEASE_SECONDS,
        margin_seconds=int(_RENEW_BEFORE.total_seconds()),
    )
    if claim is None:
        return await _await_renewal(user_id, store=store, cipher=cipher)

    try:
        tokens = await app_credentials.refresh_user_token(
            cipher.decrypt(claim["refresh_token_ciphertext"])
        )
    except UserAuthorizationRevoked:
        await store.clear_github_tokens(
            user_id=user_id, access_token_ciphertext=account["access_token_ciphertext"]
        )
        logger.info(
            "agent_github_user_token_revoked",
            extra={"event": "agent_github_user_token_revoked", "stage": "refresh"},
        )
        return None
    # A failure for a passing reason leaves the lease to lapse, and the next
    # caller to try again; the current token is still good until it expires.
    ciphertext = cipher.encrypt(tokens.access_token)
    await store.store_github_tokens(
        user_id=user_id,
        refresh_generation=claim["refresh_generation"],
        access_token_ciphertext=ciphertext,
        access_expires_at=tokens.access_expires_at,
        refresh_token_ciphertext=(
            cipher.encrypt(tokens.refresh_token) if tokens.refresh_token else None
        ),
        refresh_expires_at=tokens.refresh_expires_at,
    )
    return LiveUserToken(tokens.access_token, ciphertext)


async def _await_renewal(
    user_id: str, *, store: SourceControlRepository, cipher: Any
) -> LiveUserToken | None:
    """Read the row until another caller's renewal lands, or the old token will do."""
    for attempt in range(_WAIT_ATTEMPTS):
        account = await store.get_github_account(user_id=user_id)
        if not account or not account.get("access_token_ciphertext"):
            return None
        expires = _aware(account.get("access_expires_at"))
        if expires is None or expires > datetime.now(timezone.utc):
            ciphertext = account["access_token_ciphertext"]
            return LiveUserToken(cipher.decrypt(ciphertext), ciphertext)
        if attempt + 1 < _WAIT_ATTEMPTS:
            await asyncio.sleep(_WAIT_INTERVAL_S)
    raise UserTokenUnavailable("the user's GitHub token is being renewed")


async def forget_revoked_token(
    user_id: str, token: LiveUserToken, *, store: SourceControlRepository
) -> None:
    """Forget a token GitHub refused outright, so the user reconnects."""
    await store.clear_github_tokens(user_id=user_id, access_token_ciphertext=token.ciphertext)
    logger.info(
        "agent_github_user_token_revoked",
        extra={"event": "agent_github_user_token_revoked", "stage": "request"},
    )


__all__ = [
    "LiveUserToken",
    "UserTokenUnavailable",
    "forget_revoked_token",
    "user_access_token",
]
