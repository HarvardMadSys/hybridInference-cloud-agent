"""The rule a prompt's text follows, shared by a new task and a message."""

from __future__ import annotations


def _visible_prompt(value: str) -> str:
    """Reject a visually empty message and keep the trimmed text."""
    prompt = value.strip()
    if not prompt:
        raise ValueError("prompt must not be blank")
    return prompt
