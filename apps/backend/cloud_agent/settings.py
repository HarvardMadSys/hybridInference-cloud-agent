"""Validated, database-backed application settings shared by both listeners.

The environment is imported once when the settings row is first created.
After that, the database is authoritative, including resets to registry defaults.
Secrets are encrypted under a separate domain and never returned to the browser.
Only database access, the settings root key, recovery access and process
infrastructure remain bootstrap. Runtime identity and service keys are persisted.
"""

from __future__ import annotations

import asyncio
import math
import os
import re
import threading
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Literal
from urllib.parse import urlsplit

from pydantic import AnyHttpUrl, ValidationError

if TYPE_CHECKING:
    from collections.abc import Mapping

    from cloud_agent.ports.settings import SettingsRepository

SettingValue = str | int | float | bool
SettingKind = Literal["string", "boolean", "integer", "number"]


@dataclass(frozen=True)
class Setting:
    """An admin-editable value and its public validation rules."""

    key: str
    label: str
    category: str
    default: SettingValue
    kind: SettingKind = "string"
    env: str | None = None
    description: str = "Applies to subsequent operations after settings synchronize."
    minimum: float | None = None
    maximum: float | None = None
    choices: tuple[str, ...] = ()
    secret: bool = False
    url: bool = False
    minimum_bytes: int = 0

    def validate(self, value: Any) -> SettingValue:
        """Return a checked value, raising a message that contains no supplied data."""
        valid = True
        if self.kind == "boolean":
            valid = isinstance(value, bool)
        elif self.kind in {"number", "integer"}:
            try:
                valid = (
                    isinstance(value, (int, float))
                    and not isinstance(value, bool)
                    and math.isfinite(value)
                    and (self.kind != "integer" or isinstance(value, int))
                )
            except OverflowError:
                valid = False
            if valid:
                valid = (self.minimum is None or value >= self.minimum) and (
                    self.maximum is None or value <= self.maximum
                )
        else:
            valid = isinstance(value, str) and "\x00" not in value
            if valid:
                valid = len(value) <= (131072 if self.secret else 4096)
            if valid and value and self.minimum_bytes:
                valid = len(value.encode()) >= self.minimum_bytes
            if valid and self.key == "gateway_issuer" and value:
                valid = value == value.strip() and not any(ord(char) < 32 for char in value)
            if valid and self.choices:
                valid = value in self.choices
            if valid and self.key == "gateway_grant_base_path":
                # Grant clients validate this during startup as well as live
                # requests. Reject an unusable prefix before it can be saved.
                valid = (
                    value.rstrip("/").startswith("/")
                    and not value.startswith("//")
                    and not any(char in value for char in ("?", "#", "\\"))
                    and not any(char.isspace() or ord(char) < 32 for char in value)
                )
            if valid and value and self.key == "sandbox_archive.bucket":
                valid = bool(re.fullmatch(r"[a-z0-9][a-z0-9.-]{1,61}[a-z0-9]", value)) and (
                    not re.fullmatch(r"\d{1,3}(?:\.\d{1,3}){3}", value)
                    and not any(part in value for part in ("..", ".-", "-."))
                    and not value.startswith(("xn--", "sthree-", "amzn-s3-demo-"))
                    and not value.endswith(("-s3alias", "--ol-s3", ".mrap", "--x-s3", "--table-s3"))
                )
            if valid and value and self.key == "sandbox_archive.region":
                valid = bool(re.fullmatch(r"[a-z0-9-]{1,64}", value))
            if valid and value and self.key == "sandbox_archive.prefix":
                valid = (
                    len(value.encode()) <= 512
                    and bool(re.fullmatch(r"[a-zA-Z0-9_./-]+", value))
                    and all(part and part not in {".", ".."} for part in value.split("/"))
                )
            if valid and self.url and value:
                try:
                    if any(
                        char.isspace() or ord(char) < 32 or 127 <= ord(char) <= 159
                        for char in value
                    ):
                        raise ValueError("A URL cannot contain whitespace or control characters")
                    # The browser uses a WHATWG URL parser for saved appearance
                    # links. urlsplit alone accepts malformed hosts (such as
                    # percent signs or spaces) that would break the root layout.
                    AnyHttpUrl(value)
                    parsed = urlsplit(value)
                    valid = (
                        parsed.scheme in {"http", "https"}
                        and bool(parsed.hostname)
                        and not parsed.username
                        and not parsed.password
                        and not parsed.fragment
                    )
                    _ = parsed.port
                    if self.key in {
                        "compute.cloudlab_api_url",
                        "compute.registration_url",
                        "mcp_registry_url",
                        "sandbox_archive.endpoint",
                    }:
                        valid = valid and parsed.scheme == "https" and not parsed.query
                except (ValueError, ValidationError):
                    valid = False
        if not valid:
            if self.key == "sandbox_archive.bucket":
                raise ValueError(
                    f"Invalid value for {self.label} ({self.key}). "
                    "Use a bucket name such as freeinference; set cloud-agent/sandbox "
                    "in Sandbox archive S3 prefix."
                )
            raise ValueError(f"Invalid value for {self.label} ({self.key})")
        return value

    def definition(self) -> dict[str, Any]:
        """The public editor definition, without environment or credential values."""
        result: dict[str, Any] = {
            "key": self.key,
            "label": self.label,
            "category": self.category,
            "description": self.description,
            "kind": self.kind,
            "default": self.default,
            "secret": self.secret,
        }
        if self.minimum is not None:
            result["minimum"] = self.minimum
        if self.maximum is not None:
            result["maximum"] = self.maximum
        if self.choices:
            result["choices"] = list(self.choices)
        if self.minimum_bytes:
            result["minimum_bytes"] = self.minimum_bytes
        return result


