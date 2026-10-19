"""Unit tests for the agent patch validation gate.

This gate is the security boundary of the patch-out model: everything an
untrusted agent can influence passes through it before becoming a git object.
"""

from __future__ import annotations

import base64
import zlib

import pytest

from cloud_agent_host.patch_gate import (
    MAX_CHANGED_FILES,
    MAX_PATCH_BYTES,
    branch_name_for,
    validate_patch,
)
from cloud_agent_shared.git.patch_gate import (
    MAX_BLOB_BYTES,
    MAX_CHANGED_BLOB_BYTES,
    scan_blob_secrets,
    validate_change_set,
)

_CLEAN_PATCH = """diff --git a/src/app.py b/src/app.py
index 1111111..2222222 100644
--- a/src/app.py
+++ b/src/app.py
@@ -1,3 +1,4 @@
 def main():
+    print("fixed")
     return 0
"""


def _patch_for(path: str, added_line: str = "    pass", mode_line: str = "") -> str:
    """Build a minimal one-file patch touching ``path``."""
    mode = f"{mode_line}\n" if mode_line else ""
    return (
        f"diff --git a/{path} b/{path}\n"
        f"{mode}"
        f"--- a/{path}\n"
        f"+++ b/{path}\n"
        "@@ -0,0 +1 @@\n"
        f"+{added_line}\n"
    )


def test_clean_patch_passes():
    """A normal source-file patch is accepted."""
    result = validate_patch(_CLEAN_PATCH)
    assert result.ok is True
    assert result.changed_files == ["src/app.py"]
    assert result.requires_human_release is False
    assert result.reason == "ok"


def test_empty_patch_is_rejected():
    """An empty patch is a failure, not a silent no-op publish."""
    result = validate_patch("   \n  ")
    assert result.ok is False
    assert "empty" in result.reason


def test_unparseable_patch_is_rejected():
    """Content without diff headers never reaches git."""
    result = validate_patch("just some prose the agent emitted\n")
    assert result.ok is False
    assert "no recognizable file headers" in result.reason


@pytest.mark.parametrize(
    "path",
    [
        ".github/workflows/ci.yml",
        ".github/workflows/deploy.yaml",
        ".github/CODEOWNERS",
        ".github/dependabot.yml",
    ],
)
def test_github_directory_requires_human_release(path):
    """Workflow/CI config changes are blocked, and marked as the releasable block.

    Pushing a branch with a modified workflow can execute it with repository
    secrets on the push event — before anyone reads the draft PR.
    """
    result = validate_patch(_patch_for(path))
    assert result.ok is False
    assert result.requires_human_release is True
    assert "CI/workflow configuration" in result.reason
    assert result.violations == [result.releasable_violation]


def test_ordinary_dotfiles_are_not_blocked():
    """The block is scoped to .github/, not every dotfile."""
    assert validate_patch(_patch_for(".gitignore")).ok is True
    assert validate_patch(_patch_for("docs/.vale.ini")).ok is True


@pytest.mark.parametrize(
    "path",
    ["../outside.txt", "/etc/passwd", "src/../../escape.py", "~/.ssh/authorized_keys"],
)
def test_path_escape_is_rejected(path):
    """Paths that leave the repository root are refused."""
    result = validate_patch(_patch_for(path))
    assert result.ok is False
    assert "outside the repository" in result.reason


def test_symlink_creation_is_rejected():
    """A new symlink (mode 120000) can point at anything; refuse it."""
    result = validate_patch(
        _patch_for("link", added_line="/etc/passwd", mode_line="new mode 120000")
    )
    assert result.ok is False
    assert "symlinks" in result.reason


_GITLINK = "Subproject commit deadbeefdeadbeefdeadbeefdeadbeefdeadbeef"


