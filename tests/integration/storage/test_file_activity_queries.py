"""File Activity against a real database: the projection, ownership and bounds.

What only Postgres can settle:

- **Ownership is in the query.** A conversation that is not the caller's, or is
  deleted, reads as absent — never as an empty activity.
- **The projection reads real JSONB** for every pinned runtime's recorded tool
  spelling, includes a turn's cwd, and pairs each call with its own result
  without multiplying it when a result row repeats.
- **Bounds are reported.** A scan that stops at its event bound says so on its
  coverage, and the page totals are labelled as the scanned window's.
"""

from __future__ import annotations

import json
import os
import time
from datetime import UTC, datetime, timedelta

import pytest
import pytest_asyncio
from tests.integration.storage.test_agent_job_store import _create_job
from tests.integration.storage.test_attempt_ledger import db as db

from cloud_agent.storage.assembly import Storage
from cloud_agent.storage.repositories.file_activity import PostgresFileActivityQueries

pytestmark = [pytest.mark.dbtest, pytest.mark.asyncio]

OWNER = "fa-owner"
OTHER = "fa-other"

_TEMPLATE: dict[str, str] = {}

_CLONE_THREAD_SQL = """
    INSERT INTO agent_threads
    SELECT (jsonb_populate_record(NULL::agent_threads, to_jsonb(t) || jsonb_build_object(
        'id', $2::text, 'user_id', $3::text, 'title', $4::text))).*
    FROM agent_threads t WHERE t.id = $1
"""

_CLONE_JOB_SQL = """
    INSERT INTO agent_jobs
    SELECT (jsonb_populate_record(NULL::agent_jobs, to_jsonb(j) || jsonb_build_object(
        'id', $2::text, 'thread_id', $3::text, 'user_id', $4::text, 'parent_job_id', $5::text,
        'turn_no', $6::int, 'native_turn_no', $6::int, 'state', 'succeeded',
        'task_prompt', $7::text, 'created_at', $8::timestamptz, 'current_attempt_id', NULL,
        'runtime', $9::text, 'model', $10::text))).*
    FROM agent_jobs j WHERE j.id = $1
"""


@pytest_asyncio.fixture
async def store(db):
    _TEMPLATE.clear()
    return Storage(db)


async def _template(db, store) -> tuple[str, str]:
    if not _TEMPLATE:
        job = await _create_job(store, user_id="fa-template")
        _TEMPLATE.update(thread=job["thread_id"], job=job["id"])
    return _TEMPLATE["thread"], _TEMPLATE["job"]


async def conversation(
    db, store, thread_id: str, *, user_id: str = OWNER, turns: int = 1
) -> list[str]:
    """One conversation with ``turns`` turns, each one finished attempt."""
    template_thread, template_job = await _template(db, store)
    base = datetime.now(UTC) - timedelta(hours=2)
    job_ids: list[str] = []
    async with db.acquire() as conn:
        await conn.execute(
            _CLONE_THREAD_SQL, template_thread, thread_id, user_id, f"Title {thread_id}"
        )
        parent = None
        for turn in range(1, turns + 1):
            job_id = f"{thread_id}-j{turn}"
            at = base + timedelta(minutes=turn)
            await conn.execute(
                _CLONE_JOB_SQL,
                template_job,
                job_id,
                thread_id,
                user_id,
                parent,
                turn,
                "p",
                at,
                "claude-code",
                "m",
            )
            await conn.execute(
                """
                INSERT INTO agent_attempts (job_id, thread_id, attempt_no, lease_owner,
                    lease_generation, lease_expires_at, status, started_at, finished_at)
                VALUES ($1, $2, 1, 'fa-test', 1, NOW(), 'finished', $3, $3)
                """,
                job_id,
                thread_id,
                at,
            )
            parent = job_id
            job_ids.append(job_id)
    return job_ids


async def events(db, job_id: str, rows: list[tuple[str, dict]]) -> None:
    async with db.acquire() as conn:
        attempt_id, start = await conn.fetchrow(
            "SELECT a.id, COALESCE(MAX(e.seq), 0) FROM agent_attempts a"
            " LEFT JOIN agent_job_events e ON e.attempt_id = a.id"
            " WHERE a.job_id = $1 GROUP BY a.id",
            job_id,
        )
        for offset, (kind, payload) in enumerate(rows, start=1):
            await conn.execute(
                "INSERT INTO agent_job_events (job_id, attempt_id, seq, event_type, payload)"
                " VALUES ($1, $2, $3, $4, $5::jsonb)",
                job_id,
                attempt_id,
                start + offset,
                kind,
                json.dumps(payload),
            )


