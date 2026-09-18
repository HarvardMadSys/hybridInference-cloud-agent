"""The platform's own MCP server: the two tools that talk about the goal.

It is not a connection. There is no URL, no credential, no upstream session
and nothing to authorize — the tools read and write this service's own rows,
under the attempt fence the caller's token already carries. That is why it
lives here rather than in the connection registry: an entry there would have to
answer questions (where does it point, whose credential is it, what did its
discovery return) that have no answers, and every one of them would be a lie
the rest of the relay would then act on.

**Why it exists at all.** The goal report began as a two-line text
convention at the end of the turn's final message. That works, and it is the
fallback this still honours, but it has two costs: a model that narrates in the
middle of its answer can restate the status lines and change what the platform
reads, and a turn that ends without prose — a crash after the work was done —
reports nothing at all. A tool call is a structured act with a fence behind it,
and it is recorded the moment it happens rather than reconstructed afterwards.

**Nothing about the decision moves.** The tool writes ``goal_report`` /
``goal_reason`` on the attempt's job row, which is exactly where the text
writes them. ``worker_finish`` still decides, still buys a verification round
for a completion claim, and still requires three consecutive ``blocked``
reports. Only the signal's source changed.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from cloud_agent.mcp.host import ContentBlock, ToolResult

if TYPE_CHECKING:
    from cloud_agent.ports.goals import GoalRepository

#: The names are deliberately unprefixed and unnamespaced. Every upstream tool
#: is presented as ``<alias>__<tool>``, so a bare name can never collide with
#: one an owner connected — and a session with no connections at all still
#: finds these.
GET_GOAL = "get_goal"
REPORT_GOAL = "report_goal"

#: What a report may say. The same three words the text convention accepts, and
#: the same three the ``goal_report`` CHECK constraint accepts; a fourth would
#: be rejected by the database after the model had already been told it worked.
STATUSES = ("continue", "complete", "blocked")


def _tool(
    name: str, description: str, properties: dict[str, Any], required: list[str]
) -> dict[str, Any]:
    """One catalogue entry, in the shape every MCP client reads."""
    return {
        "name": name,
        "description": description,
        "inputSchema": {
            "type": "object",
            "properties": properties,
            "required": required,
            "additionalProperties": False,
        },
    }


def tool_entries() -> list[dict[str, Any]]:
    """The catalogue entries for the merged ``tools/list`` reply."""
    return [
        _tool(
            GET_GOAL,
            (
                "Read this conversation's completion goal: the objective, whether it is "
                "active, and how many of its automatic rounds have been used. Call this "
                "before reporting, and whenever you are unsure what is still being asked "
                "for."
            ),
            {},
            [],
        ),
        _tool(
            REPORT_GOAL,
            (
                'Report your status toward the completion goal. "continue" means work '
                'remains; "complete" means the objective\'s check has just been run and '
                'passed, in this turn; "blocked" means a condition you cannot change from '
                "inside this workspace. A completion claim is verified by a further turn "
                "before the goal is called done, and a blocked claim is accepted only after "
                "three consecutive reports."
            ),
            {
                "status": {
                    "type": "string",
                    "enum": list(STATUSES),
                    "description": "continue, complete or blocked.",
                },
                "reason": {
                    "type": "string",
                    "description": (
                        "One line: what remains, the command and result that proves "
                        "completion, or the blocker."
                    ),
                },
            },
            ["status"],
        ),
    ]


def handles(name: str) -> bool:
    """Whether a tool name belongs to this server rather than to a connection."""
    return name in (GET_GOAL, REPORT_GOAL)


def _text(payload: dict[str, Any], *, is_error: bool = False) -> ToolResult:
    """One JSON answer, as a single text block.

    JSON rather than prose because the reader is a model that has to act on the
    values, and because the shape then matches ``structuredContent`` for a
    client that prefers it.
    """
    import json

    return ToolResult(
        content=(
            ContentBlock(type="text", text=json.dumps(payload, ensure_ascii=False, indent=2)),
        ),
        structured=payload,
        is_error=is_error,
    )


def _describe(goal: dict[str, Any] | None) -> dict[str, Any]:
    """The goal as a model reads it, or the honest statement that there is none."""
    if goal is None:
        return {
            "goal": None,
            "note": (
                "This conversation has no completion goal. Work normally and do not "
                "report a status."
            ),
        }
    described: dict[str, Any] = {
        "objective": goal["objective"],
        "state": goal["state"],
        "rounds_used": goal["rounds_used"],
        "max_rounds": goal["max_rounds"],
    }
    if goal.get("pause_reason"):
        described["paused_because"] = goal["pause_reason"]
    if goal.get("blocked_code"):
        described["blocked_code"] = goal["blocked_code"]
    if goal.get("blocked_message"):
        described["blocked_message"] = goal["blocked_message"]
    if goal["state"] == "active":
        described["continues_on_its_own"] = True
        described["report_with"] = REPORT_GOAL
    else:
        described["continues_on_its_own"] = False
        described["note"] = (
            "The platform is not continuing this goal by itself. Report your status "
            "anyway; the owner decides whether it resumes."
        )
    return {"goal": described}


async def call(
    name: str,
    arguments: Any,
    *,
    goals: GoalRepository | None,
    job_id: str,
    attempt_id: int | None,
    lease_generation: int,
) -> ToolResult:
    """Run one of the platform's tools, or answer that it is unavailable."""
    if goals is None:
        return _text({"error": "this deployment has no goal storage"}, is_error=True)
    if name == GET_GOAL:
        return _text(_describe(await goals.get_goal_for_job(job_id=job_id)))
    if name != REPORT_GOAL:
        return _text({"error": f"unknown tool {name!r}"}, is_error=True)

    fields = arguments if isinstance(arguments, dict) else {}
    status = fields.get("status")
    if status not in STATUSES:
        return _text(
            {
                "error": "status must be one of " + ", ".join(STATUSES),
                "received": status,
            },
            is_error=True,
        )
    reason = fields.get("reason")
    if reason is not None and not isinstance(reason, str):
        return _text({"error": "reason must be a string when present"}, is_error=True)
    if attempt_id is None:
        return _text({"error": "this call is not bound to a turn"}, is_error=True)
    written = await goals.record_goal_report(
        job_id=job_id,
        attempt_id=attempt_id,
        lease_generation=lease_generation,
        status=str(status),
        reason=reason,
    )
    if not written:
        # The attempt is no longer the running one: a superseded turn reporting
        # a goal outcome would relabel what its successor is doing.
        return _text(
            {
                "error": (
                    "this turn is no longer running, so its report was not recorded. "
                    "Stop and let the current turn report."
                )
            },
            is_error=True,
        )
    goal = await goals.get_goal_for_job(job_id=job_id)
    return _text(
        {
            "recorded": {"status": status, "reason": reason},
            "note": (
                "The platform will act on this when your turn ends. It does not end the "
                "turn: finish what you are doing and say what you found."
            ),
            **_describe(goal),
        }
    )


__all__ = ["GET_GOAL", "REPORT_GOAL", "STATUSES", "call", "handles", "tool_entries"]
