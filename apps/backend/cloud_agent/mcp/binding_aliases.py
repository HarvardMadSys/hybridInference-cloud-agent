"""Unambiguous connection identities within one conversation."""

from __future__ import annotations

import re
from typing import TYPE_CHECKING

from cloud_agent.mcp.models import ALIAS_PATTERN

if TYPE_CHECKING:
    from collections.abc import Sequence


def binding_aliases(entries: Sequence[tuple[int, str | None, str]]) -> list[str]:
    """Reserve explicit aliases first, then allocate collision-free defaults.

    Each entry carries its connection id, optional caller alias and preferred
    default. Duplicate connections and explicit aliases are caller errors.
    Existing explicit names never move when an automatic name needs a suffix.
    """
    connections: set[int] = set()
    reserved: set[str] = set()
    for connection_id, explicit, _ in entries:
        if connection_id in connections:
            raise ValueError(f"MCP connection {connection_id} is included more than once")
        connections.add(connection_id)
        if not explicit:
            continue
        alias = explicit.strip()
        if not re.fullmatch(ALIAS_PATTERN, alias):
            raise ValueError(
                f"{alias!r} is not a usable MCP alias: use letters, digits, underscore "
                "and hyphen, starting with a letter or digit, up to 40 characters"
            )
        if alias in reserved:
            raise ValueError(f"MCP alias {alias!r} is used by more than one connection")
        reserved.add(alias)

    result: list[str] = []
    for connection_id, explicit, preferred in entries:
        if explicit:
            result.append(explicit.strip())
            continue
        base = preferred if re.fullmatch(ALIAS_PATTERN, preferred) else f"mcp{connection_id}"
        candidate = base
        counter = 1
        while candidate in reserved:
            suffix = f"-{connection_id}" + (f"-{counter}" if counter > 1 else "")
            candidate = base[: 40 - len(suffix)] + suffix
            counter += 1
        reserved.add(candidate)
        result.append(candidate)
    return result
