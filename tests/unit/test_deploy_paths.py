"""Active deployment paths resolve in this repository.

Dockerfile COPY sources, Compose build files, invoked host modules, and current
path references are checked here. Deployment behavior and security boundaries
are covered by test_deploy_config.py and test_deploy_security.py.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
import yaml

REPO = Path(__file__).resolve().parents[2]
HOST = REPO / "deploy/host"
SANDBOX = REPO / "deploy/sandbox"
CONTROL_PLANE = REPO / "deploy/control-plane"

#: Current deployment files whose repository-relative paths must resolve.
DEPLOY_FILES = sorted(
    p for p in (*HOST.iterdir(), *SANDBOX.iterdir(), *CONTROL_PLANE.iterdir()) if p.is_file()
)

# `COPY <src> <dst>` / `COPY --from=x <src> <dst>` — src is repo-relative.
_COPY = re.compile(r"^COPY(?:\s+--\S+)*\s+(\S+)\s+\S+\s*$", re.MULTILINE)
# Repository directories whose references must resolve, including documentation.
_OWN_DIRS = (
    "apps",
    "deploy",
    "docs",
    "gateway_contracts",
    "migrations",
    "tests",
)

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
    # The files an operator creates by copying the examples beside them. They
    # cannot exist here — `.gitignore` forbids committing either, which is the
    # point — and the `cp` line naming both is the instruction, not a stale
    # reference.
    "deploy/host/.env",
    "deploy/control-plane/.env",
}

_PATH_TAIL = r"/[A-Za-z0-9._/*-]*[A-Za-z0-9_/*-]"
_OWN_PATH = re.compile(rf"(?<![A-Za-z0-9._/-])(?:{'|'.join(_OWN_DIRS)}){_PATH_TAIL}")


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
    assert "COPY apps/backend/cloud_agent_host/ cloud_agent_host/" in text
    assert '"cloud_agent_host.runner"' in text


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
    """Current deployment files and workflows that reference repository paths."""
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
