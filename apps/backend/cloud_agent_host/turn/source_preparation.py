"""A chat's source, prepared by the trusted runner outside the sandbox.

A first turn's clone and checkout, with the clone credential only ever in
Git's environment; a temporary workspace's one empty commit; the
conversation's output branch; a follow-up's missing base, imported into the
retained workspace as a bundle; and the cancellable Git subprocesses a
source prefetch runs on its own thread.
"""

from __future__ import annotations

import base64
import contextlib
import os
import pathlib
import queue
import re
import signal
import stat
import subprocess
import tempfile
import threading
import time
from typing import TYPE_CHECKING, Any, BinaryIO

from cloud_agent_host.bounded_output import OutputPump
from cloud_agent_host.container.chat_sandbox import (
    CHAT_WORKDIR,
    ChatSandboxManager,
    ChatSandboxSpec,
)
from cloud_agent_host.container.sandbox import SandboxBackend, SandboxSpec
from cloud_agent_host.service.local_settings import source_import_max_bytes
from cloud_agent_host.turn.source_prefetch import SourcePrefetch
from cloud_agent_shared.environment_snapshots import (
    MAX_LOCKFILES,
    EnvironmentSnapshotRef,
    is_lockfile,
)
from cloud_agent_shared.git.branches import branch_name_for
from cloud_agent_shared.git.source_archive import (
    build_base_bundle,
    build_source_archive,
)
from cloud_agent_shared.git.source_cancellation import check_source_stop, checked_entries
from cloud_agent_shared.git.temporary_workspace import (
    EMPTY_TREE_SHA,
    TEMPORARY_WORKSPACE_BASE_SHA,
    TEMPORARY_WORKSPACE_BRANCH,
    TEMPORARY_WORKSPACE_COMMIT_MESSAGE,
    temporary_workspace_commit_env,
)

if TYPE_CHECKING:
    from cloud_agent_host.claimed_job import (
        ClaimedJob,
    )
    from cloud_agent_host.container.retained_backend import RetainedChatBackend

_CANCEL_POLL_S = 0.1


def read_tracked_lockfiles(
    workdir: str, *, env: dict[str, str], timeout: float, stop: threading.Event | None
) -> dict[str, str] | None:
    """Fingerprint tracked manifests without losing Git's non-UTF-8 names.

    ``None`` means Git cannot say, or there are too many manifests to record.
    Cancellation is a failure of source preparation, not a missing match key.
    """
    listed = run_source_git(
        ["ls-files", "-s", "-z"], cwd=workdir, env=env, timeout=timeout, stop=stop
    )
    if listed.returncode != 0:
        return None
    found: dict[str, str] = {}
    for entry in checked_entries(listed.stdout.split(b"\0"), stop):
        meta, _, raw = entry.partition(b"\t")
        fields = meta.split()
        if len(fields) != 3:
            continue
        try:
            path = raw.decode()
        except UnicodeDecodeError:
            if is_lockfile(raw.rpartition(b"/")[2].decode(errors="replace")):
                return None
            continue
        if not is_lockfile(path):
            continue
        found[path] = fields[1].decode()
        if len(found) > MAX_LOCKFILES:
            return None
    return found


def run_source_git(
    args: list[str],
    *,
    cwd: str | None,
    env: dict[str, str],
    timeout: float,
    stop: threading.Event | None,
    input_bytes: bytes | None = None,
) -> subprocess.CompletedProcess[bytes]:
    """Drain Git while checking cancellation; kill and reap its whole group.

    Bytes preserve non-UTF-8 tracked names. Credentials stay in the supplied
    environment, never in the process arguments. An already-discarded source
    does not launch another command.
    """
    check_source_stop(stop)
    with subprocess.Popen(
        ["git", *args],
        cwd=cwd,
        env=env,
        stdin=subprocess.DEVNULL if input_bytes is None else subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        start_new_session=True,
    ) as process:
        deadline = time.monotonic() + timeout
        try:
            while True:
                check_source_stop(stop)
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise subprocess.TimeoutExpired(process.args, timeout)
                try:
                    stdout, stderr = process.communicate(
                        input=input_bytes, timeout=min(_CANCEL_POLL_S, remaining)
                    )
                    check_source_stop(stop)
                    return subprocess.CompletedProcess(
                        process.args, process.returncode, stdout, stderr
                    )
                except subprocess.TimeoutExpired:
                    input_bytes = None
        except BaseException:
            # The leader may have exited while a child still holds its pipes.
            with contextlib.suppress(ProcessLookupError):
                os.killpg(process.pid, signal.SIGKILL)
            process.communicate()
            raise


