"""A waiting message's check tells an unreadable catalog from a refusal.

A refused check holds the message, and a held message stops its queue until
the owner releases it. A model catalog the Gateway could not serve says nothing
about the message, so it has to leave the message waiting for the next
settlement instead of holding it with "Available: none".
"""

from __future__ import annotations

import copy
from types import SimpleNamespace
from typing import Any

import pytest

from cloud_agent.application.pending_messages import prepare_message, settle_queue
from cloud_agent.domain.pending_messages import PreparedHead, SettleOutcome
from cloud_agent.gateway import GatewayError

_PARENT = {
    "id": "job-parent",
    "thread_id": "thread-1",
    "runtime": "claude-code",
    "model": "glm-5.1",
    "repo": None,
    "archived_at": None,
    "thread_sandbox": None,
}


class _Gateway:
    """The Gateway's answers a message check reads, with the catalog up or down."""

    def __init__(self, *, catalog_down: bool = False) -> None:
        self.catalog_down = catalog_down

    async def agent_access(self, user_id: str) -> dict[str, Any]:
        return {"user_id": user_id, "allowed": True, "permissions": ["agent.use"]}

    def _catalog(self) -> None:
        if self.catalog_down:
            raise GatewayError("gateway timed out")

    async def model_catalog(self, user_id: str) -> list[str]:
        self._catalog()
        return ["glm-5.1"]

    async def model_catalog_with_aliases(self, user_id: str) -> tuple[list[str], dict[str, str]]:
        self._catalog()
        return ["glm-5.1"], {}

    async def model_reasoning_efforts(self, user_id: str) -> dict[str, list[str]]:
        self._catalog()
        return {"glm-5.1": ["low", "high"]}


class _Queue:
    """One waiting message in front of an ended turn, settled the way the store does."""

    def __init__(self, message: dict[str, Any]) -> None:
        self.message = message
        self.held: str | None = None
        self.sent: PreparedHead | None = None

    async def settle_queue(
        self, *, thread_id: str, prepared: PreparedHead | None = None
    ) -> SettleOutcome:
        if self.held is not None or self.sent is not None:
            return SettleOutcome()
        if prepared is None:
            return SettleOutcome(head=dict(self.message), parent_job_id=_PARENT["id"])
        if prepared.error is not None:
            self.held = prepared.error
            return SettleOutcome()
        self.sent = prepared
        return SettleOutcome(job={"id": "job-next"}, message_id=prepared.message_id)


class _Queries:
    async def get_job(self, job_id: str) -> dict[str, Any] | None:
        return dict(_PARENT) if job_id == _PARENT["id"] else None


def _message(**choices: Any) -> dict[str, Any]:
    return {
        "id": "message-1",
        "revision": 1,
        "user_id": "owner-1",
        "thread_id": "thread-1",
        "state": "queued",
        "model": None,
        "reasoning_effort": None,
        "skill_ids": None,
        "attachment_ids": None,
        **choices,
    }


def _store(queue: _Queue) -> Any:
    return SimpleNamespace(
        pending_messages=queue,
        conversation_queries=_Queries(),
        attachments=None,
        skills=None,
        source_control=None,
    )


@pytest.mark.parametrize(
    "choices",
    [{"model": "glm-5.1"}, {"reasoning_effort": "high"}],
    ids=["model-switch", "effort-change"],
)
async def test_a_catalog_outage_leaves_the_message_waiting(choices: dict[str, Any]) -> None:
    queue = _Queue(_message(**choices))
    store = _store(queue)
    gateway = _Gateway(catalog_down=True)
    message = copy.deepcopy(queue.message)

    prepared = await prepare_message(
        store,
        message=message,
        parent_job_id=_PARENT["id"],
        gateway=gateway,
        app_credentials=None,
    )
    outcome = await settle_queue(store, thread_id="thread-1", gateway=gateway, app_credentials=None)

    assert prepared is None
    assert message == queue.message
    assert outcome == SettleOutcome()
    assert queue.held is None
    assert queue.sent is None


async def test_a_model_the_readable_catalog_lacks_is_still_held_with_the_reason() -> None:
    queue = _Queue(_message(model="glm-4.6"))

    outcome = await settle_queue(
        _store(queue), thread_id="thread-1", gateway=_Gateway(), app_credentials=None
    )

    assert outcome == SettleOutcome()
    assert queue.sent is None
    assert queue.held is not None
    assert "glm-4.6" in queue.held
    assert "Available: glm-5.1" in queue.held
