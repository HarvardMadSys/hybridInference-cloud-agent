"""The release Compose file is generated, and differs from the source in two ways.

`build` is gone and `image` is set from the manifest; everything else — ports,
mounts, networks, dependencies, third-party images — has to be identical, which
is what most of this file checks. The one other key that may go is the
`pull_policy: never` of a service that runs the image another service of its
role builds: a release pulls that image instead. The awkward half is `build`: Compose
interpolates a file before it does anything else, so a build argument that
required a variable would stop a deployment that has the image, and no build
arguments, from even parsing the file. `up --no-build` does not skip that,
and the deploy path runs `compose run` for migrations and readiness as well.
"""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from typing import Any

import pytest
import yaml
from deploy.release import artifact_compose as ac

REPO_ROOT = Path(__file__).resolve().parents[3]
LOCAL_SOURCE = REPO_ROOT / "deploy/local/docker-compose.yml"
LOCAL_DEV_SCRIPT = REPO_ROOT / "deploy/local/dev.sh"
LOCAL_EDGE_DOCKERFILE = REPO_ROOT / "deploy/local/Dockerfile.edge"
SOURCES = {
    "control-plane": REPO_ROOT / "deploy/control-plane/docker-compose.yml",
    "host": REPO_ROOT / "deploy/host/docker-compose.sandbox.yml",
}
CASES = [("control-plane", "root"), ("host", None)]


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
            {"role": "runner", "image": reference("runner"), "platforms": ["linux/amd64"]},
            {"role": "sandbox", "image": reference("sandbox"), "platforms": ["linux/amd64"]},
            {"role": "tunnel", "image": reference("tunnel"), "platforms": ["linux/amd64"]},
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
    release_services = {name for names in ac.STACKS[stack].values() for name in names}

    assert set(after["services"]) == set(before["services"])
    for name, service in before["services"].items():
        moved = {"build", "image"} | (
            {"pull_policy"} if _follows_a_builder(before, name) else set()
        )
        expected = {k: v for k, v in service.items() if k not in moved}
        actual = {k: v for k, v in after["services"][name].items() if k not in moved}
        assert actual == expected, f"{name} changed beyond its image"
        if name not in release_services:
            assert after["services"][name]["image"] == service["image"], (
                f"{name} is not a release artifact, so its image must be preserved"
            )

    assert set(after) == set(before), "a top-level section (volumes, networks) went missing"
    assert after.get("networks") == before.get("networks")
    assert after.get("volumes") == before.get("volumes")


def _follows_a_builder(document: dict[str, Any], name: str) -> bool:
    service = document["services"][name]
    return "build" not in service and any(
        "build" in other and other.get("image") == service.get("image")
        for other in document["services"].values()
    )


@pytest.mark.parametrize(("stack", "variant"), CASES)
def test_each_role_supplies_the_services_that_run_it(stack: str, variant: str | None) -> None:
    document = rendered(stack, variant)
    images = ac.images_from_manifest(manifest(), variant)
    for role, names in ac.STACKS[stack].items():
        for name in names:
            if role == "web" and variant is None:
                continue
            assert document["services"][name]["image"] == images[role], f"{name} != {role}"


#: The services each stack runs from someone else's image. The host stack runs
#: none: its runners, sandbox manager and tunnels all run this release's images.
THIRD_PARTY = {"control-plane": {"edge", "pgadmin", "postgres"}, "host": set()}


@pytest.mark.parametrize("stack", sorted(SOURCES))
def test_every_third_party_image_is_pinned_by_version_and_index_digest(stack: str) -> None:
    """A tag alone runs whatever it points to on the day.

    `postgres:16-alpine` took a deployment from 16.15 to 16.16 in the middle of
    an unrelated deploy: an artifact deployment pulls every image it names again,
    and the release keeps a third-party image exactly as the source names it. So
    the source names each by the digest of its multi-architecture index, with the
    version tag beside it for whoever reads or bumps it.
    """
    services = source(stack)["services"]
    owned = {name for names in ac.STACKS[stack].values() for name in names}

    assert set(services) - owned == THIRD_PARTY[stack]
    assert ac.unpinned_third_party_images(source(stack), stack) == []
    for name in THIRD_PARTY[stack]:
        assert ac.is_pinned(services[name]["image"]), services[name]["image"]


PINNED_POSTGRES = "postgres:16.16-alpine@sha256:" + "0" * 64


