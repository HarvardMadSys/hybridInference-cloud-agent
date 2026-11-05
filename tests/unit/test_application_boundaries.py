"""Keep HTTP adapters, domain vocabulary, and persistence ports directional."""

from __future__ import annotations

from pathlib import Path

import pytest
from tests.support.import_boundaries import file_imports, imported_modules, matches_package

ROOT = Path(__file__).resolve().parents[2]
BACKEND = ROOT / "apps/backend"
CONTROL = BACKEND / "cloud_agent"
BACKEND_PACKAGES = (CONTROL, BACKEND / "cloud_agent_host", BACKEND / "cloud_agent_shared")


def test_backend_imports_use_installed_package_names() -> None:
    """Checkout-only namespace imports cannot bypass service boundaries.

    The host image flattens the Python packages rather than shipping
    ``apps/backend``. An import that names that checkout namespace can resolve
    in tests while failing in the deployed image, and the installed-package
    boundary checks would not recognize its implementation dependency.
    """
    offenders = []
    for package_root in BACKEND_PACKAGES:
        paths = sorted(package_root.rglob("*.py"))
        assert paths, f"no production sources found under {package_root}"
        for path in paths:
            targets = file_imports(path, root=package_root, package_name=package_root.name)
            forbidden = sorted(
                target for target in targets if matches_package(target, "apps.backend")
            )
            if forbidden:
                offenders.append(f"{path.relative_to(BACKEND)}: {forbidden}")
    assert not offenders, f"backend imports use the checkout namespace: {offenders}"


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


#: What a port may name besides the standard library and third-party packages:
#: vocabulary, wire models, shared contracts and other ports.
PORT_DEPENDENCIES = (
    "cloud_agent.domain",
    "cloud_agent.schemas",
    "cloud_agent.ports",
    "cloud_agent_shared",
)
#: First-party packages, of which a port may import only the ones above.
FIRST_PARTY = ("cloud_agent", "cloud_agent_host", "cloud_agent_shared")

#: Ports that still name a type outside the packages above, and why it has not
#: moved yet. Each is ``(port module, imported module)``; an entry is removed
#: when its type moves, and a stale one fails the test below.
PORT_IMPORT_EXCEPTIONS = {
    # A value, not a type: the default retention window, which the protocol
    # states and its repository applies.
    ("ports/host_capacity.py", "cloud_agent.capacity_protocol"),
    # Each of the following names part of a response-model tree whose siblings
    # live in the same module; moving the one a port names would split the
    # tree. The code-structure plan moves these modules' models to schemas/
    # whole, one pull request per module.
    ("ports/context_queries.py", "cloud_agent.context.context_dna"),
    ("ports/context_queries.py", "cloud_agent.context.context_insights"),
    ("ports/context_queries.py", "cloud_agent.context.context_request"),
    ("ports/file_activity.py", "cloud_agent.file_activity.models"),
    ("ports/owner_usage.py", "cloud_agent.usage.owner_usage"),
}


def _port_imports() -> dict[str, set[str]]:
    """Every port module's first-party imports, keyed by its path under ``cloud_agent``."""
    found = {}
    for path in sorted((CONTROL / "ports").rglob("*.py")):
        targets = file_imports(path, root=CONTROL, package_name="cloud_agent")
        found[path.relative_to(CONTROL).as_posix()] = {
            target
            for target in targets
            if any(matches_package(target, package) for package in FIRST_PARTY)
        }
    assert found, "no port modules found"
    return found


def _excepted(port: str, target: str) -> bool:
    return any(
        port == excepted and matches_package(target, module)
        for excepted, module in PORT_IMPORT_EXCEPTIONS
    )


def test_ports_import_only_vocabulary_schemas_and_shared_contracts() -> None:
    """A port names the values it trades in; it does not depend on a feature's code.

    A port is what a caller and an implementation both import. Whatever it
    names, both of them depend on, so a port that imports a feature module makes
    every repository behind it depend on that feature as well.
    """
    offenders = []
    for port, targets in _port_imports().items():
        forbidden = sorted(
            target
            for target in targets
            if not any(matches_package(target, allowed) for allowed in PORT_DEPENDENCIES)
            and not _excepted(port, target)
        )
        if forbidden:
            offenders.append(f"{port}: {forbidden}")
    assert not offenders, f"a port imports outside domain/, schemas/ and shared: {offenders}"


