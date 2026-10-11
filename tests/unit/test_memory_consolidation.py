"""Memory consolidation validates evidence, bounds, grants and repository scope."""

from __future__ import annotations

import json
from unittest.mock import AsyncMock

import httpx
import pytest

from cloud_agent.memory_consolidation import (
    MemoryConsolidationError,
    MemoryEdit,
    consolidate_memory,
    parse_consolidation,
    validate_memory_edits,
)
from cloud_agent.user_memory import build_project_memory, build_user_memory, prompt_with_user_memory


def profile():
    activity = [
        {
            "job_id": "api-job",
            "repo": "owner/api",
            "model": "model-alias",
            "content": "Always run API tests. I prefer concise replies.",
            "title": "Build API",
        },
        {"job_id": "ui-job", "repo": "owner/ui", "content": "Always run UI tests."},
    ]
    view = build_user_memory(activity, None)
    memories = [
        memory
        for repo in ("owner/api", "owner/ui")
        for memory in build_project_memory(activity, None, repo)["memories"]
    ]
    view["memories"] = sorted(memories, key=lambda memory: memory["kind"] != "preference")
    return view


def scoped_prompt(prompt, view, repo):
    user = {
        **view,
        "memories": [memory for memory in view["memories"] if memory["scope"] == "user"],
    }
    project = {
        **view,
        "repo": repo,
        "memories": [
            memory
            for memory in view["memories"]
            if memory["scope"] == "repo" and memory["source_repo"] == repo
        ],
    }
    return prompt_with_user_memory(prompt, user, repo, project_view=project)


def completion(memories, finish_reason="stop"):
    return {
        "choices": [
            {
                "finish_reason": finish_reason,
                "message": {"content": json.dumps({"memories": memories})},
            }
        ]
    }


def output(source, *, scope="repo", content="Run API tests."):
    return {"source_ids": [source["id"]], "content": content, "scope": scope}


def test_consolidation_classifies_user_preferences_and_retains_repo_isolation():
    original = profile()
    api, style, ui, _task = original["memories"]
    result = parse_consolidation(
        completion(
            [
                output(api),
                output(style, scope="user", content="Keep replies concise."),
                output(ui, content="Run UI tests."),
            ]
        ),
        original["memories"],
    )
    view = {**original, "memories": result}
    api_prompt = scoped_prompt("Task", view, "owner/api")
    assert "Run API tests." in api_prompt and "Keep replies concise." in api_prompt
    assert "Run UI tests." not in api_prompt
    assert "Run API tests." not in scoped_prompt("Task", view, "owner/ui")
    assert "Keep replies concise." in scoped_prompt("Task", view, "unrelated/repo")
    assert result[0]["source_job_id"] == "api-job"
    assert result[0]["source_ids"] == api["source_ids"]


def test_empty_consolidation_is_a_valid_result():
    assert parse_consolidation(completion([]), profile()["memories"]) == []


@pytest.mark.parametrize(
    "bad",
    [
        "invented-id",
        "cross-repo",
        "task-title",
        "duplicate-source",
        "secret",
        "temporary",
        "overlong",
        "too-many-user",
        "total-text",
        "truncated",
        "malformed",
    ],
)
def test_invalid_model_results_are_rejected_whole(bad):
    original = profile()["memories"]
    item = output(original[0])
    items = [item]
    if bad == "invented-id":
        item["source_ids"] = ["other-user-memory"]
    elif bad == "cross-repo":
        item["source_ids"].append(original[2]["id"])
    elif bad == "task-title":
        item["source_ids"] = [original[3]["id"]]
    elif bad == "duplicate-source":
        items.append(item)
    elif bad == "secret":
        item["content"] = "Use password=hunter2"
    elif bad == "temporary":
        item["content"] = "Skip tests for this task."
    elif bad == "overlong":
        item["content"] = "x" * 241
    elif bad in ("too-many-user", "total-text"):
        count = 4 if bad == "too-many-user" else 11
        original = [{**original[0], "id": str(index)} for index in range(count)]
        items = [
            output(source, scope="user" if count == 4 else "repo", content="x" * 240)
            for source in original
        ]
    payload = completion(items, "length" if bad == "truncated" else "stop")
    if bad == "malformed":
        payload["choices"][0]["message"]["content"] = "not JSON"
    with pytest.raises(MemoryConsolidationError) as exc:
        parse_consolidation(payload, original)
    assert exc.value.status_code == 502
    assert "hunter2" not in str(exc.value)


