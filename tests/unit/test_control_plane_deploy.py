"""The control plane's own deployment files.

`tests/unit/test_deploy_config.py` does this for the *host* stack, which is the
one that executes untrusted code. This file covers the other machine, where the
properties worth pinning are different:

- **Secrets are required, never defaulted.** A default is a deployment that
  looks configured while signing sessions with a value published in a git
  repository.
- **The schema is Alembic's.** Nothing creates a table at startup, so an API
  container that can start before migrations have run is a crash loop against
  tables that do not exist.
- **The image ships the host package.** `patch_gate` crosses the host/control
  boundary; without it this image builds, starts, serves, and fails at the
  first publish.
- **Every variable it sets is one the code reads.** A compose that configures
  something nothing consumes is documentation that lies — and two such
  variables were found and removed while writing it.

The rule that no gateway signing secret may appear in a deployment file is
checked in ``test_scaffold.py``, beside the Python-source version of the same
rule, so the list of forbidden names has exactly one definition.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
COMPOSE_PATH = REPO_ROOT / "deploy" / "control-plane" / "docker-compose.yml"
DOCKERFILE_PATH = REPO_ROOT / "deploy" / "control-plane" / "Dockerfile"
DEPLOY_SCRIPT_PATH = REPO_ROOT / "deploy" / "control-plane" / "deploy.sh"

#: Signing and encryption keys. Each must be `:?` — required — rather than
#: `:-` — defaulted.
REQUIRED_SECRETS = (
    "AGENT_SESSION_SECRET",
    "AGENT_CONTROL_TOKEN_SECRET",
    "AGENT_SOURCE_CONTROL_ENCRYPTION_KEY",
)


@pytest.fixture(scope="module")
def compose() -> dict:
    return yaml.safe_load(COMPOSE_PATH.read_text())


@pytest.fixture(scope="module")
def control_plane_env(compose: dict) -> dict[str, str]:
    return compose["services"]["control-plane"]["environment"]


@pytest.mark.parametrize("variable", REQUIRED_SECRETS)
def test_missing_migrated_secrets_allow_setup_without_using_a_public_default(
    control_plane_env: dict, variable: str, monkeypatch
) -> None:
    """Setup can start with empty imports; credential use still fails closed."""
    from cloud_agent import identity, settings, tokens
    from cloud_agent.source_control import SourceControlCipher, SourceControlError

    assert control_plane_env[variable] == "${" + variable + ":-}"
    monkeypatch.delenv(variable, raising=False)
    settings.reset_settings()
    operations = {
        "AGENT_SESSION_SECRET": (identity._session_secret, identity.IdentityNotConfigured),
        "AGENT_CONTROL_TOKEN_SECRET": (tokens._signing_key, ValueError),
        "AGENT_SOURCE_CONTROL_ENCRYPTION_KEY": (SourceControlCipher, SourceControlError),
    }
    operation, error = operations[variable]
    with pytest.raises(error):
        operation()


async def test_saved_gateway_dispatch_credential_needs_no_environment_default(
    control_plane_env: dict, monkeypatch
) -> None:
    """An empty import disables grants; a saved credential configures the live client."""
    from cloud_agent import settings
    from cloud_agent.gateway import GatewayError
    from cloud_agent.grants import GrantClient

    assert control_plane_env["GATEWAY_GRANT_DISPATCH_TOKEN"] == (
        "${GATEWAY_GRANT_DISPATCH_TOKEN:-}"
    )
    monkeypatch.delenv("GATEWAY_GRANT_DISPATCH_TOKEN", raising=False)
    monkeypatch.setenv("AGENT_SETTINGS_ENCRYPTION_KEY", "k" * 32)
    settings.reset_settings()
    try:
        settings.apply_settings(1, {"gateway_base_url": "https://gateway.example"})
        client = GrantClient()
        assert not client.configured
        with pytest.raises(GatewayError):
            await client._post("/internal/agent-grants", {})
        encoded = settings.encode_changes({"gateway_dispatch_token": "saved-credential"})
        settings.apply_settings(2, {"gateway_base_url": "https://gateway.example", **encoded})
        assert client.configured
        assert "GATEWAY_GRANT_DISPATCH_TOKEN" not in os.environ
        assert "saved-credential" not in str(settings.settings_response(2, encoded))
    finally:
        settings.reset_settings()


def test_the_database_is_required_and_the_service_owns_it(control_plane_env: dict) -> None:
    """Defaulting the DSN points a fresh deployment at whatever is on
    localhost, which in a co-located cutover is the gateway's own database."""
    assert ":?" in control_plane_env["AGENT_DATABASE_URL"]


