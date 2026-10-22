"""Bounded Git views for a retained workspace.

The lightweight view fingerprints names, Git object identities and filesystem
metadata without constructing the binary patch or commit log.  The browser can
therefore invalidate its on-demand full view without paying for that full view
on every job-detail refresh.
"""

from __future__ import annotations

import hashlib
import re
import secrets
from collections.abc import Callable, Mapping
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from pathlib import Path

from fastapi import HTTPException

RunSandbox = Callable[..., tuple[int, str, str]]

COMMIT_SHA = re.compile(r"^[0-9a-fA-F]{7,64}$")

# Git reads the member-writable repository configuration.  These command-line
# overrides disable every configured command the read-only views do not need.
GIT_VIEW_ENV = {
    "GIT_CONFIG_COUNT": "3",
    "GIT_CONFIG_KEY_0": "core.fsmonitor",
    "GIT_CONFIG_VALUE_0": "",
    "GIT_CONFIG_KEY_1": "core.hooksPath",
    "GIT_CONFIG_VALUE_1": "/dev/null",
    "GIT_CONFIG_KEY_2": "log.showSignature",
    "GIT_CONFIG_VALUE_2": "false",
    "GIT_ALLOW_PROTOCOL": "",
}

# Filter names are repository-controlled, so neutralize them after discovery.
# Keep only Git LFS's canonical driver: a smudged file must still compare equal
# to its committed pointer.  LFS extensions remain disabled.
NEUTRALIZE_GIT_FILTERS = """set -f; IFS='
'; n="${GIT_CONFIG_COUNT:-0}"
for key in $(git config --name-only --get-regexp '^(filter|lfs[.]extension)[.]'); do
  value=$(git config --get "$key")
  case "$key=$value" in
    'filter.lfs.clean=git-lfs clean -- %f') continue ;;
    'filter.lfs.smudge=git-lfs smudge -- %f'|'filter.lfs.smudge=git-lfs smudge --skip -- %f')
      continue ;;
    'filter.lfs.process=git-lfs filter-process'|'filter.lfs.process=git-lfs filter-process --skip')
      continue ;;
  esac
  export "GIT_CONFIG_KEY_$n=$key" "GIT_CONFIG_VALUE_$n="
  n=$((n + 1))
done
export GIT_CONFIG_COUNT="$n"; unset IFS; set +f
"""


def _status_view(status_text: str) -> tuple[str, list[dict[str, str]]]:
    """Decode porcelain ``-z`` records, keeping destination paths exact."""
    records = status_text.split("\0")
    branch = ""
    if records and records[0].startswith("## "):
        branch = records.pop(0)[3:].split("...", 1)[0]
    changes: list[dict[str, str]] = []
    index = 0
    while index < len(records):
        record = records[index]
        index += 1
        if len(record) < 4:
            continue
        code = record[:2]
        changes.append({"code": code, "path": record[3:]})
        # Under -z, rename/copy destinations precede a second original-path record.
        if "R" in code or "C" in code:
            index += 1
    return branch, changes


def _revision_commands(target: str) -> str:
    """Write a content-sensitive but patch-free fingerprint transcript."""
    return (
        'revision_index="${TMPDIR:-/tmp}/hyi-browser-revision-index.$$"; '
        'rm -f "$revision_index"; '
        f'GIT_INDEX_FILE="$revision_index" git read-tree {target}; revision_status=$?; '
        f'GIT_INDEX_FILE="$revision_index" git --no-pager diff --raw -z '
        f"--no-ext-diff --no-textconv --ignore-submodules=dirty --submodule=short {target}; "
        "raw_status=$?; "
        '[ "$revision_status" -eq 0 ] && revision_status=$raw_status; '
        'GIT_INDEX_FILE="$revision_index" git ls-files -m -o --exclude-standard -z | '
        "xargs -0 -r stat --printf='%n\\0%s\\0%y\\0%z\\0%i\\0' -- 2>/dev/null || :; "
        'rm -f "$revision_index"; '
    )


def _revision(status_text: str, fingerprint: str) -> str:
    return hashlib.sha256(f"{status_text}\0{fingerprint}".encode()).hexdigest()


def run_git_metadata(
    run_sandbox: RunSandbox,
    backend: Any,
    workdir: Path | str,
    base_sha: str | None = None,
    *,
    timeout_s: float,
    max_output_bytes: int,
    env: Mapping[str, str] = GIT_VIEW_ENV,
) -> dict[str, Any]:
    """Read branch/change names and a revision without constructing a patch."""
    if base_sha and not COMMIT_SHA.fullmatch(base_sha):
        raise HTTPException(status_code=400, detail="base SHA is not a commit hash")
    target = base_sha or "HEAD"
    marker = secrets.token_hex(16)
    revision_tag = f"__HYI_{marker}_REVISION__"
    status_tag = f"__HYI_{marker}_STATUS__"
    script = NEUTRALIZE_GIT_FILTERS + (
        "git --no-pager status --porcelain=v1 --branch -z --ignore-submodules=dirty; "
        "status_status=$?; "
        f"printf '\n{revision_tag}\n'; "
        + _revision_commands(target)
        + "overall_status=$status_status; "
        '[ "$overall_status" -eq 0 ] && overall_status=$revision_status; '
        f"printf '\n{status_tag}%s\n' \"$overall_status\""
    )
    process_status, stdout, stderr = run_sandbox(
        backend,
        workdir=workdir,
        script=script,
        env={**env, "GIT_OPTIONAL_LOCKS": "0"},
        timeout_s=timeout_s,
        max_output_bytes=max_output_bytes,
    )
    status_text, separator, revision_and_status = stdout.partition(f"\n{revision_tag}\n")
    if not separator:
        raise HTTPException(status_code=502, detail=stderr or "git workspace inspection failed")
    fingerprint, _, git_status_text = revision_and_status.rpartition(f"\n{status_tag}")
    try:
        git_status = int(git_status_text.strip().splitlines()[0])
    except (ValueError, IndexError):
        git_status = process_status
    if git_status != 0:
        raise HTTPException(status_code=502, detail=stderr or "git workspace inspection failed")
    branch, changes = _status_view(status_text)
    return {
        "available": True,
        "revision": _revision(status_text, fingerprint),
        "branch": branch,
        "changes": changes,
        "change_count": len(changes),
    }


