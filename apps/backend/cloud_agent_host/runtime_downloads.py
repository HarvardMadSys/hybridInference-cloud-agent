"""Download the agent CLI versions an admin updated to, and check each one.

Every heartbeat answer carries the control plane's *targets*: exact versions,
at most two per runtime — the one an update moves new chats to and the one it
replaced, which Roll back returns to after the switch
(``cloud_agent_shared.runtime_downloads``). This runner keeps each one
installed as a local image (``runtime_images``) built on the sandbox image it
runs, and reports how far it has got on the next beat. The control plane
switches new chats to a version once every online host reports it ready.

Installing a version is four steps, each in a container of its own with every
capability dropped, and only the first has a network:

1. **Fetch.** ``npm install --ignore-scripts``: npm — part of the reviewed
   image — downloads and unpacks the package and its dependencies, checking
   each against the registry's integrity hash, and runs nothing else. ``--before``
   pins dependency resolution to the moment the admin asked, so every host
   installs the same tree.
2. **Install scripts, offline.** ``npm rebuild`` runs the packages' own install
   scripts (Claude Code's puts its native binary in place) with no network,
   then the binary must report exactly the version asked for. The finished
   tree is copied onto a fresh container of the sandbox image, so the image
   kept is one layer holding the tree once, not the fetch and the scripts'
   changes stacked.
3. **Check.** One real turn through this release's driver against a model on
   loopback (``runtime_probe``).
4. **Keep.** Only a version that passed is tagged where hosts look for it.

The sandbox image's own ``docker build`` runs those install scripts as root
with a network; this is the stricter of the two.

A version is removed again once nothing here needs it: no heartbeat answer
names it, and no chat container or checkpoint image of this deployment runs it.
A chat runs its own copy of the files (``runtime_install``), so removing one
never touches a chat, and the menu this host reports stays short.
"""

from __future__ import annotations

import contextlib
import logging
import subprocess
import threading
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING

from cloud_agent_host.runtime_images import (
    NAMESPACE_LABEL,
    RUNTIME_LABEL,
    SOURCE_LABEL,
    VERSION_LABEL,
    WORK_LABEL,
    chat_versions,
    discover,
    discover_or_none,
    image_id,
    image_ref,
    legacy_repository,
    repository,
    short_id,
)
from cloud_agent_host.runtime_probe import ProbeResult, probe_runtime
from cloud_agent_host.sandbox import runtime_bin_dir
from cloud_agent_shared.runtime_catalog import downloadable_runtimes
from cloud_agent_shared.runtime_downloads import (
    DOWNLOAD_ERROR_MAX_CHARS,
    DOWNLOADING,
    FAILED,
    MAX_TARGETS_PER_RUNTIME,
    READY,
    DownloadReport,
    DownloadTarget,
    reports_version,
)

if TYPE_CHECKING:
    from collections.abc import Callable

logger = logging.getLogger(__name__)

#: The Docker network the fetch step runs on: it needs the registry and
#: nothing else. Docker's default bridge unless a host names another.
ENV_RUNTIME_DOWNLOAD_NETWORK = "AGENT_RUNTIME_DOWNLOAD_NETWORK"
#: The npm registry to download from; empty is the public one.
ENV_RUNTIME_REGISTRY_URL = "AGENT_RUNTIME_REGISTRY_URL"

#: Fetching a few hundred megabytes over a slow link is the long step.
FETCH_TIMEOUT_S = 900.0
SCRIPTS_TIMEOUT_S = 600.0
_DOCKER_TIMEOUT_S = 120.0

#: How long a version must go unneeded before it is removed. A new chat is
#: given its version when it is claimed, but holds it (by its container's
#: label) only once that container exists, which loading an environment
#: snapshot can make a while. The version a switch has just replaced can be
#: the one such a chat was given.
UNNEEDED_GRACE_S = 600.0
#: How long the worker waits for an answer to wake it before it looks again.
_POLL_S = 60.0
#: How old a chat's copy source must be before it counts as left behind, as
#: Docker's ``until`` filter reads it (on the daemon's own clock). A copy
#: streams for at most a few minutes (``runtime_install``).
LEFT_BEHIND_SOURCE_AGE = "1h"

