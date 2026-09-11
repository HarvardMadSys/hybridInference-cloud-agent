"""Point an app's storage dependencies at one fake, per capability.

Routes depend on the capability they use rather than on the store those
capabilities were split from, so a test that mounts a router has to answer for
each getter that router asks for. Writing that list out in every test file means
adding a capability to a route breaks a dozen unrelated fixtures, and the usual
fix — stubbing a getter the test never exercises — quietly widens what the test
claims to cover.

:func:`capability_overrides` makes the list explicit instead: a test names the
capabilities its scenario genuinely needs. A route that grows a dependency the
test did not name then fails loudly rather than being papered over.

One fake is passed for every named capability because it is usually one object.
That object is still an all-purpose double, and replacing it with per-boundary
fakes is its own step; naming the capabilities here is what makes that split
mechanical when it happens.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from cloud_agent.api import deps

if TYPE_CHECKING:
    from fastapi import FastAPI

#: Capability name -> the dependency getter that answers for it.
GETTERS: dict[str, Any] = {
    "conversation_commands": deps.get_conversation_commands,
    "conversation_queries": deps.get_conversation_queries,
    "attempts": deps.get_attempt_commands,
    "execution_queries": deps.get_execution_queries,
    "sandboxes": deps.get_sandbox_repository,
    "residencies": deps.get_residency_repository,
    "session_forks": deps.get_session_fork_repository,
    "snapshots": deps.get_snapshot_repository,
    "hosts": deps.get_host_pool,
    "source_control": deps.get_source_control_repository,
    "mcp": deps.get_mcp_connections,
    "grants": deps.get_grant_repository,
    "credentials": deps.get_credential_queries,
    "ledger": deps.get_ledger_repository,
    "audit": deps.get_audit_repository,
    "admin_queries": deps.get_admin_queries,
}


def _provider(fake: Any) -> Any:
    """Return a zero-argument dependency that answers with ``fake``.

    A factory rather than a lambda with a default argument: the default-argument
    trick reads as a closure over a loop variable — the bug it exists to avoid —
    and a reader has to check twice that it is not one.
    """

    def provide() -> Any:
        return fake

    return provide


def capability_overrides(app: FastAPI, fake: Any, *capabilities: str) -> None:
    """Answer ``capabilities`` with ``fake`` on ``app``.

    Args:
        app: The application whose dependency overrides are being set.
        fake: The double. It must implement every named capability's methods.
        capabilities: The capabilities this test's routes actually use. Naming
            them is the point: an unnamed one is a dependency this test does not
            stand behind.

    Raises:
        KeyError: A name is not a known capability. Failing here beats a fixture
            that silently stubs nothing and lets the route reach the real
            database.
    """
    unknown = sorted(set(capabilities) - set(GETTERS))
    if unknown:
        raise KeyError(f"unknown capabilities {unknown}; known: {sorted(GETTERS)}")
    for name in capabilities:
        app.dependency_overrides[GETTERS[name]] = _provider(fake)
