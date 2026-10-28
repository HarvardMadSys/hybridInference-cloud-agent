"""Images a tool returned, carried the one way Chat Completions allows.

A ``role: "tool"`` message carries text only: OpenAI's Chat Completions, and
the providers that follow it, refuse an image part there. But a tool can
return one -- Claude Code's ``Read`` of a screenshot, Codex's ``view_image`` --
and dropping it would leave the model describing an image it never saw while
the CLI believes it was sent. So each tool message keeps its text and says
that its images follow, and the images go, in order and under the call id
they came from, in one ``role: "user"`` message right after that run of tool
messages. A provider that reads images sees them; one that does not refuses
the request, which the CLI reports. Nothing is replaced by a placeholder.
"""

from __future__ import annotations

from typing import Any


def _split(content: list[Any]) -> tuple[str, list[dict[str, Any]]]:
    texts: list[str] = []
    images: list[dict[str, Any]] = []
    for part in content:
        if isinstance(part, str):
            texts.append(part)
        elif isinstance(part, dict) and part.get("type") == "image_url":
            images.append(part)
        elif isinstance(part, dict) and isinstance(part.get("text"), str):
            texts.append(part["text"])
    return "\n".join(text for text in texts if text), images


def _note(count: int) -> str:
    noun = "an image" if count == 1 else f"{count} images"
    return f"[The tool returned {noun}; attached in the next message.]"


def move_tool_images(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """``messages`` with every tool message's images moved to a user message after its run."""
    out: list[dict[str, Any]] = []
    pending: list[dict[str, Any]] = []

    def flush() -> None:
        if pending:
            out.append({"role": "user", "content": list(pending)})
            pending.clear()

    for message in messages:
        content = message.get("content")
        if message.get("role") != "tool":
            flush()
            out.append(message)
            continue
        if not isinstance(content, list):
            out.append(message)
            continue
        text, images = _split(content)
        if not images:
            out.append({**message, "content": text})
            continue
        out.append(
            {**message, "content": f"{text}\n{_note(len(images))}" if text else _note(len(images))}
        )
        pending.append(
            {
                "type": "text",
                "text": f"Image output of tool call {message.get('tool_call_id', '')}:",
            }
        )
        pending.extend(images)
    flush()
    return out


__all__ = ["move_tool_images"]