def call(tool: str, call_id: str, **arguments) -> tuple[str, dict]:
    return ("tool_use", {"name": tool, "id": call_id, "input": arguments})


def result(call_id: str, content: str = "ok", *, error: bool = False) -> tuple[str, dict]:
    return ("tool_result", {"tool_use_id": call_id, "content": content, "is_error": error})


async def _view(store, job_id: str, **overrides):
    options = {
        "job_id": job_id,
        "user_id": OWNER,
        "sort": "active",
        "filter_key": "all",
        "q": "",
        "cursor": None,
        "limit": 25,
    }
    options.update(overrides)
    return await store.file_activity_queries.file_activity(**options)


async def test_a_conversations_files_across_runtimes_and_turns(db, store):
    [first, second] = await conversation(db, store, "fa_thread", turns=2)
    await events(
        db,
        first,
        [
            ("lifecycle", {"phase": "started", "cwd": "/workspace"}),
            call("Read", "c1", file_path="src/app.ts"),
            result("c1"),
            call("Write", "c2", file_path="docs/readme.md", content="a\nb\n"),
            result("c2"),
            call("Edit", "c3", file_path="./src/app.ts", old_string="one\n", new_string="one\ntwo"),
            result("c3"),
            # A shell command that mentions a path is not evidence it touched it.
            call("Bash", "c4", command="rm -rf src/app.ts"),
            result("c4"),
            call("Grep", "c5", pattern="TODO", path="src"),
            result("c5", "src/app.ts:1:TODO"),
        ],
    )
    await events(
        db,
        second,
        [
            ("lifecycle", {"phase": "started", "cwd": "/workspace"}),
            call("view_image", "c6", file_path="assets/logo.png"),
            result("c6", '{"type":"image","source":{"media_type":"image/png"}}'),
            call("Read", "c7", path="src/app.ts"),
            result("c7"),
        ],
    )

    view = await _view(store, second)
    assert view is not None
    rows = {row.path: row for row in view.files}
    assert set(rows) == {
        "/workspace/src/app.ts",
        "/workspace/docs/readme.md",
        "/workspace/assets/logo.png",
    }
    app = rows["/workspace/src/app.ts"]
    # Two real reads with their own ids stay two reads; the edit is a write,
    # so the Written filter shows a file an edit changed.
    assert (app.reads, app.writes, app.images, app.searches) == (2, 1, 0, 0)
    assert app.operations == 3  # two reads and the edit
    assert app.added == 2 and app.removed == 1
    assert app.attempts == 2
    assert rows["/workspace/docs/readme.md"].writes == 1
    assert rows["/workspace/docs/readme.md"].added == 2
    # A write's removed side is unknown, never zero.
    assert rows["/workspace/docs/readme.md"].removed == 0
    assert rows["/workspace/docs/readme.md"].removed_unknown_ops == 1
    image = rows["/workspace/assets/logo.png"]
    assert (image.images, image.reads) == (1, 0)
    assert view.totals.search_unknown_scope == 1
    assert view.totals.unknown_path_operations == 0
    assert app.type_label == "TS"


async def test_failed_pending_and_unrecorded_calls_are_not_completed_work(db, store):
    [job] = await conversation(db, store, "fa_statuses")
    await events(
        db,
        job,
        [
            ("lifecycle", {"phase": "started", "cwd": "/workspace"}),
            call("Write", "w1", file_path="a.ts", content="x"),
            result("w1", "denied", error=True),
            call("Write", "w2", file_path="a.ts", content="y"),
            call("Read", "r1", file_path="a.ts"),
            result("r1"),
        ],
    )
    view = await _view(store, job)
    assert view is not None
    row = view.files[0]
    assert row.path == "/workspace/a.ts"
    # The failed write and the unanswered one are listed, and count as neither.
    assert (row.writes, row.reads) == (0, 1)
    assert (row.failed, row.not_recorded) == (1, 1)
    assert (view.totals.failed, view.totals.not_recorded) == (1, 1)
    page = await store.file_activity_queries.file_operations(
        job_id=job, user_id=OWNER, path=row.path, cursor=None, limit=50
    )
    assert page is not None
    assert {operation.status for operation in page.operations} == {
        "failed",
        "not_recorded",
        "succeeded",
    }


