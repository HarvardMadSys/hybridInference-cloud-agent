"""Port for deployment agents, their enrollment and their intents."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Protocol

if TYPE_CHECKING:
    from collections.abc import Mapping
    from datetime import datetime


class DeploymentAgentRepository(Protocol):
    """Agents, one-time enrollment tokens and the intents created for agents."""

    async def create_enroll_token(
        self, *, token_hash: str, role: str, created_by: str, expires_at: datetime
    ) -> None:
        """Store a one-time token's digest."""
        ...

    async def register_agent(
        self,
        *,
        token_hash: str,
        agent_id: str,
        name: str,
        role: str,
        machine: str,
        credential_hash: str,
        public_key: str,
    ) -> dict[str, Any] | None:
        """Consume an unexpired token of the same role and create the agent, or ``None``.

        Raises ``ValueError`` when a live agent already has the name; the token
        then stays unspent.
        """
        ...

    async def agent_by_credential(self, credential_hash: str) -> dict[str, Any] | None:
        """The unrevoked agent holding this credential."""
        ...

    async def get_agent(self, agent_id: str) -> dict[str, Any] | None:
        """One agent."""
        ...

    async def list_agents(self) -> list[dict[str, Any]]:
        """Every agent, newest report first."""
        ...

    async def revoke_agent(self, agent_id: str) -> bool:
        """Revoke an agent's credential; its pending intents are cancelled."""
        ...

    async def record_report(
        self, agent_id: str, report: Mapping[str, Any], operations: list[Mapping[str, Any]]
    ) -> None:
        """Store the agent's report and move its intents forward from the journal summary."""
        ...

    async def create_intent(
        self,
        *,
        agent_id: str,
        intent_id: str,
        operation: str,
        params: Mapping[str, Any],
        sealed: Mapping[str, Any],
        idempotency_key: str,
        created_by: str,
        expires_at: datetime,
        workflow_id: str | None = None,
    ) -> dict[str, Any]:
        """Create an intent with the agent's next sequence number, or return the existing one."""
        ...

    async def deliver_intents(self, agent_id: str, after_seq: int) -> list[dict[str, Any]]:
        """Pending intents above ``after_seq``, marked delivered; overdue ones expire."""
        ...

    async def update_intent(
        self,
        *,
        agent_id: str,
        intent_id: str,
        state: str,
        detail: str,
        evidence: Mapping[str, Any],
    ) -> bool:
        """Move one of the agent's intents forward; refuse any other transition."""
        ...

    async def cancel_intent(self, intent_id: str) -> bool:
        """Cancel a pending intent."""
        ...

    async def list_intents(
        self, *, agent_id: str | None = None, workflow_id: str | None = None, limit: int = 100
    ) -> list[dict[str, Any]]:
        """Recent intents, newest first, without their sealed values."""
        ...

    async def create_workflow(
        self,
        *,
        workflow_id: str,
        kind: str,
        step: str,
        params: Mapping[str, Any],
        created_by: str,
    ) -> dict[str, Any] | None:
        """Start a workflow, or ``None`` while another of the same kind is unfinished."""
        ...

    async def get_workflow(self, workflow_id: str) -> dict[str, Any] | None:
        """One workflow."""
        ...

    async def list_workflows(
        self, *, active_only: bool = False, limit: int = 50
    ) -> list[dict[str, Any]]:
        """Recent workflows, newest first."""
        ...

    async def update_workflow(
        self,
        workflow_id: str,
        *,
        expected_version: int,
        state: str,
        step: str,
        detail: str,
        data: Mapping[str, Any],
    ) -> dict[str, Any] | None:
        """Move a workflow on, only from the version the caller read."""
        ...
