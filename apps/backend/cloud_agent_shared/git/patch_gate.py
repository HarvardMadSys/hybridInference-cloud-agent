"""Validation gate for agent-produced patches.

The trusted publisher validates the agent's patch before applying it, pushing
an ``agent/<thread-id>`` branch and opening or updating a draft PR. This module
is the security boundary for that publication path. A sandbox's separate,
repository-scoped GitHub token also permits direct pushes and pull requests;
those writes do not pass through this gate.

Four gates, in the order they matter:

1. **Workflow files** (``.github/``). Pushing a branch that modifies a
   workflow can make GitHub execute it *with repository secrets* on the push
   event — before any human reads the draft PR. Draft-PR review is therefore
   not a mitigation for this class, so such patches are not published. The
   result marks the block as the one a caller may override, but the platform
   offers no release, and its publishing token lacks the ``workflows``
   permission in any case.
2. **Path escape.** ``..`` segments, absolute paths, and symlink-mode entries
   let a patch write outside the worktree or turn a file into a link to one.
   Gitlink (submodule) entries are refused with them: they pull in code from
   another repository that never appears in the diff, so none of these gates
   would ever read it. Names carrying control characters are refused too: the
   publisher renders changed names into the pull request body.
3. **Secret scan.** An agent can inadvertently paste credentials it saw (env
   dumps, logs, a leaked token) into the diff; a match blocks publication.
4. **Size limits.** A patch larger than the cap, or touching more files than
   the cap, is refused rather than pushed — huge diffs are almost always a
   runaway agent, and they make review impossible anyway.

The gate checks ``git format-patch`` / ``git diff`` output before it touches a
worktree, including bounded decoding of binary patch payloads. The publisher
also checks actual staged blob contents before any commit or push.
"""

from __future__ import annotations

import base64
import re
import unicodedata
import zlib
from collections import Counter
from dataclasses import dataclass, field

# ── Limits ─────────────────────────────────────────────────────────────

MAX_PATCH_BYTES = 1_000_000
MAX_CHANGED_FILES = 200
# Encoded binary diffs can be tiny while expanding into very large objects.
# Bound both the pre-apply expansion and the decoded blobs scanned by the
# publisher; legitimate images and other small binary assets remain supported.
MAX_BLOB_BYTES = 10_000_000
MAX_CHANGED_BLOB_BYTES = 20_000_000

# ── Blocked paths ──────────────────────────────────────────────────────

# `.github/` in full: workflows are the proven push-trigger escalation, and
# the rest of the directory (actions, CODEOWNERS, dependabot config) shapes CI
# and review authority, so an agent has no business editing any of it
# unattended.
_BLOCKED_PREFIXES = (".github/",)

# ── Secret patterns ────────────────────────────────────────────────────

# Deliberately narrow: each pattern targets a credential shape with a
# recognizable prefix or structure, so ordinary code and prose do not trip it.
# A false negative is caught by the human reviewing the draft PR; a false
# positive blocks a legitimate job, so precision wins here.
SECRET_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("github_token", re.compile(r"\bgh[pousr]_[A-Za-z0-9]{16,}")),
    ("openai_key", re.compile(r"\bsk-[A-Za-z0-9]{20,}")),
    ("openai_project_key", re.compile(r"\bsk-proj-[A-Za-z0-9_-]{40,}")),
    ("openrouter_key", re.compile(r"\bsk-or-v1-[A-Za-z0-9]{32,}")),
    ("anthropic_key", re.compile(r"\bsk-ant-[A-Za-z0-9\-_]{20,}")),
    ("gateway_key", re.compile(r"\bhyi-[A-Za-z0-9\-_]{20,}")),
    ("agent_worker_token", re.compile(r"\bajt\.[A-Za-z0-9\-_]{10,}\.[A-Za-z0-9\-_]{10,}")),
    ("aws_access_key", re.compile(r"\bAKIA[0-9A-Z]{16}\b")),
    ("slack_token", re.compile(r"\bxox[abprs]-[A-Za-z0-9\-]{10,}")),
    ("google_api_key", re.compile(r"\bAIza[0-9A-Za-z\-_]{35}\b")),
    ("private_key_block", re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH |PGP )?PRIVATE KEY-----")),
)
_BLOB_SECRET_PATTERNS = tuple(
    (name, re.compile(pattern.pattern.encode("ascii"))) for name, pattern in SECRET_PATTERNS
)

# ── Diff parsing ───────────────────────────────────────────────────────

