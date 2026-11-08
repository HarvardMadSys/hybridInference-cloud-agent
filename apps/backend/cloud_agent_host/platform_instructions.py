"""What the platform tells an agent about the sandbox it works in.

Short on purpose, and only facts the agent cannot discover itself: how its
git and ``gh`` reach GitHub, who its commits belong to, and what the GitHub
relay refuses. How the work is done, and how a pull request is opened, is the
user's and the repository's to say, as it is for the same CLI on the user's own
machine; the platform adds no policy of its own (decided 2026-11-05).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from cloud_agent_host.resident_transport import ResidentLaunch

#: The REST equivalents of ``gh`` subcommands that use GraphQL, which the
#: GitHub relay refuses.
_REST = """\
- Open a pull request: `gh api repos/{repo}/pulls -f title=... -f head=<branch> -f base=<base> -f body=...` (add `-F draft=true` for a draft)
- Read a pull request: `gh api repos/{repo}/pulls/<number>`
- Its checks: `gh api repos/{repo}/commits/<sha>/check-runs`, and `gh api repos/{repo}/commits/<sha>/status`
- Its reviews and comments: `gh api repos/{repo}/pulls/<number>/reviews`, `gh api repos/{repo}/pulls/<number>/comments`
- Merge it: `gh api -X PUT repos/{repo}/pulls/<number>/merge -f sha=<head sha>`"""


def platform_instructions(launch: ResidentLaunch, *, coauthor_trailer: bool) -> str:
    """The instructions for one member, or ``""`` when it has no repository to act on.

    ``coauthor_trailer`` asks the agent to end its commits with the co-author
    line; a CLI that adds the trailer itself (Claude Code's own attribution)
    is not asked to, or it would write it twice. Each CLI's
    :class:`~cloud_agent_host.runtimes.member_launch.MemberLaunch` says which,
    and places the text where the CLI takes it.
    """
    if not launch.github_credential or not launch.github_repo:
        return ""
    repo = launch.github_repo
    lines = [
        "# Working in this FreeInference sandbox",
        "",
        f"This workspace is a checkout of `{repo}`; `origin` is "
        f"`https://github.com/{repo}.git` (add it with `git remote add origin` if it "
        "is missing). `git push`, `git fetch` and `gh api` act on GitHub as the "
        "user who owns this conversation, for this repository only. Other "
        "repositories are reachable read-only and anonymously.",
        "",
        "Your commits are authored by that user already (GIT_AUTHOR_* is set); do "
        "not change the author.",
    ]
    if launch.git_coauthor and coauthor_trailer:
        lines += [
            "End every commit message with this trailer, after a blank line:",
            f"`Co-Authored-By: {launch.git_coauthor}`",
        ]
    lines += [
        "",
        "`gh` subcommands that use GraphQL (`gh pr create`, `gh pr view`, "
        "`gh pr checks`, `gh pr merge`) do not work here. Use the REST API:",
        _REST.format(repo=repo),
        "",
        "Deleting branches and pushing tags are refused, as is a repository's "
        "administration (settings, webhooks, secrets, branch protection).",
    ]
    return "\n".join(lines) + "\n"
