"""Every environment variable this repository names, and where each one belongs.

``tests/unit/test_env_classification.py`` holds the code to this table: a
variable read by Python (``apps/backend``, ``deploy``, ``scripts``), consumed as
an input by a shell script under ``deploy`` or ``scripts``, or named by a
Compose file, an example env file, a Dockerfile ``ARG`` or the web app must
have exactly one entry here, and an entry nothing names any more must go. The
table lives with the tests, not in an image: nothing at runtime reads it.

Dispositions say where a variable's value should come from, not every fact
about it. Secrets, restart needs and irreversibility are orthogonal and stay in
each reason. ``docs/agents/specs/2026-10-29-env-inventory-and-gates.zh-CN.md``
explains each family and what moving one would take.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

Disposition = Literal[
    # A registered admin setting (``cloud_agent.settings.SETTINGS``): the
    # environment is imported once into the settings row, then the database
    # is authoritative.
    "admin",
    # Set from the admin pages and delivered to hosts on the heartbeat
    # (``cloud_agent_shared.host_settings.HOST_SETTINGS``, or a host's slots).
    # The host's environment is only its value before the first answer.
    "host_admin",
    # Not admin-managed yet, and could be with the existing machinery.
    "candidate",
    # Could leave the environment only after a design of its own.
    "design",
    # Startup, bootstrap, topology, release identity, isolation boundaries,
    # build inputs: the deployment owns the value.
    "deployment",
    # A deployment value whose change is a dedicated procedure (key rotation).
    "operation",
    # Set by this code for a process it starts; an operator never sets it.
    "internal",
    # Defined by the platform or a third-party image (PATH, DOCKER_HOST, AWS_*).
    "platform",
    # A former name, still mapped for an upgrade, or no longer read at all.
    "retired",
    # Tests, CI, benchmarks and the local development stack only.
    "tooling",
]


@dataclass(frozen=True)
class Family:
    """Variables that share a disposition for one reason."""

    disposition: Disposition
    reason: str
    names: tuple[str, ...] = ()
    #: Each covers every name that starts with it.
    prefixes: tuple[str, ...] = ()


FAMILIES: tuple[Family, ...] = (
    # -- admin: cloud_agent.settings.SETTINGS ------------------------------
    Family(
        "admin",
        "Gateway sign-in and grant dispatch. GATEWAY_ISSUER locks once the database is "
        "bound to an identity namespace; the dispatch token must be re-entered when the "
        "Gateway URL changes (validate_settings_update).",
        names=(
            "GATEWAY_BASE_URL",
            "GATEWAY_GRANT_DISPATCH_TOKEN",
            "GATEWAY_GRANT_BASE_PATH",
            "GATEWAY_GRANT_BINDING_ID",
            "GATEWAY_AUTHORIZE_URL",
            "GATEWAY_JWKS_URL",
            "GATEWAY_ISSUER",
            "AGENT_SESSION_REDIRECT_URI",
            "AGENT_WEB_BASE_URL",
            "AGENT_SESSION_COOKIE_SECURE",
            "AGENT_TRUST_PROXY_HEADERS",
        ),
    ),
    Family(
        "admin",
        "Signing and encryption keys stored as settings. Replacing the session key signs "
        "browsers out; replacing the control key invalidates host and worker credentials "
        "(hosts re-enroll); the two encryption keys lock once set and need re-encryption. "
        "AGENT_SESSION_SECRET is also read raw as the settings root key's fallback "
        "(domain_cipher) while AGENT_SETTINGS_ENCRYPTION_KEY is unset.",
        names=(
            "AGENT_SESSION_SECRET",
            "AGENT_CONTROL_TOKEN_SECRET",
            "AGENT_SOURCE_CONTROL_ENCRYPTION_KEY",
            "AGENT_COMPUTE_SECRET_KEY",
        ),
    ),
    Family(
        "admin",
        "Source control and publication. The inline App key falls back to the mounted "
        "AGENT_GITHUB_APP_PRIVATE_KEY_PATH when empty.",
        names=(
            "AGENT_REPO_ALLOWLIST",
            "AGENT_GITHUB_APP_ID",
            "AGENT_GITHUB_APP_CLIENT_ID",
            "AGENT_GITHUB_APP_CLIENT_SECRET",
            "AGENT_GITHUB_APP_PRIVATE_KEY",
            "AGENT_GITHUB_APP_INSTALL_URL",
            "AGENT_GITHUB_API",
            "AGENT_GITLAB_OAUTH_CLIENT_ID",
            "AGENT_GITLAB_OAUTH_CLIENT_SECRET",
            "AGENT_GITLAB_OAUTH_REDIRECT_URI",
            "AGENT_GITHUB_TOKEN",
            "AGENT_PUBLISH_BASE_BRANCH",
            "AGENT_GIT_AUTHOR_NAME",
            "AGENT_GIT_AUTHOR_EMAIL",
        ),
    ),
    Family(
        "admin",
        "MCP sign-in and dialing policy. The two ALLOW flags take effect for new MCP "
        "transports after the MCP listener restarts.",
        names=(
            "AGENT_MCP_PUBLIC_BASE_URL",
            "AGENT_MCP_GITHUB_CLIENT_ID",
            "AGENT_MCP_GITHUB_CLIENT_SECRET",
            "AGENT_MCP_ALLOW_PRIVATE_TARGETS",
            "AGENT_MCP_ALLOW_PLAIN_HTTP",
        ),
    ),
    Family(
        "admin",
        "Control-plane operations. The control plane's own logging reads LOG_LEVEL/FORMAT "
        "raw before the first snapshot; host processes read the same names as their own "
        "deployment setting (see RAW_SETTING_READS).",
        names=(
            "AGENT_LOG_LEVEL",
            "AGENT_LOG_FORMAT",
            "AGENT_ALERT_WEBHOOK_URL",
            "AGENT_ONCALL_USER_ID",
            "AGENT_ONCALL_DISPATCH_TOKEN",
            "AGENT_RESIDENCY_TTL_S",
        ),
    ),
    Family(
        "admin",
        "Appearance. The web server reads the saved values from the control plane; its "
        "own AGENT_WEB_* environment is a legacy fallback only when AGENT_CONTROL_PLANE_URL "
        "is unset (apps/frontend/src/config/site.server.ts).",
        names=(
            "AGENT_WEB_APP_NAME",
            "AGENT_WEB_ORG_NAME",
            "AGENT_WEB_ORG_URL",
            "AGENT_WEB_STATUS_URL",
            "AGENT_WEB_DOCS_URL",
            "AGENT_WEB_CONTACT_URL",
            "AGENT_WEB_GATEWAY_URL",
            "AGENT_WEB_FEATURE_RAG",
        ),
    ),
    Family(
        "admin",
        "Built-in web search. Registered, but neither ENVIRONMENT.md nor the supported "
        "control-plane Compose file passes them, so their first import only happens "
        "through an override.",
        names=(
            "WEB_SEARCH_PROVIDER",
            "SERPER_API_KEY",
            "BRAVE_SEARCH_API_KEY",
            "PARALLEL_API_KEY",
        ),
    ),
    Family(
        "admin",
        "Compute provider endpoints. The trusted deployment profiles stay in AGENT_COMPUTE_CONFIG.",
        names=("AGENT_CLOUDLAB_API_URL", "AGENT_COMPUTE_REGISTRATION_URL"),
    ),
    Family(
        "admin",
        "Sandbox archives and database tools (MIGRATED_SETTINGS). A saved pgAdmin URL is "
        "proxied only when the deployment also names that service "
        "(AGENT_PGADMIN_URL/AGENT_PGADMIN_BOOTSTRAP_URL, read raw as the allowlist).",
        names=(
            "AGENT_SANDBOX_ARCHIVE_S3_BUCKET",
            "AGENT_SANDBOX_ARCHIVE_S3_REGION",
            "AGENT_SANDBOX_ARCHIVE_S3_ENDPOINT",
            "AGENT_SANDBOX_ARCHIVE_S3_PREFIX",
            "AGENT_PGADMIN_URL",
            "AGENT_PGADMIN_PUBLIC_URL",
        ),
    ),
    Family(
        "admin",
        "Execution policy (MIGRATED_SETTINGS). Hosts hold their own copy of "
        "AGENT_SANDBOX_MANAGER_TOKEN; managed hosts get the value saved when they were set "
        "up, so a change is coordinated with every host.",
        names=(
            "AGENT_SANDBOX_MANAGER_TOKEN",
            "AGENT_SANDBOX_MANAGER_URL_ALLOWED_HOSTS",
            "AGENT_DEFAULT_EXECUTION_PROFILE",
            "AGENT_DEFAULT_MODEL",
        ),
    ),
    # -- host_admin: heartbeat-delivered host settings --------------------
    Family(
        "host_admin",
        "HOST_SETTINGS read by the runner: global value, then per-host value, from the "
        "heartbeat answer; the host's environment applies only before the first answer.",
        names=(
            "AGENT_SANDBOX_CPU_MILLICORES",
            "AGENT_SANDBOX_MEMORY_MIB",
            "AGENT_SANDBOX_DISK_MIB",
            "AGENT_TIMEOUT_S",
            "AGENT_SOURCE_IMPORT_MAX_MIB",
            "AGENT_CHAT_IDLE_TIMEOUT_S",
            "AGENT_STORAGE_MIN_AVAILABLE_MIB",
            "AGENT_SHARED_RESIDENT_CAPACITY",
        ),
    ),
    Family(
        "host_admin",
        "HOST_SETTINGS the runner hands to the sandbox manager, same precedence.",
        names=(
            "AGENT_TERMINAL_IDLE_TTL_SECONDS",
            "AGENT_TERMINAL_HARD_TTL_SECONDS",
            "AGENT_TERMINAL_MAX_SESSIONS",
            "AGENT_TERMINAL_MAX_SESSIONS_PER_WORKSPACE",
            "AGENT_WORKSPACE_COMMAND_TIMEOUT_S",
            "AGENT_WORKSPACE_FILE_WRITE_MAX_MIB",
            "AGENT_WORKSPACE_FILE_PREVIEW_MAX_KIB",
            "AGENT_WORKSPACE_COMMAND_OUTPUT_MAX_MIB",
            "AGENT_WORKSPACE_DIRECTORY_MAX_ENTRIES",
            "AGENT_WORKSPACE_DOWNLOAD_MAX_MIB",
            "AGENT_TERMINAL_BUFFER_MAX_MIB",
            "AGENT_TERMINAL_BUFFER_MAX_EVENTS",
            "AGENT_TERMINAL_OUTPUT_MAX_MIB",
            "AGENT_TERMINAL_INPUT_MAX_KIB",
            "AGENT_TERMINAL_MAX_STREAMS_PER_SESSION",
        ),
    ),
    Family(
        "host_admin",
        "A host's turn slots: an admin number from the Servers page "
        "(PUT /admin/agent/runner-hosts/<host>/slots) arrives on the heartbeat; the "
        "environment is the host's default until then. Not part of HOST_SETTINGS.",
        names=("AGENT_HOST_SLOTS",),
    ),
    # -- candidate: could move with existing machinery --------------------
    Family(
        "candidate",
        "Per-process cap on one account's open event streams, read on every acquire by "
        "the control plane only. Ready for MIGRATED_SETTINGS so existing deployments "
        "import their value once.",
        names=("AGENT_MAX_STREAMS_PER_USER",),
    ),
    Family(
        "candidate",
        "The lease the runner asks for on each claim (the control plane caps it at 900 s). "
        "Read once into an argparse default; HOST_SETTINGS can carry it once each claim "
        "uses the effective value and the bound stays above one heartbeat plus a request.",
        names=("AGENT_LEASE_TTL",),
    ),
    # -- design: needs a design of its own --------------------------------
    Family(
        "design",
        "Read by both the control plane (version listing) and every runner (download); "
        "the two must name the same registry, and no channel keeps them equal yet.",
        names=("AGENT_RUNTIME_REGISTRY_URL",),
    ),
    Family(
        "design",
        "Read through setting_env but not registered, so each replica follows its own "
        "environment. Safe enablement depends on every control-plane replica in the "
        "cluster understanding model connections, which nothing records yet.",
        names=("AGENT_MODEL_CONNECTIONS_ENABLED",),
    ),
    Family(
        "design",
        "Read while the FastAPI app is built, before any settings snapshot exists; moving "
        "it means deciding the documentation routes per request.",
        names=("AGENT_API_DOCS",),
    ),
    # -- deployment -------------------------------------------------------
    Family(
        "deployment",
        "Control-plane bootstrap: needed before the settings row can be read, or the "
        "recovery door when Gateway sign-in is broken.",
        names=("AGENT_DATABASE_URL", "ADMIN_TOKEN"),
    ),
    Family(
        "deployment",
        "Mounted files and their paths, and the trusted compute profiles that reference "
        "deployment secrets; never accepted through the Admin API.",
        names=(
            "AGENT_COMPUTE_CONFIG",
            "AGENT_COMPUTE_DIRECTORY",
            "AGENT_GITHUB_APP_PRIVATE_KEY_PATH",
            "AGENT_GITHUB_APP_PRIVATE_KEY_FILE",
        ),
    ),
    Family(
        "deployment",
        "pgAdmin first-time setup routing and its private volume.",
        names=(
            "AGENT_PGADMIN_BOOTSTRAP_URL",
            "AGENT_PGADMIN_BOOTSTRAP_PUBLIC_URL",
            "AGENT_PGADMIN_BOOTSTRAP_DIR",
            "AGENT_PGADMIN_COOKIE_SECURE",
            "AGENT_PGADMIN_ENTRYPOINT_FILE",
            "AGENT_PGADMIN_SERVERS_FILE",
        ),
    ),
    Family(
        "deployment",
        "Release identity and where this release published its images: evidence the "
        "deployment states about itself, matched exactly by claims and rollouts.",
        names=(
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
    Family(
        "deployment",
        "Machine addresses the deployer knows, used only to prefill the automatic compute "
        "setup form; a saved request wins.",
        names=("AGENT_GATEWAY_SSH_HOST", "AGENT_CONTROL_PLANE_SSH_HOST"),
    ),
    Family(
        "deployment",
        "Control-plane listeners, network, database container and Compose wiring.",
        names=(
            "AGENT_CONTROL_PLANE_BIND",
            "AGENT_CONTROL_PLANE_PORT",
            "AGENT_CONTROL_PLANE_COMPOSE_OVERRIDE_FILE",
            "AGENT_EDGE_BIND",
            "AGENT_EDGE_PORT",
            "AGENT_EDGE_CONFIG_FILE",
            "AGENT_MCP_RELAY_BIND",
            "AGENT_MCP_RELAY_PORT",
            "AGENT_WEB_BIND",
            "AGENT_WEB_PORT",
            "AGENT_NETWORK_NAME",
            "AGENT_POSTGRES_DB",
            "AGENT_POSTGRES_USER",
            "AGENT_POSTGRES_PASSWORD",
            "AGENT_OPERATION_PROFILING_DEPLOYMENT",
        ),
    ),
    Family(
        "deployment",
        "The web image's build-time routing (substituted at next build) and the web "
        "server's control-plane address.",
        names=(
            "AGENT_WEB_API_URL",
            "AGENT_WEB_BASE_PATH",
            "NEXT_PUBLIC_AGENT_API_URL",
            "NEXT_PUBLIC_BASE_PATH",
            "AGENT_CONTROL_PLANE_URL",
        ),
    ),
    Family(
        "deployment",
        "Host identity and enrollment: needed before the first heartbeat, and stated by "
        "the host about itself.",
        names=(
            "AGENT_ENROLL_TOKEN",
            "AGENT_HOST_CREDENTIAL",
            "AGENT_HOST_CREDENTIAL_FILE",
            "AGENT_RUNNER_HOST",
            "AGENT_PHYSICAL_HOST",
            "AGENT_WORKER_ID",
        ),
    ),
    Family(
        "deployment",
        "Host topology, mounts and paths: the sandbox manager's address and reverse "
        "tunnel, relay addresses the host rules admit, state directories, capacity "
        "probes, rollout fences and the host Compose project.",
        names=(
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
        ),
        prefixes=("AGENT_TUNNEL_", "AGENT_MCP_TUNNEL_", "AGENT_CONTROL_PLANE_TUNNEL_"),
    ),
    Family(
        "deployment",
        "Sandbox isolation and the sandbox network (AGENTS.md section 7). A value here can "
        "weaken isolation or widen what a sandbox reaches, so it never becomes an admin "
        "setting; changing the boundary is a design change.",
        names=(
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
    Family(
        "deployment",
        "Operation profiling: MODE is the deployment's ceiling (disabled refuses even the "
        "Admin control); ENABLED/RUN are the static environment experiment every producer "
        "parses alike; SINK is a file path.",
        names=(
            "AGENT_OPERATION_PROFILING_MODE",
            "AGENT_OPERATION_PROFILING_ENABLED",
            "AGENT_OPERATION_PROFILING_RUN",
            "AGENT_OPERATION_PROFILING_SINK",
        ),
    ),
    Family(
        "deployment",
        "Image build inputs: pinned and extra Agent CLI versions, mirrors and tool versions.",
        names=(
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
    Family(
        "deployment",
        "Inputs of the deployment entry points (deploy/*/deploy.sh, sandbox_host.sh, "
        "deploy/release/artifact_env.sh): what to deploy, from where, into which "
        "directory, how long to wait, and the artifact release being installed.",
        names=(
            "DEPLOY_SHA",
            "SOURCE_REPO",
            "DEPLOY_ROOT",
            "APP_DIR",
            "ALLOW_DIRTY",
            "ALLOW_STRAY_STACK",
            "HEALTH_TIMEOUT",
            "AGENT_DEPLOY_MODE",
            "AGENT_DEPLOY_PYTHON",
            "AGENT_DEPLOY_DRAINING_HOST",
            "AGENT_RELEASE_MANIFEST",
            "AGENT_WEB_VARIANT",
        ),
    ),
    Family(
        "deployment",
        "The Kata runtime a host installs (setup_kata_runtime.sh overrides): the host's "
        "isolation boundary, pinned and verified by digest.",
        names=(
            "KATA_VERSION",
            "KATA_SHA256",
            "KATA_DOWNLOAD_BASE",
            "KATA_LINK_DIR",
            "KATA_CONFIG_PATH",
            "KATA_DEBUG_KERNEL_PATH",
            "KATA_SETUP_SCRIPT",
        ),
    ),
    # -- operation --------------------------------------------------------
    Family(
        "operation",
        "The settings root key: needed to decrypt the settings row itself. Without it, "
        "the environment's AGENT_SESSION_SECRET is the fallback root; changing the root in "
        "use is a re-encryption procedure.",
        names=("AGENT_SETTINGS_ENCRYPTION_KEY",),
    ),
    # -- internal ---------------------------------------------------------
    Family(
        "internal",
        "Handed by the host to a process it starts for one member or action: the "
        "protocol-compatibility launcher, the live bridge, the CLI wrappers, environment "
        "scripts and the snapshot secret scan.",
        names=(
            "AGENT_COMPAT_CLIENT",
            "AGENT_COMPAT_UPSTREAM_URL",
            "AGENT_COMPAT_UPSTREAM_KEY",
            "AGENT_COMPAT_MODEL",
            "AGENT_COMPAT_TOKEN",
            "AGENT_LIVE_SESSION_ID",
            "AGENT_LIVE_WORKDIR",
            "AGENT_MCP_SERVERS",
            "AGENT_MCP_TOKEN",
            "AGENT_PLUGIN_PATHS",
            "AGENT_SCRIPT_STATUS",
            "AGENT_ENVIRONMENT_SCRIPT",
            "KILO_FREEINFERENCE_CONFIG_DIR",
            "KILO_GATEWAY_MODEL",
            "KILO_GATEWAY_REASONING_EFFORT",
            "OPENCODE_FREEINFERENCE_CONFIG_DIR",
            "OPENCODE_GATEWAY_MODEL",
            "OPENCODE_GATEWAY_REASONING_EFFORT",
            "PI_CODING_AGENT_DIR",
            "PI_GATEWAY_MODEL",
            "PI_GATEWAY_REASONING",
            "OPENAI_API_KEY",
            "OPENAI_BASE_URL",
        ),
        prefixes=("AGENT_SECRET_PATTERN_",),
    ),
    Family(
        "internal",
        "Image-internal override of the member process reaper's source path; the image "
        "supplies the default.",
        names=("AGENT_MEMBER_PROCESS_REAPER_SOURCE",),
    ),
    Family(
        "internal",
        "Compose maps the operator's AGENT_*TUNNEL_* values onto the tunnel container's "
        "own names; a deploy script hands its child a held lock.",
        names=(
            "_KATA_RUNTIME_EPOCH_LOCK_FD",
            "TUNNEL_LISTEN_PORT",
            "TUNNEL_REMOTE_HOST",
            "TUNNEL_REMOTE_PORT",
            "TUNNEL_REVERSE_BIND",
            "TUNNEL_REVERSE_PORT",
            "TUNNEL_REVERSE_TARGET_HOST",
            "TUNNEL_REVERSE_TARGET_PORT",
            "TUNNEL_SSH_DESTINATION",
            "TUNNEL_SSH_PORT",
            "TUNNEL_SSH_KEY",
            "TUNNEL_SSH_KNOWN_HOSTS",
        ),
    ),
    Family(
        "internal",
        "Set by a deploy script for a script or command it runs itself: host deploy.sh "
        "defers sandbox_host.sh's shared-ready marker, and the control-plane deploy.sh "
        "marks its own readiness run.",
        names=("DEFER_SHARED_READY_MARKER", "WATCH_READINESS"),
    ),
    # -- platform ---------------------------------------------------------
    Family(
        "platform",
        "Process, locale and Docker client variables defined by the OS, Docker or sshd; "
        "read to start children or by an embedded remote script.",
        names=(
            "PATH",
            "HOME",
            "LANG",
            "LC_ALL",
            "TERM",
            "TMPDIR",
            "HOSTNAME",
            "DOCKER_HOST",
            "DOCKER_CONTEXT",
            "DOCKER_CONFIG",
            "SSH_ORIGINAL_COMMAND",
            "NODE_ENV",
            "COMPOSE_PROFILES",
        ),
    ),
    Family(
        "platform",
        "Third-party images and SDKs: the AWS SDK's credential chain for sandbox archives, "
        "and the PostgreSQL and pgAdmin images' own settings.",
        names=(
            "AWS_ACCESS_KEY_ID",
            "AWS_SECRET_ACCESS_KEY",
            "AWS_SESSION_TOKEN",
            "AWS_DEFAULT_REGION",
            "POSTGRES_DB",
            "POSTGRES_USER",
            "POSTGRES_PASSWORD",
        ),
        prefixes=("PGADMIN_",),
    ),
    # -- retired ----------------------------------------------------------
    Family(
        "retired",
        "Former Workspace Broker names (LEGACY_ENV_NAMES), still accepted by manager_env "
        "and the Compose fallbacks until the COMPAT marker on LEGACY_ENV_NAMES expires.",
        names=(
            "AGENT_WORKSPACE_BROKER_TOKEN",
            "AGENT_WORKSPACE_BROKER_URL",
            "AGENT_WORKSPACE_BROKER_ADVERTISE_URL",
            "AGENT_BROKER_URL_ALLOWED_HOSTS",
            "AGENT_BROKER_REVERSE_BIND",
            "AGENT_BROKER_REVERSE_PORT",
            "AGENT_BROKER_REVERSE_TARGET_HOST",
            "AGENT_BROKER_REVERSE_TARGET_PORT",
            "AGENT_BROKER_STOP_GRACE",
        ),
    ),
    Family(
        "retired",
        "No longer read: sandbox_host.sh only warns that AGENT_RUNNER_REPLICAS is set and "
        "should be removed from .env.",
        names=("AGENT_RUNNER_REPLICAS",),
    ),
    # -- tooling ----------------------------------------------------------
    Family(
        "tooling",
        "Test seams of the host scripts: the tests point them at a temporary root, "
        "CPU and KVM description and Docker daemon file.",
        names=("KATA_EXTRACT_ROOT", "KATA_CPUINFO", "KATA_KVM_DEVICE", "DOCKER_DAEMON_JSON"),
    ),
    Family(
        "tooling",
        "The local development stack (deploy/local).",
        names=("PGHOST", "PGUSER", "PGPASSWORD", "PGDATABASE"),
        prefixes=("LOCAL_",),
    ),
    Family(
        "tooling",
        "Repository scripts, preflight checks, benchmarks, CI and browser tests; "
        "DEPLOY_PEER_JOB is the deploy workflow's peer job, watched by deploy.sh.",
        names=(
            "TEST_DB_HOST",
            "TEST_DB_PORT",
            "TEST_DB_USER",
            "TEST_DB_PASSWORD",
            "LEGACY_COPY_DATABASE_URL",
            "CAE_BENCH_DNS_SERVER",
            "CLOUD_AGENT_TEST_API_KEY_FILE",
            "CLOUD_AGENT_TEST_PROXY",
            "CLOUD_AGENT_E2E_PORT",
            "CLOUD_AGENT_E2E_SKIP_BUILD",
            "CI",
            "CI_LOG_DIR",
            "DEPLOY_PEER_JOB",
            "TEST_DB_LOCAL_CONTAINER",
            "TEST_DB_LOCAL_IMAGE",
            "TEST_DB_LOCAL_NAME",
            "TEST_DB_LOCAL_PORT",
            "TEST_DB_WORKERS",
            "UV_RUN",
            "GITHUB_API_URL",
            "GITHUB_REPOSITORY",
            "GITHUB_RUN_ATTEMPT",
            "GITHUB_RUN_ID",
            "GITHUB_TOKEN",
        ),
    ),
)

#: Modules that implement the resolvers themselves, so they read by a name
#: chosen at runtime and serve registered names from the environment.
RESOLVER_MODULES: dict[str, str] = {
    "apps/backend/cloud_agent/settings.py": (
        "the settings registry: imports each registered variable into the first settings "
        "row (and missing MIGRATED_SETTINGS later), and serves setting_env and "
        "settings_environment from the adopted snapshot"
    ),
    "apps/backend/cloud_agent_shared/sandbox_manager_compat.py": (
        "manager_env and manager_environment: a canonical name or its former spelling"
    ),
}

#: A registered admin setting read straight from the environment instead of
#: through its resolver, by (module, function, variable), and why.
RAW_SETTING_READS: dict[tuple[str, str, str], str] = {
    (
        "apps/backend/cloud_agent/domain_cipher.py",
        "settings_root_key",
        "AGENT_SESSION_SECRET",
    ): (
        "the settings root key's fallback while AGENT_SETTINGS_ENCRYPTION_KEY is unset; "
        "it decrypts the row the saved session key lives in, so it cannot come from it"
    ),
    (
        "apps/backend/cloud_agent/deployment_setup.py",
        "_root_key_state",
        "AGENT_SESSION_SECRET",
    ): "reports whether the root key in use is that fallback",
    **{
        ("apps/backend/cloud_agent/logging_utils.py", "configure_process_logging", name): (
            "process logging starts before the first snapshot; apply_settings replaces it "
            "with the saved value once one is adopted"
        )
        for name in ("AGENT_LOG_LEVEL", "AGENT_LOG_FORMAT")
    },
    **{
        ("apps/backend/cloud_agent_host/logging_utils.py", "configure_process_logging", name): (
            "a host process's own log settings: the host image has no settings registry, "
            "and its logging is deployment configuration under the same name"
        )
        for name in ("AGENT_LOG_LEVEL", "AGENT_LOG_FORMAT")
    },
    (
        "apps/backend/cloud_agent/compute_config.py",
        "ComputeConfiguration.from_env",
        "AGENT_CLOUDLAB_API_URL",
    ): (
        "the fallback field of a configuration the control plane always runs "
        "with_runtime_settings(), where current_cloudlab_api_url reads the saved value"
    ),
    (
        "apps/backend/cloud_agent/api/admin_database.py",
        "deployment_pgadmin_services",
        "AGENT_PGADMIN_URL",
    ): (
        "the deployment's own list of pgAdmin services a saved URL may reach; reading "
        "the saved value here would let the setting authorize itself"
    ),
    **{
        ("apps/backend/cloud_agent_host/runner.py", function, "AGENT_SANDBOX_MANAGER_TOKEN"): (
            "the host's own copy of the credential it presents to its sandbox manager"
        )
        for function in ("_shared_live_host_capabilities", "_session_supervisor_from_env")
    },
    (
        "apps/backend/cloud_agent_host/sandbox_manager.py",
        "create_app",
        "AGENT_SANDBOX_MANAGER_TOKEN",
    ): "the credential the sandbox manager checks callers against",
}

#: Where a host process reads a HOST_SETTINGS variable from its own
#: environment, by (module, function), and why that is its bootstrap value.
HOST_BOOTSTRAP_READS: dict[tuple[str, str], str] = {
    ("apps/backend/cloud_agent_host/runner.py", "build_parser"): (
        "AGENT_TIMEOUT_S as the --agent-timeout default, the turn limit until a settings "
        "answer replaces it"
    ),
    ("apps/backend/cloud_agent_host/runner.py", "_chat_idle_timeout_s"): (
        "the idle clock's bootstrap value"
    ),
    ("apps/backend/cloud_agent_host/runner.py", "_runner_local_settings"): (
        "the runner's local values, reported as settings_local and used only before the "
        "first settings heartbeat"
    ),
    ("apps/backend/cloud_agent_host/runner.py", "source_import_max_bytes"): (
        "the import limit's local value, feeding _runner_local_settings"
    ),
    ("apps/backend/cloud_agent_host/runner.py", "_new_sandbox_storage_error"): (
        "the storage reserve when the turn carries no settings"
    ),
    ("apps/backend/cloud_agent_host/runner.py", "_sandbox_resource_setting"): (
        "a sandbox resource limit when the turn carries no settings"
    ),
    ("apps/backend/cloud_agent_host/runner.py", "_run_shared_live_attempt"): (
        "a new group's capacity when the turn carries no settings value"
    ),
    ("apps/backend/cloud_agent_host/runner.py", "_shared_live_host_capabilities"): (
        "known debt: the capability check validates the local AGENT_SHARED_RESIDENT_CAPACITY "
        "and does not consult the admin value, so an out-of-range local value withholds "
        "the shared-live capabilities even after an admin value arrives"
    ),
    ("apps/backend/cloud_agent_host/sandbox_manager.py", "_chat_idle_timeout_from_env"): (
        "the sandbox manager's idle clock until the runner pushes settings"
    ),
    ("apps/backend/cloud_agent_host/sandbox_manager.py", "create_app"): (
        "terminal and workspace limits as local bootstrap values; the first authenticated "
        "settings push replaces all of them"
    ),
}

#: Reads whose variable is a registry entry's ``.env`` (a loop over the
#: registry), by (module, function), naming the registry.
REGISTRY_READS: dict[tuple[str, str], Literal["HOST_SETTINGS"]] = {
    ("apps/backend/cloud_agent_host/runner.py", "_new_sandbox_storage_error"): "HOST_SETTINGS",
    ("apps/backend/cloud_agent_host/runner.py", "_sandbox_resource_setting"): "HOST_SETTINGS",
    ("apps/backend/cloud_agent_host/sandbox_manager.py", "create_app"): "HOST_SETTINGS",
}

#: Backend code that hands its whole environment to a child process, by
#: (module, function), and why. The child sees every variable the parent has,
#: so each place is named.
ENVIRONMENT_COPIES: dict[tuple[str, str], str] = {
    ("apps/backend/cloud_agent/providers/compute/ssh.py", "OpenSshTransport.run"): (
        "the ssh client, minus SSH_AUTH_SOCK so no agent is forwarded"
    ),
    ("apps/backend/cloud_agent_host/chat_sandbox.py", "ChatSandboxManager.exec_client_env"): (
        "the Docker CLI's own configuration; sandbox values are added only by name"
    ),
    ("apps/backend/cloud_agent_host/chat_sandbox.py", "ChatSandboxManager.export_archive"): (
        "docker cp, configured by the host's Docker client variables"
    ),
    ("apps/backend/cloud_agent_host/chat_sandbox.py", "ChatSandboxManager._run_bytes"): (
        "a Docker CLI call, configured by the host's Docker client variables"
    ),
    ("apps/backend/cloud_agent_host/docker_engine.py", "run_docker"): (
        "a Docker CLI call, configured by the host's Docker client variables"
    ),
    ("apps/backend/cloud_agent_host/runtime_probe.py", "_context.spawn"): (
        "docker exec for a runtime probe; its values pass by name, never in argv"
    ),
    ("apps/backend/cloud_agent_host/protocol_translator.py", "launch"): (
        "the member's CLI, after the launcher has consumed its own AGENT_COMPAT_* names"
    ),
    ("apps/backend/cloud_agent_shared/git/source_archive.py", "build_base_bundle"): (
        "git, with the repository's fixed Git environment on top"
    ),
}

#: Admin settings with an environment name that are not in MIGRATED_SETTINGS.
#: An existing settings row imports only the MIGRATED_SETTINGS it lacks
#: (``initialize_settings``), so a setting outside that list reads its
#: environment only when a database's row is first created. Most of these were
#: in the row's first release (#392). The four web search names,
#: AGENT_MCP_GITHUB_CLIENT_ID/SECRET, AGENT_WEB_DOCS_URL and
#: AGENT_WEB_CONTACT_URL were registered afterwards, so a database created
#: before them never imported their environment value. Frozen: a new
#: registration with an environment name goes into MIGRATED_SETTINGS.
UNMIGRATED_ENV_SETTINGS: frozenset[str] = frozenset(
    {
        "AGENT_ALERT_WEBHOOK_URL",
        "AGENT_CLOUDLAB_API_URL",
        "AGENT_COMPUTE_REGISTRATION_URL",
        "AGENT_GITHUB_API",
        "AGENT_GITHUB_APP_CLIENT_ID",
        "AGENT_GITHUB_APP_CLIENT_SECRET",
        "AGENT_GITHUB_APP_ID",
        "AGENT_GITHUB_APP_INSTALL_URL",
        "AGENT_GITHUB_APP_PRIVATE_KEY",
        "AGENT_GITHUB_TOKEN",
        "AGENT_GITLAB_OAUTH_CLIENT_ID",
        "AGENT_GITLAB_OAUTH_CLIENT_SECRET",
        "AGENT_GITLAB_OAUTH_REDIRECT_URI",
        "AGENT_GIT_AUTHOR_EMAIL",
        "AGENT_GIT_AUTHOR_NAME",
        "AGENT_LOG_FORMAT",
        "AGENT_LOG_LEVEL",
        "AGENT_MCP_GITHUB_CLIENT_ID",
        "AGENT_MCP_GITHUB_CLIENT_SECRET",
        "AGENT_MCP_PUBLIC_BASE_URL",
        "AGENT_ONCALL_DISPATCH_TOKEN",
        "AGENT_ONCALL_USER_ID",
        "AGENT_PUBLISH_BASE_BRANCH",
        "AGENT_REPO_ALLOWLIST",
        "AGENT_RESIDENCY_TTL_S",
        "AGENT_SESSION_REDIRECT_URI",
        "AGENT_WEB_APP_NAME",
        "AGENT_WEB_BASE_URL",
        "AGENT_WEB_CONTACT_URL",
        "AGENT_WEB_DOCS_URL",
        "AGENT_WEB_FEATURE_RAG",
        "AGENT_WEB_GATEWAY_URL",
        "AGENT_WEB_ORG_NAME",
        "AGENT_WEB_ORG_URL",
        "AGENT_WEB_STATUS_URL",
        "BRAVE_SEARCH_API_KEY",
        "GATEWAY_AUTHORIZE_URL",
        "GATEWAY_BASE_URL",
        "GATEWAY_GRANT_BASE_PATH",
        "GATEWAY_GRANT_BINDING_ID",
        "GATEWAY_GRANT_DISPATCH_TOKEN",
        "PARALLEL_API_KEY",
        "SERPER_API_KEY",
        "WEB_SEARCH_PROVIDER",
    }
)


#: Shell functions that read the configuration value their first argument
#: names in a way the shell scan's patterns cannot see, by function name (the
#: deploy scripts source the file that defines them), and why.
SHELL_HELPERS: dict[str, str] = {
    "artifact_env_value": (
        "deploy/release/artifact_env.sh: looks the name up in the Compose environment "
        "rendered for an artifact deployment (.env plus the process environment)"
    ),
}

#: Shell reads the scan reports as inputs that are not, by (script, variable):
#: a sourced library reading what the script sourcing it set.
SHELL_NOT_INPUTS: dict[tuple[str, str], str] = {
    ("deploy/release/artifact_env.sh", "SHA"): (
        "both deploy.sh scripts set SHA (their argument or DEPLOY_SHA) before sourcing this file"
    ),
}

#: Shell reads whose variable the script computes, by (script, function,
#: construct), and why. None today: every indirect read resolves to a loop
#: over literal names or sits inside a helper.
SHELL_DYNAMIC_READS: dict[tuple[str, str, str], str] = {}
