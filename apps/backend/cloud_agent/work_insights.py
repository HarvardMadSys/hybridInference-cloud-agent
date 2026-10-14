"""Work insights for the home page: suggestions and the avatar's habitat.

These are not memory. Nothing here is stored, given to agents, or controlled by
the memory switches: each read counts English keywords in the owner's latest
50 eligible requests and derives the result again.
"""

from __future__ import annotations

import re
from collections import Counter
from typing import TYPE_CHECKING

from pydantic import BaseModel, Field

# Pydantic resolves this annotation when it constructs the public view schema.
from cloud_agent.user_avatar import (
    UserAvatar,
    generate_user_avatar,
)
from cloud_agent.user_text import user_sentences

if TYPE_CHECKING:
    from collections.abc import Sequence

#: Requests read for one view, newest first.
ACTIVITY_LIMIT = 50
_FOCUS_AREAS = (
    ("Backend and APIs", r"\b(?:backend|api|apis|endpoint|fastapi|server)\b"),
    ("Frontend and UI", r"\b(?:frontend|ui|ux|react|nextjs|next\.js|css|interface)\b"),
    ("Testing", r"\b(?:test|tests|testing|pytest|jest|playwright)\b"),
    ("Reliability and debugging", r"\b(?:bug|bugs|debug|debugging|crash|error|failure|fix)\b"),
    ("Security", r"\b(?:security|authentication|authorization|vulnerability|permissions)\b"),
    ("Data and storage", r"\b(?:database|postgres|sql|storage|migration|schema)\b"),
    ("Deployment and infrastructure", r"\b(?:deploy|deployment|docker|kubernetes|infra|ci)\b"),
)
_CHANGE_REQUEST = re.compile(r"\b(?:add|build|create|implement|fix|refactor|update)\b", re.I)
_SUCCESS_CHECK = re.compile(
    r"\b(?:acceptance|expected|should|must|passes?|verify|verification|test|tests)\b", re.I
)
_VERIFICATION = re.compile(r"\b(?:test|tests|testing|verify|verification|pytest|check)\b", re.I)
_BUG_REQUEST = re.compile(r"\b(?:bug|crash|error|failure|broken)\b", re.I)
_REPRODUCTION = re.compile(r"\b(?:reproduc\w*|steps|expected|actual|traceback)\b", re.I)
_REVIEW_REQUEST = re.compile(r"\b(?:review|audit)\b", re.I)


class WorkSuggestion(BaseModel):
    """An actionable improvement supported by recent requests."""

    id: str
    title: str
    reason: str
    action: str


class WorkInsights(BaseModel):
    """What the owner's recent requests show, for the home page and avatar."""

    focus_areas: list[str] = Field(default_factory=list)
    suggestions: list[WorkSuggestion] = Field(default_factory=list)
    avatar: UserAvatar


def _suggestions(requests: Sequence[str]) -> list[WorkSuggestion]:
    changes = [request for request in requests if _CHANGE_REQUEST.search(request)]
    missing_success = sum(not _SUCCESS_CHECK.search(request) for request in changes)
    missing_verification = sum(not _VERIFICATION.search(request) for request in changes)
    missing_reproduction = sum(
        bool(_BUG_REQUEST.search(request)) and not _REPRODUCTION.search(request)
        for request in requests
    )
    reviews = sum(bool(_REVIEW_REQUEST.search(request)) for request in requests)
    suggestions = []
    if missing_success:
        suggestions.append(
            WorkSuggestion(
                id="acceptance-checks",
                title="Make success explicit",
                reason=f"{missing_success} recent change requests did not include clear success checks.",
                action="Include the expected behavior and one edge case in your next change request.",
            )
        )
    if missing_verification:
        suggestions.append(
            WorkSuggestion(
                id="verification",
                title="Ask for verification",
                reason=f"{missing_verification} recent change requests did not mention verification.",
                action="Name the relevant test or check command and ask the agent to report its result.",
            )
        )
    if missing_reproduction:
        suggestions.append(
            WorkSuggestion(
                id="reproduction",
                title="Include a failure reproduction",
                reason=f"{missing_reproduction} recent requests mentioned failures without reproduction steps.",
                action="Provide the triggering input, steps to reproduce, and expected versus actual behavior.",
            )
        )
    if reviews and len(suggestions) < 3:
        suggestions.append(
            WorkSuggestion(
                id="review-scope",
                title="Give reviews a clear scope",
                reason=f"You requested reviews or audits in {reviews} recent messages.",
                action="Name the files or pull request and the risks you want the review to examine.",
            )
        )
    return suggestions[:3]


def build_insights(user_id: str, messages: Sequence[str]) -> WorkInsights:
    """Derive insights from the owner's messages, newest first.

    Only the owner's own lines count: quoted text and code blocks are dropped.
    """
    requests = []
    focus_counts: Counter[str] = Counter()
    for message in messages[:ACTIVITY_LIMIT]:
        request = " ".join(user_sentences(message))
        if not request:
            continue
        requests.append(request)
        for area, pattern in _FOCUS_AREAS:
            if re.search(pattern, request, re.I):
                focus_counts[area] += 1
    focus_areas = [area for area, _ in focus_counts.most_common(6)]
    return WorkInsights(
        focus_areas=focus_areas,
        suggestions=_suggestions(requests),
        avatar=generate_user_avatar(user_id, focus_areas=focus_areas),
    )
