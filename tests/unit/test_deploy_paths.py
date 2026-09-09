"""The deploy files must reference paths that exist in *this* repository.

D7 is the one migration task whose diff is mostly path edits rather than
imports, and nothing else checks them: a Dockerfile that `COPY`s a path from
the source repo's layout fails at build time on a runner host, long after
review. These tests read the files and assert every path they name is real.

They deliberately do not re-test *behaviour* the source repo already covers
(egress network internal, socket read-only, non-root sandbox). Those
assertions live in `test_agent_deploy_config.py`, which is still in the source
repo because it asserts on the co-located overlay compose — see MIGRATION.md's
open-decisions register.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest
import yaml

REPO = Path(__file__).resolve().parents[2]
HOST = REPO / "deploy/host"
SANDBOX = REPO / "deploy/sandbox"
CONTROL_PLANE = REPO / "deploy/control-plane"

#: `deploy/control-plane/` was outside this guard until its `.env.example`
#: arrived — so the control plane's compose and Dockerfiles were the one part
#: of the deployment whose paths nothing checked.
DEPLOY_FILES = sorted(
    p
    for p in (*HOST.iterdir(), *SANDBOX.iterdir(), *CONTROL_PLANE.iterdir())
    if p.is_file() and p.name != ".gitkeep"
)

# `COPY <src> <dst>` / `COPY --from=x <src> <dst>` — src is repo-relative.
_COPY = re.compile(r"^COPY(?:\s+--\S+)*\s+(\S+)\s+\S+\s*$", re.MULTILINE)
# Top-level directories of THIS repository. A reference to one of these must
# resolve. The list is the actual tree, not a sample: an earlier version checked
# four prefixes while claiming to verify every path, and missed real breakage in
# `distributions/` and `docs/` references precisely because they were not on it.
_OWN_DIRS = (
    "cloud_agent",
    "cloud_agent_host",
    "contracts",
    "deploy",
    "docs",
    "migrations",
    "tests",
    "web",
)

# Top-level directories that exist only in the SOURCE repository — plus
# `backend` and `host`, the one-package src roots this tree carried until the
# flatten moved both packages to the top level. Any occurrence is stale by
# construction — there is nothing here for it to resolve to.
_SOURCE_ONLY_DIRS = ("apps", "ops", "config", "distributions", "services", "backend", "host")

# Trailing dots are excluded: these paths are frequently named mid-prose in
# comments, and a sentence-ending period is not part of the filename.
# Strings that match the shape above but are not references to this tree.
# Exempt by literal value with a reason, never by directory — the same
# discipline test_no_committed_credentials.py uses, and for the same reason: a
# directory-wide exemption stops being read after the day it is added.
_NOT_OUR_PATHS = {
    # The pi CLI's own documentation, quoted in the wrapper's explanation of
    # why that CLI needs a config file rather than OPENAI_BASE_URL.
    "docs/models.md",
    # Task F1 adds the Next.js app. web-ci.yml names these deliberately and
    # guards on their absence, so referencing them before they exist is the
    # intended behaviour, not a stale path.
    "web/package.json",
    "web/package-lock.json",
    # The files an operator creates by copying the examples beside them. They
    # cannot exist here — `.gitignore` forbids committing either, which is the
    # point — and the `cp` line naming both is the instruction, not a stale
    # reference.
    "deploy/host/.env",
    "deploy/control-plane/.env",
}

_PATH_TAIL = r"/[A-Za-z0-9._/*-]*[A-Za-z0-9_/*-]"
_OWN_PATH = re.compile(rf"(?<![A-Za-z0-9._/-])(?:{'|'.join(_OWN_DIRS)}){_PATH_TAIL}")
_SOURCE_ONLY_PATH = re.compile(
    rf"(?<![A-Za-z0-9._/-])(?:{'|'.join(_SOURCE_ONLY_DIRS)}){_PATH_TAIL}"
)


@pytest.mark.parametrize("path", DEPLOY_FILES, ids=lambda p: str(p.relative_to(REPO)))
def test_no_deploy_file_references_the_source_repo_layout(path: Path) -> None:
    """No `apps/backend`, `serving.`, `ops/`, or `deploy/docker` survives."""
    text = path.read_text(encoding="utf-8")
    stale = [
        marker
        for marker in ("apps/backend", "serving.agent_jobs", "deploy/docker/", "ops/deploy/")
        if marker in text
    ]
    assert not stale, f"{path.relative_to(REPO)} still points at the source repo: {stale}"


@pytest.mark.parametrize(
    "dockerfile",
    sorted(p for p in DEPLOY_FILES if p.name.startswith("Dockerfile")),
    ids=lambda p: p.name,
)
def test_every_dockerfile_copy_source_exists(dockerfile: Path) -> None:
    """A COPY of a path that moved is a build failure on a runner host."""
    missing = [
        src
        for src in _COPY.findall(dockerfile.read_text(encoding="utf-8"))
        # Build-stage copies (`--from=builder /app/...`) are absolute and not ours.
        if not src.startswith("/") and not (REPO / src).exists()
    ]
    assert not missing, f"{dockerfile.name} copies paths that do not exist: {missing}"


def test_the_runner_image_runs_the_host_package() -> None:
    text = (HOST / "Dockerfile.agent-runner").read_text(encoding="utf-8")
    assert "COPY cloud_agent_host/ cloud_agent_host/" in text
    assert '"cloud_agent_host.runner"' in text


def test_the_runner_entrypoint_does_not_import_the_control_plane_package() -> None:
    """The slim runner image carries only the host package at runtime."""
    source = (REPO / "cloud_agent_host/runner.py").read_text(encoding="utf-8")
    imports = {
        node.module
        for node in ast.walk(ast.parse(source))
        if isinstance(node, ast.ImportFrom) and node.module is not None
    }
    assert not {
        module for module in imports if module == "cloud_agent" or module.startswith("cloud_agent.")
    }


def test_the_remote_compose_builds_from_files_that_exist() -> None:
    """Every build context/dockerfile pair in the compose must resolve."""
    compose = yaml.safe_load((HOST / "docker-compose.sandbox.yml").read_text())
    checked = 0
    for name, service in compose["services"].items():
        build = service.get("build")
        if not isinstance(build, dict):
            continue
        # Contexts are relative to the compose file's own directory.
        resolved = (HOST / build["context"] / build["dockerfile"]).resolve()
        assert resolved.exists(), f"service {name}: {build['dockerfile']} does not exist"
        checked += 1
    assert checked, "compose declares no builds — the path rewrite was not exercised"


def test_the_remote_compose_invokes_host_modules() -> None:
    """The commands must name the moved package, not the gateway's."""
    text = (HOST / "docker-compose.sandbox.yml").read_text(encoding="utf-8")
    assert "cloud_agent_host.workspace_broker" in text
    assert "cloud_agent_host.runner" in text


