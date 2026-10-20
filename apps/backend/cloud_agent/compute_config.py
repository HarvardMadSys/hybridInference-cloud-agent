"""Trusted deployment profiles and file-backed deployment secrets.

The control plane selects an available configured profile by default; API
clients may name one explicitly. Browser requests cannot supply shell fragments,
environment files, bundle paths, or deployment credentials.
"""

from __future__ import annotations

import asyncio
import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING
from urllib.parse import urlsplit, urlunsplit

from pydantic import ValidationError

from cloud_agent.compute_secrets import ComputeSecretError, StoredDeploymentResolver
from cloud_agent.ports.external.host_bootstrap import DeploymentProfile
from cloud_agent.settings import integer_setting, setting_env, text_setting

if TYPE_CHECKING:
    from cloud_agent.compute_secrets import ComputeSecretStore

ENV_COMPUTE_CONFIG = "AGENT_COMPUTE_CONFIG"
ENV_CLOUDLAB_API_URL = "AGENT_CLOUDLAB_API_URL"
ENV_REGISTRATION_URL = "AGENT_COMPUTE_REGISTRATION_URL"
REGISTRATION_PATH = "/v1/compute/node-registrations"


def registration_url() -> str:
    """Where a new node posts its host keys: explicit, or the public API origin.

    Reuses the operator-configured sign-in callback (as MCP OAuth does), never
    request headers. Only HTTPS qualifies: the node authenticates the control
    plane by its certificate before sending the token.
    """
    explicit = (setting_env(ENV_REGISTRATION_URL) or "").strip()
    if explicit:
        return explicit if urlsplit(explicit).scheme == "https" else ""
    callback = (setting_env("AGENT_SESSION_REDIRECT_URI") or "").strip()
    suffix = "/v1/session/callback"
    try:
        parsed = urlsplit(callback)
    except ValueError:
        return ""
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
        or not parsed.path.endswith(suffix)
    ):
        return ""
    base = parsed.path[: -len(suffix)]
    return urlunsplit((parsed.scheme, parsed.netloc, base + REGISTRATION_PATH, "", ""))


@dataclass(frozen=True)
class ComputeConfiguration:
    """Operator-supplied configuration, never accepted through the Admin API.

    The endpoint and the two intervals are what the saved settings start
    from; once settings have loaded, the saved values apply.
    """

    profiles: dict[str, DeploymentProfile] = field(default_factory=dict)
    deployment_secrets: dict[str, str] = field(default_factory=dict)
    cloudlab_api_url: str = ""
    poll_seconds: int = 30
    observation_seconds: int = 120

    @classmethod
    def from_env(cls) -> ComputeConfiguration:
        """Load the optional mounted configuration with no secret-value fallback."""
        location = os.environ.get(ENV_COMPUTE_CONFIG, "")
        if not location:
            return cls(cloudlab_api_url=os.environ.get(ENV_CLOUDLAB_API_URL, ""))
        try:
            path = Path(location)
            if not path.is_absolute() or path.stat().st_size > 1024 * 1024:
                raise ValueError("invalid file")
            document = json.loads(path.read_text())
            if not isinstance(document, dict) or set(document) - {
                "deployment_profiles",
                "deployment_secrets",
                "poll_seconds",
                "observation_seconds",
            }:
                raise ValueError("invalid configuration")
            secrets = document.get("deployment_secrets", {})
            if not isinstance(secrets, dict) or any(
                not isinstance(name, str)
                or not name
                or ":" in name
                or not isinstance(value, str)
                or not Path(value).is_absolute()
                for name, value in secrets.items()
            ):
                raise ValueError("invalid secret references")
            profiles: dict[str, DeploymentProfile] = {}
            for value in document.get("deployment_profiles", []):
                profile = DeploymentProfile.model_validate(value)
                if profile.id in profiles or not Path(profile.source_bundle).is_absolute():
                    raise ValueError("invalid profile identity")
                for reference in (profile.environment_ref, *profile.files.values()):
                    if not reference.startswith("deployment:") or reference[11:] not in secrets:
                        raise ValueError("deployment secrets must be explicitly configured")
                profiles[profile.id] = profile
            poll = int(document.get("poll_seconds", 30))
            observation = int(document.get("observation_seconds", 120))
            if not 5 <= poll <= 300 or not 30 <= observation <= 300:
                raise ValueError("invalid observation interval")
        except (OSError, ValueError, TypeError, ValidationError):
            raise RuntimeError(
                "invalid compute configuration; check AGENT_COMPUTE_CONFIG"
            ) from None
        return cls(
            profiles=profiles,
            deployment_secrets=secrets,
            cloudlab_api_url=os.environ.get(ENV_CLOUDLAB_API_URL, ""),
            poll_seconds=poll,
            observation_seconds=observation,
        )


def current_cloudlab_api_url(configuration: ComputeConfiguration) -> str:
    """Resolve the endpoint at request time, so a saved change applies at once."""
    return text_setting("compute.cloudlab_api_url", configuration.cloudlab_api_url).strip()


def current_poll_seconds(configuration: ComputeConfiguration) -> int:
    """Read the scheduler cadence at each pass."""
    return integer_setting("compute.poll_seconds", configuration.poll_seconds)


def current_observation_seconds(configuration: ComputeConfiguration) -> int:
    """Read the inspection cadence at each pass."""
    return integer_setting("compute.observation_seconds", configuration.observation_seconds)


class ComputeSecretResolver:
    """Keep deployment credentials in an operator-owned namespace."""

    def __init__(
        self, connections: ComputeSecretStore, configuration: ComputeConfiguration
    ) -> None:
        self.connections = connections
        self._paths = dict(configuration.deployment_secrets)
        self._stored = StoredDeploymentResolver(connections.repository, connections)

    async def resolve(self, ref: str) -> str:
        """Resolve only configured deployment files or registered connection IDs."""
        if not ref.startswith("deployment:"):
            if ref.startswith(StoredDeploymentResolver.PREFIX):
                return await self._stored.resolve(ref)
            return await self.connections.resolve(ref)
        path = self._paths.get(ref[11:])
        if path is None:
            raise ComputeSecretError("deployment credential reference is not configured")
        try:
            return await asyncio.to_thread(self._read, path)
        except (OSError, UnicodeError, ValueError):
            raise ComputeSecretError("deployment credential cannot be read") from None

    async def bundle(self, ref: str) -> bytes:
        """Resolve a bundle registered through the trusted Admin profile API."""
        if not ref.startswith(StoredDeploymentResolver.PREFIX):
            raise ComputeSecretError("deployment bundle reference is invalid")
        return await self._stored.bundle(ref)

    @staticmethod
    def _read(path: str) -> str:
        with Path(path).open("rb") as handle:
            data = handle.read(1024 * 1024 + 1)
        if len(data) > 1024 * 1024:
            raise ValueError("deployment credential exceeds size limit")
        return data.decode()