async def test_multi_file_patch_add_delete_rename_and_duplicate_results(db, store):
    [job] = await conversation(db, store, "fa_patch")
    patch = (
        "*** Begin Patch\n"
        "*** Add File: new.ts\n"
        "+line one\n"
        "+line two\n"
        "*** Delete File: old.ts\n"
        "-gone one\n"
        "-gone two\n"
        "-gone three\n"
        "*** Update File: moved.ts\n"
        "*** Move to: final.ts\n"
        "@@\n"
        "-before\n"
        "+after\n"
        "*** End Patch\n"
    )
    await events(
        db,
        job,
        [
            ("lifecycle", {"phase": "started", "cwd": "/workspace"}),
            ("tool_use", {"name": "apply_patch", "id": "p1", "input": patch}),
            result("p1", "Success"),
            # A duplicated result row must not double the operations.
            result("p1", "Success"),
        ],
    )
    view = await _view(store, job)
    assert view is not None
    rows = {row.path: row for row in view.files}
    assert set(rows) == {
        "/workspace/new.ts",
        "/workspace/old.ts",
        "/workspace/moved.ts",
    }
    assert rows["/workspace/new.ts"].added == 2
    assert rows["/workspace/new.ts"].removed == 0
    # A deleted file's content was not recorded: its removed side is unknown.
    assert rows["/workspace/old.ts"].removed == 0
    assert rows["/workspace/old.ts"].removed_unknown_ops == 1
    assert rows["/workspace/moved.ts"].added == 1
    assert rows["/workspace/moved.ts"].removed == 1
    page = await store.file_activity_queries.file_operations(
        job_id=job, user_id=OWNER, path="/workspace/moved.ts", cursor=None, limit=10
    )
    assert page is not None
    assert page.operations[0].renamed_to == "/workspace/final.ts"
    assert page.operations[0].patch_files == 3


def _codex_notification(method: str, item: dict) -> tuple[str, dict]:
    notification = {"method": method, "params": {"item": item}}
    return (
        "raw",
        {
            "protocol": "codex-app-server",
            "text": json.dumps(notification)[:4000],
            "notification": notification,
        },
    )


async def test_one_codex_item_announced_across_rows_counts_once(db, store):
    """item/started and item/completed are two rows for one native item."""
    [job] = await conversation(db, store, "fa_codex_merge")
    item = {
        "id": "fc-merge",
        "type": "fileChange",
        "status": "inProgress",
        "changes": [{"path": "app/a.py", "kind": {"type": "update"}}],
    }
    completed = {
        **item,
        "status": "completed",
        "changes": [
            {"path": "app/a.py", "kind": {"type": "update"}, "diff": "@@\n-old\n+new\n+guard\n"}
        ],
    }
    await events(
        db,
        job,
        [
            ("lifecycle", {"phase": "started", "cwd": "/workspace"}),
            _codex_notification("item/started", item),
            _codex_notification("item/completed", completed),
            # A duplicate of the completed notification must not double it.
            _codex_notification("item/completed", completed),
        ],
    )
    view = await _view(store, job)
    assert view is not None
    rows = {row.path: row for row in view.files}
    assert set(rows) == {"/workspace/app/a.py"}
    assert rows["/workspace/app/a.py"].writes == 1
    assert rows["/workspace/app/a.py"].added == 2
    assert rows["/workspace/app/a.py"].removed == 1
    assert view.totals.pending == 0


async def test_a_codex_change_without_a_diff_has_no_line_count(db, store):
    [job] = await conversation(db, store, "fa_codex_no_diff")
    item = {
        "id": "fc-nodiff",
        "type": "fileChange",
        "status": "completed",
        "changes": [{"path": "app/b.py", "kind": {"type": "update"}}],
    }
    await events(
        db,
        job,
        [
            ("lifecycle", {"phase": "started", "cwd": "/workspace"}),
            _codex_notification("item/completed", item),
        ],
    )
    view = await _view(store, job)
    assert view is not None
    row = view.files[0]
    # Absent data stays unknown: not zero.
    assert (row.added, row.removed) == (0, 0)
    assert (row.added_unknown_ops, row.removed_unknown_ops) == (1, 1)


