"""Every image carries what the manifest will claim about it.

A release manifest names a digest and a role for each artifact. The registry
knows nothing about roles, so the only place a role can be checked is inside the
image, and the only place a revision can be checked without trusting the
manifest is inside the image as well. Both are labels, so this file asserts that
they are declared, that they are declared in the stage that survives, and that
the Dockerfiles together cover exactly the roles a release must carry.

The failure this prevents is quiet in both directions: an image with no revision
label cannot be verified at all, and a label written in a builder stage is
discarded with that stage — the build succeeds, the image is published, and the
first sign of trouble is a resolver refusing a release nobody can explain.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
from deploy.release import release_manifest as rm

REPO_ROOT = Path(__file__).resolve().parents[3]

#: Dockerfile, and the artifact it builds. Web appears once: its two variants
#: are one Dockerfile built twice, with different base paths.
IMAGES = {
    "deploy/control-plane/Dockerfile": "control",
    "deploy/control-plane/Dockerfile.web": "web",
    "deploy/host/Dockerfile.agent-runner": "runner",
    "deploy/sandbox/Dockerfile.agent-sandbox": "sandbox",
    "deploy/host/Dockerfile.agent-gateway-tunnel": "tunnel",
}

SOURCE_URL = "https://github.com/HarvardMadSys/hybridInference-cloud-agent"
CASES = sorted(IMAGES.items())


def final_stage(dockerfile: str) -> str:
    """Everything after the last `FROM`: the only stage whose labels survive."""
    text = (REPO_ROOT / dockerfile).read_text()
    stages = re.split(r"(?m)^FROM ", text)
    assert len(stages) > 1, f"{dockerfile} declares no FROM"
    return stages[-1]


@pytest.mark.parametrize(("dockerfile", "role"), CASES)
def test_the_final_stage_says_what_the_image_is(dockerfile: str, role: str) -> None:
    stage = final_stage(dockerfile)
    assert f'io.hybridinference.artifact.role="{role}"' in stage
    assert f'org.opencontainers.image.source="{SOURCE_URL}"' in stage
    assert 'org.opencontainers.image.revision="${SOURCE_COMMIT}"' in stage


@pytest.mark.parametrize(("dockerfile", "role"), CASES)
def test_the_revision_can_actually_reach_the_label(dockerfile: str, role: str) -> None:
    # An ARG is per stage: one declared only in a builder is no more available
    # to the runtime stage than one that was never declared at all.
    stage = final_stage(dockerfile)
    assert "ARG SOURCE_COMMIT=" in stage, (
        f"{dockerfile} labels a revision it cannot be given: `ARG SOURCE_COMMIT` must be "
        "declared in the stage that writes the label"
    )
    assert "ARG SOURCE_COMMIT=unknown" in stage, (
        f"{dockerfile} should default the revision to `unknown`, so an image built by hand "
        "says so instead of carrying an empty label that reads as verified"
    )


def test_the_dockerfiles_cover_exactly_the_roles_a_release_carries() -> None:
    assert sorted(IMAGES.values()) == sorted(rm.EXPECTED_PLATFORMS), (
        "every role in a release is built by one Dockerfile; a role with no Dockerfile "
        "cannot appear in a manifest, and a Dockerfile with no role is an artifact nothing "
        "deploys"
    )


def test_the_web_variant_is_labelled_from_its_base_path() -> None:
    stage = final_stage("deploy/control-plane/Dockerfile.web")
    assert "ARG NEXT_PUBLIC_BASE_PATH=" in stage
    assert 'io.hybridinference.artifact.base_path="${NEXT_PUBLIC_BASE_PATH}"' in stage, (
        "the variant must come from the same build argument the bundle is compiled with, "
        "not from a second argument that can disagree with it"
    )


def test_the_sandbox_keeps_the_runtime_labels_the_manifest_reads() -> None:
    """The catalog's source, which is deliberately not renamed yet.

    `org.freeinference.runtime.*` is a persisted compatibility identifier: the
    runner stamps attempts from it and retained sessions carry it. Moving it to
    a neutral namespace is a dual-read migration of its own, so a release
    describes the labels that exist today.
    """
    stage = final_stage("deploy/sandbox/Dockerfile.agent-sandbox")
    for runtime in ("claude-code", "codex", "pi", "opencode", "kilo"):
        assert f"{rm.RUNTIME_LABEL_PREFIX}{runtime}{rm.RUNTIME_LABEL_SUFFIX}=" in stage
        assert f"{rm.RUNTIME_LABEL_PREFIX}{runtime}{rm.RUNTIME_MENU_LABEL_SUFFIX}=" in stage