def test_both_listeners_allow_a_one_time_identity_import_from_the_environment(
    compose: dict,
) -> None:
    """The shared lifespan verifies the saved issuer against the database binding."""
    expected = "${GATEWAY_ISSUER:-}"
    for service in ("control-plane", "mcp-relay"):
        assert compose["services"][service]["environment"]["GATEWAY_ISSUER"] == expected


def test_postgres_publishes_no_port(compose: dict) -> None:
    """The API reaches it over the compose network. A published port is a
    database on the internet one firewall rule later."""
    assert not compose["services"]["postgres"].get("ports")


def test_the_api_cannot_start_before_migrations_have_run(compose: dict) -> None:
    """Alembic owns the schema — `initialize()` is not called at startup — so
    "start and retry" is not a recovery here, it is a crash loop."""
    depends = compose["services"]["control-plane"]["depends_on"]
    assert depends["migrate"]["condition"] == "service_completed_successfully"


def test_migrations_wait_for_a_database_that_is_actually_ready(compose: dict) -> None:
    """`service_started` would let Alembic race a Postgres that has accepted a
    TCP connection and is still initialising."""
    assert (
        compose["services"]["migrate"]["depends_on"]["postgres"]["condition"] == "service_healthy"
    )
    assert compose["services"]["postgres"].get("healthcheck"), "nothing defines ready"


def test_the_bind_address_defaults_to_loopback(compose: dict) -> None:
    """This service terminates no TLS and authenticates with a cookie. A
    default of 0.0.0.0 serves that cookie over plain HTTP to anyone routable."""
    published = compose["services"]["control-plane"]["ports"][0]
    assert published.startswith("${AGENT_CONTROL_PLANE_BIND:-127.0.0.1}")


def test_the_cookie_and_proxy_switches_default_to_the_safe_answer(
    control_plane_env: dict,
) -> None:
    """An operator who sets neither gets a Secure cookie and an audit trail
    that records the peer rather than a header the caller chose."""
    assert (
        control_plane_env["AGENT_SESSION_COOKIE_SECURE"] == "${AGENT_SESSION_COOKIE_SECURE:-true}"
    )
    assert control_plane_env["AGENT_TRUST_PROXY_HEADERS"] == "${AGENT_TRUST_PROXY_HEADERS:-false}"


def test_the_image_ships_the_host_package() -> None:
    """`patch_gate` is imported by the publisher and the publish worker here
    and by the runner and the snapshotter on a host. The manifest called it
    host-only, which was wrong; an image without it builds, starts, serves, and
    fails at the first publish."""
    dockerfile = DOCKERFILE_PATH.read_text()
    assert re.search(
        r"^COPY apps/backend/cloud_agent_host/ apps/backend/cloud_agent_host/",
        dockerfile,
        re.MULTILINE,
    ), "the control-plane image drops cloud_agent_host/, so publishing will fail at runtime"


def test_the_image_ships_the_migrations_it_is_told_to_run() -> None:
    """The `migrate` service runs `alembic upgrade head` in *this* image."""
    dockerfile = DOCKERFILE_PATH.read_text()
    assert "COPY alembic.ini" in dockerfile
    assert re.search(r"^COPY migrations/ migrations/", dockerfile, re.MULTILINE)


def test_the_container_does_not_run_as_root() -> None:
    """It holds a git credential and a database connection."""
    assert re.search(r"^USER (?!root)", DOCKERFILE_PATH.read_text(), re.MULTILINE)


