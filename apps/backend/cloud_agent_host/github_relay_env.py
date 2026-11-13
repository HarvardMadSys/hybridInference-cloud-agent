"""How a member's git and ``gh`` reach GitHub through the GitHub relay.

The sandbox resolves ``github.com`` and ``api.github.com`` to the relay's
tunnel on its own bridge (the tunnel carries those two names as relay
aliases). The relay terminates TLS with a certificate this deployment's CA
signed, which every process in the sandbox trusts, credential or not
(:mod:`cloud_agent_host.github_relay_trust`).

What a member holds is a placeholder (``agh.``) that GitHub refuses; the relay
exchanges it for the owner's own token. git gets it from ``gh auth
git-credential``, through invocation-only ``GIT_CONFIG_*`` settings that never
touch the shared ``.git``.

The owner is the author and committer of the member's commits; the agent adds
itself as co-author with the trailer the platform instructions spell out. The
host also sets ``FREEINFERENCE_GIT_COAUTHOR`` to the same value, which nothing
reads.

The variables a member's CLI gets for it are
:class:`cloud_agent_host.launch.github_relay.GitHubRelayFacet`'s.
"""

from __future__ import annotations

import os
from typing import Any, TypedDict

#: The PEM of this deployment's GitHub relay CA, on the sandbox manager's host.
#: Set, the relay is in use: groups carry the relay's two names, and every
#: process in them trusts the CA.
RELAY_CA_FILE_ENV = "AGENT_SANDBOX_GITHUB_RELAY_CA_FILE"
#: The largest CA file read; a CA certificate is a couple of kilobytes.
_MAX_CA_BYTES = 64 * 1024


def relay_configured() -> bool:
    """Whether this host sends ``github.com`` to the GitHub relay.

    The same signal the egress rules use: :data:`RELAY_CA_FILE_ENV` is set.
    A member's placeholder credential authenticates only then. Without it,
    ``github.com`` is the public host, which refuses that placeholder.
    """
    return bool((os.environ.get(RELAY_CA_FILE_ENV) or "").strip())


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