def _numeric(
    key: str,
    label: str,
    category: str,
    default: int | float,
    minimum: float,
    maximum: float,
    *,
    env: str | None = None,
    description: str = "Applies to subsequent operations after settings synchronize.",
) -> Setting:
    return Setting(
        key,
        label,
        category,
        default,
        kind="integer" if isinstance(default, int) else "number",
        env=env,
        minimum=minimum,
        maximum=maximum,
        description=description,
    )


SETTINGS: tuple[Setting, ...] = (
    Setting(
        "log_level",
        "Control-plane log level",
        "Operations",
        "INFO",
        env="AGENT_LOG_LEVEL",
        choices=("NOTSET", "DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"),
        description="Applies to logging owned by the control plane after settings synchronize. Host process logs remain deployment configuration.",
    ),
    Setting(
        "log_format",
        "Control-plane log format",
        "Operations",
        "text",
        env="AGENT_LOG_FORMAT",
        choices=("text", "json"),
        description="Text or structured JSON output. Applies to the control plane's own log handler without a restart.",
    ),
    _numeric(
        "gateway_request_timeout_s", "Gateway request timeout (seconds)", "Gateway", 10.0, 0.1, 300
    ),
    _numeric(
        "gateway_connect_timeout_s",
        "Gateway connection timeout (seconds)",
        "Gateway",
        5.0,
        0.1,
        300,
    ),
    _numeric(
        "github_reload_interval_s",
        "GitHub configuration retry interval (seconds)",
        "Source control",
        30.0,
        1,
        3600,
    ),
    _numeric(
        "github_repository_cache_s",
        "GitHub repository cache duration (seconds)",
        "Source control",
        60.0,
        1,
        3600,
    ),
    _numeric(
        "github_request_timeout_s",
        "GitHub request timeout (seconds)",
        "Source control",
        30.0,
        0.1,
        300,
    ),
    _numeric(
        "gitlab_request_timeout_s",
        "GitLab request timeout (seconds)",
        "Source control",
        20.0,
        0.1,
        300,
    ),
    _numeric(
        "publish_poll_seconds",
        "Publication retry interval (seconds)",
        "Publication",
        15.0,
        0.1,
        3600,
    ),
    _numeric(
        "publish_idle_seconds",
        "Publication idle check interval (seconds)",
        "Publication",
        30.0,
        0.1,
        3600,
    ),
    _numeric(
        "publish_git_timeout_s", "Publication Git timeout (seconds)", "Publication", 120.0, 0.1, 300
    ),
    _numeric(
        "publish_pr_timeout_s", "Pull request API timeout (seconds)", "Publication", 30.0, 0.1, 300
    ),
    Setting(
        "pr_archive_enabled",
        "Archive sessions after PR merge",
        "Publication",
        True,
        kind="boolean",
        env="AGENT_PR_ARCHIVE_ENABLED",
        description="Archive settled sessions when their current published GitHub PR is verified merged. Preserves history and shared workspaces.",
    ),
    _numeric(
        "pr_archive_poll_s",
        "PR merge check interval (seconds)",
        "Publication",
        30.0,
        5,
        3600,
        env="AGENT_PR_ARCHIVE_POLL_S",
    ),
    _numeric(
        "pr_archive_timeout_s",
        "PR merge API timeout (seconds)",
        "Publication",
        10.0,
        0.1,
        60,
    ),
    _numeric(
        "pr_archive_batch_size",
        "PR merge checks per batch",
        "Publication",
        20,
        1,
        500,
    ),
    _numeric(
        "pr_archive_retry_max_s",
        "Maximum PR merge retry delay (seconds)",
        "Publication",
        3600.0,
        30,
        86400,
    ),
    _numeric(
        "pr_archive_max_failures",
        "PR merge integration failures before blocking",
        "Publication",
        10,
        1,
        100,
        description="Block repeated access or invalid-response failures. Temporary outages and rate limits keep retrying automatically.",
    ),
    Setting(
        "mcp_registry_url",
        "MCP registry URL",
        "MCP",
        "https://registry.modelcontextprotocol.io",
        url=True,
    ),
    _numeric(
        "mcp_catalog_sync_seconds",
        "MCP catalog refresh interval (seconds)",
        "MCP",
        3600.0,
        1,
        86400,
    ),
    _numeric(
        "mcp_catalog_retry_seconds", "MCP catalog retry interval (seconds)", "MCP", 300.0, 1, 3600
    ),
    _numeric(
        "mcp_catalog_sync_timeout_s",
        "MCP catalog refresh timeout (seconds)",
        "MCP",
        240.0,
        0.1,
        600,
    ),
    _numeric(
        "mcp_catalog_request_timeout_s",
        "MCP catalog request timeout (seconds)",
        "MCP",
        15.0,
        0.1,
        300,
    ),
    _numeric(
        "mcp_catalog_storage_timeout_s",
        "MCP catalog storage timeout (seconds)",
        "MCP",
        10.0,
        0.1,
        300,
    ),
    _numeric("mcp_request_timeout_s", "MCP request timeout (seconds)", "MCP", 120.0, 0.1, 300),
    _numeric("mcp_connect_timeout_s", "MCP connection timeout (seconds)", "MCP", 10.0, 0.1, 300),
    _numeric("mcp_metadata_timeout_s", "MCP metadata timeout (seconds)", "MCP", 20.0, 0.1, 300),
    Setting(
        "web_search_provider",
        "Web search provider",
        "Web search",
        "auto",
        env="WEB_SEARCH_PROVIDER",
        choices=("auto", "serper", "brave", "parallel"),
        description=(
            "Selects the provider behind the built-in web_search tool. Auto prefers Serper "
            "then Brave, then Parallel when multiple provider keys are configured."
        ),
    ),
    Setting(
        "serper_api_key",
        "Serper API key",
        "Web search",
        "",
        env="SERPER_API_KEY",
        secret=True,
        description=(
            "Enables Serper.dev for the built-in web_search tool. The key stays in the "
            "control plane and never enters an agent sandbox."
        ),
    ),
    Setting(
        "brave_search_api_key",
        "Brave Search API key",
        "Web search",
        "",
        env="BRAVE_SEARCH_API_KEY",
        secret=True,
        description=(
            "Enables Brave Search for the built-in web_search tool. Review Brave's result "
            "retention and end-user terms before enabling it. Queries go only to Brave, and "
            "the key never enters an agent sandbox."
        ),
    ),
    Setting(
        "parallel_api_key",
        "Parallel API key",
        "Web search",
        "",
        env="PARALLEL_API_KEY",
        secret=True,
        description=(
            "Enables Parallel Search for the built-in web_search tool. Review Parallel's "
            "data-use, retention, and end-user terms before enabling it. Queries go only "
            "to Parallel, and the key never enters an agent sandbox."
        ),
    ),
    Setting(
        "site.app_name", "Application name", "Appearance", "Cloud Agent", env="AGENT_WEB_APP_NAME"
    ),
    Setting("site.org_name", "Organization name", "Appearance", "", env="AGENT_WEB_ORG_NAME"),
    Setting(
        "site.org_url", "Organization URL", "Appearance", "", env="AGENT_WEB_ORG_URL", url=True
    ),
    Setting(
        "site.status_url", "Status page URL", "Appearance", "", env="AGENT_WEB_STATUS_URL", url=True
    ),
    Setting(
        "site.gateway_url",
        "Gateway website URL",
        "Appearance",
        "",
        env="AGENT_WEB_GATEWAY_URL",
        url=True,
    ),
    Setting(
        "site.feature_rag",
        "Docs assistant",
        "Appearance",
        False,
        kind="boolean",
        env="AGENT_WEB_FEATURE_RAG",
    ),
    Setting(
        "compute.cloudlab_api_url",
        "CloudLab API URL",
        "Compute",
        "",
        env="AGENT_CLOUDLAB_API_URL",
        url=True,
    ),
    Setting(
        "compute.registration_url",
        "Compute node registration URL",
        "Compute",
        "",
        env="AGENT_COMPUTE_REGISTRATION_URL",
        url=True,
    ),
    _numeric(
        "compute.poll_seconds", "Compute scheduling interval (seconds)", "Compute", 30, 5, 300
    ),
    _numeric(
        "compute.observation_seconds",
        "Compute observation interval (seconds)",
        "Compute",
        120,
        30,
        300,
    ),
    Setting("gateway_base_url", "Gateway API URL", "Gateway", "", env="GATEWAY_BASE_URL", url=True),
    Setting(
        "gateway_dispatch_token",
        "Gateway dispatch token",
        "Gateway",
        "",
        env="GATEWAY_GRANT_DISPATCH_TOKEN",
        secret=True,
    ),
    Setting(
        "gateway_grant_base_path",
        "Grant API path",
        "Gateway",
        "/internal/agent-grants",
        env="GATEWAY_GRANT_BASE_PATH",
    ),
    Setting(
        "gateway_grant_binding_id",
        "Grant binding ID",
        "Gateway",
        "",
        env="GATEWAY_GRANT_BINDING_ID",
    ),
    Setting(
        "gateway_authorize_url",
        "Sign-in authorization URL",
        "Sign-in",
        "",
        env="GATEWAY_AUTHORIZE_URL",
        url=True,
    ),
    Setting(
        "session_redirect_uri",
        "Sign-in callback URL",
        "Sign-in",
        "",
        env="AGENT_SESSION_REDIRECT_URI",
        url=True,
    ),
    Setting("web_base_url", "Web app URL", "Sign-in", "", env="AGENT_WEB_BASE_URL", url=True),
    Setting(
        "repo_allowlist",
        "Repository allowlist",
        "Source control",
        "",
        env="AGENT_REPO_ALLOWLIST",
        description="Comma-separated owner/repository entries. Applies to new repository access decisions.",
    ),
    Setting("github_app_id", "GitHub App ID", "Source control", "", env="AGENT_GITHUB_APP_ID"),
    Setting(
        "github_app_client_id",
        "GitHub OAuth client ID",
        "Source control",
        "",
        env="AGENT_GITHUB_APP_CLIENT_ID",
    ),
    Setting(
        "github_app_client_secret",
        "GitHub OAuth client secret",
        "Source control",
        "",
        env="AGENT_GITHUB_APP_CLIENT_SECRET",
        secret=True,
    ),
    Setting(
        "github_app_private_key",
        "GitHub App private key",
        "Source control",
        "",
        env="AGENT_GITHUB_APP_PRIVATE_KEY",
        secret=True,
        description="PEM private key. When empty, the deployment's mounted key file is used.",
    ),
    Setting(
        "github_app_install_url",
        "GitHub installation URL",
        "Source control",
        "",
        env="AGENT_GITHUB_APP_INSTALL_URL",
        url=True,
    ),
    Setting(
        "github_api_url",
        "GitHub API URL",
        "Source control",
        "https://api.github.com",
        env="AGENT_GITHUB_API",
        url=True,
    ),
    Setting(
        "gitlab_client_id",
        "GitLab OAuth client ID",
        "Source control",
        "",
        env="AGENT_GITLAB_OAUTH_CLIENT_ID",
    ),
    Setting(
        "gitlab_client_secret",
        "GitLab OAuth client secret",
        "Source control",
        "",
        env="AGENT_GITLAB_OAUTH_CLIENT_SECRET",
        secret=True,
    ),
    Setting(
        "gitlab_redirect_uri",
        "GitLab callback URL",
        "Source control",
        "",
        env="AGENT_GITLAB_OAUTH_REDIRECT_URI",
        url=True,
    ),
    Setting(
        "github_publish_token",
        "GitHub publication token",
        "Publication",
        "",
        env="AGENT_GITHUB_TOKEN",
        secret=True,
    ),
    Setting(
        "publish_base_branch",
        "Default pull request base branch",
        "Publication",
        "dev",
        env="AGENT_PUBLISH_BASE_BRANCH",
    ),
    Setting(
        "git_author_name",
        "Commit author name",
        "Publication",
        "HybridInference Agent",
        env="AGENT_GIT_AUTHOR_NAME",
    ),
    Setting(
        "git_author_email",
        "Commit author email",
        "Publication",
        "agent@localhost",
        env="AGENT_GIT_AUTHOR_EMAIL",
    ),
    Setting(
        "mcp_public_base_url",
        "MCP OAuth callback base URL",
        "MCP",
        "",
        env="AGENT_MCP_PUBLIC_BASE_URL",
        url=True,
    ),
    Setting(
        "mcp_github_client_id",
        "GitHub MCP sign-in client ID",
        "MCP",
        "",
        env="AGENT_MCP_GITHUB_CLIENT_ID",
        description="Service-managed GitHub OAuth App or GitHub App used for MCP browser sign-in.",
    ),
    Setting(
        "mcp_github_client_secret",
        "GitHub MCP sign-in client secret",
        "MCP",
        "",
        env="AGENT_MCP_GITHUB_CLIENT_SECRET",
        secret=True,
        description="Register <public API base>/v1/agent/mcp/oauth/callback/github on this app.",
    ),
    Setting(
        "alert_webhook_url",
        "Alert webhook URL",
        "Operations",
        "",
        env="AGENT_ALERT_WEBHOOK_URL",
        secret=True,
        url=True,
    ),
    Setting(
        "oncall_user_id", "On-call service account", "Operations", "", env="AGENT_ONCALL_USER_ID"
    ),
    Setting(
        "oncall_dispatch_token",
        "On-call dispatch token",
        "Operations",
        "",
        env="AGENT_ONCALL_DISPATCH_TOKEN",
        secret=True,
    ),
    _numeric(
        "residency_ttl_s",
        "Residency lease duration (seconds)",
        "Operations",
        1800.0,
        120,
        86400,
        env="AGENT_RESIDENCY_TTL_S",
    ),
    _numeric("queue_alert_after_s", "Queue alert age (seconds)", "Operations", 600.0, 60, 86400),
    _numeric(
        "queue_monitor_interval_s", "Queue check interval (seconds)", "Operations", 60.0, 5, 3600
    ),
    _numeric(
        "queue_alert_cooldown_s",
        "Alert reminder interval (seconds)",
        "Operations",
        1800.0,
        60,
        86400,
    ),
    _numeric("max_attempts", "Maximum job attempts", "Operations", 3, 1, 20),
    _numeric("grant_renewal_concurrency", "Concurrent grant renewals", "Operations", 16, 1, 128),
    _numeric("ledger_refresh_concurrency", "Concurrent usage refreshes", "Operations", 4, 1, 128),
    _numeric(
        "ledger_refresh_timeout_s", "Usage refresh timeout (seconds)", "Operations", 15.0, 1, 120
    ),
    _numeric("ledger_idle_s", "Usage idle check interval (seconds)", "Operations", 30.0, 1, 300),
    _numeric(
        "snapshot_idle_s", "Snapshot idle check interval (seconds)", "Operations", 30.0, 1, 300
    ),
    _numeric(
        "snapshot_capture_interval_s", "Snapshot interval (seconds)", "Operations", 5.0, 0.1, 300
    ),
    _numeric(
        "capacity_history_retention_s",
        "Capacity history retention (seconds)",
        "Operations",
        86400.0,
        3600,
        31536000,
    ),
    _numeric(
        "attachments.max_file_bytes",
        "Maximum upload file size (bytes)",
        "Attachments",
        5242880,
        1,
        5242880,
    ),
    _numeric(
        "attachments.max_total_bytes",
        "Maximum upload total size (bytes)",
        "Attachments",
        20971520,
        1,
        20971520,
    ),
    _numeric("attachments.max_files", "Maximum upload file count", "Attachments", 50, 1, 50),
    _numeric("skills.max_skill_bytes", "Maximum skill size (bytes)", "Skills", 65536, 1, 65536),
    _numeric(
        "skills.max_selection_bytes",
        "Maximum selected skill size (bytes)",
        "Skills",
        131072,
        1,
        131072,
    ),
    _numeric("skills.max_selected", "Maximum selected skills", "Skills", 16, 1, 16),
    _numeric("skills.max_library", "Maximum library skills", "Skills", 100, 1, 100),
    _numeric("goals.arm_after_seconds", "Goal activation delay (seconds)", "Goals", 60.0, 1, 3600),
    _numeric(
        "goals.stalled_after_seconds", "Goal stall timeout (seconds)", "Goals", 900.0, 60, 86400
    ),
    _numeric("goals.max_wakeup_attempts", "Maximum goal wakeup attempts", "Goals", 5, 1, 100),
    _numeric(
        "goals.wakeup_backoff_seconds", "Goal wakeup retry delay (seconds)", "Goals", 5.0, 0.1, 300
    ),
    _numeric(
        "goals.wakeup_backoff_max_seconds",
        "Maximum goal retry delay (seconds)",
        "Goals",
        300.0,
        1,
        3600,
    ),
    _numeric("task_titles.max_chars", "Maximum task title length", "Task titles", 36, 8, 36),
    _numeric(
        "task_titles.input_max_chars", "Maximum title input length", "Task titles", 4000, 1, 4000
    ),
    _numeric(
        "task_titles.timeout_seconds",
        "Title request timeout (seconds)",
        "Task titles",
        10.0,
        0.1,
        120,
    ),
    _numeric(
        "task_titles.total_timeout_seconds",
        "Title generation time limit (seconds)",
        "Task titles",
        120.0,
        1,
        600,
    ),
    _numeric("task_titles.max_attempts", "Maximum title attempts", "Task titles", 5, 1, 5),
    _numeric("task_titles.max_tokens", "Maximum title output tokens", "Task titles", 256, 16, 256),
)
MIGRATED_SETTINGS: tuple[Setting, ...] = (
    Setting(
        "sandbox_archive.bucket",
        "Sandbox archive S3 bucket",
        "Sandbox archives",
        "",
        env="AGENT_SANDBOX_ARCHIVE_S3_BUCKET",
        description="Enter a bucket name, or paste an S3 URI such as s3://freeinference/cloud-agent/sandbox to save the bucket and prefix together. Required for sandbox deletion; the completed archive is verified before removal. Empty disables deletion.",
    ),
    Setting(
        "sandbox_archive.region",
        "Sandbox archive S3 region",
        "Sandbox archives",
        "",
        env="AGENT_SANDBOX_ARCHIVE_S3_REGION",
        description="Optional AWS region. Empty uses the control plane's standard AWS SDK configuration.",
    ),
    Setting(
        "sandbox_archive.endpoint",
        "Sandbox archive S3 endpoint",
        "Sandbox archives",
        "",
        env="AGENT_SANDBOX_ARCHIVE_S3_ENDPOINT",
        url=True,
        description="Optional HTTPS S3-compatible endpoint supporting conditional PUT and full-object SHA-256 checksums. Empty uses AWS S3.",
    ),
    Setting(
        "sandbox_archive.prefix",
        "Sandbox archive S3 prefix",
        "Sandbox archives",
        "sandbox-archives",
        env="AGENT_SANDBOX_ARCHIVE_S3_PREFIX",
        description="Object key prefix for new deletion requests. Each request has an immutable object; completed receipts retain their original bucket and key.",
    ),
    Setting(
        "pgadmin_url",
        "Private pgAdmin URL",
        "Database tools",
        "",
        env="AGENT_PGADMIN_URL",
        url=True,
        description="Optional private pgAdmin origin. Use http://pgadmin:5050 for the bundled service after enabling its deployment profile. Empty disables access.",
    ),
    Setting(
        "pgadmin_public_url",
        "Public pgAdmin proxy URL",
        "Database tools",
        "",
        env="AGENT_PGADMIN_PUBLIC_URL",
        url=True,
        description="Optional public URL ending in /admin/agent/pgadmin. Empty derives it from the saved web base URL and /api/admin/agent/pgadmin.",
    ),
    Setting(
        "gateway_issuer",
        "Gateway identity issuer",
        "Sign-in",
        "",
        env="GATEWAY_ISSUER",
        description="Exact issuer signed by the Gateway. Once this database is bound, it cannot be changed here.",
    ),
    Setting(
        "gateway_jwks_url",
        "Gateway public keys URL",
        "Sign-in",
        "",
        env="GATEWAY_JWKS_URL",
        url=True,
    ),
    Setting(
        "session_signing_secret",
        "Session signing key",
        "Security",
        "",
        env="AGENT_SESSION_SECRET",
        secret=True,
        minimum_bytes=32,
        description="A random key of at least 32 bytes. Replacing it signs out existing browser sessions.",
    ),
    Setting(
        "control_signing_secret",
        "Host and worker signing key",
        "Security",
        "",
        env="AGENT_CONTROL_TOKEN_SECRET",
        secret=True,
        description="Use a random key of at least 32 bytes. Replacing it invalidates existing host and worker credentials; re-enroll hosts before resuming work.",
    ),
    Setting(
        "source_control_encryption_key",
        "User credential encryption key",
        "Security",
        "",
        env="AGENT_SOURCE_CONTROL_ENCRYPTION_KEY",
        secret=True,
        description="Use a random key of at least 32 bytes. Protects user source-control and MCP credentials. Once configured, rotation requires a separate re-encryption procedure.",
    ),
    Setting(
        "compute_encryption_key",
        "Compute credential encryption key",
        "Security",
        "",
        env="AGENT_COMPUTE_SECRET_KEY",
        secret=True,
        minimum_bytes=32,
        description="A random key of at least 32 bytes. Once configured, rotation requires a separate re-encryption procedure.",
    ),
    Setting(
        "workspace_broker_token",
        "Workspace broker credential",
        "Execution",
        "",
        env="AGENT_WORKSPACE_BROKER_TOKEN",
        secret=True,
        description="Must match the credential provisioned on each host's workspace broker. Coordinate changes with those hosts.",
    ),
    Setting(
        "broker_url_allowed_hosts",
        "Workspace broker host allowlist",
        "Execution",
        "",
        env="AGENT_BROKER_URL_ALLOWED_HOSTS",
        description="Comma-separated exact hostnames allowed to receive the workspace broker credential. Empty retains unrestricted host selection.",
    ),
    Setting(
        "default_execution_profile",
        "New session isolation policy",
        "Execution",
        "trusted-v1",
        env="AGENT_DEFAULT_EXECUTION_PROFILE",
        choices=("trusted-v1", "untrusted-kata-v1"),
        description="Applies to new root sessions. Public untrusted work requires untrusted-kata-v1 and a qualified Kata host. Existing sessions retain their policy.",
    ),
    Setting(
        "session_cookie_secure",
        "Require HTTPS session cookies",
        "Security",
        True,
        kind="boolean",
        env="AGENT_SESSION_COOKIE_SECURE",
        description="Keep enabled for HTTPS deployments. Disable only for local HTTP development.",
    ),
    Setting(
        "trust_proxy_headers",
        "Trust forwarded client addresses",
        "Security",
        False,
        kind="boolean",
        env="AGENT_TRUST_PROXY_HEADERS",
        description="Enable only when a trusted reverse proxy overwrites forwarded address headers.",
    ),
    Setting(
        "mcp_allow_private_targets",
        "Allow private MCP targets",
        "MCP",
        False,
        kind="boolean",
        env="AGENT_MCP_ALLOW_PRIVATE_TARGETS",
        description="Allow MCP servers on private addresses. Takes effect for new MCP transports after restarting the MCP listener.",
    ),
    Setting(
        "mcp_allow_plain_http",
        "Allow HTTP MCP targets",
        "MCP",
        False,
        kind="boolean",
        env="AGENT_MCP_ALLOW_PLAIN_HTTP",
        description="Allow unencrypted HTTP MCP connections. Takes effect for new MCP transports after restarting the MCP listener.",
    ),
)
SETTINGS += MIGRATED_SETTINGS
SETTINGS_BY_KEY = {setting.key: setting for setting in SETTINGS}
_SETTINGS_BY_ENV = {setting.env: setting for setting in SETTINGS if setting.env}