def test_the_compose_only_sets_variables_the_code_reads(control_plane_env: dict) -> None:
    """A configured variable nothing consumes is documentation that lies.

    `AGENT_LEASE_TTL` and `AGENT_TIMEOUT_S` were in the first draft, from
    ENVIRONMENT.md's "Scheduling" table under the control-plane heading; both
    are read only by the host runner. An operator tuning a lease here would
    have changed nothing and had no way to find that out.
    """
    sources = "\n".join(
        path.read_text()
        for root, pattern in (
            (REPO_ROOT / "apps" / "backend" / "cloud_agent", "*.py"),
            (REPO_ROOT / "apps" / "backend" / "cloud_agent_shared", "operation_profiling.py"),
        )
        for path in root.rglob(pattern)
        if "__pycache__" not in str(path)
    )
    unread = [name for name in control_plane_env if f'"{name}"' not in sources]
    assert not unread, f"the control-plane service sets variables nothing reads: {unread}"


def test_a_default_the_code_relies_on_is_not_blanked_out(control_plane_env: dict) -> None:
    """**`:-` is not "leave it unset".**

    Compose passes an empty string as a *set* variable, and
    `os.getenv(name, "dev")` returns that empty string rather than the default.
    So `${AGENT_PUBLISH_BASE_BRANCH:-}` silently means "open the PR against a
    branch called `''`", and `${AGENT_GITHUB_API:-}` means "talk to an empty
    API base" — both from a compose file that looks like it configured nothing.

    Variables whose *code* carries a default must repeat it here or be omitted
    entirely. Listed by name because the rule only applies to the ones with a
    default behind them; a genuinely optional variable is right to be blank.
    """
    for name, default in (
        ("AGENT_PUBLISH_BASE_BRANCH", "dev"),
        ("AGENT_GITHUB_API", "https://api.github.com"),
    ):
        assert control_plane_env[name].endswith(f":-{default}}}"), (
            f"{name} blanks out the default the code relies on"
        )


def test_control_plane_has_no_shared_claim_credential(control_plane_env: dict) -> None:
    """Claims authenticate with enrollment-issued host credentials."""
    assert "AGENT_DISPATCHER_TOKEN" not in control_plane_env


