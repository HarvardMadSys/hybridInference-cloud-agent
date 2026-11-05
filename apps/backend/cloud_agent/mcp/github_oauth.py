"""Service-managed app identity, bounded to GitHub's hosted MCP service."""

from cloud_agent.settings import setting_env

ISSUER = "https://github.com/login/oauth"
AUTHORIZATION_ENDPOINT = f"{ISSUER}/authorize"
TOKEN_ENDPOINT = f"{ISSUER}/access_token"


def client_credentials() -> tuple[str, str] | None:
    """Read the service's app identity, never a user's access token."""
    client_id = (setting_env("AGENT_MCP_GITHUB_CLIENT_ID") or "").strip()
    secret = setting_env("AGENT_MCP_GITHUB_CLIENT_SECRET") or ""
    return (client_id, secret) if client_id and secret else None


def is_endpoint(url: str | None) -> bool:
    """Limit shared app credentials to the built-in GitHub service addresses."""
    return (url or "").rstrip("/") in {
        "https://api.githubcopilot.com/mcp",
        "https://api.githubcopilot.com/mcp/readonly",
    }


def is_managed(*, issuer: object, token_endpoint: object, resource: str | None) -> bool:
    """Whether a sign-in is GitHub's own, the only one the app identity may serve.

    Its issuer and token endpoint must be GitHub's, and its resource one of
    GitHub's service addresses. Never endpoints a discovery document supplied:
    the shared secret would go wherever they point. The authorization endpoint
    is not checked here; a caller that sends the browser there checks it.
    """
    return issuer == ISSUER and token_endpoint == TOKEN_ENDPOINT and is_endpoint(resource)