def test_manual_edits_cannot_forge_provenance_and_can_remove_entries():
    original = profile()["memories"]
    edit = MemoryEdit(id=original[0]["id"], content="Run the API checks.", scope="repo")
    result = validate_memory_edits([edit], original)
    assert len(result) == 1
    assert result[0]["source_job_id"] == original[0]["source_job_id"]
    assert result[0]["edited"] is True
    assert validate_memory_edits([], original) == []
    with pytest.raises(ValueError):
        validate_memory_edits([edit, edit], original)
    with pytest.raises(ValueError):
        validate_memory_edits([edit.model_copy(update={"id": "someone-else"})], original)
    with pytest.raises(ValueError):
        validate_memory_edits(
            [edit.model_copy(update={"content": "Use token=privatevalue"})], original
        )


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", [None, "invalid", "quota", "scope", "timeout", "oversize"])
async def test_one_owned_model_call_is_bounded_and_its_grant_is_always_revoked(
    monkeypatch, failure
):
    monkeypatch.setenv("GATEWAY_BASE_URL", "https://gateway.example")
    view = profile()
    gateway = AsyncMock()
    gateway.model_catalog_with_aliases.return_value = (
        ["canonical-model"],
        {"model-alias": "canonical-model"},
    )
    grants = AsyncMock()
    grants.mint.return_value = {
        "grant_id": "grant-1",
        "token": "private-grant",
        "allowed_models": [] if failure == "scope" else ["canonical-model"],
    }
    calls = []

    async def handle(request):
        calls.append(request)
        body = json.loads(request.content)
        assert body["model"] == "canonical-model" and body["stream"] is False
        assert request.headers["Authorization"] == "Bearer private-grant"
        if failure == "timeout":
            raise httpx.ReadTimeout("sensitive context", request=request)
        if failure == "quota":
            return httpx.Response(429)
        if failure == "oversize":
            return httpx.Response(200, content=b"x" * (64 * 1024 + 1))
        return httpx.Response(
            200,
            json=completion(
                [
                    output(
                        view["memories"][0],
                        content="Use password=hunter2"
                        if failure == "invalid"
                        else "Run API tests.",
                    )
                ]
            ),
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
        if failure:
            with pytest.raises(MemoryConsolidationError) as exc:
                await consolidate_memory(
                    view, user_id="alice", gateway=gateway, grants=grants, client=client
                )
            assert "sensitive" not in str(exc.value)
        else:
            result = await consolidate_memory(
                view, user_id="alice", gateway=gateway, grants=grants, client=client
            )
            assert result[0]["content"] == "Run API tests."
    gateway.model_catalog_with_aliases.assert_awaited_once_with("alice")
    mint = grants.mint.await_args.kwargs
    assert mint["user_id"] == "alice" and mint["models"] == ["canonical-model"]
    assert mint["job_id"].startswith("memory-consolidate-") and mint["ttl_seconds"] == 120
    grants.revoke.assert_awaited_once_with("grant-1")
    assert len(calls) == (0 if failure == "scope" else 1)


@pytest.mark.asyncio
async def test_no_permitted_model_does_not_mint_a_grant(monkeypatch):
    monkeypatch.setenv("GATEWAY_BASE_URL", "https://gateway.example")
    gateway, grants = AsyncMock(), AsyncMock()
    gateway.model_catalog_with_aliases.return_value = ([], {})
    with pytest.raises(MemoryConsolidationError):
        await consolidate_memory(profile(), user_id="alice", gateway=gateway, grants=grants)
    grants.mint.assert_not_awaited()


@pytest.mark.parametrize("requested_model", ["chosen-model", "chosen-alias"])
async def test_explicit_model_is_resolved_for_the_owned_grant_and_request(
    monkeypatch, requested_model
):
    monkeypatch.setenv("GATEWAY_BASE_URL", "https://gateway.example")
    view = profile()
    gateway, grants = AsyncMock(), AsyncMock()
    gateway.model_catalog_with_aliases.return_value = (
        ["canonical-model", "chosen-model"],
        {"model-alias": "canonical-model", "chosen-alias": "chosen-model"},
    )
    grants.mint.return_value = {
        "grant_id": "grant-chosen",
        "token": "private-grant",
        "allowed_models": ["chosen-model"],
    }
    requests = []

    async def handle(request):
        requests.append(request)
        assert json.loads(request.content)["model"] == "chosen-model"
        return httpx.Response(200, json=completion([output(view["memories"][0])]))

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
        result = await consolidate_memory(
            view,
            user_id="alice",
            gateway=gateway,
            grants=grants,
            requested_model=requested_model,
            client=client,
        )
    assert result[0]["content"] == "Run API tests."
    gateway.model_catalog_with_aliases.assert_awaited_once_with("alice")
    assert grants.mint.await_args.kwargs["models"] == ["chosen-model"]
    assert grants.mint.await_args.kwargs["user_id"] == "alice"
    grants.revoke.assert_awaited_once_with("grant-chosen")
    assert len(requests) == 1


@pytest.mark.parametrize("requested_model", ["unavailable-model", "unavailable-alias", ""])
async def test_unavailable_requested_model_never_falls_back_or_mints_a_grant(
    monkeypatch, requested_model
):
    monkeypatch.setenv("GATEWAY_BASE_URL", "https://gateway.example")
    gateway, grants = AsyncMock(), AsyncMock()
    gateway.model_catalog_with_aliases.return_value = (
        ["canonical-model"],
        {"model-alias": "canonical-model", "unavailable-alias": "unavailable-model"},
    )
    with pytest.raises(MemoryConsolidationError) as exc:
        await consolidate_memory(
            profile(),
            user_id="alice",
            gateway=gateway,
            grants=grants,
            requested_model=requested_model,
        )
    assert exc.value.status_code == 400
    assert "selected model is not available" in str(exc.value)
    grants.mint.assert_not_awaited()
    grants.revoke.assert_not_awaited()