_DIFF_HEADER = re.compile(r"^diff --git (?P<a>.+?) (?P<b>.+)$")
_RENAME_TO = re.compile(r"^rename to (?P<path>.+)$")
_COPY_TO = re.compile(r"^copy to (?P<path>.+)$")
_NEW_MODE = re.compile(r"^(?:new|new file) mode (?P<mode>\d{6})$")
# A modified entry names its mode only here, after the two object ids.
_INDEX_MODE = re.compile(r"^index [0-9a-fA-F]+\.\.[0-9a-fA-F]+ (?P<mode>\d{6})$")
# Traditional hunk name lines. git omits the `diff --git` header in some diff
# formats, and a patch that carries only these still applies perfectly well —
# so a parser that keys solely off the header sees an empty change set. They
# are read only outside hunk bodies; see `_split_hunks`.
_NAME_LINE = re.compile(r"^(?:---|\+\+\+) (?P<path>.+?)(?:\t.*)?$")
# `@@ -<start>[,<count>] +<start>[,<count>] @@`, as `git apply` reads it. An
# omitted count means one line. A count of ten digits or more is no hunk a
# patch under the byte limit can hold, and `int` refuses one of thousands, so
# such a header is not counted and the lines after it are read as headers.
_HUNK_HEADER = re.compile(
    r"^@@ -[0-9]+(?:,(?P<old>[0-9]{1,9}))? \+[0-9]+(?:,(?P<new>[0-9]{1,9}))? @@"
)
# How much of a hunk's old and new counts one body line uses, by its first
# character. Git reads an empty line as empty context, and the
# `\ No newline at end of file` marker as neither side.
_BODY_LINE_COUNTS = {" ": (1, 1), "": (1, 1), "-": (1, 0), "+": (0, 1), "\\": (0, 0)}
_SYMLINK_MODE = "120000"
_GITLINK_MODE = "160000"
_BINARY_BLOCK = re.compile(r"^(literal|delta) ([0-9]+)$")


def scan_blob_secrets(content: bytes, baseline: bytes = b"") -> list[str]:
    """Find newly introduced credential matches in actual Git blob bytes.

    Byte patterns also inspect non-UTF-8 files. Compare exact matches and
    occurrence counts to the same file's base, so retaining an existing token
    is allowed while introducing another token or duplicating one is blocked.
    Return pattern names only: credential values must never reach job details.
    """
    violations: list[str] = []
    for name, pattern in _BLOB_SECRET_PATTERNS:
        added = Counter(match.group() for match in pattern.finditer(content))
        if added and added - Counter(match.group() for match in pattern.finditer(baseline)):
            violations.append(name)
    return violations


def _delta_sizes(payload: bytes) -> tuple[int, int]:
    """Read the source and result lengths from Git's delta instruction header."""
    index = 0
    sizes: list[int] = []
    for _ in range(2):
        size = 0
        for shift in range(0, 70, 7):
            if index >= len(payload):
                raise ValueError("truncated binary delta header")
            value = payload[index]
            index += 1
            size |= (value & 0x7F) << shift
            if not value & 0x80:
                sizes.append(size)
                break
        else:
            raise ValueError("invalid binary delta size")
    return sizes[0], sizes[1]


