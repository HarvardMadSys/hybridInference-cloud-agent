"""Validated, database-backed application settings shared by both listeners.

The environment is imported once when the settings row is first created.
After that, the database is authoritative, including resets to registry defaults.
Secrets are encrypted under a separate domain and never returned to the browser.
Only database access, the settings root key, recovery access and process
infrastructure remain bootstrap. Runtime identity and service keys are persisted.
"""

from __future__ import annotations

import asyncio
import os
import threading
from typing import TYPE_CHECKING, Any

from cloud_agent.credential_cipher import CredentialCipher, CredentialCipherError
from cloud_agent.deployment_setup import BOOTSTRAP as BOOTSTRAP, bootstrap_snapshot
from cloud_agent.domain_cipher import settings_root_key
from cloud_agent.setting_definition import (
    MINIMUM_CREDENTIAL_BYTES as MINIMUM_CREDENTIAL_BYTES,
    Setting,
    SettingsRevisionConflict as SettingsRevisionConflict,
    SettingTooShort,
    SettingValue,
    _github_install_url,
    _gitlab_redirect_uri,
    _grant_base_path,
    _issuer,
    _numeric,
    _s3_bucket,
    _s3_prefix,
    _s3_region,
)
from cloud_agent_shared.sandbox_manager_compat import (
    manager_env,
    manager_environment,
)

if TYPE_CHECKING:
    from collections.abc import Collection, Mapping

    from cloud_agent.ports.settings import SettingsRepository


