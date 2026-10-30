"""Every stored value the two credential encryption keys protect, by table and column.

The one list a credential key rotation re-encrypts from. A family is a column
holding one ciphertext (``text``) or a JSONB array whose elements each carry a
``value_ciphertext`` (``json_values``), in one cipher domain. Tests enumerate
the migrated schema's ciphertext columns and the code's cipher domains and
require each to be here or to belong to another key, so a new table cannot
quietly stay on a retired key.

Lookup digests are not here: the environment installation fingerprints are
rebuilt by their own step (:mod:`cloud_agent.application.credential_key_rotation`).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from cloud_agent.application.model_connections import (
    MODEL_CONNECTION_KEY_DOMAIN,
    MODEL_CONNECTION_SCAN_DOMAIN,
)
from cloud_agent.application.ssh_servers import SSH_SERVER_KEY_DOMAIN
from cloud_agent.compute_secrets import _DOMAIN as COMPUTE_DOMAIN
from cloud_agent.mcp.registry import (
    MCP_CREDENTIAL_DOMAIN,
    MCP_INVOCATION_DOMAIN,
    MCP_OAUTH_DOMAIN,
)
from cloud_agent.source_control import SOURCE_CONTROL_DOMAIN
from cloud_agent_shared.environment import ENVIRONMENT_DOMAIN

KeyName = Literal["user", "compute"]


@dataclass(frozen=True)
class Family:
    """One column of ciphertexts under one key and domain."""

    name: str
    key: KeyName
    domain: bytes
    table: str
    #: An expression naming one row, unique; compared as text for the batch cursor.
    row_key: str
    column: str
    shape: Literal["text", "json_values"] = "text"


FAMILIES: tuple[Family, ...] = (
    Family("gitlab_access", "user", SOURCE_CONTROL_DOMAIN, "agent_gitlab_connections", "id", "access_token_ciphertext"),
    Family("gitlab_refresh", "user", SOURCE_CONTROL_DOMAIN, "agent_gitlab_connections", "id", "refresh_token_ciphertext"),
    Family("github_access", "user", SOURCE_CONTROL_DOMAIN, "agent_github_accounts", "user_id", "access_token_ciphertext"),
    Family("github_refresh", "user", SOURCE_CONTROL_DOMAIN, "agent_github_accounts", "user_id", "refresh_token_ciphertext"),
    Family("oauth_state_verifier", "user", SOURCE_CONTROL_DOMAIN, "agent_oauth_states", "state_hash", "code_verifier_ciphertext"),
    Family("mcp_access", "user", MCP_CREDENTIAL_DOMAIN, "agent_mcp_connections", "id", "access_token_ciphertext"),
    Family("mcp_refresh", "user", MCP_CREDENTIAL_DOMAIN, "agent_mcp_connections", "id", "refresh_token_ciphertext"),
    Family("mcp_client_secret", "user", MCP_OAUTH_DOMAIN, "agent_mcp_connections", "id", "client_secret_ciphertext"),
    Family("mcp_flow_client_secret", "user", MCP_OAUTH_DOMAIN, "agent_mcp_oauth_flows", "state_hash", "client_secret_ciphertext"),
    Family("mcp_flow_verifier", "user", MCP_OAUTH_DOMAIN, "agent_mcp_oauth_flows", "state_hash", "code_verifier_ciphertext"),
    Family("mcp_invocation_arguments", "user", MCP_INVOCATION_DOMAIN, "agent_mcp_invocations", "id", "arguments_ciphertext"),
    Family("environment_values", "user", ENVIRONMENT_DOMAIN, "agent_environment_version_variables", "version_id || '/' || name", "value_ciphertext"),
    Family("binding_overrides", "user", ENVIRONMENT_DOMAIN, "agent_environment_bindings", "id", "overrides", "json_values"),
    Family("binding_runtime_values", "user", ENVIRONMENT_DOMAIN, "agent_environment_bindings", "id", "runtime_variables", "json_values"),
    Family("binding_runtime_history", "user", ENVIRONMENT_DOMAIN, "agent_environment_bindings", "id", "runtime_secret_history", "json_values"),
    Family("ssh_server_key", "user", SSH_SERVER_KEY_DOMAIN, "agent_ssh_servers", "id", "private_key_ciphertext"),
    Family("ssh_server_pending_key", "user", SSH_SERVER_KEY_DOMAIN, "agent_ssh_servers", "id", "pending_private_key_ciphertext"),
    Family("model_connection_key", "user", MODEL_CONNECTION_KEY_DOMAIN, "agent_model_connections", "id", "api_key_ciphertext"),
    Family("model_connection_scan_key", "user", MODEL_CONNECTION_SCAN_DOMAIN, "agent_model_connection_scan_keys", "id", "key_ciphertext"),
    Family("compute_connection", "compute", COMPUTE_DOMAIN, "agent_compute_connections", "id", "secret_ciphertext"),
    Family("compute_profile", "compute", COMPUTE_DOMAIN, "agent_compute_deployment_profiles", "id", "secret_ciphertext"),
)  # fmt: skip

#: Ciphertext columns protected by something other than these two keys.
OTHER_KEYS: dict[str, str] = {
    "agent_deploy_intents.sealed": "each deployment agent's own public key",
}


def families(key: KeyName) -> tuple[Family, ...]:
    """The families one key protects, in a fixed order."""
    return tuple(f for f in FAMILIES if f.key == key)


__all__ = ["FAMILIES", "OTHER_KEYS", "Family", "families"]
