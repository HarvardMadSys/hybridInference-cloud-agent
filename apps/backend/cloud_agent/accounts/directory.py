"""Who an owner is and whether they may use Agent, answered from this database.

Every owner check in this service (request admission, a claim, a goal round, a
waiting message, a team approval, Profile's email, team rosters) reads the
account directory the lifespan stores as ``app.state.owners``: one primary-key
read of the account, fresh on every call, so an administrator's change governs
the next request, claim and round. There is no cache and no other source.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from cloud_agent.agent_access import AgentAccessDecision
from cloud_agent.domain.accounts import may_sign_in, permissions_for
from cloud_agent.logging_utils import get_logger

if TYPE_CHECKING:
    from cloud_agent.ports.accounts import LocalAccountRepository

logger = get_logger(__name__)

#: The refusal a turn without a model connection gets: owners run only on
#: their own model connections.
MODEL_CONNECTION_REQUIRED = "model_connection_required"


class OwnerDirectoryError(Exception):
    """The directory's answer was a refusal, or it had none to give.

    ``status_code`` and ``error_type`` say which: ``403
    subject_unavailable`` is a verdict on the owner (unknown, unverified,
    suspended or deleted); ``503`` means the store could not be read.
    """

    def __init__(
        self, message: str, *, status_code: int = 503, error_type: str | None = None
    ) -> None:
        """Keep the shape callers branch on."""
        super().__init__(message)
        self.status_code = status_code
        self.error_type = error_type


class LocalDirectoryUnavailable(OwnerDirectoryError):
    """The account store could not answer; no verdict on the owner."""


class ModelConnectionRequired(OwnerDirectoryError):
    """A turn named no model connection; there is no other inference source."""

    def __init__(self) -> None:
        """A definitive refusal, so no caller retries it as an outage."""
        super().__init__(
            "Choose a model from one of your own model connections, or add one in Settings.",
            status_code=409,
            error_type=MODEL_CONNECTION_REQUIRED,
        )


class LocalOwnerDirectory:
    """Identity and admission for accounts, read fresh from the database."""

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
            raise LocalDirectoryUnavailable("account store unavailable") from exc

    async def agent_access(self, user_id: str) -> AgentAccessDecision:
        """The account's current permissions.

        An unknown, unverified, suspended or deleted account is refused with
        ``subject_unavailable``, a verdict every caller treats as a denial.
        """
        record = await self._account(user_id)
        if record is None or not may_sign_in(record):
            raise OwnerDirectoryError(
                "account unavailable", status_code=403, error_type="subject_unavailable"
            )
        permissions = permissions_for(record)
        return AgentAccessDecision.model_validate(
            {"user_id": user_id, "allowed": bool(permissions), "permissions": permissions}
        )

    async def user_email(self, user_id: str) -> str:
        """The account's email, for Profile and team rosters."""
        record = await self._account(user_id)
        if record is None:
            raise OwnerDirectoryError("account not found", status_code=404)
        return str(record.email or "")


__all__ = [
    "MODEL_CONNECTION_REQUIRED",
    "LocalDirectoryUnavailable",
    "LocalOwnerDirectory",
    "ModelConnectionRequired",
    "OwnerDirectoryError",
]
