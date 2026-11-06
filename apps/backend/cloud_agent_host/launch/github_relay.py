"""How a member's git and ``gh`` reach GitHub, and whose commits they make.

See :mod:`cloud_agent_host.github_relay_env` for the relay itself: what a
member holds is a placeholder (``agh.``) that only the relay accepts, and the
owner is the author and committer of the member's commits.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Self, override

from cloud_agent_host.launch.facet import SECRET, LaunchFacet
from cloud_agent_shared.shared_protocol import is_residency_github_credential

if TYPE_CHECKING:
    from collections.abc import Mapping


@dataclass(frozen=True)
class GitHubRelayFacet(LaunchFacet):
    """The residency's relay placeholder, the repository, and the owner's git identity.

    The placeholder and identity arrive with the residency, so a probe names
    none and borrows a live process's. The repository comes from the turn and
    is compared on its own.
    """

    # Kept in this member's environment, never in shared workspace or image config.
    credential: str = field(repr=False, metadata=SECRET)
    repo: str
    author_name: str
    author_email: str
    # The co-author line for the agent.
    coauthor: str

    @override
    def fingerprint(self) -> Mapping[str, object]:
        return {
            "github": (self.credential, self.author_name, self.author_email, self.coauthor),
            "github_repo": self.repo,
        }

    @override
    def differs_from(self, held: Self) -> tuple[str, ...]:
        names = super().differs_from(held)
        # A probe names no credential or identity: those arrive with the
        # residency, and a live process keeps its own.
        if not self.credential and (
            not held.credential or is_residency_github_credential(held.credential)
        ):
            return tuple(name for name in names if name != "github")
        return names

    @override
    def env(self) -> dict[str, str]:
        env: dict[str, str] = {}
        if self.author_name and self.author_email:
            env.update(
                {
                    "GIT_AUTHOR_NAME": self.author_name,
                    "GIT_AUTHOR_EMAIL": self.author_email,
                    "GIT_COMMITTER_NAME": self.author_name,
                    "GIT_COMMITTER_EMAIL": self.author_email,
                }
            )
        if self.coauthor:
            env["FREEINFERENCE_GIT_COAUTHOR"] = self.coauthor
        if not self.credential:
            return env
        env.update(
            {
                "GH_TOKEN": self.credential,
                "GH_HOST": "github.com",
                "GH_PROMPT_DISABLED": "1",
                "GIT_TERMINAL_PROMPT": "0",
                # Reset inherited helpers before selecting gh. These
                # invocation-only settings never write credentials or helper
                # configuration into the shared .git directory.
                "GIT_CONFIG_COUNT": "3",
                "GIT_CONFIG_KEY_1": "credential.https://github.com.helper",
                "GIT_CONFIG_VALUE_1": "",
                "GIT_CONFIG_KEY_2": "credential.https://github.com.helper",
                "GIT_CONFIG_VALUE_2": "!gh auth git-credential",
            }
        )
        if self.repo:
            env["GH_REPO"] = self.repo
        return env

    @override
    def secrets(self) -> dict[str, str]:
        return {"github_credential": self.credential} if self.credential else {}
