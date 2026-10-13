"""Regression tests for the gateway's private sandbox-manager client."""

from __future__ import annotations

import asyncio
import base64

import httpx
import pytest

from cloud_agent import sandbox_manager_client
from cloud_agent.sandbox_manager_client import SandboxManagerClient, SandboxManagerError


@pytest.mark.asyncio
@pytest.mark.parametrize("thread_id", [None, "origin/thread?member=other&extra=value #fragment"])
async def test_sandbox_inventory_selection_is_encoded_without_changing_authentication(thread_id):
    requests: list[httpx.Request] = []
    rows = [{"sandbox_id": "asbx_shared", "terminal_count": 2, "group_busy": True}]

    def inventory(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json={"sandboxes": rows})

    async with httpx.AsyncClient(transport=httpx.MockTransport(inventory)) as client:
        sandbox_manager = SandboxManagerClient("http://sandbox_manager/", "secret", client=client)
        assert await sandbox_manager.list_sandboxes(thread_id=thread_id, timeout=5) == rows
        assert client.is_closed is False

    request = requests[0]
    assert request.method == "GET"
    assert request.url.path == "/sandboxes"
    assert request.headers["X-Agent-Workspace-Token"] == "secret"
    assert request.extensions["timeout"]["read"] == 5
    if thread_id is None:
        assert str(request.url) == "http://sandbox_manager/sandboxes"
    else:
        assert list(request.url.params.multi_items()) == [("thread_id", thread_id)]
        assert request.url.fragment == ""


