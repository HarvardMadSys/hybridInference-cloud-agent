"""No unit test reaches real Docker through the network guard's long-lived helper."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from cloud_agent_host import network_guard_helper

if TYPE_CHECKING:
    from collections.abc import Iterator


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
