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

logger = get_logger(__name__)

_GIT_TIMEOUT_S = 120.0

# A commit hash and nothing else. git treats a leading `-` as an option even
# in positions that look like operands, so an unvalidated ref is an argument
# injection into a process that holds the repository credential.
_COMMIT_SHA = re.compile(r"[0-9a-fA-F]{7,64}")


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


class PublishError(Exception):
    """Raised when publication fails; the message is safe to store as detail."""


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
) -> str:
    """Run one git command hermetically and return stdout."""
    timeout_s = number_setting("publish_git_timeout_s", _GIT_TIMEOUT_S)
    try:
        completed = subprocess.run(
            ["git", *args],
            cwd=str(cwd) if cwd else None,
            env=_git_env(env_extra),
            capture_output=True,
            text=True,
            timeout=timeout_s,
            stdin=subprocess.DEVNULL,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise PublishError(f"git {args[0]} timed out after {timeout_s:.0f}s") from exc
    if completed.returncode != 0:
        detail = (completed.stderr or completed.stdout or "").strip().splitlines()
        tail = detail[-1] if detail else "no output"
        raise PublishError(f"git {args[0]} failed: {tail}")
    return completed.stdout


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
    blobs: list[_StagedBlob]


def _remote_branch_tip(repo: Path, branch: str) -> str | None:
    """Return the remote tip of ``branch``, or ``None`` when it does not exist."""
    raw = _run_git(
        ["ls-remote", "origin", "--end-of-options", f"refs/heads/{branch}"], cwd=repo
    ).strip()
    if not raw:
        return None
    return raw.split()[0]


def _check_branch_unmoved(repo: Path, branch: str, base_sha: str) -> None:
    """Refuse to publish onto a branch that has commits this run did not build on.

    A turn's commit is built on its pinned ``base_sha`` — for a follow-up,
    that is the previous turn's published commit, so the push fast-forwards.
    If the remote tip is anything else, someone (a human, most likely) pushed
    to the agent branch after the sandbox last saw it, and this patch was
    generated against a tree that does not contain their work. Pushing would
    fail anyway (the refspec never forces); the point of checking first is the
    recorded reason: "failed to push some refs" reads like an infrastructure
    fault, and this reads like what happened.
    """
    tip = _remote_branch_tip(repo, branch)
    if tip is None:
        return
    if tip.lower().startswith(base_sha.lower()):
        return
    raise PublishError(
        f"branch {branch} has moved: its tip is {tip[:12]} but this run built on "
        f"{base_sha[:12]}. Commits were pushed to the branch after this run started — "
        "the platform never force-pushes, so review them and either start a new "
        "follow-up (which builds on the branch's published state) or delete the "
        "branch to republish from scratch."
    )


def _staged_change_set(repo: Path) -> _StagedChangeSet:
    """Read the staged paths and their modes straight out of the index.

    ``-z`` output is NUL-separated and never quoted, which is the point: the
    quoting git applies in a *diff header* is exactly what a hostile patch used
    to hide a path from the pre-apply gate.
    """
    raw = _run_git(["diff", "--cached", "--raw", "-z", "--no-renames", "--abbrev=64"], cwd=repo)
    paths: list[str] = []
    symlinks: list[str] = []
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
        path = fields[index + 1]
        parts = meta[1:].split()
        dst_mode = parts[1] if len(parts) > 1 else ""
        if path:
            paths.append(path)
            if dst_mode == "120000":
                symlinks.append(path)
            if dst_mode in ("100644", "100755"):
                blobs.append(
                    _StagedBlob(
                        path=path,
                        sha=parts[3],
                        base_sha=parts[2] if parts[0] in ("100644", "100755") else None,
                    )
                )
        index += 2
    return _StagedChangeSet(paths=paths, symlinks=symlinks, blobs=blobs)


def _blob_size(repo: Path, sha: str) -> int:
    """Check an immutable object's size before capturing any of its contents."""
    size = int(_run_git(["cat-file", "-s", sha], cwd=repo).strip())
    if size > MAX_BLOB_BYTES:
        raise PublishError(
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


def _validate_staged_blobs(repo: Path, blobs: list[_StagedBlob]) -> None:
    """Reject new credentials in staged text or binary files before committing."""
    sizes = [_blob_size(repo, blob.sha) for blob in blobs]
    if sum(sizes) > MAX_CHANGED_BLOB_BYTES:
        raise PublishError(
            f"patch rejected after apply: blobs exceed the {MAX_CHANGED_BLOB_BYTES} total byte limit"
        )
    for blob, size in zip(blobs, sizes, strict=True):
        content = _read_blob(repo, blob.sha, size)
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
            raise PublishError(
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
) -> PublishResult:
    """Validate and publish one run's patch on its stable thread branch.

    ``clone_url`` should already carry whatever short-lived credential the
    caller minted — it never leaves this process and never enters the sandbox.
    ``allow_workflow_changes`` is the explicit human release for patches that
    touch ``.github/``; it defaults to False so the gate holds by default.
    Actual staged files, including binary assets, are size-checked and scanned
    for newly introduced credentials before committing or pushing.

    Raises :class:`PublishError` with a reviewer-readable message on any gate
    violation or git failure.
    """
    if not _COMMIT_SHA.fullmatch(base_sha or ""):
        # base_sha reaches here from user input on the job. Anything that is
        # not a bare commit hash is refused before it can be handed to git.
        raise PublishError(f"base_sha must be a full commit hash, got {base_sha!r}")

    gate = validate_patch(patch)
    if not _gate_permits(gate, allow_workflow_changes=allow_workflow_changes):
        raise PublishError(f"patch rejected: {gate.reason}")

    branch = branch_name_for(branch_id or job_id)
    workdir = Path(tempfile.mkdtemp(prefix=f"agent-publish-{job_id}-"))
    try:
        repo = workdir / "repo"
        # Fetch only the one commit the agent worked from: cheap, and it means
        # a patch cannot be applied onto some other branch's tree by accident.
        _run_git(["init", "--quiet", str(repo)])
        _run_git(["remote", "add", "origin", clone_url], cwd=repo)
        # Before any expensive work: a branch someone pushed to gets a clear
        # refusal naming their commits, not a late "failed to push some refs".
        _check_branch_unmoved(repo, branch, base_sha)
        # `--end-of-options` plus the shape check above: git parses a leading
        # `--` argument as an option even after the remote name, so an
        # owner-supplied base_sha of `--upload-pack=/bin/sh -c ...` would run a
        # command *inside the trusted publisher*. Two independent stops,
        # because this process holds the GitHub credential.
        _run_git(
            ["fetch", "--quiet", "--depth", "1", "origin", "--end-of-options", base_sha],
            cwd=repo,
        )
        _run_git(["checkout", "--quiet", "-b", branch, "FETCH_HEAD"], cwd=repo)

        patch_file = workdir / "job.patch"
        patch_file.write_text(patch, encoding="utf-8")
        # `--3way` recovers from minor context drift; `--whitespace=nowarn`
        # keeps whitespace-only noise from failing the apply.
        _run_git(
            ["apply", "--3way", "--whitespace=nowarn", str(patch_file)],
            cwd=repo,
        )

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
        post = validate_change_set(staged.paths, staged.symlinks)
        if not _gate_permits(post, allow_workflow_changes=allow_workflow_changes):
            raise PublishError(f"patch rejected after apply: {post.reason}")
        _validate_staged_blobs(repo, staged.blobs)
        gate = post
        _run_git(["commit", "--quiet", "--no-verify", "-m", commit_message], cwd=repo)
        commit_sha = _run_git(["rev-parse", "HEAD"], cwd=repo).strip()
        base_bundle = (
            build_base_bundle(str(repo), commit=commit_sha, prerequisite=_head_parent(repo))
            if include_base_bundle
            else None
        )

        # Pinned refspec: this process can only ever write the one branch, and
        # never force-updates it.
        _run_git(
            ["push", "--quiet", "origin", f"refs/heads/{branch}:refs/heads/{branch}"],
            cwd=repo,
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
    """The commit a published one was made on: the base its workspace holds.

    The snapshot's patch was diffed against this commit inside the workspace,
    so a base bundle needs to carry only what the published commit adds to it.
    """
    return _run_git(["rev-parse", "--verify", "HEAD^1"], cwd=repo).strip()


def fetch_base_bundle(*, clone_url: str, commit_sha: str) -> bytes:
    """Fetch one published commit and return its bounded credential-free bundle.

    Two commits deep, so the parent is at hand to leave out of the bundle.
    """
    if not _COMMIT_SHA.fullmatch(commit_sha or ""):
        raise PublishError(f"commit_sha must be a full commit hash, got {commit_sha!r}")
    workdir = Path(tempfile.mkdtemp(prefix="agent-base-import-"))
    try:
        repo = workdir / "repo"
        _run_git(["init", "--quiet", str(repo)])
        _run_git(["remote", "add", "origin", clone_url], cwd=repo)
        _run_git(
            ["fetch", "--quiet", "--depth", "2", "origin", "--end-of-options", commit_sha],
            cwd=repo,
        )
        _run_git(["checkout", "--quiet", "--detach", "FETCH_HEAD"], cwd=repo)
        try:
            return build_base_bundle(str(repo), commit=commit_sha, prerequisite=_head_parent(repo))
        except Exception as exc:
            raise PublishError(f"could not build the retained base bundle: {exc}") from exc
    finally:
        shutil.rmtree(workdir, ignore_errors=True)


def _gate_permits(gate: PatchGateResult, *, allow_workflow_changes: bool) -> bool:
    """Return whether a gate result may proceed to publication.

    The only overridable violation is the workflow-file block, and only when a
    human explicitly released it. Every other violation (secrets, path escape,
    symlinks, size) is unconditional.
    """
    if gate.ok:
        return True
    if not allow_workflow_changes or not gate.requires_human_release:
        return False
    remaining = [violation for violation in gate.violations if "human release" not in violation]
    return not remaining
