"""Image selection covers changed inputs while keeping unrelated builds out."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from typing import TYPE_CHECKING

import pytest
from deploy.release import build_host_bundle
from scripts.ci_image_matrix import image_matrix

if TYPE_CHECKING:
    from pathlib import Path
from tests.unit.deploy.test_artifacts_workflow import (
    REPO_ROOT,
    _classify_paths,
    _copied_paths,
    cells,
    step,
    workflow,
)


@pytest.mark.parametrize(
    ("paths", "scope", "names"),
    [
        (["apps/frontend/src/app/page.tsx"], "web", {"web-root", "web-agents"}),
        (
            ["apps/frontend/package-lock.json", "docs/feature.md", "tests/unit/test_ui.py"],
            "web",
            {"web-root", "web-agents"},
        ),
        (["apps/backend/cloud_agent/api/jobs.py"], "backend", {"control", "runner", "sandbox"}),
        (["apps/backend/cloud_agent_host/runner.py"], "backend", {"control", "runner", "sandbox"}),
        (["migrations/versions/next.py"], "backend", {"control", "runner", "sandbox"}),
        (["alembic.ini"], "backend", {"control", "runner", "sandbox"}),
    ],
)
def test_source_only_changes_select_their_images(
    paths: list[str], scope: str, names: set[str]
) -> None:
    selected = _classify_paths(paths)
    assert selected["docker"] == "true"
    assert selected["image_scope"] == scope
    assert {cell["name"] for cell in image_matrix(scope)} == names


@pytest.mark.parametrize(
    "extra",
    [
        "apps/backend/cloud_agent/app.py",
        "deploy/control-plane/Dockerfile.web",
        "deploy/release/smoke.py",
        "deploy/sandbox/install-runtimes.sh",
        "tests/external/test_new_container_integration.py",
        "pyproject.toml",
        "uv.lock",
        "README.md",
        "LICENSE",
        ".dockerignore",
        ".github/workflows/ci.yml",
        "scripts/ci_image_matrix.py",
    ],
)
def test_shared_inputs_and_mixed_changes_keep_the_complete_catalog(extra: str) -> None:
    selected = _classify_paths(["apps/frontend/src/app/page.tsx", extra])
    assert selected["docker"] == "true"
    assert selected["image_scope"] == "all"
    assert image_matrix(selected["image_scope"]) == cells()


def _image_inputs() -> list[tuple[str, str]]:
    inputs = []
    for cell in cells():
        sources = _copied_paths(REPO_ROOT / cell["dockerfile"]) | {cell["dockerfile"]}
        if cell["name"] == "control":
            sources.update(build_host_bundle.PACKAGE_ROOTS)
            sources.update(build_host_bundle.HOST_FILES)
        for source in sorted(sources):
            probe = f"{source}/module.py" if (REPO_ROOT / source).is_dir() else source
            inputs.append((cell["name"], probe))
    return inputs


@pytest.mark.parametrize(("name", "path"), _image_inputs())
def test_each_copied_or_embedded_input_selects_the_image_that_ships_it(
    name: str, path: str
) -> None:
    selected = _classify_paths([path])
    assert selected["docker"] == "true"
    assert name in {cell["name"] for cell in image_matrix(selected["image_scope"])}


@pytest.mark.parametrize("scope", ["all", "", "unknown"])
def test_unresolved_scopes_fail_open_to_all_images(scope: str) -> None:
    assert image_matrix(scope) == cells()


@pytest.mark.parametrize("scope", ["all", "web", "backend"])
def test_the_workflow_emits_the_matrix_its_image_job_consumes(tmp_path: Path, scope: str) -> None:
    job = workflow()["jobs"]["changes"]
    assert job["outputs"]["artifacts"] == "${{ steps.images.outputs.artifacts }}"
    planner = step("Plan image checks", job="changes")
    assert planner["env"]["IMAGE_SCOPE"] == "${{ steps.classify.outputs.image_scope }}"
    output = tmp_path / "outputs"
    script = planner["run"].replace("python3", sys.executable, 1)
    subprocess.run(
        ["bash", "-c", script],
        cwd=REPO_ROOT,
        env={**os.environ, "IMAGE_SCOPE": scope, "GITHUB_OUTPUT": str(output)},
        check=True,
    )
    key, value = output.read_text().strip().split("=", 1)
    assert key == "artifacts"
    assert json.loads(value) == image_matrix(scope)