#: Reported while each step runs. The admin page eases between them, so the
#: bar keeps moving through a long fetch without this host inventing numbers.
_PROGRESS_FETCH = 5
_PROGRESS_SCRIPTS = 55
_PROGRESS_CHECK = 75
_PROGRESS_DONE = 100

_FETCH = (
    'set -e; npm install --global --prefix "$1" --ignore-scripts --no-audit --no-fund '
    '--before "$2" "$3"; rm -rf /tmp/npm-cache'
)
_SCRIPTS = 'set -e; npm rebuild --global --prefix "$1"; "$1/bin/$2" --version'


class _StepError(Exception):
    """One install step failed; the message is what the admin page shows."""


def _tail(text: str, limit: int = 200) -> str:
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    return " | ".join(lines[-3:])[-limit:]


#: A target's identity on this host: one runtime, one version.
_Key = tuple[str, str]


def _key(target: DownloadTarget) -> _Key:
    return (target.runtime, target.version)


@dataclass
class _Job:
    target: DownloadTarget
    base_id: str


class RuntimeDownloads:
    """Keep this host's downloaded versions in step with the control plane.

    ``apply`` and ``report`` are called from the heartbeat thread; the work
    runs on a thread of its own, one version at a time, so a long fetch never
    delays a beat. ``on_change`` is called when a report changes, or a version
    is removed, so the heartbeat can carry it now rather than an interval later.
    """

    def __init__(
        self,
        *,
        docker: str,
        image: str,
        namespace: str,
        image_menu: Callable[[], dict[str, list[str]]],
        network: str = "bridge",
        registry: str | None = None,
        probe: Callable[..., ProbeResult] = probe_runtime,
        on_change: Callable[[], None] | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._docker = docker
        self._image = image
        self._namespace = namespace
        self._image_menu = image_menu
        self._network = network
        self._registry = registry
        self._probe = probe
        self._on_change = on_change
        self._clock = clock
        self._lock = threading.Lock()
        self._wake = threading.Event()
        self._stop = threading.Event()
        #: In the control plane's order: the version new chats use comes first.
        self._targets: dict[_Key, DownloadTarget] = {}
        self._reports: dict[_Key, DownloadReport] = {}
        #: (nonce, base) last attempted per target: a failure is not retried
        #: until the admin asks again or a new sandbox image arrives.
        self._attempted: dict[_Key, tuple[str, str]] = {}
        #: Every version the latest answer named; ``None`` until an answer
        #: carries the list, and after one without it. Nothing is removed
        #: while it is ``None``.
        self._named: frozenset[_Key] | None = None
        #: Every version an answer named since a cleanup pass last looked,
        #: though a later answer may have dropped it again. A chat claimed on
        #: it meanwhile may not run it yet, so its grace period starts afresh.
        self._named_meanwhile: set[_Key] = set()
        #: Whether a cleanup is owed though there is nothing to download: the
        #: list changed, a version is still waiting out its grace period, or a
        #: chat still runs a version no answer names.
        self._cleanup_due = False
        #: When each held version was first seen unneeded; worker thread only.
        self._unneeded_since: dict[_Key, float] = {}
        self._thread: threading.Thread | None = None

    # ── heartbeat side ──────────────────────────────────────────────────

    def apply(self, targets: object) -> None:
        """Adopt the targets from one heartbeat answer.

        ``None`` is an answer without them, from a control plane that does not
        send them. The targets stay as they are, but nothing is removed until
        an answer says again what to hold: any version may be what new chats
        use there.
        """
        if not isinstance(targets, list):
            with self._lock:
                self._named = None
            return
        adopted: dict[_Key, DownloadTarget] = {}
        per_runtime: dict[str, int] = {}
        for raw in targets:
            target = DownloadTarget.from_wire(raw)
            if target is None or target.runtime not in downloadable_runtimes():
                continue
            if _key(target) not in adopted:
                if per_runtime.get(target.runtime, 0) >= MAX_TARGETS_PER_RUNTIME:
                    continue
                per_runtime[target.runtime] = per_runtime.get(target.runtime, 0) + 1
            adopted[_key(target)] = target
        # Read or not: a target this host cannot use may still name a version
        # it holds, and that one is not for this host to remove.
        named = frozenset(
            (raw["runtime"], raw["version"])
            for raw in targets
            if isinstance(raw, dict)
            and isinstance(raw.get("runtime"), str)
            and isinstance(raw.get("version"), str)
        )
        with self._lock:
            changed = adopted != self._targets
            self._targets = adopted
            if named != self._named:
                self._named = named
                self._named_meanwhile |= named
                self._cleanup_due = True
                changed = True
            for key in list(self._reports):
                current = adopted.get(key)
                if current is None or current.nonce != self._reports[key].nonce:
                    del self._reports[key]
        if changed:
            self._wake.set()

    def report(self) -> list[dict[str, object]]:
        """Each target's state on this host, as the heartbeat carries it."""
        with self._lock:
            return [report.to_wire() for report in self._reports.values()]

    def targets(self) -> frozenset[_Key]:
        """Every version this host was told to hold, for its menu to keep first."""
        with self._lock:
            return frozenset(self._targets) | (self._named or frozenset())

    # ── lifecycle ───────────────────────────────────────────────────────

    def start(self) -> None:
        """Start working, on a thread of its own.

        The thread first clears what a stopped predecessor left, so a runner
        does not wait on Docker before its first beat.
        """
        self._thread = threading.Thread(target=self._run, daemon=True, name="runtime-downloads")
        self._thread.start()

    def stop(self) -> None:
        """Stop taking work; a download cut short starts over on the next start."""
        self._stop.set()
        self._wake.set()
        if self._thread is not None:
            self._thread.join(timeout=5.0)

    # ── the work ────────────────────────────────────────────────────────

    def _set(
        self, target: DownloadTarget, state: str, progress: int, error: str | None = None
    ) -> None:
        report = DownloadReport(
            runtime=target.runtime,
            version=target.version,
            nonce=target.nonce,
            state=state,
            progress=progress,
            error=None if error is None else error[:DOWNLOAD_ERROR_MAX_CHARS],
        )
        with self._lock:
            current = self._targets.get(_key(target))
            if current != target:
                return
            if self._reports.get(_key(target)) == report:
                return
            self._reports[_key(target)] = report
        self._changed()

    def _changed(self) -> None:
        if self._on_change is not None:
            with contextlib.suppress(Exception):
                self._on_change()

    def _next(self, base_id: str) -> _Job | None:
        """The first target this host does not yet hold, settling the rest."""
        installed = {
            (image.runtime, image.version)
            for image in discover(self._docker, self._namespace)
            if image.base == short_id(base_id)
        }
        menu = self._image_menu()
        with self._lock:
            targets = list(self._targets.values())
        for target in targets:
            if _key(target) in installed or target.version in menu.get(target.runtime, []):
                self._set(target, READY, _PROGRESS_DONE)
                continue
            if self._attempted.get(_key(target)) == (target.nonce, base_id):
                continue
            return _Job(target, base_id)
        return None

    def _run(self) -> None:
        try:
            self._remove_leftovers()
        except Exception:
            logger.exception(
                "runtime download cleanup failed",
                extra={"event": "runtime_download_cleanup_error"},
            )
        while not self._stop.is_set():
            self._wake.clear()
            with self._lock:
                idle = not self._targets and not self._cleanup_due
            if idle:
                # Nothing to hold or remove: no reason to ask Docker anything
                # until an answer changes the list, which wakes this thread.
                self._wake.wait(_POLL_S)
                continue
            try:
                base_id = image_id(self._docker, self._image)
                job = None
                if base_id is None:
                    with self._lock:
                        targets = list(self._targets.values())
                    for target in targets:
                        self._set(
                            target, FAILED, 0, f"sandbox image {self._image} is not on this host"
                        )
                else:
                    job = self._next(base_id)
                if job is not None:
                    self._attempted[_key(job.target)] = (job.target.nonce, job.base_id)
                    self._download(job)
                    self._clean_up()
                    continue
            except Exception:
                logger.exception(
                    "runtime download loop failed", extra={"event": "runtime_download_loop_error"}
                )
            # Nothing left to download.
            self._clean_up()
            self._wake.wait(_POLL_S)

    def _download(self, job: _Job) -> None:
        target = job.target
        log = {"runtime": target.runtime, "version": target.version}
        logger.info("runtime download started", extra={"event": "runtime_download_started", **log})
        self._set(target, DOWNLOADING, _PROGRESS_FETCH)
        try:
            self._install(job)
        except _StepError as exc:
            logger.warning(
                "runtime download failed",
                extra={"event": "runtime_download_failed", "error": str(exc), **log},
            )
            self._set(target, FAILED, 0, str(exc))
            return
        except Exception as exc:
            logger.exception(
                "runtime download crashed", extra={"event": "runtime_download_crashed", **log}
            )
            self._set(target, FAILED, 0, f"{target.runtime} {target.version}: {exc}")
            return
        logger.info("runtime download ready", extra={"event": "runtime_download_ready", **log})
        self._set(target, READY, _PROGRESS_DONE)

    def _docker_run(
        self, args: list[str], *, timeout: float = _DOCKER_TIMEOUT_S
    ) -> subprocess.CompletedProcess[str]:
        try:
            return subprocess.run(
                [self._docker, *args],
                capture_output=True,
                text=True,
                timeout=timeout,
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            raise _StepError(f"docker {args[0]} timed out after {int(timeout)}s") from exc

    def _container(self, image: str, network: str, script: str, *args: str) -> str:
        created = self._docker_run(
            [
                "create",
                "--network",
                network,
                # Root, so the tree is owned by root, but a root with nothing
                # beyond owning its files: the scripts are a package's code.
                "--user",
                "0:0",
                "--cap-drop",
                "ALL",
                "--security-opt",
                "no-new-privileges",
                "--pids-limit",
                "1024",
                "--label",
                f"{WORK_LABEL}={self._namespace}",
                "--env",
                "HOME=/tmp",
                "--env",
                "npm_config_cache=/tmp/npm-cache",
                "--env",
                "npm_config_update_notifier=false",
                *(["--env", f"npm_config_registry={self._registry}"] if self._registry else []),
                "--entrypoint",
                "/bin/sh",
                image,
                "-c",
                script,
                "sh",
                *args,
            ]
        )
        if created.returncode != 0:
            raise _StepError(f"could not create an install container: {_tail(created.stderr)}")
        return created.stdout.strip()

    def _attach(self, container: str, timeout: float) -> subprocess.CompletedProcess[str]:
        try:
            return self._docker_run(["start", "--attach", container], timeout=timeout)
        except _StepError:
            self._docker_run(["rm", "-f", container])
            raise

    def _install(self, job: _Job) -> None:
        target = job.target
        definition = downloadable_runtimes()[target.runtime]
        prefix = runtime_bin_dir(target.runtime, target.version).removesuffix("/bin")
        name = f"{target.runtime} {target.version}"
        final = image_ref(self._namespace, target.runtime, target.version, job.base_id)
        candidate = final.replace(
            repository(self._namespace), f"{repository(self._namespace)}-candidate", 1
        )

        fetch = self._container(
            self._image,
            self._network,
            _FETCH,
            prefix,
            target.before,
            f"{definition.npm_package}@{target.version}",
        )
        try:
            fetched = self._attach(fetch, FETCH_TIMEOUT_S)
            if fetched.returncode != 0:
                raise _StepError(
                    f"npm could not download {name}: {_tail(fetched.stdout + fetched.stderr)}"
                )
            committed = self._docker_run(["commit", fetch])
            if committed.returncode != 0:
                raise _StepError(
                    f"could not save the download of {name}: {_tail(committed.stderr)}"
                )
            fetched_image = committed.stdout.strip()
        finally:
            self._docker_run(["rm", "-f", fetch])

        self._set(target, DOWNLOADING, _PROGRESS_SCRIPTS)
        scripts: str | None = None
        try:
            # Inside the try: a container Docker refuses to create, as on a
            # full disk, must not strand the fetched tree below.
            scripts = self._container(fetched_image, "none", _SCRIPTS, prefix, definition.binary)
            ran = self._attach(scripts, SCRIPTS_TIMEOUT_S)
            if ran.returncode != 0:
                raise _StepError(f"{name} did not install: {_tail(ran.stdout + ran.stderr)}")
            # Either stream, as the probe reads it: pi before 0.84.0 prints its
            # version to stderr, so stdout ends with npm's own last line.
            if not (
                reports_version(ran.stdout, target.version)
                or reports_version(ran.stderr, target.version)
            ):
                raise _StepError(
                    f"{name} reports a different version: {_tail(ran.stdout + ran.stderr)}"
                )
            self._keep_tree(scripts, prefix, candidate, target)
        finally:
            if scripts is not None:
                self._docker_run(["rm", "-f", scripts])
            # Unreferenced once the tree is copied out; removing it frees the
            # fetch's layer, which held every file a second time.
            self._docker_run(["rmi", fetched_image])

        try:
            self._set(target, DOWNLOADING, _PROGRESS_CHECK)
            result = self._probe(
                docker=self._docker,
                image=candidate,
                runtime_id=target.runtime,
                version=target.version,
                label=f"{WORK_LABEL}={self._namespace}",
            )
            if not result.ok:
                raise _StepError(result.detail or f"{name} failed its check")
            tagged = self._docker_run(["tag", candidate, final])
            if tagged.returncode != 0:
                raise _StepError(f"could not keep {name}: {_tail(tagged.stderr)}")
        finally:
            # The tag goes either way; the final image keeps the layers alive.
            self._docker_run(["rmi", candidate])
        self._forget_other_bases(target, job.base_id)

    def _keep_tree(self, source: str, prefix: str, candidate: str, target: DownloadTarget) -> None:
        """Commit ``prefix`` from ``source`` as one layer on the sandbox image."""
        name = f"{target.runtime} {target.version}"
        created = self._docker_run(
            [
                "create",
                "--network",
                "none",
                "--label",
                f"{WORK_LABEL}={self._namespace}",
                "--entrypoint",
                "/bin/true",
                self._image,
            ]
        )
        if created.returncode != 0:
            raise _StepError(f"could not prepare {name}: {_tail(created.stderr)}")
        clean = created.stdout.strip()
        try:
            reader = subprocess.Popen(
                [self._docker, "cp", f"{source}:{prefix}", "-"],
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
            )
            try:
                copied = subprocess.run(
                    [self._docker, "cp", "-", f"{clean}:{prefix.rsplit('/', 1)[0]}"],
                    stdin=reader.stdout,
                    capture_output=True,
                    timeout=SCRIPTS_TIMEOUT_S,
                    check=False,
                )
            finally:
                if reader.stdout is not None:
                    reader.stdout.close()
                try:
                    read = reader.wait(timeout=30)
                except subprocess.TimeoutExpired:
                    reader.kill()
                    read = -1
            if read != 0 or copied.returncode != 0:
                raise _StepError(f"could not save {name}")
            labelled = self._docker_run(
                [
                    "commit",
                    # The container is install work, so a restarted runner
                    # removes it; the version kept must not be. ``docker
                    # commit`` keeps a container's labels, and every container
                    # made from the image would inherit this one, among them
                    # the one a chat copies the CLI from (``runtime_install``).
                    # Docker cannot unset a label, but the leftover filter
                    # names the namespace and does not match an empty value.
                    "--change",
                    f"LABEL {WORK_LABEL}=",
                    "--change",
                    f"LABEL {NAMESPACE_LABEL}={self._namespace}",
                    "--change",
                    f"LABEL {RUNTIME_LABEL}={target.runtime}",
                    "--change",
                    f"LABEL {VERSION_LABEL}={target.version}",
                    clean,
                    candidate,
                ]
            )
            if labelled.returncode != 0:
                raise _StepError(f"could not save {name}: {_tail(labelled.stderr)}")
        finally:
            self._docker_run(["rm", "-f", clean])

    def _forget_other_bases(self, target: DownloadTarget, base_id: str) -> None:
        """Drop this version's builds on older sandbox images.

        No chat depends on them: a chat carries its own copy of the files, and
        a host that holds this version on today's image offers it all the same.
        """
        for image in discover(self._docker, self._namespace):
            if (image.runtime, image.version) == (
                target.runtime,
                target.version,
            ) and image.base != short_id(base_id):
                self._docker_run(["rmi", image.ref])

    def _clean_up(self) -> None:
        """Remove what nothing needs; a failure is logged, never raised."""
        try:
            self._remove_left_behind_sources()
            self._remove_unneeded()
        except Exception:
            logger.exception(
                "runtime download cleanup failed",
                extra={"event": "runtime_download_cleanup_error"},
            )

    def _remove_left_behind_sources(self) -> None:
        """Remove the copy sources chats' processes died before removing.

        Each keeps its version's image from being removed. Only old ones go:
        a young one may belong to a copy still running. ``container prune``
        removes every container that is not running, and a source is never
        started; ``until`` keeps the ones made in the last hour.
        """
        self._docker_run(
            [
                "container",
                "prune",
                "--force",
                "--filter",
                f"label={SOURCE_LABEL}={self._namespace}",
                "--filter",
                f"until={LEFT_BEHIND_SOURCE_AGE}",
            ]
        )

    def _remove_unneeded(self) -> None:
        """Remove the versions held here that nothing needs any more.

        A version is needed while the latest answer names it, or a chat
        container or checkpoint image of this deployment runs it, and for
        :data:`UNNEEDED_GRACE_S` after. That time starts afresh whenever an
        answer names the version again. Builds on older sandbox images go by
        the same rule: the version a chat ran at a release stays on the old
        image, and would be offered for good once that chat is gone.
        """
        with self._lock:
            self._cleanup_due = False
            named = self._named
            named_meanwhile, self._named_meanwhile = self._named_meanwhile, set()
        for key in named_meanwhile:
            self._unneeded_since.pop(key, None)
        if named is None:
            # Time unneeded counts only while answers say what is needed.
            self._unneeded_since.clear()
            return
        listed = discover_or_none(self._docker, self._namespace)
        if listed is None:
            # Docker could not say what this host holds, which is not holding
            # nothing: look again, though there may be nothing to download.
            with self._lock:
                self._cleanup_due = True
            return
        held = [image for image in listed if (image.runtime, image.version) not in named]
        if not held:
            self._unneeded_since.clear()
            return
        in_use = chat_versions(self._docker, self._namespace)
        if in_use is None:
            # Docker could not say what chats run: any of these may be.
            with self._lock:
                self._cleanup_due = True
            return
        now = self._clock()
        unneeded = {(image.runtime, image.version) for image in held} - in_use
        self._unneeded_since = {key: self._unneeded_since.get(key, now) for key in unneeded}
        waiting = removed = False
        for image in held:
            key = (image.runtime, image.version)
            if key not in unneeded:
                # A chat still runs it. Nothing tells this worker when that
                # chat is deleted, so the check stays owed: a host with
                # nothing to download would otherwise never look again.
                waiting = True
                continue
            if now - self._unneeded_since[key] < UNNEEDED_GRACE_S:
                waiting = True
                continue
            # An answer may have named it since this pass read the list: an
            # admin can select a version this host still offers, and the
            # control plane takes it on that word. Even if a later answer
            # dropped it again, the next pass gives it a fresh grace period.
            with self._lock:
                if self._named is None or key in self._named or key in self._named_meanwhile:
                    continue
            # Never forced: Docker refuses an image a container still uses,
            # such as the source a chat is copying this version from, and the
            # next cleanup tries again.
            if self._docker_run(["rmi", image.ref]).returncode != 0:
                waiting = True
                continue
            removed = True
            logger.info(
                "runtime download removed",
                extra={
                    "event": "runtime_download_removed",
                    "runtime": image.runtime,
                    "version": image.version,
                },
            )
        if waiting:
            with self._lock:
                self._cleanup_due = True
        if removed:
            self._changed()

    def _remove_leftovers(self) -> None:
        """Remove work a runner stopped mid-download left behind."""
        listed = self._docker_run(
            ["ps", "--all", "--quiet", "--filter", f"label={WORK_LABEL}={self._namespace}"]
        )
        for container in listed.stdout.split():
            self._docker_run(["rm", "-f", container])
        # Earlier releases' too, under the name every namespace that reads the
        # same shares. Only this deployment's, by their label: another's may
        # still be under its check.
        for name in (repository(self._namespace), legacy_repository(self._namespace)):
            candidates = self._docker_run(
                [
                    "image",
                    "ls",
                    f"{name}-candidate",
                    "--filter",
                    f"label={NAMESPACE_LABEL}={self._namespace}",
                    "--format",
                    "{{.Repository}}:{{.Tag}}",
                ]
            )
            for ref in candidates.stdout.split():
                self._docker_run(["rmi", ref])
