"""Updating an agent CLI from the admin Settings page.

The admin sees, per agent, the version new chats use and the latest release on
npm, and presses one button to update. Pressing it stores a target
(``HostPoolRepository.request_runtime_upgrade``); every host downloads and
checks the version (``cloud_agent_host.runtime_downloads``); the heartbeat that
reports the last host ready switches new chats over
(``activate_ready_runtime_upgrades``). Existing chats keep their version.

This module holds the two parts of that with no storage of their own: asking
the registry what is current, and reducing the hosts' reports to the one state
and one progress number the page shows.
"""

from __future__ import annotations

import asyncio
import os
import time
from dataclasses import dataclass
from typing import Any
from urllib.parse import quote

import httpx

from cloud_agent.domain.runtimes import KIND_SELECTION, KIND_UPDATE
from cloud_agent.host_pool import HOST_ONLINE
from cloud_agent_shared.runtime_catalog import downloadable_runtimes
from cloud_agent_shared.runtime_downloads import (
    DEFAULT_REGISTRY_URL,
    FAILED,
    READY,
    find_report,
    is_exact_version,
)

#: The npm registry this page reads; set the same value on the hosts.
ENV_RUNTIME_REGISTRY_URL = "AGENT_RUNTIME_REGISTRY_URL"

#: The page's states. ``downloading`` covers fetching and checking alike:
#: the admin waits either way, and the bar moves through both.
STATE_DOWNLOADING = "downloading"
STATE_UPDATED = "updated"
STATE_FAILED = "failed"

#: How long a registry answer is reused. The page polls while an update runs,
#: and npm is not asked every few seconds for something that changes daily.
REGISTRY_CACHE_S = 3600.0
_REGISTRY_TIMEOUT_S = 10.0


def registry_url() -> str:
    """The npm registry this deployment reads, without a trailing slash."""
    return (os.environ.get(ENV_RUNTIME_REGISTRY_URL) or DEFAULT_REGISTRY_URL).strip().rstrip("/")


def _package_path(package: str) -> str:
    # Scoped names keep their ``@``; the slash between scope and name is part
    # of the path the registry expects.
    return quote(package, safe="@/")


def version_key(version: str) -> tuple[tuple[int, int, int], int, str]:
    """Order exact versions: a release after its own prereleases."""
    core, _, pre = version.partition("-")
    major, minor, patch = (int(part) for part in core.split("."))
    return (major, minor, patch), 0 if pre else 1, pre


class RegistryClient:
    """The latest version of each agent package, cached for an hour.

    Never raises: an unreachable registry is a message on the page, not an
    error that takes the page down with it.
    """

    def __init__(
        self,
        *,
        base_url: str | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
        clock: Any = time.monotonic,
    ) -> None:
        self._base_url = base_url
        self._transport = transport
        self._clock = clock
        self._latest: dict[str, tuple[float, str | None, str | None]] = {}
        self._lock = asyncio.Lock()

    def _client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(
            base_url=self._base_url or registry_url(),
            timeout=_REGISTRY_TIMEOUT_S,
            transport=self._transport,
            headers={"accept": "application/json"},
        )

    async def latest(self, package: str) -> tuple[str | None, str | None]:
        """``(version, None)``, or ``(None, why it is unknown)``."""
        async with self._lock:
            cached = self._latest.get(package)
            if cached is not None and self._clock() - cached[0] < REGISTRY_CACHE_S:
                return cached[1], cached[2]
        version: str | None = None
        error: str | None = None
        try:
            async with self._client() as client:
                response = await client.get(f"/{_package_path(package)}/latest")
            if response.status_code != 200:
                error = f"npm answered {response.status_code} for {package}"
            else:
                value = response.json().get("version")
                if is_exact_version(value):
                    version = value
                else:
                    error = f"npm returned no usable version for {package}"
        except (httpx.HTTPError, ValueError, AttributeError) as exc:
            error = f"could not reach the npm registry: {exc.__class__.__name__}"
        async with self._lock:
            # A failure is cached briefly, so a registry outage does not make
            # every poll of the page wait out the timeout again.
            stamp = self._clock() if version else self._clock() - REGISTRY_CACHE_S + 60
            self._latest[package] = (stamp, version, error)
        return version, error

    async def has_version(self, package: str, version: str) -> bool | None:
        """Whether npm publishes ``package@version``; None if npm cannot say."""
        try:
            async with self._client() as client:
                response = await client.get(f"/{_package_path(package)}/{quote(version)}")
        except httpx.HTTPError:
            return None
        if response.status_code == 404:
            return False
        return True if response.status_code == 200 else None


