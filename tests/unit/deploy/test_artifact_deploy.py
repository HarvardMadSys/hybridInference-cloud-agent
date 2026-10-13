"""Execute the packaged deployers without a checkout, with a recorded Docker boundary."""

from __future__ import annotations

import hashlib
import io
import json
import os
import re
import shutil
import subprocess
import sys
import tarfile
from pathlib import Path

import pytest
import yaml
from deploy.release import build_recipe

from cloud_agent_shared.shared_protocol import SHARED_SANDBOX_RUNTIME_PINS

REPO = Path(__file__).resolve().parents[3]
COMMIT = "a" * 40
# Packaged images already carry their reviewed default pins. Test processes may
# run under a launcher with its own CLI version; that is not host configuration.
_RUNTIME_DEFAULT_VARIABLES = {
    "CLAUDE_CODE_VERSION",
    "CODEX_VERSION",
    "PI_VERSION",
    "OPENCODE_VERSION",
    "KILO_VERSION",
}


def manifest() -> dict:
    artifacts = []
    for role, variant in (
        ("control", None),
        ("web", "root"),
        ("web", "agents"),
        ("runner", None),
        ("sandbox", None),
        ("tunnel", None),
    ):
        digest = hashlib.sha256(f"{role}:{variant}".encode()).hexdigest()
        entry = {
            "role": role,
            "image": f"ghcr.io/harvardmadsys/cloud-agent@sha256:{digest}",
            "platforms": ["linux/arm64" if role in {"control", "web"} else "linux/amd64"],
        }
        if variant:
            entry.update(variant=variant, base_path="/agents" if variant == "agents" else "")
        artifacts.append(entry)
    return {
        "schema_version": 1,
        "source_repository": "HarvardMadSys/hybridInference-cloud-agent",
        "source_commit": COMMIT,
        "deploy_protocol": "shared-live-exact-sha-v4",
        "artifacts": artifacts,
        "runtime_catalog": {
            "codex": {
                "default": SHARED_SANDBOX_RUNTIME_PINS["codex"],
                "versions": [SHARED_SANDBOX_RUNTIME_PINS["codex"]],
            }
        },
        "recipe": {
            "url": "https://github.com/HarvardMadSys/hybridInference-cloud-agent/releases/download/test/recipe.tgz",
            "sha256": "b" * 64,
            "format_version": 1,
        },
        "database_revision": "0004_residency_on_membership",
        "compatibility": {
            "minimum_deployer": "shared-live-exact-sha-v4",
            "verified_upgrade_from": [],
            "verified_rollback_to": [],
        },
        "publisher_run_url": "https://github.com/HarvardMadSys/hybridInference-cloud-agent/actions/runs/1",
    }


DOCKER = r"""
import json, os, pathlib, subprocess, sys
a = sys.argv[1:]
log = pathlib.Path(os.environ["TEST_COMMAND_LOG"])
with log.open("a") as f:
    f.write(json.dumps({"args": a, "sha": os.getenv("AGENT_DEPLOYMENT_SHA"),
                       "deployed_at": os.getenv("AGENT_WEB_DEPLOYED_AT"),
                       "sandbox": os.getenv("AGENT_SANDBOX_IMAGE"),
                       "edge": os.getenv("AGENT_EDGE_CONFIG_FILE"),
                       "pgadmin_servers": os.getenv("AGENT_PGADMIN_SERVERS_FILE"),
                       "pgadmin_entrypoint": os.getenv("AGENT_PGADMIN_ENTRYPOINT_FILE"),
                       "physical_host": os.getenv("AGENT_PHYSICAL_HOST"),
                       "control_ssh_host": os.getenv("AGENT_CONTROL_PLANE_SSH_HOST"),
                       "runner_host": os.getenv("AGENT_RUNNER_HOST")}) + "\n")
if "config" in a and "--environment" in a:
    if os.getenv("TEST_REAL_COMPOSE"):
        raise SystemExit(subprocess.run([os.environ["TEST_REAL_COMPOSE"], *a],
                                        input=sys.stdin.read(), text=True).returncode)
    sys.stdin.read()
    values = {}
    if pathlib.Path(".env").exists():
        for line in pathlib.Path(".env").read_text().splitlines():
            k, sep, v = line.partition("=")
            if sep: values[k] = v
    values.update(os.environ)
    print("\n".join(f"{k}={v}" for k,v in values.items()))
elif "config" in a and "--services" in a:
    print("postgres\nmigrate\ncontrol-plane\nmcp-relay\nweb")
elif "config" in a and "--format" in a:
    if os.getenv("TEST_REAL_COMPOSE"):
        raise SystemExit(subprocess.run([os.environ["TEST_REAL_COMPOSE"], *a]).returncode)
    import yaml
    model = yaml.safe_load(pathlib.Path(a[a.index("-f") + 1]).read_text())
    print(json.dumps(model))
elif a[:1] == ["compose"]:
    if "build" in a: raise SystemExit(91)
    if "pull" in a: raise SystemExit(int(os.getenv("TEST_PULL_EXIT", "0")))
    if "run" in a and "cloud_agent.identity_namespace" in a:
        raise SystemExit(int(os.getenv("TEST_IDENTITY_EXIT", "0")))
    if "run" in a and "cloud_agent.deploy_readiness" in a:
        raise SystemExit(int(os.getenv("TEST_READINESS_EXIT", "0")))
    if "ps" in a:
        if a[-1] == "runner": print("runner-1\nrunner-2\nrunner-3")
        elif "--status" in a:
            if os.getenv("TEST_RUNNING", "1") == "1": print("existing-" + a[-1])
        else: print("test-" + a[-1])
elif a[:1] == ["build"]: raise SystemExit(92)
elif a[:1] == ["ps"]:
    print(os.getenv("TEST_STRAY_STACK", ""))
elif a[:1] == ["pull"]:
    raise SystemExit(int(os.getenv("TEST_SANDBOX_PULL_EXIT", "0")))
elif a[:1] == ["info"]:
    if "--format" in a: print("/var/lib/docker")
elif a[:2] == ["image", "inspect"]:
    print("sha256:" + "1" * 64)
elif a[:1] == ["inspect"]:
    print(json.dumps([{"NetworkSettings": {"Networks": {
        "artifact-test-agent-relays": {"IPAddress": "172.24.0.2"}}}}]))
elif a[:1] == ["exec"]:
    command = " ".join(a)
    if "_shared_live_host_capabilities" in command:
        print("shared-sandbox-v1,native-fork-at-anchor-v1")
    elif "--turn" in command:
        print("declined no turn was delivered")
    elif "retained_docker_data_root" in command: print("/var/lib/docker")
    elif "AGENT_CONTROL_PLANE_URL" in command: print("200")
elif a[:1] == ["run"]:
    if a[-2:] == ["initialize", "--mounted"]:
        sys.stdin.read()
        raise SystemExit(int(os.getenv("TEST_EPOCH_EXIT", "0")))
    elif a[-1] == "uname -r":
        if os.getenv("TEST_BAD_KATA") == "1": print(os.uname().release)
        else: print("isolated-guest-kernel")
    else: print("200", end="")
"""