def _run_control_deploy(
    tmp_path: Path,
    *,
    readiness_exit: int,
    identity_exit: int = 0,
    control_running: bool = True,
    mcp_running: bool = True,
    shared_live: str = "1",
    deployment_environment: str = "production",
    dotenv: str = "",
    failed_health_match: str = "",
    deploy_protocol: bool | str = True,
) -> tuple[subprocess.CompletedProcess[str], str]:
    """Run the real deploy script against a minimal local git source."""
    source = tmp_path / "source"
    script_target = source / "deploy/control-plane/deploy.sh"
    script_target.parent.mkdir(parents=True)
    shutil.copy2(DEPLOY_SCRIPT_PATH, script_target)
    if isinstance(deploy_protocol, str):
        (source / "deploy/DEPLOY_PROTOCOL").write_text(deploy_protocol + "\n")
    elif deploy_protocol:
        shutil.copy2(REPO_ROOT / "deploy/DEPLOY_PROTOCOL", source / "deploy/DEPLOY_PROTOCOL")
    git_env = {
        **os.environ,
        "GIT_AUTHOR_NAME": "Deploy Test",
        "GIT_AUTHOR_EMAIL": "deploy@example.invalid",
        "GIT_COMMITTER_NAME": "Deploy Test",
        "GIT_COMMITTER_EMAIL": "deploy@example.invalid",
    }
    subprocess.run(["git", "init", "-q", "-b", "dev"], cwd=source, env=git_env, check=True)
    subprocess.run(["git", "add", "."], cwd=source, env=git_env, check=True)
    subprocess.run(
        ["git", "commit", "-q", "-m", "deployment fixture"],
        cwd=source,
        env=git_env,
        check=True,
    )
    sha = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=source, text=True).strip()

    deploy_root = tmp_path / "deployed"
    subprocess.run(["git", "clone", "-q", str(source), str(deploy_root)], check=True)
    (deploy_root / ".env").write_text(dotenv)

    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    docker_log = tmp_path / "docker.log"
    docker = fake_bin / "docker"
    docker.write_text(
        "#!/bin/sh\n"
        'printf "%s\\n" "$*" >> "$DOCKER_LOG"\n'
        'case "$*" in\n'
        "  *' ps --status running -q control-plane')\n"
        '    [ "${CONTROL_RUNNING}" = "1" ] && printf "%s\\n" existing-control ; exit 0 ;;\n'
        "  *' ps --status running -q mcp-relay')\n"
        '    [ "${MCP_RUNNING}" = "1" ] && printf "%s\\n" existing-mcp ; exit 0 ;;\n'
        "  *' run --rm --no-deps control-plane python -m cloud_agent.identity_namespace --check')\n"
        '    exit "${IDENTITY_EXIT}" ;;\n'
        "  *' run --rm --no-deps control-plane python -m cloud_agent.deploy_readiness '*)\n"
        '    exit "${READINESS_EXIT}" ;;\n'
        "esac\n"
        "exit 0\n"
    )
    docker.chmod(0o755)
    curl = fake_bin / "curl"
    curl.write_text(
        "#!/bin/sh\n"
        'if [ -n "${FAILED_HEALTH_MATCH:-}" ]; then\n'
        '  for arg in "$@"; do\n'
        '    case "$arg" in\n'
        '      *"${FAILED_HEALTH_MATCH}"*) exit 1 ;;\n'
        "    esac\n"
        "  done\n"
        "fi\n"
        "exit 0\n"
    )
    curl.chmod(0o755)

    # Failed health probes exercise the real deadline loop without spending
    # three wall-clock seconds on each retry. Bash's SECONDS is the clock the
    # script reads, so advancing it preserves the timeout condition.
    bash_env = tmp_path / "bash-env"
    bash_env.write_text("sleep() { SECONDS=$((SECONDS + $1)); }\n")

    result = subprocess.run(
        ["bash", str(DEPLOY_SCRIPT_PATH), sha],
        env={
            **os.environ,
            "BASH_ENV": str(bash_env),
            "PATH": f"{fake_bin}:{os.environ['PATH']}",
            "SOURCE_REPO": str(source),
            "DEPLOY_ROOT": str(deploy_root),
            "DEPLOYMENT_ENVIRONMENT": deployment_environment,
            "AGENT_SHARED_LIVE_SANDBOX": shared_live,
            "SHARED_READY_TIMEOUT": "1",
            "HEALTH_TIMEOUT": "1",
            "DOCKER_LOG": str(docker_log),
            "READINESS_EXIT": str(readiness_exit),
            "IDENTITY_EXIT": str(identity_exit),
            "CONTROL_RUNNING": "1" if control_running else "0",
            "MCP_RUNNING": "1" if mcp_running else "0",
            "FAILED_HEALTH_MATCH": failed_health_match,
        },
        capture_output=True,
        text=True,
        check=False,
    )
    return result, docker_log.read_text() if docker_log.exists() else ""


@pytest.mark.parametrize("running", [True, False])
def test_a_failed_identity_check_does_not_start_or_replace_listeners(tmp_path: Path, running: bool):
    result, docker_calls = _run_control_deploy(
        tmp_path,
        readiness_exit=0,
        identity_exit=18,
        control_running=running,
        mcp_running=running,
    )
    assert result.returncode == 18
    assert docker_calls.index(" run --rm --no-deps migrate") < docker_calls.index(
        "cloud_agent.identity_namespace --check"
    )
    assert "cloud_agent.deploy_readiness" not in docker_calls
    assert " up -d --no-deps " not in docker_calls
    assert " up -d --remove-orphans" not in docker_calls
    assert " stop " not in docker_calls


def test_a_failed_readiness_probe_never_replaces_the_running_stack(
    tmp_path: Path,
) -> None:
    result, docker_calls = _run_control_deploy(tmp_path, readiness_exit=17)

    assert result.returncode == 17
    assert (
        docker_calls.index(" build")
        < docker_calls.index(" up -d --wait --wait-timeout 1 postgres")
        < docker_calls.index(" run --rm --no-deps migrate")
        < docker_calls.index("cloud_agent.identity_namespace --check")
        < docker_calls.index("cloud_agent.deploy_readiness")
    )
    assert " up -d --remove-orphans" not in docker_calls


