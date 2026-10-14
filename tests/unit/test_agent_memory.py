"""Durable memories are one short lasting rule each, without credentials."""

from __future__ import annotations

import pytest

from cloud_agent.agent_memory import describe, memory_key, normalize_memory


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
