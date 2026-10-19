"""Reported text made storable: Postgres keeps neither NUL nor a surrogate.

``text`` and ``jsonb`` refuse NUL, and a surrogate code point is not UTF-8, so
asyncpg cannot send one and neither can a JSON encoder that writes UTF-8 --
httpx's included. Both turn up in ordinary output: NUL from a tool that prints
binary or NUL-separated data, an unpaired surrogate from a CLI that cuts a
string between the two halves of a pair, or from a file name that is not UTF-8.

What a turn reports is kept rather than refused, so each such character becomes
U+FFFD, the character a UTF-8 decoder puts where it could not read a byte. An
answer that only repeats what it was sent needs less: :func:`encodable_json`
replaces the surrogates alone.
"""

from __future__ import annotations

import re
from typing import Any

REPLACEMENT_CHARACTER = "\ufffd"
_UNSTORABLE = re.compile("[\x00\ud800-\udfff]")
_UNENCODABLE = re.compile("[\ud800-\udfff]")


def storable_text(value: str) -> str:
    """``value`` with each NUL and surrogate code point replaced by U+FFFD."""
    return _UNSTORABLE.sub(REPLACEMENT_CHARACTER, value)


def storable_json(value: Any) -> Any:
    """A copy of a JSON value whose strings, object keys included, are storable."""
    return _replaced(value, _UNSTORABLE)


def encodable_json(value: Any) -> Any:
    """A copy of a JSON value UTF-8 can encode: surrogates replaced, NUL kept.

    For an answer that repeats its request, such as a validation error: it is
    not stored, and a NUL it keeps still shows what was refused.
    """
    return _replaced(value, _UNENCODABLE)


def _replaced(value: Any, characters: re.Pattern[str]) -> Any:
    if isinstance(value, str):
        return characters.sub(REPLACEMENT_CHARACTER, value)
    if isinstance(value, dict):
        return {
            _replaced(key, characters) if isinstance(key, str) else key: _replaced(item, characters)
            for key, item in value.items()
        }
    if isinstance(value, list | tuple):
        return [_replaced(item, characters) for item in value]
    return value