_GIT_TIMEOUT_S = 300.0
#: How often a bounded read of a sandbox command checks its deadline.
_READ_POLL_INTERVAL_S = 0.5


#: The shortest wait between two looks at how much a size-bounded clone has
#: written. A look walks the whole checkout, so the wait also grows with what
#: the last look cost, which keeps a large tree from being walked continuously.
_CLONE_WATCH_INTERVAL_S = 0.5


# Both are interpolated into a URL and handed to git, which reads a leading
# `-` as an option even where an operand belongs. Validate the shape here as
# well as at the API edge: this trusted process handles clone credentials.
_COMMIT_SHA = re.compile(r"[0-9a-fA-F]{7,64}")


_REPO_SLUG = re.compile(r"[A-Za-z0-9._-]+/[A-Za-z0-9._-]+")


# Trusted-runner override for local development and self-hosted Git mirrors.
# The default remains GitHub. This is deliberately read only by the runner: a
# job request cannot choose where the platform sends a repository credential.
ENV_REPO_CLONE_BASE_URL = "AGENT_REPO_CLONE_BASE_URL"


# Hermetic git for anything the *runner* runs: no user config, no hooks, no
# credential helpers, no prompts.
_GIT_ENV = {
    "GIT_CONFIG_GLOBAL": "/dev/null",
    "GIT_CONFIG_SYSTEM": "/dev/null",
    "GIT_TERMINAL_PROMPT": "0",
    "GIT_ASKPASS": "/bin/true",
    "GIT_LFS_SKIP_SMUDGE": "1",
}


# git refuses to touch a repository owned by another user ("dubious
# ownership"). The runner normally hands the worktree over with a chown so the
# ids match, but an unprivileged runner cannot — and there the agent's own
# `git status` and our patch build would both abort. Set for the sandbox side
# only; it is not config the runner itself ever runs under.
SAFE_DIRECTORY_ENV = {
    "GIT_CONFIG_COUNT": "1",
    "GIT_CONFIG_KEY_0": "safe.directory",
    "GIT_CONFIG_VALUE_0": "*",
}


class WorktreeError(Exception):
    """Raised when the job's repository could not be materialized."""


def _run_git(
    args: list[str],
    *,
    cwd: str | None = None,
    env_extra: dict[str, str] | None = None,
    input_text: str | None = None,
    stop: threading.Event | None = None,
) -> subprocess.CompletedProcess[str]:
    """Run one git command hermetically and return the completed process."""
    env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), **_GIT_ENV, **(env_extra or {})}
    if stop is not None:
        result = run_source_git(
            args,
            cwd=cwd,
            env=env,
            timeout=_GIT_TIMEOUT_S,
            stop=stop,
            input_bytes=None if input_text is None else input_text.encode(),
        )
        return subprocess.CompletedProcess(
            result.args, result.returncode, result.stdout.decode(), result.stderr.decode()
        )
    return subprocess.run(
        ["git", *args],
        cwd=cwd,
        env=env,
        capture_output=True,
        text=True,
        timeout=_GIT_TIMEOUT_S,
        stdin=subprocess.DEVNULL if input_text is None else None,
        input=input_text,
        check=False,
    )


def _checkout_bytes(workdir: str) -> int:
    """Total size of the regular files under ``workdir``, as the import limit counts it."""
    total = 0
    for directory, _names, files in os.walk(workdir):
        for name in files:
            try:
                info = os.lstat(os.path.join(directory, name))
            except OSError:
                # Git renames its temporary and lock files as it goes.
                continue
            if stat.S_ISREG(info.st_mode):
                total += info.st_size
    return total


