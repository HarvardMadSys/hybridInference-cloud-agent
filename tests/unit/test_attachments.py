"""Per-task uploads: which paths are allowed, and what the agent is told."""

from __future__ import annotations

import io
import tarfile
from typing import Any

import pytest

from cloud_agent.attachments.models import (
    MAX_ATTACHMENT_FILES,
    MAX_ATTACHMENT_PATH_LENGTH,
    MAX_THREAD_ATTACHMENT_BYTES,
    MAX_THREAD_ATTACHMENT_FILES,
    SANDBOX_UPLOAD_DIR,
    AttachmentError,
    check_paths,
    check_thread_total,
    current_uploads,
    safe_attachment_path,
)
from cloud_agent.attachments.selection import (
    check_selection,
    prompt_with_attachments,
    resolve_attachments,
)
from cloud_agent_host.chat_sandbox import CHAT_UPLOADS_DIR, CHAT_WORKDIR
from cloud_agent_shared.errors import SandboxError
from cloud_agent_shared.git.source_archive import (
    DEFAULT_MAX_UPLOAD_ARCHIVE_BYTES,
    build_upload_archive,
    upload_archive_size,
)


class _Store:
    """The rows a staged selection would find, in the order asked for."""

    def __init__(self, rows: list[dict[str, Any]]) -> None:
        self.rows = rows

    async def select_attachments(
        self, *, user_id: str, attachment_ids: list[int]
    ) -> list[dict[str, Any]]:
        wanted = set(attachment_ids)
        return [row for row in self.rows if row["id"] in wanted]


def _row(identifier: int, path: str, size: int = 10) -> dict[str, Any]:
    return {
        "id": identifier,
        "path": path,
        "size_bytes": size,
        "content_type": "text/plain",
    }


@pytest.mark.parametrize(
    "path",
    ["a.txt", "specs/api.md", "a/b/c/d.png", "空格 和中文.md", "dot.in.name.tar.gz"],
)
def test_ordinary_relative_paths_are_kept_verbatim(path: str) -> None:
    """A safe path is never rewritten, only accepted."""
    assert safe_attachment_path(path) == path


@pytest.mark.parametrize(
    "path",
    [
        "../escape.txt",
        "a/../../escape.txt",
        "/absolute.txt",
        "C:/windows.txt",
        ".git/config",
        "a/.GIT/config",
        "a//b.txt",
        "./relative.txt",
        "",
        "   ",
        "x" * 513,
    ],
)
def test_an_unsafe_path_is_refused_rather_than_repaired(path: str) -> None:
    """Rewriting an unsafe path into a safe one would hide the problem."""
    with pytest.raises(AttachmentError):
        safe_attachment_path(path)


def test_a_backslash_is_normalized_to_a_separator_then_checked() -> None:
    """A Windows-style separator names a directory, and is checked as one."""
    assert safe_attachment_path("specs\\api.md") == "specs/api.md"
    with pytest.raises(AttachmentError):
        safe_attachment_path("specs\\..\\..\\escape.md")


@pytest.mark.parametrize(
    "path",
    [
        "a\nb.txt",
        "a\rb.txt",
        "line\nbreak.md",
        "tab\tname.txt",
        "nul\x00name.txt",
        "delete\x7fname.txt",
        "a/b\nc.txt",
    ],
)
def test_a_name_cannot_carry_a_control_character(path: str) -> None:
    """A control character would break a file reference out of its line.

    ``prompt_with_attachments`` lists files one per line, so a name carrying a
    newline reads as a second file that does not exist.
    """
    with pytest.raises(AttachmentError):
        safe_attachment_path(path)


def test_the_listing_has_exactly_one_line_per_file() -> None:
    """The listing's line count is the file count, so no name can add a line."""
    prompt = prompt_with_attachments("go", [{"path": "a.txt"}, {"path": "specs/api.md"}])
    listed = [line for line in prompt.splitlines() if line.startswith("- ")]
    assert listed == [
        f"- {SANDBOX_UPLOAD_DIR}/a.txt",
        f"- {SANDBOX_UPLOAD_DIR}/specs/api.md",
    ]