def test_online_rollback_to_a_pre_fence_commit_is_refused_before_docker(tmp_path: Path) -> None:
    result, docker_calls = _run_control_deploy(
        tmp_path,
        readiness_exit=0,
        deploy_protocol=False,
    )

    assert result.returncode != 0
    assert "pre-fence or protocol-incompatible" in result.stderr
    assert docker_calls == ""


def test_online_rollback_to_an_old_protocol_is_refused_before_docker(tmp_path: Path) -> None:
    result, docker_calls = _run_control_deploy(
        tmp_path,
        readiness_exit=0,
        deploy_protocol="shared-live-exact-sha-v1",
    )

    assert result.returncode != 0
    assert "does not implement shared-live-exact-sha-v4" in result.stderr
    assert docker_calls == ""


def test_a_successful_readiness_probe_allows_the_shared_only_service(tmp_path: Path) -> None:
    result, docker_calls = _run_control_deploy(tmp_path, readiness_exit=0)

    assert result.returncode == 0, result.stderr
    assert "cloud_agent.deploy_readiness" in docker_calls
    assert re.search(r"--expected-deployment-sha [0-9a-f]{40}(?:\s|$)", docker_calls)
    assert " up -d --remove-orphans" in docker_calls


def test_a_cold_start_bootstraps_both_services_needed_for_host_readiness(
    tmp_path: Path,
) -> None:
    result, docker_calls = _run_control_deploy(
        tmp_path,
        readiness_exit=0,
        control_running=False,
        mcp_running=False,
    )

    assert result.returncode == 0, result.stderr
    bootstrap = docker_calls.index(" up -d --no-deps control-plane mcp-relay")
    readiness = docker_calls.index("cloud_agent.deploy_readiness")
    full_up = docker_calls.index(" up -d --remove-orphans")
    assert bootstrap < readiness < full_up
    assert "parallel host readiness" in result.stdout
    assert "bootstrap MCP relay" in result.stdout


def test_a_failed_cold_start_gate_stops_the_bootstrap_api(tmp_path: Path) -> None:
    result, docker_calls = _run_control_deploy(
        tmp_path,
        readiness_exit=17,
        control_running=False,
        mcp_running=False,
    )

    assert result.returncode == 17
    assert " up -d --no-deps control-plane mcp-relay" in docker_calls
    assert " stop control-plane mcp-relay" in docker_calls
    assert " up -d --remove-orphans" not in docker_calls


def test_post_gate_health_failure_does_not_stop_bootstrap_services(tmp_path: Path) -> None:
    result, docker_calls = _run_control_deploy(
        tmp_path,
        readiness_exit=0,
        control_running=False,
        mcp_running=False,
        failed_health_match="127.0.0.1:3002",
    )

    assert result.returncode != 0
    assert "cloud_agent.deploy_readiness" in docker_calls
    assert " up -d --remove-orphans" in docker_calls
    assert " stop control-plane" not in docker_calls
    assert " stop mcp-relay" not in docker_calls
    assert not (tmp_path / "deployed" / ".deployed-sha").exists()


def test_replaced_mcp_relay_must_pass_its_post_gate_health_check(tmp_path: Path) -> None:
    result, docker_calls = _run_control_deploy(
        tmp_path,
        readiness_exit=0,
        control_running=True,
        mcp_running=True,
        failed_health_match="127.0.0.1:8001",
    )

    assert result.returncode != 0
    assert " up -d --remove-orphans" in docker_calls
    assert "MCP relay health check timed out" in result.stderr
    assert not (tmp_path / "deployed" / ".deployed-sha").exists()


def test_a_missing_mcp_relay_is_bootstrapped_without_replacing_the_live_api(
    tmp_path: Path,
) -> None:
    result, docker_calls = _run_control_deploy(
        tmp_path,
        readiness_exit=0,
        control_running=True,
        mcp_running=False,
    )

    assert result.returncode == 0, result.stderr
    assert " up -d --no-deps mcp-relay" in docker_calls
    assert " up -d --no-deps control-plane" not in docker_calls
    assert docker_calls.index(" up -d --no-deps mcp-relay") < docker_calls.index(
        "cloud_agent.deploy_readiness"
    )