def tracked_lockfiles(
    workdir: str, *, stop: threading.Event | None = None
) -> dict[str, str] | None:
    """Each tracked manifest's object id, or ``None`` when Git cannot say."""
    return read_tracked_lockfiles(
        workdir,
        env={"PATH": os.environ.get("PATH", "/usr/bin:/bin"), **_GIT_ENV},
        timeout=_GIT_TIMEOUT_S,
        stop=stop,
    )


def _choose_environment_snapshot(
    job: ClaimedJob,
    spec: ChatSandboxSpec,
    source: GitSourcePrefetch | None,
    *,
    new_group: bool,
) -> tuple[EnvironmentSnapshotRef | None, str | None]:
    """Whether a new workspace starts from the snapshot it was handed, and what to report.

    Only a group this turn creates can, and only from a snapshot built on
    this host's sandbox image and from the same dependency manifests as the
    checkout. Only the turns that were handed a snapshot wait here, for Git
    to list the manifests; the archive is built while the group starts. The
    outcome is ``None`` when there is nothing to report: no installation to
    snapshot, or a checkout that failed.
    """
    environment = job.environment
    if (
        not new_group
        or source is None
        or environment is None
        or not environment.setup_script
        or not environment.setup_fingerprint
    ):
        return None, None
    offered = job.environment_snapshot
    if offered is None:
        return None, "absent"
    if offered.base_image != spec.sandbox_image_digest:
        return None, "image_changed"
    lockfiles = source.lockfiles()
    if lockfiles is None:
        return None, None
    if lockfiles != offered.lockfiles:
        return None, "lockfiles_changed"
    return offered, None


def _run_git_within(
    args: list[str],
    *,
    cwd: str,
    max_bytes: int,
    env_extra: dict[str, str] | None = None,
    stop: threading.Event | None = None,
) -> subprocess.CompletedProcess[str]:
    """Run one git command that writes into ``cwd``, stopping it past ``max_bytes``.

    The import limit used to be checked only once the whole clone was on disk,
    and that disk is the Docker data root every retained chat on this host
    writes to. A repository far over the limit -- or a small pack that checks
    out into a huge tree -- filled it first and was refused after. Here the
    checkout is measured the way the archive is while git writes it, and git
    is killed once it is over; a step that finished between two looks is
    refused as soon as it ends. Anything refused here the archive would have
    refused too: nothing git writes here is removed by a later step.

    Its own process group keeps a terminal's Ctrl-C from reaching git, so the
    caller's ``stop`` is what ends a clone nobody will use any more: once it is
    set, git is killed the same way.
    """
    env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), **_GIT_ENV, **(env_extra or {})}
    ended = threading.Event()
    over = threading.Event()
    abandoned = threading.Event()
    # A process group of its own: a fetch writes through children it starts,
    # the transport helper and index-pack, and stopping git must stop them too.
    with subprocess.Popen(
        ["git", *args],
        cwd=cwd,
        env=env,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        start_new_session=True,
    ) as process:

        def kill_group() -> None:
            if process.returncode is None:
                with contextlib.suppress(ProcessLookupError):
                    os.killpg(process.pid, signal.SIGKILL)

        def watch() -> None:
            while not ended.is_set():
                started = time.monotonic()
                if stop is not None and stop.is_set():
                    abandoned.set()
                    kill_group()
                    return
                if _checkout_bytes(cwd) > max_bytes:
                    over.set()
                    kill_group()
                    return
                ended.wait(max(_CLONE_WATCH_INTERVAL_S, 4 * (time.monotonic() - started)))

        watcher = threading.Thread(target=watch, daemon=True, name="git-checkout-budget")
        watcher.start()
        try:
            stdout, stderr = process.communicate(timeout=_GIT_TIMEOUT_S)
        except BaseException:
            # What subprocess.run does on a timeout or an interrupt, for the
            # whole group; leaving the block then reaps git.
            kill_group()
            raise
        finally:
            ended.set()
            watcher.join()
    if abandoned.is_set():
        raise WorktreeError("the clone was abandoned before it finished")
    if over.is_set() or _checkout_bytes(cwd) > max_bytes:
        raise WorktreeError(
            f"source checkout exceeds this host's {max_bytes / (1 << 20):g} MiB retained "
            "import size limit; the clone was stopped before it finished"
        )
    return subprocess.CompletedProcess(process.args, process.returncode, stdout, stderr)


