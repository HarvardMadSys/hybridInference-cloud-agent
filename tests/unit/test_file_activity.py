"""File Activity's reducer: what a record proves, and what it does not.

These are the rules the feature must not quietly break, each asserted against a
recorded shape from a pinned runtime rather than against a made-up one:
evidence-only classification, unknown-not-zero deltas, lexical path
normalization, per-call dedup, and totals that never pretend to be pages.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from cloud_agent.file_activity.aggregate import (
    build_operations_page,
    build_view,
    group_files,
    sort_rows,
)
from cloud_agent.file_activity.cursor import CursorError, decode_cursor, encode_cursor, list_scope
from cloud_agent.file_activity.models import (
    FileActivityCursor,
    FileOperation,
    OperationsCursor,
)
from cloud_agent.file_activity.normalize import (
    ACTION_TOOL_NAMES,
    _diff_deltas,
    classify,
    codex_operations,
    file_labels,
    line_count,
    normalize_path,
    parse_patch,
    tool_operations,
)
from cloud_agent.file_activity.scan import scan_operations
from cloud_agent.storage._sql.file_activity import ACTION_TOOL_NAMES as SQL_TOOL_NAMES

NOW = datetime(2026, 10, 31, 9, 0, tzinfo=UTC)


def op(
    path: str | None,
    kind: str = "read",
    *,
    status: str = "succeeded",
    added: int | None = None,
    removed: int | None = None,
    call: str = "c",
    event: int = 1,
    at: datetime | None = None,
    attempt: int = 1,
    subagent: bool = False,
) -> FileOperation:
    return FileOperation(
        call_id=call,
        event_id=event,
        result_event_id=event + 1 if status == "succeeded" else None,
        attempt_id=attempt,
        attempt_no=attempt,
        job_id=f"job-{attempt}",
        turn_no=attempt,
        seq=event,
        at=at or NOW,
        tool="Read",
        action=kind,
        kind=kind,
        status=status,
        path=path,
        name=path.rsplit("/", 1)[-1] if path else None,
        added=added,
        removed=removed,
        subagent=subagent,
    )


@pytest.mark.parametrize(
    ("tool", "kind", "action"),
    [
        ("Read", "read", "read"),
        ("read_file", "read", "read"),
        ("mcp__files__read", "read", "read"),
        ("Write", "write", "write"),
        ("file.write", "write", "write"),
        ("Edit", "edit", "edit"),
        ("MultiEdit", "edit", "edit"),
        ("str_replace_editor", "edit", "edit"),
        ("apply_patch", "edit", "apply_patch"),
        ("applypatch", "edit", "apply_patch"),
        ("Grep", "search", "search"),
        ("glob", "search", "search"),
        ("view_image", "image", "image"),
    ],
)
def test_classification_reads_exact_recorded_tool_names(tool, kind, action):
    assert classify(tool) == (kind, action)


@pytest.mark.parametrize(
    "tool",
    ["Bash", "shell", "exec_command", "task", "run_command", "read_later", "", None, 7],
)
def test_a_shell_or_unknown_tool_is_never_file_activity(tool):
    assert classify(tool) == (None, "unknown")


def test_the_sql_projection_and_the_reducer_agree_on_every_tool_name():
    """The SQL prefilter and the classifier must not drift apart."""
    assert set(SQL_TOOL_NAMES) == set(ACTION_TOOL_NAMES)


def test_a_bash_command_that_mentions_paths_produces_no_operation():
    payload = {
        "id": 1,
        "attempt_id": 1,
        "job_id": "j",
        "tool": "Bash",
        "input": {"command": "rm -rf src/app.ts && echo /workspace/x"},
    }
    assert tool_operations(payload, "/workspace") == []


def test_line_count_ends_a_file_at_its_newline():
    assert line_count("") == 0
    assert line_count("a") == 1
    assert line_count("a\n") == 1
    assert line_count("a\nb") == 2
    assert line_count("\n") == 1


def test_paths_normalize_lexically_and_keep_their_roots():
    assert normalize_path("./a/../b.py", "/workspace") == ("/workspace/b.py", "cwd_relative")
    assert normalize_path("/abs//a.py", "/workspace") == ("/abs/a.py", "absolute")
    assert normalize_path("a.py", None) == ("a.py", "relative")
    assert normalize_path("a.py", "relative-root") == ("a.py", "relative")
    assert normalize_path("C:\\work\\A.py", "/workspace") == ("C:/work/A.py", "absolute")


def test_two_roots_that_end_the_same_way_stay_apart():
    rows = group_files([op("/one/a.py"), op("/two/a.py"), op("a.py")])
    assert sorted(row.path for row in rows) == ["/one/a.py", "/two/a.py", "a.py"]


def test_case_is_preserved_rather_than_folded():
    rows = group_files([op("/w/A.py"), op("/w/a.py")])
    assert sorted(row.path for row in rows) == ["/w/A.py", "/w/a.py"]


def test_labels_come_from_the_extension_only():
    assert file_labels("/w/a.ts") == ("TS", "code")
    assert file_labels("/w/photo.png") == ("PNG", "image")
    assert file_labels("/w/Makefile") == ("FILE", "other")
    assert file_labels("/w/a.unknownext") == ("UNKNOW", "other")


def test_a_write_over_an_existing_file_has_an_unknown_removed_side():
    payload = {
        "id": 1,
        "attempt_id": 1,
        "job_id": "j",
        "tool": "Write",
        "input": {"file_path": "a.py"},
        "content_lines": 3,
        "result_id": 2,
        "result_error": False,
    }
    [operation] = tool_operations(payload, "/w")
    assert (operation.added, operation.removed) == (3, None)


def test_a_failed_write_has_no_delta_and_no_success():
    payload = {
        "id": 1,
        "attempt_id": 1,
        "job_id": "j",
        "tool": "Write",
        "input": {"file_path": "a.py"},
        "content_lines": 3,
        "result_id": 2,
        "result_error": True,
    }
    [operation] = tool_operations(payload, "/w")
    assert operation.status == "failed"
    assert (operation.added, operation.removed) == (None, None)


def test_an_unanswered_call_is_pending_while_running_and_otherwise_unrecorded():
    base = {
        "id": 1,
        "attempt_id": 1,
        "job_id": "j",
        "tool": "Write",
        "input": {"file_path": "a.py"},
        "content_lines": 1,
    }
    [pending] = tool_operations({**base, "attempt_running": True}, "/w")
    [missing] = tool_operations({**base, "attempt_running": False}, "/w")
    assert pending.status == "pending"
    assert missing.status == "not_recorded"
    assert pending.added is None and missing.added is None


def test_replace_all_leaves_the_delta_unknown():
    payload = {
        "id": 1,
        "attempt_id": 1,
        "job_id": "j",
        "tool": "Edit",
        "input": {"file_path": "a.py", "replace_all": True},
        "edit_added_lines": 1,
        "edit_removed_lines": 1,
        "result_id": 2,
        "result_error": False,
    }
    [operation] = tool_operations(payload, "/w")
    assert (operation.added, operation.removed) == (None, None)


def test_multiedit_sums_its_pairs_and_refuses_a_clipped_one():
    payload = {
        "id": 1,
        "attempt_id": 1,
        "job_id": "j",
        "tool": "MultiEdit",
        "input": {"file_path": "a.py"},
        # The projection puts the pairs at the payload's top level, each with
        # the whole side's length, and clips only its own copy.
        "edits": [
            {"old_string": "a", "new_string": "b", "replace_all": False, "new_chars": 1},
            {"old_string": "c\nd", "new_string": "e", "replace_all": False, "new_chars": 1},
        ],
        "result_id": 2,
        "result_error": False,
    }
    [operation] = tool_operations(payload, "/w")
    # "c\nd" is two lines removed, plus "a": three.
    assert (operation.added, operation.removed) == (2, 3)

    clipped = {
        **payload,
        "edits": [
            {"old_string": "a", "new_string": "b", "replace_all": False, "new_chars": 99},
        ],
    }
    [unknown] = tool_operations(clipped, "/w")
    assert (unknown.added, unknown.removed) == (None, None)


def test_a_replace_all_pair_leaves_a_multiedit_unknown():
    payload = {
        "id": 1,
        "attempt_id": 1,
        "job_id": "j",
        "tool": "MultiEdit",
        "input": {"file_path": "a.py"},
        "edits": [
            {
                "old_string": "a",
                "new_string": "b",
                "replace_all": True,
                "new_chars": 1,
                "old_chars": 1,
            },
        ],
        "result_id": 2,
        "result_error": False,
    }
    [operation] = tool_operations(payload, "/w")
    assert (operation.added, operation.removed) == (None, None)


def test_a_multi_file_patch_is_one_operation_per_file():
    patch = (
        "*** Begin Patch\n"
        "*** Add File: new.py\n"
        "+x\n"
        "*** Update File: old.py\n"
        "*** Move to: moved.py\n"
        "@@\n"
        "-a\n"
        "+b\n"
        "*** End Patch\n"
    )
    sections = parse_patch(patch)
    assert [(s.change, s.path, s.added, s.removed, s.rename_to) for s in sections] == [
        ("add", "new.py", 1, 0, None),
        ("update", "old.py", 1, 1, "moved.py"),
    ]
    payload = {
        "id": 5,
        "attempt_id": 1,
        "job_id": "j",
        "tool": "apply_patch",
        "patch": patch,
        "patch_source_chars": len(patch),
        "result_id": 6,
        "result_error": False,
    }
    built = tool_operations(payload, "/w")
    assert [(o.path, o.patch_index, o.patch_files, o.action) for o in built] == [
        ("/w/new.py", 1, 2, "apply_patch"),
        ("/w/old.py", 2, 2, "rename"),
    ]


def test_a_deleted_file_in_a_patch_has_an_unknown_removed_side():
    patch = "*** Begin Patch\n*** Delete File: old.py\n-a\n-b\n*** End Patch\n"
    sections = parse_patch(patch)
    assert sections[0].removed is None


def test_a_clipped_patch_is_an_edit_without_a_path_or_delta():
    payload = {
        "id": 5,
        "attempt_id": 1,
        "job_id": "j",
        "tool": "apply_patch",
        "patch": "*** Begin Patch\n*** Add File: a.py\n+",
        "patch_source_chars": 999_999,
        "result_id": 6,
        "result_error": False,
    }
    [operation] = tool_operations(payload, "/w")
    assert operation.clipped is True
    assert operation.path is None
    assert (operation.added, operation.removed) == (None, None)


def test_a_search_with_a_pattern_and_directory_attaches_to_no_file():
    payload = {
        "id": 1,
        "attempt_id": 1,
        "job_id": "j",
        "tool": "Grep",
        "input": {"pattern": "TODO", "path": "src", "glob": "*.ts"},
        "result_id": 2,
        "result_error": False,
    }
    [operation] = tool_operations(payload, "/w")
    assert operation.kind == "search"
    assert operation.path is None


def test_a_search_names_one_file_only_through_an_explicit_file_field():
    payload = {
        "id": 1,
        "attempt_id": 1,
        "job_id": "j",
        "tool": "Grep",
        "input": {"pattern": "TODO", "file_path": "src/a.ts"},
        "result_id": 2,
        "result_error": False,
    }
    [operation] = tool_operations(payload, "/w")
    assert operation.path == "/w/src/a.ts"


def test_a_read_whose_result_carries_an_image_is_image_activity():
    payload = {
        "id": 1,
        "attempt_id": 1,
        "job_id": "j",
        "tool": "Read",
        "input": {"file_path": "shot.dat"},
        "result_id": 2,
        "result_error": False,
        "result_content": '{"type":"image","source":{"media_type":"image/png"}}',
    }
    [operation] = tool_operations(payload, "/w")
    assert operation.kind == "image"


def test_an_image_extension_alone_is_not_image_activity():
    payload = {
        "id": 1,
        "attempt_id": 1,
        "job_id": "j",
        "tool": "Read",
        "input": {"file_path": "logo.png"},
        "result_id": 2,
        "result_error": False,
        "result_content": '# a document that mentions {"type": "image"}',
    }
    [operation] = tool_operations(payload, "/w")
    assert operation.kind == "read"
    assert file_labels(operation.path or "") == ("PNG", "image")


def test_a_missing_path_is_an_operation_that_attaches_to_nothing():
    payload = {
        "id": 1,
        "attempt_id": 1,
        "job_id": "j",
        "tool": "Read",
        "input": {},
        "result_id": 2,
        "result_error": False,
    }
    [operation] = tool_operations(payload, "/w")
    assert operation.path is None
    assert operation.path_basis == "unknown"


def test_unicode_and_long_paths_survive_unchanged():
    long_path = "/w/" + "深い/" * 200 + "ファイル.py"
    rows = group_files([op(long_path)])
    assert rows[0].path == long_path
    assert rows[0].name == "ファイル.py"


def test_codex_file_change_reads_only_its_own_notification():
    item = {
        "method": "item/completed",
        "params": {
            "item": {
                "type": "fileChange",
                "status": "completed",
                "changes": [
                    {"path": "a.py", "kind": {"type": "add"}, "diff": "+x\n+y\n"},
                    {"path": "b.py", "kind": {"type": "delete"}},
                    {"path": "c.py", "kind": {"type": "update"}, "diff": "@@\n-a\n+b\n"},
                ],
            }
        },
    }
    payload = {
        "id": 3,
        "attempt_id": 1,
        "job_id": "j",
        "file_change_method": "item/completed",
        "file_change": __import__("json").dumps(item),
    }
    built = codex_operations(payload, "/w")
    by_path = {o.path: o for o in built}
    assert (by_path["/w/a.py"].added, by_path["/w/a.py"].removed) == (2, 0)
    # No diff was recorded for the delete: its removed side is unknown.
    assert by_path["/w/b.py"].removed is None
    assert (by_path["/w/c.py"].added, by_path["/w/c.py"].removed) == (1, 1)
    assert by_path["/w/a.py"].kind == "write"
    assert by_path["/w/c.py"].kind == "edit"


def test_codex_shell_command_text_is_not_scanned_for_paths():
    payload = {
        "id": 3,
        "attempt_id": 1,
        "job_id": "j",
        "file_change_method": "item/completed",
        "file_change": '{"method":"item/completed","params":{"item":'
        '{"type":"commandExecution","command":"rm a.py"}}}',
    }
    assert codex_operations(payload, "/w") == []


def test_the_same_call_recorded_twice_counts_once():
    row = {
        "event_type": "tool_use",
        "attempt_id": 1,
        "result_id": 2,
        "result_error": False,
        "payload": {
            "id": 1,
            "attempt_id": 1,
            "job_id": "j",
            "tool": "Read",
            "tool_id": "call-1",
            "input": {"file_path": "a.py"},
        },
    }
    result = scan_operations([row, row], thread_id="t")
    assert len(result.operations) == 1


def test_two_calls_with_their_own_ids_are_two_reads():
    def row(event: int, call: str) -> dict:
        return {
            "event_type": "tool_use",
            "attempt_id": 1,
            "result_id": event + 10,
            "result_error": False,
            "payload": {
                "id": event,
                "attempt_id": 1,
                "job_id": "j",
                "tool": "Read",
                "tool_id": call,
                "input": {"file_path": "a.py"},
            },
        }

    result = scan_operations([row(1, "a"), row(2, "b")], thread_id="t")
    assert len(result.operations) == 2
    assert group_files(result.operations)[0].reads == 2


def test_relative_paths_resolve_against_their_own_attempts_cwd():
    lifecycle = {
        "event_type": "lifecycle",
        "attempt_id": 1,
        "payload": {"id": 1, "attempt_id": 1, "job_id": "j", "cwd": "/one"},
    }
    use = {
        "event_type": "tool_use",
        "attempt_id": 1,
        "result_id": 3,
        "result_error": False,
        "payload": {
            "id": 2,
            "attempt_id": 1,
            "job_id": "j",
            "tool": "Read",
            "tool_id": "c1",
            "input": {"file_path": "a.py"},
        },
    }
    result = scan_operations([lifecycle, use], thread_id="t")
    assert result.operations[0].path == "/one/a.py"
    assert result.operations[0].path_basis == "cwd_relative"


def test_filter_counts_and_totals_describe_the_search_not_the_page():
    rows = group_files(
        [
            op("/w/a.py", "read", call="a"),
            op("/w/a.py", "write", added=5, call="b", event=3),
            op("/w/b.py", "search", call="c", event=5),
        ]
    )
    view = build_view(
        thread_id="t",
        operations=[
            op("/w/a.py", "read", call="a"),
            op("/w/a.py", "write", added=5, call="b", event=3),
            op("/w/b.py", "search", call="c", event=5),
        ],
        turns=1,
        attempts=1,
        events_scanned=6,
        capped=False,
        sort="active",
        filter_key="all",
        q="",
        cursor=None,
        limit=1,
        scope="s",
    )
    assert view.totals.files == 2 and view.totals.operations == 3
    assert view.filter_counts == {"all": 2, "read": 1, "written": 1, "searched": 1, "images": 0}
    assert len(view.files) == 1 and view.has_more is True
    assert rows  # grouping is the same input the view used


def test_unknown_deltas_are_counted_apart_from_zero():
    rows = group_files(
        [
            op("/w/a.py", "write", added=4, removed=None, call="a"),
            op("/w/a.py", "write", added=None, removed=None, call="b", event=3),
        ]
    )
    row = rows[0]
    assert row.added == 4
    assert row.added_unknown_ops == 1
    assert row.removed_unknown_ops == 2


def test_a_retry_attempt_keeps_both_real_operations():
    rows = group_files(
        [op("/w/a.py", "read", call="a", attempt=1), op("/w/a.py", "read", call="b", attempt=2)]
    )
    assert rows[0].reads == 2
    assert rows[0].attempts == 2


def test_superseded_and_subagent_operations_are_marked_not_dropped():
    operation = op("/w/a.py", "read").model_copy(update={"superseded": True, "subagent": True})
    row = group_files([operation])[0]
    assert operation.superseded is True
    assert row.subagent_operations == 1


def test_every_sort_is_total_and_deterministic():
    rows = group_files(
        [
            op("/w/b.py", "read", call="1", event=1),
            op("/w/a.py", "read", call="2", event=2),
            op("/w/c.py", "read", call="3", event=3),
        ]
    )
    for sort in ("active", "lines", "latest", "path", "name"):
        first = [row.path for row in sort_rows(rows, sort)]
        second = [row.path for row in sort_rows(list(reversed(rows)), sort)]
        assert first == second


def test_paging_never_repeats_or_skips_a_file():
    operations = [
        op(f"/w/f{index:02d}.py", "read", call=str(index), event=index) for index in range(7)
    ]
    seen: list[str] = []
    cursor = None
    for _ in range(10):
        view = build_view(
            thread_id="t",
            operations=operations,
            turns=1,
            attempts=1,
            events_scanned=7,
            capped=False,
            sort="path",
            filter_key="all",
            q="",
            cursor=cursor,
            limit=3,
            scope="s",
        )
        seen.extend(row.path for row in view.files)
        if not view.has_more:
            break
        cursor = decode_cursor(view.next_cursor, FileActivityCursor, "s")
    assert seen == [f"/w/f{index:02d}.py" for index in range(7)]


def test_operations_page_keeps_retries_and_reads_apart():
    operations = [
        op("/w/a.py", "read", call="a", event=1, at=NOW),
        op("/w/a.py", "read", call="b", event=3, at=NOW + timedelta(seconds=5)),
        op("/w/b.py", "read", call="c", event=5, at=NOW),
    ]
    page = build_operations_page(
        thread_id="t", path="/w/a.py", operations=operations, cursor=None, limit=10, scope="s"
    )
    assert page.total == 2
    assert [operation.call_id for operation in page.operations] == ["b", "a"]


def test_operations_page_continues_without_repeating():
    operations = [
        op("/w/a.py", "read", call=str(index), event=index, at=NOW + timedelta(seconds=index))
        for index in range(5)
    ]
    first = build_operations_page(
        thread_id="t", path="/w/a.py", operations=operations, cursor=None, limit=2, scope="s"
    )
    cursor = decode_cursor(first.next_cursor, OperationsCursor, "s")
    rest = build_operations_page(
        thread_id="t", path="/w/a.py", operations=operations, cursor=cursor, limit=10, scope="s"
    )
    ids = [operation.event_id for operation in first.operations + rest.operations]
    assert ids == [4, 3, 2, 1, 0]


def test_a_cursor_is_bound_to_its_scope():
    cursor = encode_cursor(
        FileActivityCursor(scope=list_scope(thread="a"), sort_number=1, key="", path="/w/a.py")
    )
    assert decode_cursor(cursor, FileActivityCursor, list_scope(thread="a")) is not None
    with pytest.raises(CursorError):
        decode_cursor(cursor, FileActivityCursor, list_scope(thread="b"))


def test_a_malformed_or_oversized_cursor_is_refused():
    for value in ("", "not-base64!!", "e30", "x" * 3000):
        with pytest.raises(CursorError):
            decode_cursor(value, FileActivityCursor, "s")


def test_coverage_reports_its_window_and_cap():
    view = build_view(
        thread_id="t",
        operations=[op("/w/a.py")],
        turns=2,
        attempts=3,
        events_scanned=4,
        capped=True,
        sort="active",
        filter_key="all",
        q="",
        cursor=None,
        limit=10,
        scope="s",
    )
    assert view.coverage.turns == 2
    assert view.coverage.attempts == 3
    assert view.coverage.events_scanned == 4
    assert view.coverage.events_capped is True
    assert view.coverage.window_first_at == NOW


def test_semantics_never_claim_a_net_git_diff():
    view = build_view(
        thread_id="t",
        operations=[],
        turns=0,
        attempts=0,
        events_scanned=0,
        capped=False,
        sort="active",
        filter_key="all",
        q="",
        cursor=None,
        limit=10,
        scope="s",
    )
    assert view.semantics.line_changes == "cumulative_recorded"
    assert view.semantics.net_git_diff == "not_read"
    assert view.semantics.search_scope == "explicit_path_only"
    assert view.semantics.image_evidence == "explicit_only"


# ── Review regressions ──────────────────────────────────────────────────────


def _codex_row(event_id: int, seq: int, method: str, item: dict) -> dict:
    notification = {"method": method, "params": {"item": item}}
    return {
        "event_type": "raw",
        "attempt_id": 1,
        "payload": {
            "id": event_id,
            "attempt_id": 1,
            "seq": seq,
            "job_id": "j",
            "file_change_method": method,
            "file_change": __import__("json").dumps(notification),
        },
    }


def test_one_codex_item_announced_three_times_is_one_operation():
    """item/started, item/updated and item/completed are three rows, one item."""
    item_started = {
        "id": "fc-1",
        "type": "fileChange",
        "status": "inProgress",
        "changes": [{"path": "a.py", "kind": {"type": "update"}}],
    }
    item_updated = {
        **item_started,
        "changes": [{"path": "a.py", "kind": {"type": "update"}, "diff": "@@\n-a\n+b\n+c\n"}],
    }
    item_completed = {**item_updated, "status": "completed"}
    rows = [
        _codex_row(1, 1, "item/started", item_started),
        _codex_row(2, 2, "item/updated", item_updated),
        _codex_row(3, 3, "item/completed", item_completed),
    ]
    result = scan_operations(rows, thread_id="t")
    assert len(result.operations) == 1
    operation = result.operations[0]
    # The completed notification is the one that counts, with its own churn.
    assert operation.status == "succeeded"
    assert (operation.added, operation.removed) == (2, 1)


def test_a_duplicated_completed_codex_item_counts_once():
    item = {
        "id": "fc-2",
        "type": "fileChange",
        "status": "completed",
        "changes": [{"path": "b.py", "kind": {"type": "add"}, "diff": "+x\n+y\n"}],
    }
    rows = [_codex_row(1, 1, "item/completed", item), _codex_row(2, 2, "item/completed", item)]
    result = scan_operations(rows, thread_id="t")
    assert len(result.operations) == 1
    assert result.operations[0].added == 2


def test_two_codex_items_with_their_own_ids_stay_two_operations():
    first = {
        "id": "fc-a",
        "type": "fileChange",
        "status": "completed",
        "changes": [{"path": "a.py", "kind": {"type": "update"}, "diff": "+1\n"}],
    }
    second = {**first, "id": "fc-b", "changes": [{"path": "b.py", "kind": {"type": "update"}}]}
    rows = [
        _codex_row(1, 1, "item/completed", first),
        _codex_row(2, 2, "item/completed", second),
    ]
    result = scan_operations(rows, thread_id="t")
    # No attempt recorded a cwd, so the relative paths stay relative.
    assert sorted(op.path for op in result.operations if op.path) == ["a.py", "b.py"]


def test_a_clipped_codex_notification_is_reported_not_dropped():
    row = {
        "event_type": "raw",
        "attempt_id": 1,
        "payload": {
            "id": 9,
            "attempt_id": 1,
            "seq": 9,
            "job_id": "j",
            "tool_id": "fc-clipped",
            "file_change_method": "item/completed",
            "file_change": '{"method":"item/completed","params":{"item":{"id":"fc-cli',
            "file_change_clipped": True,
        },
    }
    result = scan_operations([row], thread_id="t")
    assert len(result.operations) == 1
    # The item is known, its files are not: no path is invented, and the
    # projection's clip is carried so the UI can say so.
    assert result.operations[0].path is None
    assert result.operations[0].status == "succeeded"


def test_codex_change_without_a_diff_keeps_both_sides_unknown():
    item = {
        "id": "fc-3",
        "type": "fileChange",
        "status": "completed",
        "changes": [{"path": "c.py", "kind": {"type": "update"}}],
    }
    [operation] = codex_operations(_codex_row(1, 1, "item/completed", item)["payload"], "/w")
    assert (operation.added, operation.removed) == (None, None)


def test_codex_change_with_an_explicitly_empty_diff_is_zero():
    item = {
        "id": "fc-4",
        "type": "fileChange",
        "status": "completed",
        "changes": [{"path": "d.py", "kind": {"type": "update"}, "diff": ""}],
    }
    [operation] = codex_operations(_codex_row(1, 1, "item/completed", item)["payload"], "/w")
    assert (operation.added, operation.removed) == (0, 0)


def test_a_read_of_json_mentioning_an_image_is_not_an_image_operation():
    payload = {
        "id": 1,
        "attempt_id": 1,
        "job_id": "j",
        "tool": "Read",
        "input": {"file_path": "schema.json"},
        "result_id": 2,
        "result_error": False,
        "result_content": '{"description": "a block of type image", "type": "text"}',
    }
    [operation] = tool_operations(payload, "/w")
    assert operation.kind == "read"


def test_a_read_whose_result_is_an_image_block_is_image_activity():
    payload = {
        "id": 1,
        "attempt_id": 1,
        "job_id": "j",
        "tool": "Read",
        "input": {"file_path": "shot.txt"},
        "result_id": 2,
        "result_error": False,
        "result_content": '{"type":"image","source":{"media_type":"image/png","data":"AA"}}',
    }
    [operation] = tool_operations(payload, "/w")
    assert operation.kind == "image"


def test_an_apply_patch_keeps_lines_that_start_with_plus_or_minus():
    patch = (
        "*** Begin Patch\n"
        "*** Update File: a.py\n"
        "@@\n"
        "++++ four pluses\n"
        "----- five minuses\n"
        "+ordinary\n"
        "-ordinary\n"
        "*** End Patch\n"
    )
    sections = parse_patch(patch)
    assert len(sections) == 1
    # The ++++/----- lines are content, not file headers: all counted.
    assert (sections[0].added, sections[0].removed) == (2, 2)


def test_a_unified_diff_with_file_headers_counts_only_hunk_content():
    diff = (
        "diff --git a/a.py b/a.py\n"
        "index 111..222 100644\n"
        "--- a/a.py\n"
        "+++ b/a.py\n"
        "@@ -1,2 +1,3 @@\n"
        "--- content beginning with two dashes\n"
        "+++ content beginning with two pluses\n"
        " context\n"
    )
    assert _diff_deltas(diff) == (1, 1)


def test_a_bare_codex_diff_without_a_hunk_header_still_counts():
    assert _diff_deltas("+one\n+two\n") == (2, 0)
    assert _diff_deltas("-one\n") == (0, 1)
    # An explicitly empty diff really is zero.
    assert _diff_deltas("") == (0, 0)