@pytest.mark.parametrize(
    "mode_lines",
    [
        "new file mode 160000\nindex 0000000..deadbee",
        # Moving an existing submodule names its mode only on the index line.
        "index 1111111..deadbee 160000",
    ],
)
def test_submodule_entry_is_rejected(mode_lines):
    """A gitlink pulls in another repository's code, which no gate ever reads."""
    patch = (
        "diff --git a/vendor/lib b/vendor/lib\n"
        f"{mode_lines}\n"
        "--- a/vendor/lib\n+++ b/vendor/lib\n@@ -0,0 +1 @@\n"
        f"+{_GITLINK}\n"
    )

    result = validate_patch(patch)

    assert result.ok is False
    assert "submodule entries: vendor/lib" in result.reason
    assert not result.requires_human_release


def test_a_regular_file_index_line_is_not_a_submodule():
    """Only the gitlink mode trips the check, not every index line."""
    assert validate_patch(_CLEAN_PATCH).ok is True


def test_the_change_set_gate_refuses_staged_gitlinks():
    """The post-apply pass reads modes from the index; a gitlink is refused there too."""
    result = validate_change_set([".gitmodules", "vendor/lib"], [], ["vendor/lib"])

    assert result.ok is False
    assert result.violations == ["patch introduces submodule entries: vendor/lib"]


@pytest.mark.parametrize(
    ("label", "secret"),
    [
        ("github_token", "ghp_" + "a" * 36),
        ("openai_key", "sk-" + "b" * 32),
        ("openai_project_key", "sk-proj-" + "c" * 40),
        ("openrouter_key", "sk-or-v1-" + "d" * 32),
        ("anthropic_key", "sk-ant-" + "c" * 32),
        ("gateway_key", "hyi-" + "d" * 32),
        ("agent_worker_token", "ajt.abcdefghijkl.mnopqrstuvwx"),
        ("aws_access_key", "AKIAIOSFODNN7EXAMPLE"),
        ("slack_token", "xoxb-123456789012-abcdefghijkl"),
        ("private_key_block", "-----BEGIN RSA PRIVATE KEY-----"),
    ],
)
def test_added_secrets_are_detected(label, secret):
    """Credential-shaped content in added lines blocks publication."""
    result = validate_patch(_patch_for("src/config.py", added_line=f'TOKEN = "{secret}"'))
    assert result.ok is False
    assert label in result.reason


def test_preexisting_secret_in_context_does_not_block():
    """A secret on a context/removed line is not this patch's doing."""
    patch = (
        "diff --git a/src/config.py b/src/config.py\n"
        "--- a/src/config.py\n"
        "+++ b/src/config.py\n"
        "@@ -1,2 +1,2 @@\n"
        f'-TOKEN = "ghp_{"a" * 36}"\n'
        '+TOKEN = os.environ["TOKEN"]\n'
    )
    result = validate_patch(patch)
    assert result.ok is True


def test_secret_lookalike_prose_does_not_trip_the_scanner():
    """Ordinary words near 'key' or 'token' must not false-positive."""
    patch = _patch_for("docs/readme.md", added_line="Set your API key via the SK-prefixed env var.")
    assert validate_patch(patch).ok is True


@pytest.mark.parametrize(
    "secret",
    ["sk-proj-" + "c" * 39, "sk-or-v1-" + "d" * 31],
)
def test_project_key_lookalikes_below_catalog_minimum_remain_allowed(secret):
    patch = _patch_for("docs/readme.md", added_line=f"Example prefix: {secret}")

    assert validate_patch(patch).ok is True
    assert scan_blob_secrets(secret.encode()) == []


@pytest.mark.parametrize(
    ("secret", "pattern_name"),
    [
        (b"sk-proj-" + b"c" * 40, "openai_project_key"),
        (b"sk-or-v1-" + b"d" * 32, "openrouter_key"),
    ],
)
def test_staged_blob_scanner_uses_project_key_patterns(secret, pattern_name):
    assert scan_blob_secrets(b"\x00" + secret + b"\xff") == [pattern_name]


@pytest.mark.parametrize("separator", [b"_", b"-"])
def test_staged_blob_scanner_detects_url_safe_openai_project_key_characters(separator):
    secret = b"sk-proj-" + b"a" * 20 + separator + b"b" * 20

    assert scan_blob_secrets(secret) == ["openai_project_key"]