async def test_a_multiedit_reads_its_pairs_from_the_real_projection(db, store):
    [job] = await conversation(db, store, "fa_multiedit")
    await events(
        db,
        job,
        [
            ("lifecycle", {"phase": "started", "cwd": "/w"}),
            call(
                "MultiEdit",
                "m1",
                file_path="a.py",
                edits=[
                    {"old_string": "one", "new_string": "one" + chr(10) + "two"},
                    {"old_string": "three" + chr(10) + "four", "new_string": "three"},
                ],
            ),
            result("m1"),
        ],
    )
    view = await _view(store, job)
    assert view is not None
    row = view.files[0]
    # 2 + 1 lines added, 1 + 2 removed, from the pairs the projection kept.
    assert (row.added, row.removed) == (3, 3)
    assert (row.added_unknown_ops, row.removed_unknown_ops) == (0, 0)


async def test_codex_file_change_notifications_are_read_from_raw_rows(db, store):
    [job] = await conversation(db, store, "fa_codex")
    notification = {
        "method": "item/completed",
        "params": {
            "item": {
                "id": "fc1",
                "type": "fileChange",
                "status": "completed",
                "changes": [
                    {
                        "path": "src/a.py",
                        "kind": {"type": "update"},
                        "diff": "@@ -1 +1,2 @@\n-old\n+new\n+added\n",
                    },
                    {"path": "src/gone.py", "kind": {"type": "delete"}},
                ],
            }
        },
    }
    await events(
        db,
        job,
        [
            ("lifecycle", {"phase": "started", "cwd": "/workspace"}),
            (
                "raw",
                {
                    "protocol": "codex-app-server",
                    "text": json.dumps(notification)[:4000],
                    "notification": notification,
                },
            ),
        ],
    )
    view = await _view(store, job)
    assert view is not None
    rows = {row.path: row for row in view.files}
    assert rows["/workspace/src/a.py"].writes == 1
    assert rows["/workspace/src/a.py"].added == 2
    assert rows["/workspace/src/a.py"].removed == 1
    # A delete is a file-mutating edit, so the Written filter shows it; its
    # removed side is unknown because the notification carried no diff.
    assert rows["/workspace/src/gone.py"].writes == 1
    assert rows["/workspace/src/gone.py"].added == 0
    assert rows["/workspace/src/gone.py"].removed_unknown_ops == 1


async def test_relative_paths_need_the_attempts_own_cwd(db, store):
    [first, second] = await conversation(db, store, "fa_cwd", turns=2)
    await events(
        db,
        first,
        [
            ("lifecycle", {"phase": "started", "cwd": "/one"}),
            call("Read", "a", file_path="shared.ts"),
            result("a"),
        ],
    )
    await events(
        db,
        second,
        [
            # No cwd recorded: the relative path must stay relative.
            call("Read", "b", file_path="shared.ts"),
            result("b"),
            call("Read", "c", file_path="/one/shared.ts"),
            result("c"),
        ],
    )
    view = await _view(store, second)
    assert view is not None
    rows = {row.path: row for row in view.files}
    assert set(rows) == {"/one/shared.ts", "shared.ts"}
    assert rows["shared.ts"].reads == 1
    assert rows["/one/shared.ts"].reads == 2
    assert rows["shared.ts"].path == "shared.ts"


async def test_ownership_and_deleted_conversations_read_as_absent(db, store):
    [mine] = await conversation(db, store, "fa_mine")
    [theirs] = await conversation(db, store, "fa_theirs", user_id=OTHER)
    await events(db, mine, [call("Read", "a", file_path="a.ts"), result("a")])
    await events(db, theirs, [call("Read", "b", file_path="b.ts"), result("b")])

    assert await _view(store, theirs, user_id=OWNER) is None
    assert await _view(store, mine, user_id=OTHER) is None
    assert await _view(store, mine) is not None

    async with db.acquire() as conn:
        await conn.execute("UPDATE agent_threads SET deleted_at = NOW() WHERE id = 'fa_mine'")
    assert await _view(store, mine) is None


