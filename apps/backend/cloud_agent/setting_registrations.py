"""Settings registered for frontend management after the settings row existed.

Kept beside the registry in :mod:`cloud_agent.settings` so that module stays
within its size budget. Every definition here is in ``MIGRATED_SETTINGS``: an
existing database imports its environment value once.
"""

from __future__ import annotations

from cloud_agent.setting_definition import ApplyMode, Setting, _numeric

LATER_MIGRATED_DEFINITIONS: tuple[Setting, ...] = (
    Setting(
        "control_signing_secret_previous",
        "Previous host and worker signing key",
        "Security",
        "",
        secret=True,
        description=(
            "Set only by the signing key rotation: credentials signed with the previous key "
            "keep verifying until the rotation retires it. Never used to sign."
        ),
        apply_mode="next_operation",
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
        "model_connections_enabled",
        "Owners' own model connections",
        "Execution",
        False,
        kind="boolean",
        env="AGENT_MODEL_CONNECTIONS_ENABLED",
        description=(
            "Lets owners add their own inference endpoints and run turns on them. It can be "
            "turned on only while every control-plane process reports that it supports "
            "model connections and no other session is open on the database: an older "
            "process would run a connection's turn as a Gateway turn."
        ),
        apply_mode="next_operation",
        apply_description="Checked when an owner adds a connection or starts a turn on one.",
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
IMPORTED_AFTER_REGISTRATION = frozenset(
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
#: When a saved value takes effect, for settings whose readers were checked
#: (settings design S14-S16). Everything else stays ``unverified``: saving is
#: not a promise that every process already uses the value.
VERIFIED_APPLY: dict[str, tuple[ApplyMode, str]] = {
    "log_level": (
        "next_operation",
        "Each process reconfigures logging when it adopts the snapshot.",
    ),
    "log_format": (
        "next_operation",
        "Each process reconfigures logging when it adopts the snapshot.",
    ),
    "mcp_allow_private_targets": ("restart", "MCP listener: new MCP transports after it restarts."),
    "mcp_allow_plain_http": ("restart", "MCP listener: new MCP transports after it restarts."),
    "default_execution_profile": ("new_session", "New root sessions; existing ones keep theirs."),
    "default_model": ("new_session", "New tasks whose owner has not chosen a model."),
    "session_signing_secret": ("next_operation", "Existing browser sessions are signed out."),
    "control_signing_secret": (
        "host_reenroll",
        "Existing host and worker credentials stop working until hosts re-enroll.",
    ),
    "gateway_grant_base_path": ("next_operation", "Grant clients read it per call."),
}

__all__ = ["IMPORTED_AFTER_REGISTRATION", "LATER_MIGRATED_DEFINITIONS", "VERIFIED_APPLY"]
