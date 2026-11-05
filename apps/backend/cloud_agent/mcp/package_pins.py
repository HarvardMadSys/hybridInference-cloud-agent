"""Pinning a pasted command's package to the release that is current now.

A sandbox server runs one exactly pinned package (``launch._check_sandbox``):
``npx some-server`` would install whatever the registry publishes next, in a
sandbox where the server holds its keys. Almost no configuration anyone copies
names a version, though. READMEs say ``npx -y @scope/server`` or
``@scope/server@latest``, so refusing those made the most common paste fail.

An import therefore asks the registry, once, which release the pasted package
means today (no version, or a tag such as ``latest``) and writes that exact
version into the command the owner confirms and the platform stores. The
version is still decided once, when it is saved, and shown before it is: it
changes only when the owner edits the connection.

What it cannot pin stays as pasted, with a warning that says what to do, and
saving it is refused as before: a range (``^1.2``), a package or tag the
registry does not know, or a registry that cannot be reached.

The registries are the public ones the sandbox's ``npx`` and ``uvx`` install
from.
"""

from __future__ import annotations

import asyncio
import re
import time
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING, Any
from urllib.parse import quote

import httpx

from cloud_agent.logging_utils import get_logger
from cloud_agent.mcp.launch import LaunchPlanError, is_exact_release, split_package_reference

if TYPE_CHECKING:
    from collections.abc import Callable

    from cloud_agent.mcp.config_import import ImportDraft, ImportResult

logger = get_logger(__name__)

NPM_REGISTRY = "https://registry.npmjs.org"
PYPI_JSON_API = "https://pypi.org/pypi"
LOOKUP_TIMEOUT_S = 5.0
#: How long a release a registry named is reused. A preview runs on each edit
#: of the paste, and saving it asks again.
CACHE_S = 600.0
MAX_CACHED = 1024
#: How many lookups one import runs at once.
CONCURRENT_LOOKUPS = 4

_RUNNERS = {"npx": "npm", "uvx": "pypi"}
_NPM_NAME = re.compile(r"^(?:@[A-Za-z0-9][\w.~-]*/)?[A-Za-z0-9][\w.~-]*$")
_PYPI_NAME = re.compile(
    r"^(?P<name>[A-Za-z0-9](?:[A-Za-z0-9._-]*[A-Za-z0-9])?)(?:\[[A-Za-z0-9._,-]+\])?$"
)
#: A dist-tag (``latest``, ``next``): a name, not a version or a range.
_TAG = re.compile(r"^[A-Za-z][A-Za-z0-9._-]{0,63}$")


@dataclass(frozen=True)
class _Package:
    """The package a command starts, as the paste names it."""

    ecosystem: str
    #: The name the registry knows it by, without extras.
    name: str
    #: The name as the command writes it, extras included.
    written: str
    #: The version or tag the paste gave; ``None`` for a bare name.
    version: str | None
    #: A PyPI range (``>=1.2``, ``~=1.2``): not one release, and not a tag.
    ranged: bool = False
    #: Written the way the installer takes one release: ``@`` for npm, ``==`` for uv.
    as_release: bool = True

    def pinned(self, version: str) -> str:
        """The package reference that names exactly ``version``."""
        separator = "@" if self.ecosystem == "npm" else "=="
        return f"{self.written}{separator}{version}"

    @property
    def exact(self) -> bool:
        """Whether the paste already names one release as the installer needs it."""
        return self.version is not None and is_exact_release(self.version) and self.as_release


