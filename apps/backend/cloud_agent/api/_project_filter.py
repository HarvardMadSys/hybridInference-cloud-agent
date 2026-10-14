"""The owner's project filter on conversation lists: one repository, or none.

A list can be narrowed to one repository (``repo``) or to the temporary
workspaces, the conversations with no repository (``temporary``). The two
select disjoint sets, so asking for both is a contradiction rather than an
empty page, and it is refused the same way on every list that offers them.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import HTTPException, Query

#: Shared by every list that filters by project, so the parameter reads alike.
TemporaryFilter = Annotated[
    bool,
    Query(description="Only temporary workspaces: the conversations without a repository."),
]


def require_single_project_filter(repo: str | None, temporary: bool) -> None:
    """Refuse a filter naming a repository and the temporary workspaces at once."""
    if repo is not None and temporary:
        raise HTTPException(
            status_code=422,
            detail={
                "error": {
                    "type": "invalid_request",
                    "message": "repo and temporary cannot be combined: "
                    "a temporary workspace has no repository.",
                }
            },
        )
