"""An artifact deployment's values come from its release, not from its host.

The interesting half is the refusals: a host `.env` that names a different image
or a different commit would let the machine being deployed choose what runs on
it, and the exact-SHA fence would then compare a value the deployment wrote
against a value the deployment wrote. Everything emitted is validated as shell
input as well, because the manifest is a document that came from somewhere else.
"""

from __future__ import annotations

import hashlib
import json
from typing import TYPE_CHECKING, Any

import pytest

if TYPE_CHECKING:  # pragma: no cover - annotations only
    from pathlib import Path

from deploy.release import release_env as re_

COMMIT = "cba9a47" + "0" * 33


def digest(seed: str) -> str:
    return "sha256:" + hashlib.sha256(seed.encode()).hexdigest()


def image(role: str, variant: str | None = None) -> str:
    return f"ghcr.io/harvardmadsys/cloud-agent@{digest(f'{role}:{variant}')}"


def manifest() -> dict[str, Any]:
    return {
        "source_commit": COMMIT,
        "artifacts": [
            {"role": "control", "image": image("control")},
            {"role": "web", "variant": "root", "base_path": "", "image": image("web", "root")},
            {
                "role": "web",
                "variant": "agents",
                "base_path": "/agents",
                "image": image("web", "agents"),
            },
            {"role": "runner", "image": image("runner")},
            {"role": "sandbox", "image": image("sandbox")},
            {"role": "tunnel", "image": image("tunnel")},
            {"role": "egress", "image": image("egress")},
        ],
    }


def test_the_control_stack_takes_images_and_the_commit_from_the_release() -> None:
    environment = re_.environment_for(manifest(), "control-plane", "agents", ambient={})
    assert environment["AGENT_CONTROL_IMAGE"] == image("control")
    assert environment["AGENT_WEB_IMAGE"] == image("web", "agents")
    assert environment["AGENT_DEPLOYMENT_SHA"] == COMMIT
    assert environment["AGENT_WEB_VARIANT"] == "agents"
    assert environment["AGENT_WEB_BASE_PATH"] == '"/agents"'


def test_the_host_stack_takes_the_sandbox_image_too() -> None:
    environment = re_.environment_for(manifest(), "host", ambient={})
    assert environment["AGENT_RUNNER_IMAGE"] == image("runner")
    assert environment["AGENT_TUNNEL_IMAGE"] == image("tunnel")
    assert environment["AGENT_EGRESS_IMAGE"] == image("egress")
    assert environment["AGENT_SANDBOX_IMAGE"] == image("sandbox")
    assert "AGENT_WEB_IMAGE" not in environment, "a host stack runs no console"


def test_the_variant_decides_which_console_image_is_deployed() -> None:
    root = re_.environment_for(manifest(), "control-plane", "root", ambient={})
    assert root["AGENT_WEB_IMAGE"] == image("web", "root")
    assert root["AGENT_WEB_BASE_PATH"] == '""'


@pytest.mark.parametrize(
    ("key", "value"),
    [
        ("AGENT_CONTROL_IMAGE", "ghcr.io/harvardmadsys/cloud-agent@sha256:" + "a" * 64),
        ("AGENT_DEPLOYMENT_SHA", "0" * 40),
        ("AGENT_WEB_BASE_PATH", "/somewhere-else"),
    ],
)
def test_a_host_that_disagrees_with_the_release_is_refused(key: str, value: str) -> None:
    """The refusal names the variable and what the release wanted.

    An operator who set one of these meant something by it, and a deploy that
    silently won is a deployment on a different release than the one that was
    reviewed.
    """
    with pytest.raises(re_.ReleaseEnvError) as raised:
        re_.environment_for(manifest(), "control-plane", "agents", ambient={key: value})
    assert key in str(raised.value)


def test_an_agreeing_host_environment_is_fine() -> None:
    """Idempotence matters: a re-run sources the file it wrote last time."""
    first = re_.environment_for(manifest(), "control-plane", "agents", ambient={})
    ambient = {k: v.strip('"') for k, v in first.items()}
    assert re_.environment_for(manifest(), "control-plane", "agents", ambient=ambient) == first


@pytest.mark.parametrize(
    ("mutate", "stack", "variant"),
    [
        pytest.param(
            lambda m: m["artifacts"].remove(
                next(a for a in m["artifacts"] if a["role"] == "sandbox")
            ),
            "host",
            None,
            id="a role the stack runs is missing",
        ),
        pytest.param(
            lambda m: m["artifacts"][0].update(image="ghcr.io/harvardmadsys/cloud-agent:latest"),
            "control-plane",
            "agents",
            id="a tag instead of a digest",
        ),
        pytest.param(
            lambda m: m.update(source_commit="cba9a47"),
            "control-plane",
            "agents",
            id="a short commit",
        ),
        pytest.param(
            lambda m: m["artifacts"].append(dict(m["artifacts"][0])),
            "control-plane",
            "agents",
            id="a role twice",
        ),
        pytest.param(
            lambda m: m["artifacts"][2].update(base_path="/elsewhere"),
            "control-plane",
            "agents",
            id="a base path no deployment serves",
        ),
    ],
)
def test_a_manifest_that_cannot_configure_a_deployment_is_refused(
    mutate, stack: str, variant: str | None
) -> None:
    document = manifest()
    mutate(document)
    with pytest.raises(re_.ReleaseEnvError):
        re_.environment_for(document, stack, variant, ambient={})


def test_the_fragment_is_shell_and_carries_nothing_but_these_values() -> None:
    fragment = re_.render(re_.environment_for(manifest(), "host", ambient={}))
    for line in fragment.strip().splitlines():
        key, _, value = line.partition("=")
        assert key in {*re_.STACK_ROLES["host"].values(), "AGENT_DEPLOYMENT_SHA"}
        assert value == value.strip(), "no stray whitespace for the shell to keep"
    assert fragment.endswith("\n")


def test_the_cli_writes_a_file_and_refuses_a_bad_manifest(tmp_path: Path) -> None:
    manifest_path = tmp_path / "release.json"
    manifest_path.write_text(json.dumps(manifest()))
    output = tmp_path / "release.env"
    assert (
        re_.main(
            [
                "--manifest",
                str(manifest_path),
                "--stack",
                "control-plane",
                "--variant",
                "agents",
                "--output",
                str(output),
            ]
        )
        == 0
    )
    assert "AGENT_WEB_IMAGE=" in output.read_text()

    broken = manifest()
    broken["artifacts"] = [a for a in broken["artifacts"] if a["role"] != "control"]
    manifest_path.write_text(json.dumps(broken))
    missing = tmp_path / "missing.env"
    assert (
        re_.main(
            [
                "--manifest",
                str(manifest_path),
                "--stack",
                "control-plane",
                "--variant",
                "agents",
                "--output",
                str(missing),
            ]
        )
        == 1
    )
    assert not missing.exists()