@pytest.mark.parametrize(
    "image",
    [
        pytest.param("postgres:16-alpine", id="a tag alone"),
        pytest.param("postgres:16.16-alpine", id="a version tag alone"),
        pytest.param("postgres@sha256:" + "0" * 64, id="a digest without its version"),
        pytest.param("postgres:16.16-alpine@sha256:" + "0" * 63, id="a truncated digest"),
        pytest.param("postgres:16.16-alpine@sha256:" + "A" * 64, id="a digest not in hex"),
        pytest.param(f"${{AGENT_POSTGRES_IMAGE:-{PINNED_POSTGRES}}}", id="a host-chosen image"),
        pytest.param(None, id="no image at all"),
    ],
)
def test_a_third_party_image_named_any_other_way_is_refused(image: str | None) -> None:
    document = source("control-plane")
    if image is None:
        document["services"]["postgres"].pop("image")
    else:
        document["services"]["postgres"]["image"] = image
    images = ac.images_from_manifest(manifest(), "root")

    with pytest.raises(ac.ComposeError, match=r"version tag and the digest.*postgres runs"):
        ac.render(document, "control-plane", images, "root")
    # The recipe is generated through the same check, so it cannot ship one either.
    with pytest.raises(ac.ComposeError, match="postgres runs"):
        ac.recipe_template(document, "control-plane")


def test_a_third_party_service_on_the_host_is_held_to_the_same_rule() -> None:
    """The host stack has none today; the first one must arrive pinned."""
    document = source("host")
    images = ac.images_from_manifest(manifest(), None)
    document["services"]["cache"] = {"image": "redis:7-alpine"}
    with pytest.raises(ac.ComposeError, match="cache runs 'redis:7-alpine'"):
        ac.render(document, "host", images, None)

    pinned = "registry.example:5000/library/redis:7.4.1-alpine@sha256:" + "a" * 64
    document["services"]["cache"]["image"] = pinned
    assert ac.render(document, "host", images, None)["services"]["cache"]["image"] == pinned


def test_the_local_stack_runs_the_third_party_images_the_deployment_pins() -> None:
    """Locally as deployed: the pinned image, never whatever a tag means today.

    The local stack layers on the control plane's file, so its Postgres is that
    file's; the psql that registers the local host, the engine probe in dev.sh
    and the local edge's nginx name third-party images of their own, and each
    must be the one the deployment pins rather than a second, floating name.
    """
    services = source("control-plane")["services"]
    postgres, edge = services["postgres"]["image"], services["edge"]["image"]
    assert ac.is_pinned(postgres) and ac.is_pinned(edge)

    local = yaml.safe_load(LOCAL_SOURCE.read_text())["services"]
    assert local["local-bootstrap"]["image"] == postgres
    # An image reference, not the `@postgres:5432` of a database URL.
    named = re.findall(r"(?<![@\w.-])postgres:[^\s\\'\"]+", LOCAL_DEV_SCRIPT.read_text())
    assert named and set(named) == {postgres}
    bases = [
        line.split(maxsplit=1)[1]
        for line in LOCAL_EDGE_DOCKERFILE.read_text().splitlines()
        if line.startswith("FROM ")
    ]
    assert bases == [edge]


CONTROL_FOLLOWERS = ("migrate", "control-plane", "mcp-relay", "github-relay")


def test_the_control_image_is_pulled_once_for_every_service_that_runs_it() -> None:
    """The source builds it in `keys` alone; the release pulls it for all five.

    `pull_policy: never` is what keeps a source deployment from pulling a name
    only `keys` builds. Left in a release file, it would stop `compose pull`
    fetching the image for the services that carry it.
    """
    before = source("control-plane")["services"]
    assert [name for name in ac.STACKS["control-plane"]["control"] if "build" in before[name]] == [
        "keys"
    ]
    for name in CONTROL_FOLLOWERS:
        assert before[name]["image"] == before["keys"]["image"], name
        assert before[name]["pull_policy"] == "never", name

    for document in (
        rendered("control-plane", "root"),
        ac.recipe_template(source("control-plane"), "control-plane"),
    ):
        services = document["services"]
        for name in ("keys", *CONTROL_FOLLOWERS):
            assert services[name]["image"] == services["keys"]["image"], name
            assert "pull_policy" not in services[name], name
            assert "build" not in services[name], name

    # The packaged recipe is rendered again at deploy time, its followers now
    # carrying the placeholder like their builder.
    template = ac.recipe_template(source("control-plane"), "control-plane")
    images = ac.images_from_manifest(manifest(), "root")
    deployed = ac.render(template, "control-plane", images, "root")["services"]
    assert {deployed[name]["image"] for name in ("keys", *CONTROL_FOLLOWERS)} == {images["control"]}


@pytest.mark.parametrize(
    "mutate",
    [
        pytest.param(
            lambda services: services["mcp-relay"].update(image="cloudagent-mcp-relay:latest"),
            id="a follower names an image nobody here builds",
        ),
        pytest.param(
            lambda services: services["mcp-relay"].pop("pull_policy"),
            id="a follower that may pull a name only the builder makes",
        ),
        pytest.param(
            lambda services: services["mcp-relay"].update(image=services["web"]["image"]),
            id="a follower of another role's build",
        ),
        pytest.param(
            lambda services: services["keys"].pop("image"),
            id="a builder that names no image",
        ),
    ],
)
def test_a_follower_must_run_exactly_its_roles_built_image(mutate) -> None:
    document = source("control-plane")
    document["services"]["web"]["image"] = "cloudagent-web:local"
    mutate(document["services"])
    images = ac.images_from_manifest(manifest(), "root")
    with pytest.raises(ac.ComposeError, match="drifted apart"):
        ac.render(document, "control-plane", images, "root")