async def test_resolution_preserves_the_order_the_caller_asked_for() -> None:
    """The prompt lists files in the order the user attached them."""
    store = _Store([_row(1, "a.txt"), _row(2, "b.txt"), _row(3, "c.txt")])
    resolved = await resolve_attachments(store, user_id="u", attachment_ids=[3, 1])
    assert resolved is not None
    assert [item["path"] for item in resolved] == ["c.txt", "a.txt"]


async def test_nothing_attached_is_none_rather_than_an_empty_manifest() -> None:
    """Unlike skills there are no account defaults for an empty selection."""
    store = _Store([])
    assert await resolve_attachments(store, user_id="u", attachment_ids=None) is None
    assert await resolve_attachments(store, user_id="u", attachment_ids=[]) is None


async def test_an_upload_that_vanished_refuses_the_submission() -> None:
    """Better than a job whose prompt names a file the sandbox will not hold."""
    store = _Store([_row(1, "a.txt")])
    with pytest.raises(AttachmentError):
        await resolve_attachments(store, user_id="u", attachment_ids=[1, 2])


async def test_the_same_upload_cannot_be_attached_twice() -> None:
    """Two members with one path would make the archive ambiguous."""
    store = _Store([_row(1, "a.txt")])
    with pytest.raises(AttachmentError):
        await resolve_attachments(store, user_id="u", attachment_ids=[1, 1])


async def test_one_turn_cannot_attach_two_files_at_the_same_path() -> None:
    """Two picks named ``report.csv``: one would silently replace the other."""
    store = _Store([_row(1, "report.csv"), _row(2, "report.csv")])
    with pytest.raises(AttachmentError, match=r"same path: report\.csv"):
        await resolve_attachments(store, user_id="u", attachment_ids=[1, 2])


@pytest.mark.parametrize("paths", [["docs", "docs/a.md"], ["docs/a.md", "docs"]])
async def test_one_turn_cannot_make_a_path_both_a_file_and_a_folder(paths: list[str]) -> None:
    """``/uploads/docs`` can be a file or a folder, never both."""
    store = _Store([_row(index, path) for index, path in enumerate(paths, start=1)])
    with pytest.raises(AttachmentError, match="docs cannot be both a file and a folder"):
        await resolve_attachments(store, user_id="u", attachment_ids=[1, 2])


def test_a_later_turn_replaces_a_held_path_but_cannot_change_its_kind() -> None:
    """Saving over a file is ordinary; turning it into a folder is not.

    A retained sandbox already holds the earlier turns' files, and its import
    overwrites a file with a file but will not swap a file for a folder.
    """
    check_paths(["report.csv"], held=["report.csv", "notes.md"])
    check_paths(["docs/b.md"], held=["docs/a.md"])
    with pytest.raises(AttachmentError, match="docs cannot be both"):
        check_paths(["docs/a.md"], held=["docs"])
    with pytest.raises(AttachmentError, match="docs cannot be both"):
        check_paths(["docs"], held=["docs/a.md"])


def test_the_sandbox_holds_the_newest_upload_at_each_path() -> None:
    """What the host imports: one member per path, the one attached last."""
    rows = [_row(1, "report.csv"), _row(2, "notes.md"), _row(3, "report.csv")]
    assert [row["id"] for row in current_uploads(rows)] == [2, 3]
    # The archive the host builds refuses a repeated path; this list never has one.
    build_upload_archive([(row["path"], b"x") for row in current_uploads(rows)])


