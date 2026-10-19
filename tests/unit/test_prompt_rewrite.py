"""Draft rewriting preserves owner scope and validates model-produced content."""

from __future__ import annotations

import asyncio
import json
from unittest.mock import AsyncMock

import httpx
import pytest
from pydantic import ValidationError

from cloud_agent import prompt_rewrite
from cloud_agent.gateway import GatewayError
from cloud_agent.prompt_rewrite import (
    PromptRewriteError,
    PromptRewriteRequest,
    rewrite_prompt,
)


@pytest.fixture(autouse=True)
def gateway_url(monkeypatch):
    monkeypatch.setenv("GATEWAY_BASE_URL", "https://gateway.example/")


@pytest.fixture()
def gateway():
    value = AsyncMock()
    value.model_catalog_with_aliases.return_value = (["allowed-model"], {"alias": "allowed-model"})
    return value


@pytest.fixture()
def grants():
    value = AsyncMock()
    value.mint.return_value = {
        "grant_id": "rewrite-grant",
        "token": "scoped-inference-secret",
        "allowed_models": ["allowed-model"],
    }
    return value


def completion(result, *, finish_reason="stop"):
    return httpx.Response(
        200,
        json={
            "choices": [
                {"message": {"content": json.dumps(result)}, "finish_reason": finish_reason}
            ]
        },
    )


async def test_direct_rewrite_uses_owner_model_scope_and_separate_operation(gateway, grants):
    requests = []

    def handle(request):
        requests.append(request)
        return completion({"kind": "prompt", "prompt": "Build an accessible sign-in form."})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
        result = await rewrite_prompt(
            PromptRewriteRequest(prompt="Build login", model="alias"),
            user_id="owner-1",
            gateway=gateway,
            grants=grants,
            client=client,
        )

    assert result.model_dump() == {
        "kind": "prompt",
        "prompt": "Build an accessible sign-in form.",
    }
    gateway.model_catalog_with_aliases.assert_awaited_once_with("owner-1")
    mint = grants.mint.await_args.kwargs
    assert mint["user_id"] == "owner-1"
    assert mint["job_id"].startswith("prompt-rewrite-")
    assert mint["attempt_id"]
    assert mint["models"] == ["allowed-model"]
    assert mint["ttl_seconds"] == 120
    grants.revoke.assert_awaited_once_with("rewrite-grant")
    assert len(requests) == 1
    request = requests[0]
    assert str(request.url) == "https://gateway.example/v1/chat/completions"
    assert request.headers["Authorization"] == "Bearer scoped-inference-secret"
    body = json.loads(request.content)
    assert body["model"] == "allowed-model"
    assert body["stream"] is False
    assert "tools" not in body
    assert json.loads(body["messages"][1]["content"]) == {
        "action": "rewrite",
        "prompt": "Build login",
        "answers": [],
    }


async def test_clarifications_then_answers_force_a_final_prompt(gateway, grants):
    questions = ["Who will use the form?", "Which sign-in methods should it support?"]
    sources = []

    def handle(request):
        source = json.loads(json.loads(request.content)["messages"][1]["content"])
        sources.append(source)
        if source["action"] == "questions":
            return completion({"kind": "questions", "questions": questions})
        return completion({"kind": "prompt", "prompt": "Build a passwordless form for customers."})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
        first = await rewrite_prompt(
            PromptRewriteRequest(prompt="Build login", model="allowed-model", clarify=True),
            user_id="owner-1",
            gateway=gateway,
            grants=grants,
            client=client,
        )
        second = await rewrite_prompt(
            PromptRewriteRequest(
                prompt="Build login",
                model="allowed-model",
                clarify=True,
                answers=[
                    {"question": questions[0], "answer": "Customers"},
                    {"question": questions[1], "answer": "Passwordless"},
                ],
            ),
            user_id="owner-1",
            gateway=gateway,
            grants=grants,
            client=client,
        )

    assert first.kind == "questions"
    assert first.questions == questions
    assert second.kind == "prompt"
    assert [source["action"] for source in sources] == ["questions", "rewrite"]
    assert sources[1]["answers"][1]["answer"] == "Passwordless"
    operations = [call.kwargs["job_id"] for call in grants.mint.await_args_list]
    assert len(set(operations)) == 2
    assert grants.revoke.await_count == 2


