"""A deployment's own public content: its Terms of Service and the people behind it.

The deployment keeps these as files in its own repository and mounts them
read-only at :data:`SITE_CONTENT_DIR`, so a change is reviewed there and
reaches the site with the next deployment. They are neither environment
variables nor Admin settings: the environment only seeds settings once, and an
Admin edit would bypass the review the deployment's repository gives them.

Every file is optional::

    site.yaml        source_url and members (the Team page)
    terms.en.txt     Terms of Service, English; blank lines separate paragraphs
    terms.zh-CN.txt  Terms of Service, Simplified Chinese

Terms files, when present, replace the Terms settings in Admin, which then show
read-only. Without them those settings apply as before. Without ``site.yaml``
the footer hides the Team and source links.

An unreadable or invalid file is reported in the log and treated as absent, so a
mistake hides a page rather than stopping sign-in. The deployment's own checks
should refuse it before it gets here.
"""

from __future__ import annotations

import os
import threading
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlsplit

import yaml

from cloud_agent.logging_utils import get_logger

#: Where the control-plane Compose service mounts the deployment's directory.
SITE_CONTENT_DIR = Path("/etc/cloud-agent/site")
#: The UI's languages, in fallback order.
LOCALES = ("en", "zh-CN")
#: The Admin settings that deployment Terms files replace.
TERMS_SETTINGS = ("accounts.terms_url", "accounts.terms_text")

_MAX_FILE_BYTES = 256 * 1024
_MAX_MEMBERS = 50
_MAX_ROLES = 10
_MAX_LABEL = 200
_SITE_KEYS = {"source_url", "members"}
_MEMBER_KEYS = {"name", "website", "badge", "roles"}

logger = get_logger(__name__)


class SiteContentError(ValueError):
    """A site content file that cannot be used as written."""


@dataclass(frozen=True)
class TeamMember:
    """One person on the Team page. Labels map a locale to its text."""

    name: str
    website: str = ""
    badge: dict[str, str] = field(default_factory=dict)
    roles: tuple[dict[str, str], ...] = ()

    def public(self) -> dict[str, object]:
        """The member as the site configuration publishes it."""
        return {
            "name": self.name,
            "website": self.website,
            "badge": dict(self.badge),
            "roles": [dict(role) for role in self.roles],
        }


@dataclass(frozen=True)
class SiteContent:
    """What the deployment's files say; empty where a file is absent."""

    terms: dict[str, str] = field(default_factory=dict)
    source_url: str = ""
    team: tuple[TeamMember, ...] = ()


def _http_url(value: object, where: str) -> str:
    if not isinstance(value, str):
        raise SiteContentError(f"{where} must be a string")
    url = value.strip()
    parts = urlsplit(url)
    if parts.scheme not in ("http", "https") or not parts.netloc or parts.username:
        raise SiteContentError(f"{where} must be an absolute http(s) URL")
    return url