def run_git(
    run_sandbox: RunSandbox,
    backend: Any,
    workdir: Path | str,
    base_sha: str | None = None,
    *,
    timeout_s: float,
    max_output_bytes: int,
    exact_patch: bool = False,
    env: Mapping[str, str] = GIT_VIEW_ENV,
) -> dict[str, Any]:
    """Read status, a revision, the base-relative patch, and recent commits."""
    if base_sha and not COMMIT_SHA.fullmatch(base_sha):
        raise HTTPException(status_code=400, detail="base SHA is not a commit hash")
    target = base_sha or "HEAD"
    marker = secrets.token_hex(16)
    revision_tag = f"__HYI_{marker}_REVISION__"
    diff_tag = f"__HYI_{marker}_DIFF__"
    commits_tag = f"__HYI_{marker}_COMMITS__"
    status_tag = f"__HYI_{marker}_STATUS__"
    script = NEUTRALIZE_GIT_FILTERS + (
        "git --no-pager status --porcelain=v1 --branch -z --ignore-submodules=dirty; "
        "status_status=$?; "
        f"printf '\n{revision_tag}\n'; " + _revision_commands(target) + f"printf '\n{diff_tag}\n'; "
        'index_path="${TMPDIR:-/tmp}/hyi-browser-index.$$"; '
        'rm -f "$index_path"; trap \'rm -f "$index_path"\' EXIT; '
        f'GIT_INDEX_FILE="$index_path" git read-tree {target} && '
        "{ printf '.\\0'; GIT_INDEX_FILE=\"$index_path\" git ls-files -z --stage | "
        "sed -z -n 's/^160000 [0-9a-f]* [0-3]\\t/:(exclude,literal)/p'; } | "
        'GIT_INDEX_FILE="$index_path" git add -A -N --pathspec-from-file=- '
        "--pathspec-file-nul >/dev/null 2>&1 && "
        f'GIT_INDEX_FILE="$index_path" git --no-pager diff --no-ext-diff --no-textconv '
        "--no-color --src-prefix=a/ --dst-prefix=b/ --unified=3 "
        f"--ignore-submodules=dirty --submodule=short --binary {target}; diff_status=$?; "
        f"printf '\n{commits_tag}\n'; "
        "git --no-pager log -20 --pretty=format:'%H%x09%h%x09%s%x09%an%x09%aI'; "
        "log_status=$?; overall_status=$status_status; "
        '[ "$overall_status" -eq 0 ] && overall_status=$revision_status; '
        '[ "$overall_status" -eq 0 ] && overall_status=$diff_status; '
        '[ "$overall_status" -eq 0 ] && overall_status=$log_status; '
        f"printf '\n{status_tag}%s\n' \"$overall_status\""
    )
    process_status, stdout, stderr = run_sandbox(
        backend,
        workdir=workdir,
        script=script,
        env={**env, "GIT_OPTIONAL_LOCKS": "0"},
        timeout_s=timeout_s,
        max_output_bytes=max_output_bytes,
    )
    status_text, separator, remainder = stdout.partition(f"\n{revision_tag}\n")
    if not separator:
        raise HTTPException(status_code=502, detail=stderr or "git workspace inspection failed")
    fingerprint, separator, remainder = remainder.partition(f"\n{diff_tag}\n")
    if not separator:
        raise HTTPException(status_code=502, detail=stderr or "git workspace inspection failed")
    patch, separator, commits_and_status = remainder.partition(f"\n{commits_tag}\n")
    if not separator:
        raise HTTPException(status_code=502, detail=stderr or "git workspace inspection failed")
    commits_text, _, git_status_text = commits_and_status.rpartition(f"\n{status_tag}")
    try:
        git_status = int(git_status_text.strip().splitlines()[0])
    except (ValueError, IndexError):
        git_status = process_status
    if git_status != 0:
        raise HTTPException(status_code=502, detail=stderr or "git workspace inspection failed")
    if exact_patch and "\ufffd" in patch:
        raise HTTPException(status_code=502, detail="workspace patch is not exact UTF-8 text")

    branch, changes = _status_view(status_text)
    commits = []
    for line in commits_text.splitlines():
        parts = line.split("\t", 4)
        if len(parts) == 5:
            commits.append(
                {
                    "sha": parts[0],
                    "short_sha": parts[1],
                    "subject": parts[2],
                    "author": parts[3],
                    "authored_at": parts[4],
                }
            )
    return {
        "available": True,
        "revision": _revision(status_text, fingerprint),
        "branch": branch,
        "changes": changes,
        "change_count": len(changes),
        "patch": patch,
        "commits": commits,
    }
