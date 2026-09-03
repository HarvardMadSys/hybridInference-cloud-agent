"""Provider boundary adapters for historical native-session Fork."""

from __future__ import annotations

import json
import runpy
import sys
from pathlib import Path
from types import ModuleType

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]


def _script(path: str) -> dict:
    return runpy.run_path(str(REPO_ROOT / path), run_name=f"test_{Path(path).name}")


def test_opencode_and_kilo_fork_at_the_next_exclusive_user_boundary() -> None:
    payload = _script("deploy/sandbox/http-agent-live-bridge")["fork_payload"]
    messages = [
        {
            "info": {"id": "user-1", "role": "user"},
            "parts": [{"type": "text", "text": "first"}],
        },
        {"info": {"id": "assistant-1", "role": "assistant"}},
        {
            "info": {"id": "user-2", "role": "user"},
            "parts": [{"type": "text", "text": "second"}],
        },
        {"info": {"id": "assistant-2", "role": "assistant"}},
    ]

    assert payload(messages, 1) == {"messageID": "user-2"}
    assert payload(messages, 2) == {}
    with pytest.raises(RuntimeError, match=r"3 of 2"):
        payload(messages, 3)


def test_opencode_and_kilo_ignore_compaction_and_synthetic_continuations() -> None:
    payload = _script("deploy/sandbox/http-agent-live-bridge")["fork_payload"]
    messages = [
        {
            "info": {"id": "user-1", "role": "user"},
            "parts": [{"type": "text", "text": "first"}],
        },
        {"info": {"id": "assistant-tool", "role": "assistant"}},
        {
            "info": {"id": "compact", "role": "user"},
            "parts": [{"type": "compaction", "auto": True}],
        },
        {"info": {"id": "summary", "role": "assistant", "summary": True}},
        {
            "info": {"id": "continue", "role": "user"},
            "parts": [{"type": "text", "synthetic": True, "text": "continue"}],
        },
        {"info": {"id": "assistant-1", "role": "assistant"}},
        {
            "info": {"id": "user-2", "role": "user"},
            "parts": [{"type": "text", "text": "second"}],
        },
        {"info": {"id": "assistant-2", "role": "assistant"}},
    ]

    assert payload(messages, 1) == {"messageID": "user-2"}
    assert payload(messages, 2) == {}


def _claude_script(
    monkeypatch: pytest.MonkeyPatch,
) -> dict:
    sdk = ModuleType("claude_agent_sdk")
    sdk.fork_session = lambda *_args, **_kwargs: None  # type: ignore[attr-defined]
    internal = ModuleType("claude_agent_sdk._internal")
    sessions = ModuleType("claude_agent_sdk._internal.sessions")
    sessions._resolve_session_file_path = lambda *_args, **_kwargs: None  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "claude_agent_sdk", sdk)
    monkeypatch.setitem(sys.modules, "claude_agent_sdk._internal", internal)
    monkeypatch.setitem(sys.modules, "claude_agent_sdk._internal.sessions", sessions)
    return _script("deploy/sandbox/claude-session-fork")


def test_claude_fork_ignores_unattributed_tool_results_and_compaction(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    anchor = _claude_script(monkeypatch)["assistant_anchor"]

    def message(kind: str, uuid: str, content: object = "") -> dict:
        return {
            "type": kind,
            "uuid": uuid,
            "message": {"role": kind, "content": content},
        }

    messages = [
        message("user", "user-1"),
        message("assistant", "assistant-tool"),
        message("user", "tool-user", [{"type": "tool_result", "tool_use_id": "tool-1"}]),
        message("assistant", "assistant-1"),
        {
            **message("user", "compact-summary", "This session is being continued"),
            "isCompactSummary": True,
            "isVisibleInTranscriptOnly": True,
        },
        message("user", "user-2"),
        {**message("assistant", "subagent"), "isSidechain": True},
        message("assistant", "assistant-2"),
    ]

    assert anchor(messages, 1) == "assistant-1"
    assert anchor(messages, 2) == "assistant-2"
    with pytest.raises(ValueError, match=r"3 of 2"):
        anchor(messages, 3)


def test_claude_raw_chain_follows_logical_parent_across_compaction(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    script = _claude_script(monkeypatch)
    transcript = tmp_path / "session.jsonl"
    entries = [
        {
            "type": "user",
            "uuid": "user-1",
            "parentUuid": None,
            "message": {"role": "user", "content": "one"},
        },
        {
            "type": "assistant",
            "uuid": "assistant-1",
            "parentUuid": "user-1",
            "message": {"role": "assistant", "content": "done"},
        },
        {
            "type": "system",
            "subtype": "compact_boundary",
            "uuid": "boundary",
            "parentUuid": None,
            "logicalParentUuid": "assistant-1",
        },
        {
            "type": "user",
            "uuid": "summary",
            "parentUuid": "boundary",
            "isCompactSummary": True,
            "message": {"role": "user", "content": "summary"},
        },
        {
            "type": "user",
            "uuid": "user-2",
            "parentUuid": "summary",
            "message": {"role": "user", "content": "two"},
        },
        {
            "type": "assistant",
            "uuid": "assistant-2",
            "parentUuid": "user-2",
            "message": {"role": "assistant", "content": "done"},
        },
    ]
    transcript.write_text("\n".join(json.dumps(entry) for entry in entries) + "\n")
    script["_session_entries"].__globals__["_resolve_session_file_path"] = (
        lambda *_args, **_kwargs: transcript
    )

    loaded = script["_session_entries"]("session", "/workspace")
    anchor = script["assistant_anchor"]

    assert [entry["uuid"] for entry in loaded] == [
        "user-1",
        "assistant-1",
        "boundary",
        "summary",
        "user-2",
        "assistant-2",
    ]
    assert anchor(loaded, 1) == "assistant-1"
    assert anchor(loaded, 2) == "assistant-2"


def test_claude_fork_calls_sdk_with_the_inclusive_assistant_anchor(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    script = _claude_script(monkeypatch)
    transcript = tmp_path / "session.jsonl"
    transcript.write_text(
        "\n".join(
            json.dumps(entry)
            for entry in [
                {
                    "type": "user",
                    "uuid": "11111111-1111-4111-8111-111111111111",
                    "parentUuid": None,
                    "message": {"role": "user", "content": "one"},
                },
                {
                    "type": "assistant",
                    "uuid": "22222222-2222-4222-8222-222222222222",
                    "parentUuid": "11111111-1111-4111-8111-111111111111",
                    "message": {"role": "assistant", "content": "done"},
                },
            ]
        )
        + "\n"
    )
    script["_session_entries"].__globals__["_resolve_session_file_path"] = (
        lambda *_args, **_kwargs: transcript
    )
    call: dict[str, object] = {}

    def fork_session(source: str, **kwargs: object) -> object:
        call["source"] = source
        call.update(kwargs)
        return type(
            "ForkResult",
            (),
            {"session_id": "33333333-3333-4333-8333-333333333333"},
        )()

    script["main"].__globals__["fork_session"] = fork_session
    monkeypatch.setattr(
        sys,
        "argv",
        ["claude-session-fork", "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa", "1"],
    )

    script["main"]()

    assert call == {
        "source": "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
        "directory": "/workspace",
        "up_to_message_id": "22222222-2222-4222-8222-222222222222",
    }
    assert json.loads(capsys.readouterr().out) == {
        "source_session_id": "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
        "session_id": "33333333-3333-4333-8333-333333333333",
    }