@pytest.mark.parametrize(
    ("paths", "kept"),
    [
        (["docs", "docs/a.md"], [2]),
        (["docs/a.md", "docs/b.md", "docs"], [3]),
        # The file ``docs`` removed ``docs/a.md`` before ``docs/b.md`` replaced it.
        (["docs/a.md", "docs", "docs/b.md"], [3]),
        (["docs/a.md", "docs/b.md"], [1, 2]),
    ],
)
def test_an_older_file_and_folder_clash_keeps_whatever_came_later(
    paths: list[str], kept: list[int]
) -> None:
    """Conversations from before the clash was refused still rebuild cleanly."""
    rows = [_row(index, path) for index, path in enumerate(paths, start=1)]
    current = current_uploads(rows)
    assert [row["id"] for row in current] == kept
    build_upload_archive([(row["path"], b"x") for row in current])


def test_a_conversation_is_bounded_by_what_the_host_will_import() -> None:
    """Every turn's files are imported together, so their sum is what is bounded."""
    check_thread_total([_row(1, "a.bin", size=MAX_THREAD_ATTACHMENT_BYTES)])
    with pytest.raises(AttachmentError, match="Start a new chat"):
        check_thread_total(
            [_row(1, "a.bin", size=MAX_THREAD_ATTACHMENT_BYTES), _row(2, "b.bin", size=1)]
        )
    many = [_row(index, f"{index}.txt", size=1) for index in range(MAX_THREAD_ATTACHMENT_FILES)]
    check_thread_total(many)
    with pytest.raises(AttachmentError, match="Start a new chat"):
        check_thread_total([*many, _row(MAX_THREAD_ATTACHMENT_FILES, "one-more.txt", size=1)])


