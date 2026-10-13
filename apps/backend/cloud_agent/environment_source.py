"""Read bounded repository facts for preparation tests and script suggestions."""

from __future__ import annotations

import json
from typing import Any
from urllib.parse import quote

import httpx
from fastapi import HTTPException

from cloud_agent.github_app import AppNotInstalled, GitHubAppError


async def read_repository_token(repo: str, credentials: Any) -> str | None:
    """Keep source access read-only and permit anonymous public repositories."""
    if credentials is None:
        return None
    try:
        return await credentials.token_for(
            repo, permissions={"contents": "read"}, repository_scoped=True
        )
    except AppNotInstalled:
        return None
    except GitHubAppError:
        raise HTTPException(503, "Repository credentials are temporarily unavailable") from None


async def _get(repo: str, suffix: str, credentials: Any) -> dict[str, Any]:
    from cloud_agent.github_app import GITHUB_API
    from cloud_agent.settings import setting_env

    api_base = (
        getattr(getattr(credentials, "config", None), "api_base", None)
        or setting_env("AGENT_GITHUB_API", GITHUB_API)
        or GITHUB_API
    )
    api_base = api_base.rstrip("/")
    headers = {"Accept": "application/vnd.github+json"}
    token = await read_repository_token(repo, credentials)
    if token:
        headers["Authorization"] = f"Bearer {token}"
    try:
        async with (
            httpx.AsyncClient(timeout=15) as client,
            client.stream("GET", f"{api_base}/repos/{repo}{suffix}", headers=headers) as response,
        ):
            response.raise_for_status()
            content = bytearray()
            async for chunk in response.aiter_bytes():
                if len(content) + len(chunk) > 2 * 1024 * 1024:
                    raise HTTPException(422, "Repository metadata exceeds size limit")
                content.extend(chunk)
            result = json.loads(content)
            if not isinstance(result, dict):
                raise ValueError("Invalid repository response")
            return result
    except (httpx.HTTPError, ValueError):
        raise HTTPException(
            503, "Repository metadata is unavailable; retry after checking access"
        ) from None


async def resolve_test_commit(repo: str, ref: str | None, credentials: Any) -> str:
    """Pin the selected or default branch without exposing credentials."""
    if not ref:
        ref = str((await _get(repo, "", credentials)).get("default_branch") or "")
    if not ref:
        raise HTTPException(422, "Repository has no default branch")
    commit = await _get(repo, f"/commits/{quote(ref, safe='')}", credentials)
    sha = commit.get("sha")
    if not isinstance(sha, str) or len(sha) != 40 or any(c not in "0123456789abcdef" for c in sha):
        raise HTTPException(422, "Repository ref does not identify a commit")
    return sha


async def script_suggestions(repo: str, ref: str | None, credentials: Any) -> dict[str, Any]:
    """Suggest only commands backed by top-level lockfiles the user can review."""
    sha = await resolve_test_commit(repo, ref, credentials)
    tree = await _get(repo, f"/git/trees/{sha}", credentials)
    paths = {str(item.get("path")) for item in tree.get("tree", []) if item.get("type") == "blob"}
    suggestions = []
    for lock, command in (
        ("package-lock.json", "npm ci"),
        ("pnpm-lock.yaml", "corepack pnpm install --frozen-lockfile"),
        ("yarn.lock", "corepack yarn install --immutable"),
        ("uv.lock", "uv sync --frozen"),
        ("poetry.lock", "poetry install --sync"),
        ("Cargo.lock", "cargo fetch --locked"),
        ("go.sum", "go mod download"),
    ):
        if lock in paths:
            suggestions.append({"file": lock, "script": command})
    return {"commit": sha, "suggestions": suggestions, "requires_choice": len(suggestions) > 1}
