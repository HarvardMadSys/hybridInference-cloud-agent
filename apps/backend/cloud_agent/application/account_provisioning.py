"""Invite approved accounts to the connected inference service, and revoke them.

One pass, run on the provisioning worker's clock:

1. Accounts approved since provisioning was first enabled are queued
   (``enqueue_new_approvals`` reads the activation marker, so existing
   accounts are invited only by the operator backfill command).
2. Each queued account is minted a one-time invitation token at the
   inference service and emailed a link to set a password there. The token
   travels only in that email (in the link's fragment, which browsers do not
   send) and as a hash at the service; the scoped API key the service issues
   never reaches this service.
3. Invited rows are polled to completion, and rows whose Agent access here
   has ended have their scoped key revoked there.

Every step is idempotent at the service (a re-mint supersedes the old link,
a re-mint after redemption answers 409, revoke answers for accounts that
never redeemed), so a failed step simply leaves its row for the next pass.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Protocol

import httpx

from cloud_agent.accounts.mail import AccountMail
from cloud_agent.logging_utils import get_logger
from cloud_agent.settings import boolean_setting, setting_env, text_setting

if TYPE_CHECKING:
    from cloud_agent.storage.assembly import Storage

logger = get_logger(__name__)

PROVISIONING_INTERVAL_S = 30.0

_ENABLED = "accounts.inference_provisioning_enabled"
_API_URL = "accounts.inference_provisioning_api_url"
_WEB_URL = "accounts.inference_provisioning_web_url"
_TOKEN_ENV = "AGENT_LOCAL_INFERENCE_PROVISIONING_DISPATCH_TOKEN"


class ProvisioningRefused(Exception):
    """The inference service refused a call; the row stays for the next pass."""


class InferenceProvisioningClient:
    """The inference service's internal invitation endpoints, dispatch-token guarded."""

    def __init__(
        self,
        api_url: str,
        dispatch_token: str,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        """Bind the service origin and the shared token."""
        self._base = api_url.rstrip("/")
        self._headers = {"Authorization": f"Bearer {dispatch_token}"}
        self._transport = transport

    def _http(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(timeout=10.0, transport=self._transport)

    async def mint(self, *, account_id: str, email: str) -> tuple[str, str | None]:
        """Mint an invitation token; ``("already", None)`` once redeemed."""
        try:
            async with self._http() as client:
                response = await client.post(
                    f"{self._base}/internal/agent-invitations",
                    headers=self._headers,
                    json={"agent_account_id": account_id, "email": email},
                )
        except httpx.HTTPError as exc:
            raise ProvisioningRefused(f"mint unreachable: {type(exc).__name__}") from exc
        if response.status_code == 409:
            return "already", None
        if response.status_code != 201:
            raise ProvisioningRefused(f"mint answered {response.status_code}")
        token = response.json().get("token")
        if not isinstance(token, str) or not token:
            raise ProvisioningRefused("mint answered without a token")
        return "minted", token

    async def provisioned(self, *, account_id: str) -> bool:
        """Whether the service holds a live provisioned account for this one."""
        try:
            async with self._http() as client:
                response = await client.get(
                    f"{self._base}/internal/agent-invitations/{account_id}",
                    headers=self._headers,
                )
        except httpx.HTTPError as exc:
            raise ProvisioningRefused(f"status unreachable: {type(exc).__name__}") from exc
        if response.status_code != 200:
            raise ProvisioningRefused(f"status answered {response.status_code}")
        return bool(response.json().get("provisioned"))

    async def revoke(self, *, account_id: str) -> None:
        """Revoke the scoped key this account redeemed, if it redeemed one."""
        try:
            async with self._http() as client:
                response = await client.post(
                    f"{self._base}/internal/agent-invitations/revoke",
                    headers=self._headers,
                    json={"agent_account_id": account_id},
                )
        except httpx.HTTPError as exc:
            raise ProvisioningRefused(f"revoke unreachable: {type(exc).__name__}") from exc
        if response.status_code != 200:
            raise ProvisioningRefused(f"revoke answered {response.status_code}")


class _Mailer(Protocol):
    async def send(self, mail: AccountMail) -> bool: ...


def _client_from_settings() -> tuple[InferenceProvisioningClient, str] | None:
    """The configured client and web origin, or None when not configured."""
    api_url = text_setting(_API_URL).strip()
    web_url = text_setting(_WEB_URL).strip()
    token = (setting_env(_TOKEN_ENV) or "").strip()
    if not api_url or not web_url or not token:
        logger.warning(
            "agent_inference_provisioning_unconfigured",
            extra={"event": "agent_inference_provisioning_unconfigured"},
        )
        return None
    return InferenceProvisioningClient(api_url, token), web_url.rstrip("/")


async def _revoke_ended(repo: Any, client: InferenceProvisioningClient) -> None:
    """Revoke the scoped key of every row whose Agent access here has ended."""
    for row in await repo.access_ended():
        account_id = row["account_id"]
        try:
            if row["state"] == "pending":
                await repo.mark_revoked(account_id, audited=False)
                continue
            await client.revoke(account_id=account_id)
            await repo.mark_revoked(account_id, audited=True)
        except ProvisioningRefused as exc:
            await repo.record_attempt(account_id, error=str(exc))


async def _invite_pending(
    repo: Any, client: InferenceProvisioningClient, mailer: _Mailer, web_url: str
) -> None:
    """Mint and mail an invitation for every queued account."""
    for row in await repo.pending():
        account_id, email = row["account_id"], row["email"]
        if not email:
            await repo.record_attempt(account_id, error="account has no email address")
            continue
        try:
            outcome, token = await client.mint(account_id=account_id, email=email)
            if outcome == "already":
                await repo.mark_provisioned(account_id)
                continue
            sent = await mailer.send(
                AccountMail(
                    kind="inference_invite",
                    to=email,
                    link=f"{web_url}/agent-invite#token={token}",
                )
            )
            if sent:
                await repo.mark_invited(account_id)
            else:
                await repo.record_attempt(account_id, error="invitation mail was not delivered")
        except ProvisioningRefused as exc:
            await repo.record_attempt(account_id, error=str(exc))


async def _poll_invited(repo: Any, client: InferenceProvisioningClient) -> None:
    """Mark invited rows the service reports as provisioned."""
    for row in await repo.invited():
        account_id = row["account_id"]
        try:
            if await client.provisioned(account_id=account_id):
                await repo.mark_provisioned(account_id)
        except ProvisioningRefused as exc:
            await repo.record_attempt(account_id, error=str(exc))


async def run_provisioning_pass(
    storage: Storage,
    *,
    mailer: _Mailer,
    client: InferenceProvisioningClient | None = None,
    web_url: str = "",
) -> None:
    """One provisioning pass; see the module docstring for the steps."""
    if not boolean_setting(_ENABLED):
        return
    if client is None:
        configured = _client_from_settings()
        if configured is None:
            return
        client, web_url = configured

    repo = storage.account_provisioning
    activated_at = await repo.ensure_activation()
    await repo.enqueue_new_approvals(activated_at=activated_at)
    await _revoke_ended(repo, client)
    await _invite_pending(repo, client, mailer, web_url)
    await _poll_invited(repo, client)


__all__ = [
    "PROVISIONING_INTERVAL_S",
    "InferenceProvisioningClient",
    "ProvisioningRefused",
    "run_provisioning_pass",
]
