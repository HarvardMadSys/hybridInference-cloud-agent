"""The import boundary between the shared packages and the two implementations.

``cloud_agent_shared`` and ``cloud_agent_shared.git`` are the packages both images
ship, so neither may import an implementation package, and only the files
``IO_EXEMPTIONS`` names may reach outside the process. The control plane and
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

#: Imports that reach outside the process: other programs, the network, and
#: work that outlives the call that started it. "I/O" in the shared package
#: means importing one of these. A module covers its submodules, and
#: ``from urllib import request`` counts as ``urllib.request``. Builtin file
#: access needs no import, so this scan does not see it. Everything else in the
#: shared package is vocabulary the two images agree on, and a network client
#: or a background thread in it would ship in both.
IO_MODULES = frozenset(
    {
        # Other programs, and the source archives built from their output.
        "subprocess",
        "multiprocessing",
        "tarfile",
        # The network: standard-library modules, and third-party clients and
        # database drivers.
        "socket",
        "ssl",
        "http.client",
        "urllib.request",
        "httpx",
        "requests",
        "aiohttp",
        "websockets",
        "asyncpg",
        "psycopg",
        "boto3",
        "botocore",
        # Work that outlives the call that started it. ``threading`` counts
        # whole, locks included: the scan sees a module, not what is done with
        # it, and vocabulary has no shared state to lock.
        "threading",
        "concurrent.futures",
        "atexit",
    }
)

#: The only places in the shared package that may import those, by path
#: relative to the package, and which of them each may import. A directory
#: covers every file under it. The check is two-sided: an import outside an
#: entry fails, and so does an entry whose code no longer imports something it
#: names. An exemption therefore goes when its reason does, instead of
#: quietly covering an empty set.
IO_EXEMPTIONS: dict[str, frozenset[str]] = {
    # Git drives ``subprocess`` and the source archives are ``tarfile``.
    "git": frozenset({"subprocess", "tarfile"}),
    # The operation-profiling collector both images run (AGENTS.md, repo map):
    # ``socket`` only for ``gethostname()``, ``threading`` for its locks and
    # its daemon exporter thread, and ``atexit`` to flush that exporter. It
    # opens no connection. It writes to a file the deployment names.
    "profiling": frozenset({"socket", "threading", "atexit"}),
    # The collector's stable facade starts no thread; it re-exports
    # ``threading`` for callers that patch the collector through it.
    "operation_profiling.py": frozenset({"threading"}),
}

#: The control-plane package, which the runner image does not ship. This is the
#: one direction of the boundary no compatibility shim can bridge.
CONTROL_PLANE_PACKAGE = "cloud_agent"

#: The host package, which the control plane must not import.
HOST_PACKAGE = "cloud_agent_host"

#: Top-level packages that depend on the host side of the deployment; a shared
#: package can never import one of these.
IMPLEMENTATION_PACKAGES = frozenset({"cloud_agent", "cloud_agent_host"})


#: The host's re-exports of modules that moved to ``cloud_agent_shared``. Each
#: stays only while something still imports it, and is deleted after.
HOST_SHIMS = frozenset(
    {
        "cloud_agent_host.patch_gate",
        "cloud_agent_host.shared_protocol",
        "cloud_agent_host.source_archive",
    }
)

#: Where a shim may still be imported from: every tree a Python import can
#: start in, the ``cloud_agent_host`` package included.
SHIM_SCAN_ROOTS = ("apps/backend", "scripts", "tests")

#: Who still imports a shim, as ``(file, shim)``. Another transition allowlist
#: that may only shrink: new code imports the shared module, and an importer
#: that moves deletes its entry here. No ``cloud_agent_host`` module is on it.
SHIM_IMPORT_ALLOWLIST: frozenset[tuple[str, str]] = frozenset(
    {
        ("scripts/bench_operation_overhead.py", "cloud_agent_host.shared_protocol"),
        (
            "tests/integration/servers/test_agent_jobs_lifecycle.py",
            "cloud_agent_host.shared_protocol",
        ),
        (
            "tests/integration/servers/test_measurement_acceptance_db.py",
            "cloud_agent_host.shared_protocol",
        ),
        (
            "tests/integration/servers/test_pending_message_queue.py",
            "cloud_agent_host.shared_protocol",
        ),
        (
            "tests/integration/storage/test_agent_grant_store.py",
            "cloud_agent_host.shared_protocol",
        ),
        ("tests/integration/storage/test_agent_job_store.py", "cloud_agent_host.shared_protocol"),
        (
            "tests/integration/storage/test_host_pool_store.py",
            "cloud_agent_host.shared_protocol",
        ),
        ("tests/integration/test_agent_publisher.py", "cloud_agent_host.patch_gate"),
        ("tests/unit/test_agent_patch_gate.py", "cloud_agent_host.patch_gate"),
        ("tests/unit/test_candidate_check.py", "cloud_agent_host.shared_protocol"),
        ("tests/unit/test_cross_artifact_contracts.py", "cloud_agent_host.shared_protocol"),
        ("tests/unit/test_deploy_readiness.py", "cloud_agent_host.shared_protocol"),
        ("tests/unit/test_execution_profiles.py", "cloud_agent_host.shared_protocol"),
        ("tests/unit/test_source_archive.py", "cloud_agent_host.source_archive"),
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


def _io_imports(path: Path) -> set[str]:
    """The ``IO_MODULES`` entries one file imports, in any import form.

    A module counts under the entry that contains it, so ``asyncpg.pool`` is
    ``asyncpg``. A ``from`` import also counts each name it takes as a possible
    submodule, so ``from urllib import request`` is ``urllib.request``, as
    ``import urllib.request`` is.
    """
    names: set[str] = set()
    for node in ast.walk(_parse(path)):
        if isinstance(node, ast.Import):
            names.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            names.add(node.module)
            names.update(f"{node.module}.{alias.name}" for alias in node.names)
    return {
        entry
        for entry in IO_MODULES
        if any(name == entry or name.startswith(f"{entry}.") for name in names)
    }


def _io_exemption(relative: Path) -> str | None:
    """The ``IO_EXEMPTIONS`` entry covering one file of the shared package."""
    for entry in IO_EXEMPTIONS:
        if relative == Path(entry) or Path(entry) in relative.parents:
            return entry
    return None


def test_only_the_exempted_shared_modules_reach_outside_the_process() -> None:
    """I/O lives where ``IO_EXEMPTIONS`` says, and is still there to be found.

    Both halves are asserted. Nothing outside an entry imports an I/O module,
    and nothing inside one imports a module its entry does not name. Every
    entry also still imports everything it names. If the Git algorithms stop
    needing ``subprocess``, or the profiling collector stops starting a
    thread, this fails, and the exemption shrinks instead of quietly covering
    an empty set.
    """
    offenders: dict[str, set[str]] = {}
    used: dict[str, set[str]] = {entry: set() for entry in IO_EXEMPTIONS}
    for path in sorted(SHARED_ROOT.rglob("*.py")):
        found = _io_imports(path)
        if not found:
            continue
        entry = _io_exemption(path.relative_to(SHARED_ROOT))
        allowed = IO_EXEMPTIONS[entry] if entry is not None else frozenset()
        if entry is not None:
            used[entry] |= found & allowed
        if found - allowed:
            offenders[str(path.relative_to(REPO_ROOT))] = found - allowed

    assert not offenders, (
        "only IO_EXEMPTIONS may reach outside the process in the shared package, "
        f"and only through the modules each entry names: {offenders}"
    )
    unused = {
        entry: sorted(modules - used[entry])
        for entry, modules in IO_EXEMPTIONS.items()
        if modules - used[entry]
    }
    assert not unused, (
        f"these exemptions name modules their code no longer imports; narrow or delete them: "
        f"{unused}"
    )


def test_the_io_scanner_sees_every_import_form(tmp_path: Path) -> None:
    """The negative case for the check above, one line per form.

    The set this replaced held two names and compared them exactly. So
    ``import asyncpg.pool``, ``from urllib import request``, and any client not
    on that list all passed as vocabulary. Parsing a URL is not I/O.
    """
    offender = tmp_path / "offender.py"
    offender.write_text(
        "import asyncpg.pool\n"
        "from urllib import request\n"
        "from http import client as http_client\n"
        "from httpx import AsyncClient\n"
        "import threading as _threading\n"
        "from urllib.parse import urlsplit\n",
        encoding="utf-8",
    )

    assert _io_imports(offender) == {
        "asyncpg",
        "urllib.request",
        "http.client",
        "httpx",
        "threading",
    }


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


def _shim_imports(path: Path) -> set[str]:
    """The host shims one file imports, in every form an import can take."""
    shims: set[str] = set()
    for node in ast.walk(_parse(path)):
        if isinstance(node, ast.Import):
            shims.update(alias.name for alias in node.names if alias.name in HOST_SHIMS)
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            if node.module in HOST_SHIMS:
                shims.add(node.module)
            elif node.module == HOST_PACKAGE:
                # ``from cloud_agent_host import shared_protocol``
                shims.update(
                    f"{HOST_PACKAGE}.{alias.name}"
                    for alias in node.names
                    if f"{HOST_PACKAGE}.{alias.name}" in HOST_SHIMS
                )
    return shims


def test_host_shim_importers_are_the_transition_allowlist() -> None:
    """The host's re-exports of moved modules may only lose importers.

    Each shim's docstring used to list its callers by hand, and those lists
    went stale as new tests imported the old path -- the shims were gaining
    importers instead of losing them, and ``runner.py`` imported one module
    through both paths. Equality, not a subset check: a new importer fails,
    and so does an entry left behind by one that moved.
    """
    names = {shim.rsplit(".", 1)[1] for shim in HOST_SHIMS}
    scanned: set[tuple[str, str]] = set()
    for root in SHIM_SCAN_ROOTS:
        for path in sorted((REPO_ROOT / root).rglob("*.py")):
            text = path.read_text(encoding="utf-8")
            # Parsing every file is most of this test's time; a file that never
            # spells a shim's name cannot import it in any form.
            if HOST_PACKAGE not in text or not any(name in text for name in names):
                continue
            relative = path.relative_to(REPO_ROOT).as_posix()
            scanned.update((relative, shim) for shim in _shim_imports(path))

    allowlist = set(SHIM_IMPORT_ALLOWLIST)
    assert scanned == allowlist, (
        "import the cloud_agent_shared module instead of the host shim:\n"
        f"new shim imports: {sorted(scanned - allowlist)}\n"
        f"stale allowlist entries to delete: {sorted(allowlist - scanned)}"
    )
    shimmed = {shim for _path, shim in allowlist}
    assert shimmed == set(HOST_SHIMS), (
        f"nothing imports {sorted(set(HOST_SHIMS) - shimmed)} any more: delete the module "
        "and its entry in HOST_SHIMS"
    )


def test_the_shim_scanner_sees_every_import_form(tmp_path: Path) -> None:
    """The negative case for the check above, one line per import form."""
    importer = tmp_path / "importer.py"
    importer.write_text(
        "import cloud_agent_host.patch_gate\n"
        "from cloud_agent_host import shared_protocol, sandbox\n"
        "from cloud_agent_host.source_archive import build_source_archive\n"
        "from cloud_agent_shared.shared_protocol import FORK_HELPER\n",
        encoding="utf-8",
    )

    assert _shim_imports(importer) == set(HOST_SHIMS)