def executable(path: Path, source: str) -> None:
    path.write_text(source)
    path.chmod(0o755)


def deployment(tmp_path: Path, stack: str, *, real_compose: bool = False) -> tuple[Path, dict]:
    with tarfile.open(fileobj=io.BytesIO(build_recipe.build(REPO)), mode="r:gz") as archive:
        archive.extractall(tmp_path / "unpacked", filter="data")
    recipe = tmp_path / "unpacked/recipe"
    assert not (recipe / ".git").exists()
    assert not (recipe / "cloud_agent").exists()
    assert not (recipe / "cloud_agent_host").exists()
    assert not list(recipe.rglob("Dockerfile*"))
    for compose in recipe.rglob("docker-compose*.yml"):
        assert all(
            "build" not in service
            for service in yaml.safe_load(compose.read_text())["services"].values()
        )

    state = tmp_path / "persistent state"
    state.mkdir()
    host_state = state / "enrollment"
    host_state.mkdir()
    (host_state / "credential").write_text("test credential")
    release = tmp_path / "release.json"
    release.write_text(json.dumps(manifest()))
    config = {
        "COMPOSE_PROJECT": "artifact-test",
        "AGENT_WEB_VARIANT": "agents",
        "AGENT_HOST_STATE_ROOT": str(host_state),
        "AGENT_HOST_SLOTS": "3",
        "AGENT_SANDBOX_BACKEND": "kata",
        "AGENT_RETAINED_DOCKER_DATA_ROOT": "/var/lib/docker",
        "AGENT_SANDBOX_MANAGER_TOKEN": "fixture-sandbox-manager-token",
        "AGENT_SANDBOX_MANAGER_REVERSE_BIND": "172.24.0.1",
        "AGENT_SANDBOX_MANAGER_REVERSE_PORT": "18092",
        "AGENT_SANDBOX_MANAGER_ADVERTISE_URL": "http://172.24.0.1:18092",
        "AGENT_EGRESS_CHAT_NETWORK_POOL": "10.240.0.0/16",
        "AGENT_EGRESS_DNS_SERVER": "1.1.1.1",
    }
    for compose in recipe.rglob("docker-compose*.yml"):
        # Optional compute is not enabled just because its overlay ships in
        # the recipe. Tests explicitly opt in with a private directory below.
        if compose.name == "docker-compose.compute.yml":
            continue
        for key in re.findall(r"\$\{([A-Z_]+):\?", compose.read_text()):
            if not key.endswith("_IMAGE") and key != "AGENT_DEPLOYMENT_SHA":
                config.setdefault(key, "fixture-value")
    for prefix in ("AGENT_TUNNEL", "AGENT_MCP_TUNNEL", "AGENT_CONTROL_PLANE_TUNNEL"):
        key, known = state / f"{prefix}.key", state / f"{prefix}.known-hosts"
        key.write_text("fixture key")
        known.write_text("fixture host fingerprint")
        config.update(
            {
                f"{prefix}_SSH_DESTINATION": "fixture@service.invalid",
                f"{prefix}_SSH_KEY": str(key),
                f"{prefix}_SSH_KNOWN_HOSTS": str(known),
            }
        )
    (state / ".env").write_text("".join(f"{k}={v}\n" for k, v in config.items()))
    (state / ".deployed-sha").write_text("previous-generation\n")
    (state / ".shared-live-ready").write_text("previous-generation\n")

    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    kata_root = tmp_path / "kata-root"
    kata_root.mkdir(mode=0o755)
    executable(fake_bin / "docker", f"#!{sys.executable}\n{DOCKER}")
    executable(fake_bin / "git", '#!/bin/sh\necho "git was invoked" >&2\nexit 93\n')
    executable(fake_bin / "curl", '#!/bin/sh\nprintf "%s\\n" "$*" >> "$TEST_CURL_LOG"\n')
    executable(fake_bin / "sleep", "#!/bin/sh\nexit 0\n")
    executable(fake_bin / "kata-check", "#!/bin/sh\nexit 0\n")
    env = {
        k: v
        for k, v in os.environ.items()
        if not k.startswith(("AGENT_", "COMPOSE_", "TEST_"))
        and k
        not in {
            "DEPLOY_SHA",
            "REPLICAS",
            "SIBLING_COMPOSE_PROJECTS",
            "ALLOW_STRAY_STACK",
            *_RUNTIME_DEFAULT_VARIABLES,
        }
    }
    env.update(
        {
            "PATH": f"{fake_bin}:{Path(sys.executable).parent}:{os.environ['PATH']}",
            "AGENT_DEPLOY_MODE": "artifacts",
            "AGENT_RELEASE_MANIFEST": str(release),
            "AGENT_DEPLOY_PYTHON": sys.executable,
            "DEPLOY_ROOT": str(state),
            "APP_DIR": str(state),
            "TEST_COMMAND_LOG": str(tmp_path / "commands.jsonl"),
            "TEST_CURL_LOG": str(tmp_path / "curl.log"),
            "KATA_SETUP_SCRIPT": str(fake_bin / "kata-check"),
            "KATA_EXTRACT_ROOT": str(kata_root),
            "HEALTH_TIMEOUT": "1",
            "SHARED_READY_TIMEOUT": "1",
        }
    )
    if real_compose:
        docker = shutil.which("docker")
        if not docker:
            pytest.skip("Docker Compose CLI is needed for its actual environment parser")
        env["TEST_REAL_COMPOSE"] = docker
    script = recipe / f"deploy/{stack}/deploy.sh"
    return script, env


