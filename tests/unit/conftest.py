"""No unit test reaches real Docker through the network guard's long-lived helper,
or a package registry through an import, and every unit test starts on a host
that has just started."""

from __future__ import annotations

from typing import TYPE_CHECKING

import httpx
import pytest
from tests.support.group_lifecycle import set_flags

from cloud_agent.mcp import package_pins
from cloud_agent_host import sandbox_mcp
from cloud_agent_host.network import network_guard_helper

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator


@pytest.fixture(autouse=True)
def _no_real_network_helper(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Refuse, and fail the test for, a real long-lived host-rule helper.

    A guard keeps one only when it is given ``spawn``, as production gives it
    ``network_guard_helper.spawn_helper``: ``docker run`` with ``NET_ADMIN`` in
    the host's network namespace, and ``remove_helper``'s ``docker rm -f`` to
    take a failed one away. A test passes stand-ins of its own instead; one
    that really needs either sets it back with ``monkeypatch`` to opt in.
    """
    attempts: list[str] = []

    def spawn(argv: list[str], *_args: object) -> object:
        attempts.append(" ".join(argv[:3]))
        raise FileNotFoundError("unit tests start no real network guard helper")

    def remove(docker_binary: str, name: str, *_args: object) -> bool:
        attempts.append(f"{docker_binary} rm -f {name}")
        return False

    monkeypatch.setattr(network_guard_helper, "spawn_helper", spawn)
    monkeypatch.setattr(network_guard_helper, "remove_helper", remove)
    yield
    assert not attempts, f"a test reached real Docker for the guard's long-lived helper: {attempts}"


@pytest.fixture(autouse=True)
def _a_freshly_started_host(monkeypatch: pytest.MonkeyPatch) -> None:
    """Forget which slots the sandbox MCP admission knew to be clean.

    It is process state, so without this one test's launches decide whether
    the next test's launch revokes, and an exact sequence of execs depends on
    the order the tests ran in.
    """
    monkeypatch.setattr(sandbox_mcp, "_known_clean", set())


@pytest.fixture(autouse=True)
def _no_package_registry(monkeypatch: pytest.MonkeyPatch) -> None:
    """Answer every package lookup an import makes as an unreachable registry.

    An import pins a bare package name to its current release
    (``cloud_agent.mcp.package_pins``); a test that wants one found sets its
    own ``package_pins.releases``.
    """
    unreachable = httpx.MockTransport(lambda _request: httpx.Response(503))
    monkeypatch.setattr(package_pins, "releases", package_pins.Releases(transport=unreachable))


@pytest.fixture
def lifecycle_flags() -> Callable[..., None]:
    """Set a group's lifecycle by its flag names (``tests.support.group_lifecycle``)."""
    return set_flags
