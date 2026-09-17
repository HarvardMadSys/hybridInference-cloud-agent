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

REPO = Path(__file__).resolve().parents[3]
COMMIT = "a" * 40


def manifest() -> dict:
    artifacts = []
    for role, variant in (
        ("control", None),
        ("web", "root"),
        ("web", "agents"),
        ("runner", None),
        ("sandbox", None),
        ("tunnel", None),
        ("egress", None),
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
        "deploy_protocol": "shared-live-exact-sha-v3",
        "artifacts": artifacts,
        "runtime_catalog": {"codex": {"default": "0.145.0", "versions": ["0.145.0"]}},
        "recipe": {
            "url": "https://github.com/HarvardMadSys/hybridInference-cloud-agent/releases/download/test/recipe.tgz",
            "sha256": "b" * 64,
            "format_version": 1,
        },
        "database_revision": "0004_residency_on_membership",
        "compatibility": {
            "minimum_deployer": "shared-live-exact-sha-v3",
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
                       "sandbox": os.getenv("AGENT_SANDBOX_IMAGE"),
                       "edge": os.getenv("AGENT_EDGE_CONFIG_FILE")}) + "\n")
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
elif a[:1] == ["inspect"]:
    print(json.dumps([{"NetworkSettings": {"Networks": {
        "agent-egress": {"IPAddress": "172.24.0.2"}}}}]))
elif a[:1] == ["exec"]:
    command = " ".join(a)
    if "_shared_live_host_capabilities" in command:
        print("shared-sandbox-v1,native-fork-at-anchor-v1")
    elif "retained_docker_data_root" in command: print("/var/lib/docker")
    elif "AGENT_CONTROL_PLANE_URL" in command: print("200")
elif a[:1] == ["run"]:
    if a[-1] == "uname -r":
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
        "AGENT_RUNNER_REPLICAS": "3",
        "AGENT_SANDBOX_BACKEND": "kata",
        "AGENT_RETAINED_DOCKER_DATA_ROOT": "/var/lib/docker",
        "AGENT_WORKSPACE_BROKER_TOKEN": "fixture-broker-token",
        "AGENT_BROKER_REVERSE_BIND": "172.24.0.1",
        "AGENT_BROKER_REVERSE_PORT": "18092",
        "AGENT_WORKSPACE_BROKER_ADVERTISE_URL": "http://172.24.0.1:18092",
    }
    for compose in recipe.rglob("docker-compose*.yml"):
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
    executable(fake_bin / "docker", f"#!{sys.executable}\n{DOCKER}")
    executable(fake_bin / "git", '#!/bin/sh\necho "git was invoked" >&2\nexit 93\n')
    executable(fake_bin / "curl", '#!/bin/sh\nprintf "%s\\n" "$*" >> "$TEST_CURL_LOG"\n')
    executable(fake_bin / "sleep", "#!/bin/sh\nexit 0\n")
    executable(fake_bin / "kata-check", "#!/bin/sh\nexit 0\n")
    env = {
        k: v
        for k, v in os.environ.items()
        if not k.startswith(("AGENT_", "COMPOSE_", "TEST_"))
        and k not in {"DEPLOY_SHA", "REPLICAS", "SIBLING_COMPOSE_PROJECTS", "ALLOW_STRAY_STACK"}
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


def run(script: Path, env: dict) -> tuple[subprocess.CompletedProcess, list[dict]]:
    result = subprocess.run(
        ["bash", str(script)], env=env, text=True, capture_output=True, timeout=30
    )
    log = Path(env["TEST_COMMAND_LOG"])
    calls = [json.loads(line) for line in log.read_text().splitlines()] if log.exists() else []
    return result, calls


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
    if stack == "host":
        assert (state / ".shared-live-ready").read_text().strip() == COMMIT
        sandbox = next(a["image"] for a in manifest()["artifacts"] if a["role"] == "sandbox")
        assert any(c["args"] == ["pull", sandbox] for c in calls)
        drain = next(i for i, c in enumerate(calls) if c["args"][-2:] == ["stop", "runner"])
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
        assert "/agents" in Path(env["TEST_CURL_LOG"]).read_text()


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
