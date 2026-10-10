"""Native questions reach only the owner and the attempt that asked them."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from cloud_agent.api import turns
from cloud_agent.api._owner_auth import require_agent_owner
from cloud_agent.api.deps import get_storage
from cloud_agent.workspace_broker_client import WorkspaceBrokerError

pytestmark = pytest.mark.asyncio

_PATH = "/v1/agent/threads/thread-1/questions"
_QUESTION = {
    "request_id": "native-request-1",
    "questions": [
        {
            "id": "target",
            "header": "Target",
            "question": "Which target should I use?",
            "multiple": False,
            "options": [
                {"label": "Development", "description": "Use the development environment."}
            ],
            "allow_custom": True,
        }
    ],
}
_ANSWER = {
    "job_id": "job-1",
    "attempt_id": 41,
    "request_id": "native-request-1",
    "answers": {"target": ["Development"]},
}


@pytest.fixture
def api(monkeypatch):
    latest = {
        "id": "job-1",
        "user_id": "owner-1",
        "state": "running",
        "current_attempt_id": 41,
        "cancel_requested": False,
    }
    broker = SimpleNamespace(
        pending_shared_questions=AsyncMock(return_value={"questions": [_QUESTION]}),
        answer_shared_question=AsyncMock(return_value={"accepted": True, "reason": None}),
    )
    store = SimpleNamespace(
        conversation_queries=SimpleNamespace(latest_turn=AsyncMock(side_effect=lambda **_: latest)),
        host_pool=object(),
    )
    resolve = AsyncMock(return_value=broker)
    monkeypatch.setattr(turns, "broker_for_attempt", resolve)
    app = FastAPI()
    app.include_router(turns.router)
    app.dependency_overrides[require_agent_owner] = lambda: {"user_id": "owner-1"}
    app.dependency_overrides[get_storage] = lambda: store
    return SimpleNamespace(app=app, latest=latest, broker=broker, store=store, resolve=resolve)


async def test_questions_are_read_from_the_exact_running_attempt(api):
    async with AsyncClient(transport=ASGITransport(app=api.app), base_url="http://test") as client:
        response = await client.get(_PATH)

    assert response.status_code == 200, response.text
    assert response.json() == {"job_id": "job-1", "attempt_id": 41, "questions": [_QUESTION]}
    api.resolve.assert_awaited_once_with(api.store.host_pool, 41)
    api.broker.pending_shared_questions.assert_awaited_once_with("thread-1", owner="attempt:41")


async def test_answers_resume_the_native_request_without_adding_a_message(api):
    async with AsyncClient(transport=ASGITransport(app=api.app), base_url="http://test") as client:
        response = await client.post(_PATH, json=_ANSWER)

    assert response.status_code == 200, response.text
    assert response.json() == {"accepted": True, "reason": None}
    api.broker.answer_shared_question.assert_awaited_once_with(
        "thread-1",
        owner="attempt:41",
        request_id="native-request-1",
        answers={"target": ["Development"]},
        reject=False,
    )


async def test_skip_is_an_explicit_native_rejection(api):
    async with AsyncClient(transport=ASGITransport(app=api.app), base_url="http://test") as client:
        response = await client.post(_PATH, json={**_ANSWER, "answers": {}, "reject": True})

    assert response.status_code == 200, response.text
    assert api.broker.answer_shared_question.await_args.kwargs["reject"] is True
    assert api.broker.answer_shared_question.await_args.kwargs["answers"] == {}


@pytest.mark.parametrize(
    "change", [{"state": "succeeded"}, {"cancel_requested": True}, {"current_attempt_id": None}]
)
async def test_questions_disappear_when_no_live_turn_can_answer_them(api, change):
    api.latest.update(change)
    async with AsyncClient(transport=ASGITransport(app=api.app), base_url="http://test") as client:
        response = await client.get(_PATH)

    assert response.status_code == 200, response.text
    assert response.json()["questions"] == []
    api.resolve.assert_not_awaited()


@pytest.mark.parametrize(
    "change",
    [{"id": "job-2"}, {"current_attempt_id": 42}, {"state": "failed"}, {"cancel_requested": True}],
)
async def test_a_stale_popup_never_answers_a_later_turn(api, change):
    api.latest.update(change)
    async with AsyncClient(transport=ASGITransport(app=api.app), base_url="http://test") as client:
        response = await client.post(_PATH, json=_ANSWER)

    assert response.status_code == 409, response.text
    assert response.json()["detail"]["error"]["type"] == "question_expired"
    api.resolve.assert_not_awaited()
    api.broker.answer_shared_question.assert_not_awaited()


@pytest.mark.parametrize("change", [{"user_id": "another-owner"}, {"deleted_at": "deleted"}])
async def test_other_owners_and_deleted_threads_expose_neither_questions_nor_answers(api, change):
    api.latest.update(change)
    async with AsyncClient(transport=ASGITransport(app=api.app), base_url="http://test") as client:
        read = await client.get(_PATH)
        answer = await client.post(_PATH, json=_ANSWER)

    assert read.status_code == answer.status_code == 404
    api.resolve.assert_not_awaited()


async def test_a_missing_thread_is_not_disclosed(api):
    api.store.conversation_queries.latest_turn.side_effect = None
    api.store.conversation_queries.latest_turn.return_value = None
    async with AsyncClient(transport=ASGITransport(app=api.app), base_url="http://test") as client:
        assert (await client.get(_PATH)).status_code == 404
        assert (await client.post(_PATH, json=_ANSWER)).status_code == 404
    api.resolve.assert_not_awaited()


async def test_the_question_routes_require_an_owner_session(api):
    api.app.dependency_overrides.pop(require_agent_owner)
    async with AsyncClient(transport=ASGITransport(app=api.app), base_url="http://test") as client:
        assert (await client.get(_PATH)).status_code == 401
        assert (await client.post(_PATH, json=_ANSWER)).status_code == 401
    api.resolve.assert_not_awaited()


@pytest.mark.parametrize("missing", [True, False])
async def test_an_unreachable_host_is_not_reported_as_having_no_questions(api, missing):
    if missing:
        api.resolve.return_value = None
    else:
        api.broker.pending_shared_questions.side_effect = WorkspaceBrokerError(503, "unreachable")
        api.broker.answer_shared_question.side_effect = WorkspaceBrokerError(503, "unreachable")
    async with AsyncClient(transport=ASGITransport(app=api.app), base_url="http://test") as client:
        assert (await client.get(_PATH)).status_code == 503
        assert (await client.post(_PATH, json=_ANSWER)).status_code == 503


async def test_a_question_already_answered_in_another_tab_preserves_the_refusal(api):
    api.broker.answer_shared_question.return_value = {"accepted": False, "reason": "turn_ended"}
    async with AsyncClient(transport=ASGITransport(app=api.app), base_url="http://test") as client:
        response = await client.post(_PATH, json=_ANSWER)
    assert response.json() == {"accepted": False, "reason": "turn_ended"}


@pytest.mark.parametrize(
    "pending", [{}, {"questions": [None]}, {"questions": [{"request_id": "q"}]}]
)
async def test_invalid_host_question_data_is_a_transport_error(api, pending):
    api.broker.pending_shared_questions.return_value = pending
    async with AsyncClient(transport=ASGITransport(app=api.app), base_url="http://test") as client:
        response = await client.get(_PATH)
    assert response.status_code == 502, response.text


@pytest.mark.parametrize(
    "change",
    [
        {"attempt_id": None},
        {"request_id": ""},
        {"answers": {"target": ["x" * 10_001]}},
        {"answers": {"target": ["x"] * 21}},
        {"answers": {str(i): ["x"] for i in range(11)}},
        {"answers": {"target": "Development"}},
    ],
)
async def test_answer_payloads_are_bounded_before_the_host_receives_them(api, change):
    async with AsyncClient(transport=ASGITransport(app=api.app), base_url="http://test") as client:
        response = await client.post(_PATH, json={**_ANSWER, **change})
    assert response.status_code == 422, response.text
    api.broker.answer_shared_question.assert_not_awaited()
