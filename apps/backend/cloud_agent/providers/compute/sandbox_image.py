"""Find the sandbox image the release pipeline published for one commit.

An automatically configured Host pulls this image instead of building one,
so the groups it creates record a registry digest that any other Host can
fetch: the precondition for restoring them elsewhere from a checkpoint. The
publish workflow tags every published sandbox image ``sandbox-<commit>``.
Only anonymous access is attempted; a private or unreachable registry, or a
commit that was never published, answers ``None`` and the Host builds its
image as before.
"""

from __future__ import annotations

import logging
import os
import re
from typing import Final

import httpx

from cloud_agent_shared.sandbox_restore import pullable_image

logger = logging.getLogger(__name__)

#: Where this repository's release pipeline publishes the sandbox image.
#: A distribution that publishes elsewhere sets its own; empty disables it.
DEFAULT_SANDBOX_IMAGE_REPOSITORY: Final = "ghcr.io/harvardmadsys/cloud-agent-sandbox"
_REPOSITORY_RE: Final = re.compile(r"([a-z0-9.-]+(?::[0-9]{1,5})?)/([a-z0-9]+(?:[._/-][a-z0-9]+)*)")
_BEARER_RE: Final = re.compile(r'(\w+)="([^"]*)"')
_ACCEPT: Final = ", ".join(
    (
        "application/vnd.oci.image.index.v1+json",
        "application/vnd.oci.image.manifest.v1+json",
        "application/vnd.docker.distribution.manifest.list.v2+json",
        "application/vnd.docker.distribution.manifest.v2+json",
    )
)


def sandbox_image_repository() -> str:
    """The configured repository, or ``""`` when automatic setups should build."""
    configured = os.environ.get("AGENT_COMPUTE_SANDBOX_IMAGE_REPOSITORY")
    return (DEFAULT_SANDBOX_IMAGE_REPOSITORY if configured is None else configured).strip()


async def find_published_sandbox_image(
    commit: str,
    *,
    repository: str | None = None,
    transport: httpx.AsyncBaseTransport | None = None,
) -> str | None:
    """Return ``<repository>@<digest>`` for ``sandbox-<commit>``, or ``None``.

    The digest pins exactly the image that was published; a later push to the
    tag cannot change what a Host created from this setup runs.
    """
    repository = sandbox_image_repository() if repository is None else repository
    matched = _REPOSITORY_RE.fullmatch(repository)
    if not repository or matched is None or not re.fullmatch(r"[0-9a-f]{40}", commit):
        return None
    registry, name = matched.groups()
    url = f"https://{registry}/v2/{name}/manifests/sandbox-{commit}"
    try:
        async with httpx.AsyncClient(timeout=10.0, transport=transport) as client:
            response = await client.head(url, headers={"Accept": _ACCEPT})
            challenge = response.headers.get("www-authenticate", "")
            if response.status_code == 401 and challenge.lower().startswith("bearer "):
                fields = dict(_BEARER_RE.findall(challenge))
                realm = fields.pop("realm", "")
                if not realm.startswith("https://"):
                    return None
                token = await client.get(realm, params=fields)
                token.raise_for_status()
                body = token.json()
                bearer = body.get("token") or body.get("access_token")
                if not isinstance(bearer, str) or not bearer:
                    return None
                response = await client.head(
                    url, headers={"Accept": _ACCEPT, "Authorization": f"Bearer {bearer}"}
                )
    except (httpx.HTTPError, ValueError) as exc:
        logger.warning(
            "published sandbox image lookup failed",
            extra={
                "event": "compute_sandbox_image_lookup_failed",
                "error_type": type(exc).__name__,
            },
        )
        return None
    digest = response.headers.get("docker-content-digest", "")
    if response.status_code != 200 or not re.fullmatch(r"sha256:[0-9a-f]{64}", digest):
        return None
    reference = f"{repository}@{digest}"
    return reference if pullable_image(reference) else None


__all__ = [
    "DEFAULT_SANDBOX_IMAGE_REPOSITORY",
    "find_published_sandbox_image",
    "sandbox_image_repository",
]
