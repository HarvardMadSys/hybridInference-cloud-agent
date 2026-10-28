"""Bind managed host inputs to this control plane's deployment identity mode."""

from __future__ import annotations

from cloud_agent.accounts.config import auth_mode
from cloud_agent.ports.external.compute_provider import ProviderError


def profile_tunnel_prefixes(values: dict[str, str | None]) -> tuple[str, ...]:
    """Stamp the trusted mode; a profile cannot select another identity source."""
    mode = auth_mode()
    declared = (values.get("AGENT_AUTH_MODE") or mode).strip().lower()
    if declared != mode:
        raise ProviderError(
            "deployment_profile_invalid",
            "The host account mode must match the control-plane deployment",
        )
    values["AGENT_AUTH_MODE"] = mode
    prefixes = ("AGENT_MCP_TUNNEL", "AGENT_CONTROL_PLANE_TUNNEL")
    if mode == "gateway":
        return ("AGENT_TUNNEL", *prefixes)
    if values.get("AGENT_GATEWAY_URL") or any(
        value for name, value in values.items() if name.startswith("AGENT_TUNNEL_")
    ):
        raise ProviderError(
            "deployment_profile_invalid",
            "A local-account host profile must not configure a Gateway inference relay",
        )
    return prefixes
