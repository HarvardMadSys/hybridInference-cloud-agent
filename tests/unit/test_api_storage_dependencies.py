"""What `Depends(get_storage)` hands a handler, checked against what it claims.

`get_storage` returns `Storage`: the object that *holds* the capability ports,
not one of them. A handler that annotates a single port instead narrows nothing
-- FastAPI erases the annotation, mypy sees `Any` through `Depends`, and the
attribute lookup happens against the assembly at runtime.

`verify_claiming_host` was annotated `HostPoolRepository` and called
`store.authenticate_host(...)`, which no assembly has. Every
`POST /v1/agent/worker/claim` answered 500, so no runner on staging could claim
any job at all -- and nothing in the suite noticed, because the fake store the
API tests use carries that method flat rather than behind a port.
"""

from __future__ import annotations

import pathlib
import re

import pytest

REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]
API_DIR = REPO_ROOT / "cloud_agent" / "api"

#: Every module whose handlers take the store as a dependency.
MODULES = ("routes.py", "admin_runs.py", "hosts.py", "deps.py")

PARAMETER = re.compile(
    r"^[ \t]*(?P<name>\w+):[ \t]*(?P<annotation>[^=\n]+?)[ \t]*=[ \t]*Depends\(get_storage\)",
    re.MULTILINE,
)

PORT_CLASS = re.compile(r"^class (\w+)\(Protocol\)", re.MULTILINE)


def port_names() -> set[str]:
    """Every capability port's class name, read from the ports package itself.

    Enumerated rather than listed here: each module declares its own assembly
    protocol (`_RoutesStorage`, `_HostsStorage`, `_AdminRunsStorage`), and a new
    one must pass without editing this test. What must never appear is one of
    the ports those assemblies hold.
    """
    found: set[str] = set()
    for path in (REPO_ROOT / "cloud_agent" / "ports").glob("*.py"):
        found |= set(PORT_CLASS.findall(path.read_text(encoding="utf-8")))
    assert found, "no capability ports were found; the ports package moved"
    return found


@pytest.mark.parametrize("module", MODULES)
def test_no_handler_annotates_one_port_for_the_whole_store(module: str) -> None:
    source = (API_DIR / module).read_text(encoding="utf-8")
    ports = port_names()

    wrong = [
        (match.group("name"), annotation)
        for match in PARAMETER.finditer(source)
        if (annotation := match.group("annotation").strip())
        and any(port in annotation for port in ports)
    ]

    assert not wrong, (
        f"{module}: these Depends(get_storage) parameters name a port rather than the "
        f"assembly that dependency returns, so any attribute lookup on them resolves "
        f"against the assembly at runtime: {wrong}"
    )


def test_the_check_sees_the_parameters_it_is_about() -> None:
    """A regex that matched nothing would pass every module silently."""
    found = sum(
        len(PARAMETER.findall((API_DIR / module).read_text(encoding="utf-8"))) for module in MODULES
    )

    assert found >= 5, f"only {found} store dependencies were found; the pattern has drifted"
