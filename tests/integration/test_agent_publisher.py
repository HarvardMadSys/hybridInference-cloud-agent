"""Publisher tests against real local git repositories.

These use throwaway on-disk repos (no network, no GitHub) so the actual git
behavior is exercised: the branch really gets created, the patch really
applies, and — the part that matters — a rejected patch really leaves the
remote untouched.
"""

from __future__ import annotations

import subprocess
from typing import TYPE_CHECKING

import pytest

if TYPE_CHECKING:
    from pathlib import Path

from cloud_agent.publisher import (
    PublishError,
    TransientPublishError,
    fetch_base_bundle,
    publish_patch,
)

pytestmark = pytest.mark.integration

_GIT_ENV = {
    "PATH": "/usr/bin:/bin:/usr/local/bin",
    "GIT_CONFIG_GLOBAL": "/dev/null",
    "GIT_CONFIG_SYSTEM": "/dev/null",
    "GIT_AUTHOR_NAME": "Test",
    "GIT_AUTHOR_EMAIL": "test@example.com",
    "GIT_COMMITTER_NAME": "Test",
    "GIT_COMMITTER_EMAIL": "test@example.com",
}


def _git(args: list[str], cwd: Path) -> str:
    """Run git in a test repo."""
    result = subprocess.run(
        ["git", *args],
        cwd=str(cwd),
        env=_GIT_ENV,
        capture_output=True,
        text=True,
        check=True,
    )
    return result.stdout


@pytest.fixture()
def origin(tmp_path: Path) -> tuple[str, str]:
    """Create a bare origin with one commit; return (clone_url, base_sha)."""
    source = tmp_path / "source"
    source.mkdir()
    _git(["init", "--quiet", "--initial-branch=main", "."], cwd=source)
    (source / "app.py").write_text("def main():\n    return 0\n", encoding="utf-8")
    (source / "README.md").write_text("# demo\n", encoding="utf-8")
    _git(["add", "--all"], cwd=source)
    _git(["commit", "--quiet", "-m", "initial"], cwd=source)
    base_sha = _git(["rev-parse", "HEAD"], cwd=source).strip()

    bare = tmp_path / "origin.git"
    _git(["clone", "--quiet", "--bare", str(source), str(bare)], cwd=tmp_path)
    # Allow fetching an arbitrary SHA, as a real forge does.
    _git(["config", "uploadpack.allowAnySHA1InWant", "true"], cwd=bare)
    return str(bare), base_sha


