"""The import boundary between the shared packages and the two implementations.

``cloud_agent_shared`` and ``cloud_agent_shared.git`` are the packages both images
ship, so neither may import an implementation package. The control plane and
host do not import one another: shared runtime discovery facts live in the
shared catalog, and host command builders and output parsers remain on the
host. Each deployment can therefore load its own package independently.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
CONTROL_PLANE_ROOT = REPO_ROOT / "apps" / "backend" / "cloud_agent"
SHARED_ROOT = REPO_ROOT / "apps" / "backend" / "cloud_agent_shared"
HOST_ROOT = REPO_ROOT / "apps" / "backend" / "cloud_agent_host"

#: The packages both images ship. One entry where there were two: the Git
#: algorithms became ``cloud_agent_shared.git``, and a subpackage is not a
#: second root to scan on its own — the I/O rule below is what keeps the one
#: thing that made it separate from spreading.
SHARED_ROOTS = {"cloud_agent_shared": SHARED_ROOT}

#: The only subpackage of the shared package allowed to touch the outside
#: world, and the two modules that do it: Git drives ``subprocess`` and the
#: source archives are ``tarfile``. Everything else there is vocabulary two
#: images agree on, and a parser or a network client in it would ride into both.
IO_SUBPACKAGE = "git"
IO_MODULES = frozenset({"subprocess", "tarfile"})

#: The control-plane package, which the runner image does not ship. This is the
#: one direction of the boundary no compatibility shim can bridge.
CONTROL_PLANE_PACKAGE = "cloud_agent"

#: The host package, which the control plane must not import.
HOST_PACKAGE = "cloud_agent_host"

#: Top-level packages that depend on the host side of the deployment; a shared
#: package can never import one of these.
IMPLEMENTATION_PACKAGES = frozenset({"cloud_agent", "cloud_agent_host"})


def _parse(path: Path) -> ast.Module:
    """Parse one source file; ``TYPE_CHECKING`` blocks are ordinary AST nodes."""
    return ast.parse(path.read_text(encoding="utf-8"), filename=str(path))


def _host_import_pairs(path: Path) -> set[tuple[str, str]]:
    """The ``(cloud_agent_host.<module>, symbol)`` pairs one file imports."""
    pairs: set[tuple[str, str]] = set()
    for node in ast.walk(_parse(path)):
        if not isinstance(node, ast.ImportFrom) or node.module is None:
            continue
        if node.module != HOST_PACKAGE and not node.module.startswith(f"{HOST_PACKAGE}."):
            continue
        pairs.update((node.module, alias.name) for alias in node.names)
    return pairs


def _host_module_imports(path: Path) -> set[str]:
    """Modules one file imports *whole* from ``cloud_agent_host``.

    A scan of its own because it is a different node: ``import a.b as c`` is an
    ``ast.Import``, and walking only ``ast.ImportFrom`` would miss it. Whole
    modules are reported separately from symbol imports so either violation
    points to the dependency that must move into the shared package.
    """
    modules: set[str] = set()
    for node in ast.walk(_parse(path)):
        if not isinstance(node, ast.Import):
            continue
        modules.update(
            alias.name
            for alias in node.names
            if alias.name == HOST_PACKAGE or alias.name.startswith(f"{HOST_PACKAGE}.")
        )
    return modules


def _imported_modules(path: Path) -> set[str]:
    """Every module name one file imports, in any form, ``TYPE_CHECKING`` too."""
    modules: set[str] = set()
    for node in ast.walk(_parse(path)):
        if isinstance(node, ast.Import):
            modules.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            modules.add(node.module)
    return modules


def test_control_plane_imports_no_host_package() -> None:
    """The control plane imports shared vocabulary instead of host adapters.

    Both symbol imports and whole-module imports are scanned, including
    ``TYPE_CHECKING`` blocks. No transition exceptions remain.
    """
    scanned = set()
    whole_modules: dict[str, set[str]] = {}
    for path in sorted(CONTROL_PLANE_ROOT.rglob("*.py")):
        scanned |= _host_import_pairs(path)
        imported = _host_module_imports(path)
        if imported:
            whole_modules[str(path.relative_to(REPO_ROOT))] = imported

    assert whole_modules == {}, (
        "the control plane must not import host implementation modules:\n"
        + "\n".join(f"{path}: {sorted(modules)}" for path, modules in sorted(whole_modules.items()))
    )

    assert scanned == set(), f"the control plane imports host symbols: {sorted(scanned)}"


def test_only_the_git_subpackage_reaches_outside_the_process() -> None:
    """I/O lives in one subpackage, and it is still there to be found.

    The exemption has two halves and both are asserted: nothing outside
    ``git/`` imports the I/O modules, and ``git/`` still does — so the day the
    algorithms stop needing ``subprocess``, this fails and the exemption goes
    rather than quietly covering an empty set.
    """

    def _io_imports(path: Path) -> set[str]:
        return _imported_modules(path) & IO_MODULES

    offenders: dict[str, set[str]] = {}
    doing_io: set[str] = set()
    for path in sorted(SHARED_ROOT.rglob("*.py")):
        found = _io_imports(path)
        if not found:
            continue
        if path.parent.name == IO_SUBPACKAGE:
            doing_io |= found
        else:
            offenders[str(path.relative_to(REPO_ROOT))] = found

    assert not offenders, f"only {IO_SUBPACKAGE}/ may do I/O in the shared package: {offenders}"
    assert doing_io == IO_MODULES, (
        f"{IO_SUBPACKAGE}/ no longer imports {sorted(IO_MODULES - doing_io)}; "
        "the exemption above now covers nothing and should go"
    )


@pytest.mark.parametrize(
    ("source", "pairs", "modules"),
    [
        (
            "import cloud_agent_host.sandbox as host_impl\n",
            set(),
            {"cloud_agent_host.sandbox"},
        ),
        (
            "from cloud_agent_host.runtimes import get_runtime\n",
            {("cloud_agent_host.runtimes", "get_runtime")},
            set(),
        ),
    ],
)
def test_the_scanner_sees_both_host_import_forms(
    tmp_path: Path, source: str, pairs: set[tuple[str, str]], modules: set[str]
) -> None:
    """Both whole modules and individual host symbols violate the boundary."""
    offender = tmp_path / "offender.py"
    offender.write_text(source, encoding="utf-8")

    assert _host_import_pairs(offender) == pairs
    assert _host_module_imports(offender) == modules


@pytest.mark.parametrize("package", sorted(SHARED_ROOTS))
def test_shared_package_imports_no_implementation_package(package: str) -> None:
    """A shared package must import neither implementation package.

    Both the control plane and the runner image ship these packages, and one
    that reached back into either side would drag that whole side of the
    deployment along with it. Relative imports cannot cross packages, so only
    absolute ones can break this.
    """
    root = SHARED_ROOTS[package]
    offenders: dict[str, set[str]] = {}
    scanned = sorted(root.rglob("*.py"))
    assert scanned, f"no Python sources found under {root}"
    for path in scanned:
        imported = {
            module
            for module in _imported_modules(path)
            if module.split(".", 1)[0] in IMPLEMENTATION_PACKAGES
        }
        if imported:
            offenders[str(path.relative_to(REPO_ROOT))] = imported
    assert offenders == {}


def test_host_package_imports_no_control_plane_package() -> None:
    """``cloud_agent_host`` must never import the control plane.

    The runner image ships the host package and the two shared packages only,
    so a control-plane import works in this checkout and fails in the image
    that runs untrusted work. That is a run-time ``ImportError`` on a host,
    which is why the count is zero and not "zero for now": there is no
    compatibility shim this direction could go through. ``TYPE_CHECKING``
    blocks are walked like any other node, because the annotation that needs
    them is exactly the kind of dependency this boundary is about.
    """
    offenders: set[str] = set()
    scanned = sorted(HOST_ROOT.rglob("*.py"))
    assert scanned, f"no Python sources found under {HOST_ROOT}"
    for path in scanned:
        for node in ast.walk(_parse(path)):
            if isinstance(node, ast.Import):
                modules = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                modules = [node.module]
            else:
                continue
            for module in modules:
                if module.split(".", 1)[0] == CONTROL_PLANE_PACKAGE:
                    relative = path.relative_to(REPO_ROOT)
                    offenders.add(f"{relative}:{node.lineno} imports {module}")
    assert offenders == set(), "cloud_agent_host imports the control plane:\n" + "\n".join(
        sorted(offenders)
    )
