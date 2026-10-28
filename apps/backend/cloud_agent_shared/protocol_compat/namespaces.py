"""Namespaced Responses function tools, as flat Chat Completions functions and back.

Codex 0.145.0 groups some function tools under a ``namespace`` tool: its
sub-agent tools (``multi_agent_v1``) and every MCP tool the platform relays
(``mcp__session``). A call to one comes back as a ``function_call`` item
carrying the inner ``name`` and a ``namespace`` field, and is replayed that way
in the conversation history. Chat Completions has only flat function names,
so each namespaced function is offered upstream under one flat name and
restored on the way back -- in the streamed items, the final response and,
through the history, every later request.

The flat name is ``<namespace>__<name>``, reduced to the characters and length
Chat Completions accepts. When that is not exactly reversible -- a character
had to be replaced, it had to be shortened, or it would equal another tool's
name -- a short digest of the namespace and name is appended instead, so two
different tools never share a flat name and the same tool always gets the
same one: a history item maps exactly as the tool list does.
"""

from __future__ import annotations

import hashlib
import re
from typing import Any

_FLAT_INVALID = re.compile(r"[^A-Za-z0-9_-]")
_MAX_NAME = 64


def _digest(namespace: str, name: str) -> str:
    return hashlib.sha256(f"{namespace}\0{name}".encode()).hexdigest()[:10]


class NameCollision(ValueError):
    """Two tools would share one flat name even with the digest: refused, not guessed."""


class ToolNames:
    """The flat name of each namespaced function in one request, and the way back."""

    def __init__(self, plain: set[str]) -> None:
        """Start from the request's plain function names, which keep their own."""
        self._plain = set(plain)
        self._flat: dict[tuple[str, str], str] = {}
        self._original: dict[str, tuple[str, str]] = {}

    def __bool__(self) -> bool:
        """Whether any namespaced function was named."""
        return bool(self._flat)

    def flat(self, namespace: str, name: str) -> str:
        """The one flat name for ``name`` in ``namespace``."""
        key = (namespace, name)
        if key in self._flat:
            return self._flat[key]
        base = f"{namespace}__{name}"
        flat = _FLAT_INVALID.sub("_", base)
        taken = flat in self._plain or flat in self._original
        if flat != base or len(flat) > _MAX_NAME or taken:
            flat = f"{flat[: _MAX_NAME - 12]}__{_digest(namespace, name)}"
            if flat in self._plain or flat in self._original:
                raise NameCollision(f"tool {name!r} in {namespace!r} has no unambiguous flat name")
        self._flat[key] = flat
        self._original[flat] = key
        return flat

    def original(self, flat: str) -> tuple[str, str] | None:
        """``(namespace, name)`` behind a flat name this request offered, else ``None``."""
        return self._original.get(flat)


def _plain_functions(tools: Any) -> set[str]:
    if not isinstance(tools, list):
        return set()
    return {
        str(tool.get("name"))
        for tool in tools
        if isinstance(tool, dict) and tool.get("type") == "function" and tool.get("name")
    }


def _flatten_tools(tools: list[Any], names: ToolNames) -> list[Any]:
    flat: list[Any] = []
    for tool in tools:
        if not (isinstance(tool, dict) and tool.get("type") == "namespace"):
            flat.append(tool)
            continue
        namespace = str(tool.get("name") or "")
        for inner in tool.get("tools") or []:
            if not isinstance(inner, dict) or inner.get("type") != "function":
                # Refused by the gate, which sees it as it is.
                flat.append({**inner, "namespace": namespace} if isinstance(inner, dict) else inner)
                continue
            flat.append({**inner, "name": names.flat(namespace, str(inner.get("name") or ""))})
    return flat


def _flatten_items(items: list[Any], names: ToolNames) -> list[Any]:
    flat: list[Any] = []
    for item in items:
        if isinstance(item, dict) and item.get("type") == "function_call" and item.get("namespace"):
            namespace = str(item["namespace"])
            item = {key: value for key, value in item.items() if key != "namespace"}
            item["name"] = names.flat(namespace, str(item.get("name") or ""))
        flat.append(item)
    return flat


def _flatten_choice(choice: Any, names: ToolNames) -> Any:
    """A forced call to a namespaced function names it as its definition does."""
    if isinstance(choice, dict) and choice.get("type") == "function" and choice.get("namespace"):
        name = names.flat(str(choice["namespace"]), str(choice.get("name") or ""))
        return {"type": "function", "name": name}
    return choice


def flatten_request(body: dict[str, Any]) -> tuple[dict[str, Any], ToolNames]:
    """``body`` with every namespaced function flat, and the names to restore.

    Raises ``NameCollision`` when a namespaced function cannot be given a flat
    name no other tool already has.
    """
    tools = body.get("tools")
    names = ToolNames(_plain_functions(tools))
    flat = dict(body)
    if isinstance(tools, list):
        flat["tools"] = _flatten_tools(tools, names)
    items = body.get("input")
    if isinstance(items, list):
        flat["input"] = _flatten_items(items, names)
    if "tool_choice" in body:
        flat["tool_choice"] = _flatten_choice(body["tool_choice"], names)
    return flat, names


def restore_item(item: Any, names: ToolNames) -> Any:
    """A ``function_call`` item under its namespaced name again; anything else unchanged."""
    if not isinstance(item, dict) or item.get("type") != "function_call":
        return item
    original = names.original(str(item.get("name") or ""))
    if original is None:
        return item
    namespace, name = original
    return {**item, "name": name, "namespace": namespace}


def restore_payload(payload: dict[str, Any], names: ToolNames) -> dict[str, Any]:
    """One Responses event with every function call it carries restored."""
    restored = dict(payload)
    if "item" in restored:
        restored["item"] = restore_item(restored["item"], names)
    response = restored.get("response")
    if isinstance(response, dict) and isinstance(response.get("output"), list):
        restored["response"] = {
            **response,
            "output": [restore_item(item, names) for item in response["output"]],
        }
    if isinstance(restored.get("output"), list):
        restored["output"] = [restore_item(item, names) for item in restored["output"]]
    return restored


__all__ = ["NameCollision", "ToolNames", "flatten_request", "restore_item", "restore_payload"]
