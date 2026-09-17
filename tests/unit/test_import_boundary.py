"""The import boundary between the contracts, the control plane, and the host.

``cloud_agent_contracts`` is the one package both images ship, so it must not
import either implementation package. The control plane may still reach into
``cloud_agent_host`` for what has not moved yet, but only through a transition
allowlist that may shrink and never grow.
"""

from __future__ import annotations

import ast
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
CONTROL_PLANE_ROOT = REPO_ROOT / "cloud_agent"
CONTRACTS_ROOT = REPO_ROOT / "cloud_agent_contracts"

#: Top-level packages that depend on the host side of the deployment; a shared
#: contracts package can never import one of these.
IMPLEMENTATION_PACKAGES = frozenset({"cloud_agent", "cloud_agent_host"})

#: What the control plane may still import from ``cloud_agent_host``, as
#: ``(module, symbol)``. This is a transition allowlist: it may only shrink,
#: one entry per name that moves to ``cloud_agent_contracts``.
HOST_IMPORT_ALLOWLIST: frozenset[tuple[str, str]] = frozenset(
    {
        ("cloud_agent_host.egress", "EgressPolicyError"),
        ("cloud_agent_host.egress", "build_policy_from_env"),
        ("cloud_agent_host.event_payloads", "MAX_EVENT_PAYLOAD_BYTES"),
        ("cloud_agent_host.event_payloads", "bound_event_payload"),
        ("cloud_agent_host.host_capacity", "CPU_MISSING_COUNTER_RESET"),
        ("cloud_agent_host.host_capacity", "CPU_MISSING_INVALID_SAMPLE"),
        ("cloud_agent_host.host_capacity", "CPU_MISSING_SOURCE_UNAVAILABLE"),
        ("cloud_agent_host.host_capacity", "CPU_MISSING_WARMING_UP"),
        ("cloud_agent_host.host_capacity", "MEMORY_MISSING_INVALID_SAMPLE"),
        ("cloud_agent_host.host_capacity", "MEMORY_MISSING_SOURCE_UNAVAILABLE"),
        ("cloud_agent_host.host_capacity", "OTHER_STATES"),
        ("cloud_agent_host.host_capacity", "SCHEMA_VERSION"),
        ("cloud_agent_host.host_capacity", "SOURCE"),
        ("cloud_agent_host.host_capacity", "memory_usage"),
        ("cloud_agent_host.patch_gate", "PatchGateResult"),
        ("cloud_agent_host.patch_gate", "branch_name_for"),
        ("cloud_agent_host.patch_gate", "validate_change_set"),
        ("cloud_agent_host.patch_gate", "validate_patch"),
        ("cloud_agent_host.runtimes", "registered_runtimes"),
        ("cloud_agent_host.runtimes", "runtime_reasoning_efforts"),
        ("cloud_agent_host.runtimes", "runtimes_supporting_mcp"),
        ("cloud_agent_host.sandbox", "KATA_RUNTIME"),
        ("cloud_agent_host.source_archive", "build_base_bundle"),
        ("cloud_agent_host.turn_resources", "summarize_run_resources"),
        ("cloud_agent_host.turn_resources", "summarize_turn_resources"),
        ("cloud_agent_host.workspace_broker_client", "WorkspaceBrokerClient"),
        ("cloud_agent_host.workspace_broker_client", "WorkspaceBrokerError"),
        ("cloud_agent_host.workspace_browser", "WorkspacePathError"),
        ("cloud_agent_host.workspace_browser", "normalize_workspace_path"),
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