def _auth_env(clone_token: str | None) -> dict[str, str]:
    """Carry the clone credential in git's environment, never in argv.

    ``GIT_CONFIG_COUNT`` sets config for this invocation only. That matters
    twice over: the token never lands in ``.git/config`` (which is about to be
    archived into the chat container), and it never appears in process
    arguments (where another user on the runner host could see it in ``ps``
    during the fetch).
    """
    if not clone_token:
        return {}
    return {
        "GIT_CONFIG_COUNT": "1",
        "GIT_CONFIG_KEY_0": "http.extraHeader",
        "GIT_CONFIG_VALUE_0": f"Authorization: Basic {_basic_credential(clone_token)}",
    }


def _basic_credential(clone_token: str) -> str:
    """Return the HTTP Basic value git is given for the clone credential."""
    return base64.b64encode(f"x-access-token:{clone_token}".encode()).decode()


def _assert_no_credential_on_disk(
    workdir: str, clone_token: str | None, *, stop: threading.Event | None = None
) -> None:
    """Fail rather than mount a worktree that carries the clone credential.

    No repository credential may be stored in the source tree, which is about
    to be streamed into the sandbox. Asserting the property beats assuming it:
    git writes several files during a fetch, and a future authentication change
    could quietly start recording the token in one of them.
    """
    if not clone_token:
        return
    # Git is only ever handed the Basic header value, so that -- not the raw
    # token -- is what it would persist. The 15-byte ``x-access-token:`` prefix
    # is a whole number of base64 groups, so the token's own encoding appears
    # verbatim inside the header value; it is searched for on its own as well.
    forms = (
        clone_token,
        _basic_credential(clone_token),
        base64.b64encode(clone_token.encode()).decode().rstrip("="),
    )
    git_dir = pathlib.Path(workdir) / ".git"
    for path in checked_entries(git_dir.rglob("*"), stop):
        # Object and pack files hold repository content, not our configuration,
        # and are compressed; skipping them keeps this check bounded.
        if not path.is_file() or path.is_symlink() or "objects" in path.parts:
            continue
        try:
            if path.stat().st_size > 1_000_000:
                continue
            text = path.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        if any(form in text for form in forms):
            raise WorktreeError(
                f"refusing to run: the clone credential was written to .git/{path.name}, "
                "which the sandbox would be able to read"
            )


def prepare_worktree(
    *,
    workdir: str,
    repo: str,
    base_sha: str | None,
    clone_token: str | None = None,
    remote_base: str = "https://github.com",
    depth: int = 1,
    max_bytes: int | None = None,
    stop: threading.Event | None = None,
) -> str:
    """Materialize the job's repository in ``workdir`` and return its commit.

    Runs in the **trusted runner**, and deliberately before the agent starts:
    at this point the worktree is ours, not the agent's, so running git here is
    safe because later Git inspection runs inside the retained container.

    A shallow fetch by URL with the credential in the environment only, then
    ``origin`` recorded as the plain HTTPS URL: the agent pushes through it, and
    the GitHub relay, not this URL, decides what that buys. ``depth`` 2 also
    brings the commit's parent, for a caller that bundles only what it adds.

    ``max_bytes`` is the import limit the checkout is about to be held to. The
    fetch and the checkout are stopped once ``workdir`` holds more than that,
    rather than being written out whole first, or once ``stop`` is set.
    """
    if depth < 1:
        raise ValueError("fetch depth must be at least 1")
    parts = repo.split("/") if repo else []
    if (
        not _REPO_SLUG.fullmatch(repo or "")
        or len(parts) != 2
        or any(part in {".", ".."} for part in parts)
    ):
        raise WorktreeError(f"repo must be 'owner/name', got {repo!r}")
    if base_sha and not _COMMIT_SHA.fullmatch(base_sha):
        raise WorktreeError(f"base_sha must be a commit hash, got {base_sha!r}")

    url = f"{remote_base.rstrip('/')}/{repo}.git"
    auth = _auth_env(clone_token)

    def run_writing(
        args: list[str], env_extra: dict[str, str] | None = None
    ) -> subprocess.CompletedProcess[str]:
        if max_bytes is None:
            return _run_git(args, cwd=workdir, env_extra=env_extra, stop=stop)
        return _run_git_within(
            args, cwd=workdir, max_bytes=max_bytes, env_extra=env_extra, stop=stop
        )

    init = _run_git(["init", "--quiet", workdir], stop=stop)
    if init.returncode != 0:
        raise WorktreeError(f"git init failed: {init.stderr.strip()[:200]}")

    # `--end-of-options` so a ref that survived the shape check above still
    # cannot be parsed as an option by a git version we did not anticipate.
    ref = base_sha or "HEAD"
    fetch = run_writing(
        ["fetch", "--quiet", "--depth", str(depth), url, "--end-of-options", ref], auth
    )
    if fetch.returncode != 0:
        detail = (fetch.stderr or fetch.stdout).strip().splitlines()
        raise WorktreeError(
            f"could not fetch {repo}@{ref}: {detail[-1] if detail else 'no output'}"
        )

    checkout = run_writing(["checkout", "--quiet", "--detach", "FETCH_HEAD"])
    if checkout.returncode != 0:
        raise WorktreeError(f"could not check out {ref}: {checkout.stderr.strip()[:200]}")

    if _run_git(["remote", "add", "origin", url], cwd=workdir, stop=stop).returncode != 0:
        raise WorktreeError("could not record origin")
    resolved = _run_git(["rev-parse", "HEAD"], cwd=workdir, stop=stop).stdout.strip()
    _assert_no_credential_on_disk(workdir, clone_token, stop=stop)
    return resolved or (base_sha or "")


