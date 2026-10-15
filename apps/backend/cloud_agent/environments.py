"""Safe domain failures for owner environment configuration and execution."""

from cloud_agent.source_control import SourceControlCipher, SourceControlError
from cloud_agent_shared.environment import ENVIRONMENT_DOMAIN


class EnvironmentNotFound(Exception):
    """An owned environment, binding or run does not exist."""


class EnvironmentConflict(Exception):
    """A revision or idempotency key differs from the reviewed input."""

    def __init__(self, message: str, *, current_revision: int | None = None) -> None:
        """Carry only safe revision metadata with a conflict explanation."""
        super().__init__(message)
        self.current_revision = current_revision


class EnvironmentForbidden(Exception):
    """The caller may use this environment but not change it.

    A team's environments are changed by the team's owner and admins; every
    other active member may only select them.
    """


class EnvironmentAccessRevoked(Exception):
    """A chat uses a team environment its owner may no longer use.

    The owner left or was removed from that team, rejoined it since the chat
    bound the environment, or the team was deleted. The binding carries the
    team's Secrets, so no further turn runs on it.
    """


class EnvironmentBusy(Exception):
    """The owner already has a nonterminal environment test."""


class EnvironmentEncryptionError(Exception):
    """Environment values cannot be encrypted or decrypted by this deployment."""


class EnvironmentCipher(SourceControlCipher):
    """Translate only environment-key failures into the environment error domain."""

    def __init__(self) -> None:
        """Derive the environment domain key without exposing key diagnostics."""
        try:
            super().__init__(domain=ENVIRONMENT_DOMAIN)
        except SourceControlError:
            raise EnvironmentEncryptionError("Environment encryption is unavailable") from None

    def decrypt(self, value: str) -> str:
        """Read environment ciphertext with an environment-specific failure."""
        try:
            return super().decrypt(value)
        except SourceControlError:
            raise EnvironmentEncryptionError("Environment decryption failed") from None
