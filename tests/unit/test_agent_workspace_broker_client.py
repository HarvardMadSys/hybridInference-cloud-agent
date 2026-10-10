"""Regression tests for the gateway's private workspace-broker client."""

from __future__ import annotations

import asyncio
import base64

import pytest

from cloud_agent import workspace_broker_client
from cloud_agent.workspace_broker_client import WorkspaceBrokerClient


def _shared_snapshot() -> dict:
    return {
        "snapshot_id": 7,
        "sandbox_id": "asbx_shared",
        "generation": 2,
        "origin_thread_id": "athr_origin",
        "container_id": "container-shared",
        "runtime_id": "codex",
        "runtime_version": "0.145.0",
        "sandbox_image_digest": "sha256:image",
        "resident_slot_capacity": 8,
        "memory_limit": "4g",
        "cpu_limit": "2",
        "stop_timeout_seconds": 10,
        "workspace_epoch": 4,
        "base_sha": "a" * 40,
        "members": [
            {
                "thread_id": "athr_origin",
                "native_session_id": "native-one",
                "context_status": "live",
                "activation_generation": 3,
                "process_generation": 2,
                "residency_generation": 5,
                "resident_state": "live",
                "resident_slot_id": 0,
                "last_settled_job_id": "ajob_one",
            }
        ],
    }


@pytest.mark.asyncio
async def test_native_question_client_keeps_read_and_response_scoped_to_the_owner(monkeypatch):
    broker = WorkspaceBrokerClient("http://broker", "secret")
    calls: list[tuple] = []

    async def request(method, thread_id, action, *, params=None, json=None, timeout=None):
        calls.append((method, thread_id, action, params, json, timeout))
        return {"questions": []} if method == "GET" else {"accepted": True, "reason": None}

    monkeypatch.setattr(broker, "_shared_sandbox_request", request)
    answers = {"approach": ["First", "Custom answer"]}
    assert await broker.pending_shared_questions("athr_source", owner="attempt:4") == {
        "questions": []
    }
    assert await broker.answer_shared_question(
        "athr_source", owner="attempt:4", request_id="request-one", answers=answers
    ) == {"accepted": True, "reason": None}
    await broker.answer_shared_question(
        "athr_source", owner="attempt:4", request_id="request-two", answers={}, reject=True
    )
    assert calls == [
        ("GET", "athr_source", "questions", {"owner": "attempt:4"}, None, 30),
        (
            "POST",
            "athr_source",
            "questions",
            None,
            {
                "owner": "attempt:4",
                "request_id": "request-one",
                "answers": answers,
                "reject": False,
            },
            30,
        ),
        (
            "POST",
            "athr_source",
            "questions",
            None,
            {"owner": "attempt:4", "request_id": "request-two", "answers": {}, "reject": True},
            30,
        ),
    ]


@pytest.mark.asyncio
async def test_cancelled_terminal_connect_closes_its_dedicated_http_client(monkeypatch):
    """Cancelling during ``send`` must not leak the unowned streaming client."""

    class _BlockedClient:
        def __init__(self, *args, **kwargs) -> None:
            self.started = asyncio.Event()
            self.closed = False

        @staticmethod
        def build_request(*args, **kwargs):
            return object()

        async def send(self, request, *, stream: bool):
            self.started.set()
            await asyncio.Event().wait()

        async def aclose(self) -> None:
            self.closed = True

    created: list[_BlockedClient] = []

    def make_client(*args, **kwargs):
        client = _BlockedClient(*args, **kwargs)
        created.append(client)
        return client

    monkeypatch.setattr(workspace_broker_client.httpx, "AsyncClient", make_client)
    broker = WorkspaceBrokerClient("http://broker", "secret")
    task = asyncio.create_task(broker.stream_terminal("ajob_test", "term_test", after=0))
    await asyncio.sleep(0)
    await created[0].started.wait()

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert created[0].closed is True


