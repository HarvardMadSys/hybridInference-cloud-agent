"""Security properties of the standalone deployment.

The source repo's whole `test_agent_deploy_config.py` is ported as
`test_deploy_config.py`, re-pointed at the standalone compose. This file holds
only what that port could not carry: assertions the standalone topology needs
that the overlay never had, and the record of what did not port.

The Kata preflight lives in `test_setup_kata_runtime.py`, which drives the
real script.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest
import yaml

REPO = Path(__file__).resolve().parents[2]
COMPOSE_PATH = REPO / "deploy/host/docker-compose.sandbox.yml"
SANDBOX_DOCKERFILE = REPO / "deploy/sandbox/Dockerfile.agent-sandbox"
CODEX_REQUIREMENTS = REPO / "deploy/sandbox/codex-requirements.toml"
WORKFLOW_DIR = REPO / ".github/workflows"


@pytest.fixture(scope="module")
def compose() -> dict:
    return yaml.safe_load(COMPOSE_PATH.read_text(encoding="utf-8"))


def _mounts(service: dict) -> list[str]:
    """Volume entries as strings, whatever syntax the compose file uses."""
    out = []
    for volume in service.get("volumes") or []:
        if isinstance(volume, str):
            out.append(volume)
        else:  # long syntax
            ro = ":ro" if volume.get("read_only") else ""
            out.append(f"{volume.get('source', '')}:{volume.get('target', '')}{ro}")
    return out


def test_trusted_ci_events_keep_the_existing_self_hosted_pools() -> None:
    """Every job keeps the pool whose cache and hardware it was measured on."""
    workflow = yaml.safe_load((WORKFLOW_DIR / "ci.yml").read_text())
    assert workflow["permissions"] == {"contents": "read"}
    jobs = workflow["jobs"]
    special_runners = {
        "artifacts": "fromJSON(matrix.artifact.runner)",
        "compose": ["self-hosted", "Linux", "ARM64", "image-verify-arm64"],
        "kata": ["self-hosted", "Linux", "X64", "image-verify-x64"],
    }
    assert special_runners.keys() <= jobs.keys()
    for name, job in jobs.items():
        expected = special_runners.get(name, ["self-hosted", "Linux", "ci-general"])
        if isinstance(expected, list):
            assert job["runs-on"] == expected, f"{name} left its pool"
        else:
            assert expected in job["runs-on"], f"{name} left its pool"


def test_kata_validation_cannot_run_pull_request_code_on_the_deployment_host() -> None:
    workflow = yaml.safe_load((WORKFLOW_DIR / "ci.yml").read_text())
    job = workflow["jobs"]["kata"]
    assert "github.event_name != 'pull_request'" in job["if"]
    assert "github.ref == 'refs/heads/dev'" in job["if"]
    assert "kata" in workflow["jobs"]["ci-gate"]["needs"]
    assert job["concurrency"]["group"] != "cloud-agent-host-deploy"
    commands = "\n".join(step.get("run", "") for step in job["steps"])
    assert "cloud_agent_host.candidate_check" in commands
    assert "compose up" not in commands
    assert "deploy.sh" not in commands


def test_ci_images_build_on_their_deployment_architecture() -> None:
    """Matrix indirection must preserve both self-hosting and native builds.

    One cell per artifact, and the console appears twice because a mount prefix
    is resolved while its bundle is compiled — both variants are images, and
    until this matrix covered them neither was built in CI at all.
    """
    workflow = yaml.safe_load((WORKFLOW_DIR / "ci.yml").read_text())
    matrix = workflow["jobs"]["artifacts"]["strategy"]["matrix"]
    assert set(matrix) == {"artifact"}
    cells = matrix["artifact"]
    x64 = ["self-hosted", "Linux", "X64", "image-verify-x64"]
    arm64 = ["self-hosted", "Linux", "ARM64", "image-verify-arm64"]
    expected = {
        "control": ("deploy/control-plane/Dockerfile", arm64),
        "web-root": ("deploy/control-plane/Dockerfile.web", arm64),
        "web-agents": ("deploy/control-plane/Dockerfile.web", arm64),
        "runner": ("deploy/host/Dockerfile.agent-runner", x64),
        "sandbox": ("deploy/sandbox/Dockerfile.agent-sandbox", x64),
        "tunnel": ("deploy/host/Dockerfile.agent-gateway-tunnel", x64),
        "egress": ("deploy/host/Dockerfile.agent-egress-proxy", x64),
    }
    assert {cell["name"] for cell in cells} == set(expected)
    for cell in cells:
        dockerfile, pool = expected[cell["name"]]
        assert cell["dockerfile"] == dockerfile, cell["name"]
        assert json.loads(cell["runner"]) == pool, cell["name"]


# ---------------------------------------------------------------------------
# The network boundary
# ---------------------------------------------------------------------------

_VAR = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)(?::([-?])([^{}]*))?\}")


def _defaults(value: str) -> str:
    """Expand `${VAR:-default}` taking every default — the unconfigured host.

    Innermost-first and repeated, because these nest: a URL's default is built
    from two more defaulted variables. A required `${VAR:?...}` has no value to
    take, so it becomes a marker no assertion here will mistake for an address.
    """
    seen = set()
    while (
        expanded := _VAR.sub(lambda m: m.group(3) or f"<required:{m.group(1)}>", value)
    ) != value:
        assert value not in seen, f"cycle expanding {value}"
        seen.add(value)
        value = expanded
    return value


def _tunnels(compose: dict) -> dict[str, dict]:
    """Every tunnel by the `host:port` a consumer would dial it at.

    Keyed by address rather than by service name because that is the join a
    consumer actually makes: it is handed a URL, and what answers is whichever
    tunnel claimed that alias and that listening port.
    """
    out = {}
    for name, svc in compose["services"].items():
        env = svc.get("environment") or {}
        if "TUNNEL_REMOTE_PORT" not in env:
            continue
        listen = _defaults(str(env["TUNNEL_LISTEN_PORT"]))
        networks = svc.get("networks") or {}
        for network, cfg in networks.items():
            for alias in (cfg or {}).get("aliases") or []:
                out[f"{_defaults(alias)}:{listen}"] = {
                    "service": name,
                    "remote_port": _defaults(str(env["TUNNEL_REMOTE_PORT"])),
                    "network": network,
                }
    return out


@pytest.mark.parametrize(
    ("variable", "remote_port", "why"),
    [
        ("AGENT_CONTROL_PLANE_URL", "8000", "claim, register and heartbeat"),
        ("AGENT_SANDBOX_MCP_URL", "8001", "the MCP relay, and nothing else"),
        ("AGENT_GATEWAY_URL", "8080", "model calls"),
    ],
)
def test_each_address_reaches_the_listener_it_names(
    compose: dict, variable: str, remote_port: str, why: str
) -> None:
    """An unconfigured host must dial the right far end, not merely a live one.

    This is the assertion the MCP split needed and did not have. Moving the
    relay to the control plane's 8001 listener left `AGENT_CONTROL_PLANE_URL`
    defaulted at the MCP tunnel, so every claim, registration and heartbeat
    arrived at a listener that serves only MCP. Nothing failed to start: jobs
    were simply never taken, and the 404 named a path rather than a port.

    Resolving the *defaults* is the point — a deployment that sets every
    variable by hand would have papered over it.
    """
    env = compose["services"]["runner"]["environment"]
    address = _defaults(env[variable]).removeprefix("http://").removeprefix("https://")
    tunnel = _tunnels(compose).get(address)
    assert tunnel is not None, f"{variable} defaults to {address}, which no tunnel answers"
    assert tunnel["remote_port"] == remote_port, (
        f"{variable} reaches {tunnel['service']} → port {tunnel['remote_port']}, "
        f"but it is for {why} on {remote_port}"
    )


def test_nothing_on_the_closed_network_reaches_the_control_plane(compose: dict) -> None:
    """What splitting the MCP listener onto 8001 actually bought.

    The sandbox network exists so untrusted code reaches the gateway and the
    MCP relay and nothing else. A second tunnel aliased onto it whose far end
    is the control plane's 8000 would hand back everything the split removed —
    jobs are created and cancelled there — without changing a line of the code
    that reasons about it.
    """
    exposed = {
        t["service"]: t["remote_port"]
        for t in _tunnels(compose).values()
        if t["network"] == "agent-egress"
    }
    assert "8000" not in exposed.values(), (
        f"the control plane's main listener is on the sandbox network: {exposed}"
    )


# ---------------------------------------------------------------------------
# The runner's privileges
# ---------------------------------------------------------------------------


def test_every_docker_socket_holder_is_known_and_read_only(compose: dict) -> None:
    """The socket is the host's keys — the holder list is pinned, not open.

    Two services legitimately hold it: the runner starts each sandbox, and the
    broker execs into a running one to serve terminals. Both read-only. This
    asserts the *set*, so a third service acquiring the socket fails here
    rather than being noticed later, and it asserts read-only per holder rather
    than only for the runner.
    """
    holders = {
        name: [m for m in _mounts(svc) if "docker.sock" in m]
        for name, svc in compose["services"].items()
        if any("docker.sock" in m for m in _mounts(svc))
    }
    assert set(holders) == {"runner", "workspace-broker"}, sorted(holders)
    for name, mounts in holders.items():
        assert all(m.endswith(":ro") for m in mounts), f"{name}: {mounts}"


def test_the_sandbox_image_is_required_not_defaulted(compose: dict) -> None:
    """A defaulted image name is how a host silently runs something else."""
    assert ":?" in compose["services"]["runner"]["environment"]["AGENT_SANDBOX_IMAGE"]


# ---------------------------------------------------------------------------
# The sandbox image
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# What did not port
# ---------------------------------------------------------------------------


def test_the_dropped_assertions_stay_dropped_deliberately(compose: dict) -> None:
    """Two source assertions do not port, and this records why.

    `test_the_gateway_is_reachable_from_the_sandbox_network` and the
    backend-service egress-network assertion both keyed on the gateway's own
    `backend:` service. In this topology the gateway is not a service at all —
    it is reached through the tunnel — so there is nothing to assert against.
    If a `backend` service ever appears here, that reasoning has expired and
    those assertions need rewriting rather than staying dropped.
    """
    assert "backend" not in compose["services"]


# ---------------------------------------------------------------------------
# The egress proxy's logs
# ---------------------------------------------------------------------------


def test_the_egress_proxy_hands_its_log_pipes_to_the_user_it_becomes() -> None:
    """A cheap guard in front of an expensive lesson.

    Squid opens its logs after dropping to `cache_effective_user`, and a
    container's `/dev/stdout` is a `prw-------` pipe owned by root — so the
    open fails and squid treats it as fatal. It exited on every start, 2814
    times on staging, and took every agent job with it: the runner's isolation
    preflight refuses to start without a reachable proxy.

    **The real check is `egress-proxy-starts` in docker-build.yml**, which
    starts the image, because nothing textual catches this — the broken config
    parsed, and so did the first attempted fix. This only stops the `chown`
    disappearing from the CMD in an edit that never runs CI.
    """
    dockerfile = (REPO / "deploy/host/Dockerfile.agent-egress-proxy").read_text()
    config = (REPO / "apps" / "backend" / "cloud_agent_host/egress_proxy.py").read_text()

    logs_to_pipes = "stdio:/dev/stdout" in config or "stdio:/dev/stderr" in config
    if not logs_to_pipes:
        pytest.skip("the proxy no longer logs to the container's pipes")

    assert "chown proxy /dev/stdout /dev/stderr" in dockerfile, (
        "squid drops to `proxy` before opening these pipes, and they are "
        "root-owned — without the chown it exits on every start and the "
        "runner's preflight takes the whole host down with it"
    )