def prepare_temporary_worktree(*, workdir: str, stop: threading.Event | None = None) -> str:
    """Start a temporary workspace in ``workdir`` and return its first commit.

    A temporary workspace has no repository, so nothing is fetched and no
    credential is involved: the directory becomes a Git repository holding one
    empty commit on ``main``. It is the same commit in every temporary
    workspace (see :mod:`cloud_agent_shared.git.temporary_workspace`), which
    the control plane already recorded as the job's base, so it is checked
    rather than trusted. As in :func:`prepare_worktree`, running git here is
    safe because the agent has not touched this directory yet.
    """

    def git(
        args: list[str],
        *,
        env_extra: dict[str, str] | None = None,
        input_text: str | None = None,
    ) -> str:
        result = _run_git(args, cwd=workdir, env_extra=env_extra, input_text=input_text, stop=stop)
        if result.returncode != 0:
            raise WorktreeError(
                f"could not start the temporary workspace: git {args[0]} failed: "
                f"{result.stderr.strip()[:200]}"
            )
        return result.stdout.strip()

    git(["init", "--quiet", workdir])
    # Written rather than assumed: commit-tree refuses a tree it cannot read.
    if git(["mktree"], input_text="") != EMPTY_TREE_SHA:
        raise WorktreeError("could not start the temporary workspace: unexpected empty tree")
    commit = git(
        ["commit-tree", "--no-gpg-sign", EMPTY_TREE_SHA, "-m", TEMPORARY_WORKSPACE_COMMIT_MESSAGE],
        env_extra=temporary_workspace_commit_env(),
    )
    if commit != TEMPORARY_WORKSPACE_BASE_SHA:
        raise WorktreeError(
            f"temporary workspace commit {commit[:12]} is not the expected "
            f"{TEMPORARY_WORKSPACE_BASE_SHA[:12]}"
        )
    branch = f"refs/heads/{TEMPORARY_WORKSPACE_BRANCH}"
    git(["update-ref", branch, commit])
    git(["symbolic-ref", "HEAD", branch])
    return commit