def run(script: Path, env: dict, *arguments: str) -> tuple[subprocess.CompletedProcess, list[dict]]:
    result = subprocess.run(
        ["bash", str(script), *arguments], env=env, text=True, capture_output=True, timeout=30
    )
    log = Path(env["TEST_COMMAND_LOG"])
    calls = [json.loads(line) for line in log.read_text().splitlines()] if log.exists() else []
    return result, calls


@pytest.mark.parametrize("custom_definitions", [False, True])
def test_pgadmin_definitions_resolve_from_recipe_or_operator_override(
    tmp_path: Path, custom_definitions: bool
):
    """The generated Compose file runs outside the extracted recipe directory."""
    script, env = deployment(tmp_path, "control-plane")
    definitions = script.parent / "pgadmin-servers.json"
    entrypoint = script.parent / "pgadmin-entrypoint.py"
    if custom_definitions:
        definitions = tmp_path / "custom-servers.json"
        definitions.write_text('{"Servers": {}}\n')
        with (Path(env["DEPLOY_ROOT"]) / ".env").open("a") as config:
            config.write(f"AGENT_PGADMIN_SERVERS_FILE={definitions}\n")
        entrypoint = tmp_path / "custom-pgadmin-entrypoint.py"
        entrypoint.write_text("# custom pgAdmin entrypoint\n")
        with (Path(env["DEPLOY_ROOT"]) / ".env").open("a") as config:
            config.write(f"AGENT_PGADMIN_ENTRYPOINT_FILE={entrypoint}\n")
    result, calls = run(script, env)
    assert result.returncode == 0, result.stdout + result.stderr
    activation = next(call for call in calls if "--remove-orphans" in call["args"])
    assert activation["pgadmin_servers"] == str(definitions)
    assert definitions.is_file()
    assert activation["pgadmin_entrypoint"] == str(entrypoint)
    assert entrypoint.is_file()


@pytest.mark.parametrize("stack", ["control-plane", "host"])
def test_packaged_deployers_run_without_source_and_preserve_state(tmp_path: Path, stack: str):
    script, env = deployment(tmp_path, stack)
    state = Path(env["DEPLOY_ROOT"])
    marker_inode = (state / ".shared-live-ready").stat().st_ino
    result, calls = run(script, env)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "git was invoked" not in result.stderr
    assert all("build" not in call["args"] for call in calls)
    assert (state / ".deployed-sha").read_text().strip() == COMMIT
    assert (state / "enrollment/credential").read_text() == "test credential"
    assert (state / ".shared-live-ready").stat().st_ino == marker_inode
    generated = list((state / f"deploy/{stack}").glob(".release-*.yml"))
    assert len(generated) == 1
    services = yaml.safe_load(generated[0].read_text())["services"]
    assert all("build" not in service for service in services.values())
    for call in calls:
        if call["args"][:1] == ["compose"] and "up" in call["args"]:
            assert call["args"][call["args"].index("-p") + 1] == "artifact-test"
            assert "--no-build" in call["args"]
            assert call["sha"] == COMMIT
    if stack == "control-plane":
        deployed_at = (state / ".deployed-at").read_text().strip()
        assert re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z", deployed_at)
        activation = next(c for c in calls if "--remove-orphans" in c["args"])
        assert (
            activation["control_ssh_host"]
            == subprocess.check_output(["hostname", "-f"], text=True).strip()
        )
        assert activation["deployed_at"] == deployed_at
        preserved = "2026-08-07T12:00:00Z"
        (state / ".deployed-at").write_text(preserved + "\n")
        repeated, repeated_calls = run(script, env)
        assert repeated.returncode == 0, repeated.stdout + repeated.stderr
        assert (state / ".deployed-at").read_text().strip() == preserved
        assert repeated_calls[-1]["deployed_at"] == preserved
    if stack == "host":
        assert (state / ".shared-live-ready").read_text().strip() == COMMIT
        sandbox = next(a["image"] for a in manifest()["artifacts"] if a["role"] == "sandbox")
        assert any(c["args"] == ["pull", sandbox] for c in calls)
        drain = next(
            i
            for i, c in enumerate(calls)
            if c["args"] == ["stop", "--time=-1", "runner-1", "runner-2", "runner-3"]
        )
        pulls = [
            i
            for i, c in enumerate(calls)
            if c["args"][:1] == ["pull"] or c["args"][-1:] == ["pull"]
        ]
        assert pulls and max(pulls) < drain
    else:
        readiness = next(
            i for i, c in enumerate(calls) if "cloud_agent.deploy_readiness" in c["args"]
        )
        replace = next(i for i, c in enumerate(calls) if "--remove-orphans" in c["args"])
        assert readiness < replace
        assert "--expected-deployment-sha" in calls[readiness]["args"]
        assert COMMIT in calls[readiness]["args"]
        assert "--draining-host" not in calls[readiness]["args"]
        assert "/agents" in Path(env["TEST_CURL_LOG"]).read_text()


@pytest.mark.parametrize("source", ["environment", "dotenv"])
def test_control_plane_preserves_an_explicit_ssh_address(tmp_path: Path, source: str):
    script, env = deployment(tmp_path, "control-plane")
    address = "control-ssh.example.org"
    if source == "environment":
        env["AGENT_CONTROL_PLANE_SSH_HOST"] = address
    else:
        with (Path(env["DEPLOY_ROOT"]) / ".env").open("a") as config:
            config.write(f"AGENT_CONTROL_PLANE_SSH_HOST={address}\n")

    result, calls = run(script, env)

    assert result.returncode == 0, result.stdout + result.stderr
    activation = next(call for call in calls if "--remove-orphans" in call["args"])
    assert activation["control_ssh_host"] == address