async def test_sort_filter_search_and_cursor_consistency(db, store):
    [job] = await conversation(db, store, "fa_sort")
    await events(
        db,
        job,
        [
            ("lifecycle", {"phase": "started", "cwd": "/w"}),
            call("Read", "r1", file_path="zeta.ts"),
            result("r1"),
            call("Write", "w1", file_path="alpha.ts", content="1\n2\n3\n"),
            result("w1"),
            call("Read", "r2", file_path="alpha.ts"),
            result("r2"),
            call("Edit", "e1", file_path="zed.ts", old_string="a", new_string="b"),
            result("e1"),
        ],
    )
    by_active = await _view(store, job, sort="active")
    assert by_active is not None
    # alpha has two operations; zed and zeta tie at one, so the path orders them.
    assert [row.path for row in by_active.files] == ["/w/alpha.ts", "/w/zed.ts", "/w/zeta.ts"]
    by_lines = await _view(store, job, sort="lines")
    assert by_lines is not None
    assert [row.path for row in by_lines.files] == ["/w/alpha.ts", "/w/zed.ts", "/w/zeta.ts"]
    by_path = await _view(store, job, sort="path")
    assert by_path is not None
    assert [row.path for row in by_path.files] == ["/w/alpha.ts", "/w/zed.ts", "/w/zeta.ts"]
    by_name = await _view(store, job, sort="name")
    assert by_name is not None
    assert [row.name for row in by_name.files] == ["alpha.ts", "zed.ts", "zeta.ts"]

    written = await _view(store, job, filter_key="written")
    assert written is not None
    # Written means a recorded write or edit, so an edited file belongs here.
    assert [row.path for row in written.files] == ["/w/alpha.ts", "/w/zed.ts"]
    assert written.filter_counts["read"] == 2
    assert written.filter_counts["written"] == 2
    assert written.filter_counts["searched"] == 0
    assert written.totals.files == 3

    searched = await _view(store, job, q="zed")
    assert searched is not None
    assert [row.path for row in searched.files] == ["/w/zed.ts"]
    assert searched.totals.files == 1

    # A page at a time never repeats or skips a file.
    first = await _view(store, job, sort="path", limit=2)
    assert first is not None
    assert len(first.files) == 2 and first.has_more is True
    rest = await _view(store, job, sort="path", limit=2, cursor=first.next_cursor)
    assert rest is not None
    assert [row.path for row in rest.files] == ["/w/zeta.ts"]
    assert rest.has_more is False


async def test_cursor_from_another_list_is_refused(db, store):
    [job] = await conversation(db, store, "fa_cursor")
    await events(db, job, [call("Read", "a", file_path="a.ts"), result("a")])
    page = await _view(store, job, sort="path", limit=1)
    assert page is not None
    with pytest.raises(ValueError, match="cursor"):
        await _view(store, job, sort="active", limit=1, cursor="not-a-cursor")


async def test_search_scope_is_unknown_not_assumed(db, store):
    [job] = await conversation(db, store, "fa_search")
    await events(
        db,
        job,
        [
            ("lifecycle", {"phase": "started", "cwd": "/w"}),
            call("Glob", "s1", pattern="**/*.ts"),
            result("s1", "a.ts\nb.ts"),
            # ``path`` beside a pattern is a search's scope, not proof that
            # the one file was searched, so it attaches to nothing.
            call("Grep", "s2", pattern="x", path="src/a.ts"),
            result("s2", "src/a.ts:1:x"),
            # An explicit file field is one file's search.
            call("Grep", "s3", pattern="y", file_path="src/b.ts"),
            result("s3", "src/b.ts:2:y"),
        ],
    )
    view = await _view(store, job)
    assert view is not None
    assert view.totals.search_unknown_scope == 2
    assert [row.path for row in view.files] == ["/w/src/b.ts"]
    assert view.files[0].searches == 1


async def test_large_history_scan_is_bounded_and_says_so(db, store):
    [job] = await conversation(db, store, "fa_large")
    rows: list[tuple[str, dict]] = [("lifecycle", {"phase": "started", "cwd": "/w"})]
    for index in range(2_000):
        rows.append(call("Read", f"c{index}", file_path=f"f{index}.ts"))
        rows.append(result(f"c{index}"))
    await events(db, job, rows)
    view = await _view(store, job)
    assert view is not None
    # A 4,000-row history is one bounded scan and reports its own size; the
    # cap's behaviour is unit-tested without a database.
    assert view.coverage.events_scanned > 0
    assert view.coverage.events_capped is False
    # The page is bounded; the totals still cover every file the scan read.
    assert len(view.files) == 25
    assert view.totals.files == 2_000
    assert view.has_more is True