def _branches(bare_url: str) -> list[str]:
    """List branches present in the bare origin."""
    out = subprocess.run(
        ["git", "for-each-ref", "--format=%(refname:short)", "refs/heads/"],
        cwd=bare_url,
        env=_GIT_ENV,
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    return [line.strip() for line in out.splitlines() if line.strip()]


def _blob_patch(
    origin: tuple[str, str],
    tmp_path: Path,
    *,
    before: bytes | None,
    after: bytes | None,
    path: str = "asset.bin",
) -> tuple[str, str]:
    """Generate an actual Git binary patch against an optional existing blob."""
    clone_url, base_sha = origin
    editor = tmp_path / "editor"
    _git(["clone", "--quiet", clone_url, str(editor)], cwd=tmp_path)
    target = editor / path
    target.parent.mkdir(parents=True, exist_ok=True)
    if before is not None:
        target.write_bytes(before)
        _git(["add", "--all"], cwd=editor)
        _git(["commit", "--quiet", "-m", "baseline binary asset"], cwd=editor)
        _git(["push", "--quiet", "origin", "main"], cwd=editor)
        base_sha = _git(["rev-parse", "HEAD"], cwd=editor).strip()
    if after is None:
        target.unlink()
    else:
        target.write_bytes(after)
    _git(["add", "--all"], cwd=editor)
    patch = _git(["diff", "--cached", "--binary", "--full-index"], cwd=editor)
    assert "GIT binary patch" in patch
    return patch, base_sha


def _published_blob(clone_url: str, branch: str, path: str = "asset.bin") -> bytes:
    return subprocess.run(
        ["git", "show", f"{branch}:{path}"],
        cwd=clone_url,
        env=_GIT_ENV,
        capture_output=True,
        check=True,
    ).stdout


@pytest.mark.parametrize("allow_workflow_changes", [False, True])
def test_binary_credential_is_refused_before_commit_or_push(
    origin, tmp_path, monkeypatch, allow_workflow_changes
):
    """Encoded credentials cannot use either binary data or human release to bypass scanning."""
    from cloud_agent import publisher

    credential = ("gh" + "p_" + "a" * 36).encode()
    path = ".github/workflows/asset.bin" if allow_workflow_changes else "asset.bin"
    patch, base_sha = _blob_patch(
        origin, tmp_path, before=None, after=b"\0\xff" + credential + b"\0", path=path
    )
    real_run_git = publisher._run_git
    commands: list[str] = []

    def record_command(args, **kwargs):
        commands.append(args[0])
        return real_run_git(args, **kwargs)

    monkeypatch.setattr(publisher, "_run_git", record_command)
    with pytest.raises(PublishError, match=r"after apply.*github_token") as excinfo:
        publish_patch(
            job_id="ajob_binary_secret",
            patch=patch,
            clone_url=origin[0],
            base_sha=base_sha,
            commit_message="agent: binary asset",
            allow_workflow_changes=allow_workflow_changes,
        )
    assert credential.decode() not in str(excinfo.value)
    assert "commit" not in commands
    assert "push" not in commands
    assert _branches(origin[0]) == ["main"]


@pytest.mark.parametrize("existing", [False, True])
def test_clean_binary_literals_and_deltas_publish(origin, tmp_path, existing):
    """Legitimate binary assets still round-trip through publication."""
    baseline = bytes(range(256)) * 32
    content = baseline[:1000] + b"binary update" + baseline[1013:]
    patch, base_sha = _blob_patch(
        origin, tmp_path, before=baseline if existing else None, after=content
    )
    assert ("delta " if existing else "literal ") in patch
    result = publish_patch(
        job_id="ajob_clean_binary",
        patch=patch,
        clone_url=origin[0],
        base_sha=base_sha,
        commit_message="agent: update binary asset",
    )
    assert _published_blob(origin[0], result.branch) == content


@pytest.mark.parametrize("change", ["unrelated", "remove", "delete"])
def test_binary_preexisting_or_removed_credentials_do_not_block(origin, tmp_path, change):
    """The scanner holds the patch responsible only for credentials it adds."""
    credential = ("gh" + "p_" + "a" * 36).encode()
    baseline = b"\0" + credential + b"\0existing asset"
    after = {
        "unrelated": baseline + b" updated",
        "remove": b"\0clean asset",
        "delete": None,
    }[change]
    patch, base_sha = _blob_patch(origin, tmp_path, before=baseline, after=after)
    result = publish_patch(
        job_id="ajob_existing_binary",
        patch=patch,
        clone_url=origin[0],
        base_sha=base_sha,
        commit_message="agent: update existing binary asset",
    )
    if after is not None:
        assert _published_blob(origin[0], result.branch) == after
    else:
        assert "asset.bin" not in _git(
            ["ls-tree", "--name-only", result.branch], cwd=tmp_path / "origin.git"
        )


@pytest.mark.parametrize("duplicate", [False, True])
def test_binary_new_or_duplicated_credential_is_blocked(origin, tmp_path, duplicate):
    """An existing credential shape does not waive new values or extra copies."""
    credential = ("gh" + "p_" + "a" * 36).encode()
    new_credential = credential if duplicate else ("gh" + "p_" + "b" * 36).encode()
    baseline = b"\0" + credential + b"\0"
    patch, base_sha = _blob_patch(
        origin, tmp_path, before=baseline, after=baseline + new_credential + b"\0"
    )
    with pytest.raises(PublishError, match=r"after apply.*github_token"):
        publish_patch(
            job_id="ajob_added_binary_secret",
            patch=patch,
            clone_url=origin[0],
            base_sha=base_sha,
            commit_message="agent: append binary asset",
        )
    assert _branches(origin[0]) == ["main"]


def test_binary_expansion_limit_refuses_before_git(origin, tmp_path, monkeypatch):
    """A compressed literal exceeding the decoded cap cannot reach Git apply."""
    from cloud_agent import publisher
    from cloud_agent_shared.git.patch_gate import MAX_BLOB_BYTES, MAX_PATCH_BYTES

    patch, base_sha = _blob_patch(origin, tmp_path, before=None, after=b"\0" * (MAX_BLOB_BYTES + 1))
    assert len(patch.encode()) < MAX_PATCH_BYTES

    def refuse_git(*args, **kwargs):
        pytest.fail("an oversized decoded binary must be rejected before any Git command")

    monkeypatch.setattr(publisher, "_run_git", refuse_git)
    with pytest.raises(PublishError, match="decoded blob byte limit"):
        publish_patch(
            job_id="ajob_binary_expansion",
            patch=patch,
            clone_url=origin[0],
            base_sha=base_sha,
            commit_message="agent: oversized binary",
        )
    assert _branches(origin[0]) == ["main"]


def test_post_apply_blob_limit_cannot_be_bypassed_by_patch_parser(origin, tmp_path, monkeypatch):
    """Actual object sizes are bounded independently of pre-apply declarations."""
    from cloud_agent import publisher

    patch, base_sha = _blob_patch(origin, tmp_path, before=None, after=b"\0" * 129)
    monkeypatch.setattr(publisher, "MAX_BLOB_BYTES", 128)
    with pytest.raises(PublishError, match=r"after apply.*128 byte limit"):
        publish_patch(
            job_id="ajob_binary_post_size",
            patch=patch,
            clone_url=origin[0],
            base_sha=base_sha,
            commit_message="agent: oversized binary",
        )
    assert _branches(origin[0]) == ["main"]


def test_binary_delta_assembling_credential_from_existing_fragments_is_blocked(origin, tmp_path):
    """Copied bytes in a delta are scanned after they become contiguous."""
    padding = bytes(range(256)) * 32
    prefix = b"gh" + b"p_"
    suffix = b"a" * 36
    baseline = padding + b"\0" + prefix + b" separator " + suffix + b"\0"
    after = padding + b"\0" + prefix + suffix + b"\0"
    patch, base_sha = _blob_patch(origin, tmp_path, before=baseline, after=after)
    assert "delta " in patch
    with pytest.raises(PublishError, match=r"after apply.*github_token"):
        publish_patch(
            job_id="ajob_binary_fragment_secret",
            patch=patch,
            clone_url=origin[0],
            base_sha=base_sha,
            commit_message="agent: reassemble binary asset",
        )
    assert _branches(origin[0]) == ["main"]


def test_gitattributes_cannot_hide_plaintext_credential_as_binary(origin, tmp_path):
    """Git's user-controlled binary classification does not control the scan."""
    clone_url, base_sha = origin
    editor = tmp_path / "editor"
    _git(["clone", "--quiet", clone_url, str(editor)], cwd=tmp_path)
    (editor / ".gitattributes").write_text("asset.txt -diff\n", encoding="utf-8")
    (editor / "asset.txt").write_bytes(("gh" + "p_" + "a" * 36).encode())
    _git(["add", "--all"], cwd=editor)
    patch = _git(["diff", "--cached", "--binary", "--full-index"], cwd=editor)
    assert "GIT binary patch" in patch
    with pytest.raises(PublishError, match=r"after apply.*github_token"):
        publish_patch(
            job_id="ajob_binary_attributes_secret",
            patch=patch,
            clone_url=clone_url,
            base_sha=base_sha,
            commit_message="agent: classify text as binary",
        )
    assert _branches(clone_url) == ["main"]


def test_staged_total_blob_limit_refuses_before_push(origin, tmp_path, monkeypatch):
    """Many small decoded files cannot evade the aggregate staged size cap."""
    from cloud_agent import publisher

    patch, base_sha = _blob_patch(origin, tmp_path, before=None, after=b"\0" * 64)
    editor = tmp_path / "editor"
    (editor / "another.bin").write_bytes(b"\0" * 64)
    _git(["add", "--all"], cwd=editor)
    patch = _git(["diff", "--cached", "--binary", "--full-index"], cwd=editor)
    monkeypatch.setattr(publisher, "MAX_CHANGED_BLOB_BYTES", 127)
    with pytest.raises(PublishError, match=r"after apply.*127 total byte limit"):
        publish_patch(
            job_id="ajob_binary_total_size",
            patch=patch,
            clone_url=origin[0],
            base_sha=base_sha,
            commit_message="agent: too many decoded bytes",
        )
    assert _branches(origin[0]) == ["main"]


def test_the_branch_tip_is_read_from_the_exact_ref_only(origin, tmp_path):
    """``ls-remote`` also lists any ref ending in the branch's name, and it can sort first."""
    from cloud_agent.publisher import _remote_branch_tip

    bare, base_sha = origin
    clone = tmp_path / "clone"
    _git(["clone", "--quiet", bare, str(clone)], cwd=tmp_path)
    _git(["commit", "--quiet", "--allow-empty", "-m", "decoy"], cwd=clone)
    _git(["push", "--quiet", "origin", "HEAD:refs/heads/a/refs/heads/agent/t1"], cwd=clone)

    assert _remote_branch_tip(clone, "agent/t1") is None
    _git(["push", "--quiet", "origin", f"{base_sha}:refs/heads/agent/t1"], cwd=clone)
    assert _remote_branch_tip(clone, "agent/t1") == base_sha


def test_publishes_branch_with_applied_patch(origin):
    """A clean patch lands on exactly one agent/<job-id> branch."""
    clone_url, base_sha = origin
    patch = (
        "diff --git a/app.py b/app.py\n"
        "--- a/app.py\n"
        "+++ b/app.py\n"
        "@@ -1,2 +1,3 @@\n"
        " def main():\n"
        '+    print("patched")\n'
        "     return 0\n"
    )

    result = publish_patch(
        job_id="ajob_test01",
        patch=patch,
        clone_url=clone_url,
        base_sha=base_sha,
        commit_message="agent: patch app.py",
    )

    assert result.branch == "agent/ajob_test01"
    assert result.changed_files == ["app.py"]
    assert sorted(_branches(clone_url)) == ["agent/ajob_test01", "main"]

    # main is untouched and the branch really contains the change.
    content = subprocess.run(
        ["git", "show", f"{result.branch}:app.py"],
        cwd=clone_url,
        env=_GIT_ENV,
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    assert 'print("patched")' in content
    main_content = subprocess.run(
        ["git", "show", "main:app.py"],
        cwd=clone_url,
        env=_GIT_ENV,
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    assert "patched" not in main_content


def test_the_push_credential_stays_out_of_argv_and_git_config(origin, monkeypatch):
    """Only the environment of the commands that reach the remote carries it.

    In the clone URL, the token was written to the clone's `.git/config` and
    passed as an argument to every `git-remote-https`, readable in `/proc`.
    """
    from pathlib import Path

    from cloud_agent import publisher

    clone_url, base_sha = origin
    secret = "publish-token-" + "s" * 24
    auth_env = {
        "GIT_CONFIG_COUNT": "1",
        "GIT_CONFIG_KEY_0": "http.https://github.com/.extraHeader",
        "GIT_CONFIG_VALUE_0": f"Authorization: Basic {secret}",
    }
    commands: list[tuple[list[str], bool]] = []
    real_run = subprocess.run

    def recording_run(args, **kwargs):
        env = kwargs.get("env") or {}
        commands.append((list(args), secret in env.get("GIT_CONFIG_VALUE_0", "")))
        return real_run(args, **kwargs)

    configs: list[str] = []
    real_rmtree = publisher.shutil.rmtree

    def inspect_then_remove(path, **kwargs):
        config = Path(path) / "repo" / ".git" / "config"
        configs.append(config.read_text(encoding="utf-8"))
        real_rmtree(path, **kwargs)

    monkeypatch.setattr(publisher.subprocess, "run", recording_run)
    monkeypatch.setattr(publisher.shutil, "rmtree", inspect_then_remove)
    publish_patch(
        job_id="ajob_credential",
        patch=(
            "diff --git a/app.py b/app.py\n--- a/app.py\n+++ b/app.py\n"
            "@@ -1,2 +1,3 @@\n def main():\n+    pass\n     return 0\n"
        ),
        clone_url=clone_url,
        base_sha=base_sha,
        commit_message="agent: patch app.py",
        auth_env=auth_env,
    )

    assert commands and all(secret not in " ".join(args) for args, _ in commands)
    assert len(configs) == 1 and secret not in configs[0]
    assert {args[1] for args, carried in commands if carried} == {"ls-remote", "fetch", "push"}
    assert _branches(clone_url) == ["agent/ajob_credential", "main"]


def test_a_retry_that_adopts_its_pushed_commit_still_authenticates_by_environment(
    origin, monkeypatch
):
    """The reconciling fetch and compare-and-set push reach the remote too."""
    from cloud_agent import publisher

    clone_url, base_sha = origin
    secret = "publish-token-" + "r" * 24
    auth_env = {
        "GIT_CONFIG_COUNT": "1",
        "GIT_CONFIG_KEY_0": "http.https://github.com/.extraHeader",
        "GIT_CONFIG_VALUE_0": f"Authorization: Basic {secret}",
    }
    commands: list[tuple[list[str], bool]] = []
    real_run = subprocess.run

    def recording_run(args, **kwargs):
        env = kwargs.get("env") or {}
        commands.append((list(args), secret in env.get("GIT_CONFIG_VALUE_0", "")))
        return real_run(args, **kwargs)

    monkeypatch.setattr(publisher.subprocess, "run", recording_run)
    published = [
        publish_patch(
            job_id="ajob_credential_retry",
            branch_id="athr_credential_retry",
            patch=_PATCH_APP,
            clone_url=clone_url,
            base_sha=base_sha,
            commit_message=f"agent: attempt {attempt}",
            auth_env=auth_env,
        )
        for attempt in (1, 2)
    ]

    assert published[1].commit_sha == published[0].commit_sha
    remote = [
        (args, carried) for args, carried in commands if "origin" in args and args[1] != "remote"
    ]
    assert remote and all(carried for _, carried in remote)
    assert all(secret not in " ".join(args) for args, _ in commands)
    reconciled = {args[1] for args, _ in remote if any(a.startswith("refs/heads/") for a in args)}
    assert reconciled == {"ls-remote", "fetch", "push"}


def test_workflow_patch_is_refused_and_remote_untouched(origin):
    """A .github/ patch never reaches the remote."""
    clone_url, base_sha = origin
    patch = (
        "diff --git a/.github/workflows/ci.yml b/.github/workflows/ci.yml\n"
        "--- a/.github/workflows/ci.yml\n"
        "+++ b/.github/workflows/ci.yml\n"
        "@@ -0,0 +1 @@\n"
        "+on: push\n"
    )

    with pytest.raises(PublishError) as excinfo:
        publish_patch(
            job_id="ajob_evil",
            patch=patch,
            clone_url=clone_url,
            base_sha=base_sha,
            commit_message="agent: sneak a workflow in",
        )
    assert "CI/workflow configuration" in str(excinfo.value)
    assert _branches(clone_url) == ["main"]


def test_secret_patch_is_refused_even_with_human_release(origin):
    """The workflow override does not waive the secret scan."""
    clone_url, base_sha = origin
    patch = (
        "diff --git a/.github/workflows/ci.yml b/.github/workflows/ci.yml\n"
        "--- a/.github/workflows/ci.yml\n"
        "+++ b/.github/workflows/ci.yml\n"
        "@@ -0,0 +1 @@\n"
        f'+  token: "ghp_{"a" * 36}"\n'
    )

    with pytest.raises(PublishError) as excinfo:
        publish_patch(
            job_id="ajob_secret",
            patch=patch,
            clone_url=clone_url,
            base_sha=base_sha,
            commit_message="agent: with secret",
            allow_workflow_changes=True,
        )
    assert "github_token" in str(excinfo.value)
    assert _branches(clone_url) == ["main"]


def test_human_released_workflow_patch_publishes(origin):
    """With explicit release, a clean workflow patch is allowed through."""
    clone_url, base_sha = origin
    patch = (
        "diff --git a/.github/workflows/ci.yml b/.github/workflows/ci.yml\n"
        "new file mode 100644\n"
        "--- /dev/null\n"
        "+++ b/.github/workflows/ci.yml\n"
        "@@ -0,0 +1 @@\n"
        "+on: push\n"
    )

    result = publish_patch(
        job_id="ajob_released",
        patch=patch,
        clone_url=clone_url,
        base_sha=base_sha,
        commit_message="agent: approved workflow change",
        allow_workflow_changes=True,
    )
    assert result.branch == "agent/ajob_released"
    assert "agent/ajob_released" in _branches(clone_url)


def test_unapplyable_patch_fails_without_publishing(origin):
    """A patch that does not apply is an error, not a partial push."""
    clone_url, base_sha = origin
    patch = (
        "diff --git a/missing.py b/missing.py\n"
        "--- a/missing.py\n"
        "+++ b/missing.py\n"
        "@@ -1,3 +1,3 @@\n"
        " context that does not exist\n"
        "-old\n"
        "+new\n"
    )

    with pytest.raises(PublishError):
        publish_patch(
            job_id="ajob_bad",
            patch=patch,
            clone_url=clone_url,
            base_sha=base_sha,
            commit_message="agent: broken patch",
        )
    assert _branches(clone_url) == ["main"]


def test_noop_patch_is_reported_rather_than_pushed(origin):
    """A patch that applies to nothing does not create an empty branch."""
    clone_url, base_sha = origin
    patch = (
        "diff --git a/app.py b/app.py\n"
        "--- a/app.py\n"
        "+++ b/app.py\n"
        "@@ -1,2 +1,2 @@\n"
        " def main():\n"
        "     return 0\n"
    )

    with pytest.raises(PublishError):
        publish_patch(
            job_id="ajob_noop",
            patch=patch,
            clone_url=clone_url,
            base_sha=base_sha,
            commit_message="agent: no-op",
        )
    assert _branches(clone_url) == ["main"]


def test_new_file_patch_publishes(origin):
    """Adding a new file works end to end (the common agent case)."""
    clone_url, base_sha = origin
    patch = (
        "diff --git a/newmod.py b/newmod.py\n"
        "new file mode 100644\n"
        "--- /dev/null\n"
        "+++ b/newmod.py\n"
        "@@ -0,0 +1,2 @@\n"
        "+def helper():\n"
        "+    return 42\n"
    )

    result = publish_patch(
        job_id="ajob_newfile",
        patch=patch,
        clone_url=clone_url,
        base_sha=base_sha,
        commit_message="agent: add helper",
    )
    listed = subprocess.run(
        ["git", "ls-tree", "--name-only", result.branch],
        cwd=clone_url,
        env=_GIT_ENV,
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    assert "newmod.py" in listed


def test_option_shaped_base_sha_is_refused_before_git_sees_it():
    """base_sha must never reach git as an option.

    Regression: git parses a leading `--` argument as an option even after the
    remote name, so `--upload-pack=/bin/sh -c ...` would have executed a
    command inside the trusted publisher — the process that holds the
    repository credential.
    """
    for hostile in (
        "--upload-pack=/bin/sh -c 'touch /tmp/pwned'",
        "--exec=evil",
        "-x",
        "not-a-sha",
        "",
    ):
        with pytest.raises(PublishError) as excinfo:
            publish_patch(
                job_id="ajob_x",
                patch="diff --git a/x b/x\n",
                clone_url="/nonexistent",
                base_sha=hostile,
                commit_message="m",
            )
        assert "commit hash" in str(excinfo.value)


def test_the_post_apply_gate_catches_what_the_parser_would_miss(origin, monkeypatch):
    """The tree that gets pushed is gated, not just the patch text.

    The pre-apply gate reads a *parse* of the patch, and a parse can be talked
    around — a C-quoted `.github/` path did exactly that. This pins the
    backstop: with the text gate neutralised, the post-apply pass still refuses,
    because its paths come from git's own index rather than from the patch.

    Pushing a branch that carries a workflow file is the outcome being
    prevented: GitHub will execute agent-authored YAML with repository secrets,
    which draft-PR review does not mitigate.
    """
    from cloud_agent import publisher
    from cloud_agent_host.patch_gate import PatchGateResult

    clone_url, base_sha = origin
    patch = (
        "diff --git a/.github/workflows/evil.yml b/.github/workflows/evil.yml\n"
        "new file mode 100644\n"
        "--- /dev/null\n"
        "+++ b/.github/workflows/evil.yml\n"
        "@@ -0,0 +1 @@\n"
        "+on: push\n"
    )

    # Stand in for a parser the patch has defeated: the text gate sees nothing.
    monkeypatch.setattr(
        publisher,
        "validate_patch",
        lambda *_a, **_k: PatchGateResult(ok=True, changed_files=["README.md"]),
    )

    with pytest.raises(PublishError, match="after apply"):
        publish_patch(
            job_id="ajob_postgate",
            patch=patch,
            clone_url=clone_url,
            base_sha=base_sha,
            commit_message="agent: sneak in a workflow",
        )

    # And nothing reached the remote.
    assert "agent/ajob_postgate" not in _branches(clone_url)


def _gitlink_patch(origin: tuple[str, str], tmp_path: Path) -> str:
    """A real `git diff` adding `.gitmodules` and a submodule entry."""
    clone_url, _ = origin
    editor = tmp_path / "editor"
    _git(["clone", "--quiet", clone_url, str(editor)], cwd=tmp_path)
    (editor / ".gitmodules").write_text(
        '[submodule "vendor/lib"]\n\tpath = vendor/lib\n\turl = https://example.invalid/lib.git\n',
        encoding="utf-8",
    )
    _git(["add", ".gitmodules"], cwd=editor)
    _git(["update-index", "--add", "--cacheinfo", f"160000,{'d' * 40},vendor/lib"], cwd=editor)
    patch = _git(["diff", "--cached", "--binary", "--full-index"], cwd=editor)
    assert "new file mode 160000" in patch
    return patch


@pytest.mark.parametrize("parser_defeated", [False, True])
def test_a_submodule_entry_never_reaches_the_remote(origin, tmp_path, monkeypatch, parser_defeated):
    """A gitlink carries code no gate reads: both passes refuse it, and nothing is pushed.

    `git apply --3way` writes such an entry into the index, so without the
    post-apply check a parser that missed it pushed a commit whose tree held
    `160000 commit <sha> vendor/lib`.
    """
    from cloud_agent import publisher
    from cloud_agent_host.patch_gate import PatchGateResult

    clone_url, base_sha = origin
    patch = _gitlink_patch(origin, tmp_path)
    if parser_defeated:
        monkeypatch.setattr(
            publisher,
            "validate_patch",
            lambda *_a, **_k: PatchGateResult(ok=True, changed_files=[".gitmodules"]),
        )

    with pytest.raises(PublishError, match="submodule entries: vendor/lib") as excinfo:
        publish_patch(
            job_id="ajob_gitlink",
            patch=patch,
            clone_url=clone_url,
            base_sha=base_sha,
            commit_message="agent: vendor a library",
        )
    assert ("after apply" in str(excinfo.value)) is parser_defeated
    assert _branches(clone_url) == ["main"]


def test_the_post_apply_gate_refuses_a_name_with_a_newline(origin, monkeypatch):
    """A newline git really staged in a file name is refused before any push.

    The publisher renders changed names into the pull request body the App
    posts, and a line break is how a name would leave its code span there.
    """
    from cloud_agent import publisher
    from cloud_agent_host.patch_gate import PatchGateResult

    clone_url, base_sha = origin
    patch = (
        'diff --git "a/x\\ny.txt" "b/x\\ny.txt"\n'
        "new file mode 100644\n"
        "--- /dev/null\n"
        '+++ "b/x\\ny.txt"\n'
        "@@ -0,0 +1 @@\n"
        "+hello\n"
    )
    monkeypatch.setattr(
        publisher,
        "validate_patch",
        lambda *_a, **_k: PatchGateResult(ok=True, changed_files=["README.md"]),
    )

    with pytest.raises(PublishError, match=r"after apply: .*control characters"):
        publish_patch(
            job_id="ajob_newline_name",
            patch=patch,
            clone_url=clone_url,
            base_sha=base_sha,
            commit_message="agent: add a file",
        )

    assert "agent/ajob_newline_name" not in _branches(clone_url)


def test_the_post_apply_gate_reports_the_real_paths(origin):
    """A clean publish reports the change set git staged, not the parse."""
    clone_url, base_sha = origin
    patch = (
        "diff --git a/app.py b/app.py\n"
        "--- a/app.py\n+++ b/app.py\n@@ -1,2 +1,3 @@\n def main():\n+    x = 1\n     return 0\n"
    )

    result = publish_patch(
        job_id="ajob_realpaths",
        patch=patch,
        clone_url=clone_url,
        base_sha=base_sha,
        commit_message="agent: touch app.py",
    )

    assert result.changed_files == ["app.py"]


_PATCH_APP = (
    "diff --git a/app.py b/app.py\n"
    "--- a/app.py\n+++ b/app.py\n@@ -1,2 +1,3 @@\n def main():\n+    x = 1\n     return 0\n"
)


def test_follow_up_fast_forwards_its_own_branch(origin):
    """Turn 2 built on turn 1's published commit lands on the same branch."""
    clone_url, base_sha = origin

    first = publish_patch(
        job_id="ajob_turn1",
        branch_id="athr_thread1",
        patch=_PATCH_APP,
        clone_url=clone_url,
        base_sha=base_sha,
        commit_message="agent: turn 1",
    )

    second_patch = (
        "diff --git a/README.md b/README.md\n"
        "--- a/README.md\n+++ b/README.md\n@@ -1 +1,2 @@\n # demo\n+turn two\n"
    )
    second = publish_patch(
        job_id="ajob_turn2",
        branch_id="athr_thread1",
        patch=second_patch,
        clone_url=clone_url,
        # A follow-up pins the previous turn's published commit — the case the
        # moved-branch check must let through.
        base_sha=first.commit_sha,
        commit_message="agent: turn 2",
    )

    assert second.branch == first.branch == "agent/athr_thread1"


def test_retry_adopts_the_matching_commit_already_pushed_before_a_crash(origin):
    """A lost DB/PR write reuses the exact remote commit, not a rebuilt one."""
    clone_url, base_sha = origin
    first = publish_patch(
        job_id="ajob_lost_result",
        branch_id="athr_lost_result",
        patch=_PATCH_APP,
        clone_url=clone_url,
        base_sha=base_sha,
        commit_message="agent: first process",
        include_base_bundle=True,
    )
    retry = publish_patch(
        job_id="ajob_lost_result",
        branch_id="athr_lost_result",
        patch=_PATCH_APP,
        clone_url=clone_url,
        base_sha=base_sha,
        commit_message="agent: a rebuilt commit would differ",
        include_base_bundle=True,
    )

    assert retry.commit_sha == first.commit_sha
    assert retry.changed_files == first.changed_files
    assert retry.base_bundle is not None


def test_retry_rejects_matching_tree_hidden_in_a_merge_commit(origin, tmp_path):
    """Matching tree and first parent are insufficient: publisher commits never merge."""
    clone_url, base_sha = origin
    human = tmp_path / "merge"
    _git(["clone", "--quiet", clone_url, str(human)], cwd=tmp_path)
    _git(["checkout", "--quiet", "--detach", base_sha], cwd=human)

    # The second parent contains work that the matching tree deliberately hides.
    _git(["checkout", "--quiet", "-b", "hidden", base_sha], cwd=human)
    (human / "HIDDEN.md").write_text("human work\n", encoding="utf-8")
    _git(["add", "--all"], cwd=human)
    _git(["commit", "--quiet", "-m", "human: hidden parent"], cwd=human)
    hidden_parent = _git(["rev-parse", "HEAD"], cwd=human).strip()

    _git(["checkout", "--quiet", "--detach", base_sha], cwd=human)
    (human / "app.py").write_text("def main():\n    x = 1\n    return 0\n", encoding="utf-8")
    _git(["add", "--all"], cwd=human)
    expected_tree = _git(["write-tree"], cwd=human).strip()
    forged_merge = _git(
        [
            "commit-tree",
            expected_tree,
            "-p",
            base_sha,
            "-p",
            hidden_parent,
            "-m",
            "human: forged matching merge",
        ],
        cwd=human,
    ).strip()
    _git(
        ["push", "--quiet", "origin", f"{forged_merge}:refs/heads/agent/athr_merge"],
        cwd=human,
    )

    with pytest.raises(PublishError, match="has moved") as refused:
        publish_patch(
            job_id="ajob_merge",
            branch_id="athr_merge",
            patch=_PATCH_APP,
            clone_url=clone_url,
            base_sha=base_sha,
            commit_message="agent: must not adopt a merge",
        )
    assert not isinstance(refused.value, TransientPublishError)


def test_retry_fences_a_branch_advance_after_recovered_tip_validation(
    origin, tmp_path, monkeypatch
):
    """The final ordinary push prevents recording an old reconciled tip."""
    from cloud_agent import publisher as publisher_module

    clone_url, base_sha = origin
    first = publish_patch(
        job_id="ajob_reconcile_race",
        branch_id="athr_reconcile_race",
        patch=_PATCH_APP,
        clone_url=clone_url,
        base_sha=base_sha,
        commit_message="agent: first process",
    )

    human = tmp_path / "reconcile-racer"
    _git(["clone", "--quiet", clone_url, str(human)], cwd=tmp_path)
    _git(
        ["checkout", "--quiet", "-b", "agent/athr_reconcile_race", first.commit_sha],
        cwd=human,
    )
    (human / "HOTFIX.md").write_text("human edit\n", encoding="utf-8")
    _git(["add", "--all"], cwd=human)
    _git(["commit", "--quiet", "-m", "human: advance during reconciliation"], cwd=human)
    human_tip = _git(["rev-parse", "HEAD"], cwd=human).strip()

    real_run_git = publisher_module._run_git
    advanced = False

    def race_on_fence(args, **kwargs):
        nonlocal advanced
        expected_refspec = f"{first.commit_sha}:refs/heads/agent/athr_reconcile_race"
        if args[0] == "push" and args[-1] == expected_refspec and not advanced:
            advanced = True
            _git(
                ["push", "--quiet", "origin", f"{human_tip}:refs/heads/agent/athr_reconcile_race"],
                cwd=human,
            )
        return real_run_git(args, **kwargs)

    monkeypatch.setattr(publisher_module, "_run_git", race_on_fence)
    with pytest.raises(TransientPublishError, match="git push failed"):
        publish_patch(
            job_id="ajob_reconcile_race",
            branch_id="athr_reconcile_race",
            patch=_PATCH_APP,
            clone_url=clone_url,
            base_sha=base_sha,
            commit_message="agent: retry after lost result",
        )

    assert advanced
    assert (
        _git(
            ["rev-parse", "refs/heads/agent/athr_reconcile_race"], cwd=tmp_path / "origin.git"
        ).strip()
        == human_tip
    )


def _prerequisites(bundle: bytes) -> list[str]:
    """The commits a bundle requires the receiving repository to hold."""
    header = bundle.split(b"\n\n", 1)[0].decode()
    return [line[1:].split()[0] for line in header.splitlines() if line.startswith("-")]


def test_base_bundle_carries_only_what_the_published_commit_adds(origin, tmp_path):
    """The workspace already holds the base its patch was taken against.

    Both bundles name that base as a prerequisite, so neither ships the
    repository a second time, and each imports into a shallow checkout of the
    base without moving its HEAD — the retry from the remote as much as the
    one built at publish time.
    """
    clone_url, base_sha = origin

    result = publish_patch(
        job_id="ajob_bundle",
        branch_id="athr_bundle",
        patch=_PATCH_APP,
        clone_url=clone_url,
        base_sha=base_sha,
        commit_message="agent: turn 1",
        include_base_bundle=True,
    )
    retried = fetch_base_bundle(clone_url=f"file://{clone_url}", commit_sha=result.commit_sha)

    assert result.base_bundle is not None
    assert _prerequisites(result.base_bundle) == [base_sha]
    assert _prerequisites(retried) == [base_sha]

    workspace = tmp_path / "workspace"
    _git(["init", "--quiet", str(workspace)], cwd=tmp_path)
    _git(["fetch", "--quiet", "--depth", "1", f"file://{clone_url}", base_sha], cwd=workspace)
    _git(["checkout", "--quiet", "--detach", "FETCH_HEAD"], cwd=workspace)
    bundle = tmp_path / "base.bundle"
    for payload in (result.base_bundle, retried):
        bundle.write_bytes(payload)
        listed = _git(["bundle", "list-heads", str(bundle), "HEAD"], cwd=workspace).strip()
        assert listed == f"{result.commit_sha} HEAD"
        _git(["bundle", "verify", str(bundle)], cwd=workspace)
        _git(["bundle", "unbundle", str(bundle)], cwd=workspace)
    _git(["cat-file", "-e", f"{result.commit_sha}^{{commit}}"], cwd=workspace)
    assert _git(["rev-parse", "HEAD"], cwd=workspace).strip() == base_sha


def test_republishing_the_same_patch_reuses_the_commit_already_pushed(origin):
    """A retry after the push, and before the PR is recorded, must not refuse.

    The second commit object differs because its timestamps do, but it is the
    same change on the same base. The branch already has that change, and the
    platform does not force-push, so the retry keeps the existing tip.
    """
    clone_url, base_sha = origin
    kwargs = {
        "job_id": "ajob_retry",
        "branch_id": "athr_retry",
        "patch": _PATCH_APP,
        "clone_url": clone_url,
        "base_sha": base_sha,
        "commit_message": "agent: turn 1",
        "include_base_bundle": True,
    }
    first = publish_patch(**kwargs)
    second = publish_patch(**kwargs)

    assert second.commit_sha == first.commit_sha
    assert second.branch == first.branch
    assert second.changed_files == first.changed_files
    assert second.base_bundle is not None
    tip = subprocess.run(
        ["git", "rev-parse", "refs/heads/agent/athr_retry"],
        cwd=clone_url,
        env=_GIT_ENV,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    assert tip == first.commit_sha


def test_branch_moved_by_someone_else_is_refused_with_the_reason(origin, tmp_path):
    """Human commits on the agent branch refuse the publish, by name.

    The platform never force-pushes, so the push would fail regardless; the
    property under test is that the recorded reason says what happened
    ("branch has moved", whose commits) instead of git's "failed to push some
    refs", which reads like an infrastructure fault. Nothing may reach the
    remote.
    """
    clone_url, base_sha = origin

    first = publish_patch(
        job_id="ajob_turnA",
        branch_id="athr_moved",
        patch=_PATCH_APP,
        clone_url=clone_url,
        base_sha=base_sha,
        commit_message="agent: turn 1",
    )

    # A human pushes a commit onto the agent branch.
    human = tmp_path / "human"
    _git(["clone", "--quiet", "--branch", "agent/athr_moved", clone_url, str(human)], cwd=tmp_path)
    (human / "HOTFIX.md").write_text("human edit\n", encoding="utf-8")
    _git(["add", "--all"], cwd=human)
    _git(["commit", "--quiet", "-m", "human: hotfix on the agent branch"], cwd=human)
    _git(["push", "--quiet", "origin", "agent/athr_moved"], cwd=human)
    moved_tip = _git(["rev-parse", "HEAD"], cwd=human).strip()

    with pytest.raises(PublishError, match="has moved"):
        publish_patch(
            job_id="ajob_turnB",
            branch_id="athr_moved",
            patch=_PATCH_APP,
            clone_url=clone_url,
            base_sha=first.commit_sha,
            commit_message="agent: turn 2",
        )

    # The human's commit is still the tip; the refused turn left no trace.
    out = subprocess.run(
        ["git", "rev-parse", "refs/heads/agent/athr_moved"],
        cwd=clone_url,
        env=_GIT_ENV,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    assert out == moved_tip


def test_non_fast_forward_race_retries_once_then_reports_divergence(origin, tmp_path, monkeypatch):
    """An uncertain push retries; the next read makes human movement permanent."""
    from cloud_agent import publisher as publisher_module

    clone_url, base_sha = origin
    human = tmp_path / "racer"
    _git(["clone", "--quiet", clone_url, str(human)], cwd=tmp_path)
    _git(["checkout", "--quiet", "-b", "agent/athr_race", base_sha], cwd=human)
    (human / "HOTFIX.md").write_text("human edit\n", encoding="utf-8")
    _git(["add", "--all"], cwd=human)
    _git(["commit", "--quiet", "-m", "human: win push race"], cwd=human)
    _git(["push", "--quiet", "origin", "agent/athr_race"], cwd=human)

    monkeypatch.setattr(publisher_module, "_remote_branch_tip", lambda *_args: None)
    with pytest.raises(TransientPublishError, match="git push failed"):
        publish_patch(
            job_id="ajob_race",
            branch_id="athr_race",
            patch=_PATCH_APP,
            clone_url=clone_url,
            base_sha=base_sha,
            commit_message="agent: lost race",
        )

    monkeypatch.undo()
    with pytest.raises(PublishError, match="has moved") as refused:
        publish_patch(
            job_id="ajob_race",
            branch_id="athr_race",
            patch=_PATCH_APP,
            clone_url=clone_url,
            base_sha=base_sha,
            commit_message="agent: retry after race",
        )
    assert not isinstance(refused.value, TransientPublishError)