def _binary_expansion_violations(
    patch: str, *, max_blob_bytes: int, max_decoded_bytes: int
) -> list[str]:
    """Bound binary literals and delta results before Git allocates them.

    Both forward and reverse blocks are decoded because Git reads both, and a
    delta's declared block length is its instruction size, not its result size.
    The index is independently checked after apply as the authoritative pass.
    """
    lines = patch.splitlines()
    total = 0
    for index, line in enumerate(lines):
        if line != "GIT binary patch":
            continue
        index += 1
        blocks = 0
        try:
            while index < len(lines) and (header := _BINARY_BLOCK.fullmatch(lines[index])):
                kind, declared = header.groups()
                declared_size = int(declared)
                if declared_size > max_blob_bytes:
                    return [f"binary patch exceeds the {max_blob_bytes} decoded blob byte limit"]
                index += 1
                compressed = bytearray()
                while index < len(lines) and lines[index]:
                    encoded = lines[index]
                    prefix = encoded[0]
                    if "A" <= prefix <= "Z":
                        size = ord(prefix) - ord("A") + 1
                    elif "a" <= prefix <= "z":
                        size = ord(prefix) - ord("a") + 27
                    else:
                        raise ValueError("invalid binary patch line length")
                    if len(encoded) != 1 + ((size + 3) // 4) * 5:
                        raise ValueError("invalid binary patch encoding")
                    compressed.extend(base64.b85decode(encoded[1:])[:size])
                    index += 1
                decoder = zlib.decompressobj()
                payload = decoder.decompress(bytes(compressed), max_blob_bytes + 1)
                if len(payload) > max_blob_bytes:
                    return [f"binary patch exceeds the {max_blob_bytes} decoded blob byte limit"]
                if not decoder.eof or decoder.unused_data or len(payload) != declared_size:
                    raise ValueError("invalid binary patch compressed size")
                expanded = len(payload)
                if kind == "delta":
                    source_size, expanded = _delta_sizes(payload)
                    if source_size > max_blob_bytes or expanded > max_blob_bytes:
                        return [
                            f"binary patch exceeds the {max_blob_bytes} decoded blob byte limit"
                        ]
                total += max(expanded, len(payload))
                if total > max_decoded_bytes:
                    return [
                        f"binary patch exceeds the {max_decoded_bytes} total decoded byte limit"
                    ]
                blocks += 1
                index += 1  # blank separator between forward and reverse blocks
            if not blocks:
                raise ValueError("binary patch contains no data blocks")
            if index < len(lines) and lines[index].startswith(("literal ", "delta ")):
                # Git's numeric parser accepts suffixes and signs that the
                # strict header parser above refuses. A reverse block must
                # not disappear from our bounds because of that difference.
                raise ValueError("invalid binary patch block header")
        except (ValueError, zlib.error):
            return ["patch contains malformed binary data"]
    return []


# The single-letter escapes git's `quote_c_style` writes; every other control
# byte arrives as octal. Decoding all of them keeps a path's control characters
# visible to the pre-apply gate rather than turning `\a` into a plain `a`.
_C_ESCAPES = {
    "a": b"\a",
    "b": b"\b",
    "f": b"\f",
    "n": b"\n",
    "r": b"\r",
    "t": b"\t",
    "v": b"\v",
}


def _unquote_path(raw: str) -> str:
    r"""Decode one path as git writes it in a diff header.

    git quotes any path containing non-ASCII bytes, control characters, quotes
    or backslashes, and escapes them C-style — so `.github/x` can legitimately
    arrive as `"\\056github/x"`. A parser that matches the raw text sees a path
    that starts with a quote and matches no blocked prefix, while `git apply`
    decodes it and writes the real file. That difference is the whole bypass.
    """
    raw = raw.strip()
    if not (raw.startswith('"') and raw.endswith('"') and len(raw) >= 2):
        return raw
    body = raw[1:-1]
    out = bytearray()
    index = 0
    while index < len(body):
        char = body[index]
        if char != "\\":
            out.extend(char.encode("utf-8"))
            index += 1
            continue
        index += 1
        if index >= len(body):
            break
        escape = body[index]
        if escape in "01234567":
            octal = body[index : index + 3]
            try:
                out.append(int(octal, 8))
                index += len(octal)
                continue
            except ValueError:
                pass
        out.extend(_C_ESCAPES.get(escape, escape.encode("utf-8")))
        index += 1
    return out.decode("utf-8", errors="replace")


def _strip_prefix(path: str) -> str:
    """Drop git's ``a/``/``b/`` diff prefix, after unquoting."""
    path = _unquote_path(path)
    if path in ("/dev/null", ""):
        return ""
    for prefix in ("a/", "b/"):
        if path.startswith(prefix):
            return path[len(prefix) :]
    return path


@dataclass
class PatchGateResult:
    """Outcome of validating one patch."""

    ok: bool
    violations: list[str] = field(default_factory=list)
    changed_files: list[str] = field(default_factory=list)
    requires_human_release: bool = False
    #: The one violation an explicit release may override, word for word.
    releasable_violation: str | None = None

    @property
    def reason(self) -> str:
        """Single-line summary suitable for a job's ``detail`` field."""
        return "; ".join(self.violations) if self.violations else "ok"


def _split_hunks(patch: str) -> tuple[str, list[str]]:
    r"""Return the patch without its hunk bodies, and the lines those bodies add.

    A hunk header declares how many old and new lines follow it, and ``git
    apply`` reads exactly that many as content before it looks for a header
    again. Removing the comment ``-- note`` puts ``--- note`` in the patch: a
    removed line, not a file name.

    Lines split at ``\n`` alone, as git splits them. ``str.splitlines`` also
    splits at a form feed, and a hunk counted that way can end early and start
    a second one whose counts cover the real headers after it.

    A hunk whose lines run out before its counts do is corrupt, and git refuses
    the patch. Its lines are not trusted as content: they stay in the returned
    text and are read as headers, as every line was before hunks were counted.
    """
    outside: list[str] = []
    added: list[str] = []
    body: list[str] = []
    old = new = 0
    for line in patch.split("\n"):
        if old or new:
            uses = _BODY_LINE_COUNTS.get(line[:1])
            if uses is not None and uses[0] <= old and uses[1] <= new:
                old, new = old - uses[0], new - uses[1]
                body.append(line)
                if not (old or new):
                    added.extend(entry for entry in body if entry.startswith("+"))
                    body = []
                continue
            # The lines ran out before the counts did: read them as headers.
            outside.extend(body)
            body, old, new = [], 0, 0
        header = _HUNK_HEADER.match(line)
        if header:
            old, new = int(header.group("old") or 1), int(header.group("new") or 1)
        outside.append(line)
    outside.extend(body)
    return "\n".join(outside), added


def _collect_paths(patch: str) -> tuple[list[str], list[str], list[str]]:
    """Return (changed paths, paths given a symlink mode, paths given a gitlink mode)."""
    changed: list[str] = []
    symlinks: list[str] = []
    gitlinks: list[str] = []
    pending_paths: list[str] = []

    def remember(path: str) -> None:
        if path and path not in changed:
            changed.append(path)

    headers, _ = _split_hunks(patch)
    for line in headers.splitlines():
        header = _DIFF_HEADER.match(line)
        if header:
            # A new header ends the previous file, so mode lines can no longer
            # be attributed to it. Without this reset a `new file mode 120000`
            # belonging to an unparsed entry was blamed on the *previous* path,
            # reporting a symlink on a file that had none.
            pending_paths = [_strip_prefix(header.group("a")), _strip_prefix(header.group("b"))]
            pending_paths = [p for p in pending_paths if p]
            for path in pending_paths:
                remember(path)
            continue

        name = _NAME_LINE.match(line)
        if name:
            path = _strip_prefix(name.group("path"))
            if path:
                # Only adopt as pending when there was no `diff --git` header
                # for this file; otherwise the header's paths already stand.
                if not pending_paths:
                    pending_paths = [path]
                elif path not in pending_paths:
                    pending_paths.append(path)
                remember(path)
            continue

        for pattern in (_RENAME_TO, _COPY_TO):
            match = pattern.match(line)
            if match:
                path = _strip_prefix(match.group("path"))
                pending_paths.append(path)
                remember(path)
        mode = _NEW_MODE.match(line)
        if mode and mode.group("mode") == _SYMLINK_MODE:
            symlinks.extend(pending_paths)
        mode = mode or _INDEX_MODE.match(line)
        if mode and mode.group("mode") == _GITLINK_MODE:
            gitlinks.extend(pending_paths)

    return changed, symlinks, gitlinks


def _is_escaping(path: str) -> bool:
    """Return whether a path escapes the repository root."""
    if path.startswith("/") or path.startswith("~"):
        return True
    if re.match(r"^[A-Za-z]:[\\/]", path):  # Windows drive-absolute
        return True
    return any(segment == ".." for segment in path.replace("\\", "/").split("/"))


def _has_control_character(path: str) -> bool:
    """Return whether a path carries a control character, such as a newline.

    Git stores any byte but NUL in a path. A newline or carriage return in a
    file name is not something a repository legitimately needs, and it is how
    a name rendered into the pull request body breaks out of its code span.
    """
    return any(unicodedata.category(char) == "Cc" for char in path)


def _scan_secrets(patch: str, paths: list[str]) -> list[str]:
    """Return the names of secret patterns found in added content or new paths.

    Only ``+`` lines of content are scanned: an existing secret already in the
    repository is not this patch's doing, and flagging it would block every
    later job.

    Paths are scanned too, and separately, because a filename is never an
    added-content line — a patch adding ``ghp_<token>.txt`` would otherwise
    sail through the gate and get the credential pushed as part of the tree.

    Every line a hunk adds is scanned whole as well: one can itself begin
    with ``++``, and its hunk's counts, not that prefix, make it content.
    """
    added = "\n".join(
        [
            line[1:]
            for line in patch.splitlines()
            if line.startswith("+") and not line.startswith("+++")
        ]
        + [line[1:] for line in _split_hunks(patch)[1]]
    )
    haystack = added + "\n" + "\n".join(paths)
    return [name for name, pattern in SECRET_PATTERNS if pattern.search(haystack)]


def validate_change_set(
    paths: list[str],
    symlinks: list[str] | None = None,
    gitlinks: list[str] | None = None,
    *,
    max_files: int = MAX_CHANGED_FILES,
) -> PatchGateResult:
    """Apply the path-shaped rules to a set of changed paths.

    Split out from :func:`validate_patch` so the same rules can be re-run
    against the change set **git itself reports after applying** the patch,
    rather than against a parse of the patch text. That second pass is the one
    that cannot be talked around: a hostile patch only has to defeat the
    parser, and quoting alone was enough to do it. Only git knows what actually
    landed on disk.
    """
    violations: list[str] = []
    unique_files = sorted({path for path in paths if path and path != "/dev/null"})

    if len(unique_files) > max_files:
        violations.append(f"patch touches {len(unique_files)} files, over the {max_files} limit")

    blocked = [
        path
        for path in unique_files
        if any(path.startswith(prefix) for prefix in _BLOCKED_PREFIXES)
    ]
    requires_human_release = bool(blocked)
    releasable_violation = None
    if blocked:
        releasable_violation = (
            "patch modifies CI/workflow configuration, which agent publication does "
            f"not push: {', '.join(sorted(blocked))}"
        )
        violations.append(releasable_violation)

    escaping = [path for path in unique_files if _is_escaping(path)]
    if escaping:
        violations.append(f"patch writes outside the repository: {', '.join(sorted(escaping))}")

    # Counted rather than listed: the names are exactly what cannot be shown
    # safely, and one may also carry a credential the scan below withholds.
    controlled = [path for path in unique_files if _has_control_character(path)]
    if controlled:
        violations.append(
            f"patch introduces {len(controlled)} path(s) containing control characters"
        )

    symlink_paths = sorted({path for path in (symlinks or []) if path and path != "/dev/null"})
    if symlink_paths:
        violations.append(f"patch introduces symlinks: {', '.join(symlink_paths)}")

    gitlink_paths = sorted({path for path in (gitlinks or []) if path and path != "/dev/null"})
    if gitlink_paths:
        violations.append(f"patch introduces submodule entries: {', '.join(gitlink_paths)}")

    secrets = _scan_secrets("", unique_files)
    if secrets:
        violations.append(f"patch contains credential-shaped paths: {', '.join(secrets)}")

    return PatchGateResult(
        ok=not violations,
        violations=violations,
        changed_files=unique_files,
        requires_human_release=requires_human_release,
        releasable_violation=releasable_violation,
    )


def validate_patch(
    patch: str,
    *,
    max_bytes: int = MAX_PATCH_BYTES,
    max_files: int = MAX_CHANGED_FILES,
    max_blob_bytes: int = MAX_BLOB_BYTES,
    max_decoded_bytes: int = MAX_CHANGED_BLOB_BYTES,
) -> PatchGateResult:
    """Validate an agent-produced patch before it is allowed near git.

    Returns a :class:`PatchGateResult`; ``requires_human_release`` and
    ``releasable_violation`` mark the workflow-file block, the one violation
    an explicit release could override, as opposed to hard violations like a
    leaked credential. Nothing in the platform releases it.
    """
    violations: list[str] = []
    requires_human_release = False

    if not patch.strip():
        return PatchGateResult(ok=False, violations=["patch is empty"])

    size = len(patch.encode("utf-8"))
    if size > max_bytes:
        violations.append(f"patch is {size} bytes, over the {max_bytes} byte limit")
    else:
        violations.extend(
            _binary_expansion_violations(
                patch, max_blob_bytes=max_blob_bytes, max_decoded_bytes=max_decoded_bytes
            )
        )

    changed, symlinks, gitlinks = _collect_paths(patch)
    unique_files = sorted({path for path in changed if path != "/dev/null"})
    if not unique_files:
        violations.append("patch contains no recognizable file headers")

    path_result = validate_change_set(unique_files, symlinks, gitlinks, max_files=max_files)
    violations.extend(path_result.violations)
    requires_human_release = path_result.requires_human_release

    secrets = _scan_secrets(patch, [])
    if secrets:
        violations.append(f"patch contains credential-shaped content: {', '.join(secrets)}")

    return PatchGateResult(
        ok=not violations,
        violations=violations,
        changed_files=unique_files,
        requires_human_release=requires_human_release,
        releasable_violation=path_result.releasable_violation,
    )


def branch_name_for(job_id: str) -> str:
    """Return the only branch name a job is ever allowed to publish to.

    Dots are dropped rather than kept: git rejects refs containing ``..`` or
    ending in ``.``, so a naive sanitizer that preserves them can emit a name
    git refuses. Job ids are generated (``ajob_<hex>``), so this is defensive
    against a hand-crafted id rather than a routine transformation.
    """
    safe = re.sub(r"[^A-Za-z0-9_-]", "-", job_id).strip("-") or "unnamed"
    return f"agent/{safe}"
