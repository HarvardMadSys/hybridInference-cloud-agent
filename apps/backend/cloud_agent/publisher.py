"""Trusted publisher for agent-produced patches.

Runs **outside** the sandbox. The agent never holds a git credential and never
pushes: it emits a patch, and this module validates it, applies it to a
throwaway clone, and pushes to the conversation's stable
``agent/<thread-id>`` branch.

Ordering is the whole point: :func:`~cloud_agent_shared.git.patch_gate.validate_patch`
runs *before* the bytes reach a worktree, and the push refspec is pinned to
the single computed branch — so neither a hostile patch nor a bug in the apply
step can write to ``dev``/``main`` or execute a workflow with repository
secrets.

Draft PR creation is left to the caller (it needs the GitHub App installation
token, which lives in the credential layer); this module returns the pushed
branch so the caller can open the PR against it.
"""

from __future__ import annotations

import re
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from cloud_agent.logging_utils import get_logger
from cloud_agent.settings import number_setting, setting_env
from cloud_agent_shared.git.patch_gate import (
    MAX_BLOB_BYTES,
    MAX_CHANGED_BLOB_BYTES,
    PatchGateResult,
    branch_name_for,
    scan_blob_secrets,
    validate_change_set,
    validate_patch,
)
from cloud_agent_shared.git.source_archive import build_base_bundle
from cloud_agent_shared.secret_redaction import secret_patterns

if TYPE_CHECKING:
    from collections.abc import Callable, Iterable

logger = get_logger(__name__)

_GIT_TIMEOUT_S = 120.0
#: How many error lines a failed local git command's message quotes.
_ERROR_LINES_QUOTED = 3

# A commit hash and nothing else. git treats a leading `-` as an option even
# in positions that look like operands, so an unvalidated ref is an argument
# injection into a process that holds the repository credential.
_COMMIT_SHA = re.compile(r"[0-9a-fA-F]{7,64}")
# What `surrogateescape` decodes a byte that is not UTF-8 to.
_ESCAPED_BYTE = re.compile("[\udc80-\udcff]")


# Hermetic git: no user config, no hooks, no credential helpers, no prompts.
# The identity these commits carry. A deployment sets its own; the defaults
# name the software rather than one site. Read from the runner's environment,
# which a patch cannot reach — the whole point of the dictionary below.
def _identity_name() -> str:
    return setting_env("AGENT_GIT_AUTHOR_NAME") or "HybridInference Agent"


def _identity_email() -> str:
    return setting_env("AGENT_GIT_AUTHOR_EMAIL") or "agent@localhost"


# A patch must not be able to reach configuration that changes what git does.
_GIT_ENV = {
    "GIT_CONFIG_GLOBAL": "/dev/null",
    "GIT_CONFIG_SYSTEM": "/dev/null",
    "GIT_TERMINAL_PROMPT": "0",
    "GIT_ASKPASS": "/bin/true",
}

# No command starts git's automatic maintenance. Each repository here is thrown
# away after one publication, so housekeeping buys nothing. Since git 2.54 the
# maintenance a fetch leaves running repacks, which in a shallow repository
# rewrites .git/shallow under the next fetch and fails it. Set on the command
# line, where the credential's own GIT_CONFIG_COUNT entries cannot replace it.
_NO_AUTO_MAINTENANCE = ("-c", "maintenance.auto=false")


class PublishError(Exception):
    """Raised when publication fails; the message is safe to store as detail."""


class PatchRejected(PublishError):
    """The patch itself was refused: by the gate, or by a check of what it staged.

    Never retried, whatever its text says: the detail names the patch's own
    paths, and a path such as ``x(503)`` or ``timed out.md`` must not read as an
    HTTP status or a disconnect.
    """


class TransientPublishError(PublishError):
    """An external publication failure that is safe to retry durably.

    The branch name is deterministic and the publisher reconciles an already
    pushed matching commit before it writes again. Callers may therefore put
    these failures back on the publication queue without duplicating remote
    work. Gate, patch, and branch-divergence failures remain ``PublishError``
    and require a person rather than a retry loop.
    """

    def __init__(self, message: str, *, retry_after_s: float | None = None) -> None:
        """Keep how long the provider asked to wait, when it refused for a rate limit."""
        super().__init__(message)
        self.retry_after_s = retry_after_s


