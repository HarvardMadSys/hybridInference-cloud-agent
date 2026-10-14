"""Durable memories are one short lasting rule each, without credentials."""

from __future__ import annotations

import json

import pytest

from cloud_agent.agent_memory import (
    TASK_CONTEXT_LIMIT,
    describe,
    fit_task_memories,
    memory_key,
    normalize_memory,
    prompt_with_agent_memories,
)


def test_descriptions_and_keys():
    assert describe("short") == "short"
    long = "x" * 100
    assert len(describe(long)) == 80 and describe(long).endswith("…")
    assert memory_key("Use pnpm.") == memory_key("use PNPM") == memory_key("use pnpm。")


@pytest.mark.parametrize(
    "content", ["", "Use token=privatevalue", "x" * 241, "Skip tests for this PR"]
)
def test_invalid_memories_are_rejected(content):
    with pytest.raises(ValueError):
        normalize_memory(content)


def test_memories_are_normalized():
    assert normalize_memory("  以后都用中文回复  ") == "以后都用中文回复"


def _scope(project=(), user=()):
    return {
        "repo": "owner/api",
        "project": [{"id": f"p{n}", "content": text} for n, text in enumerate(project)],
        "user": [{"id": f"u{n}", "content": text} for n, text in enumerate(user)],
    }


@pytest.mark.parametrize("memories", [None, _scope()])
def test_a_task_without_memories_keeps_its_prompt(memories):
    assert prompt_with_agent_memories("fix it", memories) == "fix it"


def test_memories_precede_the_task_with_ids_the_tools_accept():
    prompt = prompt_with_agent_memories("fix it", _scope(["Use pnpm."], ["Reply in Chinese."]))
    data = prompt.split("Durable memories:\n", 1)[1].split("\n\nCurrent task:\n", 1)[0]
    assert json.loads(data) == {
        "project": {"repo": "owner/api", "memories": [{"id": "p0", "content": "Use pnpm."}]},
        "user": [{"id": "u0", "content": "Reply in Chinese."}],
    }
    assert "memory_save" in prompt and prompt.endswith("Current task:\nfix it")


def test_project_memories_are_kept_first_within_the_limit():
    rule = "r" * 230
    prompt = prompt_with_agent_memories("fix it", _scope([rule] * 30, [rule] * 30))
    data = json.loads(prompt.split("Durable memories:\n", 1)[1].split("\n\nCurrent task:\n")[0])
    assert len(prompt) - len("fix it") <= TASK_CONTEXT_LIMIT
    assert len(data["project"]["memories"]) == 30
    assert 0 < len(data["user"]) < 30


def test_fitting_keeps_the_order_given_and_reports_only_what_fits():
    rule = "r" * 230
    memories = _scope([rule] * 30, [f"newest {rule}"] + [rule] * 29)
    kept = fit_task_memories(memories)
    # The caller lists each scope newest first, so the newest user rule survives.
    assert kept["user"][0]["id"] == "u0"
    assert 0 < len(kept["user"]) < 30
    assert prompt_with_agent_memories("x", memories) == prompt_with_agent_memories("x", kept)