def _text(value: object, where: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise SiteContentError(f"{where} must be non-empty text")
    if len(value) > _MAX_LABEL:
        raise SiteContentError(f"{where} is longer than {_MAX_LABEL} characters")
    return value.strip()


def _localized(value: object, where: str) -> dict[str, str]:
    """Text for every locale, or a mapping of locale to text."""
    if isinstance(value, str):
        text = _text(value, where)
        return dict.fromkeys(LOCALES, text)
    if not isinstance(value, Mapping) or not value:
        raise SiteContentError(f"{where} must be text or a mapping of locale to text")
    unknown = set(value) - set(LOCALES)
    if unknown:
        raise SiteContentError(
            f"{where} names unknown locales: {', '.join(sorted(map(str, unknown)))}"
        )
    return {
        locale: _text(value[locale], f"{where}.{locale}") for locale in LOCALES if locale in value
    }


def _member(value: object, index: int) -> TeamMember:
    where = f"members[{index}]"
    if not isinstance(value, Mapping):
        raise SiteContentError(f"{where} must be a mapping")
    unknown = set(value) - _MEMBER_KEYS
    if unknown:
        raise SiteContentError(f"{where} has unknown keys: {', '.join(sorted(map(str, unknown)))}")
    roles = value.get("roles", [])
    if not isinstance(roles, list) or len(roles) > _MAX_ROLES:
        raise SiteContentError(f"{where}.roles must be a list of at most {_MAX_ROLES}")
    return TeamMember(
        name=_text(value.get("name"), f"{where}.name"),
        website=_http_url(value["website"], f"{where}.website") if "website" in value else "",
        badge=_localized(value["badge"], f"{where}.badge") if "badge" in value else {},
        roles=tuple(_localized(role, f"{where}.roles[{i}]") for i, role in enumerate(roles)),
    )


def parse_site_yaml(raw: str) -> tuple[str, tuple[TeamMember, ...]]:
    """The source link and members ``site.yaml`` declares, or :class:`SiteContentError`."""
    try:
        document = yaml.safe_load(raw)
    except yaml.YAMLError as exc:
        raise SiteContentError(f"site.yaml is not valid YAML: {exc}") from None
    if document is None:
        return "", ()
    if not isinstance(document, Mapping):
        raise SiteContentError("site.yaml must be a mapping")
    unknown = set(document) - _SITE_KEYS
    if unknown:
        raise SiteContentError(
            f"site.yaml has unknown keys: {', '.join(sorted(map(str, unknown)))}"
        )
    source = document.get("source_url")
    source_url = _http_url(source, "source_url") if source else ""
    members = document.get("members") or []
    if not isinstance(members, list) or len(members) > _MAX_MEMBERS:
        raise SiteContentError(f"members must be a list of at most {_MAX_MEMBERS}")
    return source_url, tuple(_member(member, index) for index, member in enumerate(members))


def _read(path: Path) -> str | None:
    """A file's text, or ``None`` when it does not exist."""
    try:
        with path.open("rb") as handle:
            data = handle.read(_MAX_FILE_BYTES + 1)
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise SiteContentError(f"{path.name} cannot be read: {exc.strerror}") from None
    if len(data) > _MAX_FILE_BYTES:
        raise SiteContentError(f"{path.name} is larger than {_MAX_FILE_BYTES} bytes")
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        raise SiteContentError(f"{path.name} is not UTF-8") from None


def read_site_content(directory: Path) -> SiteContent:
    """Read every file, logging and skipping one that cannot be used."""
    terms: dict[str, str] = {}
    for locale in LOCALES:
        try:
            text = (_read(directory / f"terms.{locale}.txt") or "").strip()
        except SiteContentError as exc:
            logger.warning("site_content_ignored %s", exc)
            continue
        if text:
            terms[locale] = text
    source_url: str = ""
    team: tuple[TeamMember, ...] = ()
    try:
        raw = _read(directory / "site.yaml")
        if raw is not None:
            source_url, team = parse_site_yaml(raw)
    except SiteContentError as exc:
        logger.warning("site_content_ignored %s", exc)
    return SiteContent(terms=terms, source_url=source_url, team=team)


_lock = threading.Lock()
_cached: tuple[tuple[object, ...], SiteContent] | None = None


def _fingerprint(directory: Path) -> tuple[object, ...]:
    marks: list[object] = [str(directory)]
    for name in ("site.yaml", *(f"terms.{locale}.txt" for locale in LOCALES)):
        try:
            stat = os.stat(directory / name)
        except OSError:
            marks.append(None)
        else:
            marks.append((stat.st_mtime_ns, stat.st_size, stat.st_ino))
    return tuple(marks)


def site_content(directory: Path | None = None) -> SiteContent:
    """The deployment's content, read again only when a file changed."""
    global _cached
    directory = directory or SITE_CONTENT_DIR
    mark = _fingerprint(directory)
    with _lock:
        if _cached is not None and _cached[0] == mark:
            return _cached[1]
    content = read_site_content(directory)
    with _lock:
        _cached = (mark, content)
    return content


def terms_text_for(terms: Mapping[str, str]) -> str:
    """The text older clients show: English, else the first language given."""
    return terms.get("en") or next(iter(terms.values()), "")