def test_kata_epoch_mount_is_host_wide_read_only_and_tracks_atomic_replacements():
    document = source("host")
    mounts = document["services"]["runner"]["volumes"]
    matches = [
        item
        for item in mounts
        if isinstance(item, dict) and item.get("target") == "/run/agent-kata-runtime"
    ]
    assert matches == [
        {
            "type": "bind",
            "source": "/var/lib/freeinference/kata-runtime",
            "target": "/run/agent-kata-runtime",
            "read_only": True,
            "bind": {"create_host_path": True},
        }
    ]


def test_supported_services_receive_the_opt_in_profiling_descriptor() -> None:
    """Values in a Compose .env are inert unless each producer passes them.

    The mode is part of the same set and for the same reason: it decides whether
    an administrator's Start and Stop can reach a producer at all, so a producer
    that never receives it stays on the legacy environment switch no matter what
    the operator sets. The sandbox manager needs it as much as the runner does -- it is a
    different container, and a selection it is handed is only honoured while its
    own mode says a control plane may decide.
    """
    names = {
        "AGENT_OPERATION_PROFILING_ENABLED",
        "AGENT_OPERATION_PROFILING_RUN",
        "AGENT_OPERATION_PROFILING_SINK",
        "AGENT_OPERATION_PROFILING_MODE",
    }
    control = source("control-plane")["services"]["control-plane"]["environment"]
    host = source("host")["services"]

    assert names <= set(control)
    assert names <= set(host["runner"]["environment"])
    assert names <= set(host["sandbox-manager"]["environment"])


def test_host_stacks_share_receipts_without_the_credential_root():
    cases = (
        (
            rendered("host", None)["services"],
            "${AGENT_HOST_STATE_ROOT:-/var/lib/hybridinference/agent-host}/upload-receipts",
            "/var/lib/hybridinference/upload-receipts",
            "${AGENT_HOST_STATE_ROOT:-/var/lib/hybridinference/agent-host}:"
            "${AGENT_HOST_STATE_ROOT:-/var/lib/hybridinference/agent-host}",
        ),
        (
            yaml.safe_load(LOCAL_SOURCE.read_text())["services"],
            "local-upload-receipts",
            "/var/lib/freeinference/upload-receipts",
            "local-host-state:/var/lib/freeinference/agent-host",
        ),
    )
    for services, source_path, target_path, credential_mount in cases:
        receipt_mount = f"{source_path}:{target_path}"
        for name in ("runner", "sandbox-manager"):
            assert services[name]["environment"]["AGENT_UPLOAD_RECEIPT_DIR"] == target_path
            assert receipt_mount in services[name]["volumes"]
        assert {
            name
            for name, service in services.items()
            if receipt_mount in service.get("volumes", [])
        } == {"runner", "sandbox-manager"}
        assert credential_mount not in services["sandbox-manager"]["volumes"]


@pytest.mark.parametrize(
    ("mutate", "stack", "variant"),
    [
        pytest.param(
            lambda m: m["artifacts"].remove(
                next(a for a in m["artifacts"] if a["role"] == "control")
            ),
            "control-plane",
            "root",
            id="a role this stack needs is missing",
        ),
        pytest.param(
            lambda m: m["artifacts"].remove(
                next(a for a in m["artifacts"] if a["role"] == "tunnel")
            ),
            "host",
            None,
            id="a role the host stack needs is missing",
        ),
        pytest.param(
            lambda m: m["artifacts"][0].update(image="ghcr.io/harvardmadsys/cloud-agent:latest"),
            "control-plane",
            "root",
            id="a tag instead of a digest",
        ),
        pytest.param(
            lambda m: m["artifacts"].append(dict(m["artifacts"][0])),
            "control-plane",
            "root",
            id="a role appears twice",
        ),
    ],
)
def test_a_manifest_that_cannot_describe_a_deployment_is_refused(
    mutate, stack: str, variant: str | None
) -> None:
    """A role the stack runs is not optional, and a tag is not a digest.

    The tunnel case renders the host stack on purpose: a missing role only
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
            "root",
            "--output",
            str(output),
        ]
    )
    assert code == 0
    written = yaml.safe_load(output.read_text())
    assert "build" not in yaml.safe_dump(written)
    assert written["services"]["web"]["image"] == reference("web", "root")


def test_the_cli_writes_nothing_when_it_refuses(tmp_path: Path) -> None:
    broken = manifest()
    broken["artifacts"] = [a for a in broken["artifacts"] if a["role"] != "tunnel"]
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