@dataclass
class PublishResult:
    """Outcome of publishing one job's patch."""

    branch: str
    commit_sha: str
    changed_files: list[str]
    base_bundle: bytes | None = None


def _git_env(env_extra: dict[str, str] | None = None) -> dict[str, str]:
    """Build the hermetic environment for text commands and binary blob reads."""
    return {
        "PATH": "/usr/bin:/bin:/usr/local/bin",
        **_GIT_ENV,
        "GIT_COMMITTER_NAME": _identity_name(),
        "GIT_COMMITTER_EMAIL": _identity_email(),
        "GIT_AUTHOR_NAME": _identity_name(),
        "GIT_AUTHOR_EMAIL": _identity_email(),
        **(env_extra or {}),
    }


def _run_git(
    args: list[str],
    *,
    cwd: Path | None = None,
    env_extra: dict[str, str] | None = None,
    remote: bool = False,
    errors: str = "strict",
) -> str:
    """Run one git command hermetically and return stdout.

    ``errors`` handles output bytes that do not decode, as for ``bytes.decode``.
    """
    timeout_s = number_setting("publish_git_timeout_s", _GIT_TIMEOUT_S)
    try:
        completed = subprocess.run(
            ["git", *_NO_AUTO_MAINTENANCE, *args],
            cwd=str(cwd) if cwd else None,
            env=_git_env(env_extra),
            capture_output=True,
            text=True,
            errors=errors,
            timeout=timeout_s,
            stdin=subprocess.DEVNULL,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        error = TransientPublishError if remote else PublishError
        raise error(f"git {args[0]} timed out after {timeout_s:.0f}s") from exc
    if completed.returncode != 0:
        detail = (completed.stderr or completed.stdout or "").strip().splitlines()
        # The publish worker tells a dropped connection from a refusal by a
        # remote command's last line, so that one stays as it is.
        tail = (None if remote else _error_lines(detail)) or (detail[-1] if detail else "no output")
        error = TransientPublishError if remote else PublishError
        raise error(f"git {args[0]} failed: {tail}")
    return completed.stdout


def _error_lines(detail: list[str]) -> str | None:
    """The first few distinct ``error:`` and ``fatal:`` lines of a failed command.

    Git writes them where they happen and goes on writing progress after them:
    ``git apply --3way`` ends a failed apply with "Falling back to direct
    application..." or "Applied patch to 'b.txt' cleanly.", so its last line
    does not say which file failed. ``None`` when there are none.
    """
    errors = list(dict.fromkeys(line for line in detail if line.startswith(("error:", "fatal:"))))
    if not errors:
        return None
    quoted = "; ".join(errors[:_ERROR_LINES_QUOTED])
    more = len(errors) - _ERROR_LINES_QUOTED
    return f"{quoted}; and {more} more" if more > 0 else quoted


@dataclass
class _StagedBlob:
    """A regular staged file and its same-path object from the base commit."""

    path: str
    sha: str
    base_sha: str | None


@dataclass
class _StagedChangeSet:
    """What git reports is staged, as opposed to what the patch text implied."""

    paths: list[str]
    symlinks: list[str]
    gitlinks: list[str]
    blobs: list[_StagedBlob]


def _remote_branch_tip(
    repo: Path, branch: str, auth_env: dict[str, str] | None = None
) -> str | None:
    """Return the remote tip of ``branch``, or ``None`` when it does not exist.

    ``ls-remote`` matches its pattern against the end of every ref, so a ref
    named ``refs/heads/x/refs/heads/<branch>`` is listed too, and can sort
    first. Only the exact ref is read.
    """
    want = f"refs/heads/{branch}"
    raw = _run_git(
        ["ls-remote", "origin", "--end-of-options", want],
        cwd=repo,
        env_extra=auth_env,
        remote=True,
    )
    for line in raw.splitlines():
        sha, _, ref = line.partition("\t")
        if ref.strip() == want:
            return sha.strip()
    return None


def _branch_moved(branch: str, tip: str, base_sha: str) -> PublishError:
    """The recorded reason when the agent branch is not this run's base."""
    return PublishError(
        f"branch {branch} has moved: its tip is {tip[:12]} but this run built on "
        f"{base_sha[:12]}. Commits were pushed to the branch after this run started — "
        "the platform never force-pushes, so review them and either start a new "
        "follow-up (which builds on the branch's published state) or delete the "
        "branch to republish from scratch."
    )


def _displaced_remote_tip(
    repo: Path, branch: str, base_sha: str, auth_env: dict[str, str] | None = None
) -> str | None:
    """Return a remote tip that is not the base this patch was built on.

    ``None`` when the branch is absent or still at ``base_sha``: the push
    creates it or fast-forwards it. Another tip may be this publication's own
    previous push, which the caller reuses instead of force-pushing.
    """
    tip = _remote_branch_tip(repo, branch, auth_env)
    if tip is None or tip.lower().startswith(base_sha.lower()):
        return None
    return tip


def _reuse_pushed_commit(
    repo: Path,
    *,
    branch: str,
    tip: str,
    base: str,
    local_sha: str,
    own_pushes: frozenset[str] = frozenset(),
    auth_env: dict[str, str] | None = None,
) -> str:
    """Return the commit to push over the branch's tip; otherwise the branch moved.

    A retry builds a second commit for the same tree, because the timestamps
    differ. The branch already points at the first object. Reusing it keeps
    the push idempotent.

    ``own_pushes`` (lowercase full ids) are this group's earlier commits on this
    base that were pushed but never published, because a later step failed.
    This patch, cut from the same base, carries their changes, so a tip that is
    one of them is built on rather than refused: the branch fast-forwards to
    this patch's tree. Either way every commit between the tip and ``base``, the
    full id of the commit the patch was cut from, must be one of those recorded
    pushes, with a single parent. Anything else on the branch is still a
    refusal, and nothing is force-pushed.

    The returned commit is checked out; the caller pushes it.
    """
    if tip.lower() == local_sha.lower():
        return local_sha
    _run_git(
        [
            "fetch",
            "--quiet",
            "--depth",
            str(len(own_pushes) + 2),
            "origin",
            "--end-of-options",
            f"refs/heads/{branch}",
        ],
        cwd=repo,
        env_extra=auth_env,
        remote=True,
    )
    fetched = _run_git(["rev-parse", "FETCH_HEAD"], cwd=repo).strip().lower()
    if not fetched.startswith(tip.lower()) and not tip.lower().startswith(fetched):
        raise _branch_moved(branch, fetched, base)
    # Everything the tip adds to the base, each commit with its parents, in one
    # listing. The shallow fetch's last commit lists with none. Publisher
    # commits have exactly one parent: a matching tree and first parent must
    # never disguise a merge that also carries human ancestry.
    parents: dict[str, list[str]] = {}
    for line in _run_git(["rev-list", "--parents", fetched, f"^{base}"], cwd=repo).splitlines():
        commit, *listed = line.lower().split()
        parents[commit] = listed
    current = fetched
    while True:
        ancestry = parents.pop(current, None)
        if ancestry is None or len(ancestry) != 1:
            raise _branch_moved(branch, tip, base)
        if ancestry[0] == base:
            break
        if ancestry[0] not in own_pushes:
            raise _branch_moved(branch, tip, base)
        current = ancestry[0]
    tip_tree = _run_git(["rev-parse", f"{fetched}^{{tree}}"], cwd=repo).strip()
    local_tree = _run_git(["rev-parse", f"{local_sha}^{{tree}}"], cwd=repo).strip()
    if tip_tree.lower() == local_tree.lower():
        result = fetched
    elif fetched in own_pushes:
        message = _run_git(["log", "-1", "--format=%B", local_sha], cwd=repo).strip()
        result = _run_git(
            ["commit-tree", local_tree, "-p", fetched, "-m", message], cwd=repo
        ).strip()
    else:
        raise _branch_moved(branch, tip, base)
    # The retained bundle names HEAD, so the recorded commit has to be the one
    # the branch will point at, not the retry's duplicate object.
    _run_git(["checkout", "--quiet", "--detach", result], cwd=repo)
    return result


def _staged_change_set(repo: Path) -> _StagedChangeSet:
    """Read the staged paths and their modes straight out of the index.

    ``-z`` output is NUL-separated and never quoted, which is the point: the
    quoting git applies in a *diff header* is exactly what a hostile patch used
    to hide a path from the pre-apply gate.

    Unquoted also means a name's raw bytes. One that is not UTF-8 is refused
    here, before anything stores or renders it, and counted rather than shown.
    """
    raw = _run_git(
        ["diff", "--cached", "--raw", "-z", "--find-renames", "--abbrev=64"],
        cwd=repo,
        errors="surrogateescape",
    )
    paths: list[str] = []
    symlinks: list[str] = []
    gitlinks: list[str] = []
    blobs: list[_StagedBlob] = []
    # `:<srcmode> <dstmode> <srcsha> <dstsha> <status>\0<path>\0`
    fields = raw.split("\0")
    index = 0
    while index < len(fields):
        meta = fields[index]
        if not meta.startswith(":"):
            index += 1
            continue
        if index + 1 >= len(fields):
            break
        parts = meta[1:].split()
        status = parts[4] if len(parts) > 4 else ""
        path_count = 2 if status.startswith("R") else 1
        if index + path_count >= len(fields):
            raise PublishError("git returned an incomplete staged path record")
        source_path = fields[index + 1]
        path = fields[index + 2] if path_count == 2 else source_path
        dst_mode = parts[1] if len(parts) > 1 else ""
        if path:
            paths.extend((source_path, path) if path_count == 2 else (path,))
            if dst_mode == "120000":
                symlinks.append(path)
            if dst_mode == "160000":
                gitlinks.append(path)
            if dst_mode in ("100644", "100755"):
                unchanged_rename = status.startswith("R") and parts[2] == parts[3]
                blobs.append(
                    _StagedBlob(
                        path=path,
                        sha=parts[3],
                        base_sha=(
                            parts[2]
                            if parts[0] in ("100644", "100755")
                            and (status.startswith("M") or unchanged_rename)
                            else None
                        ),
                    )
                )
        index += path_count + 1
    not_utf8 = [path for path in paths if _ESCAPED_BYTE.search(path)]
    if not_utf8:
        raise PatchRejected(
            f"patch rejected after apply: patch changes {len(not_utf8)} path(s) that are not UTF-8"
        )
    return _StagedChangeSet(paths=paths, symlinks=symlinks, gitlinks=gitlinks, blobs=blobs)


def _blob_size(repo: Path, sha: str) -> int:
    """Check an immutable object's size before capturing any of its contents."""
    size = int(_run_git(["cat-file", "-s", sha], cwd=repo).strip())
    if size > MAX_BLOB_BYTES:
        raise PatchRejected(
            f"patch rejected after apply: blob exceeds the {MAX_BLOB_BYTES} byte limit"
        )
    return size


def _read_blob(repo: Path, sha: str, size: int) -> bytes:
    """Read a size-checked object as bytes, without ever logging its contents."""
    timeout_s = number_setting("publish_git_timeout_s", _GIT_TIMEOUT_S)
    try:
        completed = subprocess.run(
            ["git", "cat-file", "blob", sha],
            cwd=str(repo),
            env=_git_env(),
            capture_output=True,
            timeout=timeout_s,
            stdin=subprocess.DEVNULL,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise PublishError(f"git cat-file timed out after {timeout_s:.0f}s") from exc
    if completed.returncode or len(completed.stdout) != size:
        raise PublishError("could not read publication blob")
    return completed.stdout


def _validate_staged_blobs(
    repo: Path, blobs: list[_StagedBlob], known_secrets: dict[str, str] | None = None
) -> None:
    """Reject new credentials in staged text or binary files before committing."""
    sizes = [_blob_size(repo, blob.sha) for blob in blobs]
    if sum(sizes) > MAX_CHANGED_BLOB_BYTES:
        raise PatchRejected(
            f"patch rejected after apply: blobs exceed the {MAX_CHANGED_BLOB_BYTES} total byte limit"
        )
    for blob, size in zip(blobs, sizes, strict=True):
        content = _read_blob(repo, blob.sha, size)
        for name, value in (known_secrets or {}).items():
            for pattern in secret_patterns(value):
                if not pattern:
                    continue
                token = pattern.encode("utf-8")
                if token not in content:
                    continue
                old_content = (
                    _read_blob(repo, blob.base_sha, _blob_size(repo, blob.base_sha))
                    if blob.base_sha
                    else b""
                )
                if content.count(token) > old_content.count(token):
                    raise PatchRejected(f"patch rejected: Secret {name} in {blob.path!r}")
        # Most files need no baseline read. A match requires checking the same
        # path's original object; deletion has no new blob and is never scanned.
        if not scan_blob_secrets(content):
            continue
        baseline = (
            _read_blob(repo, blob.base_sha, _blob_size(repo, blob.base_sha))
            if blob.base_sha
            else b""
        )
        secrets = scan_blob_secrets(content, baseline)
        if secrets:
            raise PatchRejected(
                "patch rejected after apply: credential-shaped content in "
                f"{blob.path!r}: {', '.join(secrets)}"
            )


def publish_patch(
    *,
    job_id: str,
    patch: str,
    clone_url: str,
    base_sha: str,
    commit_message: str,
    branch_id: str | None = None,
    allow_workflow_changes: bool = False,
    include_base_bundle: bool = False,
    auth_env: dict[str, str] | None = None,
    known_secrets: dict[str, str] | None = None,
    own_pushes: Iterable[str] = (),
    before_push: Callable[[str], None] | None = None,
) -> PublishResult:
    """Validate and publish one run's patch on its stable thread branch.

    ``clone_url`` names the repository and carries no credential. The
    short-lived one the caller minted arrives as ``auth_env``, git
    configuration in the environment of only the commands that reach the
    remote: inside the URL it was written to the clone's ``.git/config`` and
    passed in the arguments of every ``git-remote-https`` process, where any
    process able to read ``/proc`` could see it. It never enters the sandbox.
    ``allow_workflow_changes`` would release the block on patches that touch
    ``.github/``. It defaults to False, and the platform never sets it: there
    is no release path, and the publish worker's token cannot push workflows.
    Actual staged files, including binary assets, are size-checked and scanned
    for newly introduced credentials before committing or pushing;
    ``known_secrets`` adds the values of the job's environment secrets to that
    scan. ``own_pushes`` are this group's commits on ``base_sha`` that were
    pushed but never published (see :func:`_reuse_pushed_commit`).
    ``before_push`` receives the commit about to be pushed, after every local
    step has succeeded; whatever it raises stops the push.

    Raises :class:`PublishError` with a reviewer-readable message on any gate
    violation or git failure.
    """
    if not _COMMIT_SHA.fullmatch(base_sha or ""):
        # base_sha reaches here from user input on the job. Anything that is
        # not a bare commit hash is refused before it can be handed to git.
        raise PublishError(f"base_sha must be a full commit hash, got {base_sha!r}")

    gate = validate_patch(patch)
    if not _gate_permits(gate, allow_workflow_changes=allow_workflow_changes):
        raise PatchRejected(f"patch rejected: {gate.reason}")

    branch = branch_name_for(branch_id or job_id)
    own = frozenset(sha.lower() for sha in own_pushes)
    workdir = Path(tempfile.mkdtemp(prefix=f"agent-publish-{job_id}-"))
    try:
        repo = workdir / "repo"
        # Fetch only the one commit the agent worked from: cheap, and it means
        # a patch cannot be applied onto some other branch's tree by accident.
        _run_git(["init", "--quiet", str(repo)])
        _run_git(["remote", "add", "origin", clone_url], cwd=repo)
        # A tip other than this base may be our own previous push. It is reused
        # after the local commit when the tree matches, and refused when it
        # does not — never force-pushed.
        displaced = _displaced_remote_tip(repo, branch, base_sha, auth_env)
        # `--end-of-options` plus the shape check above: git parses a leading
        # `--` argument as an option even after the remote name, so an
        # owner-supplied base_sha of `--upload-pack=/bin/sh -c ...` would run a
        # command *inside the trusted publisher*. Two independent stops,
        # because this process holds the GitHub credential.
        _run_git(
            ["fetch", "--quiet", "--depth", "1", "origin", "--end-of-options", base_sha],
            cwd=repo,
            env_extra=auth_env,
            remote=True,
        )
        _run_git(["checkout", "--quiet", "-b", branch, "FETCH_HEAD"], cwd=repo)

        patch_file = workdir / "job.patch"
        patch_file.write_text(patch, encoding="utf-8")
        # `--3way` recovers from minor context drift; `--whitespace=nowarn`
        # keeps whitespace-only noise from failing the apply.
        try:
            _run_git(
                ["apply", "--3way", "--whitespace=nowarn", str(patch_file)],
                cwd=repo,
            )
        except PublishError as exc:
            # The branch is not this base, and the patch does not apply onto
            # it. That is the moved-branch refusal. A timeout is not: the
            # caller retries those. Nor is the group's own earlier push: the
            # patch carries it, so its failure to apply is the patch's own.
            if (
                displaced is not None
                and displaced.lower() not in own
                and "timed out" not in str(exc).lower()
            ):
                raise _branch_moved(branch, displaced, base_sha) from None
            raise

        _run_git(["add", "--all"], cwd=repo)
        if not _run_git(["status", "--porcelain"], cwd=repo).strip():
            raise PublishError("patch applied cleanly but produced no changes")

        # Re-gate against what git says actually landed, not against the parse
        # of the patch text. The pre-apply gate gives an early rejection with a
        # readable reason, but a hostile patch only has to defeat the *parser*
        # to get past it — a C-quoted `.github/` path did exactly that. Here
        # the paths and modes come from the index itself, so there is nothing
        # left to misread.
        staged = _staged_change_set(repo)
        post = validate_change_set(staged.paths, staged.symlinks, staged.gitlinks)
        if not _gate_permits(post, allow_workflow_changes=allow_workflow_changes):
            raise PatchRejected(f"patch rejected after apply: {post.reason}")
        _validate_staged_blobs(
            repo, staged.blobs, **({"known_secrets": known_secrets} if known_secrets else {})
        )
        gate = post
        _run_git(["commit", "--quiet", "--no-verify", "-m", commit_message], cwd=repo)
        commit_sha = _run_git(["rev-parse", "HEAD"], cwd=repo).strip()
        # The workspace holds the base, which is not the published commit's
        # parent when that commit is built on unpublished pushes of its own.
        base = _run_git(["rev-parse", "--verify", f"{base_sha}^{{commit}}"], cwd=repo)
        base = base.strip().lower()
        if displaced is not None:
            commit_sha = _reuse_pushed_commit(
                repo,
                branch=branch,
                tip=displaced,
                base=base,
                local_sha=commit_sha,
                own_pushes=own,
                auth_env=auth_env,
            )
        base_bundle = (
            build_base_bundle(str(repo), commit=commit_sha, prerequisite=base)
            if include_base_bundle
            else None
        )
        # Every local step has succeeded. The caller records the commit before
        # it is pushed, so a push that lands while this process loses track of
        # it (a timeout after the remote accepted it, a crash, a later step
        # that fails) is still the group's own to the next publication.
        if before_push is not None:
            before_push(commit_sha)
        # Pinned refspec: this process can only ever write the one branch, and
        # never force-updates it. Ordinary receive-pack is also the final
        # compare-and-set fence: it creates the branch, fast-forwards it, or
        # changes nothing when it already names this commit, and fails
        # transiently if somebody advanced it while we reconciled.
        _run_git(
            ["push", "--quiet", "origin", f"{commit_sha}:refs/heads/{branch}"],
            cwd=repo,
            env_extra=auth_env,
            remote=True,
        )
        logger.info(
            "agent_job_published",
            extra={
                "event": "agent_job_published",
                "job_id": job_id,
                "branch": branch,
                "files": len(gate.changed_files),
            },
        )
        return PublishResult(
            branch=branch,
            commit_sha=commit_sha,
            changed_files=gate.changed_files,
            base_bundle=base_bundle,
        )
    finally:
        shutil.rmtree(workdir, ignore_errors=True)


def _head_parent(repo: Path) -> str:
    """The published commit's parent.

    That is the base the workspace holds only when the commit is the first push
    on it. A commit built on unpublished pushes of its own has one of those as
    its parent instead; :func:`fetch_base_bundle` then deepens to the base.
    """
    return _run_git(["rev-parse", "--verify", "HEAD^1"], cwd=repo).strip()


def fetch_base_bundle(
    *,
    clone_url: str,
    commit_sha: str,
    auth_env: dict[str, str] | None = None,
    base_sha: str | None = None,
) -> bytes:
    """Fetch one published commit and return its bounded credential-free bundle.

    Two commits deep, so the parent is at hand to leave out of the bundle when
    it is the base the workspace holds. The credential travels as in
    :func:`publish_patch`: in ``auth_env``, never in ``clone_url``.

    ``base_sha`` is the commit the workspace holds. A commit built on
    unpublished pushes of its own (see :func:`_reuse_pushed_commit`) is not its
    parent; the history is then deepened down to it, and the bundle carries
    those pushes too.
    """
    if not _COMMIT_SHA.fullmatch(commit_sha or ""):
        raise PublishError(f"commit_sha must be a full commit hash, got {commit_sha!r}")
    if base_sha is not None and not _COMMIT_SHA.fullmatch(base_sha):
        raise PublishError(f"base_sha must be a full commit hash, got {base_sha!r}")
    workdir = Path(tempfile.mkdtemp(prefix="agent-base-import-"))
    try:
        repo = workdir / "repo"
        _run_git(["init", "--quiet", str(repo)])
        _run_git(["remote", "add", "origin", clone_url], cwd=repo)
        _run_git(
            ["fetch", "--quiet", "--depth", "2", "origin", "--end-of-options", commit_sha],
            cwd=repo,
            env_extra=auth_env,
            remote=True,
        )
        _run_git(["checkout", "--quiet", "--detach", "FETCH_HEAD"], cwd=repo)
        prerequisite = _head_parent(repo)
        if (
            base_sha is not None
            and not commit_sha.lower().startswith(base_sha.lower())
            and not prerequisite.lower().startswith(base_sha.lower())
        ):
            prerequisite = _deepen_to_base(repo, commit_sha, base_sha, auth_env)
        try:
            return build_base_bundle(str(repo), commit=commit_sha, prerequisite=prerequisite)
        except Exception as exc:
            raise PublishError(f"could not build the retained base bundle: {exc}") from exc
    finally:
        shutil.rmtree(workdir, ignore_errors=True)


def _deepen_to_base(
    repo: Path, commit_sha: str, base_sha: str, auth_env: dict[str, str] | None
) -> str:
    """Fetch ``commit_sha``'s first-parent history down to ``base_sha``; return its full id.

    Publication builds on any number of the group's own pushes, so the depth
    doubles until the base appears. History is immutable and publication
    checked that the commit descends from the base, so the search ends there;
    it fails only once the whole history is fetched without it.
    """
    depth = 2
    while True:
        depth *= 2
        _run_git(
            [
                "fetch",
                "--quiet",
                "--depth",
                str(depth),
                "origin",
                "--end-of-options",
                commit_sha,
            ],
            cwd=repo,
            env_extra=auth_env,
            remote=True,
        )
        for ancestor in _run_git(["rev-list", "--first-parent", "HEAD"], cwd=repo).split():
            if ancestor.lower().startswith(base_sha.lower()):
                return ancestor
        if _run_git(["rev-parse", "--is-shallow-repository"], cwd=repo).strip() != "true":
            raise PublishError(
                f"published commit {commit_sha[:12]} is not built on {base_sha[:12]}"
            )


def _gate_permits(gate: PatchGateResult, *, allow_workflow_changes: bool) -> bool:
    """Return whether a gate result may proceed to publication.

    The only overridable violation is the workflow-file block, and only when a
    caller explicitly released it. Every other violation (secrets, path escape,
    symlinks, submodules, size) is unconditional. The block is matched as the
    exact violation the gate marked, never by its wording: a substring match
    also dropped a path-escape or symlink violation naming a file such as
    ``../human release.md``.
    """
    if gate.ok:
        return True
    if not allow_workflow_changes or not gate.requires_human_release:
        return False
    remaining = [
        violation for violation in gate.violations if violation != gate.releasable_violation
    ]
    return not remaining