def test_size_limit_is_enforced():
    """An oversized patch is refused rather than pushed."""
    big = _CLEAN_PATCH + ("+" + "x" * 200 + "\n") * 100
    result = validate_patch(big, max_bytes=1000)
    assert result.ok is False
    assert "byte limit" in result.reason


def test_file_count_limit_is_enforced():
    """A patch touching too many files is refused."""
    patch = "".join(_patch_for(f"src/file_{index}.py") for index in range(5))
    result = validate_patch(patch, max_files=3)
    assert result.ok is False
    assert "over the 3 limit" in result.reason
    assert len(result.changed_files) == 5


def test_default_file_limit_is_generous_enough_for_real_work():
    """A realistic multi-file change is not blocked by the default cap."""
    patch = "".join(_patch_for(f"src/file_{index}.py") for index in range(50))
    assert MAX_CHANGED_FILES >= 50
    assert validate_patch(patch).ok is True


# ── The shipped limits ─────────────────────────────────────────────────
#
# The two tests above hand `validate_patch` a limit of their own — 3 files,
# 1000 bytes — which verifies the mechanism and says nothing about the number
# production runs with. Mutation testing found the gap: raising
# MAX_CHANGED_FILES from 200 to 500, or MAX_PATCH_BYTES from 1MB to 100MB,
# left the whole suite green, because the only assertion about either constant
# was a one-sided `>= 50`.
#
# One-sided is the wrong side. This gate is the containment boundary of the
# patch-out model, and a cap only ever weakens by growing. So both bounds are
# pinned to the shipped value: raising a limit should require editing the test
# that states it, which is the point at which somebody has to say why.


def test_the_shipped_file_limit_is_the_one_that_was_agreed():
    """MAX_CHANGED_FILES is pinned, not merely bounded from below."""
    assert MAX_CHANGED_FILES == 200


def test_the_shipped_byte_limit_is_the_one_that_was_agreed():
    """MAX_PATCH_BYTES is pinned, not merely bounded from below."""
    assert MAX_PATCH_BYTES == 1_000_000


# ── Where the limits actually bite ─────────────────────────────────────
#
# `>` versus `>=` is invisible to every test that clears the limit by a
# margin: the file-count case above sends 5 files at a cap of 3, so an
# off-by-one in the comparison changes nothing it asserts. These sit the patch
# exactly on the boundary and one step past it, which is the only arrangement
# that can tell those two operators apart.


def test_a_patch_exactly_at_the_file_limit_is_allowed():
    """The cap is the largest accepted value, not the smallest rejected one."""
    patch = "".join(_patch_for(f"src/file_{index}.py") for index in range(3))
    result = validate_patch(patch, max_files=3)
    assert result.ok is True
    assert len(result.changed_files) == 3


def test_one_file_over_the_limit_is_refused():
    """Exceeding the cap by the smallest possible amount still refuses."""
    patch = "".join(_patch_for(f"src/file_{index}.py") for index in range(4))
    result = validate_patch(patch, max_files=3)
    assert result.ok is False
    assert "over the 3 limit" in result.reason


def test_a_patch_exactly_at_the_byte_limit_is_allowed():
    """Same boundary, measured in bytes rather than files."""
    patch = _patch_for("src/app.py")
    size = len(patch.encode("utf-8"))
    assert validate_patch(patch, max_bytes=size).ok is True


def test_one_byte_over_the_limit_is_refused():
    """A patch one byte past the cap is refused, not rounded down to it."""
    patch = _patch_for("src/app.py")
    size = len(patch.encode("utf-8"))
    result = validate_patch(patch, max_bytes=size - 1)
    assert result.ok is False
    assert f"over the {size - 1} byte limit" in result.reason