@dataclass(frozen=True)
class UpgradeState:
    """One runtime's update, as the page shows it."""

    version: str
    state: str
    progress: int
    error: str | None
    previous_version: str | None
    requested_at: str | None
    activated_at: str | None
    #: ``update`` for the Update button's, ``selection`` for an admin's own
    #: choice that some host could not download.
    kind: str = KIND_UPDATE


def _eligible(hosts: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """The hosts an update waits for: the ones a new chat could be given to."""
    return [host for host in hosts if host["status"] == HOST_ONLINE and host["enrolled"]]


def default_version(runtime: str, hosts: list[dict[str, Any]]) -> str | None:
    """The image default every online host agrees on, or None."""
    versions = {
        host["runtime_versions"].get(runtime)
        for host in _eligible(hosts)
        if host["runtime_versions"].get(runtime)
    }
    return versions.pop() if len(versions) == 1 else None


def upgrade_state(
    runtime: str, target: dict[str, Any] | None, hosts: list[dict[str, Any]]
) -> UpgradeState | None:
    """Reduce every host's report on ``target`` to one state and one number.

    Failed if any online host failed this request (by nonce). Otherwise
    updated once new chats were switched, and downloading until then, with
    the mean of the hosts' progress: a host that already offers the version
    counts as done, one that has not reported yet as not started.

    A host checking a done update again — after a new sandbox image — does not
    put the page back into "downloading": new chats already use the version.

    A version the admin chose by hand, Roll back included, is shown only when
    a host failed to download it: there is no update to follow, but a host
    without it starts new chats on its own default.
    """
    if not isinstance(target, dict) or not is_exact_version(target.get("version")):
        return None
    version = str(target["version"])
    nonce = str(target.get("nonce") or "")
    eligible = _eligible(hosts)
    failures: list[str] = []
    progress: list[int] = []
    for host in eligible:
        report = find_report(host["runtime_downloads"], runtime, version)
        if report is not None and report.nonce != nonce:
            report = None  # about an earlier request
        if report is not None and report.state == FAILED:
            failures.append(f"{host['host']}: {report.error or 'download failed'}")
        if version in (host["runtime_versions_available"].get(runtime) or []) or (
            report is not None and report.state == READY
        ):
            progress.append(100)
        else:
            progress.append(0 if report is None else report.progress)
    activated = target.get("activated_at")
    kind = KIND_SELECTION if target.get("kind") == KIND_SELECTION else KIND_UPDATE
    if kind == KIND_SELECTION and not failures:
        return None
    if failures:
        state, error = STATE_FAILED, "; ".join(failures)
    elif activated:
        state, error = STATE_UPDATED, None
    else:
        state, error = STATE_DOWNLOADING, None
    if state == STATE_UPDATED:
        percent = 100
    elif progress:
        percent = round(sum(progress) / len(progress))
    else:
        percent = 0
    # Never 100 before the switch: the bar finishing is the promise that new
    # chats now use the version.
    if state == STATE_DOWNLOADING:
        percent = min(percent, 99)
    return UpgradeState(
        version=version,
        state=state,
        progress=percent,
        error=error,
        previous_version=target.get("previous_version"),
        requested_at=target.get("requested_at"),
        activated_at=activated,
        kind=kind,
    )


def is_newer(candidate: str | None, current: str | None) -> bool:
    """Whether ``candidate`` is a later release than ``current``."""
    if not is_exact_version(candidate):
        return False
    if not is_exact_version(current):
        return True
    return version_key(str(candidate)) > version_key(str(current))


def update_runtimes() -> list[str]:
    """The runtimes this page can update, in display order."""
    return sorted(downloadable_runtimes())
