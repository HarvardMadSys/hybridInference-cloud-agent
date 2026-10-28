"""The local answers to the questions Gateway mode asks the Gateway.

Every owner check in this service (request admission, a claim, a goal round, a
waiting message, a team approval, Profile's email, team rosters) reaches
identity and admission through the object the lifespan stores as
``app.state.gateway``. In local mode that object is :class:`LocalModeGateway`:
the same methods, answered from this database by :class:`LocalOwnerDirectory`,
and the Gateway-only ones refused with a typed error **without a network
call**. A local account id is never sent to the Gateway.

The decision comes from the database on every call: there is no cache, so an
administrator's change governs the next request, claim and round.

``LocalModeGrantClient`` stands in for the Gateway grant client the same way.
Gateway grants are inference credentials keyed to a Gateway subject, and a
local account has none: every mint is refused.

**Models.** A local account runs only on its own model connections. A turn
on one names its ``model_connection_id``, and every step of it (admission,
follow-ups, the waiting queue, goal rounds, Fork, claims and residencies)
resolves the model through that connection, never through a catalog here. So
this object has no catalog at all: a Gateway model lookup, which is what a
turn without a connection asks for, raises :class:`ModelConnectionRequired`.
Feeding connection models in here would erase their source, and two
connections, or a connection and the Gateway, may offer the same model id.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from cloud_agent.agent_access import AgentAccessDecision
from cloud_agent.domain.accounts import may_sign_in, permissions_for
from cloud_agent.gateway import GatewayError
from cloud_agent.logging_utils import get_logger

if TYPE_CHECKING:
    from datetime import datetime

    from cloud_agent.grants import GrantUsageReport
    from cloud_agent.ports.accounts import LocalAccountRepository

logger = get_logger(__name__)

MODEL_CONNECTION_REQUIRED = "model_connection_required"
NO_GATEWAY_FEATURE = "gateway_feature_unavailable"


class ModelConnectionRequired(GatewayError):
    """Local mode has no Gateway inference: a personal model connection is needed."""

    def __init__(self) -> None:
        """A definitive refusal, so no caller retries it as an outage."""
        super().__init__(
            "This deployment has no shared models. Choose a model from one of your own "
            "model connections, or add one in Settings.",
            status_code=409,
            error_type=MODEL_CONNECTION_REQUIRED,
        )


class LocalDirectoryUnavailable(GatewayError):
    """The local account store could not answer; no verdict on the owner."""


class LocalOwnerDirectory:
    """Identity and admission for local accounts, read fresh from the database."""

    def __init__(self, accounts: LocalAccountRepository) -> None:
        """Answer from this repository."""
        self._accounts = accounts

    async def _account(self, user_id: str) -> Any:
        try:
            return await self._accounts.get_account(user_id)
        except Exception as exc:
            logger.warning(
                "agent_local_directory_unavailable",
                exc_info=True,
                extra={"event": "agent_local_directory_unavailable"},
            )
            raise LocalDirectoryUnavailable("local account store unavailable") from exc

    async def agent_access(self, user_id: str) -> AgentAccessDecision:
        """The account's current permissions, in the Gateway contract's shape.

        An unknown, unverified or suspended account is ``subject_unavailable``
        exactly as the Gateway reports an unknown or inactive one, so every
        caller treats it as the denial it already handles.
        """
        record = await self._account(user_id)
        if record is None or not may_sign_in(record):
            raise GatewayError(
                "local account unavailable", status_code=403, error_type="subject_unavailable"
            )
        permissions = permissions_for(record)
        return AgentAccessDecision.model_validate(
            {"user_id": user_id, "allowed": bool(permissions), "permissions": permissions}
        )

    async def user_email(self, user_id: str) -> str:
        """The account's email, for Profile and team rosters."""
        record = await self._account(user_id)
        if record is None:
            raise GatewayError("local account not found", status_code=404)
        return str(record.email)


class LocalModeGateway:
    """``app.state.gateway`` in local mode: local identity, no Gateway calls."""

    #: Callers that check whether a Gateway is reachable find one that answers.
    configured = True

    def __init__(self, directory: LocalOwnerDirectory) -> None:
        """Answer identity from ``directory``."""
        self._directory = directory

    async def agent_access(self, user_id: str) -> AgentAccessDecision:
        """See :meth:`LocalOwnerDirectory.agent_access`."""
        return await self._directory.agent_access(user_id)

    async def user_email(self, user_id: str) -> str:
        """See :meth:`LocalOwnerDirectory.user_email`."""
        return await self._directory.user_email(user_id)

    async def model_catalog_bundle(
        self, user_id: str
    ) -> tuple[list[str], dict[str, str], dict[str, list[str]]]:
        """No Gateway catalog exists here: a typed refusal, without a network call."""
        raise ModelConnectionRequired

    async def model_catalog(self, user_id: str) -> list[str]:
        """Model ids only."""
        models, _aliases, _efforts = await self.model_catalog_bundle(user_id)
        return models

    async def model_catalog_with_aliases(self, user_id: str) -> tuple[list[str], dict[str, str]]:
        """Model ids and aliases."""
        models, aliases, _efforts = await self.model_catalog_bundle(user_id)
        return models, aliases

    async def model_reasoning_efforts(self, user_id: str) -> dict[str, list[str]]:
        """Effort domains."""
        _models, _aliases, efforts = await self.model_catalog_bundle(user_id)
        return efforts

    async def model_catalog_with_reasoning_efforts(
        self, user_id: str
    ) -> tuple[list[str], dict[str, list[str]]]:
        """Model ids and effort domains."""
        models, _aliases, efforts = await self.model_catalog_bundle(user_id)
        return models, efforts

    async def exchange_authorization_code(self, **_kwargs: Any) -> str:
        """Gateway sign-in does not exist in local mode."""
        raise GatewayError(
            "Gateway sign-in is disabled in local mode",
            status_code=404,
            error_type="gateway_not_configured",
        )


class LocalModeGrantClient:
    """``app.state.grants`` in local mode: no Gateway grant is ever minted or read."""

    binding_id = ""
    configured = False

    async def mint(self, **_kwargs: Any) -> dict[str, Any]:
        """Refuse definitively, as for a subject the Gateway cannot serve.

        Unreachable for a turn on a model connection, which never asks for a
        grant, and job creation refuses every other turn; should one get this
        far, the claim path ends it instead of returning it to the queue.
        """
        raise GatewayError(
            "Gateway inference grants are not available to local accounts",
            status_code=403,
            error_type="subject_unavailable",
        )

    async def renew(self, grant_id: str) -> dict[str, Any]:
        """No grant exists to renew."""
        raise ModelConnectionRequired

    async def revoke(self, grant_id: str) -> bool:
        """No grant exists to revoke; nothing is sent."""
        return True

    async def usage(self, grant_id: str, *, since: datetime, until: datetime) -> GrantUsageReport:
        """Gateway usage is not available in local mode."""
        raise GatewayError(
            "Gateway usage is not available in local mode",
            status_code=404,
            error_type=NO_GATEWAY_FEATURE,
        )

    async def usage_requests(self, *_args: Any, **_kwargs: Any) -> Any:
        """Gateway request detail is not available in local mode."""
        raise GatewayError(
            "Gateway request detail is not available in local mode",
            status_code=404,
            error_type=NO_GATEWAY_FEATURE,
        )


__all__ = [
    "MODEL_CONNECTION_REQUIRED",
    "NO_GATEWAY_FEATURE",
    "LocalDirectoryUnavailable",
    "LocalModeGateway",
    "LocalModeGrantClient",
    "LocalOwnerDirectory",
    "ModelConnectionRequired",
]
