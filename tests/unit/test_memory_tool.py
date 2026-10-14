"""The memory tools act for the fenced task and answer failures as tool errors."""

from __future__ import annotations

import json
import logging
from unittest.mock import AsyncMock

import pytest

from cloud_agent import memory_tool
from cloud_agent.agent_memory import (
    AgentMemoryDisabled,
    AgentMemoryFull,
    AgentMemoryNotFound,
    AgentMemoryTaskEnded,
    MemoryTask,
)

TASK = MemoryTask(user_id="alice", job_id="job-1", attempt_id=4, lease_generation=2)


def _memory(repo="owner/api", content="Use pnpm, not npm."):
    return {"id": "m1", "repo": repo, "content": content}


def _text(result):
    return result["content"][0]["text"]


def test_every_tool_is_defined_once_with_a_closed_schema():
    names = [tool["name"] for tool in memory_tool.TOOL_DEFINITIONS]
    assert sorted(names) == sorted(memory_tool.TOOL_NAMES)
    for tool in memory_tool.TOOL_DEFINITIONS:
        assert tool["inputSchema"]["additionalProperties"] is False
        assert "English" in json.dumps(tool) or tool["name"] == memory_tool.DELETE


@pytest.mark.parametrize(
    ("scope", "all_projects", "repo", "shown"),
    [
        (None, False, "owner/api", "repository"),
        ("repository", False, "owner/api", "repository"),
        ("all_projects", True, None, "all_projects"),
    ],
)
async def test_save_uses_the_fenced_owner_and_task(scope, all_projects, repo, shown):
    repository = AsyncMock()
    repository.save_agent_memory.return_value = _memory(repo=repo)
    arguments = {"content": "Use pnpm, not npm."}
    if scope is not None:
        arguments["scope"] = scope
    result = await memory_tool.call(memory_tool.SAVE, arguments, task=TASK, repository=repository)
    repository.save_agent_memory.assert_awaited_once_with(
        TASK, "Use pnpm, not npm.", all_projects=all_projects
    )
    assert json.loads(_text(result)) == {
        "saved": {"id": "m1", "content": "Use pnpm, not npm.", "scope": shown}
    }
    assert "isError" not in result


async def test_update_and_delete_address_a_memory_by_id():
    repository = AsyncMock()
    repository.replace_agent_memory.return_value = _memory(content="Use yarn.")
    repository.archive_agent_memory.return_value = _memory()
    updated = await memory_tool.call(
        memory_tool.UPDATE,
        {"id": "m1", "content": "Use yarn."},
        task=TASK,
        repository=repository,
    )
    deleted = await memory_tool.call(
        memory_tool.DELETE, {"id": "m1"}, task=TASK, repository=repository
    )
    repository.replace_agent_memory.assert_awaited_once_with(TASK, "m1", "Use yarn.")
    repository.archive_agent_memory.assert_awaited_once_with(TASK, "m1")
    assert json.loads(_text(updated))["updated"]["content"] == "Use yarn."
    assert json.loads(_text(deleted))["deleted"]["id"] == "m1"


@pytest.mark.parametrize(
    ("name", "arguments"),
    [
        (memory_tool.SAVE, {}),
        (memory_tool.SAVE, {"content": "  "}),
        (memory_tool.SAVE, {"content": "Use pnpm", "scope": "everywhere"}),
        (memory_tool.UPDATE, {"id": "m1"}),
        (memory_tool.DELETE, {"id": 3}),
        (memory_tool.DELETE, "not an object"),
    ],
)
async def test_invalid_arguments_change_nothing(name, arguments):
    repository = AsyncMock()
    result = await memory_tool.call(name, arguments, task=TASK, repository=repository)
    assert result["isError"] is True and "Nothing was changed" in _text(result)
    assert repository.mock_calls == []


@pytest.mark.parametrize(
    ("error", "words"),
    [
        (AgentMemoryTaskEnded("x"), "no longer running"),
        (AgentMemoryNotFound("x"), "No active memory"),
        (AgentMemoryDisabled("x"), "turned memory off"),
        (AgentMemoryFull("x"), "full of memories the owner wrote"),
        (ValueError("x"), "at most 240 characters"),
        (RuntimeError("database down"), "temporarily unavailable"),
    ],
)
async def test_failures_are_tool_errors_with_fixed_text(error, words, caplog):
    repository = AsyncMock()
    repository.save_agent_memory.side_effect = error
    with caplog.at_level(logging.WARNING):
        result = await memory_tool.call(
            memory_tool.SAVE,
            {"content": "Remember my private detail"},
            task=TASK,
            repository=repository,
        )
    assert result["isError"] is True and words in _text(result)
    assert "private detail" not in _text(result) and "private detail" not in caplog.text
