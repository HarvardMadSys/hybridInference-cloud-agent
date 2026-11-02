"""What the platform adds to a turn's prompt at claim, and how much of it.

A claimed turn's prompt is the owner's task wrapped, in a fixed order, in the
platform's own context: the uploaded files' paths, durable memories, the
selected skills, the chat's SSH servers and an active goal. Each wrapper only
adds text around the prompt it is given, so what one added is exactly the
difference it made. That difference is recorded per wrapper -- which one, how
many items, how many characters (with the ASCII and CJK counts the token
estimate needs) and, for skills, their names -- and never the text itself.

The runner stores the record on the attempt's ``started`` event, which it
writes anyway, so recording costs the turn nothing (see the context design
record, section 7.2). Installed plugins reach the CLI as plugins, not through
the prompt: they are listed by id with no size, because the platform cannot
see what each one adds.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from cloud_agent.agent_memory import fit_task_memories, prompt_with_agent_memories
from cloud_agent.application.ssh_servers import prompt_with_ssh_servers
from cloud_agent.attachments.selection import prompt_with_attachments
from cloud_agent.goals.prompts import prompt_with_goal
from cloud_agent.skills.selection import prompt_with_skills
from cloud_agent_shared.text_tokens import split_counts

if TYPE_CHECKING:
    from collections.abc import Callable

#: Names one injection record lists (skills, plugins).
MAX_NAMES = 20


def _names(rows: list[dict[str, Any]] | None, key: str) -> list[str]:
    return [str(row.get(key))[:120] for row in rows or [] if row.get(key)][:MAX_NAMES]


def claim_prompt(
    task_prompt: str,
    *,
    attachments: list[dict[str, Any]] | None,
    memories: dict[str, Any] | None,
    skills: list[dict[str, Any]] | None,
    ssh_servers: list[dict[str, Any]],
    goal: dict[str, Any] | None,
    plugins: list[dict[str, Any]] | None = None,
) -> tuple[str, list[dict[str, Any]]]:
    """The prompt the turn runs with, and what each platform wrapper added to it."""
    kept = fit_task_memories(memories) if memories is not None else None
    steps: list[tuple[str, Callable[[str], str], int, list[str]]] = [
        (
            "attachments",
            lambda prompt: prompt_with_attachments(prompt, attachments),
            len(attachments or []),
            [],
        ),
        (
            "memory",
            lambda prompt: prompt_with_agent_memories(prompt, memories),
            len((kept or {}).get("project") or []) + len((kept or {}).get("user") or []),
            [],
        ),
        (
            "skills",
            lambda prompt: prompt_with_skills(prompt, skills),
            len(skills or []),
            _names(skills, "name"),
        ),
        (
            "ssh_servers",
            lambda prompt: prompt_with_ssh_servers(prompt, ssh_servers),
            len(ssh_servers),
            [],
        ),
        ("goal", lambda prompt: prompt_with_goal(prompt, goal), 1, []),
    ]
    prompt = task_prompt
    counts = split_counts(prompt)
    injections: list[dict[str, Any]] = []
    for producer, wrap, items, names in steps:
        wrapped = wrap(prompt)
        added = len(wrapped) - len(prompt)
        if added > 0:
            after = split_counts(wrapped)
            injections.append(
                {
                    "producer": producer,
                    "items": items,
                    "chars": added,
                    "ascii": after[0] - counts[0],
                    "cjk": after[1] - counts[1],
                    **({"names": names} if names else {}),
                }
            )
            counts = after
        prompt = wrapped
    plugin_names = _names(plugins, "plugin")
    if plugins:
        injections.append(
            {"producer": "plugins", "items": len(plugins), "names": plugin_names, "chars": None}
        )
    return prompt, injections