class Releases:
    """The release a public registry names for a package now, cached briefly."""

    def __init__(
        self,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._transport = transport
        self._clock = clock
        self._found: dict[tuple[str, str, str], tuple[float, str]] = {}

    async def current(self, ecosystem: str, name: str, tag: str) -> str | None:
        """The exact version ``tag`` names for ``name``, or ``None``. Never raises."""
        key = (ecosystem, name, tag)
        cached = self._found.get(key)
        if cached is not None and self._clock() - cached[0] < CACHE_S:
            return cached[1]
        try:
            version = await self._lookup(ecosystem, name, tag)
        except (httpx.HTTPError, ValueError) as exc:
            logger.info(
                "agent_mcp_package_lookup_failed",
                extra={
                    "event": "agent_mcp_package_lookup_failed",
                    "ecosystem": ecosystem,
                    "package": name,
                    "error": type(exc).__name__,
                },
            )
            return None
        if version is not None:
            if len(self._found) >= MAX_CACHED:
                self._found.clear()
            self._found[key] = (self._clock(), version)
        return version

    async def _lookup(self, ecosystem: str, name: str, tag: str) -> str | None:
        if ecosystem == "npm":
            url = f"{NPM_REGISTRY}/{quote(name, safe='@/')}/{quote(tag, safe='')}"
        else:
            # PEP 503's normal form, which PyPI serves without a redirect.
            url = f"{PYPI_JSON_API}/{quote(re.sub(r'[-_.]+', '-', name).lower(), safe='')}/json"
        async with httpx.AsyncClient(
            timeout=LOOKUP_TIMEOUT_S,
            transport=self._transport,
            headers={"accept": "application/json"},
        ) as client:
            response = await client.get(url)
        if response.status_code != 200:
            return None
        document: Any = response.json()
        if ecosystem == "pypi":
            document = document.get("info") if isinstance(document, dict) else None
        value = document.get("version") if isinstance(document, dict) else None
        return value if isinstance(value, str) and is_exact_release(value) else None


#: The lookups imports use. Unit tests replace it, so none reaches a registry.
releases = Releases()


def _package(ecosystem: str, reference: str) -> _Package | None:
    """The package ``reference`` names, or ``None`` for one this cannot read."""
    try:
        written, version = split_package_reference(reference)
    except LaunchPlanError:
        return None
    if ecosystem == "npm":
        if not _NPM_NAME.match(written):
            return None
        return _Package("npm", written, written, version)
    match = _PYPI_NAME.match(written)
    if match is None:
        return None
    as_release = "==" in reference
    ranged = version is not None and not as_release and "@" not in reference
    return _Package("pypi", match["name"], written, version, ranged, as_release)


def _target(draft: ImportDraft) -> tuple[int, _Package] | None:
    """Which argument names the draft's package, and that package."""
    launch = draft.launch
    if launch is None:
        return None
    ecosystem = _RUNNERS.get(launch.command.replace("\\", "/").rsplit("/", 1)[-1].lower())
    # The package is the first argument that is not a flag, as the launch
    # plan reads it (``launch._install_for``).
    index = next((i for i, arg in enumerate(launch.args) if not arg.startswith("-")), None)
    if ecosystem is None or index is None:
        return None
    package = _package(ecosystem, launch.args[index])
    return (index, package) if package is not None else None


async def _release(package: _Package) -> tuple[str | None, str | None]:
    """The release to pin and what the owner is told, or ``None`` and why not."""
    example = package.pinned("1.2.3")
    if package.version is not None and is_exact_release(package.version) and not package.ranged:
        return package.version, None  # ``uvx pkg@1.2.3``: rewritten as ``==``, nothing to say
    tag = package.version or "latest"
    if package.ranged or not _TAG.match(tag) or (package.ecosystem == "pypi" and tag != "latest"):
        return None, f"name one release of {package.name}, for example {example}"
    found = await releases.current(package.ecosystem, package.name, tag)
    if found is None:
        return None, (
            f"the current release of {package.name} could not be looked up; "
            f"name one, for example {example}"
        )
    return found, f"pinned to {package.name} {found}, its current release"


async def pin_draft(draft: ImportDraft) -> ImportDraft:
    """``draft`` with its package pinned to one release, or as pasted with a warning."""
    target = _target(draft)
    if target is None or draft.launch is None or target[1].exact:
        return draft
    index, package = target
    version, note = await _release(package)
    warnings = (*draft.warnings, note) if note else draft.warnings
    if version is None:
        return replace(draft, warnings=warnings)
    args = list(draft.launch.args)
    args[index] = package.pinned(version)
    return replace(draft, launch=replace(draft.launch, args=tuple(args)), warnings=warnings)


async def pin_import(result: ImportResult) -> ImportResult:
    """``result`` with each command's package pinned where it can be."""
    gate = asyncio.Semaphore(CONCURRENT_LOOKUPS)

    async def one(draft: ImportDraft) -> ImportDraft:
        async with gate:
            return await pin_draft(draft)

    drafts = await asyncio.gather(*(one(draft) for draft in result.drafts))
    return replace(result, drafts=tuple(drafts))


__all__ = ["Releases", "pin_draft", "pin_import", "releases"]
