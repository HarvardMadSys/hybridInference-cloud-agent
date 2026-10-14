"""The platform memory tools: agents save, replace and archive durable memories.

Like Claude Code's automatic memory, the agent doing the task decides what is
worth keeping. The relay serves these tools itself; the owner and the task are
read from the fenced job token, never from the arguments, so a sandbox can only
write memories for the task it runs and the repository that task belongs to.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

from cloud_agent.agent_memory import (
    AgentMemoryDisabled,
    AgentMemoryFull,
    AgentMemoryNotFound,
    AgentMemoryTaskEnded,
    MemoryTask,
)
from cloud_agent.logging_utils import get_logger
from cloud_agent.user_memory import MEMORY_CONTENT_LIMIT

if TYPE_CHECKING:
    from cloud_agent.ports.agent_memory import AgentMemoryRepository

logger = get_logger(__name__)

#: A non-secret marker that makes the runtime load the merged MCP endpoint
#: even when a conversation selected no user-owned connections.
RUNTIME_SERVER_ID = ":platform:memory"
SAVE = "memory_save"
UPDATE = "memory_update"
DELETE = "memory_delete"
TOOL_NAMES = frozenset({SAVE, UPDATE, DELETE})

_ID = {"type": "string", "minLength": 1, "maxLength": 64, "description": "The memory's id."}
_CONTENT = {
    "type": "string",
    "minLength": 1,
    "maxLength": MEMORY_CONTENT_LIMIT,
    "description": (
        "One short imperative sentence in English, translated if the user wrote in another "
        "language. Keep commands, paths, names and quoted text exactly as written."
    ),
}

TOOL_DEFINITIONS: list[dict[str, Any]] = [
    {
        "name": SAVE,
        "description": (
            "Save one lasting rule to follow in this user's future tasks. Save when the user "
            "asks you to remember something, corrects how you work, or states a stable "
            "preference. Do not save instructions for this task only, facts the code or "
            "AGENTS.md already shows, or secrets. The memories listed at the start of the "
            "task are already saved: update one of them instead of saving a near-duplicate. "
            "The owner sees every saved memory and can undo it."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "content": _CONTENT,
                "scope": {
                    "type": "string",
                    "enum": ["repository", "all_projects"],
                    "default": "repository",
                    "description": (
                        "'repository' applies to this task's repository only. Use "
                        "'all_projects' only when the user says the rule applies everywhere."
                    ),
                },
            },
            "required": ["content"],
            "additionalProperties": False,
        },
    },
    {
        "name": UPDATE,
        "description": (
            "Rewrite a saved memory, by id, when it is incomplete or conflicts with what the "
            "user now wants. The memory keeps its scope; the old text is archived and the "
            "owner can restore it."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {"id": _ID, "content": _CONTENT},
            "required": ["id", "content"],
            "additionalProperties": False,
        },
    },
    {
        "name": DELETE,
        "description": (
            "Remove a saved memory, by id, that the user says is wrong or no longer applies. "
            "It is archived and the owner can restore it."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {"id": _ID},
            "required": ["id"],
            "additionalProperties": False,
        },
    },
]

_INVALID_CONTENT = (
    "Nothing was saved: a memory must be one lasting rule of at most 240 characters, "
    "without credentials or wording limited to this task."
)


def _error(text: str) -> dict[str, Any]:
    return {"content": [{"type": "text", "text": text}], "isError": True}


def _string(arguments: dict[str, Any], key: str) -> str:
    value = arguments.get(key)
    if not isinstance(value, str) or not value.strip():
        raise TypeError(key)
    return value


def _result(action: str, memory: dict[str, Any]) -> dict[str, Any]:
    """Answer with the memory as the next task will see it."""
    shown = {
        "id": memory["id"],
        "content": memory["content"],
        "scope": "all_projects" if memory["repo"] is None else "repository",
    }
    return {"content": [{"type": "text", "text": json.dumps({action: shown}, ensure_ascii=False)}]}


async def call(
    name: str,
    arguments: Any,
    *,
    task: MemoryTask,
    repository: AgentMemoryRepository,
) -> dict[str, Any]:
    """Run one memory tool for the fenced task; failures are tool errors, not faults.

    Answers use fixed text and the stored memory only: neither the arguments
    nor saved memories are logged.
    """
    values = arguments if isinstance(arguments, dict) else {}
    try:
        if name == SAVE:
            scope = values.get("scope", "repository")
            if scope not in ("repository", "all_projects"):
                raise TypeError("scope")
            memory = await repository.save_agent_memory(
                task, _string(values, "content"), all_projects=scope == "all_projects"
            )
            return _result("saved", memory)
        if name == UPDATE:
            memory = await repository.replace_agent_memory(
                task, _string(values, "id"), _string(values, "content")
            )
            return _result("updated", memory)
        memory = await repository.archive_agent_memory(task, _string(values, "id"))
        return _result("deleted", memory)
    except TypeError as exc:
        return _error(f"Nothing was changed: {exc} is missing or invalid.")
    except AgentMemoryTaskEnded:
        return _error("This task is no longer running. Nothing was changed.")
    except AgentMemoryNotFound:
        return _error("No active memory with that id is visible to this task.")
    except AgentMemoryDisabled:
        return _error("The owner turned memory off for this scope. Nothing was changed.")
    except AgentMemoryFull:
        return _error(
            "This scope is full of memories the owner wrote. Nothing was saved; tell the owner."
        )
    except ValueError:
        return _error(_INVALID_CONTENT)
    except Exception:
        logger.warning("agent_memory_tool_failed", extra={"event": "agent_memory_tool_failed"})
        return _error("Memory is temporarily unavailable. Continue the task without it.")
