"""The release Compose file is generated, and differs from the source in two ways.

`build` is gone and `image` is set from the manifest; everything else — ports,
mounts, networks, dependencies, third-party images — has to be identical, which
is what most of this file checks. The awkward half is `build`: Compose
interpolates a file before it does anything else, and the console's build args
carry `${AGENT_WEB_API_URL:?...}`, so a deployment that has the image and no
build arguments cannot even parse the file. `up --no-build` does not skip that,
and the deploy path runs `compose run` for migrations and readiness as well.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

import pytest
import yaml
from deploy.release import artifact_compose as ac

REPO_ROOT = Path(__file__).resolve().parents[3]
SOURCES = {
    "control-plane": REPO_ROOT / "deploy/control-plane/docker-compose.yml",
    "host": REPO_ROOT / "deploy/host/docker-compose.sandbox.yml",
}
CASES = [("control-plane", "agents"), ("control-plane", "root"), ("host", None)]


def digest(seed: str) -> str:
    return "sha256:" + hashlib.sha256(seed.encode()).hexdigest()


def reference(role: str, variant: str | None = None) -> str:
    return f"ghcr.io/harvardmadsys/cloud-agent@{digest(f'{role}:{variant}')}"


def manifest() -> dict[str, Any]:
    return {
        "artifacts": [
            {"role": "control", "image": reference("control"), "platforms": ["linux/arm64"]},
            {
                "role": "web",
                "variant": "root",
                "base_path": "",
                "image": reference("web", "root"),
                "platforms": ["linux/arm64"],
            },
            {
                "role": "web",
                "variant": "agents",
                "base_path": "/agents",
                "image": reference("web", "agents"),
                "platforms": ["linux/arm64"],
            },
            {"role": "runner", "image": reference("runner"), "platforms": ["linux/amd64"]},
            {"role": "sandbox", "image": reference("sandbox"), "platforms": ["linux/amd64"]},
            {"role": "tunnel", "image": reference("tunnel"), "platforms": ["linux/amd64"]},
            {"role": "egress", "image": reference("egress"), "platforms": ["linux/amd64"]},
        ]
    }


def source(stack: str) -> dict[str, Any]:
    return yaml.safe_load(SOURCES[stack].read_text())


def rendered(stack: str, variant: str | None) -> dict[str, Any]:
    images = ac.images_from_manifest(manifest(), variant)
    return ac.render(source(stack), stack, images, variant)


@pytest.mark.parametrize(("stack", "variant"), CASES)
def test_the_release_file_builds_nothing(stack: str, variant: str | None) -> None:
    document = rendered(stack, variant)
    still_building = [name for name, service in document["services"].items() if "build" in service]
    assert not still_building, (
        f"{still_building} still carry `build`, and a required build argument makes the whole "
        "file fail to interpolate on a host that has the images and no source"
    )
    for name, service in document["services"].items():
        assert service.get("image"), f"{name} has no image, so compose would try to build it"


@pytest.mark.parametrize(("stack", "variant"), CASES)
def test_nothing_but_the_build_and_the_image_changed(stack: str, variant: str | None) -> None:
    """Structure is compared service by service, key by key.

    A generator that dropped a mount, rewrote a healthcheck or lost a dependency
    would still produce a file that starts — differently, on a host nobody is
    watching. This is the assertion that keeps the generated file honest.
    """
    before = source(stack)
    after = rendered(stack, variant)

    assert set(after["services"]) == set(before["services"])
    for name, service in before["services"].items():
        expected = {k: v for k, v in service.items() if k not in {"build", "image"}}
        actual = {k: v for k, v in after["services"][name].items() if k not in {"build", "image"}}
        assert actual == expected, f"{name} changed beyond its image"

    assert set(after) == set(before), "a top-level section (volumes, networks) went missing"
    assert after.get("networks") == before.get("networks")
    assert after.get("volumes") == before.get("volumes")


@pytest.mark.parametrize(("stack", "variant"), CASES)
def test_each_role_supplies_the_services_that_run_it(stack: str, variant: str | None) -> None:
    document = rendered(stack, variant)
    images = ac.images_from_manifest(manifest(), variant)
    for role, names in ac.STACKS[stack].items():
        for name in names:
            if role == "web" and variant is None:
                continue
            assert document["services"][name]["image"] == images[role], f"{name} != {role}"


def test_the_web_variant_chooses_the_image_the_deployment_serves() -> None:
    """`/` and `/agents` are different images, and a deployment serves one."""
    agents = rendered("control-plane", "agents")["services"]["web"]["image"]
    root = rendered("control-plane", "root")["services"]["web"]["image"]
    assert agents == reference("web", "agents")
    assert root == reference("web", "root")
    assert agents != root


def test_third_party_services_keep_their_own_images() -> None:
    document = rendered("control-plane", "agents")
    assert document["services"]["postgres"]["image"] == "postgres:16-alpine"
    assert document["services"]["edge"]["image"] == "nginx:1.27.5-alpine"


def test_supported_services_receive_the_opt_in_profiling_descriptor() -> None:
    """Values in a Compose .env are inert unless each producer passes them."""
    names = {
        "AGENT_OPERATION_PROFILING_ENABLED",
        "AGENT_OPERATION_PROFILING_RUN",
        "AGENT_OPERATION_PROFILING_SINK",
    }
    control = source("control-plane")["services"]["control-plane"]["environment"]
    host = source("host")["services"]

    assert names <= set(control)
    assert names <= set(host["runner"]["environment"])
    assert names <= set(host["workspace-broker"]["environment"])


@pytest.mark.parametrize(
    ("mutate", "stack", "variant"),
    [
        pytest.param(
            lambda m: m["artifacts"].remove(
                next(a for a in m["artifacts"] if a["role"] == "control")
            ),
            "control-plane",
            "agents",
            id="a role this stack needs is missing",
        ),
        pytest.param(
            lambda m: m["artifacts"].remove(
                next(a for a in m["artifacts"] if a["role"] == "egress")
            ),
            "host",
            None,
            id="a role the host stack needs is missing",
        ),
        pytest.param(
            lambda m: m["artifacts"][0].update(image="ghcr.io/harvardmadsys/cloud-agent:latest"),
            "control-plane",
            "agents",
            id="a tag instead of a digest",
        ),
        pytest.param(
            lambda m: m["artifacts"].append(dict(m["artifacts"][0])),
            "control-plane",
            "agents",
            id="a role appears twice",
        ),
    ],
)
def test_a_manifest_that_cannot_describe_a_deployment_is_refused(
    mutate, stack: str, variant: str | None
) -> None:
    """A role the stack runs is not optional, and a tag is not a digest.

    The egress case renders the host stack on purpose: a missing role only
    matters to the stack that runs it, and a test that asserted otherwise would
    be demanding artifacts no deployment here uses.
    """
    document = manifest()
    mutate(document)
    with pytest.raises(ac.ComposeError):
        images = ac.images_from_manifest(document, variant)
        ac.render(source(stack), stack, images, variant)


def test_the_cli_writes_a_file_with_no_build_left(tmp_path: Path) -> None:
    manifest_path = tmp_path / "release.json"
    manifest_path.write_text(json.dumps(manifest()))
    output = tmp_path / "compose.yml"
    code = ac.main(
        [
            "--manifest",
            str(manifest_path),
            "--compose",
            str(SOURCES["control-plane"]),
            "--stack",
            "control-plane",
            "--variant",
            "agents",
            "--output",
            str(output),
        ]
    )
    assert code == 0
    written = yaml.safe_load(output.read_text())
    assert "build" not in yaml.safe_dump(written)
    assert written["services"]["web"]["image"] == reference("web", "agents")


def test_the_cli_writes_nothing_when_it_refuses(tmp_path: Path) -> None:
    broken = manifest()
    broken["artifacts"] = [a for a in broken["artifacts"] if a["role"] != "egress"]
    manifest_path = tmp_path / "release.json"
    manifest_path.write_text(json.dumps(broken))
    output = tmp_path / "compose.yml"
    assert (
        ac.main(
            [
                "--manifest",
                str(manifest_path),
                "--compose",
                str(SOURCES["host"]),
                "--stack",
                "host",
                "--output",
                str(output),
            ]
        )
        == 1
    )
    assert not output.exists()
