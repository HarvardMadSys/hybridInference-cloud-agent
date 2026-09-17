"""The import boundary between the contracts, the control plane, and the host.

``cloud_agent_contracts`` is the one package both images ship, so it must not
import either implementation package. The control plane may still reach into
``cloud_agent_host`` for what has not moved yet, but only through a transition
allowlist that may shrink and never grow. In the other direction the host may
not reach into ``cloud_agent`` at all: the runner image does not ship it, so
such an import fails at run time rather than at review time.
"""

from __future__ import annotations

import ast
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
CONTROL_PLANE_ROOT = REPO_ROOT / "cloud_agent"
CONTRACTS_ROOT = REPO_ROOT / "cloud_agent_contracts"
HOST_ROOT = REPO_ROOT / "cloud_agent_host"

#: The control-plane package, which the runner image does not ship. This is the
#: one direction of the boundary no compatibility shim can bridge.
CONTROL_PLANE_PACKAGE = "cloud_agent"

#: Top-level packages that depend on the host side of the deployment; a shared
#: contracts package can never import one of these.
IMPLEMENTATION_PACKAGES = frozenset({"cloud_agent", "cloud_agent_host"})

#: What the control plane may still import from ``cloud_agent_host``, as
#: ``(module, symbol)``. This is a transition allowlist: it may only shrink,
#: one entry per name that moves to ``cloud_agent_contracts``.
HOST_IMPORT_ALLOWLIST: frozenset[tuple[str, str]] = frozenset(
    {
        ("cloud_agent_host.patch_gate", "PatchGateResult"),
        ("cloud_agent_host.patch_gate", "branch_name_for"),
        ("cloud_agent_host.patch_gate", "validate_change_set"),
        ("cloud_agent_host.patch_gate", "validate_patch"),
        ("cloud_agent_host.runtimes", "registered_runtimes"),
        ("cloud_agent_host.runtimes", "runtime_reasoning_efforts"),
        ("cloud_agent_host.runtimes", "runtimes_supporting_mcp"),
        ("cloud_agent_host.sandbox", "KATA_RUNTIME"),
        ("cloud_agent_host.source_archive", "build_base_bundle"),
        ("cloud_agent_host.workspace_broker_client", "WorkspaceBrokerClient"),
        ("cloud_agent_host.workspace_broker_client", "WorkspaceBrokerError"),
    }
)


def _parse(path: Path) -> ast.Module:
    """Parse one source file; ``TYPE_CHECKING`` blocks are ordinary AST nodes."""
    return ast.parse(path.read_text(encoding="utf-8"), filename=str(path))


def _host_import_pairs(path: Path) -> set[tuple[str, str]]:
    """The ``(cloud_agent_host.<module>, symbol)`` pairs one file imports."""
    pairs: set[tuple[str, str]] = set()
    for node in ast.walk(_parse(path)):
        if not isinstance(node, ast.ImportFrom) or node.module is None:
            continue
        if node.module != "cloud_agent_host" and not node.module.startswith("cloud_agent_host."):
            continue
        pairs.update((node.module, alias.name) for alias in node.names)
    return pairs


def _imported_modules(path: Path) -> set[str]:
    """Every module name one file imports, in any form, ``TYPE_CHECKING`` too."""
    modules: set[str] = set()
    for node in ast.walk(_parse(path)):
        if isinstance(node, ast.Import):
            modules.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            modules.add(node.module)
    return modules


def test_control_plane_host_imports_are_the_transition_allowlist() -> None:
    """The control plane's reach into ``cloud_agent_host`` may only shrink.

    The comparison is equality and not a subset check on purpose: an allowlist
    that is never re-derived from the tree goes stale, so a subset check would
    keep blessing a moved module's phantom entry forever and the boundary would
    only ever appear to improve. Equality makes both directions speak — a new
    import fails, and an entry left behind by work that already moved fails.
    """
    scanned = set()
    for path in sorted(CONTROL_PLANE_ROOT.rglob("*.py")):
        scanned |= _host_import_pairs(path)
    allowlist = set(HOST_IMPORT_ALLOWLIST)
    assert scanned == allowlist, (
        f"imports missing from the allowlist: {sorted(scanned - allowlist)}\n"
        f"stale allowlist entries to delete: {sorted(allowlist - scanned)}"
    )


def test_contracts_package_imports_no_implementation_package() -> None:
    """``cloud_agent_contracts`` must import neither implementation package.

    Both the control plane and the runner image ship it, and a contracts
    package that reached back into either one would drag the whole side of the
    deployment along with it. Relative imports cannot cross packages, so only
    absolute ones can break this.
    """
    offenders: dict[str, set[str]] = {}
    scanned = sorted(CONTRACTS_ROOT.rglob("*.py"))
    assert scanned, f"no Python sources found under {CONTRACTS_ROOT}"
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

    The runner image ships the host package and the contracts package only, so
    a control-plane import works in this checkout and fails in the image that
    runs untrusted work. That is a run-time ``ImportError`` on a host, which is
    why the count is zero and not "zero for now": there is no compatibility
    shim this direction could go through. ``TYPE_CHECKING`` blocks are walked
    like any other node, because the annotation that needs them is exactly the
    kind of dependency this boundary is about.
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