def test_renames_are_tracked():
    """Rename targets count as changed files (format-patch preserves them)."""
    patch = (
        "diff --git a/old.py b/new.py\n"
        "similarity index 100%\n"
        "rename from old.py\n"
        "rename to new.py\n"
    )
    result = validate_patch(patch)
    assert "new.py" in result.changed_files
    assert result.ok is True


def test_rename_into_github_is_blocked():
    """Renaming a file *into* .github/ is caught like a direct edit."""
    patch = (
        "diff --git a/script.yml b/script.yml\n"
        "similarity index 100%\n"
        "rename from script.yml\n"
        "rename to .github/workflows/evil.yml\n"
    )
    result = validate_patch(patch)
    assert result.ok is False
    assert result.requires_human_release is True


@pytest.mark.parametrize(
    ("target", "violation"),
    [
        ("../outside.txt", "writes outside the repository"),
        ("ghp_0123456789abcdefghij.txt", "credential-shaped paths"),
        (".github/workflows/evil.yml", "CI/workflow configuration"),
    ],
)
def test_a_rename_new_target_is_gated_like_rename_to(target, violation):
    """`git apply` reads `rename new` as `rename to`, so its target is gated too.

    Here only that line names the target. The parser read `rename to` alone,
    so the target passed until the re-gate after apply. Confirmed against git
    2.43: it applies the credential-shaped and `.github/` renames, and refuses
    the escaping one itself.
    """
    patch = (
        "diff --git a/src.txt b/src.txt\n"
        "similarity index 100%\n"
        "rename old src.txt\n"
        f"rename new {target}\n"
    )

    result = validate_patch(patch)

    assert result.ok is False
    assert target in result.changed_files
    assert violation in result.reason


@pytest.mark.parametrize(("source", "target"), [("from", "to"), ("old", "new")])
def test_a_rename_away_from_github_is_blocked(source, target):
    """Renaming a workflow away deletes it, whichever spelling names the source.

    Here only the rename lines name it. The parser took sources from the
    `diff --git` line alone, so the deletion passed until the re-gate after
    apply. Confirmed against git 2.43, which applies both spellings.
    """
    patch = (
        "diff --git a/notes.txt b/notes.txt\n"
        "similarity index 100%\n"
        f"rename {source} .github/workflows/ci.yml\n"
        f"rename {target} notes.txt\n"
    )

    result = validate_patch(patch)

    assert result.ok is False
    assert ".github/workflows/ci.yml" in result.changed_files
    assert result.requires_human_release is True


def test_multiple_violations_are_all_reported():
    """The gate reports every problem, not just the first."""
    patch = _patch_for(".github/workflows/ci.yml", added_line=f'token: "ghp_{"a" * 36}"')
    result = validate_patch(patch)
    assert result.ok is False
    assert "CI/workflow configuration" in result.reason
    assert "github_token" in result.reason


def test_a_release_overrides_the_workflow_block_and_nothing_that_mentions_it():
    """The override was a substring match on "human release", in any violation.

    A symlink (or path-escape) violation naming a file with those words was
    dropped along with the workflow block, and the symlink was published.
    """
    from cloud_agent.publisher import _gate_permits

    workflow_only = validate_change_set([".github/workflows/ci.yml"])
    assert _gate_permits(workflow_only, allow_workflow_changes=True) is True
    assert _gate_permits(workflow_only, allow_workflow_changes=False) is False

    named = "docs/human release.md"
    both = validate_change_set([".github/workflows/ci.yml", named], [named])
    assert both.requires_human_release is True
    assert _gate_permits(both, allow_workflow_changes=True) is False


def test_branch_name_is_namespaced_and_sanitized():
    """Publication targets exactly one predictable branch per job."""
    assert branch_name_for("ajob_abc123") == "agent/ajob_abc123"


