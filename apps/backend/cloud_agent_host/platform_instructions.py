"""What the platform tells an agent about the sandbox it works in.

Short on purpose, and only facts the agent cannot discover itself: how its
git and ``gh`` reach GitHub, who its commits belong to, and that opening a pull
request is its own job. The repository's own instructions still govern how the
work is done; they are not loaded automatically here (a shared workspace's
project settings are untrusted launch input), so the agent is told to read them.
"""

from __future__ import annotations

import json
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


def platform_instructions(launch: ResidentLaunch) -> str:
    """The instructions for one member, or ``""`` when it has no repository to act on."""
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
    # Claude Code adds the trailer itself (``claude_attribution``); asked to as
    # well, it would write it twice.
    if launch.git_coauthor and launch.runtime_id != "claude-code":
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


def with_platform_instructions(
    runtime_id: str, argv: list[str], env: dict[str, str], instructions: str
) -> tuple[list[str], dict[str, str]]:
    """Hand ``instructions`` to one CLI the way that CLI takes extra system instructions.

    Each was checked against its pinned release:

    - Claude Code and pi: ``--append-system-prompt``, appended last so the slots
      a resident session finds by index stay where they are.
    - Codex: the ``developer_instructions`` config key, as a ``-c`` override
      before the ``app-server`` subcommand (a JSON string is a TOML basic string).
    - OpenCode and Kilo: their config's ``instructions`` file list, which the
      image's ``<name>-freeinference`` wrapper writes from
      ``<NAME>_FREEINFERENCE_INSTRUCTIONS`` beside the config it already writes.
    """
    if not instructions:
        return argv, env
    if runtime_id in {"claude-code", "pi"}:
        return [*argv, "--append-system-prompt", instructions], env
    if runtime_id == "codex" and "app-server" in argv:
        at = argv.index("app-server")
        override = ["-c", f"developer_instructions={json.dumps(instructions)}"]
        return [*argv[:at], *override, *argv[at:]], env
    if runtime_id in {"opencode", "kilo"}:
        return argv, {**env, f"{runtime_id.upper()}_FREEINFERENCE_INSTRUCTIONS": instructions}
    return argv, env


def claude_attribution(launch: ResidentLaunch) -> dict[str, dict[str, str]]:
    """Claude Code's own attribution settings, naming the platform's co-author.

    Claude Code adds its attribution to the commits and pull requests it
    writes; here the co-author is the deployment's, and the pull request
    description carries none. Empty without a co-author: Claude's defaults stand.
    """
    if not launch.git_coauthor:
        return {}
    return {"attribution": {"commit": f"Co-Authored-By: {launch.git_coauthor}", "pr": ""}}