def repo_clone_base_url(
    env: dict[str, str] | None = None, *, clone_token: str | None = None
) -> str:
    """Return the operator-selected repository origin, safely.

    A local Docker stack serves a tiny read-only ``git://`` repository so it
    can run without a GitHub account or a host bind mount. Production keeps
    using GitHub unless its trusted runner explicitly selects another origin.
    A repository credential is never sent over plaintext HTTP or the git
    protocol: those transports provide no confidentiality.
    """
    from urllib.parse import urlparse

    source = env if env is not None else os.environ
    configured = (source.get(ENV_REPO_CLONE_BASE_URL) or "").strip()
    base = configured or "https://github.com"
    parsed = urlparse(base)
    if parsed.scheme not in {"https", "http", "git"} or not parsed.hostname:
        raise WorktreeError(
            f"{ENV_REPO_CLONE_BASE_URL} must be an http(s) or git URL, got {base!r}"
        )
    if clone_token and parsed.scheme != "https":
        raise WorktreeError(
            f"refusing to send a repository credential over {parsed.scheme!r}; "
            f"{ENV_REPO_CLONE_BASE_URL} must use https when clone_token is present"
        )
    return base.rstrip("/")


def checkout_output_branch(
    workdir: str, branch_id: str, *, stop: threading.Event | None = None
) -> str:
    """Put a fresh durable worktree on the conversation's local ``agent/<id>`` branch."""
    branch = branch_name_for(branch_id)
    switched = _run_git(["checkout", "--quiet", "-B", branch, "HEAD"], cwd=workdir, stop=stop)
    if switched.returncode != 0:
        raise WorktreeError(
            f"could not create workspace branch {branch}: {switched.stderr.strip()[:200]}"
        )
    return branch


def _read_bounded(process: Any, *, deadline_s: float, max_bytes: int) -> str:
    """Drain a sandbox process's stdout under a deadline and a byte cap.

    This step still runs git against a worktree the agent owned for the whole
    run, including its ``.git/config``. A ``diff.external`` (or a textconv
    filter, or ``core.fsmonitor``) that never returns would otherwise block
    here forever — and because the heartbeat thread keeps renewing the lease
    until the enclosing ``finally``, the attempt never expires, the reaper
    never takes it, and this runner claims nothing further. One hostile
    repository would take a runner out permanently.
    """
    chunks: list[str] = []
    total = 0
    output = OutputPump(getattr(process, "chunks", process.lines)(), name="agent-patch")
    deadline = time.monotonic() + deadline_s
    try:
        while True:
            if time.monotonic() > deadline:
                output.close(drain=True)
                process.kill()
                process.wait()
                raise WorktreeError(
                    f"building the patch exceeded {deadline_s:.0f}s; the working tree's git "
                    "configuration is agent-controlled and may be hostile"
                )
            try:
                chunk = output.get(timeout=_READ_POLL_INTERVAL_S)
            except queue.Empty:
                continue
            if chunk is None:
                break
            total += len(chunk.encode())
            if total > max_bytes:
                output.close(drain=True)
                process.kill()
                process.wait()
                raise WorktreeError(
                    f"the patch exceeded {max_bytes} bytes while being read; refusing to "
                    "buffer unbounded agent output"
                )
            chunks.append(chunk)
    finally:
        output.close()

    exit_code = process.wait()
    if exit_code != 0:
        stderr = process.stderr_text().strip()
        raise WorktreeError(
            f"could not build the workspace patch (exit {exit_code}): {stderr[-500:]}"
        )
    return "".join(chunks)


def _resident_base_ready(workdir: str, job: ClaimedJob, backend: SandboxBackend) -> bool:
    """Is the turn's pinned base resolvable in the retained workspace?

    Probed **inside the sandbox** because this tree — ``.git/config``
    included — has belonged to the agent since import, and trusted-runner git
    against it would execute whatever the agent left in ``diff.external`` or
    ``core.fsmonitor`` with the runner's privileges. A missing object is
    imported as a credential-free Git bundle built in a temporary trusted
    clone; clone credentials never enter the retained container or its
    agent-controlled filesystem.
    """
    if not job.base_sha:
        return True
    return _sandbox_has_commit(workdir, job.base_sha, backend)


def _sandbox_has_commit(workdir: str, commit: str, backend: SandboxBackend) -> bool:
    """Does the retained workspace resolve ``commit``? Asked inside the sandbox."""
    if not _COMMIT_SHA.fullmatch(commit):
        return False
    process = backend.spawn(
        SandboxSpec(
            argv=["git", "cat-file", "-e", f"{commit}^{{commit}}"],
            workdir=workdir,
            env=dict(SAFE_DIRECTORY_ENV),
        )
    )
    try:
        _read_bounded(process, deadline_s=_GIT_TIMEOUT_S, max_bytes=65536)
    except WorktreeError:
        return False
    return True