def test_a_failed_partial_bootstrap_only_restores_the_service_that_was_stopped(
    tmp_path: Path,
) -> None:
    result, docker_calls = _run_control_deploy(
        tmp_path,
        readiness_exit=17,
        control_running=True,
        mcp_running=False,
    )

    assert result.returncode == 17
    assert " stop mcp-relay" in docker_calls
    assert " stop control-plane" not in docker_calls


def test_a_missing_api_is_bootstrapped_without_restarting_a_live_mcp_relay(
    tmp_path: Path,
) -> None:
    result, docker_calls = _run_control_deploy(
        tmp_path,
        readiness_exit=0,
        control_running=False,
        mcp_running=True,
    )

    assert result.returncode == 0, result.stderr
    assert " up -d --no-deps control-plane" in docker_calls
    assert " up -d --no-deps control-plane mcp-relay" not in docker_calls
    assert "bootstrap MCP relay" not in result.stdout


def test_removed_default_off_switch_is_inert(
    tmp_path: Path,
) -> None:
    result, docker_calls = _run_control_deploy(
        tmp_path,
        readiness_exit=0,
        shared_live="0",
    )

    assert result.returncode == 0, result.stderr
    assert "cloud_agent.deploy_readiness" in docker_calls
    assert " up -d --remove-orphans" in docker_calls


def test_manual_staging_deploy_reads_its_persistent_environment(tmp_path: Path) -> None:
    result, docker_calls = _run_control_deploy(
        tmp_path,
        readiness_exit=0,
        shared_live="",
        deployment_environment="",
        dotenv="DEPLOYMENT_ENVIRONMENT=staging\n",
    )

    assert result.returncode == 0, result.stderr
    assert "shared-live scheduling gate" not in result.stdout
    assert "cloud_agent.deploy_readiness" in docker_calls
    assert " up -d --remove-orphans" in docker_calls


def test_every_configured_file_path_is_actually_mounted(compose: dict) -> None:
    """A path names a location *inside* the container.

    Setting `AGENT_GITHUB_APP_PRIVATE_KEY_PATH` without mounting anything
    produces a service that starts, offers Integrations, and fails at the first
    publish. It was exactly that until review caught it. `AGENT_MCP_CONFIG` had
    the same shape and is no longer here at all — see
    `test_no_mcp_registry_is_configurable` below.
    """
    service = compose["services"]["control-plane"]
    env = service["environment"]
    # Split from the right: the host side is still `${VAR:-/dev/null}` in the
    # unrendered file and carries a colon of its own, so counting from the left
    # lands inside the interpolation.
    mounted = set()
    for mount in service.get("volumes") or []:
        if isinstance(mount, dict):
            mounted.add(mount.get("target"))
            continue
        parts = str(mount).rsplit(":", 2)
        mounted.add(parts[1] if len(parts) == 3 else parts[-1])
    for name in ("AGENT_GITHUB_APP_PRIVATE_KEY_PATH",):
        default = env[name].split(":-", 1)[1].rstrip("}")
        assert default in mounted, f"{name} defaults to {default}, which nothing mounts"


def test_no_mcp_registry_is_configurable(compose: dict) -> None:
    """The stack offers no way to supply a deployment-wide MCP server list.

    Checked at the compose layer as well as in the loader, because this is
    where an operator would look for the knob: a variable left behind here
    would read as "configurable, just undocumented", and the mount is what
    made the old registry reachable at all.
    """
    for name, service in compose["services"].items():
        env = service.get("environment") or {}
        offenders = sorted(key for key in env if "MCP_CONFIG" in key)
        assert not offenders, f"{name} still takes {offenders}"
        for mount in service.get("volumes") or []:
            target = mount.get("target") if isinstance(mount, dict) else str(mount)
            assert "mcp" not in str(target).lower(), f"{name} still mounts {target}"
