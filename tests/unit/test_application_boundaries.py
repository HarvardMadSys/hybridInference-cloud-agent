"""Keep HTTP adapters, domain vocabulary, and persistence ports directional."""

from __future__ import annotations

from pathlib import Path

import pytest
from tests.support.import_boundaries import file_imports, imported_modules, matches_package

ROOT = Path(__file__).resolve().parents[2]
CONTROL = ROOT / "apps/backend/cloud_agent"


def test_only_api_and_app_assembly_import_http_adapters() -> None:
    """Background tasks and service startup must not depend on route modules."""
    offenders = []
    for path in CONTROL.rglob("*.py"):
        relative = path.relative_to(CONTROL)
        if relative.parts[0] == "api" or relative == Path("app.py"):
            continue
        targets = file_imports(path, root=CONTROL, package_name="cloud_agent")
        forbidden = sorted(
            target for target in targets if matches_package(target, "cloud_agent.api")
        )
        if forbidden:
            offenders.append(f"{relative}: {forbidden}")
    assert not offenders, f"HTTP adapters imported below the API layer: {offenders}"


@pytest.mark.parametrize("layer", ["domain", "ports"])
def test_domain_and_ports_do_not_import_storage_or_http_frameworks(layer: str) -> None:
    """Rules and capability contracts remain independent of their adapters."""
    forbidden_packages = ("cloud_agent.storage", "cloud_agent.api", "fastapi", "starlette")
    offenders = []
    for path in (CONTROL / layer).rglob("*.py"):
        targets = file_imports(path, root=CONTROL, package_name="cloud_agent")
        forbidden = sorted(
            target
            for target in targets
            if any(matches_package(target, package) for package in forbidden_packages)
        )
        if forbidden:
            offenders.append(f"{path.relative_to(CONTROL)}: {forbidden}")
    assert not offenders, f"{layer} imports an adapter or framework: {offenders}"


@pytest.mark.parametrize(
    "source",
    [
        "import cloud_agent.api.turns as turns",
        "from cloud_agent.api import turns",
        "from cloud_agent import api",
        "from ..api import turns",
        "from .. import api",
        "if TYPE_CHECKING:\n    from cloud_agent.api.turns import router",
        "def deferred():\n    from ..api import turns",
    ],
)
def test_import_scanner_catches_every_route_import_form(source: str) -> None:
    """Relative, deferred, and type-only imports cannot bypass the boundary."""
    targets = imported_modules(source, package="cloud_agent.application")
    assert any(matches_package(target, "cloud_agent.api") for target in targets)


def test_import_scanner_does_not_confuse_similar_package_names() -> None:
    """A prefix alone must not classify a different package as an adapter."""
    assert not matches_package("cloud_agent.api_client", "cloud_agent.api")