class GitSourcePrefetch(SourcePrefetch):
    """A new chat's source, made with this module's Git steps; see :class:`SourcePrefetch`.

    The steps are looked up here when they run, so they stay this module's.
    """

    def __init__(self, job: ClaimedJob, *, uid: int, gid: int, max_bytes: int | None) -> None:
        self._job = job
        self._uid = uid
        self._gid = gid
        self._max_bytes = source_import_max_bytes() if max_bytes is None else max_bytes
        super().__init__()

    def _check_out(self, workdir: str, stop: threading.Event) -> str:
        if self._job.repo is None:
            # Nothing to clone and nothing to publish, so no output branch.
            return prepare_temporary_worktree(workdir=workdir, stop=stop)
        base_sha = prepare_worktree(
            workdir=workdir,
            repo=self._job.repo,
            base_sha=self._job.base_sha,
            clone_token=self._job.clone_token,
            remote_base=repo_clone_base_url(clone_token=self._job.clone_token),
            max_bytes=self._max_bytes,
            stop=stop,
        )
        check_source_stop(stop)
        checkout_output_branch(workdir, self._job.thread_id, stop=stop)
        return base_sha

    def _list_lockfiles(self, workdir: str, stop: threading.Event) -> dict[str, str] | None:
        return tracked_lockfiles(workdir, stop=stop)

    def _build_archive(self, workdir: str, out: BinaryIO, stop: threading.Event) -> None:
        build_source_archive(
            workdir, out, uid=self._uid, gid=self._gid, max_bytes=self._max_bytes, stop=stop
        )


def _prefetch_retained_source(
    *, job: ClaimedJob, uid: int, gid: int, max_bytes: int | None = None
) -> GitSourcePrefetch:
    """Start preparing a new chat's source; see :class:`GitSourcePrefetch`.

    ``max_bytes`` is the import limit this turn runs with; ``None`` reads this
    host's own, for a turn started without the runner's settings.
    """
    return GitSourcePrefetch(job, uid=uid, gid=gid, max_bytes=max_bytes)


def _import_retained_base(
    *,
    job: ClaimedJob,
    manager: ChatSandboxManager,
    spec: ChatSandboxSpec,
    backend: SandboxBackend,
) -> str:
    """Fetch a follow-up base outside the sandbox and import objects only.

    When the workspace already holds the base's parent, the bundle carries
    only what the commit adds; the whole tree, which for a large repository
    does not fit a bundle, goes only to a workspace that lacks the parent
    too.
    """
    with tempfile.TemporaryDirectory(prefix="freeinference-base-") as source:
        held: str | None = None
        if job.repo is None:
            # A temporary workspace's base is its fixed empty commit, which a
            # fresh empty repository rebuilds exactly; it has no parent.
            resolved = prepare_temporary_worktree(workdir=source)
        else:
            resolved = prepare_worktree(
                workdir=source,
                repo=job.repo,
                base_sha=job.base_sha,
                clone_token=job.clone_token,
                remote_base=repo_clone_base_url(clone_token=job.clone_token),
                depth=2,
            )
            parent = _run_git(["rev-parse", "--verify", "--quiet", "HEAD^1"], cwd=source)
            held = (parent.stdout.strip() or None) if parent.returncode == 0 else None
            if held and not _sandbox_has_commit(CHAT_WORKDIR, held, backend):
                held = None
        with build_base_bundle(source, commit=resolved, prerequisite=held) as bundle:
            manager.import_base_bundle(
                spec,
                bundle,
                commit=resolved,
                env=dict(SAFE_DIRECTORY_ENV),
            )
    return resolved


def _retained_head(backend: RetainedChatBackend) -> str:
    process = backend.spawn(
        SandboxSpec(
            argv=["git", "rev-parse", "HEAD"],
            workdir=CHAT_WORKDIR,
            env=dict(SAFE_DIRECTORY_ENV),
        )
    )
    return _read_bounded(process, deadline_s=_GIT_TIMEOUT_S, max_bytes=65536).strip()