def test_control_plane_defaults_to_node_hostname_when_fqdn_is_unavailable(tmp_path: Path):
    script, env = deployment(tmp_path, "control-plane")
    executable(
        tmp_path / "bin/hostname",
        '#!/bin/sh\nif [ "$1" = "-f" ]; then exit 1; fi\nprintf "service-node\\n"\n',
    )

    result, calls = run(script, env)

    assert result.returncode == 0, result.stdout + result.stderr
    activation = next(call for call in calls if "--remove-orphans" in call["args"])
    assert activation["control_ssh_host"] == "service-node"


def test_control_readiness_accepts_the_exact_draining_host(tmp_path: Path):
    script, env = deployment(tmp_path, "control-plane")
    env["AGENT_DEPLOY_DRAINING_HOST"] = "runner-draining"

    result, calls = run(script, env)

    assert result.returncode == 0, result.stdout + result.stderr
    readiness = next(call for call in calls if "cloud_agent.deploy_readiness" in call["args"])
    index = readiness["args"].index("--draining-host")
    assert readiness["args"][index + 1] == "runner-draining"


@pytest.mark.parametrize("stack", ["control-plane", "host"])
@pytest.mark.parametrize("failure", ["TEST_PULL_EXIT", "TEST_MANIFEST_CONFLICT"])
def test_preparation_failure_keeps_the_previous_generation(
    tmp_path: Path, stack: str, failure: str
):
    script, env = deployment(tmp_path, stack)
    if failure == "TEST_MANIFEST_CONFLICT":
        env["AGENT_DEPLOYMENT_SHA"] = "c" * 40
    else:
        env[failure] = "19"
    result, calls = run(script, env)
    assert result.returncode != 0
    assert (Path(env["DEPLOY_ROOT"]) / ".deployed-sha").read_text().strip() == "previous-generation"
    assert (
        Path(env["DEPLOY_ROOT"]) / ".shared-live-ready"
    ).read_text().strip() == "previous-generation"
    assert all("up" not in c["args"] and "stop" not in c["args"] for c in calls)


@pytest.mark.parametrize("running", ["0", "1"])
def test_control_readiness_failure_never_replaces_the_stack(tmp_path: Path, running: str):
    script, env = deployment(tmp_path, "control-plane")
    env.update(TEST_READINESS_EXIT="18", TEST_RUNNING=running)
    result, calls = run(script, env)
    assert result.returncode == 18, result.stdout + result.stderr
    assert all("--remove-orphans" not in c["args"] for c in calls)
    assert (Path(env["DEPLOY_ROOT"]) / ".deployed-sha").read_text().strip() == "previous-generation"
    stops = [c for c in calls if "stop" in c["args"]]
    assert bool(stops) == (running == "0")


def test_host_requires_real_isolation_before_draining(tmp_path: Path):
    script, env = deployment(tmp_path, "host")
    env["TEST_BAD_KATA"] = "1"
    result, calls = run(script, env)
    assert result.returncode != 0
    assert "NOT" in result.stderr and "VM-isolated" in result.stderr
    assert all("stop" not in c["args"] for c in calls)


def test_host_epoch_initialization_failure_keeps_the_serving_generation(tmp_path: Path):
    script, env = deployment(tmp_path, "host")
    env["TEST_EPOCH_EXIT"] = "19"
    result, calls = run(script, env)
    assert result.returncode != 0
    assert "Kata epoch initialization failed" in result.stderr
    assert all("stop" not in c["args"] and "up" not in c["args"] for c in calls)
    assert (
        Path(env["DEPLOY_ROOT"]) / ".shared-live-ready"
    ).read_text().strip() == "previous-generation"
    initializer = next(c["args"] for c in calls if c["args"][-2:] == ["initialize", "--mounted"])
    assert initializer[initializer.index("--network") + 1] == "none"
    assert initializer[initializer.index("--runtime") + 1] == "runc"
    assert initializer[initializer.index("--cap-drop") + 1] == "ALL"
    assert initializer.count("-v") == 1
    assert initializer[initializer.index("-v") + 1].endswith(
        "/var/lib/freeinference/kata-runtime:/run/agent-kata-runtime"
    )
    assert "sha256:" + "1" * 64 in initializer


def test_artifact_host_refuses_a_stray_even_with_the_legacy_override(tmp_path: Path):
    script, env = deployment(tmp_path, "host")
    env.update(TEST_STRAY_STACK="unexpected unexpected-runner-1", ALLOW_STRAY_STACK="1")
    result, calls = run(script, env)
    assert result.returncode != 0
    assert "refusing to add a second stack" in result.stderr
    assert all("pull" not in c["args"] and "up" not in c["args"] for c in calls)


@pytest.mark.parametrize("exported", [COMMIT, "c" * 40, f'"{COMMIT}"'])
def test_real_compose_precedence_reaches_the_deployer(tmp_path: Path, exported: str):
    script, env = deployment(tmp_path, "control-plane", real_compose=True)
    dotenv = Path(env["DEPLOY_ROOT"]) / ".env"
    with dotenv.open("a") as file:
        file.write(f"AGENT_DEPLOYMENT_SHA={'c' * 40 if exported == COMMIT else COMMIT}\n")
        file.write("PREFIX=/agents\nAGENT_WEB_BASE_PATH=$PREFIX\n")
    env["AGENT_DEPLOYMENT_SHA"] = exported
    result, calls = run(script, env)
    assert (result.returncode == 0) == (exported == COMMIT), result.stdout + result.stderr
    if exported != COMMIT:
        assert all("up" not in c["args"] and "pull" not in c["args"] for c in calls)