class SettingsEncryptionError(Exception):
    """Persisted settings cannot be encrypted or decrypted safely."""


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
    # Generous enough for a busy gateway, bounded because a control plane
    # blocked on a lookup is a control plane that has stopped scheduling. It
    # also bounds one usage page as a whole, from waiting for a slot to the
    # last byte of the body.
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
    # Seconds between attempts to load an App that failed. Also the Retry-After
    # a caller refused in the meantime is given.
    _numeric(
        "github_reload_interval_s",
        "GitHub configuration retry interval (seconds)",
        "Source control",
        30.0,
        1,
        3600,
    ),
    # How long one user's repository answer is reused. Entitlement is asked on
    # every workspace file read and terminal open; a minute keeps those off
    # GitHub while bounding how long a revoked permission still reads as
    # granted.
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
    # The longest an idle publisher that is told about new work sleeps. It
    # wakes sooner for a notification or the earliest publication/base import
    # retry.
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
        "mcp_registry_url",
        "MCP registry URL",
        "MCP",
        "https://registry.modelcontextprotocol.io",
        url=True,
        https_only=True,
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
    # An MCP call is a tool call: it can be a repository search or an issue
    # fetch, and it is not a token stream. Generous, but bounded: a sandbox
    # waiting on a hung upstream is a job burning its wall-clock timeout for
    # nothing.
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
        "site.docs_url",
        "Documentation URL",
        "Appearance",
        "",
        env="AGENT_WEB_DOCS_URL",
        url=True,
        description=(
            "User documentation for this deployment, linked from the account menu's Help and "
            "My Settings. Empty hides the link."
        ),
    ),
    Setting(
        "site.contact_url",
        "Contact URL",
        "Appearance",
        "",
        env="AGENT_WEB_CONTACT_URL",
        url=True,
        mailto=True,
        description=(
            "Where users reach the operators: a support page, or mailto: and one address. "
            "Linked as Contact us from the account menu's Help. Empty hides the link."
        ),
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
        https_only=True,
        description=(
            "CloudLab's Portal API: https://boss.emulab.net:43794. The portal's web address, "
            "www.cloudlab.us, does not serve it. Empty disables CloudLab. Applies to "
            "subsequent operations after settings synchronize."
        ),
    ),
    Setting(
        "compute.registration_url",
        "Compute node registration URL",
        "Compute",
        "",
        env="AGENT_COMPUTE_REGISTRATION_URL",
        url=True,
        https_only=True,
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
    Setting(
        "gateway_base_url",
        "Gateway API URL",
        "Gateway",
        "",
        env="GATEWAY_BASE_URL",
        url=True,
        https_unless_private=True,
    ),
    Setting(
        "gateway_dispatch_token",
        "Gateway dispatch token",
        "Gateway",
        "",
        env="GATEWAY_GRANT_DISPATCH_TOKEN",
        secret=True,
        minimum_bytes=MINIMUM_CREDENTIAL_BYTES,
        minimum_bytes_new_values_only=True,
        description="A random credential of at least 32 bytes, the same value the Gateway holds. Sent only to the Gateway API URL: to change that URL, clear this first and enter it again afterwards, or save both together.",
    ),
    Setting(
        "gateway_grant_base_path",
        "Grant API path",
        "Gateway",
        "/internal/agent-grants",
        env="GATEWAY_GRANT_BASE_PATH",
        check=_grant_base_path,
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
        https_unless_private=True,
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
        description="Comma-separated owner/repository entries or owner/* rules. Every admitted user may have the platform clone and publish to these with this deployment's GitHub credentials, without connecting GitHub. Applies to new repository access decisions.",
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
        check=_github_install_url,
        description="An https://github.com URL, normally https://github.com/apps/<app-slug>/installations/new.",
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
        check=_gitlab_redirect_uri,
        description="The exact callback registered with GitLab. HTTPS, except http://localhost for development.",
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
        minimum_bytes=MINIMUM_CREDENTIAL_BYTES,
        minimum_bytes_new_values_only=True,
        description="A random credential of at least 32 bytes, the same value the alert relay sends. Empty opens nothing.",
    ),
    # How long a residency's lease lives between beats (design §7) and, since
    # the lease and the thread-attributed grant are renewed by the same beat
    # and revoked together (retained-session design §§3 and 6), the cap on how
    # long an idle resident process keeps a live credential after its host goes
    # quiet.
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
    # The longest an idle usage collector waits on its wake-up before looking
    # anyway.
    _numeric("ledger_idle_s", "Usage idle check interval (seconds)", "Operations", 30.0, 1, 300),
    # The longest an idle coordinator that is told about new work sleeps. It
    # wakes sooner for a notification or the earliest capture retry; this is
    # for what announces nothing, such as a capture the reaper found stalled.
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
    # Abandoned staged uploads count against staging quota until collected.
    # Pending-message and job-claimed rows are kept by the repository
    # predicate.
    _numeric(
        "attachments.staged_retention_hours",
        "Abandoned upload retention (hours)",
        "Attachments",
        24,
        1,
        8760,
        description="Remove uploads this old only when no job or pending message holds them. Changes apply on the next maintenance pass.",
    ),
    # Everything one owner's uploads keep in the database, staged or attached
    # to a conversation that still exists. Attached bytes stay until the
    # conversation is deleted, so the per-turn bounds alone would let one
    # account grow the shared database without end.
    _numeric(
        "attachments.max_user_bytes",
        "Maximum stored upload bytes per user",
        "Attachments",
        1073741824,
        1,
        68719476736,
        description=(
            "Everything one user's uploads keep, staged or in conversations not yet "
            "deleted. Applies to subsequent uploads after settings synchronize."
        ),
    ),
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
    # How recently an active goal with no pending obligation must have moved
    # for the recovery sweep to assume a consumer is still about to act on it.
    # Long enough to cover a decision in flight, short enough that a goal
    # nothing is working on is noticed rather than re-armed for ever.
    _numeric("goals.arm_after_seconds", "Goal activation delay (seconds)", "Goals", 60.0, 1, 3600),
    # How long an active goal may sit with no queued or running turn and no
    # pending wakeup before the recovery sweep pauses it. This is a backstop
    # for a path that forgot to schedule, not a mechanism: every ordinary way a
    # goal stops being able to continue pauses it where and when it happens.
    _numeric(
        "goals.stalled_after_seconds", "Goal stall timeout (seconds)", "Goals", 900.0, 60, 86400
    ),
    # How many times a wakeup may fail on temporary scheduling infrastructure
    # before the goal is paused with scheduling_failed rather than retried
    # forever behind a green "active" badge.
    _numeric("goals.max_wakeup_attempts", "Maximum goal wakeup attempts", "Goals", 5, 1, 100),
    # The first backoff step; each retry doubles it up to
    # goals.wakeup_backoff_max_seconds.
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
_MIGRATED_DEFINITIONS: tuple[Setting, ...] = (
    Setting(
        "sandbox_archive.bucket",
        "Sandbox archive S3 bucket",
        "Sandbox archives",
        "",
        env="AGENT_SANDBOX_ARCHIVE_S3_BUCKET",
        check=_s3_bucket,
        hint=(
            "Use a bucket name such as freeinference; set cloud-agent/sandbox "
            "in Sandbox archive S3 prefix."
        ),
        description="Enter a bucket name, or paste an S3 URI such as s3://freeinference/cloud-agent/sandbox to save the bucket and prefix together. Required for sandbox deletion; the completed archive is verified before removal. Empty disables deletion.",
    ),
    Setting(
        "sandbox_archive.region",
        "Sandbox archive S3 region",
        "Sandbox archives",
        "",
        env="AGENT_SANDBOX_ARCHIVE_S3_REGION",
        check=_s3_region,
        description="Optional AWS region. Empty uses the control plane's standard AWS SDK configuration.",
    ),
    Setting(
        "sandbox_archive.endpoint",
        "Sandbox archive S3 endpoint",
        "Sandbox archives",
        "",
        env="AGENT_SANDBOX_ARCHIVE_S3_ENDPOINT",
        url=True,
        https_only=True,
        description="Optional HTTPS S3-compatible endpoint supporting conditional PUT and full-object SHA-256 checksums. Empty uses AWS S3.",
    ),
    Setting(
        "sandbox_archive.prefix",
        "Sandbox archive S3 prefix",
        "Sandbox archives",
        "sandbox-archives",
        env="AGENT_SANDBOX_ARCHIVE_S3_PREFIX",
        check=_s3_prefix,
        description="Object key prefix for new deletion requests. Each request has an immutable object; completed receipts retain their original bucket and key.",
    ),
    Setting(
        "pgadmin_url",
        "Private pgAdmin URL",
        "Database tools",
        "",
        env="AGENT_PGADMIN_URL",
        url=True,
        description="Optional private pgAdmin origin. Use http://pgadmin:5050 for the bundled service after enabling its deployment profile. Any other service must also be named by AGENT_PGADMIN_URL or AGENT_PGADMIN_BOOTSTRAP_URL in the deployment environment. Empty disables access.",
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
        check=_issuer,
        description="Exact issuer signed by the Gateway. Once this database is bound, it cannot be changed here.",
    ),
    Setting(
        "gateway_jwks_url",
        "Gateway public keys URL",
        "Sign-in",
        "",
        env="GATEWAY_JWKS_URL",
        url=True,
        https_unless_private=True,
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
        minimum_bytes=MINIMUM_CREDENTIAL_BYTES,
        minimum_bytes_new_values_only=True,
        description="A random key of at least 32 bytes. Replacing it invalidates existing host and worker credentials; re-enroll hosts before resuming work.",
    ),
    Setting(
        "source_control_encryption_key",
        "User credential encryption key",
        "Security",
        "",
        env="AGENT_SOURCE_CONTROL_ENCRYPTION_KEY",
        secret=True,
        minimum_bytes=MINIMUM_CREDENTIAL_BYTES,
        minimum_bytes_new_values_only=True,
        description="A random key of at least 32 bytes. Protects user source-control, MCP, environment, and SSH credentials. Once configured, rotation requires a separate re-encryption procedure.",
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
        "sandbox_manager_token",
        "Sandbox manager credential",
        "Execution",
        "",
        env="AGENT_SANDBOX_MANAGER_TOKEN",
        secret=True,
        description="Must match the credential provisioned on each host's sandbox manager. Coordinate changes with those hosts.",
    ),
    Setting(
        "sandbox_manager_url_allowed_hosts",
        "Sandbox manager host allowlist",
        "Execution",
        "",
        env="AGENT_SANDBOX_MANAGER_URL_ALLOWED_HOSTS",
        description="Comma-separated hostnames, or scheme://host:port origins that also pin the scheme and port, allowed to receive the sandbox manager credential. Empty retains unrestricted host selection.",
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
        description="Enable only when a reverse proxy you control sits directly in front of the control plane and sets or appends X-Forwarded-For. Only its rightmost entry is recorded.",
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
    Setting(
        "default_model",
        "Default model for new tasks",
        "Execution",
        "deepseek-v4-flash",
        env="AGENT_DEFAULT_MODEL",
        empty_env_imports=True,
        description=(
            "The model a new task starts on when its owner has not chosen one. A user's "
            "pinned model beats their last used model, which beats this default. A model "
            "the gateway does not offer to a user is skipped rather than forced, and if "
            "this one is unavailable the task waits for the user to choose. Empty means "
            "this deployment offers no default at all."
        ),
    ),
    _numeric(
        "streams.max_per_user",
        "Live streams per account",
        "Operations",
        24,
        1,
        1_000_000,
        env="AGENT_MAX_STREAMS_PER_USER",
        description=(
            "How many live-update streams (the change stream and job event streams) one "
            "account may hold open on one control-plane process; one more answers 429 with "
            "Retry-After, which the web app backs off on. A browser tab holds one change "
            "stream and one stream per conversation it is watching."
        ),
        apply_mode="next_operation",
        apply_description="Checked each time a stream opens; open streams are not closed.",
        invalid_env_uses_default=True,
    ),
    Setting(
        "runtime_registry_url",
        "Agent package registry",
        "Agent versions",
        "",
        env="AGENT_RUNTIME_REGISTRY_URL",
        url=True,
        https_unless_private=True,
        description=(
            "The npm registry agent versions are listed from here and downloaded from on "
            "every server. Empty is the public registry (https://registry.npmjs.org). "
            "Servers receive it on their next heartbeat; updates are refused until every "
            "online server reports using it."
        ),
        apply_mode="next_operation",
        apply_description=(
            "Version lists use it at once; each server uses it for downloads it starts "
            "after its next heartbeat."
        ),
    ),
    Setting(
        "api_docs",
        "Serve API documentation",
        "Operations",
        False,
        kind="boolean",
        env="AGENT_API_DOCS",
        description=(
            "Serves /docs, /redoc and /openapi.json on the main API. Keep off in production: "
            "the schema is unauthenticated, and the two pages load scripts from a CDN at a "
            "floating version. The MCP-only listener never serves them."
        ),
        apply_mode="next_operation",
        apply_description="Checked on each documentation request; no restart.",
        invalid_env_uses_default=True,
    ),
)
#: Registered after settings rows existed, but only in SETTINGS, so a database
#: created before their registration never imported their environment value
#: (P1 section 5.1). Listed here, an existing row missing them imports them once.
_IMPORTED_AFTER_REGISTRATION = frozenset(
    {
        "web_search_provider",
        "serper_api_key",
        "brave_search_api_key",
        "parallel_api_key",
        "mcp_github_client_id",
        "mcp_github_client_secret",
        "site.docs_url",
        "site.contact_url",
    }
)
SETTINGS += _MIGRATED_DEFINITIONS
#: Settings an existing database imports from the environment when its row
#: lacks them. Every registration with an environment name belongs here.
MIGRATED_SETTINGS: tuple[Setting, ...] = _MIGRATED_DEFINITIONS + tuple(
    setting for setting in SETTINGS if setting.key in _IMPORTED_AFTER_REGISTRATION
)
SETTINGS_BY_KEY = {setting.key: setting for setting in SETTINGS}
SETTINGS_BY_STORAGE_KEY = {setting.storage_key: setting for setting in SETTINGS}
_SETTINGS_BY_ENV = {setting.env: setting for setting in SETTINGS if setting.env}

_lock = threading.RLock()
_values: dict[str, SettingValue] | None = None
_revision = -1


def _cipher(key: str) -> CredentialCipher:
    secret = settings_root_key()
    if len(secret.encode()) < 32:
        raise SettingsEncryptionError(
            "A settings encryption key of at least 32 bytes is required to store secret settings"
        )
    return CredentialCipher(secret, domain=b"cloud-agent/admin-settings/v1/" + key.encode())


def _encode(setting: Setting, value: SettingValue) -> Any:
    if setting.secret and value:
        return {"ciphertext": _cipher(setting.storage_key).encrypt(str(value))}
    return value


def _decode(
    encoded: Mapping[str, Any],
    problems: list[str] | None = None,
    *,
    changed: Collection[str] = (),
) -> dict[str, SettingValue]:
    """Decrypt and check a snapshot, strictly unless ``problems`` collects.

    With ``problems``, the snapshot is a stored row, read the way the release
    after the one that saved it must be able to: a value this registry no
    longer accepts (a narrower bound, a stricter URL rule) falls back to its
    default, and an ordering a stored pair breaks falls back to both defaults,
    each described in ``problems`` rather than refusing every setting at once.
    A stored value short of a newer minimum length is kept: it is the key
    everything was signed or encrypted with, and the default is no key at all.
    ``changed`` keys stay strict, so an update cannot save what this refuses.
    Decryption failures and unencrypted secrets always raise.
    """
    values: dict[str, SettingValue] = {}
    for setting in SETTINGS:
        value = encoded.get(setting.storage_key, setting.default)
        if setting.secret and isinstance(value, dict) and set(value) == {"ciphertext"}:
            cipher = _cipher(setting.storage_key)
            try:
                value = cipher.decrypt(value["ciphertext"])
            except CredentialCipherError:
                # Name the setting, never the value: the cipher's own message
                # cannot say which of the stored secrets failed.
                raise SettingsEncryptionError(
                    f"Stored secret for {setting.label} could not be decrypted"
                ) from None
        elif setting.secret and value:
            raise ValueError(f"Unencrypted stored secret for {setting.label}")
        try:
            values[setting.key] = setting.validate(value, saved=True)
        except ValueError as exc:
            if problems is None or setting.key in changed:
                raise
            if isinstance(exc, SettingTooShort):
                problems.append(f"{exc}; the saved value stays in use until it is replaced")
                values[setting.key] = value
            else:
                problems.append(f"{exc}; using the default instead")
                values[setting.key] = setting.default
    _validate_relations(values, problems, changed=changed)
    return values


def _validate_relations(
    values: dict[str, Any],
    problems: list[str] | None = None,
    *,
    changed: Collection[str] = (),
) -> None:
    for smaller, larger in (
        ("goals.arm_after_seconds", "goals.stalled_after_seconds"),
        ("attachments.max_file_bytes", "attachments.max_total_bytes"),
        ("attachments.max_total_bytes", "attachments.max_user_bytes"),
        ("skills.max_skill_bytes", "skills.max_selection_bytes"),
        ("goals.wakeup_backoff_seconds", "goals.wakeup_backoff_max_seconds"),
        ("task_titles.timeout_seconds", "task_titles.total_timeout_seconds"),
    ):
        if values[smaller] > values[larger]:
            message = (
                f"{SETTINGS_BY_KEY[smaller].label} cannot exceed {SETTINGS_BY_KEY[larger].label}"
            )
            if problems is None or smaller in changed or larger in changed:
                raise ValueError(message)
            # The pairs share no key, and every default pair is ordered.
            problems.append(f"{message}; using the defaults for both instead")
            values[smaller] = SETTINGS_BY_KEY[smaller].default
            values[larger] = SETTINGS_BY_KEY[larger].default


def encode_changes(changes: Mapping[str, Any]) -> dict[str, Any]:
    """Validate a patch and encrypt its secrets; ``None`` resets registry defaults."""
    if not changes or len(changes) > len(SETTINGS):
        raise ValueError("Supply one or more known settings")
    checked: dict[str, SettingValue] = {}
    for key, value in changes.items():
        setting = SETTINGS_BY_KEY.get(key) or SETTINGS_BY_STORAGE_KEY.get(key)
        if setting is None:
            raise ValueError("Unknown setting")
        if setting.key in checked:
            raise ValueError("Supply each setting only once")
        checked[setting.key] = setting.validate(setting.default if value is None else value)
    return {
        SETTINGS_BY_KEY[key].storage_key: _encode(SETTINGS_BY_KEY[key], value)
        for key, value in checked.items()
    }


def validate_stored_settings(encoded: Mapping[str, Any]) -> None:
    """Validate a merged snapshot inside the repository's locked transaction."""
    _decode(encoded)


def validate_settings_update(previous: Mapping[str, Any], encoded: Mapping[str, Any]) -> None:
    """Validate an atomic update against the snapshot it replaces.

    Only the changed keys are held to this registry. A value an earlier release
    saved and this one refuses must never block the update that repairs it,
    and the editor saves one key at a time. Stored credential encryption keys
    stay stable. And the gateway URL cannot move away from the dispatch token
    it was configured with: the token is write-only, but it is sent to
    whatever URL is saved, so an update that changes the URL must also supply
    the token again or clear it. A supplied secret is always fresh ciphertext
    (Fernet's IV is random), so an encoded value equal to the previous one is a
    token this update did not supply.
    """
    changed = {
        SETTINGS_BY_STORAGE_KEY[key].key
        for key, value in encoded.items()
        if key in SETTINGS_BY_STORAGE_KEY and previous.get(key) != value
    }
    before, after = _decode(previous, []), _decode(encoded, [], changed=changed)
    for key in ("source_control_encryption_key", "compute_encryption_key"):
        if before[key] and before[key] != after[key]:
            raise ValueError(
                f"{SETTINGS_BY_KEY[key].label} cannot be replaced or reset without re-encrypting stored credentials"
            )
    # Compared with whatever the URL was, unset included, so clearing it and
    # then naming a new host is the same move.
    if (
        after["gateway_base_url"]
        and after["gateway_base_url"] != before["gateway_base_url"]
        and after["gateway_dispatch_token"]
        and encoded.get("gateway_dispatch_token") == previous.get("gateway_dispatch_token")
    ):
        url, token = (
            SETTINGS_BY_KEY["gateway_base_url"].label,
            SETTINGS_BY_KEY["gateway_dispatch_token"].label,
        )
        raise ValueError(
            f"{token} is sent to {url}. To change {url}, clear {token} first and "
            "enter it again afterwards, or save both in one update."
        )


def initial_settings(  # noqa: C901, PLR0912 complexity baseline, #771
    *, keys: set[str] | None = None, problems: list[str] | None = None
) -> dict[str, Any]:
    """Import only registered environment variables into the initial settings row.

    The first row refuses an invalid value, so a new deployment learns about
    it before anything is stored. ``keys`` imports into an existing row on
    upgrade: there an invalid value would stop a running deployment, so it
    imports the default and ``problems`` names the variable, never its value.
    A setting marked ``invalid_env_uses_default`` does that on the first row
    too, as the reader it replaced did.
    """
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
        if keys is not None and setting.key not in keys and setting.storage_key not in keys:
            continue
        value: Any = imported.get(setting.key, setting.default)
        raw = manager_env(setting.env, "") if setting.env else ""
        if (
            raw == ""
            and setting.env
            and setting.empty_env_imports
            and manager_env(setting.env) is not None
        ):
            value = ""
        elif raw:
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
                value = setting.validate(value, saved=True)
            except (ValueError, OverflowError) as exc:
                if keys is None and not setting.invalid_env_uses_default:
                    if isinstance(exc, SettingTooShort):
                        raise ValueError(
                            f"Invalid initial configuration for {setting.label} "
                            f"({setting.env}): use at least {setting.minimum_bytes} bytes"
                        ) from None
                    raise ValueError(f"Invalid initial configuration for {setting.label}") from None
                value = imported.get(setting.key, setting.default)
                if problems is not None:
                    problems.append(
                        f"{setting.env} is not a valid {setting.label}; imported the default"
                    )
        values[setting.key] = value
        result[setting.storage_key] = _encode(setting, value)
    _validate_relations(values)
    return result


def apply_settings(revision: int, encoded: Mapping[str, Any]) -> None:
    """Atomically adopt a committed snapshot, ignoring stale reads.

    A stored value this registry refuses is reported once per adopted revision,
    by key and never by value; :func:`_decode` says what applies instead.
    """
    global _values, _revision
    problems: list[str] = []
    adopted = False
    with _lock:
        if _values is not None and revision <= _revision:
            return
        values = _decode(encoded, problems)
        if revision >= _revision:
            adopted = revision != _revision
            logging_changed = _values is None or any(
                values[key] != _values[key] for key in ("log_level", "log_format")
            )
            _values, _revision = values, revision
            if logging_changed:
                from cloud_agent.logging_utils import apply_logging_settings

                apply_logging_settings(str(values["log_level"]), str(values["log_format"]))
    if adopted and problems:
        from cloud_agent.logging_utils import get_logger

        logger = get_logger(__name__)
        for problem in problems:
            logger.warning("saved setting not applied as stored: %s", problem)


def settings_response(revision: int, encoded: Mapping[str, Any]) -> dict[str, Any]:
    """Build the editor response with write-only secret status."""
    values = _decode(encoded, [])
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
        "bootstrap": bootstrap_snapshot(values),
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
            ("control_signing_secret", "sandbox_manager_token"),
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
    root_key = settings_root_key()
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
        raw = manager_env(setting.env)
        if raw == "" and setting.empty_env_imports:
            return ""
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
    return manager_env(name, default)


def settings_environment() -> dict[str, str]:
    """Build a provider configuration mapping from bootstrap and saved settings."""
    result = manager_environment(os.environ)
    with _lock:
        if _values is not None:
            for env, setting in _SETTINGS_BY_ENV.items():
                value = _values[setting.key]
                result[env] = str(value).lower() if isinstance(value, bool) else str(value)
    return result


def weak_credentials() -> list[str]:
    """Name each configured credential shorter than :data:`MINIMUM_CREDENTIAL_BYTES`.

    Names only; no value leaves this function. The signing key, the user
    credential encryption key and the dispatch tokens are read from the
    adopted snapshot, where a value saved before their floor existed may still
    be short. ``ADMIN_TOKEN`` is read from the process environment, the only
    place it lives: it is not refused, because a deployment's recovery door
    closing on upgrade would be a silent lock-out, but it is reported.
    """
    weak = []
    for setting in SETTINGS:
        if not (setting.minimum_bytes_new_values_only and setting.env):
            continue
        value = text_setting(setting.key)
        if value and len(value.encode()) < setting.minimum_bytes:
            weak.append(setting.env)
    admin = (os.environ.get("ADMIN_TOKEN") or "").strip()
    if admin and len(admin.encode()) < MINIMUM_CREDENTIAL_BYTES:
        weak.append("ADMIN_TOKEN")
    return weak


async def initialize_settings(repository: SettingsRepository, *, seed: bool = True) -> None:
    """Seed once, then adopt the database rather than reimporting the environment."""
    revision, encoded = await repository.read_settings()
    missing = {setting.storage_key for setting in MIGRATED_SETTINGS} - encoded.keys()
    if revision < 0 or missing:
        if seed:
            problems: list[str] = []
            revision, encoded = await repository.seed_settings(
                initial_settings(keys=missing if revision >= 0 else None, problems=problems)
            )
            from cloud_agent.logging_utils import get_logger

            for problem in problems:
                get_logger(__name__).warning("settings import: %s", problem)
        else:
            # Only the main listener has the complete legacy environment.
            # A relay racing it must never seed blank integration credentials.
            for _ in range(300):
                await asyncio.sleep(0.1)
                revision, encoded = await repository.read_settings()
                if revision >= 0 and all(
                    setting.storage_key in encoded for setting in MIGRATED_SETTINGS
                ):
                    break
            else:
                raise RuntimeError(
                    "The main control plane must initialize admin settings before the relay starts"
                )
    apply_settings(revision, encoded)
    from cloud_agent.logging_utils import get_logger

    for name in weak_credentials():
        get_logger(__name__).warning(
            "agent_weak_credential %s is shorter than %d bytes; "
            "replace it with a value from `openssl rand -base64 32`",
            name,
            MINIMUM_CREDENTIAL_BYTES,
            extra={"event": "agent_weak_credential", "credential": name},
        )


async def synchronize_settings(repository: SettingsRepository) -> None:
    """Refresh every listener's snapshot; failed reads retain the last committed values."""
    from cloud_agent.logging_utils import get_logger

    logger = get_logger(__name__)
    while True:
        await asyncio.sleep(5)
        try:
            with _lock:
                current_revision = _revision
            revision, encoded = await repository.read_settings(after_revision=current_revision)
            if revision >= 0:
                apply_settings(revision, encoded)
        except (ValueError, SettingsEncryptionError) as exc:
            # Raised by decoding the snapshot, and worded to name a setting
            # without its value; the type alone left nothing to act on.
            logger.warning("settings synchronization failed (%s: %s)", type(exc).__name__, exc)
        except Exception as exc:
            logger.warning("settings synchronization failed (%s)", type(exc).__name__)


def reset_settings() -> None:
    """Release the process snapshot when a listener stops (also useful for test isolation)."""
    global _values, _revision
    with _lock:
        _values, _revision = None, -1
