"""The contract every launch facet keeps, and what its trusted step is handed."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Protocol, Self

if TYPE_CHECKING:
    from collections.abc import Mapping

    from cloud_agent_host.container.chat_sandbox import ChatSandboxManager, ChatSandboxSpec


@dataclass(frozen=True)
class PrepareContext:
    """Where a facet's trusted step runs: one member slot of one sandbox."""

    manager: ChatSandboxManager
    spec: ChatSandboxSpec
    slot_id: int


class LaunchFacet(Protocol):
    """Everything one platform capability adds to one member's CLI launch.

    Facets subclass this explicitly and inherit its defaults: no
    process-fixed value, no trusted step, no environment, no secret.
    """

    def fingerprint(self) -> Mapping[str, object]:
        """The process-fixed values, by the name a refused reuse reports.

        A name whose value differs between the live process and the offered
        launch needs a new CLI process (the native session still resumes).
        The names are recorded; the values never are, so they may be
        credentials. Values are compared by equality.
        """
        return {}

    def differs_from(self, held: Self) -> tuple[str, ...]:
        """The names keeping this offered launch from borrowing ``held``'s process.

        By default, the names whose fingerprint differs. A facet whose live
        process may serve an offer that carries less -- a credential-less
        probe borrowing a residency's credential -- narrows this.
        """
        mine, theirs = self.fingerprint(), held.fingerprint()
        return tuple(name for name, value in mine.items() if theirs.get(name) != value)

    def prepare(self, context: PrepareContext) -> None:
        """What the trusted host does in the container before the CLI starts."""
        return

    def env(self) -> dict[str, str]:
        """The variables this capability gives the member's CLI."""
        return {}

    def secrets(self) -> dict[str, str]:
        """The values, by name, this capability brings into the sandbox.

        Output must never show them. The runner's redactor still takes its
        values from the claim (``claim_secret_values``); this states the
        launch's own half, which is not yet wired into it.
        """
        return {}
