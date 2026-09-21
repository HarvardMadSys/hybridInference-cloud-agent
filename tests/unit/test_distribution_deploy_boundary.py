"""The upstream publishes releases but owns no FreeInference deployment."""

from __future__ import annotations

from pathlib import Path

REPO = Path(__file__).resolve().parents[2]

RETIRED_WORKFLOWS = (
    REPO / ".github/workflows/deploy-staging.yml",
    REPO / ".github/workflows/deploy-prod.yml",
)
RETIRED_CONFIGURATION = (
    "LEGACY_DIRECT_STAGING_DEPLOY",
    "LEGACY_DIRECT_PRODUCTION_DEPLOY",
    "deploy-cloud-agent-control-staging",
    "deploy-cloud-agent-sandbox-staging",
    "deploy-cloud-agent-control-prod",
    "deploy-cloud-agent-sandbox-prod",
    "STAGING_ALERT_WEBHOOK_URL",
)
TEXT_SUFFIXES = {".conf", ".env", ".ini", ".json", ".md", ".py", ".sh", ".toml", ".yaml", ".yml"}


def test_named_environment_deploy_workflows_are_retired() -> None:
    assert all(not path.exists() for path in RETIRED_WORKFLOWS)
    assert not (REPO / "deploy/control-plane/README-cd.md").exists()


def test_active_upstream_configuration_has_no_retired_fi_dependency() -> None:
    roots = (
        REPO / ".github",
        REPO / "deploy",
        REPO / "ENVIRONMENT.md",
        REPO / "docs/operations.md",
        REPO / "docs/deployment-reliability.md",
        REPO / "docs/minimal-identity.md",
    )
    files: list[Path] = []
    for root in roots:
        files.extend(root.rglob("*") if root.is_dir() else (root,))
    active = [
        path
        for path in files
        if path.is_file() and (path.suffix in TEXT_SUFFIXES or path.name.startswith("Dockerfile"))
    ]

    for token in RETIRED_CONFIGURATION:
        offenders = [str(path.relative_to(REPO)) for path in active if token in path.read_text()]
        assert not offenders, f"{token} remains in active upstream configuration: {offenders}"
