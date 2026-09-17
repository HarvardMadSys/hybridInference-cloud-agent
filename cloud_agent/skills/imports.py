"""Bounded SKILL.md imports; caller URLs never become arbitrary fetch targets."""

from __future__ import annotations

import base64
import binascii
import hashlib
import json
import re
from dataclasses import dataclass
from typing import Any
from urllib.parse import unquote, urlsplit

import httpx
import yaml
from pydantic import ValidationError

from cloud_agent.entitlement import REPO_PATTERN
from cloud_agent.github_app import GitHubAppError, read_repository_contents
from cloud_agent.skills.models import MAX_SKILL_BYTES, SkillContent, SkillError


class SkillImportNotFound(SkillError):
    """The public GitHub request could not access the requested skill."""


@dataclass(frozen=True)
class GitHubSkillLocation:
    """A validated repository, ref and instruction file on github.com."""

    repo: str
    ref: str
    path: str


def github_skill_location(url: str) -> GitHubSkillLocation:
    """Accept repository, skill-directory, or SKILL.md links on github.com."""
    try:
        parsed = urlsplit(url)
    except ValueError as exc:
        raise SkillError("The GitHub URL is invalid.") from exc
    if parsed.scheme != "https" or parsed.netloc.lower() != "github.com":
        raise SkillError(
            "Use an https://github.com/ link to a repository, skill folder, or SKILL.md."
        )
    parts = [unquote(part) for part in parsed.path.strip("/").split("/")]
    if len(parts) < 2 or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9-]{0,38}", parts[0]):
        raise SkillError("The GitHub repository URL is invalid.")
    if not re.fullmatch(REPO_PATTERN, "/".join(parts[:2])):
        raise SkillError("The GitHub repository URL is invalid.")
    if any(not part or part in {".", ".."} or "\\" in part or "\x00" in part for part in parts):
        raise SkillError("The GitHub skill path is invalid.")
    repo = "/".join(parts[:2])
    if len(parts) == 2:
        return GitHubSkillLocation(repo, "HEAD", "SKILL.md")
    if len(parts) < 4 or parts[2] not in {"tree", "blob"}:
        raise SkillError("Link to a GitHub skill folder or its SKILL.md file.")
    ref = parts[3]
    if len(ref) > 255 or any("/" in part for part in parts[4:]):
        raise SkillError("The GitHub skill path is invalid.")
    path = "/".join(parts[4:])
    if parts[2] == "tree":
        path = f"{path}/SKILL.md" if path else "SKILL.md"
    elif not path or parts[-1] != "SKILL.md":
        raise SkillError("Choose a file named SKILL.md.")
    if len(path) > 1024:
        raise SkillError("The GitHub skill path is too long.")
    return GitHubSkillLocation(repo, ref, path)


def parse_skill_document(content: str) -> SkillContent:
    """Read standard YAML name/description metadata and Markdown instructions."""
    if len(content.encode("utf-8")) > MAX_SKILL_BYTES:
        raise SkillError("SKILL.md must be no larger than 64 KB.")
    if "\x00" in content:
        raise SkillError("SKILL.md must be UTF-8 text without NUL characters.")
    normalized = content.lstrip("\ufeff").replace("\r\n", "\n")
    match = re.match(r"\A---\s*\n(.*?)\n---[ \t]*(?:\n|$)(.*)\Z", normalized, re.DOTALL)
    if not match:
        raise SkillError("SKILL.md needs YAML frontmatter with name and description.")
    try:
        metadata = yaml.safe_load(match[1])
    except yaml.YAMLError as exc:
        raise SkillError("SKILL.md has invalid YAML frontmatter.") from exc
    if not isinstance(metadata, dict):
        raise SkillError("SKILL.md needs name and description in its frontmatter.")
    try:
        return SkillContent.model_validate(
            {
                "name": metadata.get("name"),
                "description": metadata.get("description"),
                "instructions": match[2],
            }
        )
    except ValidationError as exc:
        raise SkillError(
            "Provide a name (1-80 characters), description (1-1024 characters), "
            "and nonempty Markdown instructions."
        ) from exc


async def fetch_github_skill(
    url: str,
    *,
    token: str | None = None,
    credentials: Any = None,
    transport: httpx.AsyncBaseTransport | None = None,
) -> dict[str, str]:
    """Read one bounded GitHub API response, ignoring download and redirect URLs."""
    location = github_skill_location(url)
    try:
        if credentials is not None:
            body = await credentials.repository_contents(
                location.repo,
                path=location.path,
                ref=location.ref,
                allow_symbolic_ref=True,
                max_response_bytes=MAX_SKILL_BYTES * 2 + 16384,
            )
        else:
            async with httpx.AsyncClient(
                timeout=15, follow_redirects=False, transport=transport, trust_env=False
            ) as client:
                body = await read_repository_contents(
                    client,
                    api_base="https://api.github.com",
                    repo=location.repo,
                    path=location.path,
                    ref=location.ref,
                    token=token,
                    max_response_bytes=MAX_SKILL_BYTES * 2 + 16384,
                )
        if (
            not isinstance(body, dict)
            or body.get("type") != "file"
            or body.get("encoding") != "base64"
        ):
            raise SkillError("The GitHub URL must resolve to a readable SKILL.md file.")
        encoded = body.get("content")
        revision = body.get("sha")
        if not isinstance(encoded, str) or not isinstance(revision, str):
            raise SkillError("GitHub returned an invalid skill file.")
        if not re.fullmatch(r"(?:[0-9a-f]{40}|[0-9a-f]{64})", revision):
            raise SkillError("GitHub returned an invalid skill revision.")
        raw = base64.b64decode("".join(encoded.split()), validate=True)
        if len(raw) > MAX_SKILL_BYTES:
            raise SkillError("SKILL.md must be no larger than 64 KB.")
        content = raw.decode("utf-8")
    except GitHubAppError as exc:
        if exc.status == 404:
            raise SkillImportNotFound(
                "SKILL.md was not found. Check the URL and repository access."
            ) from exc
        raise SkillError(
            "GitHub could not return this skill. Check access or try again later."
        ) from exc
    except (httpx.HTTPError, UnicodeError, json.JSONDecodeError, binascii.Error, ValueError) as exc:
        if isinstance(exc, SkillError):
            raise
        raise SkillError(
            "Could not read SKILL.md from GitHub. Check the file and try again."
        ) from exc
    skill = parse_skill_document(content)
    return {
        **skill.model_dump(),
        "source": "github",
        "source_url": urlsplit(url)._replace(query="", fragment="").geturl(),
        "source_revision": revision,
        "content_digest": hashlib.sha256(raw).hexdigest(),
    }