@pytest.mark.parametrize("stack", ["control-plane", "host"])
def test_real_compose_keeps_persistent_mounts_outside_the_recipe(tmp_path: Path, stack: str):
    script, env = deployment(tmp_path, stack, real_compose=True)
    result, calls = run(script, env)
    assert result.returncode == 0, result.stdout + result.stderr
    state = Path(env["DEPLOY_ROOT"])
    generated = next((state / f"deploy/{stack}").glob(".release-*.yml"))
    prepared = (
        next(c for c in calls if c["sha"] == COMMIT and c["sandbox"] is not None)
        if stack == "host"
        else next(c for c in calls if c["edge"])
    )
    config = subprocess.run(
        [
            env["TEST_REAL_COMPOSE"],
            "compose",
            "-p",
            "artifact-test",
            "--env-file",
            str(state / ".env"),
            "-f",
            str(generated),
            "--profile",
            "edge",
            "config",
            "--format",
            "json",
        ],
        env={
            **env,
            "AGENT_DEPLOYMENT_SHA": COMMIT,
            "AGENT_SANDBOX_IMAGE": prepared["sandbox"] or "",
            "AGENT_EDGE_CONFIG_FILE": prepared["edge"] or "",
        },
        text=True,
        capture_output=True,
        check=False,
    )
    assert config.returncode == 0, config.stderr
    document = json.loads(config.stdout)
    if stack == "host":
        mounts = {mount["target"]: mount for mount in document["services"]["runner"]["volumes"]}
        assert (
            Path(mounts["/run/cloud-agent/shared-live-ready"]["source"]).resolve()
            == (state / ".shared-live-ready").resolve()
        )
        assert str(state / "enrollment") in mounts
    else:
        edge = document["services"]["edge"]["volumes"][0]
        assert Path(edge["source"]).resolve() == (script.parent / "edge.nginx.conf").resolve()
        assert document["volumes"]["agent-postgres"]["name"] == "artifact-test_agent-postgres"


def test_root_web_variant_controls_both_image_and_health_path(tmp_path: Path):
    script, env = deployment(tmp_path, "control-plane")
    dotenv = Path(env["DEPLOY_ROOT"]) / ".env"
    dotenv.write_text(
        dotenv.read_text().replace("AGENT_WEB_VARIANT=agents", "AGENT_WEB_VARIANT=root")
    )
    result, _ = run(script, env)
    assert result.returncode == 0, result.stdout + result.stderr
    generated = next((Path(env["DEPLOY_ROOT"]) / "deploy/control-plane").glob(".release-*.yml"))
    root = next(a for a in manifest()["artifacts"] if a.get("variant") == "root")
    assert yaml.safe_load(generated.read_text())["services"]["web"]["image"] == root["image"]
    probes = Path(env["TEST_CURL_LOG"]).read_text()
    assert "http://127.0.0.1:3002/" in probes
    assert "/agents" not in probes


def test_manifest_validation_and_rendering_use_the_same_bytes(tmp_path: Path):
    script, env = deployment(tmp_path, "control-plane")
    wrapper = tmp_path / "python-wrapper"
    executable(
        wrapper,
        f"#!{sys.executable}\n"
        "import json, os, pathlib, subprocess, sys\n"
        f"result = subprocess.run([{sys.executable!r}, *sys.argv[1:]])\n"
        "if sys.argv[1].endswith('release_env.py') and result.returncode == 0:\n"
        "    path = pathlib.Path(os.environ['AGENT_RELEASE_MANIFEST'])\n"
        "    document = json.loads(path.read_text())\n"
        "    document['artifacts'][0]['image'] = 'ghcr.io/harvardmadsys/cloud-agent@sha256:' + 'f' * 64\n"
        "    path.write_text(json.dumps(document))\n"
        "raise SystemExit(result.returncode)\n",
    )
    env["AGENT_DEPLOY_PYTHON"] = str(wrapper)
    result, _ = run(script, env)
    assert result.returncode == 0, result.stdout + result.stderr
    generated = next((Path(env["DEPLOY_ROOT"]) / "deploy/control-plane").glob(".release-*.yml"))
    service = yaml.safe_load(generated.read_text())["services"]["control-plane"]
    assert service["image"] == manifest()["artifacts"][0]["image"]


# ------------------------------------------------------------------ phases
#
# The release design sequences a deployment across two machines: both sides
# prepare, the control plane then prepares its database and checks the identity
# namespace, the runner host then publishes its generation, and only then does
# the control plane switch public services. A deployer could not express that
# while each side had one entry point that did all of it (#270).
#
# What these pin is the property that makes the phases worth having: `prepare`
# replaces nothing, and the things that replace something are all in `activate`.


def replaces_something(call: dict) -> bool:
    """Would this Docker invocation disturb a container that is serving?

    `up -d --wait postgres` and the readiness `run --rm` are in neither camp by
    name, so the check is narrow on purpose: a `stop`, or an `up` that is not
    the durable dependency the database phase brings up by itself.
    """
    arguments = call["args"]
    if "stop" in arguments or "down" in arguments:
        return True
    if "up" in arguments:
        return "postgres" not in arguments
    return False


@pytest.mark.parametrize("stack", ["control-plane", "host"])
def test_preparing_replaces_nothing_that_is_serving(tmp_path: Path, stack: str):
    script, env = deployment(tmp_path, stack)
    state = Path(env["DEPLOY_ROOT"])

    result, calls = run(script, env, "--phase", "prepare", COMMIT)

    assert result.returncode == 0, result.stdout + result.stderr
    assert not [c for c in calls if replaces_something(c)], [c["args"] for c in calls]
    assert any(c["args"][:1] == ["pull"] or c["args"][-1:] == ["pull"] for c in calls), (
        "preparing is where the images arrive"
    )
    # The generation this host advertises, and the record of what is deployed,
    # are both untouched: nothing has been replaced, so nothing may claim to be.
    assert (state / ".deployed-sha").read_text().strip() == "previous-generation"
    assert (state / ".shared-live-ready").read_text().strip() == "previous-generation"


