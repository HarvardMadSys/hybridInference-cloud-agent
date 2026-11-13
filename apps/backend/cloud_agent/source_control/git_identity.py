"""Who a conversation's commits belong to: its owner, with the agent as co-author."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from cloud_agent.settings import setting_env
from cloud_agent.tokens import mint_residency_github_token

if TYPE_CHECKING:
    from cloud_agent.ports import SourceControlRepository


async def residency_github_fields(
    source_control: SourceControlRepository, thread_id: str, residency: dict[str, Any]
) -> dict[str, str]:
    """What a residency's process gets for GitHub: its relay placeholder and git identity.

    The placeholder is not a GitHub credential: the GitHub relay exchanges it
    for the owner's token while this residency serves a running turn. Like the
    residency's MCP credential it names the residency, so the process keeps it
    for every later turn it serves.
    """
    return {
        "github_credential": mint_residency_github_token(
            thread_id=thread_id, residency_generation=int(residency["generation"])
        ),
        **await git_identity(source_control, str(residency["user_id"])),
    }


async def git_identity(source_control: SourceControlRepository, user_id: str) -> dict[str, str]:
    """The owner as their commits' author, and the agent as its co-author.

    The noreply address is the one GitHub assigns the account, so GitHub shows
    the owner's name and avatar on every commit the agent makes for them.
    Without a connected GitHub account there is no author to name, and the
    agent's commits carry whatever the sandbox would otherwise use.
    """
    account = await source_control.get_github_account(user_id=user_id)
    identity = {"git_coauthor": (setting_env("AGENT_GITHUB_COAUTHOR") or "").strip()}
    if account and account.get("login") and account.get("github_user_id"):
        login = str(account["login"])
        identity["git_author_name"] = login
        identity["git_author_email"] = (
            f"{int(account['github_user_id'])}+{login}@users.noreply.github.com"
        )
    return identity
