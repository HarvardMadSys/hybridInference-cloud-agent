"""Port: repository authorization and the GitLab credentials behind it.

Repo grants, OAuth state, and the stored GitLab connection. Declared here
because the publishing path needs it without the whole store.

Declared here, beside the code that calls it, so a caller depends on the
operations it needs rather than on the whole persistence surface.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Protocol

if TYPE_CHECKING:
    from contextlib import AbstractAsyncContextManager


class GitHubAccountSession(Protocol):
    """GitHub account operations bound to one serialized database connection."""

    async def connect_github_account(
        self,
        *,
        user_id: str,
        github_user_id: int,
        login: str,
        installations: list[dict[str, Any]],
        access_token_ciphertext: str | None = None,
        access_expires_at: Any = None,
        refresh_token_ciphertext: str | None = None,
        refresh_expires_at: Any = None,
    ) -> None:
        """Record the GitHub account and replace its installation grants."""
        ...

    async def get_github_account(self, *, user_id: str) -> dict[str, Any] | None:
        """Return the GitHub account this user connected, if any."""
        ...

    async def claim_github_token_refresh(
        self, *, user_id: str, lease_seconds: int, margin_seconds: int
    ) -> dict[str, Any] | None:
        """Take the lease to refresh an expiring token, or ``None`` if unavailable."""
        ...

    async def store_github_tokens(
        self,
        *,
        user_id: str,
        refresh_generation: int,
        access_token_ciphertext: str,
        access_expires_at: Any,
        refresh_token_ciphertext: str | None,
        refresh_expires_at: Any,
    ) -> bool:
        """Write only if this generation's lease is still live, then release it.

        Expiry makes the claim eligible for takeover and invalidates even a
        same-generation result that returns late.
        """
        ...

    async def clear_github_tokens(self, *, user_id: str, access_token_ciphertext: str) -> bool:
        """Forget a refused token unless it was replaced meanwhile."""
        ...

    async def disconnect_github_account(
        self,
        *,
        user_id: str,
    ) -> dict[str, Any] | None:
        """Atomically take and forget the current account and all of its grants."""
        ...


class GitLabAccountSession(Protocol):
    """GitLab account operations bound to one serialized database connection."""

    async def upsert_gitlab_connection(
        self,
        *,
        user_id: str,
        external_user_id: int,
        username: str,
        display_name: str | None,
        web_url: str | None,
        access_token_ciphertext: str,
        refresh_token_ciphertext: str,
        expires_at: Any,
    ) -> int:
        """Store one GitLab.com identity per platform user."""
        ...

    async def get_gitlab_connection(self, *, user_id: str) -> dict[str, Any] | None:
        """Return the owning user's current GitLab connection."""
        ...

    async def claim_gitlab_token_refresh(
        self,
        *,
        connection_id: int,
        user_id: str,
        lease_seconds: int,
        margin_seconds: int,
    ) -> dict[str, Any] | None:
        """Exclusively claim rotation of the current expiring token."""
        ...

    async def update_gitlab_tokens(
        self,
        *,
        connection_id: int,
        user_id: str,
        refresh_generation: int,
        access_token_ciphertext: str,
        refresh_token_ciphertext: str,
        expires_at: Any,
    ) -> bool:
        """Rotate only while this claimed generation's lease is still live.

        Expiry invalidates a late result even before another caller takes over.
        """
        ...

    async def delete_gitlab_connection(
        self, *, user_id: str, connection_id: int
    ) -> dict[str, Any] | None:
        """Atomically take and delete the current connection when its id matches."""
        ...