BOOTSTRAP = (
    {
        "key": "AGENT_DATABASE_URL",
        "label": "Database connection",
        "description": "Required before saved settings can be read.",
    },
    {
        "key": "AGENT_SETTINGS_ENCRYPTION_KEY / AGENT_SESSION_SECRET",
        "label": "Settings encryption key",
        "description": "Use a stable encryption key of at least 32 bytes. Existing session secret is used if a dedicated key is absent. Rotate only with data re-encryption.",
    },
    {
        "key": "ADMIN_TOKEN",
        "label": "Administrator recovery access",
        "description": "Use this deployment credential on the setup page when Gateway sign-in is not configured. It remains independent of saved settings.",
    },
    {
        "key": "AGENT_GITHUB_APP_PRIVATE_KEY_PATH",
        "label": "Mounted GitHub App key",
        "description": "Deployment file path; an inline private key can be set above.",
    },
    {
        "key": "AGENT_COMPUTE_CONFIG",
        "label": "Compute deployment profiles",
        "description": "Trusted deployment bundles and mounted secret file references remain operator configuration.",
    },
    {
        "key": "AGENT_PGADMIN_BOOTSTRAP_URL / AGENT_PGADMIN_BOOTSTRAP_PUBLIC_URL / AGENT_PGADMIN_BOOTSTRAP_DIR",
        "label": "pgAdmin first-time setup",
        "description": "Deployment routing and the private setup volume let the first administrator choose the account in Admin → Database. Setup saves the connection; later changes use the Database tools settings.",
    },
    {
        "key": "Host networking, isolation, listener and build configuration",
        "label": "Deployment infrastructure",
        "description": "Machine addresses, credentials, sandbox isolation, egress, proxy trust and build routing are configured when deploying.",
    },
)

