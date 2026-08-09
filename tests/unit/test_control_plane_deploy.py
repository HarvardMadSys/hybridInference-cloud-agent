"""The control plane's own deployment files (task E11's dependency).

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

import re
from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
COMPOSE_PATH = REPO_ROOT / "deploy" / "control-plane" / "docker-compose.yml"
DOCKERFILE_PATH = REPO_ROOT / "deploy" / "control-plane" / "Dockerfile"

#: Signing and encryption keys. Each must be `:?` — required — rather than
#: `:-` — defaulted.
REQUIRED_SECRETS = (
    "AGENT_SESSION_SECRET",
    "AGENT_CONTROL_TOKEN_SECRET",
    "AGENT_SOURCE_CONTROL_ENCRYPTION_KEY",
    "GATEWAY_GRANT_DISPATCH_TOKEN",
)


@pytest.fixture(scope="module")
def compose() -> dict:
    return yaml.safe_load(COMPOSE_PATH.read_text())


@pytest.fixture(scope="module")
def control_plane_env(compose: dict) -> dict[str, str]:
    return compose["services"]["control-plane"]["environment"]


@pytest.mark.parametrize("variable", REQUIRED_SECRETS)
def test_a_secret_is_required_rather_than_defaulted(control_plane_env: dict, variable: str) -> None:
    """`:-` here would be a deployment signing with a public value and no sign
    that anything is wrong."""
    assert variable in control_plane_env, f"{variable} is not configured at all"
    assert ":?" in control_plane_env[variable], (
        f"{variable} has a default; a secret that can be omitted will be omitted"
    )


def test_the_database_is_required_and_the_service_owns_it(control_plane_env: dict) -> None:
    """Defaulting the DSN points a fresh deployment at whatever is on
    localhost, which in a co-located cutover is the gateway's own database."""
    assert ":?" in control_plane_env["AGENT_DATABASE_URL"]


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
    assert re.search(r"^COPY host/ host/", dockerfile, re.MULTILINE), (
        "the control-plane image drops host/, so publishing will fail at runtime"
    )


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
        for path in (REPO_ROOT / "backend").rglob("*.py")
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


def test_the_runner_credential_is_required(control_plane_env: dict) -> None:
    """Without it every claim is 401 and the queue never drains — with nothing
    in this service's logs saying why, because the refusal is correct."""
    assert ":?" in control_plane_env["AGENT_DISPATCHER_TOKEN"]


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