@pytest.mark.parametrize(
    "job_id",
    ["ajob_abc123", "weird/../id", "trailing.", "..", "with spaces", "-leading", ""],
)
def test_branch_names_are_always_valid_git_refs(job_id):
    """No job id can produce a ref name git would reject.

    git refuses refs containing '..', ending in '.', or with empty components,
    so the sanitizer must never emit one — otherwise publication fails at the
    push with a confusing error instead of being caught here.
    """
    name = branch_name_for(job_id)
    assert name.startswith("agent/")
    suffix = name[len("agent/") :]
    assert suffix
    assert ".." not in name
    assert not name.endswith(".")
    assert " " not in name


def test_credential_in_a_filename_is_caught():
    """A secret can hide in a path, which is never an added-content line.

    Regression: the scan only looked at `+` lines, so a patch adding a file
    *named* after a token got the credential pushed as part of the tree.
    """
    patch = (
        "diff --git a/ghp_0123456789abcdefghij.txt b/ghp_0123456789abcdefghij.txt\n"
        "new file mode 100644\n"
        "--- /dev/null\n"
        "+++ b/ghp_0123456789abcdefghij.txt\n"
        "@@ -0,0 +1 @@\n"
        "+harmless content\n"
    )
    result = validate_patch(patch)
    assert not result.ok
    assert any("credential-shaped" in violation for violation in result.violations)


# ── the parser is not the boundary ─────────────────────────────────────
#
# git quotes any path containing non-ASCII bytes, control characters, quotes or
# backslashes. A gate that matches the raw header text sees a path starting
# with a quote — matching no blocked prefix — while `git apply` decodes it and
# writes the real file. Confirmed against real git before these were written.


def test_a_quoted_workflow_path_is_seen_through():
    """`.github/` hidden behind C-quoting must still hit the block.

    The bypass needed one benign file alongside it: with nothing parseable the
    patch was rejected as unrecognizable, so a lone quoted entry looked safe.
    Mixed with a normal file it sailed through with ok=True.
    """
    patch = (
        "diff --git a/README.md b/README.md\n"
        "--- a/README.md\n+++ b/README.md\n@@ -1 +1,2 @@\n hello\n+world\n"
        'diff --git "a/\\056github/workflows/evil.yml" "b/\\056github/workflows/evil.yml"\n'
        "new file mode 100644\n"
        '--- /dev/null\n+++ "b/\\056github/workflows/evil.yml"\n@@ -0,0 +1 @@\n+on: push\n'
    )

    result = validate_patch(patch)

    assert not result.ok
    assert ".github/workflows/evil.yml" in result.changed_files
    assert result.requires_human_release


def test_a_headerless_hunk_still_counts():
    """A patch with no `diff --git` line at all applies fine and must be gated."""
    patch = "--- /dev/null\n+++ b/.github/workflows/evil.yml\n@@ -0,0 +1 @@\n+on: push\n"

    result = validate_patch(patch)

    assert not result.ok
    assert result.changed_files == [".github/workflows/evil.yml"]


def test_a_symlink_is_blamed_on_the_file_that_introduced_it():
    """A mode line must not be attributed to the previous file.

    The old parser kept the last header's paths as `pending`, so a mode line
    belonging to an entry it failed to parse was reported against whichever
    file came before — a false positive on an innocent path, and the real one
    unnamed.
    """
    patch = (
        "diff --git a/README.md b/README.md\n"
        "--- a/README.md\n+++ b/README.md\n@@ -1 +1,2 @@\n hello\n+world\n"
        'diff --git "a/w\\303\\251ird" "b/w\\303\\251ird"\n'
        "new file mode 120000\n"
        '--- /dev/null\n+++ "b/w\\303\\251ird"\n@@ -0,0 +1 @@\n+/etc/passwd\n'
    )

    result = validate_patch(patch)

    assert not result.ok
    assert "wéird" in result.reason
    assert "README.md" not in result.reason


def test_an_ordinary_patch_is_unaffected():
    """The unquoting must not change the common case."""
    patch = (
        "diff --git a/src/app.py b/src/app.py\n"
        "--- a/src/app.py\n+++ b/src/app.py\n@@ -1 +1,2 @@\n x\n+y\n"
    )

    result = validate_patch(patch)

    assert result.ok
    assert result.changed_files == ["src/app.py"]