_lock = threading.RLock()
_values: dict[str, SettingValue] | None = None
_revision = -1


def _cipher(key: str) -> Any:
    from cloud_agent.source_control import SourceControlCipher, SourceControlError

    secret = (
        os.environ.get("AGENT_SETTINGS_ENCRYPTION_KEY")
        or os.environ.get("AGENT_SESSION_SECRET")
        or ""
    )
    if len(secret.encode()) < 32:
        raise SourceControlError(
            "A settings encryption key of at least 32 bytes is required to store secret settings"
        )
    return SourceControlCipher(secret, domain=b"cloud-agent/admin-settings/v1/" + key.encode())


def _encode(setting: Setting, value: SettingValue) -> Any:
    if setting.secret and value:
        return {"ciphertext": _cipher(setting.key).encrypt(str(value))}
    return value


def _decode(encoded: Mapping[str, Any]) -> dict[str, SettingValue]:
    values: dict[str, SettingValue] = {}
    for setting in SETTINGS:
        value = encoded.get(setting.key, setting.default)
        if setting.secret and isinstance(value, dict) and set(value) == {"ciphertext"}:
            value = _cipher(setting.key).decrypt(value["ciphertext"])
        elif setting.secret and value:
            raise ValueError(f"Unencrypted stored secret for {setting.label}")
        values[setting.key] = setting.validate(value)
    _validate_relations(values)
    return values