def test_the_control_planes_database_phase_migrates_and_stops_there(tmp_path: Path):
    """The phase the design puts between the two hosts' preparation and the
    host's replacement, which had no name a deployer could call."""
    script, env = deployment(tmp_path, "control-plane")

    result, calls = run(script, env, "--phase", "prepare-database", COMMIT)

    assert result.returncode == 0, result.stdout + result.stderr
    assert any("migrate" in c["args"] for c in calls)
    assert any("cloud_agent.identity_namespace" in c["args"] for c in calls)
    assert not any("cloud_agent.deploy_readiness" in c["args"] for c in calls), (
        "the fence belongs to activation"
    )
    assert not any("--remove-orphans" in c["args"] for c in calls)
    assert (Path(env["DEPLOY_ROOT"]) / ".deployed-sha").read_text().strip() == "previous-generation"


@pytest.mark.parametrize("stack", ["control-plane", "host"])
def test_the_phases_together_do_what_one_invocation_does(tmp_path: Path, stack: str):
    """The split has to be a split, not a rewrite: running the phases in order
    has to end where the single entry point ends."""
    phases = (
        ["prepare", "prepare-database", "activate"]
        if stack == "control-plane"
        else [
            "prepare",
            "activate",
        ]
    )
    script, env = deployment(tmp_path, stack)
    state = Path(env["DEPLOY_ROOT"])

    for phase in phases:
        result, _ = run(script, env, "--phase", phase, COMMIT)
        assert result.returncode == 0, f"{phase}: {result.stdout}{result.stderr}"

    assert (state / ".deployed-sha").read_text().strip() == COMMIT
    if stack == "host":
        assert (state / ".shared-live-ready").read_text().strip() == COMMIT


@pytest.mark.parametrize("stack", ["control-plane", "host"])
def test_an_unknown_phase_is_refused_before_anything_runs(tmp_path: Path, stack: str):
    script, env = deployment(tmp_path, stack)

    result, calls = run(script, env, "--phase", "activat", COMMIT)

    assert result.returncode != 0
    assert "unknown phase" in result.stderr
    assert calls == []


def test_activating_on_its_own_still_names_this_machine(tmp_path: Path):
    """The state that has to survive the boundary.

    `up` resolved the physical host and the runner host in the middle of the
    rollout and exported them for the `compose up` that followed. Split into two
    processes, the second one would have started the runners with no name for
    their machine — and the admin inventory identifies a server by exactly that.
    """
    script, env = deployment(tmp_path, "host")

    prepared, _ = run(script, env, "--phase", "prepare", COMMIT)
    assert prepared.returncode == 0, prepared.stdout + prepared.stderr
    activated, calls = run(script, env, "--phase", "activate", COMMIT)
    assert activated.returncode == 0, activated.stdout + activated.stderr

    replacement = next(
        call for call in calls if "up" in call["args"] and "--remove-orphans" in call["args"]
    )
    assert replacement["physical_host"], replacement
    assert replacement["runner_host"], replacement


def test_a_cold_machine_has_its_listeners_before_the_host_activates(tmp_path: Path):
    """The deadlock the phase split introduced, and the design's constraint 6.

    With the host activating before the control plane, a cold machine had its
    database prepared, its runners replaced, and then waited for an API and an
    MCP relay that only a later phase would have started. Starting a service
    that is not running replaces nothing, which is what lets the bootstrap live
    in the database phase where the host can reach it.
    """
    script, env = deployment(tmp_path, "control-plane")
    env["TEST_RUNNING"] = "0"

    result, calls = run(script, env, "--phase", "prepare-database", COMMIT)

    assert result.returncode == 0, result.stdout + result.stderr
    started = next(call for call in calls if "up" in call["args"] and "--no-deps" in call["args"])
    assert "control-plane" in started["args"] and "mcp-relay" in started["args"]
    assert (Path(env["DEPLOY_ROOT"]) / ".bootstrapped-services").read_text().split() == [
        "control-plane",
        "mcp-relay",
    ]
    assert not any("cloud_agent.deploy_readiness" in c["args"] for c in calls), (
        "the fence is still activation's"
    )


def test_a_warm_machine_bootstraps_nothing(tmp_path: Path):
    """The ordinary rolling deploy keeps the old API and relay serving."""
    script, env = deployment(tmp_path, "control-plane")

    result, calls = run(script, env, "--phase", "prepare-database", COMMIT)

    assert result.returncode == 0, result.stdout + result.stderr
    assert not any("--no-deps" in c["args"] and "up" in c["args"] for c in calls)
    assert not (Path(env["DEPLOY_ROOT"]) / ".bootstrapped-services").exists()


def test_a_failed_activation_stops_only_what_the_database_phase_started(tmp_path: Path):
    """The two phases are two processes, so the list of services this
    deployment started has to reach the one that may have to undo them."""
    script, env = deployment(tmp_path, "control-plane")
    env["TEST_RUNNING"] = "0"
    prepared, _ = run(script, env, "--phase", "prepare-database", COMMIT)
    assert prepared.returncode == 0, prepared.stdout + prepared.stderr

    env["TEST_READINESS_EXIT"] = "9"
    failed, calls = run(script, env, "--phase", "activate", COMMIT)

    assert failed.returncode != 0
    stopped = next(call for call in calls if "stop" in call["args"])
    assert "control-plane" in stopped["args"] and "mcp-relay" in stopped["args"]
    assert not any("--remove-orphans" in c["args"] for c in calls), "nothing was replaced"
    assert (Path(env["DEPLOY_ROOT"]) / ".deployed-sha").read_text().strip() == "previous-generation"


def test_a_successful_activation_stops_adopting_them(tmp_path: Path):
    """Past the gate they are the target stack, not temporary readiness aids."""
    script, env = deployment(tmp_path, "control-plane")
    env["TEST_RUNNING"] = "0"
    run(script, env, "--phase", "prepare-database", COMMIT)

    result, _ = run(script, env, "--phase", "activate", COMMIT)

    assert result.returncode == 0, result.stdout + result.stderr
    assert not (Path(env["DEPLOY_ROOT"]) / ".bootstrapped-services").exists()