def _binary_patch_block(kind: str, payload: bytes, declared_size: int | None = None) -> str:
    compressed = zlib.compress(payload)
    lines: list[str] = []
    for offset in range(0, len(compressed), 52):
        chunk = compressed[offset : offset + 52]
        size = len(chunk)
        prefix = chr(ord("A") + size - 1) if size <= 26 else chr(ord("a") + size - 27)
        padded = chunk + b"\0" * (-size % 4)
        lines.append(prefix + base64.b85encode(padded).decode())
    declared = len(payload) if declared_size is None else declared_size
    return f"{kind} {declared}\n" + "\n".join(lines) + "\n\n"


def _binary_patch(*blocks: str) -> str:
    return "diff --git a/asset.bin b/asset.bin\nGIT binary patch\n" + "".join(blocks)


def test_decoded_blob_limits_are_pinned():
    assert MAX_BLOB_BYTES == 10_000_000
    assert MAX_CHANGED_BLOB_BYTES == 20_000_000


def test_binary_literal_decoded_limit_boundary():
    patch = _binary_patch(_binary_patch_block("literal", b"\0" * 32))
    assert validate_patch(patch, max_blob_bytes=32).ok
    result = validate_patch(patch, max_blob_bytes=31)
    assert not result.ok
    assert "decoded blob byte limit" in result.reason


def test_binary_declared_size_cannot_hide_larger_decoded_payload():
    patch = _binary_patch(_binary_patch_block("literal", b"\0" * 33, declared_size=16))
    result = validate_patch(patch, max_blob_bytes=32)
    assert not result.ok
    assert "decoded blob byte limit" in result.reason


@pytest.mark.parametrize("sizes", [b"\x01\x21", b"\x21\x01"])
def test_binary_delta_source_and_result_sizes_are_bounded(sizes):
    """A tiny delta can name a much larger source or result object."""
    patch = _binary_patch(_binary_patch_block("delta", sizes + b"\x01x"))
    result = validate_patch(patch, max_blob_bytes=32)
    assert not result.ok
    assert "decoded blob byte limit" in result.reason


def test_binary_delta_size_header_uses_multibyte_varints():
    # 129 is encoded as 0x81, 0x01, not as either individual byte's value.
    patch = _binary_patch(_binary_patch_block("delta", b"\x01\x81\x01\x01x"))
    result = validate_patch(patch, max_blob_bytes=128)
    assert not result.ok
    assert "decoded blob byte limit" in result.reason


def test_binary_reverse_block_is_also_bounded():
    patch = _binary_patch(
        _binary_patch_block("literal", b"\0"),
        _binary_patch_block("delta", b"\x01\x21\x01x"),
    )
    assert not validate_patch(patch, max_blob_bytes=32).ok


@pytest.mark.parametrize("header", ["literal 100000000 extra", "literal +100000000"])
def test_loose_git_reverse_header_cannot_hide_expansion(header):
    patch = _binary_patch(_binary_patch_block("literal", b"\0")) + header + "\n\n"
    result = validate_patch(patch)
    assert not result.ok
    assert "malformed binary data" in result.reason


def test_total_binary_expansion_limit():
    patch = _binary_patch(
        _binary_patch_block("literal", b"\0" * 16),
        _binary_patch_block("literal", b"\0" * 16),
    )
    assert validate_patch(patch, max_decoded_bytes=32).ok
    result = validate_patch(patch, max_decoded_bytes=31)
    assert not result.ok
    assert "total decoded byte limit" in result.reason


@pytest.mark.parametrize(
    "block",
    [
        "literal 1\n!invalid\n\n",
        _binary_patch_block("literal", b"\0", declared_size=2),
        _binary_patch_block("delta", b"\x80"),
        _binary_patch_block("delta", b"\x80" * 20),
        "",
    ],
)
def test_malformed_binary_data_is_refused(block):
    result = validate_patch(_binary_patch(block))
    assert not result.ok
    assert "malformed binary data" in result.reason