def _text_files() -> list[Path]:
    """Deploy files plus the workflows — everything D7 rewrote paths in."""
    return [*DEPLOY_FILES, *sorted((REPO / ".github/workflows").glob("*.yml"))]


@pytest.mark.parametrize("path", _text_files(), ids=lambda p: str(p.relative_to(REPO)))
def test_every_own_path_reference_resolves(path: Path) -> None:
    """A path naming this repo's own tree must exist — including in prose.

    Error strings and comments are checked deliberately: an operator following
    a message that names a file we do not ship is worse than no message, and
    that is exactly how a stale `docs/developer/...` reference survived review.
    """
    missing = sorted(
        {
            candidate
            for candidate in _OWN_PATH.findall(path.read_text(encoding="utf-8"))
            # A glob is a runtime match, not a file we can stat.
            if "*" not in candidate
            and candidate not in _NOT_OUR_PATHS
            and not (REPO / candidate).exists()
        }
    )
    assert not missing, f"{path.relative_to(REPO)} names paths that do not exist: {missing}"


@pytest.mark.parametrize("path", _text_files(), ids=lambda p: str(p.relative_to(REPO)))
def test_no_file_references_a_source_repo_only_directory(path: Path) -> None:
    """`apps/`, `ops/`, `distributions/`, … do not exist here at all.

    Separate from the check above because these can never resolve: they are
    concepts of the repository we migrated *from*. A reference to one means a
    rewrite was missed, and the failure is silent — a compose `--env-file` glob
    that matches nothing simply contributes no configuration.
    """
    stale = sorted(set(_SOURCE_ONLY_PATH.findall(path.read_text(encoding="utf-8"))))
    assert not stale, f"{path.relative_to(REPO)} references the source repo layout: {stale}"


def test_the_legacy_actions_runner_workflow_is_removed() -> None:
    """Manual dispatch must not reopen the removed one-shot/process backend."""
    assert not (REPO / ".github/workflows/agent-job-runner.yml").exists()


@pytest.mark.parametrize(
    ("workflow", "expected"),
    [
        (".github/workflows/deploy-staging.yml", "staging"),
        (".github/workflows/deploy-prod.yml", "production"),
    ],
)
def test_every_deploy_job_records_which_environment_it_deployed(workflow: str, expected: str):
    """Both halves of a deploy must name their environment, or it is invisible.

    A job without `environment:` deploys without leaving a record, so the
    repository's Deployments panel shows nothing for it. Staging shipped that
    way: the only trace of a deploy was in the Actions tab, filed under the
    default branch because `workflow_run` runs are — so filtering the run list
    by `dev` showed nothing at all and read as "it never deployed".

    Parameterized over both workflows because the failure is asymmetry: one
    half declared and the other not is exactly the state this replaces, and it
    is invisible until somebody goes looking for a deployment that happened.
    """
    spec = yaml.safe_load((REPO / workflow).read_text(encoding="utf-8"))
    environments = {name: job.get("environment") for name, job in spec["jobs"].items()}

    assert environments, f"{workflow} declares no jobs"
    assert all(value == expected for value in environments.values()), (
        f"{workflow} has jobs that deploy without recording it: {environments}"
    )
