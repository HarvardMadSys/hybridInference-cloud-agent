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


#: The modules that define each port, so a boundary can be derived from the
#: Protocol rather than written out by hand.
#: Capability name (as ``capability_overrides`` receives it, and as ``GETTERS``
#: is keyed) -> the Protocol that describes it. One mapping, one key space: an
#: earlier revision keyed this by Protocol name while the caller passed
#: capability names, so every lookup missed and the boundary was silently never
#: applied -- which is exactly the failure mode the boundary exists to prevent.
PORT_MODULES: dict[str, str] = {
    "conversation_commands": "ConversationCommands",
    "conversation_queries": "ConversationQueries",
    "attempts": "AttemptCommands",
    "execution_queries": "ExecutionQueries",
    "sandboxes": "SandboxRepository",
    "residencies": "ResidencyRepository",
    "session_forks": "SessionForkRepository",
    "snapshots": "SnapshotRepository",
    "hosts": "HostPoolRepository",
    "source_control": "SourceControlRepository",
    "mcp": "McpConnectionRepository",
    "grants": "GrantRepository",
    "credentials": "CredentialQueries",
    "ledger": "LedgerRepository",
    "audit": "AuditRepository",
}


#: Protocol name -> its defining module, for the introspection below.
_PROTOCOL_MODULES: dict[str, str] = {
    "ConversationCommands": "cloud_agent.ports.conversations",
    "ConversationQueries": "cloud_agent.ports.conversation_queries",
    "AttemptCommands": "cloud_agent.ports.attempts",
    "ExecutionQueries": "cloud_agent.ports.execution_queries",
    "SandboxRepository": "cloud_agent.ports.sandboxes",
    "ResidencyRepository": "cloud_agent.ports.residencies",
    "SessionForkRepository": "cloud_agent.ports.session_forks",
    "SnapshotRepository": "cloud_agent.ports.snapshots",
    "HostPoolRepository": "cloud_agent.ports.host_pool",
    "SourceControlRepository": "cloud_agent.ports.source_control",
    "McpConnectionRepository": "cloud_agent.ports.mcp",
    "GrantRepository": "cloud_agent.ports.grants",
    "CredentialQueries": "cloud_agent.ports.credential_queries",
    "LedgerRepository": "cloud_agent.ports.ledger",
    "AuditRepository": "cloud_agent.ports.audit",
}


def _protocol_methods(port: str) -> frozenset[str]:
    """The method names one Protocol declares."""
    import importlib
    import inspect

    module = importlib.import_module(_PROTOCOL_MODULES[port])
    protocol = getattr(module, port)
    return frozenset(
        name
        for name, _ in inspect.getmembers(protocol, inspect.isfunction)
        if not name.startswith("_")
    )


class Boundary:
    """One double, narrowed to the methods of a single capability.

    Why this exists: a route handed the wrong port does not fail against a
    permissive double, because the double answers every method either way. Seven
    such mis-wirings reached a green suite for exactly that reason. Answering
    only what the capability declares turns "this handler used the wrong port"
    into an AttributeError in the test, which is the failure the type checker
    would give if the caller files were in its scope.
    """

    def __init__(self, fake: Any, port: str) -> None:
        """Wrap ``fake``, exposing only what ``port`` declares.

        A private name is delegated rather than refused: ``_pool`` is how the
        in-memory doubles reach their own storage, and it is not part of the
        capability contract either way.
        """
        self._fake = fake
        self._port = port
        self._allowed = _protocol_methods(port)

    def __getattr__(self, name: str) -> Any:
        """Return the double's attribute, or refuse a method outside the port.

        The refusal is on the *port*, not on whether the double happens to
        implement the name. That distinction is the whole point: the doubles are
        all-purpose, so a check that consulted them would answer for every
        capability and enforce nothing.
        """
        if name.startswith("_"):
            return getattr(self._fake, name)
        if name not in self._allowed:
            raise AttributeError(
                f"{name!r} is not part of {self._port}: this call used a capability "
                f"it was not given"
            )
        return getattr(self._fake, name)


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
        # An unconfigured deployment wires None itself; wrapping it would hide
        # the None the route is supposed to refuse.
        wired: Any = (
            Boundary(fake, PORT_MODULES[name])
            if name in PORT_MODULES and fake is not None
            else fake
        )
        app.dependency_overrides[GETTERS[name]] = _provider(wired)
