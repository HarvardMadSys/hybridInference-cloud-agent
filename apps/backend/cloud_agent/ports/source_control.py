"""Port: repository authorization and the GitLab credentials behind it.

Repo grants, OAuth state, and the stored GitLab connection. Declared here
because the publishing path needs it without the whole store.

Declared here, beside the code that calls it, so a caller depends on the
operations it needs rather than on the whole persistence surface.
"""

from __future__ import annotations

from typing import Any, Protocol


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
        """Write a refreshed token pair while the lease is still ours, and release it."""
        ...

    async def clear_github_tokens(self, *, user_id: str, access_token_ciphertext: str) -> bool:
        """Forget a token GitHub no longer honours, unless it was replaced meanwhile."""
        ...

    async def disconnect_github_account(self, *, user_id: str) -> None:
        """Forget the GitHub account, its tokens, and every installation grant."""
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

    async def update_gitlab_tokens(
        self,
        *,
        connection_id: int,
        user_id: str,
        access_token_ciphertext: str,
        refresh_token_ciphertext: str,
        expires_at: Any,
    ) -> bool:
        """Rotate encrypted OAuth tokens without crossing user ownership."""
        ...

    async def delete_gitlab_connection(self, *, user_id: str, connection_id: int) -> bool:
        """Delete only a connection owned by the authenticated user."""
        ...
