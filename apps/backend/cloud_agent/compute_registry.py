"""Assemble provider capabilities and their restricted administrator forms."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Literal

from cryptography.exceptions import UnsupportedAlgorithm
from cryptography.hazmat.primitives import serialization

from cloud_agent.domain.compute import ComputeRefusal
from cloud_agent.providers.compute.cloudlab import CloudLabProvider
from cloud_agent.providers.compute.existing_ssh import ExistingSSHProvider

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from cloud_agent.compute_config import ComputeConfiguration
    from cloud_agent.ports.external.compute_provider import ComputeProvider, SecretResolver
    from cloud_agent.ports.external.host_bootstrap import SshTransport


@dataclass(frozen=True)
class ComputeProviderRegistration:
    """A vendor entry; application commands depend on declared capabilities."""

    id: str
    label: str
    provider: ComputeProvider
    fields: tuple[dict[str, Any], ...]
    ssh_credential_fields: tuple[str, str] | None = None
    connection_fields: tuple[dict[str, Any], ...] = ()
    secret_label: str = "API token"
    credential_kind: Literal["ssh_key", "api_token"] = "api_token"
    connection_probe: Callable[[str], Awaitable[None]] | None = None
    can_import: bool = False

    def configuration(
        self, supplied: dict[str, Any], connection_id: str, ssh_connection_id: str | None
    ) -> dict[str, Any]:
        """Inject credential references from authorized connection selections."""
        allowed = {field["key"] for field in self.fields}
        if set(supplied) - allowed:
            raise ComputeRefusal(
                "invalid_configuration", "Unsupported provider configuration field.", status=422
            )
        result = {**supplied, "credential_ref": connection_id}
        if not ssh_connection_id:
            for field in self.fields:
                if field.get("deployment_only"):
                    result.pop(field["key"], None)
        if self.ssh_credential_fields is not None:
            if ssh_connection_id:
                private, known = self.ssh_credential_fields
                result[private] = ssh_connection_id
                result[known] = f"{ssh_connection_id}:known_hosts"
        elif self.credential_kind == "ssh_key":
            result["known_hosts_ref"] = f"{connection_id}:known_hosts"
        return result

    def catalog(self) -> dict[str, Any]:
        """Describe supported operations without code or credentials in the form."""
        capabilities = self.provider.capabilities()
        return {
            "id": self.id,
            "label": self.label,
            "modes": capabilities.acquisition_modes,
            "capabilities": {
                "extend": capabilities.can_extend,
                "release": capabilities.can_release,
                "import": self.can_import,
            },
            "fields": self.fields,
            "connection_fields": self.connection_fields,
            "secret_label": self.secret_label,
            "requires_ssh_connection": self.ssh_credential_fields is not None,
        }

    def validate_secret(self, configuration: dict[str, Any], secret: str) -> dict[str, Any]:
        """Validate write-only material without logging or returning its contents."""
        if self.credential_kind == "api_token":
            if configuration or not secret.strip() or any(ch in secret for ch in "\r\n"):
                raise ComputeRefusal(
                    "invalid_credential",
                    "API token or connection configuration is invalid.",
                    status=422,
                )
            return {}
        if set(configuration) - {"known_hosts"}:
            raise ComputeRefusal(
                "invalid_configuration",
                "Only verified SSH host keys belong in this connection.",
                status=422,
            )
        known = configuration.get("known_hosts", "")
        if not isinstance(known, str) or len(known) > 65536:
            raise ComputeRefusal("invalid_host_keys", "SSH host-key list is invalid.", status=422)
        try:
            for line in known.splitlines():
                if not line.strip() or line.lstrip().startswith("#"):
                    continue
                parts = line.split()
                if parts[0].startswith("@"):
                    parts = parts[1:]
                if len(parts) < 3:
                    raise ValueError("missing key")
                serialization.load_ssh_public_key(f"{parts[1]} {parts[2]}".encode())
            try:
                serialization.load_ssh_private_key(secret.encode(), password=None)
            except ValueError:
                serialization.load_pem_private_key(secret.encode(), password=None)
        except (ValueError, TypeError, UnsupportedAlgorithm):
            raise ComputeRefusal(
                "invalid_ssh_key",
                "Provide a supported private key without a passphrase and valid verified host keys.",
                status=422,
            ) from None
        return {"known_hosts": known}

    async def validate_connection(self, reference: str, configuration: dict[str, Any]) -> None:
        """Check API access or SSH key configuration without allocating a machine."""
        if self.connection_probe is not None:
            await self.connection_probe(reference)
        elif (
            self.credential_kind == "ssh_key"
            and not str(configuration.get("known_hosts", "")).strip()
        ):
            raise ComputeRefusal(
                "host_keys_required",
                "Configure verified SSH host keys; target connectivity is checked by server preflight.",
                status=422,
            )


class ComputeProviderRegistry:
    """Resolve configured children without provider branches in Host operations."""

    def __init__(self, entries: list[ComputeProviderRegistration]) -> None:
        self.entries = {entry.id: entry for entry in entries}

    def get(self, provider_id: str) -> ComputeProviderRegistration:
        """Refuse a missing implementation or an unconfigured cloud endpoint."""
        entry = self.entries.get(provider_id)
        if entry is None:
            raise ComputeRefusal(
                "provider_unavailable", "This provider is not configured on the control plane."
            )
        return entry


def build_registry(
    configuration: ComputeConfiguration, *, resolver: SecretResolver, transport: SshTransport
) -> ComputeProviderRegistry:
    """Register only working adapters; future vendors add one entry here."""
    entries = [
        ComputeProviderRegistration(
            id="existing_ssh",
            label="Existing SSH server",
            provider=ExistingSSHProvider(transport),
            secret_label="SSH private key",
            credential_kind="ssh_key",
            fields=(
                {"key": "address", "label": "SSH address", "type": "text", "required": True},
                {
                    "key": "port",
                    "label": "SSH port",
                    "type": "number",
                    "default": 22,
                    "min": 1,
                    "max": 65535,
                },
                {"key": "username", "label": "SSH user", "type": "text", "required": True},
            ),
            connection_fields=(
                {
                    "key": "known_hosts",
                    "label": "Verified SSH host keys",
                    "type": "textarea",
                    "description": "Known-hosts lines verified with the server owner. Required before SSH access; unknown keys are never accepted automatically.",
                },
            ),
        )
    ]
    if configuration.cloudlab_api_url:
        cloudlab = CloudLabProvider(api_url=configuration.cloudlab_api_url, resolver=resolver)
        entries.append(
            ComputeProviderRegistration(
                id="cloudlab",
                label="CloudLab",
                provider=cloudlab,
                connection_probe=cloudlab.validate_connection,
                can_import=True,
                ssh_credential_fields=("ssh_credential_ref", "ssh_known_hosts_ref"),
                fields=(
                    {"key": "project", "label": "Project", "type": "text", "required": True},
                    {
                        "key": "external_id",
                        "label": "Experiment UUID",
                        "type": "text",
                        "required": True,
                        "modes": ["attach"],
                    },
                    {
                        "key": "name",
                        "label": "Experiment name",
                        "type": "text",
                        "modes": ["provision"],
                    },
                    {
                        "key": "profile_project",
                        "label": "Profile project",
                        "type": "text",
                        "required": True,
                        "modes": ["provision"],
                    },
                    {
                        "key": "profile_name",
                        "label": "Profile name",
                        "type": "text",
                        "required": True,
                        "modes": ["provision"],
                    },
                    {
                        "key": "duration",
                        "label": "Initial duration (hours)",
                        "type": "number",
                        "default": 4,
                        "min": 1,
                        "max": 168,
                        "modes": ["provision"],
                    },
                    {
                        "key": "bindings",
                        "label": "Profile parameters",
                        "type": "json",
                        "default": {},
                        "modes": ["provision"],
                    },
                    {"key": "group", "label": "Project group", "type": "text"},
                    {
                        "key": "ssh_username",
                        "label": "CloudLab SSH user",
                        "type": "text",
                        "deployment_only": True,
                    },
                ),
            )
        )
    return ComputeProviderRegistry(entries)