@pytest.mark.asyncio
async def test_shared_snapshot_client_forwards_only_the_reviewed_identity(monkeypatch):
    broker = WorkspaceBrokerClient("http://broker", "secret")
    calls: list[tuple] = []

    async def request(method, thread_id, action, *, json=None, timeout=None):
        calls.append((method, thread_id, action, json, timeout))
        return {"ok": True}

    monkeypatch.setattr(broker, "_shared_sandbox_request", request)
    snapshot = _shared_snapshot()

    assert await broker.capture_shared_snapshot(snapshot) == {"ok": True}
    assert await broker.import_shared_base(
        snapshot,
        commit_sha="b" * 40,
        bundle=b"bundle",
    ) == {"ok": True}

    capture = calls[0]
    assert capture[:3] == ("POST", "athr_origin", "snapshot")
    assert capture[3]["workspace_epoch"] == 4
    assert capture[3]["base_sha"] == "a" * 40
    assert capture[3]["expected_container_id"] == "container-shared"
    assert "user_id" not in capture[3] and "repo" not in capture[3]
    imported = calls[1]
    assert imported[:3] == ("POST", "athr_origin", "import-base")
    assert imported[3]["commit_sha"] == "b" * 40
    assert imported[3]["bundle_b64"] == base64.b64encode(b"bundle").decode()


@pytest.mark.asyncio
async def test_shared_fork_client_forwards_the_group_origin(monkeypatch):
    broker = WorkspaceBrokerClient("http://broker", "secret")
    calls: list[tuple] = []

    async def request(method, thread_id, action, *, json=None, timeout=None):
        calls.append((method, thread_id, action, json, timeout))
        return {"ok": True}

    monkeypatch.setattr(broker, "_shared_sandbox_request", request)

    result = await broker.fork_shared_session(
        "athr_source",
        sandbox_id="asbx_shared",
        generation=2,
        origin_thread_id="athr_origin",
        runtime_id="codex",
        runtime_version="0.145.0",
        sandbox_image_digest="sha256:image",
        expected_container_id="container-shared",
        resident_slot_capacity=8,
        memory_limit="4g",
        cpu_limit="2",
        stop_timeout_s=10,
        members=[],
        operation_id="asfo_test",
        source_native_session_id="native-source",
        source_native_turn_no=2,
        target_thread_id="athr_target",
        target_slot_id=1,
        source_launch={},
        target_launch={},
    )

    assert result == {"ok": True}
    call = calls[0]
    assert call[:3] == ("POST", "athr_source", "fork")
    assert call[3]["origin_thread_id"] == "athr_origin"
    assert call[3]["source_native_turn_no"] == 2
    assert call[4] == 180


@pytest.mark.asyncio
async def test_shared_registration_projects_db_manifest_before_broker_recovery(monkeypatch):
    broker = WorkspaceBrokerClient("http://broker", "secret")
    calls: list[tuple] = []

    async def request(method, thread_id, action, *, json=None, timeout=None):
        calls.append((method, thread_id, action, json, timeout))
        return {"ok": True}

    monkeypatch.setattr(broker, "_shared_sandbox_request", request)
    snapshot = _shared_snapshot()
    sandbox = {
        **snapshot,
        "shared_live": True,
        "group_members": [
            {
                **snapshot["members"][0],
                "admitted_at": "must-not-cross-the-wire",
                "active_attempt_id": 99,
            }
        ],
    }

    assert await broker.register_shared_sandbox(sandbox) == {"ok": True}
    call = calls[0]
    assert call[:3] == ("POST", "athr_origin", "register")
    assert call[3]["expected_container_id"] == "container-shared"
    assert call[3]["members"] == snapshot["members"]
    assert call[4] is None


@pytest.mark.asyncio
async def test_terminal_coordination_identifies_the_turn_owning_each_suspension(monkeypatch):
    broker = WorkspaceBrokerClient("http://broker", "secret")
    calls: list[tuple] = []

    async def request(method, workspace_id, action, *, params=None, json=None, timeout=30):
        calls.append((method, workspace_id, action, params, json, timeout))
        return {"ok": True}

    monkeypatch.setattr(broker, "_request", request)

    await broker.suspend_terminals(
        "athr_shared",
        lease_generation=2,
        job_id="ajob_parent",
    )
    await broker.resume_terminals(
        "athr_shared",
        lease_generation=2,
        job_id="ajob_parent",
    )
    await broker.resume_settled_terminals(
        "athr_shared",
        job_id="ajob_parent",
    )

    assert [call[4] for call in calls] == [
        {"lease_generation": 2, "job_id": "ajob_parent"},
        {"lease_generation": 2, "job_id": "ajob_parent"},
        {"job_id": "ajob_parent"},
    ]
