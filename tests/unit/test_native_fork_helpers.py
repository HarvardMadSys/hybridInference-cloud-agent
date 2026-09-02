"""Provider boundary adapters for historical native-session Fork."""

from __future__ import annotations

import runpy
import sys
from pathlib import Path
from types import ModuleType, SimpleNamespace

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]


def _script(path: str) -> dict:
    return runpy.run_path(str(REPO_ROOT / path), run_name=f"test_{Path(path).name}")


def test_opencode_and_kilo_fork_at_the_next_exclusive_user_boundary() -> None:
    payload = _script("deploy/sandbox/http-agent-live-bridge")["fork_payload"]
    messages = [
        {"info": {"id": "user-1", "role": "user"}},
        {"info": {"id": "assistant-1", "role": "assistant"}},
        {"info": {"id": "user-2", "role": "user"}},
        {"info": {"id": "assistant-2", "role": "assistant"}},
    ]

    assert payload(messages, 1) == {"messageID": "user-2"}
    assert payload(messages, 2) == {}
    with pytest.raises(RuntimeError, match=r"3 of 2"):
        payload(messages, 3)


def test_claude_fork_uses_the_top_level_assistant_closing_the_selected_turn(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sdk = ModuleType("claude_agent_sdk")
    sdk.fork_session = lambda *_args, **_kwargs: None  # type: ignore[attr-defined]
    sdk.get_session_messages = lambda *_args, **_kwargs: []  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "claude_agent_sdk", sdk)
    anchor = _script("deploy/sandbox/claude-session-fork")["assistant_anchor"]

    def message(kind: str, uuid: str, **kwargs):
        return SimpleNamespace(
            type=kind,
            uuid=uuid,
            message={"role": kind},
            parent_tool_use_id=kwargs.get("parent_tool_use_id"),
            parent_agent_id=kwargs.get("parent_agent_id"),
        )

    messages = [
        message("user", "user-1"),
        message("assistant", "subagent", parent_agent_id="agent-1"),
        message("assistant", "assistant-1"),
        message("user", "tool-user", parent_tool_use_id="tool-1"),
        message("user", "user-2"),
        message("assistant", "assistant-2"),
    ]

    assert anchor(messages, 1) == "assistant-1"
    assert anchor(messages, 2) == "assistant-2"
    with pytest.raises(ValueError, match=r"3 of 2"):
        anchor(messages, 3)