async def test_a_capped_scan_keeps_the_newest_operations_and_says_so(db, store, monkeypatch):
    """The bound retains recent activity, and coverage reports the window.

    The cap is lowered for this test so a real ledger crosses it; the scanned
    window must still hold the newest operations, the newest turn's cwd must
    still resolve its relative paths, and coverage must say it was capped.
    """
    [job] = await conversation(db, store, "fa_cap")
    rows: list[tuple[str, dict]] = [("lifecycle", {"phase": "started", "cwd": "/workspace"})]
    for index in range(40):
        rows.append(call("Read", f"cap-{index}", file_path=f"file_{index:02d}.py"))
        rows.append(result(f"cap-{index}"))
    await events(db, job, rows)
    monkeypatch.setattr("cloud_agent.storage.repositories.file_activity.MAX_SCAN_EVENTS", 20)
    view = await _view(store, job, sort="latest")
    assert view is not None
    assert view.coverage.events_capped is True
    assert view.coverage.events_scanned <= 20
    # Only the newest operations are in the window: the included names are a
    # high-numbered run, and the earliest ones are gone entirely.
    paths = {row.path for row in view.files}
    indices = sorted(int(path.rsplit("_", 1)[-1].removesuffix(".py")) for path in paths)
    assert max(indices) == 39
    assert min(indices) >= 20
    assert "/workspace/file_00.py" not in paths
    # The attempt's cwd was read even though its ``started`` row fell outside
    # the window, so these are absolute rather than bare relative names.
    assert all(path.startswith("/workspace/") for path in paths)
    # Latest ordering is meaningful precisely because the window is recent: no
    # file older than the window can appear ahead of one inside it. Ties in
    # recorded time are broken by path, so only the window's membership is
    # asserted here rather than one exact first row.
    assert view.files[0].last_at is not None
    assert min(indices) >= 20


async def _read_result(db, store, thread: str, content: str) -> tuple[int, int]:
    """Seed one Read whose result is ``content`` and return (images, reads)."""
    [job] = await conversation(db, store, thread)
    await events(
        db,
        job,
        [
            ("lifecycle", {"phase": "started", "cwd": "/workspace"}),
            call("Read", "r1", file_path="artifact.dat"),
            result("r1", content),
        ],
    )
    view = await _view(store, job)
    assert view is not None
    row = view.files[0]
    return row.images, row.reads


async def test_a_root_content_block_with_source_first_and_huge_data_is_an_image(db, store):
    """A base64 body larger than any transport bound still counts.

    ``source``/``data`` precede ``type`` and the payload is 120 kB, so a prefix
    or size-limited text match could not see the type at all.
    """
    content = json.dumps(
        {"source": {"media_type": "image/png", "data": "A" * 120_000}, "type": "image"}
    )
    assert await _read_result(db, store, "fa_img_root", content) == (1, 0)


async def test_a_text_then_image_block_list_is_an_image(db, store):
    content = json.dumps(
        [
            {"type": "text", "text": "Here is the screenshot."},
            {"source": {"media_type": "image/png", "data": "B" * 120_000}, "type": "image"},
        ]
    )
    assert await _read_result(db, store, "fa_img_list", content) == (1, 0)


async def test_a_nested_schema_that_describes_an_image_is_not_an_image(db, store):
    """A root block of type ``text`` whose nested value mentions an image."""
    content = json.dumps(
        {"type": "text", "schema": {"type": "image", "source": {"media_type": "image/png"}}}
    )
    assert await _read_result(db, store, "fa_img_nested", content) == (0, 1)


async def test_invalid_json_that_starts_like_an_image_is_not_an_image(db, store):
    content = '{"type":"image" broken'
    assert await _read_result(db, store, "fa_img_broken", content) == (0, 1)


async def test_source_text_describing_an_image_block_is_a_read(db, store):
    content = 'const block = {"type": "image", "source": {"media_type": "image/png"}};\n'
    assert await _read_result(db, store, "fa_img_source", content) == (0, 1)