def test_non_utf8_blob_secrets_compare_exact_matches_and_counts():
    credential = ("gh" + "p_" + "a" * 36).encode()
    baseline = b"\xff" + credential + b"\0"
    assert scan_blob_secrets(baseline) == ["github_token"]
    assert scan_blob_secrets(baseline + b"safe", baseline) == []
    assert scan_blob_secrets(b"\0safe", baseline) == []
    assert scan_blob_secrets(baseline + credential, baseline) == ["github_token"]
    assert scan_blob_secrets(b"\0" + credential.replace(b"a", b"b"), baseline) == ["github_token"]


@pytest.mark.parametrize(
    "path", ["a\nb.txt", "a\rb.txt", "a\tb.txt", "a\x1bb.txt", "a\x7fb.txt", "a\x85b.txt"]
)
def test_a_staged_path_with_a_control_character_is_refused(path):
    """Git stores any byte but NUL in a name; the publisher renders names into the PR."""
    credential = "gh" + "p_" + "a" * 36
    result = validate_change_set(["ok.txt", path, f"{credential}\n.txt"])
    assert not result.ok
    assert "2 path(s) containing control characters" in result.reason
    # Counted, not echoed: a refused name may be exactly what cannot be shown.
    assert path not in result.reason
    assert credential not in result.reason


def test_a_quoted_control_character_is_seen_before_apply():
    """Every single-letter escape git writes decodes to its control character."""
    for escape in "abfnrtv":
        quoted = f'"a/x\\{escape}y"'
        patch = (
            f"diff --git {quoted} {quoted.replace('a/', 'b/')}\n"
            f"--- {quoted}\n+++ {quoted.replace('a/', 'b/')}\n@@ -0,0 +1 @@\n+z\n"
        )
        result = validate_patch(patch)
        assert not result.ok, escape
        assert "control characters" in result.reason


def test_ordinary_unicode_paths_are_not_control_characters():
    assert validate_change_set(["docs/über.md", "docs/文件.md", "docs/a b.md"]).ok


def test_actual_staged_paths_are_also_scanned_for_credentials():
    credential = "gh" + "p_" + "a" * 36
    result = validate_change_set([f"{credential}.bin"])
    assert not result.ok
    assert "github_token" in result.reason
    assert credential not in result.reason


# ── hunk bodies are content ────────────────────────────────────────────
#
# A hunk header declares how many old and new lines follow it, and `git apply`
# reads exactly that many as content. Removing the SQL, Lua or Haskell comment
# `-- note` puts `--- note` in the patch: a removed line, not a file name.


def test_a_removed_comment_is_not_read_as_a_file_name():
    """Deleting a Lua comment that names a home-directory path is an ordinary edit."""
    patch = (
        "diff --git a/init.lua b/init.lua\n"
        "--- a/init.lua\n+++ b/init.lua\n@@ -1,2 +1 @@\n"
        "--- ~/.config/nvim/init.lua\n vim.opt.number = true\n"
    )

    result = validate_patch(patch)

    assert result.ok, result.reason
    assert result.changed_files == ["init.lua"]


def test_removed_comments_do_not_count_toward_the_file_limit():
    """A pg_dump has a `-- Name:` comment per object; deleting one is one file."""
    count = MAX_CHANGED_FILES + 50
    comments = "".join(
        f"--- Name: table_{index}; Type: TABLE; Schema: public; Owner: app\n"
        for index in range(count)
    )
    patch = (
        "diff --git a/schema.sql b/schema.sql\ndeleted file mode 100644\n"
        f"--- a/schema.sql\n+++ /dev/null\n@@ -1,{count} +0,0 @@\n{comments}"
    )

    result = validate_patch(patch)

    assert result.ok, result.reason
    assert result.changed_files == ["schema.sql"]


