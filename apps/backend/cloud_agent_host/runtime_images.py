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

import contextlib
import hashlib
import json
import re
import subprocess
import threading
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING

from cloud_agent_host.chat_sandbox_models import (
    CHAT_NAMESPACE_LABEL,
    CHAT_RUNTIME_LABEL,
    CHAT_RUNTIME_VERSION_LABEL,
)
from cloud_agent_shared.runtime_catalog import downloadable_runtimes
from cloud_agent_shared.runtime_downloads import is_exact_version

if TYPE_CHECKING:
    from collections.abc import Callable, Collection

LABEL = "org.freeinference.runtime-download"
#: Which deployment downloaded it. Staging and production share a Docker daemon
#: on some machines, and neither may offer the other's downloads.
NAMESPACE_LABEL = f"{LABEL}.namespace"
RUNTIME_LABEL = f"{LABEL}.runtime"
VERSION_LABEL = f"{LABEL}.version"
#: Transient install and probe containers, so a runner restarted mid-download
#: can remove what its predecessor left.
WORK_LABEL = f"{LABEL}.work"
#: The stopped container a chat copies a downloaded version out of
#: (``runtime_install``). The copy removes it; one its process died before
#: removing pins the version's image until the downloader removes it, once it
#: is too old to belong to a copy still running.
SOURCE_LABEL = f"{LABEL}.source"

#: How long a discovered menu is reused. Short: a version that finished
#: downloading should be offered on the next beat, not a minute later.
_CACHE_S = 5.0
#: How long what chats run is reused for a menu too long to report. Read only
#: then, but a host can stay that way a while, and every beat and claim asks.
_IN_USE_CACHE_S = 60.0


@dataclass(frozen=True)
class RuntimeImage:
    """One downloaded version, built on one sandbox image."""

    runtime: str
    version: str
    #: The first 12 hex digits of the sandbox image ID it was built on.
    base: str
    ref: str


def _readable(namespace: str) -> str:
    """``namespace`` lowercased, every run of anything but ``a-z0-9`` folded to ``-``."""
    return re.sub(r"[^a-z0-9]+", "-", namespace.lower()).strip("-") or "default"


def repository(namespace: str) -> str:
    """This deployment's repository name for downloaded versions.

    The namespace, readable, then the first 12 hex digits of its SHA-256.
    Reading it loses case and punctuation: ``foo_bar`` and ``foo-bar`` read
    the same, and two such deployments on one Docker daemon would share a
    repository, each download moving the other's tag off its image. The
    digest of the exact namespace keeps them apart, as it does their bridges
    and tables (``egress``).
    """
    digest = hashlib.sha256(namespace.encode()).hexdigest()[:12]
    return f"{_readable(namespace)}-agent-runtime-{digest}"


def legacy_repository(namespace: str) -> str:
    """Where earlier releases kept downloads, shared by every namespace that reads the same.

    Hosts still hold versions there. They are offered, used and removed as
    before; nothing new is kept there.
    """
    return f"{_readable(namespace)}-agent-runtime"


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
    # The old name is shared, but the listing holds only images labelled with
    # this exact namespace: under either name, they are this deployment's.
    if repo not in (repository(namespace), legacy_repository(namespace)):
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
    return discover_or_none(docker, namespace, timeout=timeout) or []


def discover_or_none(
    docker: str, namespace: str, *, timeout: float = 15
) -> list[RuntimeImage] | None:
    """Every downloaded version this deployment holds; ``None`` if Docker cannot say.

    For a caller that must not take a failed listing for holding nothing, such
    as a cleanup deciding whether it is still owed.
    """
    listed = _read(
        docker,
        [
            "image",
            "ls",
            "--filter",
            f"label={NAMESPACE_LABEL}={namespace}",
            "--format",
            "{{.Repository}}:{{.Tag}}",
        ],
        timeout,
    )
    if listed is None:
        return None
    found = (_parse(line.strip(), namespace) for line in listed.splitlines())
    return [image for image in found if image is not None]


def _read(docker: str, args: list[str], timeout: float) -> str | None:
    """What one Docker command prints, or None if it fails."""
    try:
        done = subprocess.run(
            [docker, *args], capture_output=True, text=True, timeout=timeout, check=False
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return done.stdout if done.returncode == 0 else None


def chat_versions(
    docker: str, namespace: str, *, timeout: float = 15
) -> set[tuple[str, str]] | None:
    """The (runtime, version) each chat container and checkpoint image of ``namespace`` runs.

    Containers count in any state. So do checkpoint images: ``docker commit``
    keeps a container's labels, so a checkpoint carries its chat's. ``None``
    when Docker cannot say, which a caller removing images must read as every
    version being in use.
    """
    owned = [
        "--filter",
        f"label={CHAT_NAMESPACE_LABEL}={namespace}",
        "--filter",
        f"label={CHAT_RUNTIME_VERSION_LABEL}",
    ]
    fields = "\t".join(
        '{{.Label "' + label + '"}}' for label in (CHAT_RUNTIME_LABEL, CHAT_RUNTIME_VERSION_LABEL)
    )
    containers = _read(docker, ["ps", "--all", *owned, "--format", fields], timeout)
    images = _read(docker, ["image", "ls", "--quiet", "--no-trunc", *owned], timeout)
    if containers is None or images is None:
        return None
    found: set[tuple[str, str]] = set()
    for line in containers.splitlines():
        runtime, _, version = line.partition("\t")
        found.add((runtime, version))
    ids = sorted(set(images.split()))
    if ids:
        # ``image ls`` cannot print labels; one inspect reads them all.
        inspected = _read(
            docker, ["image", "inspect", "--format", "{{json .Config.Labels}}", *ids], timeout
        )
        if inspected is None:
            return None
        for line in inspected.splitlines():
            with contextlib.suppress(ValueError):
                labels = json.loads(line)
                if isinstance(labels, dict):
                    found.add(
                        (
                            str(labels.get(CHAT_RUNTIME_LABEL) or ""),
                            str(labels.get(CHAT_RUNTIME_VERSION_LABEL) or ""),
                        )
                    )
    return {pair for pair in found if all(pair)}


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
        #: The versions this host was told to hold. The runner wires in its
        #: downloader's; a process with none has no targets.
        self.targets: Callable[[], Collection[tuple[str, str]]] | None = None
        self._in_use: set[tuple[str, str]] = set()
        self._in_use_at = float("-inf")

    def versions(self) -> dict[str, list[str]]:
        """Downloaded versions per runtime, at most a few seconds old."""
        with self._lock:
            if time.monotonic() - self._read_at >= _CACHE_S:
                self._versions = by_runtime(discover(self._docker, self._namespace))
                self._read_at = time.monotonic()
            return {runtime: list(found) for runtime, found in self._versions.items()}

    def needed(self) -> set[tuple[str, str]]:
        """What a menu too long to report keeps first: targets, and what chats run.

        What chats run is at most a minute old; when Docker cannot say, the
        last answer stands.
        """
        with self._lock:
            if time.monotonic() - self._in_use_at >= _IN_USE_CACHE_S:
                found = chat_versions(self._docker, self._namespace)
                if found is not None:
                    self._in_use = found
                self._in_use_at = time.monotonic()
            needed = set(self._in_use)
        if self.targets is not None:
            needed.update(self.targets())
        return needed

    def invalidate(self) -> None:
        """Read Docker again next time: a download has just finished."""
        with self._lock:
            self._read_at = float("-inf")
