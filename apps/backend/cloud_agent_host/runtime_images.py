"""Agent CLI versions this host downloaded, kept as local Docker images.

The sandbox image carries the versions it was built with. A version an admin
updates to later is downloaded by the runner (``runtime_downloads``) into an
image of its own: the sandbox image plus ``/opt/runtimes/<runtime>/<version>``,
tagged here. Docker is the store on purpose. The runner, each turn process and
the sandbox manager are separate processes — the sandbox manager a separate
container — and the daemon is the one thing they already share, so no host
directory has to be mounted anywhere and no process has to ask another what
it holds.

A retained chat never runs *from* one of these images: its container is still
created from the deployment's sandbox image, and the version's files are copied
into its writable layer the first time a member starts (``runtime_install``).
The chat therefore carries its CLI wherever its writable layer goes, and these
images can be replaced or removed without touching any chat.

Images are keyed by the sandbox image they were built on as well, because the
check a version passed is a check against that image's wrappers and Node. A
new release downloads and checks its targets again.
"""

from __future__ import annotations

import re
import subprocess
import threading
import time
from dataclasses import dataclass

from cloud_agent_shared.runtime_catalog import downloadable_runtimes
from cloud_agent_shared.runtime_downloads import is_exact_version

LABEL = "org.freeinference.runtime-download"
#: Which deployment downloaded it. Staging and production share a Docker daemon
#: on some machines, and neither may offer the other's downloads.
NAMESPACE_LABEL = f"{LABEL}.namespace"
RUNTIME_LABEL = f"{LABEL}.runtime"
VERSION_LABEL = f"{LABEL}.version"
#: Transient install and probe containers, so a runner restarted mid-download
#: can remove what its predecessor left.
WORK_LABEL = f"{LABEL}.work"

#: How long a discovered menu is reused. Short: a version that finished
#: downloading should be offered on the next beat, not a minute later.
_CACHE_S = 5.0


@dataclass(frozen=True)
class RuntimeImage:
    """One downloaded version, built on one sandbox image."""

    runtime: str
    version: str
    #: The first 12 hex digits of the sandbox image ID it was built on.
    base: str
    ref: str


def repository(namespace: str) -> str:
    """This deployment's repository name for downloaded versions."""
    name = re.sub(r"[^a-z0-9]+", "-", namespace.lower()).strip("-") or "default"
    return f"{name}-agent-runtime"


def short_id(image_id: str) -> str:
    """``sha256:abcdef…`` → ``abcdef012345``: what tags and labels carry."""
    return image_id.removeprefix("sha256:")[:12]


def image_ref(namespace: str, runtime: str, version: str, base: str) -> str:
    """The tag of ``runtime`` ``version`` built on sandbox image ``base``.

    ``_`` separates the parts: runtime IDs and exact versions never contain
    one, and Docker tags allow it.
    """
    return f"{repository(namespace)}:{runtime}_{version}_{short_id(base)}"


def _parse(ref: str, namespace: str) -> RuntimeImage | None:
    repo, _, tag = ref.rpartition(":")
    if repo != repository(namespace):
        return None
    parts = tag.split("_")
    if len(parts) != 3:
        return None
    runtime, version, base = parts
    if runtime not in downloadable_runtimes() or not is_exact_version(version):
        return None
    if not re.fullmatch(r"[0-9a-f]{12}", base):
        return None
    return RuntimeImage(runtime=runtime, version=version, base=base, ref=ref)


def discover(docker: str, namespace: str, *, timeout: float = 15) -> list[RuntimeImage]:
    """Every downloaded version this deployment holds; empty if Docker cannot say."""
    try:
        listed = subprocess.run(
            [
                docker,
                "image",
                "ls",
                "--filter",
                f"label={NAMESPACE_LABEL}={namespace}",
                "--format",
                "{{.Repository}}:{{.Tag}}",
            ],
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return []
    if listed.returncode != 0:
        return []
    found = (_parse(line.strip(), namespace) for line in listed.stdout.splitlines())
    return [image for image in found if image is not None]


def image_id(docker: str, ref: str, *, timeout: float = 30) -> str | None:
    """The full ID of a local image, or None if it is not there."""
    try:
        inspected = subprocess.run(
            [docker, "image", "inspect", ref, "--format", "{{.Id}}"],
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    value = inspected.stdout.strip()
    return value if inspected.returncode == 0 and value.startswith("sha256:") else None


def _version_key(version: str) -> tuple[int, ...]:
    return tuple(int(part) for part in version.split("-", 1)[0].split("."))


def by_runtime(images: list[RuntimeImage]) -> dict[str, list[str]]:
    """Downloaded versions per runtime, oldest first."""
    versions: dict[str, set[str]] = {}
    for image in images:
        versions.setdefault(image.runtime, set()).add(image.version)
    return {runtime: sorted(found, key=_version_key) for runtime, found in versions.items()}


class DownloadedVersions:
    """A briefly cached :func:`discover`, for the version stamp."""

    def __init__(self, docker: str, namespace: str) -> None:
        self._docker = docker
        self._namespace = namespace
        self._lock = threading.Lock()
        self._read_at = float("-inf")
        self._versions: dict[str, list[str]] = {}

    def versions(self) -> dict[str, list[str]]:
        """Downloaded versions per runtime, at most a few seconds old."""
        with self._lock:
            if time.monotonic() - self._read_at >= _CACHE_S:
                self._versions = by_runtime(discover(self._docker, self._namespace))
                self._read_at = time.monotonic()
            return {runtime: list(found) for runtime, found in self._versions.items()}

    def invalidate(self) -> None:
        """Read Docker again next time: a download has just finished."""
        with self._lock:
            self._read_at = float("-inf")