def test_every_port_import_exception_is_still_needed() -> None:
    """An exception whose import is gone is removed, so the list only shrinks."""
    imports = _port_imports()
    stale = sorted(
        (port, module)
        for port, module in PORT_IMPORT_EXCEPTIONS
        if not any(matches_package(target, module) for target in imports.get(port, ()))
    )
    assert not stale, f"port import exceptions no longer needed: {stale}"


@pytest.mark.parametrize(
    "source",
    [
        "from cloud_agent.goals.models import GoalInput",
        "if TYPE_CHECKING:\n    from cloud_agent.agent_memory import MemoryTask",
        "from ..usage import owner_usage",
        "import cloud_agent_host.runner",
        "def deferred():\n    from ..storage import assembly",
    ],
)
def test_port_import_scanner_refuses_feature_modules(source: str) -> None:
    """Negative controls: type-only, relative and deferred imports are all seen."""
    targets = imported_modules(source, package="cloud_agent.ports")
    assert any(
        any(matches_package(target, package) for package in FIRST_PARTY)
        and not any(matches_package(target, allowed) for allowed in PORT_DEPENDENCIES)
        for target in targets
    )


@pytest.mark.parametrize(
    "source",
    [
        "from cloud_agent.domain.goals import GoalAction",
        "if TYPE_CHECKING:\n    from ..schemas.goals import GoalInput",
        "from .errors import PortError",
        "from cloud_agent_shared.shared_protocol import GOAL_REPORT_CAPABILITY",
        "from pydantic import BaseModel",
        "from collections.abc import AsyncIterator",
        "import cloud_agent_shared_extra",
    ],
)
def test_port_import_scanner_allows_vocabulary_and_libraries(source: str) -> None:
    """Domain, schemas, shared contracts, other ports and libraries are all fine."""
    targets = imported_modules(source, package="cloud_agent.ports")
    assert not any(
        any(matches_package(target, package) for package in FIRST_PARTY)
        and not any(matches_package(target, allowed) for allowed in PORT_DEPENDENCIES)
        for target in targets
    )


@pytest.mark.parametrize("layer", ["application", "domain"])
def test_application_and_domain_do_not_import_web_frameworks(layer: str) -> None:
    """Flows and rules refuse with domain errors; only the API maps them onto HTTP.

    A flow also runs with no request behind it (a waiting message, a goal round,
    the recovery sweep), where an ``HTTPException`` is nobody's answer and its
    status code is a guess at what the caller should do.
    """
    offenders = []
    for path in (CONTROL / layer).rglob("*.py"):
        targets = file_imports(path, root=CONTROL, package_name="cloud_agent")
        forbidden = sorted(
            target
            for target in targets
            if matches_package(target, "fastapi") or matches_package(target, "starlette")
        )
        if forbidden:
            offenders.append(f"{path.relative_to(CONTROL)}: {forbidden}")
    assert not offenders, f"{layer} imports a web framework: {offenders}"


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


@pytest.mark.parametrize(
    "source",
    [
        "import apps.backend.cloud_agent_host.runtimes as runtimes",
        "from apps.backend.cloud_agent.api import turns",
        "from apps.backend import cloud_agent_shared",
        "from apps import backend",
        "from apps.backend import *",
        "if TYPE_CHECKING:\n    from apps.backend.cloud_agent_host import runner",
        "def deferred():\n    from apps.backend.cloud_agent.storage import repositories",
    ],
)
def test_import_scanner_catches_checkout_namespace_imports(source: str) -> None:
    """Absolute, parent, and deferred forms cannot disguise a checkout-only import."""
    targets = imported_modules(source, package="cloud_agent.application")
    assert any(matches_package(target, "apps.backend") for target in targets)


@pytest.mark.parametrize(
    "source",
    [
        "from cloud_agent_shared import runtime_catalog",
        "from ..domain import execution",
        "import apps.backend_client",
        "from apps import backend_client",
    ],
)
def test_import_scanner_allows_installed_and_unrelated_namespaces(source: str) -> None:
    """Only the checkout package prefix is forbidden by the namespace guard."""
    targets = imported_modules(source, package="cloud_agent.application")
    assert not any(matches_package(target, "apps.backend") for target in targets)