def _validate_relations(values: Mapping[str, Any]) -> None:
    for smaller, larger in (
        ("goals.arm_after_seconds", "goals.stalled_after_seconds"),
        ("attachments.max_file_bytes", "attachments.max_total_bytes"),
        ("skills.max_skill_bytes", "skills.max_selection_bytes"),
        ("goals.wakeup_backoff_seconds", "goals.wakeup_backoff_max_seconds"),
        ("task_titles.timeout_seconds", "task_titles.total_timeout_seconds"),
    ):
        if values[smaller] > values[larger]:
            raise ValueError(
                f"{SETTINGS_BY_KEY[smaller].label} cannot exceed {SETTINGS_BY_KEY[larger].label}"
            )


def encode_changes(changes: Mapping[str, Any]) -> dict[str, Any]:
    """Validate a patch and encrypt its secrets; ``None`` resets registry defaults."""
    if not changes or len(changes) > len(SETTINGS):
        raise ValueError("Supply one or more known settings")
    checked: dict[str, SettingValue] = {}
    for key, value in changes.items():
        setting = SETTINGS_BY_KEY.get(key)
        if setting is None:
            raise ValueError("Unknown setting")
        checked[key] = setting.validate(setting.default if value is None else value)
    return {key: _encode(SETTINGS_BY_KEY[key], value) for key, value in checked.items()}