class SourceControlRepository(Protocol):
    """Port: repository authorization and the GitLab credentials behind it."""

    async def record_repo_grant(
        self, *, user_id: str, installation_id: int, account_login: str | None = None
    ) -> None:
        """Record that this user may use this installation."""
        ...

    async def list_repo_grants(self, *, user_id: str) -> list[dict[str, Any]]:
        """Return the installations this user has connected."""
        ...

    async def revoke_repo_grant(self, *, user_id: str, installation_id: int) -> bool:
        """Drop one connection. Uninstalling on GitHub is the other half."""
        ...

    def serialize_github_account(
        self, *, user_id: str
    ) -> AbstractAsyncContextManager[GitHubAccountSession]:
        """Serialize whole-grant GitHub authorization changes for one user."""
        ...

    def serialize_gitlab_account(
        self, *, user_id: str
    ) -> AbstractAsyncContextManager[GitLabAccountSession]:
        """Serialize GitLab connect and disconnect for one user."""
        ...

    async def connect_github_account(
        self,
        *,
        user_id: str,
        github_user_id: int,
        login: str,
        installations: list[dict[str, Any]],
        access_token_ciphertext: str | None = None,
        access_expires_at: Any = None,
        refresh_token_ciphertext: str | None = None,
        refresh_expires_at: Any = None,
    ) -> None:
        """Record the GitHub account and make its grants exactly ``installations``."""
        ...

    async def get_github_account(self, *, user_id: str) -> dict[str, Any] | None:
        """Return the GitHub account this user connected, if any."""
        ...

    async def claim_github_token_refresh(
        self, *, user_id: str, lease_seconds: int, margin_seconds: int
    ) -> dict[str, Any] | None:
        """Take the lease to refresh an expiring token, or ``None`` if it is not ours."""
        ...

    async def store_github_tokens(
        self,
        *,
        user_id: str,
        refresh_generation: int,
        access_token_ciphertext: str,
        access_expires_at: Any,
        refresh_token_ciphertext: str | None,
        refresh_expires_at: Any,
    ) -> bool:
        """Write only if this generation's lease is still live, then release it."""
        ...

    async def clear_github_tokens(self, *, user_id: str, access_token_ciphertext: str) -> bool:
        """Forget a token GitHub no longer honours, unless it was replaced meanwhile."""
        ...

    async def disconnect_github_account(
        self,
        *,
        user_id: str,
    ) -> dict[str, Any] | None:
        """Atomically take and forget the current account and all of its grants."""
        ...

    async def create_oauth_state(
        self,
        *,
        state_hash: str,
        user_id: str,
        provider: str,
        code_verifier_ciphertext: str | None,
        expires_at: Any,
    ) -> None:
        """Persist a short-lived state without retaining the bearer value."""
        ...

    async def consume_oauth_state(
        self, *, state_hash: str, user_id: str, provider: str
    ) -> dict[str, Any] | None:
        """Consume one live state exactly once for its owning user/provider."""
        ...

    async def upsert_gitlab_connection(
        self,
        *,
        user_id: str,
        external_user_id: int,
        username: str,
        display_name: str | None,
        web_url: str | None,
        access_token_ciphertext: str,
        refresh_token_ciphertext: str,
        expires_at: Any,
    ) -> int:
        """Store one GitLab.com identity per platform user."""
        ...

    async def get_gitlab_connection(self, *, user_id: str) -> dict[str, Any] | None:
        """Return the owning user's connection, including encrypted tokens."""
        ...

    async def claim_gitlab_token_refresh(
        self,
        *,
        connection_id: int,
        user_id: str,
        lease_seconds: int,
        margin_seconds: int,
    ) -> dict[str, Any] | None:
        """Exclusively claim rotation of the current connection's expiring token."""
        ...

    async def update_gitlab_tokens(
        self,
        *,
        connection_id: int,
        user_id: str,
        refresh_generation: int,
        access_token_ciphertext: str,
        refresh_token_ciphertext: str,
        expires_at: Any,
    ) -> bool:
        """Rotate only while this owner holds the generation and its live lease."""
        ...

    async def delete_gitlab_connection(
        self, *, user_id: str, connection_id: int
    ) -> dict[str, Any] | None:
        """Atomically take and delete the current connection when its id matches."""
        ...
