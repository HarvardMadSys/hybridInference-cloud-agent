"""The semantic configuration catalog the Admin pages are built from.

The P1 inventory classifies 369 environment names by where their value comes
from today. Administrators do not manage names; they manage functional items:
"control-plane listeners", "desired release", "settings root key". Each item
here says how it is managed (``method``), where each of its meanings comes
from (``sources``), which application-owned place manages it (``owner``) and
when a change takes effect.

The catalog is static: it reads no value, path, file or network address, so
it can be served to an administrator without leaking deployment data. Live
state (saved, adopted, applied, observed) comes from the status endpoints
that join it with evidence (``cloud_agent.management_status``).

``env_names`` ties an item back to the inventory. ``tests/unit/
test_management_catalog.py`` checks that every inventoried name that is not
platform, tooling or derived plumbing belongs to some item, so a new
deployment input cannot appear without an administrator-facing home.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal

from cloud_agent.setting_definition import APPLY_MODES
from cloud_agent.settings import SETTINGS
from cloud_agent_shared.host_settings import HOST_SETTINGS

#: How an item is managed (P1 section 4.2).
Method = Literal["edit", "apply", "workflow", "observe", "object"]
METHODS: tuple[Method, ...] = ("edit", "apply", "workflow", "observe", "object")
#: Where one meaning of an item comes from today.
SourceKind = Literal[
    "settings", "host_settings", "deployment_store", "mounted_file", "release", "observed"
]
#: Which machine's deployment agent applies a deployment item.
Role = Literal["control-plane", "host", "any"]


@dataclass(frozen=True)
class Source:
    """One meaning of an item: where its value comes from and who reads it."""

    kind: SourceKind
    reader: str
    meaning: str

    def public(self) -> dict[str, str]:
        """The source as the API returns it."""
        return {"kind": self.kind, "reader": self.reader, "meaning": self.meaning}


@dataclass(frozen=True)
class CatalogItem:
    """One administrator-facing configuration item or operation."""

    id: str
    label: str
    group: str
    method: Method
    sources: tuple[Source, ...]
    #: The application-owned place that manages it: a settings key prefix
    #: (``settings:<category>``), ``host_settings``, ``servers``,
    #: ``deployment:<operation>``, ``workflow:<kind>`` or ``object:<page>``.
    owner: str
    description: str
    apply_mode: str = "unverified"
    #: Services a change restarts, for items applied by a deployment agent.
    restart_scope: tuple[str, ...] = ()
    #: Which machines' agents apply it.
    role: Role | None = None
    #: Inventory names this item covers (exact names, or prefixes ending ``_``).
    env_names: tuple[str, ...] = ()
    #: Settings keys edited through this item.
    setting_keys: tuple[str, ...] = ()
    #: AGENTS.md section 7 boundary: only reviewed values the machine's local
    #: policy allows can be chosen, and only toward narrower protection.
    bounded: bool = False
    extra: dict[str, Any] = field(default_factory=dict)

    def public(self) -> dict[str, Any]:
        """The catalog entry as the API returns it; never a value."""
        result: dict[str, Any] = {
            "id": self.id,
            "label": self.label,
            "group": self.group,
            "method": self.method,
            "sources": [source.public() for source in self.sources],
            "owner": self.owner,
            "description": self.description,
            "apply_mode": self.apply_mode,
            "restart_scope": list(self.restart_scope),
            "role": self.role,
            "env_names": list(self.env_names),
            "setting_keys": list(self.setting_keys),
            "bounded": self.bounded,
        }
        result.update(self.extra)
        return result


_CP: Role = "control-plane"
_HOST: Role = "host"


def _setting_items() -> tuple[CatalogItem, ...]:
    """One edit item per settings category, in registry order."""
    categories: dict[str, list[str]] = {}
    for setting in SETTINGS:
        categories.setdefault(setting.category, []).append(setting.key)
    return tuple(
        CatalogItem(
            id="settings." + category.lower().replace(" ", "_").replace("-", "_"),
            label=category,
            group=category,
            method="edit",
            sources=(Source("settings", "control plane", "saved setting"),),
            owner=f"settings:{category}",
            description="Edited on the Settings page; each field states when it takes effect.",
            apply_mode="per_setting",
            setting_keys=tuple(keys),
            env_names=tuple(
                setting.env for setting in SETTINGS if setting.category == category and setting.env
            ),
        )
        for category, keys in categories.items()
    )


_HOST_ITEMS = (
    CatalogItem(
        id="hosts.runtime_settings",
        label="Server runtime settings",
        group="Servers",
        method="edit",
        sources=(
            Source("host_settings", "runner and sandbox manager", "value for all servers"),
            Source("host_settings", "runner and sandbox manager", "value for one server"),
            Source("observed", "each server", "local bootstrap value (settings_local)"),
        ),
        owner="host_settings",
        description=(
            "Saved for all servers or one server and delivered on the heartbeat; each "
            "server reports the values it applies."
        ),
        apply_mode="next_operation",
        env_names=tuple(setting.env for setting in HOST_SETTINGS),
        setting_keys=tuple(setting.key for setting in HOST_SETTINGS),
    ),
    CatalogItem(
        id="hosts.slots",
        label="Turn slots per server",
        group="Servers",
        method="edit",
        sources=(Source("host_settings", "runner", "admin limit per server"),),
        owner="servers",
        description="Set per server on the Servers page; delivered on the heartbeat.",
        apply_mode="next_operation",
        env_names=("AGENT_HOST_SLOTS",),
    ),
    CatalogItem(
        id="hosts.identity",
        label="Server identity and enrollment",
        group="Servers",
        method="object",
        sources=(
            Source("observed", "each server", "host name and physical server it reports"),
            Source("deployment_store", "runner", "enrollment token and host credential"),
        ),
        owner="object:servers",
        description=(
            "Enrollment tokens are issued and servers removed or re-enrolled on the Servers "
            "page; identity is reported, not edited."
        ),
        role=_HOST,
        env_names=(
            "AGENT_ENROLL_TOKEN",
            "AGENT_HOST_CREDENTIAL",
            "AGENT_HOST_CREDENTIAL_FILE",
            "AGENT_RUNNER_HOST",
            "AGENT_PHYSICAL_HOST",
            "AGENT_WORKER_ID",
        ),
    ),
    CatalogItem(
        id="hosts.topology",
        label="Server topology and paths",
        group="Deployment",
        method="apply",
        sources=(Source("deployment_store", "runner, sandbox manager, tunnels", "host .env"),),
        owner="deployment:apply_config",
        description=(
            "The sandbox manager address, tunnels, state directories and probes of one "
            "server. Applied by the server's deployment agent, or by the control plane's "
            "SSH executor for managed compute servers."
        ),
        apply_mode="restart",
        restart_scope=("runner", "sandbox-manager", "tunnels"),
        role=_HOST,
        env_names=(
            "AGENT_SANDBOX_MANAGER_URL",
            "AGENT_SANDBOX_MANAGER_ADVERTISE_URL",
            "AGENT_SANDBOX_MANAGER_REVERSE_BIND",
            "AGENT_SANDBOX_MANAGER_REVERSE_PORT",
            "AGENT_SANDBOX_MANAGER_REVERSE_TARGET_HOST",
            "AGENT_SANDBOX_MANAGER_REVERSE_TARGET_PORT",
            "AGENT_SANDBOX_MANAGER_STOP_GRACE",
            "AGENT_RUNNER_STOP_GRACE",
            "AGENT_CONTROL_PLANE_HOSTNAME",
            "AGENT_GATEWAY_HOSTNAME",
            "AGENT_MCP_HOSTNAME",
            "AGENT_GATEWAY_URL",
            "AGENT_SANDBOX_MCP_URL",
            "AGENT_SHARED_LIVE_READY_FILE",
            "AGENT_RUNTIME_DOWNLOAD_NETWORK",
            "AGENT_REPO_CLONE_BASE_URL",
            "AGENT_HOST_STATE_ROOT",
            "AGENT_HOST_CONTAINERD_CONTENT_DIR",
            "AGENT_CONTAINERD_CONTENT_DIR",
            "AGENT_SANDBOX_ARCHIVE_STATE_DIR",
            "AGENT_RETAINED_DOCKER_DATA_ROOT",
            "AGENT_UPLOAD_RECEIPT_DIR",
            "AGENT_HOST_CAPACITY_ENABLED",
            "AGENT_HOST_STORAGE_PROBE",
            "AGENT_SANDBOX_IMAGE_PULL",
            "COMPOSE_PROJECT",
            "SIBLING_COMPOSE_PROJECTS",
            "SHARED_READY_TIMEOUT",
            "AGENT_TUNNEL_",
            "AGENT_MCP_TUNNEL_",
            "AGENT_CONTROL_PLANE_TUNNEL_",
            "AGENT_CONTROL_PLANE_URL",
        ),
    ),
    CatalogItem(
        id="hosts.sandbox_boundary",
        label="Sandbox isolation and network",
        group="Deployment",
        method="apply",
        sources=(
            Source("deployment_store", "runner and sandbox manager", "host .env"),
            Source("observed", "each server", "runtime it actually runs (sandbox_runtime)"),
        ),
        owner="deployment:apply_config",
        description=(
            "Chosen only among values the server's local policy lists; protected ranges "
            "can only grow. Isolation-weakening switches are observed, never set here "
            "(AGENTS.md section 7)."
        ),
        apply_mode="restart",
        restart_scope=("runner", "sandbox-manager"),
        role=_HOST,
        bounded=True,
        env_names=(
            "AGENT_SANDBOX_BACKEND",
            "AGENT_SANDBOX_RUNTIME",
            "AGENT_SANDBOX_ALLOW_UNISOLATED",
            "AGENT_SANDBOX_EXTRA_ARGS",
            "AGENT_SANDBOX_UID",
            "AGENT_SANDBOX_GID",
            "AGENT_SANDBOX_HOME",
            "AGENT_SANDBOX_DOCKER",
            "AGENT_SANDBOX_IMAGE",
            "AGENT_SANDBOX_NAMESPACE",
            "AGENT_EGRESS_CHAT_NETWORK_POOL",
            "AGENT_EGRESS_DNS_SERVER",
            "AGENT_EGRESS_PROTECTED_CIDRS",
            "AGENT_RELAY_NETWORK",
        ),
    ),
    CatalogItem(
        id="hosts.kata",
        label="Kata runtime version",
        group="Deployment",
        method="apply",
        sources=(
            Source("release", "host recipe", "reviewed version and digest pairs"),
            Source("observed", "host agent", "installed version and digest"),
        ),
        owner="deployment:kata_install",
        description=(
            "Installs a reviewed version and digest pair after draining the server; the "
            "download source and paths are the server's local configuration."
        ),
        apply_mode="restart",
        restart_scope=("runner",),
        role=_HOST,
        bounded=True,
        env_names=(
            "KATA_VERSION",
            "KATA_SHA256",
            "KATA_DOWNLOAD_BASE",
            "KATA_LINK_DIR",
            "KATA_CONFIG_PATH",
            "KATA_DEBUG_KERNEL_PATH",
            "KATA_SETUP_SCRIPT",
        ),
    ),
    CatalogItem(
        id="hosts.tunnel_keys",
        label="Tunnel SSH keys",
        group="Security",
        method="workflow",
        sources=(Source("deployment_store", "tunnel containers", "key and known hosts files"),),
        owner="workflow:tunnel_keys",
        description=(
            "New keys are authorized beside the old ones on the far end; each server drains, "
            "switches, and its heartbeat over the new tunnel is the proof before the old keys "
            "are revoked. A far end nothing here can edit asks an administrator to change it."
        ),
        role=_HOST,
        env_names=(),
    ),
)

_CONTROL_PLANE_ITEMS = (
    CatalogItem(
        id="deployment.control_plane_listeners",
        label="Control-plane listeners and network",
        group="Deployment",
        method="apply",
        sources=(Source("deployment_store", "Compose", "control-plane .env"),),
        owner="deployment:apply_config",
        description=(
            "Bind addresses and ports of the API, MCP listener, web server and edge, and "
            "the Compose network. The agent checks the new address answers before keeping it."
        ),
        apply_mode="restart",
        restart_scope=("control-plane", "mcp-relay", "web", "edge"),
        role=_CP,
        env_names=(
            "AGENT_CONTROL_PLANE_BIND",
            "AGENT_CONTROL_PLANE_PORT",
            "AGENT_MCP_RELAY_BIND",
            "AGENT_MCP_RELAY_PORT",
            "AGENT_WEB_BIND",
            "AGENT_WEB_PORT",
            "AGENT_EDGE_BIND",
            "AGENT_EDGE_PORT",
            "AGENT_EDGE_CONFIG_FILE",
            "AGENT_NETWORK_NAME",
        ),
    ),
    CatalogItem(
        id="deployment.optional_services",
        label="Optional services",
        group="Deployment",
        method="apply",
        sources=(Source("deployment_store", "Compose", "COMPOSE_PROFILES"),),
        owner="deployment:apply_config",
        description="Turns the bundled pgAdmin and edge services on or off.",
        apply_mode="restart",
        restart_scope=("pgadmin", "edge"),
        role=_CP,
        env_names=("COMPOSE_PROFILES",),
    ),
    CatalogItem(
        id="deployment.pgadmin",
        label="pgAdmin service",
        group="Database tools",
        method="apply",
        sources=(
            Source("deployment_store", "pgAdmin and control plane", "first-time setup routing"),
            Source("deployment_store", "control plane", "services a saved pgAdmin URL may reach"),
        ),
        owner="deployment:apply_config",
        description=(
            "First-time setup address, cookie security and the deployment's own list of "
            "pgAdmin services. A service outside the bundled one needs its address here."
        ),
        apply_mode="restart",
        restart_scope=("pgadmin", "control-plane"),
        role=_CP,
        env_names=(
            "AGENT_PGADMIN_BOOTSTRAP_URL",
            "AGENT_PGADMIN_BOOTSTRAP_PUBLIC_URL",
            "AGENT_PGADMIN_BOOTSTRAP_DIR",
            "AGENT_PGADMIN_COOKIE_SECURE",
            "AGENT_PGADMIN_ENTRYPOINT_FILE",
            "AGENT_PGADMIN_SERVERS_FILE",
        ),
    ),
    CatalogItem(
        id="deployment.profiling",
        label="Operation profiling ceiling",
        group="Operations",
        method="apply",
        sources=(Source("deployment_store", "control plane and hosts", "profiling ceiling"),),
        owner="deployment:apply_config",
        description=(
            "The deployment's ceiling for operation profiling; the Admin profiling control "
            "works within it. Raising it widens what may be collected."
        ),
        apply_mode="restart",
        restart_scope=("control-plane", "runner"),
        role="any",
        env_names=(
            "AGENT_OPERATION_PROFILING_DEPLOYMENT",
            "AGENT_OPERATION_PROFILING_MODE",
            "AGENT_OPERATION_PROFILING_ENABLED",
            "AGENT_OPERATION_PROFILING_RUN",
            "AGENT_OPERATION_PROFILING_SINK",
        ),
    ),
    CatalogItem(
        id="deployment.web_routing",
        label="Web routing",
        group="Deployment",
        method="apply",
        sources=(
            Source("release", "web image", "build-time base path (variant)"),
            Source("deployment_store", "web server", "control-plane address"),
        ),
        owner="deployment:apply_config",
        description=(
            "The web variant (root or /agents) is chosen with the desired release; a base "
            "path change is submitted with the matching sign-in redirect."
        ),
        apply_mode="restart",
        restart_scope=("web",),
        role=_CP,
        env_names=(
            "AGENT_WEB_API_URL",
            "AGENT_WEB_BASE_PATH",
            "NEXT_PUBLIC_AGENT_API_URL",
            "NEXT_PUBLIC_BASE_PATH",
            "AGENT_WEB_VARIANT",
        ),
    ),
    CatalogItem(
        id="deployment.compose_override",
        label="Compose override file",
        group="Deployment",
        method="observe",
        sources=(Source("deployment_store", "Compose", "override file path"),),
        owner="deployment:observe",
        description="Shown when present. Its content is arbitrary container configuration and is not edited here.",
        role=_CP,
        env_names=("AGENT_CONTROL_PLANE_COMPOSE_OVERRIDE_FILE",),
    ),
    CatalogItem(
        id="deployment.mounted_files",
        label="Mounted keys and compute profiles",
        group="Compute",
        method="object",
        sources=(
            Source("mounted_file", "control plane", "GitHub App key file"),
            Source("mounted_file", "control plane", "trusted compute profiles"),
            Source("settings", "control plane", "inline GitHub App key (wins when set)"),
        ),
        owner="object:compute_setup",
        description=(
            "The inline GitHub App key and registered compute profiles are managed on their "
            "pages; file-backed ones are listed by source."
        ),
        role=_CP,
        env_names=(
            "AGENT_COMPUTE_CONFIG",
            "AGENT_COMPUTE_DIRECTORY",
            "AGENT_GITHUB_APP_PRIVATE_KEY_PATH",
            "AGENT_GITHUB_APP_PRIVATE_KEY_FILE",
        ),
    ),
    CatalogItem(
        id="compute.setup_prefill",
        label="Compute setup addresses",
        group="Compute",
        method="object",
        sources=(Source("deployment_store", "Compute setup form", "prefilled SSH addresses"),),
        owner="object:compute_setup",
        description="Prefills the automatic compute setup form; the saved request wins.",
        role=_CP,
        env_names=("AGENT_GATEWAY_SSH_HOST", "AGENT_CONTROL_PLANE_SSH_HOST"),
    ),
)

_RELEASE_ITEMS = (
    CatalogItem(
        id="release.desired",
        label="Desired release",
        group="Deployment",
        method="apply",
        sources=(Source("release", "deployment agents", "verified release lock on each machine"),),
        owner="deployment:release",
        description=(
            "Chosen from manifests each machine's agent has verified and imported. Hosts "
            "are prepared and activated before the control plane, as the release recipe "
            "requires."
        ),
        apply_mode="restart",
        restart_scope=("control-plane", "runner"),
        role="any",
        env_names=(
            "DEPLOY_SHA",
            "SOURCE_REPO",
            "HEALTH_TIMEOUT",
            "AGENT_DEPLOY_MODE",
            "AGENT_DEPLOY_DRAINING_HOST",
            "AGENT_RELEASE_MANIFEST",
        ),
    ),
    CatalogItem(
        id="release.identity",
        label="Running release",
        group="Deployment",
        method="observe",
        sources=(
            Source("observed", "control-plane instances", "build commit"),
            Source("observed", "hosts", "deployment commit on the heartbeat"),
            Source("observed", "deployment agents", "deployed commit and manifest"),
        ),
        owner="deployment:observe",
        description="What actually runs, from evidence; never edited.",
        env_names=(
            "AGENT_DEPLOYMENT_SHA",
            "AGENT_WEB_DEPLOYED_AT",
            "AGENT_RUNNER_VERSION",
            "AGENT_COMPUTE_SANDBOX_IMAGE_REPOSITORY",
            "BUILD_SHA",
            "BUILD_TIMESTAMP",
            "SOURCE_COMMIT",
            "NEXT_PUBLIC_BUILD_SHA",
            "NEXT_PUBLIC_BUILD_TIMESTAMP",
        ),
    ),
    CatalogItem(
        id="release.image_inputs",
        label="Image build inputs",
        group="Agent versions",
        method="observe",
        sources=(Source("release", "release manifest", "runtime catalog of the images"),),
        owner="deployment:observe",
        description=(
            "Part of the release. The Agent CLI version new chats use is managed under "
            "Agent versions."
        ),
        env_names=(
            "CLAUDE_CODE_VERSION",
            "CLAUDE_CODE_VERSIONS",
            "CLAUDE_CODE_EXTRA_VERSIONS",
            "CODEX_VERSION",
            "CODEX_VERSIONS",
            "CODEX_EXTRA_VERSIONS",
            "KILO_VERSION",
            "KILO_VERSIONS",
            "KILO_EXTRA_VERSIONS",
            "OPENCODE_VERSION",
            "OPENCODE_VERSIONS",
            "OPENCODE_EXTRA_VERSIONS",
            "PI_VERSION",
            "PI_VERSIONS",
            "PI_EXTRA_VERSIONS",
            "DEBIAN_MIRROR",
            "PIP_DEFAULT_TIMEOUT",
            "UV_HTTP_TIMEOUT",
            "UV_VERSION",
        ),
    ),
    CatalogItem(
        id="deployment.agent_install",
        label="Deployment agent installation",
        group="Deployment",
        method="observe",
        sources=(Source("deployment_store", "deployment agent", "its own install paths"),),
        owner="deployment:observe",
        description="Where each machine's agent and deployment root live; set on the machine.",
        role="any",
        env_names=("DEPLOY_ROOT", "APP_DIR", "AGENT_DEPLOY_PYTHON", "AGENT_CONTROL_PLANE_MACHINE"),
    ),
    CatalogItem(
        id="deployment.legacy_names",
        label="Retired variable names",
        group="Deployment",
        method="observe",
        sources=(Source("deployment_store", "deployment agent", "names still present"),),
        owner="deployment:observe",
        description="Reported by each agent so a retired name can be removed from its .env.",
        role="any",
        env_names=(
            "AGENT_WORKSPACE_BROKER_TOKEN",
            "AGENT_WORKSPACE_BROKER_URL",
            "AGENT_WORKSPACE_BROKER_ADVERTISE_URL",
            "AGENT_BROKER_URL_ALLOWED_HOSTS",
            "AGENT_BROKER_REVERSE_BIND",
            "AGENT_BROKER_REVERSE_PORT",
            "AGENT_BROKER_REVERSE_TARGET_HOST",
            "AGENT_BROKER_REVERSE_TARGET_PORT",
            "AGENT_BROKER_STOP_GRACE",
            "AGENT_RUNNER_REPLICAS",
        ),
    ),
)

_WORKFLOW_ITEMS = (
    CatalogItem(
        id="security.admin_credentials",
        label="Administrator API credentials",
        group="Security",
        method="object",
        sources=(Source("settings", "control plane", "one-way verifier per credential"),),
        owner="object:admin_credentials",
        description=(
            "Named, revocable credentials for administrator automation, shown once when "
            "created. They never replace the deployment's ADMIN_TOKEN."
        ),
    ),
    CatalogItem(
        id="security.admin_token",
        label="Deployment ADMIN_TOKEN",
        group="Security",
        method="workflow",
        sources=(Source("deployment_store", "API and MCP listener", "full administrator token"),),
        owner="workflow:admin_token",
        description=(
            "Grants full administrator access on every request and opens /setup when "
            "Gateway sign-in is broken. Rotated through the control-plane machines' agents."
        ),
        apply_mode="restart",
        restart_scope=("control-plane", "mcp-relay"),
        role=_CP,
        env_names=("ADMIN_TOKEN",),
    ),
    CatalogItem(
        id="security.settings_root_key",
        label="Settings root key",
        group="Security",
        method="workflow",
        sources=(
            Source("deployment_store", "control plane", "dedicated root key"),
            Source("deployment_store", "control plane", "AGENT_SESSION_SECRET fallback root"),
        ),
        owner="workflow:root_key",
        description=(
            "Decrypts the settings row. First separated from the session secret with the "
            "same bytes, then rotated with re-encryption; never stored in the database."
        ),
        apply_mode="restart",
        restart_scope=("control-plane", "mcp-relay"),
        role=_CP,
        env_names=("AGENT_SETTINGS_ENCRYPTION_KEY", "AGENT_SETTINGS_ENCRYPTION_KEY_PREVIOUS"),
    ),
    CatalogItem(
        id="security.key_rotation",
        label="Signing key and sandbox credential rotation",
        group="Security",
        method="workflow",
        sources=(Source("settings", "control plane", "saved keys"),),
        owner="workflow:key_rotation",
        env_names=("AGENT_SANDBOX_MANAGER_TOKEN_NEXT",),
        description=(
            "Host signing key (every server renews its credential and earlier tokens drain "
            "before the previous key is retired) and sandbox manager credential (every server, "
            "through its agent or the SSH executor, accepts both during the switch)."
        ),
        setting_keys=(
            "control_signing_secret",
            "control_signing_secret_previous",
            "sandbox_manager_token",
        ),
    ),
    CatalogItem(
        id="security.credential_keys",
        label="Credential encryption keys",
        group="Security",
        method="workflow",
        sources=(Source("settings", "control plane", "saved keys"),),
        owner="workflow:credential_key",
        description=(
            "The user credential key and the compute credential key. Every stored value is "
            "re-encrypted, and environment fingerprints rebuilt, before the previous key is "
            "dropped; both keep working throughout."
        ),
        apply_mode="next_operation",
        setting_keys=(
            "source_control_encryption_key",
            "source_control_encryption_key_previous",
            "compute_encryption_key",
            "compute_encryption_key_previous",
            "credential_index_uses_previous",
        ),
    ),
    CatalogItem(
        id="database.connection",
        label="Database connection",
        group="Database",
        method="workflow",
        sources=(
            Source("deployment_store", "control plane and migrations", "connection URL"),
            Source("deployment_store", "bundled PostgreSQL", "database, user and password"),
        ),
        owner="workflow:database_connection",
        description=(
            "Changes the address or credentials of the same database after proving, by "
            "live probes, that the new connection reaches it. Data is not moved."
        ),
        apply_mode="restart",
        restart_scope=("control-plane", "mcp-relay"),
        role=_CP,
        env_names=(
            "AGENT_DATABASE_URL",
            "AGENT_POSTGRES_DB",
            "AGENT_POSTGRES_USER",
            "AGENT_POSTGRES_PASSWORD",
        ),
    ),
    CatalogItem(
        id="database.migration",
        label="Move to another database",
        group="Database",
        method="workflow",
        sources=(Source("deployment_store", "deployment agent", "migration journal"),),
        owner="workflow:database_migration",
        description=(
            "Fences every writer, copies into an empty target, verifies it and switches. "
            "Once writes resume on the target the source is never switched back to "
            "automatically."
        ),
        apply_mode="restart",
        restart_scope=("control-plane", "mcp-relay"),
        role=_CP,
    ),
)

CATALOG: tuple[CatalogItem, ...] = (
    *_setting_items(),
    *_HOST_ITEMS,
    *_CONTROL_PLANE_ITEMS,
    *_RELEASE_ITEMS,
    *_WORKFLOW_ITEMS,
)
CATALOG_BY_ID: dict[str, CatalogItem] = {item.id: item for item in CATALOG}

#: Inventoried names with no administrator-facing item, and why (P1 section 7.5).
NOT_MANAGED: dict[str, str] = {
    "ALLOW_DIRTY": "bypasses the deploy script's clean-checkout check; never offered",
    "ALLOW_STRAY_STACK": "bypasses the deploy script's stray-stack check; never offered",
    "AGENT_MEMBER_PROCESS_REAPER_SOURCE": "image-internal path",
}
#: Inventory families (by disposition) with no item: platform variables,
#: derived plumbing set by our own code, tests and the local stack.
NOT_MANAGED_DISPOSITIONS = frozenset({"internal", "platform", "tooling"})


def catalog_response() -> dict[str, Any]:
    """The whole catalog, as ``GET /admin/management/catalog`` returns it."""
    return {
        "methods": list(METHODS),
        "apply_modes": list(APPLY_MODES),
        "items": [item.public() for item in CATALOG],
    }


def item_for_env(name: str) -> CatalogItem | None:
    """The item covering an inventoried name, exactly or by prefix."""
    for item in CATALOG:
        for covered in item.env_names:
            if name == covered or (covered.endswith("_") and name.startswith(covered)):
                return item
    return None


__all__ = [
    "CATALOG",
    "CATALOG_BY_ID",
    "METHODS",
    "NOT_MANAGED",
    "NOT_MANAGED_DISPOSITIONS",
    "CatalogItem",
    "Source",
    "catalog_response",
    "item_for_env",
]