def validate_stored_settings(encoded: Mapping[str, Any]) -> None:
    """Validate a merged snapshot inside the repository's locked transaction."""
    _decode(encoded)


def validate_settings_update(previous: Mapping[str, Any], encoded: Mapping[str, Any]) -> None:
    """Keep stored credential encryption keys stable while validating an atomic update."""
    before, after = _decode(previous), _decode(encoded)
    for key in ("source_control_encryption_key", "compute_encryption_key"):
        if before[key] and before[key] != after[key]:
            raise ValueError(
                f"{SETTINGS_BY_KEY[key].label} cannot be replaced or reset without re-encrypting stored credentials"
            )


def initial_settings(*, keys: set[str] | None = None) -> dict[str, Any]:
    """Import only registered environment variables into the initial settings row."""
    result: dict[str, Any] = {}
    values: dict[str, SettingValue] = {setting.key: setting.default for setting in SETTINGS}
    imported: dict[str, SettingValue] = {}
    if keys is None and os.environ.get("AGENT_COMPUTE_CONFIG"):
        from cloud_agent.compute_config import ComputeConfiguration

        configuration = ComputeConfiguration.from_env()
        imported = {
            "compute.poll_seconds": configuration.poll_seconds,
            "compute.observation_seconds": configuration.observation_seconds,
        }
    for setting in SETTINGS:
        if keys is not None and setting.key not in keys:
            continue
        value: Any = imported.get(setting.key, setting.default)
        raw = os.environ.get(setting.env, "") if setting.env else ""
        if raw:
            try:
                if setting.kind == "integer":
                    value = int(raw)
                elif setting.kind == "number":
                    value = float(raw)
                elif setting.kind == "boolean":
                    if raw.lower() not in {"true", "false", "yes", "no", "1", "0"}:
                        raise ValueError("invalid boolean")
                    value = raw.lower() in {"true", "yes", "1"}
                else:
                    value = raw.strip() if not setting.secret else raw
                if setting.key == "log_level":
                    value = raw.strip().upper()
                    value = {"WARN": "WARNING", "FATAL": "CRITICAL"}.get(value, value)
                elif setting.key == "log_format":
                    normalized = raw.strip().lower()
                    value = normalized if normalized in {"text", "json"} else "text"
                value = setting.validate(value)
            except (ValueError, OverflowError):
                raise ValueError(f"Invalid initial configuration for {setting.label}") from None
        values[setting.key] = value
        result[setting.key] = _encode(setting, value)
    _validate_relations(values)
    return result


