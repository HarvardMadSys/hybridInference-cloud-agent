"""Owner-authorized prompt preparation before a job is submitted."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException

from cloud_agent.api._owner_auth import require_agent_owner
from cloud_agent.api.deps import get_gateway_client, get_grant_client
from cloud_agent.prompt_rewrite import (
    PromptRewriteError,
    PromptRewriteRequest,
    PromptRewriteResponse,
    rewrite_prompt,
)

router = APIRouter(prefix="/v1/agent")


@router.post("/prompts/rewrite", response_model=PromptRewriteResponse)
async def rewrite_agent_prompt(
    body: PromptRewriteRequest,
    user: dict[str, Any] = Depends(require_agent_owner),
    gateway: Any = Depends(get_gateway_client),
    grants: Any = Depends(get_grant_client),
) -> PromptRewriteResponse:
    """Ask clarifications or rewrite a draft using the selected model."""
    try:
        return await rewrite_prompt(body, user_id=user["user_id"], gateway=gateway, grants=grants)
    except PromptRewriteError as exc:
        raise HTTPException(
            status_code=exc.status_code,
            detail={"error": {"type": exc.error_type, "message": str(exc)}},
        ) from None
