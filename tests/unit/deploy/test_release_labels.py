"""Final images declare the repository whose source they carry.

Actual image smoke validates role, revision, and web mount labels. The source
label is not part of that runtime check, so its final-stage declaration remains
covered here.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]

#: Each final-stage source label is checked once per Dockerfile.
DOCKERFILES = (
    "deploy/control-plane/Dockerfile",
    "deploy/control-plane/Dockerfile.web",
    "deploy/host/Dockerfile.agent-runner",
    "deploy/sandbox/Dockerfile.agent-sandbox",
    "deploy/host/Dockerfile.agent-gateway-tunnel",
)

SOURCE_URL = "https://github.com/HarvardMadSys/hybridInference-cloud-agent"


def final_stage(dockerfile: str) -> str:
    """Everything after the last `FROM`: the only stage whose labels survive."""
    text = (REPO_ROOT / dockerfile).read_text()
    stages = re.split(r"(?m)^FROM ", text)
    assert len(stages) > 1, f"{dockerfile} declares no FROM"
    return stages[-1]


@pytest.mark.parametrize("dockerfile", sorted(DOCKERFILES))
def test_the_final_stage_names_its_source_repository(dockerfile: str) -> None:
    stage = final_stage(dockerfile)
    assert f'org.opencontainers.image.source="{SOURCE_URL}"' in stage
