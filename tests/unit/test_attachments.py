"""Per-task uploads: which paths are allowed, and what the agent is told."""

from __future__ import annotations

import io
import tarfile
from typing import Any

import pytest

from cloud_agent.attachments.models import (
    MAX_ATTACHMENT_PATH_LENGTH,
    MAX_THREAD_ATTACHMENT_BYTES,
    MAX_THREAD_ATTACHMENT_FILES,
    SANDBOX_UPLOAD_DIR,
    AttachmentError,
    attachment_max_files,
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
    "path", [" report.csv ", "notes.txt ", " specs/api.md ", "  two  spaces.md  "]
)
def test_surrounding_whitespace_is_part_of_the_name(path: str) -> None:
    """A name the browser accepts is stored under the name it was given.

    ``isSafeUploadPath`` accepts surrounding whitespace, and on Linux
    ``" report.csv "`` and ``"report.csv"`` are different files. Trimming here
    would store a path the user never attached.
    """
    assert safe_attachment_path(path) == path


def test_names_differing_only_in_whitespace_stay_distinct() -> None:
    """The trim cannot collapse two names the browser keeps apart.

    Trimming used to make these one stored path, so the second upload silently
    took the first one's name.
    """
    assert safe_attachment_path(" report.csv ") != safe_attachment_path("report.csv")


@pytest.mark.parametrize("path", [" ", "   ", "\t", "\n", " \t "])
def test_a_path_that_is_only_whitespace_is_still_refused(path: str) -> None:
    """Keeping surrounding whitespace does not make a blank path a name."""
    with pytest.raises(AttachmentError):
        safe_attachment_path(path)


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
        f'- "{SANDBOX_UPLOAD_DIR}/a.txt"',
        f'- "{SANDBOX_UPLOAD_DIR}/specs/api.md"',
    ]


@pytest.mark.parametrize("path", ["notes.txt ", " report.csv", "  two  spaces.md  "])
def test_the_listing_shows_the_whitespace_a_name_keeps(path: str) -> None:
    """Quoted, a trailing space is still there for the agent to open.

    Unquoted, ``- /uploads/notes.txt `` ends in a space nobody can see, and the
    agent opens ``notes.txt``, which does not exist.
    """
    prompt = prompt_with_attachments("go", [{"path": path}])
    listed = [line for line in prompt.splitlines() if line.startswith("- ")]
    assert listed == [f'- "{SANDBOX_UPLOAD_DIR}/{path}"']