def test_retrying_a_failed_activation_restores_what_its_cleanup_stopped(tmp_path: Path):
    """The retry path the phases make reachable, and the one review found dead.

    A failed fence leaves the trap stopping the listeners this deployment
    started. Going straight back to the fence then waits for a heartbeat newer
    than the check, with no API running to accept one: the transient failure is
    gone and the retry still times out.
    """
    script, env = deployment(tmp_path, "control-plane")
    env["TEST_RUNNING"] = "0"
    run(script, env, "--phase", "prepare-database", COMMIT)

    env["TEST_READINESS_EXIT"] = "9"
    failed, first = run(script, env, "--phase", "activate", COMMIT)
    assert failed.returncode != 0
    assert any("stop" in call["args"] for call in first)
    assert not (Path(env["DEPLOY_ROOT"]) / ".bootstrapped-services").exists()

    del env["TEST_READINESS_EXIT"]
    retried, second = run(script, env, "--phase", "activate", COMMIT)

    assert retried.returncode == 0, retried.stdout + retried.stderr
    restarted = next(
        call for call in second if "up" in call["args"] and "--no-deps" in call["args"]
    )
    assert "control-plane" in restarted["args"] and "mcp-relay" in restarted["args"]
    fence = next(i for i, c in enumerate(second) if "cloud_agent.deploy_readiness" in c["args"])
    started = next(
        i for i, c in enumerate(second) if "up" in c["args"] and "--no-deps" in c["args"]
    )
    assert started < fence, "the API has to be accepting heartbeats before the fence waits for one"
    assert (Path(env["DEPLOY_ROOT"]) / ".deployed-sha").read_text().strip() == COMMIT


def test_repeating_the_database_phase_keeps_this_deployments_bootstrap_record(
    tmp_path: Path,
):
    """Finding them running is not a reason to forget them: they may be running
    *because* an earlier run of this same phase started them."""
    script, env = deployment(tmp_path, "control-plane")
    env["TEST_RUNNING"] = "0"
    first, _ = run(script, env, "--phase", "prepare-database", COMMIT)
    assert first.returncode == 0, first.stdout + first.stderr
    record = Path(env["DEPLOY_ROOT"]) / ".bootstrapped-services"
    assert record.read_text().split() == ["control-plane", "mcp-relay"]

    env["TEST_RUNNING"] = "1"
    again, _ = run(script, env, "--phase", "prepare-database", COMMIT)

    assert again.returncode == 0, again.stdout + again.stderr
    assert record.read_text().split() == ["control-plane", "mcp-relay"], (
        "the second run found them running and must not disown them"
    )

    env["TEST_READINESS_EXIT"] = "9"
    failed, calls = run(script, env, "--phase", "activate", COMMIT)
    assert failed.returncode != 0
    stopped = next(call for call in calls if "stop" in call["args"])
    assert "control-plane" in stopped["args"] and "mcp-relay" in stopped["args"]


def test_a_warm_machine_never_records_the_generation_that_is_serving(tmp_path: Path):
    """The other direction, and the one that matters more: a failure must not
    stop an API this deployment found already serving."""
    script, env = deployment(tmp_path, "control-plane")
    run(script, env, "--phase", "prepare-database", COMMIT)
    assert not (Path(env["DEPLOY_ROOT"]) / ".bootstrapped-services").exists()

    env["TEST_READINESS_EXIT"] = "9"
    failed, calls = run(script, env, "--phase", "activate", COMMIT)

    assert failed.returncode != 0
    assert not any("stop" in call["args"] for call in calls), [c["args"] for c in calls]


def test_abandoning_stops_what_this_deployment_started_and_nothing_else(tmp_path: Path):
    """The case a deployer has and this script did not: the *other* machine
    failed, so there is no activation to run, and the only code that cleaned
    these up lived inside the activation."""
    script, env = deployment(tmp_path, "control-plane")
    env["TEST_RUNNING"] = "0"
    prepared, _ = run(script, env, "--phase", "prepare-database", COMMIT)
    assert prepared.returncode == 0, prepared.stdout + prepared.stderr

    result, calls = run(script, env, "--phase", "abandon", COMMIT)

    assert result.returncode == 0, result.stdout + result.stderr
    stopped = next(call for call in calls if "stop" in call["args"])
    assert "control-plane" in stopped["args"] and "mcp-relay" in stopped["args"]
    assert not (Path(env["DEPLOY_ROOT"]) / ".bootstrapped-services").exists()
    assert not any("--remove-orphans" in c["args"] for c in calls), "it deploys nothing"
    assert (Path(env["DEPLOY_ROOT"]) / ".deployed-sha").read_text().strip() == "previous-generation"


def test_abandoning_a_warm_machine_touches_nothing(tmp_path: Path):
    """A service that was already serving when this deployment began is not
    this deployment's to stop. Undoing a rollout must not take down the one it
    failed to replace."""
    script, env = deployment(tmp_path, "control-plane")
    run(script, env, "--phase", "prepare-database", COMMIT)

    result, calls = run(script, env, "--phase", "abandon", COMMIT)

    assert result.returncode == 0, result.stdout + result.stderr
    assert not any("stop" in call["args"] for call in calls), [c["args"] for c in calls]


def test_abandoning_twice_is_not_an_error(tmp_path: Path):
    """A cleanup a workflow runs on failure will sometimes run with nothing to
    do, and that is not a failure of its own."""
    script, env = deployment(tmp_path, "control-plane")
    env["TEST_RUNNING"] = "0"
    run(script, env, "--phase", "prepare-database", COMMIT)
    first, _ = run(script, env, "--phase", "abandon", COMMIT)
    # The recorded boundary appends across invocations; this assertion is about
    # the second one alone.
    Path(env["TEST_COMMAND_LOG"]).unlink()
    second, calls = run(script, env, "--phase", "abandon", COMMIT)

    assert first.returncode == 0 and second.returncode == 0
    assert not any("stop" in call["args"] for call in calls), [c["args"] for c in calls]


