"""How a member's git and ``gh`` reach GitHub through the GitHub relay.

The sandbox resolves ``github.com`` and ``api.github.com`` to the relay's
tunnel on its own bridge (the tunnel carries those two names as relay
aliases). The relay terminates TLS with a certificate this deployment's CA
signed, so a member trusts that CA, and only for its own processes: the
combined bundle lives in the member's private tmpfs.

What a member holds is a placeholder (``agh.``) that GitHub refuses; the relay
exchanges it for the owner's own token. git gets it from ``gh auth
git-credential``, through invocation-only ``GIT_CONFIG_*`` settings that never
touch the shared ``.git``.

The owner is the author and committer of the member's commits; the agent adds
itself as co-author (``FREEINFERENCE_GIT_COAUTHOR``, named in the platform
instructions).
"""

from __future__ import annotations

import os
from typing import TYPE_CHECKING, Any, TypedDict

from cloud_agent_host.member_paths import member_ca_bundle_path, member_relay_ca_path

if TYPE_CHECKING:
    from cloud_agent_host.resident_transport import ResidentLaunch

#: The PEM of this deployment's GitHub relay CA, on the sandbox manager's host.
#: Set, the relay is in use: groups carry the relay's two names, and members
#: trust the CA.
RELAY_CA_FILE_ENV = "AGENT_SANDBOX_GITHUB_RELAY_CA_FILE"
#: The largest CA file read; a CA certificate is a couple of kilobytes.
_MAX_CA_BYTES = 64 * 1024


def relay_ca_pem() -> str | None:
    """This deployment's relay CA certificate, or ``None`` with no relay configured."""
    path = (os.environ.get(RELAY_CA_FILE_ENV) or "").strip()
    if not path:
        return None
    with open(path, "rb") as handle:
        data = handle.read(_MAX_CA_BYTES + 1)
    if len(data) > _MAX_CA_BYTES or b"-----BEGIN CERTIFICATE-----" not in data:
        raise ValueError(f"{RELAY_CA_FILE_ENV} does not name a PEM certificate")
    return data.decode("ascii")


def github_relay_env(launch: ResidentLaunch, *, relay_ca: bool | None = None) -> dict[str, str]:
    """The variables one member's CLI gets for GitHub, and nothing for a launch without it."""
    env: dict[str, str] = {}
    if launch.git_author_name and launch.git_author_email:
        env.update(
            {
                "GIT_AUTHOR_NAME": launch.git_author_name,
                "GIT_AUTHOR_EMAIL": launch.git_author_email,
                "GIT_COMMITTER_NAME": launch.git_author_name,
                "GIT_COMMITTER_EMAIL": launch.git_author_email,
            }
        )
    if launch.git_coauthor:
        env["FREEINFERENCE_GIT_COAUTHOR"] = launch.git_coauthor
    if not launch.github_credential:
        return env
    env.update(
        {
            "GH_TOKEN": launch.github_credential,
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
    if launch.github_repo:
        env["GH_REPO"] = launch.github_repo
    if relay_ca if relay_ca is not None else bool(os.environ.get(RELAY_CA_FILE_ENV, "").strip()):
        bundle = member_ca_bundle_path(launch.slot_id)
        env.update(
            {
                # Go (gh) and OpenSSL read SSL_CERT_FILE; git's libcurl and the
                # curl CLI are told explicitly, as are Python requests and Node.
                "SSL_CERT_FILE": bundle,
                "GIT_SSL_CAINFO": bundle,
                "CURL_CA_BUNDLE": bundle,
                "REQUESTS_CA_BUNDLE": bundle,
                "NODE_EXTRA_CA_CERTS": member_relay_ca_path(launch.slot_id),
            }
        )
    return env


class ResidencyGitHubLaunch(TypedDict):
    """The GitHub fields of a resident launch that arrive with its residency."""

    github_credential: str
    git_author_name: str
    git_author_email: str
    git_coauthor: str


def residency_github_launch(repo: str | None, residency: dict[str, Any]) -> ResidencyGitHubLaunch:
    """The GitHub half of a residency's launch: placeholder and the owner's git identity.

    A conversation without a repository gets the identity alone, so a commit
    it makes is still the owner's, but no placeholder: there is no repository
    for the relay to let it act on.
    """
    return ResidencyGitHubLaunch(
        github_credential=str(residency.get("github_credential") or "") if repo else "",
        git_author_name=str(residency.get("git_author_name") or ""),
        git_author_email=str(residency.get("git_author_email") or ""),
        git_coauthor=str(residency.get("git_coauthor") or ""),
    )