@pytest.mark.parametrize(
    "result",
    [
        {"kind": "questions", "questions": ["One?", "Two?"]},
        {"kind": "prompt", "prompt": "   "},
        {"kind": "prompt", "prompt": "x" * 32001},
        {"kind": "prompt", "prompt": "Useful draft", "unexpected": True},
        {"kind": "prompt", "prompt": 123},
        {"kind": "other", "prompt": "Useful draft"},
    ],
)
async def test_invalid_final_output_is_refused_and_grant_revoked(gateway, grants, result):
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda _: completion(result))
    ) as client:
        with pytest.raises(PromptRewriteError) as error:
            await rewrite_prompt(
                PromptRewriteRequest(prompt="Build login", model="allowed-model"),
                user_id="owner-1",
                gateway=gateway,
                grants=grants,
                client=client,
            )
    assert error.value.error_type == "invalid_rewrite"
    grants.revoke.assert_awaited_once_with("rewrite-grant")


@pytest.mark.parametrize(
    "questions",
    [["One?"], ["One?"] * 5, ["Same?", "same?"], ["One?", "   "]],
)
async def test_clarification_list_must_be_bounded_distinct_and_nonempty(gateway, grants, questions):
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda _: completion({"kind": "questions", "questions": questions})
        )
    ) as client:
        with pytest.raises(PromptRewriteError, match="complete rewrite"):
            await rewrite_prompt(
                PromptRewriteRequest(prompt="Build login", model="allowed-model", clarify=True),
                user_id="owner-1",
                gateway=gateway,
                grants=grants,
                client=client,
            )
    grants.revoke.assert_awaited_once_with("rewrite-grant")


@pytest.mark.parametrize(
    ("response", "error_type", "status"),
    [
        (httpx.Response(200, json={"choices": []}), "invalid_rewrite", 502),
        (httpx.Response(200, text="not-json secret"), "invalid_rewrite", 502),
        (
            completion({"kind": "prompt", "prompt": "Partial"}, finish_reason="length"),
            "invalid_rewrite",
            502,
        ),
        (
            httpx.Response(
                200,
                json={
                    "choices": [
                        {"message": {"content": "⏳ The model is starting up — please wait."}}
                    ]
                },
            ),
            "model_starting",
            503,
        ),
        (httpx.Response(429, text="secret quota body"), "rewrite_rate_limited", 429),
        (httpx.Response(500, text="secret upstream body"), "rewrite_unavailable", 503),
        (
            httpx.Response(302, headers={"location": "https://untrusted.example"}),
            "rewrite_unavailable",
            503,
        ),
        (httpx.Response(200, content=b"x" * 262145), "rewrite_unavailable", 503),
    ],
)
async def test_upstream_failures_are_sanitized_and_revoke_grant(
    gateway, grants, response, error_type, status, caplog
):
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda _: response)) as client:
        with pytest.raises(PromptRewriteError) as error:
            await rewrite_prompt(
                PromptRewriteRequest(prompt="sensitive original prompt", model="allowed-model"),
                user_id="owner-1",
                gateway=gateway,
                grants=grants,
                client=client,
            )
    assert error.value.error_type == error_type
    assert error.value.status_code == status
    assert "secret" not in str(error.value)
    assert "sensitive original" not in caplog.text
    assert "scoped-inference-secret" not in caplog.text
    grants.revoke.assert_awaited_once_with("rewrite-grant")


