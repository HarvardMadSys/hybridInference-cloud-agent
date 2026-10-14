"""Read a user's own words safely: no control characters, credentials or quotations."""

from __future__ import annotations

import re
import unicodedata
from typing import Any

CREDENTIAL = re.compile(
    r"-----BEGIN [A-Z ]*PRIVATE KEY-----|"
    r"\b(?:sk-[A-Za-z0-9_-]{12,}|github_pat_[A-Za-z0-9_]+|gh[pousr]_[A-Za-z0-9]+|"
    r"(?:AKIA|ASIA)[A-Z0-9]{16}|eyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+)\b|"
    r"\b(?:api[_ -]?key|access[_ -]?token|refresh[_ -]?token|auth[_ -]?token|"
    r"password|passwd|secret|credential|token)[\"'`]?\s*(?:[:=]|\bis\b)\s*\S+|"
    r"\bbearer\s+[A-Za-z0-9._~+/-]{8,}|https?://[^\s/@]+:[^\s/@]+@",
    re.IGNORECASE,
)


def normalize_text(value: str) -> str:
    """Collapse whitespace and drop control and format characters."""
    return " ".join(
        "".join(
            char
            for char in value
            if char.isspace() or not unicodedata.category(char).startswith("C")
        ).split()
    )


def clean(value: Any, *, limit: int) -> str:
    """Normalized text within ``limit``, or empty when it is not text or holds a credential."""
    if not isinstance(value, str):
        return ""
    value = normalize_text(value)
    # Check normalized, untruncated text: stripping format characters must not
    # turn an obfuscated credential field into a displayed secret.
    if CREDENTIAL.search(value):
        return ""
    if len(value) > limit:
        value = value[: limit - 1].rstrip() + "…"
    return value


def user_sentences(content: Any) -> list[str]:
    """The sentences a user wrote themselves, without quoted lines or code blocks."""
    if not isinstance(content, str):
        return []
    # Remove whole quoted/code lines before sentence splitting, so later
    # sentences in a blockquote cannot pass for the user's own words.
    lines: list[str] = []
    fence_char: str | None = None
    fence_length = 0
    for line in content[:8000].splitlines():
        fence = re.match(r"^[ \t]{0,3}(`{3,}|~{3,})(.*)$", line)
        if fence_char:
            if (
                fence
                and fence[1][0] == fence_char
                and len(fence[1]) >= fence_length
                and not fence[2].strip()
            ):
                fence_char = None
            continue
        if fence:
            fence_char, fence_length = fence[1][0], len(fence[1])
            continue
        if line.startswith(("    ", "\t")) or line.lstrip().startswith((">", '"', "'", "`")):
            continue
        lines.append(line)
    text = "\n".join(lines)
    return [
        cleaned
        for sentence in re.split(r"(?<=[.!?])\s+|\n+", text)
        if not sentence.lstrip().startswith((">", "`", '"', "'"))
        if (cleaned := clean(sentence, limit=500))
    ]