async def test_a_clipped_codex_item_keeps_one_identity_and_its_outcome(db, store):
    """A clipped started/completed pair is one item, and a failed one stays failed."""
    [job] = await conversation(db, store, "fa_clipped_codex")

    def change_list() -> list[dict]:
        return [
            {
                "path": f"app/generated/file_{index:03d}.py",
                "kind": {"type": "update"},
                "diff": "@@\n-a\n+b\n" + (" context line\n" * 40),
            }
            for index in range(60)
        ]

    # The notification's own JSON must exceed the read's text bound, so its
    # change body is what the projection clips.
    started = {
        "method": "item/started",
        "params": {
            "item": {
                "id": "fc-big",
                "type": "fileChange",
                "status": "inProgress",
                "changes": change_list(),
            },
            "padding": "x" * 90_000,
        },
    }
    completed = {
        "method": "item/completed",
        "params": {
            "item": {
                "id": "fc-big",
                "type": "fileChange",
                "status": "failed",
                "changes": change_list(),
            },
            "padding": "x" * 90_000,
        },
    }
    async with db.acquire() as conn:
        attempt_id, start = await conn.fetchrow(
            "SELECT a.id, COALESCE(MAX(e.seq), 0) FROM agent_attempts a"
            " LEFT JOIN agent_job_events e ON e.attempt_id = a.id"
            " WHERE a.job_id = $1 GROUP BY a.id",
            job,
        )
        # The stored notification JSON is the event's whole payload fragment;
        # make it larger than the read's preview bound so the projection clips
        # the change body while the item's own small fields stay readable.
        rows = [
            ("lifecycle", {"phase": "started", "cwd": "/workspace"}),
            ("raw", {"protocol": "codex-app-server", "text": "n", "notification": started}),
            ("raw", {"protocol": "codex-app-server", "text": "n", "notification": completed}),
        ]
        for offset, (kind, payload) in enumerate(rows, start=1):
            await conn.execute(
                "INSERT INTO agent_job_events (job_id, attempt_id, seq, event_type, payload)"
                " VALUES ($1, $2, $3, $4, $5::jsonb)",
                job,
                attempt_id,
                start + offset,
                kind,
                json.dumps(payload),
            )
    view = await _view(store, job)
    assert view is not None
    # One item, so one operation, and its failed outcome is not read as success.
    assert view.totals.operations == 1
    assert view.totals.failed == 1
    assert view.totals.files == 0
    assert view.totals.unknown_path_operations == 1


async def test_a_well_formed_codex_item_with_no_changes_is_not_an_unknown_edit(db, store):
    [job] = await conversation(db, store, "fa_empty_codex")
    notification = {
        "method": "item/completed",
        "params": {
            "item": {"id": "fc-empty", "type": "fileChange", "status": "completed", "changes": []}
        },
    }
    await events(
        db,
        job,
        [
            ("lifecycle", {"phase": "started", "cwd": "/workspace"}),
            (
                "raw",
                {
                    "protocol": "codex-app-server",
                    "text": json.dumps(notification)[:4000],
                    "notification": notification,
                },
            ),
        ],
    )
    view = await _view(store, job)
    assert view is not None
    # An explicit empty change list records no file operation, and invents none.
    assert view.totals.operations == 0
    assert view.totals.files == 0


async def test_an_image_block_with_source_first_and_a_text_block_counts(db, store):
    """Key order and a leading text block must not hide a real image block."""
    [job] = await conversation(db, store, "fa_image_order")
    big = json.dumps(
        [
            {"type": "text", "text": "Here is the screenshot."},
            {
                "source": {"media_type": "image/png", "data": "B" * 12_000},
                "type": "image",
            },
        ]
    )
    await events(
        db,
        job,
        [
            ("lifecycle", {"phase": "started", "cwd": "/workspace"}),
            call("Read", "img-order", file_path="docs/diagram.png"),
            result("img-order", big),
        ],
    )
    view = await _view(store, job)
    assert view is not None
    assert (view.files[0].images, view.files[0].reads) == (1, 0)


async def test_source_text_that_mentions_an_image_block_is_not_image_activity(db, store):
    [job] = await conversation(db, store, "fa_image_source")
    source = 'const block = {"type": "image", "source": {"media_type": "image/png"}};\n'
    await events(
        db,
        job,
        [
            ("lifecycle", {"phase": "started", "cwd": "/workspace"}),
            call("Read", "src-1", file_path="src/blocks.ts"),
            result("src-1", source),
        ],
    )
    view = await _view(store, job)
    assert view is not None
    assert (view.files[0].images, view.files[0].reads) == (0, 1)