def apply_settings(revision: int, encoded: Mapping[str, Any]) -> None:
    """Atomically adopt a committed snapshot, ignoring stale reads."""
    global _values, _revision
    values = _decode(encoded)
    with _lock:
        if revision >= _revision:
            logging_changed = _values is None or any(
                values[key] != _values[key] for key in ("log_level", "log_format")
            )
            _values, _revision = values, revision
            if logging_changed:
                from cloud_agent.logging_utils import apply_logging_settings

                apply_logging_settings(str(values["log_level"]), str(values["log_format"]))


def settings_response(revision: int, encoded: Mapping[str, Any]) -> dict[str, Any]:
    """Build the editor response with write-only secret status."""
    values = _decode(encoded)
    locked = {
        key
        for key in ("gateway_issuer", "source_control_encryption_key", "compute_encryption_key")
        if values[key]
    }
    return {
        "revision": revision,
        "definitions": [
            {**setting.definition(), "read_only": setting.key in locked} for setting in SETTINGS
        ],
        "values": {
            setting.key: None if setting.secret else values[setting.key] for setting in SETTINGS
        },
        "secret_configured": {
            setting.key: bool(values[setting.key]) for setting in SETTINGS if setting.secret
        },
        "bootstrap": list(BOOTSTRAP),
        "setup": setup_status(values),
    }