def test_the_conversation_cap_fits_the_host_archive_with_its_worst_headers() -> None:
    """The cap must leave room for the tar headers of as many files as it allows.

    One-byte members carry the most padding a member can have, and every file
    here has the longest path, in a long-named folder of its own. Only folders
    nested deeper than that would add header blocks this does not count.
    """
    folder = "d" * (MAX_ATTACHMENT_PATH_LENGTH // 2)
    files = []
    for index in range(MAX_THREAD_ATTACHMENT_FILES):
        prefix = f"{folder}{index:04d}/"
        files.append((prefix + "f" * (MAX_ATTACHMENT_PATH_LENGTH - len(prefix)), b"x"))
    overhead = len(build_upload_archive(files)) - len(files)
    assert MAX_THREAD_ATTACHMENT_BYTES + overhead <= DEFAULT_MAX_UPLOAD_ARCHIVE_BYTES


def _deepest_upload(first: str) -> str:
    """The longest path the control plane admits, a one-letter folder every other character."""
    parts = [first]
    while len("/".join([*parts, "d", "f"])) <= MAX_ATTACHMENT_PATH_LENGTH:
        parts.append("d")
    return "/".join([*parts, "f"])


def _deepest_uploads() -> list[tuple[str, bytes]]:
    """As many one-byte files as a conversation holds, each in a folder chain of its own."""
    return [(_deepest_upload(f"{index:03d}"), b"x") for index in range(MAX_THREAD_ATTACHMENT_FILES)]


def test_a_conversation_never_holds_more_than_the_host_imports() -> None:
    """Every folder level is one more tar header, which the byte cap never counted.

    At the control plane's own limits, almost no content makes an archive of
    headers alone, over the host's limit, and every turn of the conversation
    on a fresh container failed to import what the claim had accepted.
    """
    files = _deepest_uploads()
    rows = [_row(index, path, size=len(content)) for index, (path, content) in enumerate(files)]
    assert [safe_attachment_path(row["path"]) for row in rows] == [path for path, _ in files]

    with pytest.raises(SandboxError, match="upload import size limit"):
        build_upload_archive(files)
    with pytest.raises(AttachmentError, match="Start a new chat"):
        check_thread_total(rows)


def test_the_most_a_conversation_may_hold_is_what_the_host_imports() -> None:
    """At the limits the two agree to the file: one more and the claim refuses."""
    files = _deepest_uploads()
    rows = [_row(index, path, size=len(content)) for index, (path, content) in enumerate(files)]

    def accepted(count: int) -> bool:
        try:
            check_thread_total(rows[:count])
        except AttachmentError:
            return False
        return True

    low, high = 1, len(rows)
    while low < high:
        middle = (low + high + 1) // 2
        low, high = (middle, high) if accepted(middle) else (low, middle - 1)
    assert accepted(low) and not accepted(low + 1)
    build_upload_archive(files[:low])


@pytest.mark.parametrize(
    "files",
    [
        [("a.txt", b"x"), ("block.bin", b"y" * 512), ("over.bin", b"z" * 513)],
        [("docs/a.md", b"a"), ("docs/b.md", b"b"), ("docs/sub/c.md", b"c" * 1000)],
        [("空格 和中文.md", b"cv"), ("\U00020000/\U00020001/f", b"x"), ("é" * 101, b"e")],
        [("d" * 99 + "/f", b"x"), ("e" * 100 + "/f", b"x"), ("g" * 300 + "/" + "h" * 211, b"x")],
        [(_deepest_upload(f"{index:03d}"), b"x" * index) for index in range(3)],
    ],
)
def test_the_claim_measures_the_upload_archive_exactly(files: list[tuple[str, bytes]]) -> None:
    """The control plane holds paths and sizes; its count is the archive's own length."""
    sized = [(path, len(content)) for path, content in files]
    assert upload_archive_size(sized) == len(build_upload_archive(files))


def test_the_server_bounds_the_selection_as_well_as_the_browser() -> None:
    """A browser check is a convenience; this one is the boundary."""
    check_selection([_row(i, f"{i}.txt") for i in range(MAX_ATTACHMENT_FILES)])
    with pytest.raises(AttachmentError):
        check_selection([_row(i, f"{i}.txt") for i in range(MAX_ATTACHMENT_FILES + 1)])
    with pytest.raises(AttachmentError):
        check_selection([_row(1, "big", size=21 * 1024 * 1024)])


def test_the_prompt_names_every_file_by_its_sandbox_path() -> None:
    """The five Agent CLIs take a prompt and a directory; this is the bridge."""
    prompt = prompt_with_attachments(
        "Fix the timeout", [_row(1, "log.txt"), _row(2, "specs/api.md")]
    )
    assert f"{SANDBOX_UPLOAD_DIR}/log.txt" in prompt
    assert f"{SANDBOX_UPLOAD_DIR}/specs/api.md" in prompt
    assert prompt.endswith("Fix the timeout")
    assert "must not be committed" in prompt


def test_an_empty_selection_leaves_the_task_prompt_untouched() -> None:
    """A task that attached nothing reads exactly as it did before."""
    assert prompt_with_attachments("Fix the timeout", None) == "Fix the timeout"
    assert prompt_with_attachments("Fix the timeout", []) == "Fix the timeout"


def _members(blob: bytes) -> dict[str, tarfile.TarInfo]:
    with tarfile.open(fileobj=io.BytesIO(blob)) as archive:
        return {member.name: member for member in archive.getmembers()}


def test_uploads_are_root_owned_and_unwritable_by_the_sandbox() -> None:
    """The sandbox allows no read-only mounts, so mode and owner are the lock."""
    members = _members(build_upload_archive([("a.txt", b"hello"), ("specs/api.md", b"x")]))
    assert members["a.txt"].mode == 0o444
    assert members["specs"].isdir()
    assert members["specs"].mode == 0o555
    assert all(member.uid == 0 and member.gid == 0 for member in members.values())


def test_intermediate_directories_are_emitted_rather_than_implied() -> None:
    """Extraction would otherwise create them with its own, writable, defaults."""
    members = _members(build_upload_archive([("a/b/c/deep.txt", b"x")]))
    assert [name for name in members if members[name].isdir()] == ["a", "a/b", "a/b/c"]


@pytest.mark.parametrize("path", ["../escape.txt", "/abs.txt", "a/../../escape.txt"])
def test_the_archive_refuses_a_path_that_escapes_the_upload_directory(path: str) -> None:
    """A second refusal, on the host, after the control plane's own."""
    with pytest.raises(SandboxError):
        build_upload_archive([(path, b"x")])


def test_the_archive_refuses_two_members_with_the_same_path() -> None:
    """Which of the two won would otherwise depend on extraction order."""
    with pytest.raises(SandboxError):
        build_upload_archive([("a.txt", b"one"), ("a.txt", b"two")])


def test_the_archive_refuses_more_than_its_own_size_limit() -> None:
    """The host keeps a bound of its own if the control plane's ever slips."""
    with pytest.raises(SandboxError):
        build_upload_archive([("big.bin", b"x" * 2048)], max_bytes=1024)


def test_uploads_land_outside_the_worktree_so_they_cannot_reach_a_pull_request() -> None:
    """The publish path stages the workspace with ``git add -A -N``.

    Anything under ``CHAT_WORKDIR`` is therefore committed and shows up in the
    draft pull request. This is the invariant that keeps a user's screenshot
    out of their own diff, so it is asserted rather than assumed.
    """
    assert CHAT_UPLOADS_DIR != CHAT_WORKDIR
    assert not CHAT_UPLOADS_DIR.startswith(f"{CHAT_WORKDIR}/")
    assert SANDBOX_UPLOAD_DIR == CHAT_UPLOADS_DIR


def _job(**overrides: Any) -> Any:
    from cloud_agent_host.runner import ClaimedJob

    return ClaimedJob.from_response(
        {
            "job_id": "j",
            "thread_id": "t",
            "attempt_id": 1,
            "attempt_no": 1,
            "repo": "o/r",
            "task_prompt": "p",
            "runtime": "claude-code",
            "model": "m",
            "worker_token": "w",
            "sandbox_token": "s",
            **overrides,
        }
    )


class _Control:
    """Counts what the host would actually fetch."""

    def __init__(self, files: list[tuple[str, bytes]]) -> None:
        self.files = files
        self.listed = 0
        self.read = 0

    def list_attachments(self) -> list[dict[str, Any]]:
        self.listed += 1
        return [{"id": index} for index, _ in enumerate(self.files)]

    def read_attachment(self, attachment_id: int) -> tuple[str, bytes]:
        self.read += 1
        return self.files[attachment_id]


class _Manager:
    """Records where the archive was written, which is the whole invariant."""

    def __init__(self) -> None:
        self.imports: list[tuple[str, bytes]] = []

    def import_archive(self, spec: Any, path: str, archive: bytes) -> None:
        self.imports.append((path, archive))


@pytest.mark.parametrize(
    ("fresh", "thread", "turn", "expected"),
    [
        (True, 0, 0, False),  # nothing was ever attached
        (True, 2, 0, True),  # new container restores the whole conversation
        (False, 2, 0, False),  # retained container already holds them
        (False, 2, 1, True),  # this turn added one, so write them again
        (True, 2, 2, True),  # first turn of a task that attached files
    ],
)
def test_uploads_are_fetched_only_when_the_container_needs_them(
    fresh: bool, thread: int, turn: int, expected: bool
) -> None:
    """Re-fetching every turn would move the conversation's bytes for nothing."""
    from cloud_agent_host.runner import _import_uploads

    control = _Control([("a.txt", b"x"), ("b.txt", b"y")])
    manager = _Manager()
    imported = _import_uploads(
        job=_job(thread_attachment_count=thread, turn_attachment_count=turn),
        control=control,
        manager=manager,
        spec=object(),
        fresh_container=fresh,
    )
    assert bool(imported) is expected
    assert bool(manager.imports) is expected
    assert control.read == (2 if expected else 0)


def test_the_imported_archive_is_written_outside_the_worktree() -> None:
    """The one invariant that keeps an upload out of the user's own diff."""
    from cloud_agent_host.runner import _import_uploads

    manager = _Manager()
    _import_uploads(
        job=_job(thread_attachment_count=1, turn_attachment_count=1),
        control=_Control([("notes.md", b"hello")]),
        manager=manager,
        spec=object(),
        fresh_container=True,
    )
    path, archive = manager.imports[0]
    assert path == CHAT_UPLOADS_DIR
    assert "notes.md" in _members(archive)