@pytest.mark.asyncio
@pytest.mark.parametrize("thread_id", [None, "athr_origin"])
@pytest.mark.parametrize(
    "status,body,error_status",
    [
        (401, {"detail": "PRIVATE credential diagnostic"}, 503),
        (500, {"detail": "PRIVATE container diagnostic"}, 503),
        (200, {"sandboxes": [{"thread_id": "athr_origin"}]}, 502),
        (200, {"unrelated": []}, 502),
    ],
)
async def test_sandbox_inventory_selection_preserves_fail_closed_validation(
    thread_id, status, body, error_status
):
    requests: list[httpx.Request] = []

    def inventory(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(status, json=body)

    async with httpx.AsyncClient(transport=httpx.MockTransport(inventory)) as client:
        sandbox_manager = SandboxManagerClient("http://sandbox_manager", "secret", client=client)
        with pytest.raises(SandboxManagerError) as failure:
            await sandbox_manager.list_sandboxes(thread_id=thread_id)

    assert failure.value.status_code == error_status
    assert "PRIVATE" not in failure.value.message
    assert len(requests) == 1


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
        "disk_limit_mib": 16384,
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
@pytest.mark.parametrize(
    ("method_name", "suffix", "timeout"),
    [
        ("prepare_sandbox_deletion", "prepare", 1800),
        ("upload_sandbox_deletion_archive", "upload", 1800),
        ("commit_sandbox_deletion", "commit", 120),
        ("abort_sandbox_deletion", "abort", 120),
    ],
)
async def test_archive_delete_calls_stay_on_the_authenticated_shared_sandbox_manager(
    monkeypatch, method_name, suffix, timeout
):
    sandbox_manager = SandboxManagerClient("http://sandbox_manager", "secret")
    calls = []
    payload = {
        "request_id": "delete-one",
        "generation": 4,
        "expected_container_id": "container-one",
        "archive_key": "sandbox-archives/group/delete-one/archive.tar",
    }

    async def request(method, thread_id, action, *, json, timeout):
        calls.append((method, thread_id, action, json, timeout))
        return {"request_id": "delete-one"}

    monkeypatch.setattr(sandbox_manager, "_shared_sandbox_request", request)
    assert await getattr(sandbox_manager, method_name)("origin/thread", payload) == {
        "request_id": "delete-one"
    }
    assert calls == [("POST", "origin/thread", f"admin-delete/{suffix}", payload, timeout)]


@pytest.mark.asyncio
async def test_native_question_client_keeps_read_and_response_scoped_to_the_owner(monkeypatch):
    sandbox_manager = SandboxManagerClient("http://sandbox_manager", "secret")
    calls: list[tuple] = []

    async def request(method, thread_id, action, *, params=None, json=None, timeout=None):
        calls.append((method, thread_id, action, params, json, timeout))
        return {"questions": []} if method == "GET" else {"accepted": True, "reason": None}

    monkeypatch.setattr(sandbox_manager, "_shared_sandbox_request", request)
    answers = {"approach": ["First", "Custom answer"]}
    assert await sandbox_manager.pending_shared_questions("athr_source", owner="attempt:4") == {
        "questions": []
    }
    assert await sandbox_manager.answer_shared_question(
        "athr_source", owner="attempt:4", request_id="request-one", answers=answers
    ) == {"accepted": True, "reason": None}
    await sandbox_manager.answer_shared_question(
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

    monkeypatch.setattr(sandbox_manager_client.httpx, "AsyncClient", make_client)
    sandbox_manager = SandboxManagerClient("http://sandbox_manager", "secret")
    task = asyncio.create_task(sandbox_manager.stream_terminal("ajob_test", "term_test", after=0))
    await asyncio.sleep(0)
    await created[0].started.wait()

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert created[0].closed is True


@pytest.mark.asyncio
async def test_shared_snapshot_client_forwards_only_the_reviewed_identity(monkeypatch):
    sandbox_manager = SandboxManagerClient("http://sandbox_manager", "secret")
    calls: list[tuple] = []

    async def request(method, thread_id, action, *, json=None, timeout=None):
        calls.append((method, thread_id, action, json, timeout))
        return {"ok": True}

    monkeypatch.setattr(sandbox_manager, "_shared_sandbox_request", request)
    snapshot = _shared_snapshot()

    assert await sandbox_manager.capture_shared_snapshot(snapshot) == {"ok": True}
    assert await sandbox_manager.import_shared_base(
        snapshot,
        commit_sha="b" * 40,
        bundle=b"bundle",
    ) == {"ok": True}

    capture = calls[0]
    assert capture[:3] == ("POST", "athr_origin", "snapshot")
    assert capture[3]["workspace_epoch"] == 4
    assert capture[3]["base_sha"] == "a" * 40
    assert capture[3]["expected_container_id"] == "container-shared"
    assert capture[3]["disk_limit_mib"] == 16384
    assert "user_id" not in capture[3] and "repo" not in capture[3]
    imported = calls[1]
    assert imported[:3] == ("POST", "athr_origin", "import-base")
    assert imported[3]["commit_sha"] == "b" * 40
    assert imported[3]["disk_limit_mib"] == 16384
    assert imported[3]["bundle_b64"] == base64.b64encode(b"bundle").decode()


@pytest.mark.asyncio
@pytest.mark.parametrize("plugins, timeout", [([], 180), ([{"plugin": "example"}], 960)])
async def test_shared_fork_client_forwards_the_group_origin(monkeypatch, plugins, timeout):
    sandbox_manager = SandboxManagerClient("http://sandbox_manager", "secret")
    calls: list[tuple] = []

    async def request(method, thread_id, action, *, json=None, timeout=None):
        calls.append((method, thread_id, action, json, timeout))
        return {"ok": True}

    monkeypatch.setattr(sandbox_manager, "_shared_sandbox_request", request)

    result = await sandbox_manager.fork_shared_session(
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
        disk_limit_mib=16384,
        stop_timeout_s=10,
        members=[],
        operation_id="asfo_test",
        source_native_session_id="native-source",
        source_native_turn_no=2,
        target_thread_id="athr_target",
        target_slot_id=1,
        source_launch={"plugins": plugins},
        target_launch={"plugins": plugins},
    )

    assert result == {"ok": True}
    call = calls[0]
    assert call[:3] == ("POST", "athr_source", "fork")
    assert call[3]["origin_thread_id"] == "athr_origin"
    assert call[3]["source_native_turn_no"] == 2
    assert call[3]["disk_limit_mib"] == 16384
    assert call[3]["source_launch"]["plugins"] == plugins
    assert call[3]["target_launch"]["plugins"] == plugins
    assert call[4] == timeout


@pytest.mark.asyncio
async def test_shared_registration_projects_db_manifest_before_sandbox_manager_recovery(
    monkeypatch,
):
    sandbox_manager = SandboxManagerClient("http://sandbox_manager", "secret")
    calls: list[tuple] = []

    async def request(method, thread_id, action, *, json=None, timeout=None):
        calls.append((method, thread_id, action, json, timeout))
        return {"ok": True}

    monkeypatch.setattr(sandbox_manager, "_shared_sandbox_request", request)
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

    assert await sandbox_manager.register_shared_sandbox(sandbox) == {"ok": True}
    call = calls[0]
    assert call[:3] == ("POST", "athr_origin", "register")
    assert call[3]["expected_container_id"] == "container-shared"
    assert call[3]["members"] == snapshot["members"]
    assert call[3]["disk_limit_mib"] == 16384
    assert call[4] is None


@pytest.mark.asyncio
async def test_terminal_coordination_identifies_the_turn_owning_each_suspension(monkeypatch):
    sandbox_manager = SandboxManagerClient("http://sandbox_manager", "secret")
    calls: list[tuple] = []

    async def request(method, workspace_id, action, *, params=None, json=None, timeout=30):
        calls.append((method, workspace_id, action, params, json, timeout))
        return {"ok": True}

    monkeypatch.setattr(sandbox_manager, "_request", request)

    await sandbox_manager.suspend_terminals(
        "athr_shared",
        lease_generation=2,
        job_id="ajob_parent",
    )
    await sandbox_manager.resume_terminals(
        "athr_shared",
        lease_generation=2,
        job_id="ajob_parent",
    )
    await sandbox_manager.resume_settled_terminals(
        "athr_shared",
        job_id="ajob_parent",
    )

    assert [call[4] for call in calls] == [
        {"lease_generation": 2, "job_id": "ajob_parent"},
        {"lease_generation": 2, "job_id": "ajob_parent"},
        {"job_id": "ajob_parent"},
    ]


def _counting_body(chunks: int, size: int = 64 * 1024):
    """An async body that records how far a reader pulled it."""
    pulled = [0]

    async def body():
        for _ in range(chunks):
            pulled[0] += 1
            yield b" " * size

    return body, pulled


def _broker(handler) -> SandboxManagerClient:
    return SandboxManagerClient(
        "http://broker",
        "secret",
        client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("call", "limit_name"),
    [
        (lambda broker: broker.files("athr_one", "src"), "MAX_SANDBOX_MANAGER_CONTENT_BYTES"),
        (lambda broker: broker.list_terminals("athr_one"), "MAX_SANDBOX_MANAGER_CONTROL_BYTES"),
        (
            lambda broker: broker.capture_shared_snapshot(_shared_snapshot()),
            "MAX_SANDBOX_MANAGER_CONTENT_BYTES",
        ),
        (
            lambda broker: broker.shared_chat_sandbox_status("athr_origin"),
            "MAX_SANDBOX_MANAGER_CONTROL_BYTES",
        ),
    ],
)
async def test_an_oversized_broker_reply_is_refused_before_it_is_buffered(
    monkeypatch, call, limit_name
):
    """The host decides how much it sends; this process decides how much it keeps."""
    monkeypatch.setattr(sandbox_manager_client, limit_name, 1024 * 1024)
    body, pulled = _counting_body(64)

    def respond(request):
        assert request.headers["accept-encoding"] == "identity"
        return httpx.Response(200, content=body())

    with pytest.raises(SandboxManagerError) as refused:
        await call(_broker(respond))
    assert refused.value.status_code == 502
    assert "more data than the control plane accepts" in refused.value.message
    # 1 MiB is 16 chunks; the 17th is refused instead of kept.
    assert pulled[0] == 17


@pytest.mark.asyncio
async def test_an_oversized_inventory_is_refused(monkeypatch):
    monkeypatch.setattr(sandbox_manager_client, "MAX_SANDBOX_MANAGER_CONTROL_BYTES", 1024 * 1024)
    body, pulled = _counting_body(64)
    broker = _broker(lambda request: httpx.Response(200, content=body()))

    with pytest.raises(SandboxManagerError) as refused:
        await broker.list_sandboxes()
    assert refused.value.status_code == 502
    assert pulled[0] == 17


@pytest.mark.asyncio
async def test_an_oversized_error_reply_keeps_its_status_and_drops_its_detail(monkeypatch):
    monkeypatch.setattr(sandbox_manager_client, "MAX_SANDBOX_MANAGER_CONTROL_BYTES", 1024 * 1024)
    body, pulled = _counting_body(64)
    broker = _broker(lambda request: httpx.Response(409, content=body()))

    with pytest.raises(SandboxManagerError) as refused:
        await broker.list_terminals("athr_one")
    assert (refused.value.status_code, refused.value.message) == (
        409,
        "The live workspace request failed.",
    )
    assert pulled[0] == 17


@pytest.mark.asyncio
async def test_a_health_probe_never_reads_a_success_body():
    body, pulled = _counting_body(64)
    broker = _broker(lambda request: httpx.Response(200, content=body()))

    await broker.health()
    assert pulled[0] == 0


@pytest.mark.asyncio
async def test_a_compressed_broker_reply_is_refused_before_decoding():
    broker = _broker(
        lambda request: httpx.Response(
            200, content=b"\x1f\x8b not json", headers={"Content-Encoding": "gzip"}
        )
    )

    with pytest.raises(SandboxManagerError) as refused:
        await broker.files("athr_one", "src")
    assert refused.value.status_code == 503


@pytest.mark.asyncio
async def test_a_bounded_reply_still_decodes_and_errors_keep_their_detail():
    def respond(request):
        if request.url.path.endswith("/files"):
            return httpx.Response(200, json={"kind": "directory", "entries": []})
        return httpx.Response(404, json={"detail": "terminal not found"})

    broker = _broker(respond)
    assert await broker.files("athr_one", "") == {"kind": "directory", "entries": []}
    with pytest.raises(SandboxManagerError) as missing:
        await broker.list_terminals("athr_one")
    assert (missing.value.status_code, missing.value.message) == (404, "terminal not found")
