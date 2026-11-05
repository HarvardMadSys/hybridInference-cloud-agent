"""The OAuth callback's refusals that need no database read.

Split from :mod:`cloud_agent.api.mcp_connections`, which answers the callback;
each function names the message the callback redirects with, or ``None``.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Mapping


def _callback_request_refusal(provider: str, *, error: str, state: str, code: str) -> str | None:
    """Why the callback request itself cannot complete a flow, or None."""
    if error:
        return (
            "GitHub sign-in was cancelled. You can try again."
            if provider == "github" and error == "access_denied"
            else error
        )
    if not state or not code:
        return "missing state or code"
    return None


def _stale_flow_refusal(row: Mapping[str, Any], flow: Mapping[str, Any]) -> str | None:
    """What changed on the connection since its flow started, or None."""
    for key, message in (
        ("config_revision", "the connection was reconfigured"),
        ("authorization_epoch", "the connection was reconnected"),
        ("oauth_generation", "a newer authorization replaced this one"),
    ):
        if int(row.get(key) or 0) != int(flow[key]):
            # Per-item CAS: each version is compared on its own so the message
            # names what changed rather than a generic failure.
            return message
    return None