def test_a_removed_comment_quoting_a_key_is_not_a_credential_path():
    """Removing a comment that quotes AWS's documented example key adds no credential."""
    patch = (
        "diff --git a/setup.sql b/setup.sql\n"
        "--- a/setup.sql\n+++ b/setup.sql\n@@ -1,2 +1 @@\n"
        "--- aws_access_key_id = AKIAIOSFODNN7EXAMPLE\n SELECT 1;\n"
    )

    result = validate_patch(patch)

    assert result.ok, result.reason
    assert result.changed_files == ["setup.sql"]


def test_a_no_newline_marker_counts_toward_neither_side():
    """`git diff` writes the marker mid-hunk when the old last line had no newline."""
    patch = (
        "diff --git a/notes.txt b/notes.txt\n"
        "--- a/notes.txt\n+++ b/notes.txt\n@@ -1,2 +1,2 @@\n"
        "--- ~/a\n-x\n\\ No newline at end of file\n+x\n+++ ~/b\n"
    )

    result = validate_patch(patch)

    assert result.ok, result.reason
    assert result.changed_files == ["notes.txt"]


def test_a_binary_patch_after_a_hunk_is_still_read():
    """A hunk ends at its counts, so the binary entry after it keeps its gates."""
    patch = (
        "diff --git a/schema.sql b/schema.sql\n"
        "--- a/schema.sql\n+++ b/schema.sql\n@@ -1,2 +1 @@\n--- old note\n SELECT 1;\n"
    ) + _binary_patch(_binary_patch_block("literal", b"\x89PNG"))

    result = validate_patch(patch)
    oversized = validate_patch(patch, max_blob_bytes=3)

    assert result.ok, result.reason
    assert result.changed_files == ["asset.bin", "schema.sql"]
    assert not oversized.ok
    assert "3 decoded blob byte limit" in oversized.reason


def test_a_hunk_that_claims_more_lines_than_it_has_hides_no_header():
    """Git refuses a hunk whose lines run out first; its lines are still read as headers.

    Counting alone would take the next file's traditional headers for the rest
    of this hunk's body.
    """
    patch = (
        "--- a/README.md\n+++ b/README.md\n@@ -1,3 +1,3 @@\n-hello\n+world\n"
        "--- /dev/null\n+++ b/.github/workflows/evil.yml\n@@ -0,0 +1 @@\n+on: push\n"
    )

    result = validate_patch(patch)

    assert not result.ok
    assert ".github/workflows/evil.yml" in result.changed_files
    assert result.requires_human_release


def test_a_hunk_count_of_thousands_of_digits_is_not_a_crash():
    """`int` refuses a string of over 4300 digits; the gate must still answer."""
    patch = (
        f"diff --git a/a.txt b/a.txt\n--- a/a.txt\n+++ b/a.txt\n@@ -1,{'9' * 5000} +1 @@\n-x\n+y\n"
    )

    result = validate_patch(patch)

    assert result.changed_files == ["a.txt"]


def test_a_form_feed_does_not_end_a_hunk_line():
    """Git splits lines only at newlines, so the count must too.

    `str.splitlines` also splits at a form feed. Counted that way, the removed
    line below ends its hunk early and starts a second one, whose counts then
    cover the real headers that follow. Git applies both files.
    """
    patch = (
        "diff --git a/README.md b/README.md\n"
        "--- a/README.md\n+++ b/README.md\n@@ -1 +1 @@\n"
        "-hello\x0c@@ -1,1 +1,2 @@\n+world\n"
        "--- /dev/null\n+++ b/.github/workflows/evil.yml\n@@ -0,0 +1 @@\n+on: push\n"
    )

    result = validate_patch(patch)

    assert not result.ok
    assert ".github/workflows/evil.yml" in result.changed_files


def test_an_added_line_that_starts_with_plus_signs_is_still_scanned():
    """A line a hunk adds is content, whatever it starts with."""
    credential = "gh" + "p_" + "a" * 36

    result = validate_patch(_patch_for("notes.txt", added_line=f"++ {credential}"))

    assert not result.ok
    assert "credential-shaped content: github_token" in result.reason