def setup_status(values: Mapping[str, SettingValue]) -> dict[str, Any]:
    """Report incomplete features using field names and status, never saved values."""
    groups: list[dict[str, Any]] = []
    configured = dict(values)
    mounted_key = os.environ.get("AGENT_GITHUB_APP_PRIVATE_KEY_PATH", "")
    if mounted_key and os.path.isfile(mounted_key) and os.access(mounted_key, os.R_OK):
        configured["github_app_private_key"] = True
    for key, label, required, fields in (
        (
            "sign_in",
            "Gateway sign-in",
            True,
            (
                "gateway_base_url",
                "gateway_dispatch_token",
                "gateway_issuer",
                "gateway_jwks_url",
                "gateway_authorize_url",
                "session_redirect_uri",
                "web_base_url",
                "session_signing_secret",
            ),
        ),
        (
            "execution",
            "Agent execution",
            True,
            ("control_signing_secret", "workspace_broker_token"),
        ),
        ("credentials", "User connections", True, ("source_control_encryption_key",)),
        (
            "github",
            "GitHub account connections",
            False,
            (
                "github_app_id",
                "github_app_client_id",
                "github_app_client_secret",
                "github_app_install_url",
                "github_app_private_key",
                "source_control_encryption_key",
            ),
        ),
        (
            "gitlab",
            "GitLab account connections",
            False,
            (
                "gitlab_client_id",
                "gitlab_client_secret",
                "gitlab_redirect_uri",
                "source_control_encryption_key",
            ),
        ),
        (
            "mcp_github",
            "GitHub MCP browser sign-in",
            False,
            ("mcp_github_client_id", "mcp_github_client_secret", "source_control_encryption_key"),
        ),
        ("compute", "Compute credential storage", False, ("compute_encryption_key",)),
    ):
        missing = [
            {"key": field, "label": SETTINGS_BY_KEY[field].label}
            for field in fields
            if not configured[field]
        ]
        # A shared encryption key alone does not mean an optional provider is enabled.
        started = any(values[field] for field in fields if field != "source_control_encryption_key")
        if key == "compute":
            started = started or bool(os.environ.get("AGENT_COMPUTE_CONFIG"))
        groups.append(
            {
                "key": key,
                "label": label,
                "required": required,
                "configured": not missing,
                "needs_setup": bool(missing) and (required or started),
                "missing": missing,
            }
        )
    root_key = (
        os.environ.get("AGENT_SETTINGS_ENCRYPTION_KEY")
        or os.environ.get("AGENT_SESSION_SECRET")
        or ""
    )
    bootstrap_missing = []
    if len(root_key.encode()) < 32:
        bootstrap_missing.append(
            {"key": "AGENT_SETTINGS_ENCRYPTION_KEY", "label": "Settings encryption root key"}
        )
    return {
        "required": bool(bootstrap_missing) or any(group["needs_setup"] for group in groups),
        "groups": groups,
        "bootstrap_missing": bootstrap_missing,
    }


def get_setting(key: str, fallback: SettingValue | None = None) -> SettingValue:
    """Read the current snapshot, or a pre-startup/test default."""
    setting = SETTINGS_BY_KEY[key]
    with _lock:
        if _values is not None:
            return _values[key]
    if setting.env:
        raw = os.environ.get(setting.env)
        if raw:
            if setting.kind == "integer":
                return int(raw)
            if setting.kind == "number":
                return float(raw)
            if setting.kind == "boolean":
                return raw.lower() in {"true", "1", "yes"}
            return raw
    return setting.default if fallback is None else fallback


def integer_setting(key: str, fallback: int | None = None) -> int:
    """Read an integer setting."""
    return int(get_setting(key, fallback))


def number_setting(key: str, fallback: float | None = None) -> float:
    """Read a numeric setting."""
    return float(get_setting(key, fallback))


def text_setting(key: str, fallback: str | None = None) -> str:
    """Read a text setting."""
    return str(get_setting(key, fallback))


def boolean_setting(key: str, fallback: bool | None = None) -> bool:
    """Read a boolean setting."""
    return bool(get_setting(key, fallback))


def setting_env(name: str, default: str | None = None) -> str | None:
    """Adapt legacy string readers without writing secrets into the process environment."""
    setting = _SETTINGS_BY_ENV.get(name)
    with _lock:
        if _values is not None and setting is not None:
            value = _values[setting.key]
            return str(value).lower() if isinstance(value, bool) else str(value)
    return os.environ.get(name, default)


def settings_environment() -> dict[str, str]:
    """Build a provider configuration mapping from bootstrap and saved settings."""
    result = dict(os.environ)
    with _lock:
        if _values is not None:
            for env, setting in _SETTINGS_BY_ENV.items():
                value = _values[setting.key]
                result[env] = str(value).lower() if isinstance(value, bool) else str(value)
    return result


async def initialize_settings(repository: SettingsRepository, *, seed: bool = True) -> None:
    """Seed once, then adopt the database rather than reimporting the environment."""
    revision, encoded = await repository.read_settings()
    missing = {setting.key for setting in MIGRATED_SETTINGS} - encoded.keys()
    if revision < 0 or missing:
        if seed:
            revision, encoded = await repository.seed_settings(
                initial_settings(keys=missing if revision >= 0 else None)
            )
        else:
            # Only the main listener has the complete legacy environment.
            # A relay racing it must never seed blank integration credentials.
            for _ in range(300):
                await asyncio.sleep(0.1)
                revision, encoded = await repository.read_settings()
                if revision >= 0 and all(setting.key in encoded for setting in MIGRATED_SETTINGS):
                    break
            else:
                raise RuntimeError(
                    "The main control plane must initialize admin settings before the relay starts"
                )
    apply_settings(revision, encoded)


async def synchronize_settings(repository: SettingsRepository) -> None:
    """Refresh every listener's snapshot; failed reads retain the last committed values."""
    from cloud_agent.logging_utils import get_logger

    logger = get_logger(__name__)
    while True:
        await asyncio.sleep(5)
        try:
            revision, encoded = await repository.read_settings()
            apply_settings(revision, encoded)
        except Exception as exc:
            logger.warning("settings synchronization failed (%s)", type(exc).__name__)


def reset_settings() -> None:
    """Release the process snapshot when a listener stops (also useful for test isolation)."""
    global _values, _revision
    with _lock:
        _values, _revision = None, -1
