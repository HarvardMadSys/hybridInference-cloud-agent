"""Short task titles: the first meaningful line of a conversation's prompt.

A conversation is titled from its first prompt when it is created, and its
owner may rename it. No model is asked for a title.
"""

from __future__ import annotations

import unicodedata
from typing import Any

from cloud_agent.logging_utils import get_logger
from cloud_agent.settings import integer_setting

logger = get_logger(__name__)


def _shorten_title(value: str) -> str:
    max_chars = integer_setting("task_titles.max_chars")
    if len(value) <= max_chars:
        return value
    return value[: max_chars - 1].rstrip() + "…"


def fallback_title(prompt: str) -> str:
    """Return a compact first meaningful line of ``prompt``."""
    for line in prompt.splitlines():
        cleaned = " ".join(
            "".join(
                char
                for char in line
                if char.isspace() or not unicodedata.category(char).startswith("C")
            ).split()
        )
        if any(char.isalnum() for char in cleaned):
            return _shorten_title(cleaned)
    return "New task"


def is_warmup_title(value: Any) -> bool:
    """Recognize the proxy's completion-shaped status, including old truncations."""
    return isinstance(value, str) and value.strip().strip("\"'“”\u2018\u2019` ").startswith(
        "⏳ The model is starting up"
    )
