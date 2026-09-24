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
import subprocess
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
        ],
    }


def full_manifest() -> dict[str, Any]:
    """A document the published schema accepts, which the CLI now requires.

    The mapping below reads four fields; a manifest has ten, and the ones it does
    not read are what a deployment is entitled to assume. Review asked for the
    full check at the entry point, so the CLI tests use a document that would
    pass one.
    """
    platforms = {"control": "linux/arm64", "web": "linux/arm64"}
    artifacts = [
        {"role": "control", "image": image("control"), "platforms": ["linux/arm64"]},
        {
            "role": "web",
            "variant": "root",
            "base_path": "",
            "image": image("web", "root"),
            "platforms": ["linux/arm64"],
        },
        {
            "role": "web",
            "variant": "agents",
            "base_path": "/agents",
            "image": image("web", "agents"),
            "platforms": ["linux/arm64"],
        },
    ]
    for role in ("runner", "sandbox", "tunnel"):
        artifacts.append({"role": role, "image": image(role), "platforms": ["linux/amd64"]})
    del platforms
    return {
        "schema_version": 1,
        "source_repository": "HarvardMadSys/hybridInference-cloud-agent",
        "source_commit": COMMIT,
        "deploy_protocol": "shared-live-exact-sha-v4",
        "artifacts": artifacts,
        "runtime_catalog": {"codex": {"default": "0.145.0", "versions": ["0.145.0"]}},
        "recipe": {
            "url": "https://github.com/HarvardMadSys/hybridInference-cloud-agent/releases/download/x/r.tgz",
            "sha256": "a" * 64,
            "format_version": 1,
        },
        "database_revision": "0004_residency_on_membership",
        "compatibility": {
            "minimum_deployer": "shared-live-exact-sha-v4",
            "verified_upgrade_from": [],
            "verified_rollback_to": [],
        },
        "publisher_run_url": (
            "https://github.com/HarvardMadSys/hybridInference-cloud-agent/actions/runs/1"
        ),
    }


def test_a_partial_document_is_not_a_manifest(tmp_path: Path) -> None:
    """Four fields are enough to map and not enough to deploy.

    Caught at the entry point rather than at the mapping, because the mapping
    cannot tell "no recipe" from "no recipe field it reads".
    """
    path = tmp_path / "partial.json"
    path.write_text(json.dumps({"source_commit": COMMIT, "artifacts": manifest()["artifacts"]}))
    output = tmp_path / "release.env"
    assert (
        re_.main(
            ["--manifest", str(path), "--stack", "host", "--output", str(output)],
        )
        == 1
    )
    assert not output.exists()


def test_the_fragment_reaches_a_child_process_and_a_host_conflict_is_refused(
    tmp_path: Path,
) -> None:
    """The handoff, executed: Compose's resolved values in, bash in between, environment out.

    Review reproduced the gap this closes — a host that named a different SHA was
    invisible to a helper reading `os.environ`, the fragment was sourced over the
    top of it, and the values never reached a child process at all. A unit test
    that passes an ambient dictionary cannot see any of that, so this one runs
    the real thing.
    """
    manifest_path = tmp_path / "release.json"
    manifest_path.write_text(json.dumps(full_manifest()))
    fragment = tmp_path / "release.env"

    refused = re_.main(
        [
            "--manifest",
            str(manifest_path),
            "--stack",
            "host",
            "--ambient",
            f"AGENT_DEPLOYMENT_SHA={'b' * 40}",
            "--output",
            str(fragment),
        ]
    )
    assert refused == 1, "a host that names another release must not be deployed over"
    assert not fragment.exists()

    assert (
        re_.main(
            [
                "--manifest",
                str(manifest_path),
                "--stack",
                "host",
                "--ambient",
                "AGENT_HOST_SLOTS=3",
                "--output",
                str(fragment),
            ]
        )
        == 0
    )

    child = 'import os; print(os.environ["AGENT_DEPLOYMENT_SHA"])'
    seen = subprocess.run(
        ["bash", "-c", f"set -euo pipefail; source {fragment}; python3 -c '{child}'"],
        capture_output=True,
        text=True,
        check=True,
    )
    assert seen.stdout.strip() == COMMIT, (
        "the fragment has to be exported, or the readiness probe and Compose — both children "
        "— see nothing"
    )


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
    assert environment["AGENT_SANDBOX_IMAGE"] == image("sandbox")
    # One sandbox network: there is no proxy image left to deploy.
    assert "AGENT_EGRESS_IMAGE" not in environment
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


def test_the_resolved_value_answers_the_check_not_the_ambient_one(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Compose's precedence, or its opposite — the difference is a silent deploy.

    A shell variable beats a `.env` entry in Compose, so the resolved value is
    what the deployment will use. Comparing against anything else either misses a
    host that named another image (the file said the right thing, the environment
    said otherwise) or refuses a host that is fine (the file disagreed, the
    environment matched).
    """
    monkeypatch.setenv("AGENT_DEPLOYMENT_SHA", "b" * 40)
    # Compose resolved this to the release's commit, because the environment wins.
    assert (
        re_.environment_for(
            manifest(),
            "control-plane",
            "agents",
            ambient={"AGENT_DEPLOYMENT_SHA": COMMIT},
        )["AGENT_DEPLOYMENT_SHA"]
        == COMMIT
    )

    # And when Compose resolved it to something else, that is what is refused.
    with pytest.raises(re_.ReleaseEnvError):
        re_.environment_for(
            manifest(),
            "control-plane",
            "agents",
            ambient={"AGENT_DEPLOYMENT_SHA": "b" * 40},
        )


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
            lambda m: m.update(source_commit=COMMIT + "\n"),
            "control-plane",
            "agents",
            id="a commit with a trailing newline",
        ),
        pytest.param(
            lambda m: m["artifacts"][0].update(image=image("control") + "\n"),
            "control-plane",
            "agents",
            id="an image with a trailing newline",
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
        prefix, _, line = line.partition(" ")
        assert prefix == "export", "a plain assignment never reaches a child process"
        key, _, value = line.partition("=")
        assert key in {*re_.STACK_ROLES["host"].values(), "AGENT_DEPLOYMENT_SHA"}
        assert value == value.strip(), "no stray whitespace for the shell to keep"
    assert fragment.endswith("\n")


def test_a_variant_and_a_base_path_must_agree() -> None:
    """Both halves valid, the pair wrong — and the deployment probes the wrong mount.

    Caught here because this is the code that acts on it, and stated in the
    manifest schema as well. A helper that checked each half on its own accepted
    `agents` with an empty base path.
    """
    document = manifest()
    agents = next(a for a in document["artifacts"] if a.get("variant") == "agents")
    agents["base_path"] = ""
    with pytest.raises(re_.ReleaseEnvError) as raised:
        re_.environment_for(document, "control-plane", "agents", ambient={})
    assert "base path" in str(raised.value)


def test_the_cli_writes_a_file_and_refuses_a_bad_manifest(tmp_path: Path) -> None:
    manifest_path = tmp_path / "release.json"
    manifest_path.write_text(json.dumps(full_manifest()))
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

    broken = full_manifest()
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