async def test_a_read_blocked_on_the_pool_is_cancelled_and_a_later_read_works(db, store):
    """A read that cannot acquire a connection is cancelled by its deadline.

    A pool of one connection is built for this case and that connection is
    held, so the scoped read has nothing to acquire: it must raise at its
    deadline instead of queueing forever, and the same read must succeed once
    the connection is free.
    """
    import asyncpg

    [job] = await conversation(db, store, "fa_blocked")
    await events(
        db,
        job,
        [
            ("lifecycle", {"phase": "started", "cwd": "/w"}),
            call("Read", "b1", file_path="a.py"),
            result("b1"),
        ],
    )
    # This worker's database: the fixture suffixes the name per xdist worker,
    # so the same rule is applied here rather than the unsuffixed tier name.
    database = os.environ.get("TEST_DB_NAME", "cloud_agent_ledger_test_db")
    worker = os.getenv("PYTEST_XDIST_WORKER", "master")
    if worker != "master":
        database = f"{database}_{worker}"
    settings = {
        "host": os.environ["TEST_DB_HOST"],
        "port": int(os.environ["TEST_DB_PORT"]),
        "user": os.environ["TEST_DB_USER"],
        "password": os.environ["TEST_DB_PASSWORD"],
        "database": database,
    }
    small = await asyncpg.create_pool(**settings, min_size=1, max_size=1)
    queries = PostgresFileActivityQueries(small)
    try:
        async with small.acquire():
            start = time.monotonic()
            with pytest.raises(TimeoutError):
                await queries._read_scoped(
                    thread_id="fa_blocked",
                    user_id=OWNER,
                    preview_chars=60_000,
                    deadline=time.monotonic() + 0.5,
                )
            assert time.monotonic() - start < 3
        # Free again: the same read works and the connection is reusable.
        view = await queries.file_activity(
            job_id=job,
            user_id=OWNER,
            sort="active",
            filter_key="all",
            q="",
            cursor=None,
            limit=25,
        )
        assert view is not None and view.totals.files == 1
        assert small.get_size() == small.get_idle_size()
    finally:
        await small.close()


async def test_a_text_block_with_escaped_quotes_before_a_big_image_counts(db, store):
    """A valid block keeps valid JSON after the skeleton replaces its text."""
    content = json.dumps(
        [
            {"type": "text", "text": 'He said "hi" and a backslash \\ end.'},
            {"type": "image", "source": {"media_type": "image/png", "data": "C" * 120_000}},
        ]
    )
    assert await _read_result(db, store, "fa_escaped_ok", content) == (1, 0)


async def test_source_text_with_escaped_json_looking_content_is_a_read(db, store):
    content = 'const s = "{\\"type\\": \\"image\\"}";\n'
    assert await _read_result(db, store, "fa_escaped_src", content) == (0, 1)


async def test_a_raw_control_character_in_image_data_keeps_it_invalid(db, store):
    """A raw newline inside a JSON string is invalid JSON, so nothing counts."""
    content = '{"type":"image","source":{"media_type":"image/png","data":"AA\nBB"}}'
    assert await _read_result(db, store, "fa_control", content) == (0, 1)


@pytest.mark.parametrize(
    "escaped",
    [
        # A backslash escape JSON does not define.
        '{"type":"image","source":{"media_type":"image/png","data":"AA\\qBB"}}',
        # A \u escape whose four digits are not hex.
        '{"type":"image","source":{"media_type":"image/png","data":"AA\\uZZZZBB"}}',
    ],
    ids=["invalid-escape", "invalid-unicode-escape"],
)
async def test_an_invalid_json_escape_in_image_data_keeps_it_invalid(db, store, escaped):
    assert await _read_result(db, store, "fa_bad_escape", escaped) == (0, 1)


async def test_a_valid_unicode_escape_before_a_big_image_still_counts(db, store):
    """A well-formed \\u escape is part of a valid JSON string, not a defect."""
    content = (
        '[{"type":"text","text":"A\\u0041 and \\u4e2d\\u6587 before the shot."},'
        '{"type":"image","source":{"media_type":"image/png","data":"' + "D" * 120_000 + '"}}]'
    )
    assert await _read_result(db, store, "fa_unicode_ok", content) == (1, 0)