def test_compute_overlay_is_packaged_and_enabled_by_persistent_configuration(tmp_path: Path):
    script, env = deployment(tmp_path, "control-plane")
    private = tmp_path / "private compute"
    private.mkdir()
    configuration = Path(env["DEPLOY_ROOT"]) / ".env"
    with configuration.open("a") as output:
        output.write(f"AGENT_COMPUTE_DIRECTORY={private}\n")
    result, calls = run(script, env)
    assert result.returncode == 0, result.stdout + result.stderr
    overlay = str(script.parent / "docker-compose.compute.yml")
    assert Path(overlay).is_file()
    compose_calls = [call["args"] for call in calls if call["args"][:1] == ["compose"]]
    # Includes preparation/migrations/readiness as well as final activation.
    assert compose_calls
    assert all(overlay in args for args in compose_calls if "config" not in args)
    assert (script.parent / "compute.example.json").is_file()


def test_private_override_is_last_for_every_artifact_stack_compose_call(tmp_path: Path):
    script, env = deployment(tmp_path, "control-plane")
    private_compute = tmp_path / "private-compute"
    private_compute.mkdir()
    override = tmp_path / "control-plane-private.yml"
    override.write_text("services:\n  control-plane:\n    environment:\n      TEST_OVERRIDE: set\n")
    configuration = Path(env["DEPLOY_ROOT"]) / ".env"
    with configuration.open("a") as output:
        output.write(
            f"AGENT_COMPUTE_DIRECTORY={private_compute}\n"
            f"AGENT_CONTROL_PLANE_COMPOSE_OVERRIDE_FILE={override}\n"
        )

    result, calls = run(script, env)

    assert result.returncode == 0, result.stdout + result.stderr
    compose_calls = [
        call["args"] for call in calls if call["args"][:1] == ["compose"] and "-p" in call["args"]
    ]
    assert compose_calls
    compute_overlay = str(script.parent / "docker-compose.compute.yml")
    for arguments in compose_calls:
        compose_files = [
            arguments[index + 1] for index, argument in enumerate(arguments) if argument == "-f"
        ]
        assert compose_files[-2:] == [compute_overlay, str(override)]


def test_control_plane_generates_one_persistent_compute_encryption_key(tmp_path: Path):
    script, env = deployment(tmp_path, "control-plane")
    dotenv = Path(env["DEPLOY_ROOT"]) / ".env"
    assert "AGENT_COMPUTE_SECRET_KEY=" not in dotenv.read_text()

    first, _ = run(script, env, "--phase", "prepare", COMMIT)
    assert first.returncode == 0, first.stdout + first.stderr
    values = dict(line.split("=", 1) for line in dotenv.read_text().splitlines() if "=" in line)
    generated = values["AGENT_COMPUTE_SECRET_KEY"]
    assert re.fullmatch(r"[a-f0-9]{64}", generated)
    assert dotenv.stat().st_mode & 0o077 == 0
    assert generated not in first.stdout + first.stderr

    second, _ = run(script, env, "--phase", "prepare", COMMIT)
    assert second.returncode == 0, second.stdout + second.stderr
    repeated = dict(line.split("=", 1) for line in dotenv.read_text().splitlines() if "=" in line)
    assert repeated["AGENT_COMPUTE_SECRET_KEY"] == generated


def test_control_plane_preserves_an_existing_compute_encryption_key(tmp_path: Path):
    script, env = deployment(tmp_path, "control-plane")
    dotenv = Path(env["DEPLOY_ROOT"]) / ".env"
    existing = "existing-compute-key-material-0123456789"
    with dotenv.open("a") as output:
        output.write(f"AGENT_COMPUTE_SECRET_KEY={existing}\n")

    result, _ = run(script, env, "--phase", "prepare", COMMIT)

    assert result.returncode == 0, result.stdout + result.stderr
    assert dotenv.read_text().splitlines().count(f"AGENT_COMPUTE_SECRET_KEY={existing}") == 1
    assert "generated and persisted" not in result.stdout


def test_compute_overlay_gives_credentials_and_mount_only_to_main_control_plane(tmp_path: Path):
    script, env = deployment(tmp_path, "control-plane", real_compose=True)
    private = tmp_path / "private compute"
    private.mkdir()
    configuration = Path(env["DEPLOY_ROOT"]) / ".env"
    with configuration.open("a") as output:
        output.write(
            f"AGENT_COMPUTE_DIRECTORY={private}\nAGENT_COMPUTE_SECRET_KEY=test-only-independent-key-material\nAGENT_CLOUDLAB_API_URL=https://api.example.org\n"
        )
    result, _ = run(script, env, "--phase", "prepare")
    assert result.returncode == 0, result.stdout + result.stderr
    generated = next((Path(env["DEPLOY_ROOT"]) / "deploy/control-plane").glob(".release-*.yml"))
    config = subprocess.run(
        [
            env["TEST_REAL_COMPOSE"],
            "compose",
            "--env-file",
            str(configuration),
            "-f",
            str(generated),
            "-f",
            str(script.parent / "docker-compose.compute.yml"),
            "config",
            "--format",
            "json",
        ],
        env={**env, "AGENT_DEPLOYMENT_SHA": COMMIT},
        capture_output=True,
        text=True,
        timeout=20,
    )
    assert config.returncode == 0, config.stderr
    model = json.loads(config.stdout)
    for name, service in model["services"].items():
        keys = {
            key
            for key in service.get("environment", {})
            if key.startswith("AGENT_COMPUTE_") or key == "AGENT_CLOUDLAB_API_URL"
        }
        compute_mounts = [
            mount for mount in service.get("volumes", []) if mount.get("target") == "/run/compute"
        ]
        if name == "control-plane":
            assert service["environment"]["AGENT_COMPUTE_CONFIG"] == "/run/compute/compute.json"
            assert (
                service["environment"]["AGENT_COMPUTE_SECRET_KEY"]
                == "test-only-independent-key-material"
            )
            assert len(compute_mounts) == 1 and compute_mounts[0]["read_only"] is True
            assert compute_mounts[0]["source"] == str(private)
        else:
            assert not keys, f"compute authority leaked to {name}"
            assert not compute_mounts
