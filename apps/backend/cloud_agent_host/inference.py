"""The deployment's inference source, never inferred from its management URL."""

from __future__ import annotations

import os


def local_accounts_host() -> bool:
    """Read the trusted deployment mode shared with the control plane."""
    mode = (os.environ.get("AGENT_AUTH_MODE") or "gateway").strip().lower()
    if mode not in {"gateway", "local"}:
        raise ValueError("AGENT_AUTH_MODE must be 'gateway' or 'local'")
    return mode == "local"


def inference_gateway_url(value: str) -> str:
    """Require a Gateway only on a Gateway deployment; local has none."""
    value = value.strip()
    if local_accounts_host():
        if value:
            raise ValueError("a local-account host must not configure AGENT_GATEWAY_URL")
    elif not value:
        raise ValueError("AGENT_GATEWAY_URL is required in gateway mode")
    return value


def validate_resident_inference(gateway_url: str, source: str) -> None:
    """A local member must carry its owner's connection, never a Gateway grant."""
    inference_gateway_url(gateway_url)
    if local_accounts_host() and not source:
        raise ValueError("a local-account resident requires an owner model connection")