@pytest.mark.parametrize(
    "patch",
    [
        {"allowed_models": []},
        {"allowed_models": None},
        {"token": ""},
        {"token": None},
        {"grant_id": ""},
    ],
)
async def test_incomplete_or_narrowed_grant_never_calls_inference(gateway, grants, patch):
    grants.mint.return_value.update(patch)
    client = AsyncMock()
    with pytest.raises(PromptRewriteError):
        await rewrite_prompt(
            PromptRewriteRequest(prompt="Build login", model="allowed-model"),
            user_id="owner-1",
            gateway=gateway,
            grants=grants,
            client=client,
        )
    client.stream.assert_not_called()
    if grants.mint.return_value["grant_id"]:
        grants.revoke.assert_awaited_once_with("rewrite-grant")
    else:
        grants.revoke.assert_not_awaited()


async def test_model_outside_owner_catalog_never_mints_grant(gateway, grants):
    with pytest.raises(PromptRewriteError) as error:
        await rewrite_prompt(
            PromptRewriteRequest(prompt="Build login", model="forbidden-model"),
            user_id="owner-1",
            gateway=gateway,
            grants=grants,
        )
    assert error.value.error_type == "model_not_available"
    grants.mint.assert_not_awaited()


async def test_a_catalog_outage_is_unavailable_not_a_model_refusal(gateway, grants):
    gateway.model_catalog_with_aliases.side_effect = GatewayError("gateway timed out")
    with pytest.raises(PromptRewriteError) as error:
        await rewrite_prompt(
            PromptRewriteRequest(prompt="Build login", model="allowed-model"),
            user_id="owner-1",
            gateway=gateway,
            grants=grants,
        )
    assert error.value.status_code == 503
    assert error.value.error_type == "rewrite_unavailable"
    grants.mint.assert_not_awaited()


async def test_grant_refusal_is_sanitized(gateway, grants):
    grants.mint.side_effect = GatewayError("sensitive details", status_code=403)
    with pytest.raises(PromptRewriteError) as error:
        await rewrite_prompt(
            PromptRewriteRequest(prompt="Build login", model="allowed-model"),
            user_id="owner-1",
            gateway=gateway,
            grants=grants,
        )
    assert error.value.status_code == 403
    assert "sensitive" not in str(error.value)
    grants.revoke.assert_not_awaited()


@pytest.mark.parametrize("cancel", [False, True])
async def test_timeout_and_cancellation_revoke_the_grant(monkeypatch, gateway, grants, cancel):
    started = asyncio.Event()

    async def handle(_request):
        started.set()
        await asyncio.Event().wait()

    if not cancel:
        monkeypatch.setattr(prompt_rewrite, "_REQUEST_TIMEOUT_SECONDS", 0.01)
    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
        task = asyncio.create_task(
            rewrite_prompt(
                PromptRewriteRequest(prompt="Build login", model="allowed-model"),
                user_id="owner-1",
                gateway=gateway,
                grants=grants,
                client=client,
            )
        )
        if cancel:
            await started.wait()
            task.cancel()
        with pytest.raises(asyncio.CancelledError if cancel else PromptRewriteError):
            await asyncio.wait_for(task, timeout=1)
    grants.revoke.assert_awaited_once_with("rewrite-grant")


@pytest.mark.parametrize(
    "patch",
    [
        {"prompt": "   "},
        {"prompt": "x" * 32001},
        {"model": ""},
        {"clarify": "true"},
        {"answers": [{"question": "?", "answer": ""}] * 5},
        {"answers": [{"question": "", "answer": "answer"}]},
        {"answers": [{"question": "?", "answer": "x" * 5001}]},
        {"user_id": "other-owner"},
    ],
)
def test_rewrite_input_is_strict_and_bounded(patch):
    with pytest.raises(ValidationError):
        PromptRewriteRequest.model_validate(
            {"prompt": "Build login", "model": "allowed-model", **patch}
        )
