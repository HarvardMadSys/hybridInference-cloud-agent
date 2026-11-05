"""What the platform tells an agent about the sandbox it works in.

Short on purpose, and only facts the agent cannot discover itself: how its
git and ``gh`` reach GitHub, who its commits belong to, and that opening a pull
request is its own job. The repository's own instructions still govern how the
work is done; they are not loaded automatically here (a shared workspace's
project settings are untrusted launch input), so the agent is told to read them.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from cloud_agent_host.resident_transport import ResidentLaunch

#: The REST equivalents of ``gh`` subcommands that use GraphQL, which the
#: GitHub relay refuses.
_REST = """\
- Open a pull request: `gh api repos/{repo}/pulls -f title=... -f head=<branch> -f base=<base> -F draft=true -f body=...`
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
        "When the user asks for a pull request, open it yourself; do not ask "
        "whether to, and do not tell them to use a button. Before you do:",
        "1. Read the repository's own instructions (AGENTS.md, CLAUDE.md, "
        "CONTRIBUTING.md) and run the checks they require before a pull request. "
        "They are not loaded for you automatically here.",
        "2. Work on a new branch; never push to the default branch.",
        "3. Open it as a draft unless the user says otherwise, with a description "
        "that says what changed, why, which checks you ran with their results, "
        "and anything you could not run.",
        "",
        "`gh` subcommands that use GraphQL (`gh pr create`, `gh pr view`, "
        "`gh pr checks`, `gh pr merge`) do not work here. Use the REST API:",
        _REST.format(repo=repo),
        "",
        "Deleting branches and pushing tags are refused, as is a repository's "
        "administration (settings, webhooks, secrets, branch protection).",
    ]
    return "\n".join(lines) + "\n"