def test_a_quoted_name_stays_one_readable_path() -> None:
    """A quote or a non-ASCII letter in a name neither ends the path nor is escaped away."""
    prompt = prompt_with_attachments("go", [{"path": 'say "hi".txt'}, {"path": "报告.csv"}])
    listed = [line for line in prompt.splitlines() if line.startswith("- ")]
    assert listed == [
        f'- "{SANDBOX_UPLOAD_DIR}/say \\"hi\\".txt"',
        f'- "{SANDBOX_UPLOAD_DIR}/报告.csv"',
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
    check_selection([_row(i, f"{i}.txt") for i in range(attachment_max_files())])
    with pytest.raises(AttachmentError):
        check_selection([_row(i, f"{i}.txt") for i in range(attachment_max_files() + 1)])
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
        return current_uploads(
            [
                {"id": index, "path": path, "size_bytes": len(content)}
                for index, (path, content) in enumerate(self.files)
            ]
        )

    def read_attachment(self, attachment_id: int) -> tuple[str, bytes]:
        self.read += 1
        return self.files[attachment_id]


class _Manager:
    """Records where the archive was written, which is the whole invariant."""

    def __init__(self) -> None:
        self.imports: list[tuple[str, bytes]] = []

    def import_archive(self, spec: Any, path: str, archive: bytes) -> None:
        self.imports.append((path, archive if isinstance(archive, bytes) else archive.read()))


@pytest.fixture(autouse=True)
def _upload_receipts(tmp_path, monkeypatch):
    monkeypatch.delenv("AGENT_UPLOAD_RECEIPT_DIR", raising=False)
    monkeypatch.setattr(
        "cloud_agent_host.upload_import.credential_path", lambda: tmp_path / "host.json"
    )


def test_upload_receipt_directory_can_be_shared_without_host_credential(tmp_path, monkeypatch):
    from cloud_agent_host import upload_import

    shared = tmp_path / "shared-upload-receipts"
    monkeypatch.setenv("AGENT_UPLOAD_RECEIPT_DIR", str(shared))
    monkeypatch.setattr(
        upload_import,
        "credential_path",
        lambda: pytest.fail("the dedicated receipt path must not read the host credential path"),
    )

    assert {path.parent for path in upload_import._paths("sandbox")} == {shared}


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
    from types import SimpleNamespace

    spec = SimpleNamespace(container_name="test-container")
    if not fresh and not turn and thread:
        _import_uploads(
            job=_job(thread_attachment_count=thread, turn_attachment_count=thread),
            control=control,
            manager=manager,
            spec=spec,
            fresh_container=True,
            container_id="container-1",
        )
        control.read = control.listed = 0
        manager.imports.clear()
    imported = _import_uploads(
        job=_job(thread_attachment_count=thread, turn_attachment_count=turn),
        control=control,
        manager=manager,
        spec=spec,
        fresh_container=fresh,
        container_id="container-1",
    )
    assert bool(imported) is expected
    assert bool(manager.imports) is expected
    assert control.read == (2 if expected else 0)


def test_the_imported_archive_is_written_outside_the_worktree() -> None:
    """The one invariant that keeps an upload out of the user's own diff."""
    from types import SimpleNamespace

    from cloud_agent_host.runner import _import_uploads

    manager = _Manager()
    _import_uploads(
        job=_job(thread_attachment_count=1, turn_attachment_count=1),
        control=_Control([("notes.md", b"hello")]),
        manager=manager,
        spec=SimpleNamespace(container_name="test-container"),
        fresh_container=True,
        container_id="container-1",
    )
    path, archive = manager.imports[0]
    assert path == CHAT_UPLOADS_DIR
    assert "notes.md" in _members(archive)


def test_retained_upload_import_transfers_only_new_or_replaced_bodies():
    from types import SimpleNamespace

    from cloud_agent_host.upload_import import import_uploads

    control = _Control([(f"{i}.txt", b"x" * 1024) for i in range(199)])
    manager = _Manager()
    spec = SimpleNamespace(container_name="test-container")

    def run():
        return import_uploads(
            job=_job(thread_attachment_count=len(control.files), turn_attachment_count=1),
            control=control,
            manager=manager,
            spec=spec,
            fresh_container=False,
            container_id="container-1",
        )

    assert run() == 199
    control.files.append(("new.txt", b"new"))
    control.read = 0
    assert run() == 1
    assert control.read == 1
    assert set(_members(manager.imports[-1][1])) == {"new.txt"}
    control.files.append(("0.txt", b"replacement"))
    control.read = 0
    assert run() == 1
    assert control.read == 1
    assert set(_members(manager.imports[-1][1])) == {"0.txt"}


def test_cumulative_deltas_still_obey_the_full_archive_limit(monkeypatch):
    from types import SimpleNamespace

    from cloud_agent_host import upload_import as upload_import_module

    content = b"x" * (6 * 1024)
    control = _Control([("a.bin", content)])
    manager = _Manager()
    spec = SimpleNamespace(container_name="test-container")
    single_archive = upload_archive_size([("a.bin", len(content))])
    monkeypatch.setattr(upload_import_module, "DEFAULT_MAX_UPLOAD_ARCHIVE_BYTES", single_archive)

    def run():
        return upload_import_module.import_uploads(
            job=_job(thread_attachment_count=len(control.files), turn_attachment_count=1),
            control=control,
            manager=manager,
            spec=spec,
            fresh_container=False,
            container_id="container-1",
        )

    assert run() == 1
    control.files.append(("b.bin", content))
    control.read = 0
    with pytest.raises(SandboxError, match="upload import size limit"):
        run()
    assert control.read == 0
    assert len(manager.imports) == 1


def test_interrupted_import_retries_without_requiring_new_turn_uploads():
    from types import SimpleNamespace

    from cloud_agent_host.upload_import import import_uploads

    control = _Control([("a.txt", b"old")])
    manager = _Manager()
    spec = SimpleNamespace(container_name="test-container")

    def run(turn=1, container="container-1"):
        return import_uploads(
            job=_job(thread_attachment_count=len(control.files), turn_attachment_count=turn),
            control=control,
            manager=manager,
            spec=spec,
            fresh_container=False,
            container_id=container,
        )

    assert run() == 1
    original = manager.import_archive

    def interrupted(*args):
        original(*args)
        raise RuntimeError("Docker copied bytes before the reply was lost")

    manager.import_archive = interrupted
    control.files.append(("b.txt", b"new"))
    with pytest.raises(RuntimeError, match="reply was lost"):
        run()
    manager.import_archive = original
    control.read = 0
    assert run(turn=0) == 2
    assert control.read == 2
    control.read = 0
    assert run(turn=0) == 0
    assert control.read == 0
    assert run(turn=0, container="replacement") == 2


def test_streaming_upload_archive_memory_does_not_scale_with_total_bytes():
    import tempfile
    import tracemalloc

    from cloud_agent_shared.git.source_archive import write_upload_archive

    tracemalloc.start()
    try:
        with tempfile.TemporaryFile() as archive:
            write_upload_archive(((f"{i}.bin", b"x" * (1024 * 1024)) for i in range(60)), archive)
            assert archive.tell() > 60 * 1024 * 1024
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    assert peak < 8 * 1024 * 1024


def test_a_sibling_overwrite_is_restored_even_without_new_uploads(tmp_path):
    from types import SimpleNamespace

    from cloud_agent_host.upload_import import import_uploads

    original = _Control([("shared.txt", b"original")])
    sibling = _Control([("shared.txt", b"original"), ("shared.txt", b"sibling")])
    manager = _Manager()
    spec = SimpleNamespace(container_name="shared-container")

    def run(thread, control):
        return import_uploads(
            job=_job(
                thread_id=thread,
                thread_attachment_count=len(control.files),
                turn_attachment_count=0,
            ),
            control=control,
            manager=manager,
            spec=spec,
            fresh_container=False,
            container_id="container-1",
        )

    assert run("origin", original) == 1
    assert run("sibling", sibling) == 1
    assert run("origin", original) == 1
    assert original.read == 2
    with tarfile.open(fileobj=io.BytesIO(manager.imports[-1][1])) as archive:
        assert archive.extractfile("shared.txt").read() == b"original"
    assert run("origin", original) == 0

    # Losing host-local state must cause restoration, not trusting the count.
    receipt = next((tmp_path / "upload-receipts").glob("*.json"))
    receipt.unlink()
    assert run("origin", original) == 1


def test_deleted_container_forgets_only_its_upload_receipt(tmp_path):
    from types import SimpleNamespace

    from cloud_agent_host.upload_import import (
        _paths,
        forget_deleted_container_uploads,
        import_uploads,
    )

    manager = _Manager()

    def import_for(container_name):
        return import_uploads(
            job=_job(thread_attachment_count=1, turn_attachment_count=1),
            control=_Control([("notes.md", b"hello")]),
            manager=manager,
            spec=SimpleNamespace(container_name=container_name),
            fresh_container=True,
            container_id=f"id-{container_name}",
        )

    assert import_for("deleted") == 1
    assert import_for("retained") == 1
    root = tmp_path / "upload-receipts"
    deleted = set(_paths("deleted"))
    retained = set(_paths("retained"))
    assert set(root.iterdir()) == deleted | retained

    forget_deleted_container_uploads("deleted")

    assert set(root.iterdir()) == retained
    forget_deleted_container_uploads("deleted")
    assert set(root.iterdir()) == retained


def test_upload_receipt_cleanup_retries_a_failed_directory_sync(tmp_path, monkeypatch):
    from cloud_agent_host import upload_import

    path, lock_path = upload_import._paths("deleted")
    path.parent.mkdir(parents=True)
    path.write_text("{}")
    lock_path.touch()
    syncs = 0

    def fsync(_descriptor):
        nonlocal syncs
        syncs += 1
        if syncs == 1:
            raise OSError("directory sync failed")

    monkeypatch.setattr(upload_import.os, "fsync", fsync)

    with pytest.raises(OSError, match="directory sync"):
        upload_import.forget_deleted_container_uploads("deleted")

    assert not path.exists()
    assert not lock_path.exists()
    upload_import.forget_deleted_container_uploads("deleted")
    assert syncs == 2
    assert not path.exists()
    assert not lock_path.exists()
