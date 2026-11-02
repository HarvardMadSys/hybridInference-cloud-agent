"""Command executions: recorded shapes, parsing, identity and page consistency.

Everything here is built from the row shapes the storage projection yields, so
a test states the record it means rather than an execution the code invents.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from cloud_agent.command_executions import (
    MAX_COMMAND_CHARS,
    MAX_OUTPUT_PREVIEW,
    SLOW_COMMAND_MS,
    assemble_command_executions,
    command_executions_page,
    parse_program,
)

T0 = datetime(2026, 10, 30, 9, tzinfo=UTC)


def job(
    job_id: str = "j1",
    turn_no: int = 1,
    *,
    state: str = "succeeded",
    runtime: str = "claude-code",
    copied: bool = False,
) -> dict[str, Any]:
    return {
        "job_id": job_id,
        "turn_no": turn_no,
        "state": state,
        "runtime": runtime,
        "copied": copied,
    }


def call(
    event_id: int,
    *,
    attempt_id: int = 1,
    job_id: str = "j1",
    turn_no: int = 1,
    attempt_no: int = 1,
    call_id: str = "c1",
    command: str = "pytest -q",
    tool: str = "Bash",
    cwd: str | None = "/work",
    session: str | None = None,
    background: bool = False,
    duration_ms: int | None = None,
    attempt_status: str = "finished",
    second: int = 0,
) -> dict[str, Any]:
    return {
        "event_id": event_id,
        "attempt_id": attempt_id,
        "job_id": job_id,
        "turn_no": turn_no,
        "attempt_no": attempt_no,
        "attempt_status": attempt_status,
        "event_type": "tool_use",
        "seq": event_id,
        "at": T0 + timedelta(seconds=second),
        "tool_name": tool,
        "call_id": call_id,
        "command_text": command,
        "command_chars": len(command),
        "cwd": cwd,
        "session_id": session,
        "background": background,
        "duration_ms": duration_ms,
        "timing_source": "message" if duration_ms is not None else None,
    }


def result(
    event_id: int,
    *,
    attempt_id: int = 1,
    job_id: str = "j1",
    call_id: str = "c1",
    content: str = "ok",
    is_error: bool = False,
    exit_code: int | None = None,
    session: str | None = None,
    status: str | None = None,
    structured_output: dict[str, Any] | None = None,
    second: int = 1,
) -> dict[str, Any]:
    return {
        "event_id": event_id,
        "attempt_id": attempt_id,
        "job_id": job_id,
        "event_type": "tool_result",
        "seq": event_id,
        "at": T0 + timedelta(seconds=second),
        "tool_use_id": call_id,
        "command_text": None,
        "tool_name": None,
        "cwd": None,
        "session_id": session,
        "is_error": "true" if is_error else None,
        "exit_code": exit_code,
        "content": content,
        "content_chars": len(content),
        "status": status,
        "background": False,
        "structured_output": structured_output,
    }


def poll(
    event_id: int,
    *,
    attempt_id: int = 1,
    job_id: str = "j1",
    call_id: str = "poll-1",
    session: str = "s1",
    second: int = 2,
) -> dict[str, Any]:
    row = call(
        event_id,
        attempt_id=attempt_id,
        job_id=job_id,
        call_id=call_id,
        command="",
        session=session,
        second=second,
    )
    row["command_text"] = None
    return row


def build(rows: list[dict[str, Any]], jobs: list[dict[str, Any]] | None = None, total=None):
    return assemble_command_executions(
        thread_id="t1",
        generated_at=T0 + timedelta(minutes=5),
        jobs=jobs or [job()],
        rows=rows,
        executions_total=total,
    )


@pytest.mark.parametrize(
    ("command", "expected"),
    [
        ("pytest -q", "pytest"),
        ("/usr/bin/git status", "git"),
        ("cd /tmp && ls -la", "ls"),
        ("FOO=bar BAZ=1 make test", "make"),
        ("cd /work", "shell"),
        ("cd /work; cat a; sed -n 1,2p b", "cat"),
        ("env -u HOME FOO=1 timeout 30 npm run build", "npm"),
        ("sudo -u root systemctl restart nginx", "systemctl"),
        ("git log | head -5", "git"),
        ("uv run pytest -q && git diff", "uv"),
        ("source .venv/bin/activate", "shell"),
        ("cat <<'EOF' > x\nhello\nEOF", "cat"),
        ("python3 - <<'PY'\nprint(1)\nPY", "python3"),
        ("if true; then echo hi; fi", "shell"),
        ('for f in *.ts; do grep -n x "$f"; done', "shell"),
        ("echo $HOME", "echo"),
        ("  ", "shell"),
        ("bash -c 'npm test'", "bash"),
    ],
)
def test_the_parsed_program_skips_setup_and_names_the_subject(command, expected):
    assert parse_program(command)[0] == expected


@pytest.mark.parametrize(
    "command",
    ["cd /tmp && ls", "git status", "/usr/bin/make", "sudo git status"],
)
def test_resolved_heads_are_marked_executables(command):
    assert parse_program(command)[1] == "executable"


def test_a_command_ending_in_cd_is_grouped_as_shell_not_named_cd():
    assert parse_program("cd /tmp && FOO=1") == ("shell", "shell")


def test_success_failure_running_and_unknown_are_distinct():
    rows = [
        call(1, call_id="ok"),
        result(2, call_id="ok", exit_code=0),
        call(3, call_id="bad", second=3),
        result(4, call_id="bad", is_error=True, exit_code=2, second=4),
        call(5, call_id="run", second=5),
        call(6, call_id="gone", second=6),
    ]
    jobs = [job(state="succeeded")]
    assembly = build(rows, jobs)
    statuses = {record.id: record.status for record in assembly.records}
    assert statuses == {
        "1:ok": "completed",
        "1:bad": "failed",
        "1:run": "unknown",
        "1:gone": "unknown",
    }

    running = build(rows, [job(state="running")])
    statuses = {record.id: record.status for record in running.records}
    assert statuses["1:run"] == "running"


def test_a_missing_exit_code_is_unknown_and_not_zero():
    rows = [call(1), result(2, content="fine")]
    page = command_executions_page(build(rows))
    execution = page.executions[0]
    assert execution.status == "completed"
    assert execution.exit_code is None
    assert execution.tool_error is False


def test_a_tool_error_that_is_not_an_exit_code_still_fails():
    rows = [call(1), result(2, is_error=True)]
    page = command_executions_page(build(rows))
    assert page.executions[0].status == "failed"
    assert page.executions[0].exit_code is None


def test_a_delayed_result_and_later_polls_stay_one_execution():
    rows = [
        call(1, call_id="c1", session="s1", command="npm run dev", background=True),
        result(2, call_id="c1", content="started", session="s1", status="running"),
        poll(3, call_id="poll-1", session="s1"),
        result(4, call_id="poll-1", content="ready", session="s1"),
        poll(5, call_id="poll-2", session="s1"),
        result(6, call_id="poll-2", content="listening", session="s1", exit_code=0),
    ]
    page = command_executions_page(build(rows))
    assert page.summary.total == 1
    assert page.executions[0].id == "1:c1"
    assert page.executions[0].output_preview == "listening"
    assert page.executions[0].background is True
    assert page.executions[0].duration_basis == "events"


def test_a_running_poll_does_not_settle_the_execution():
    rows = [
        call(1, call_id="c1", session="s1", command="sleep 60"),
        poll(2, call_id="p1", session="s1"),
        result(3, call_id="p1", session="s1", content="still going", status="running"),
    ]
    page = command_executions_page(build(rows, [job(state="running")]))
    assert page.executions[0].status == "running"
    assert page.summary.running == 1


def test_duplicate_identities_are_merged_and_true_repeats_are_not():
    rows = [
        call(1, call_id="c1"),
        call(2, call_id="c1", cwd="/other"),
        result(3, call_id="c1", exit_code=0),
        # The same text under a new call id is a second, real execution.
        call(4, call_id="c2", second=4),
        result(5, call_id="c2", exit_code=0, second=5),
    ]
    page = command_executions_page(build(rows))
    assert page.summary.total == 2
    assert [execution.id for execution in page.executions] == ["1:c2", "1:c1"]
    assert page.executions[1].cwd == "/work"
    assert page.attention.repeats == {"command": "pytest -q", "count": 2}


def test_slow_cutoff_is_the_published_constant_and_a_zero_duration_counts():
    rows = [
        call(1, call_id="fast", duration_ms=0, second=0),
        result(2, call_id="fast", second=1),
        call(3, call_id="slow", duration_ms=SLOW_COMMAND_MS, second=2),
        result(4, call_id="slow", second=3),
    ]
    page = command_executions_page(build(rows), filter="slow")
    assert page.slow_cutoff_ms == SLOW_COMMAND_MS
    assert [execution.id for execution in page.executions] == ["1:slow"]
    assert page.counts.slow == 1
    assert page.counts.all == 2


def test_summary_uses_a_settled_denominator_and_calls_the_total_cumulative():
    rows = [
        call(1, call_id="a", duration_ms=1_500, second=0),
        result(2, call_id="a", exit_code=0, second=1),
        call(3, call_id="b", duration_ms=500, second=2),
        result(4, call_id="b", exit_code=1, second=3),
        call(5, call_id="c", second=4),
    ]
    page = command_executions_page(build(rows, [job(state="running")]))
    summary = page.summary
    assert summary.total == 3
    assert summary.settled == 2
    assert summary.failed == 1
    assert summary.failure_rate == pytest.approx(0.5)
    assert summary.cumulative_ms == 2_000
    assert summary.durations_known == 2
    assert summary.durations_missing == 1
    assert summary.duration_basis == "recorded"
    assert summary.running == 1


def test_failure_rate_is_null_rather_than_zero_with_no_denominator():
    page = command_executions_page(build([call(1)], [job(state="running")]))
    assert page.summary.settled == 0
    assert page.summary.failure_rate is None


def test_programs_rank_by_count_with_honest_shares():
    rows = [
        call(1, call_id="a", command="git status", second=0),
        call(2, call_id="b", command="git diff", second=1),
        call(3, call_id="c", command="npm test", second=2),
        call(4, call_id="d", command="cd /tmp", second=3),
    ]
    page = command_executions_page(build(rows))
    assert [(p.name, p.count) for p in page.programs] == [("git", 2), ("npm", 1), ("shell", 1)]
    assert page.programs[0].share == pytest.approx(0.5)
    assert page.programs[2].fallback is True


def test_wrappers_pipelines_and_heredocs_keep_the_subject_program():
    rows = [
        call(1, call_id="a", command="cd /w && env FOO=1 grep -rn needle src | head", second=0),
        call(2, call_id="b", command="cat <<'EOF' > probe.txt\n1\n2\nEOF", second=1),
        call(3, call_id="c", command="python3 - <<'PY'\nprint(1)\nPY", second=2),
    ]
    page = command_executions_page(build(rows))
    assert [(execution.program, execution.parse_basis) for execution in page.executions] == [
        ("python3", "executable"),
        ("cat", "executable"),
        ("grep", "executable"),
    ]


def test_long_command_and_output_are_bounded_and_marked():
    long_command = "grep -n " + "x" * (MAX_COMMAND_CHARS + 500)
    long_output = "y" * (MAX_OUTPUT_PREVIEW + 250)
    rows = [call(1, command=long_command), result(2, content=long_output)]
    execution = command_executions_page(build(rows)).executions[0]
    assert len(execution.command) == MAX_COMMAND_CHARS
    assert execution.command_truncated is True
    assert execution.command_chars > MAX_COMMAND_CHARS
    assert len(execution.output_preview) == MAX_OUTPUT_PREVIEW
    assert execution.output_truncated is True
    assert execution.output_chars > MAX_OUTPUT_PREVIEW


def test_multiline_commands_keep_their_text_and_parse_the_subject():
    rows = [call(1, command="cd /w && \\\n  pytest \\\n  -q")]
    execution = command_executions_page(build(rows)).executions[0]
    assert execution.command.startswith("cd /w &&")
    assert execution.program == "pytest"


def test_multiple_turns_attempts_and_runtimes_are_kept_apart():
    jobs = [
        job("j1", 1),
        job("j2", 2, runtime="codex"),
        job("j3", 3, runtime="pi", state="failed"),
    ]
    rows = [
        call(1, job_id="j1", turn_no=1, attempt_id=11, call_id="a"),
        result(2, job_id="j1", attempt_id=11, call_id="a", exit_code=0),
        call(
            3,
            job_id="j2",
            turn_no=2,
            attempt_id=12,
            call_id="a",
            command="ls",
            tool="commandExecution",
            second=2,
        ),
        result(4, job_id="j2", attempt_id=12, call_id="a", exit_code=0, second=3),
        call(
            5,
            job_id="j1",
            turn_no=1,
            attempt_id=21,
            call_id="a",
            command="ls",
            attempt_status="superseded",
            second=4,
        ),
        result(6, job_id="j1", attempt_id=21, call_id="a", exit_code=1, second=5),
        call(7, job_id="j3", turn_no=3, attempt_id=13, call_id="a", command="make", second=6),
    ]
    page = command_executions_page(build(rows, jobs))
    assert page.summary.total == 4
    assert {execution.attempt_id for execution in page.executions} == {11, 12, 21, 13}
    assert page.coverage.turns == 3
    assert page.coverage.attempts == 4
    # The retried attempt is real history, listed and marked.
    assert [e.superseded for e in page.executions] == [False, True, False, False]


def test_copied_fork_turns_are_not_counted_and_are_reported():
    jobs = [job("j1", 1), job("copy", 2, copied=True)]
    rows = [call(1, job_id="j1")]
    page = command_executions_page(build(rows, jobs))
    assert page.summary.total == 1
    assert page.coverage.copied_jobs_excluded == 1


def test_unsupported_runtimes_are_named_and_add_no_rows():
    jobs = [job("j1", 1, runtime="unknown-agent")]
    rows = [call(1, job_id="j1")]
    page = command_executions_page(build(rows, jobs))
    assert page.summary.total == 0
    assert page.coverage.unsupported_runtimes == ["unknown-agent"]


def test_a_non_command_tool_of_a_supported_runtime_is_not_a_command():
    rows = [call(1, tool="Read", command="")]
    page = command_executions_page(build(rows))
    assert page.summary.total == 0


def test_search_narrows_the_counts_and_the_list_together():
    rows = [
        call(1, call_id="a", command="git status", second=0),
        call(2, call_id="b", command="git diff", second=1),
        call(3, call_id="c", command="npm test", second=2),
    ]
    page = command_executions_page(build(rows), q="git")
    assert page.counts.all == 2
    assert page.page.total == 2
    assert page.summary.total == 3
    assert [execution.command for execution in page.executions] == ["git diff", "git status"]


def test_search_matches_the_recorded_cwd_and_the_program():
    rows = [call(1, command="pytest -q", cwd="/srv/app")]
    assert command_executions_page(build(rows), q="/srv").counts.all == 1
    assert command_executions_page(build(rows), q="pytest").counts.all == 1
    assert command_executions_page(build(rows), q="missing").counts.all == 0


def test_program_narrowing_keeps_the_other_programs_ranked():
    rows = [
        call(1, call_id="a", command="git status", second=0),
        call(2, call_id="b", command="npm test", second=1),
    ]
    page = command_executions_page(build(rows), program="npm")
    assert page.counts.all == 1
    assert [program.name for program in page.programs] == ["git", "npm"]


def test_latest_and_slowest_sorts_agree_with_the_cursor_walk():
    rows: list[dict[str, Any]] = []
    for index in range(5):
        rows.append(
            call(1 + index * 2, call_id=f"c{index}", duration_ms=index * 1000, second=index)
        )
    page = command_executions_page(build(rows), sort="latest", limit=2)
    assert [execution.id for execution in page.executions] == ["1:c4", "1:c3"]
    assert page.page.total == 5
    second = command_executions_page(
        build(rows), sort="latest", limit=2, cursor=page.page.next_cursor
    )
    assert [execution.id for execution in second.executions] == ["1:c2", "1:c1"]
    third = command_executions_page(
        build(rows), sort="latest", limit=2, cursor=second.page.next_cursor
    )
    assert [execution.id for execution in third.executions] == ["1:c0"]
    assert third.page.next_cursor is None

    slowest = command_executions_page(build(rows), sort="slowest", limit=2)
    assert [execution.id for execution in slowest.executions] == ["1:c4", "1:c3"]
    # Ties fall back to recency, so two pages never repeat a row.
    tied = [call(11, call_id="x", duration_ms=5), call(12, call_id="y", duration_ms=5)]
    first = command_executions_page(build(tied), sort="slowest", limit=1)
    rest = command_executions_page(
        build(tied), sort="slowest", limit=1, cursor=first.page.next_cursor
    )
    assert first.executions[0].id != rest.executions[0].id


def test_a_cursor_from_another_filter_is_refused():
    rows = [call(1, call_id="a", command="git status"), call(2, call_id="b", command="npm test")]
    page = command_executions_page(build(rows), limit=1)
    with pytest.raises(ValueError):
        command_executions_page(build(rows), filter="failed", cursor=page.page.next_cursor)


def test_attention_reports_slowest_failing_and_background():
    rows = [
        call(1, call_id="a", command="make", duration_ms=SLOW_COMMAND_MS + 500, second=0),
        result(2, call_id="a", exit_code=1, second=1),
        call(3, call_id="b", command="make", duration_ms=SLOW_COMMAND_MS + 900, second=2),
        result(4, call_id="b", exit_code=1, second=3),
        call(5, call_id="c", command="npm start", background=True, second=4),
    ]
    attention = command_executions_page(build(rows)).attention
    assert attention.slowest is not None and attention.slowest.id == "1:b"
    assert attention.failing is not None
    assert (attention.failing.name, attention.failing.count) == ("make", 2)
    assert attention.repeats == {"command": "make", "count": 2}
    assert attention.background == 1


def test_a_covered_subset_is_reported_as_truncated_not_as_the_whole():
    rows = [call(1, call_id="a"), call(2, call_id="b", second=1)]
    page = command_executions_page(build(rows, total=97))
    assert page.coverage.executions_total == 97
    assert page.coverage.executions_covered == 2
    assert page.coverage.truncated is True


def test_a_result_without_a_covered_call_invents_no_command():
    rows = [result(1, call_id="not-covered")]
    page = command_executions_page(build(rows))
    assert page.summary.total == 0
    assert page.executions == []


def test_a_codex_command_execution_event_is_recognized():
    rows = [
        call(1, tool="commandExecution", command="ls -la"),
        result(2, exit_code=0),
    ]
    page = command_executions_page(build(rows, [job(runtime="codex")]))
    assert page.executions[0].program == "ls"
    assert page.executions[0].status == "completed"


def test_a_launch_acknowledgement_is_not_completion():
    """A background call whose result only started the session stays running."""
    rows = [
        call(1, call_id="c1", session="s1", command="npm run dev", background=True),
        result(2, call_id="c1", content="Command running in background", session="s1"),
    ]
    page = command_executions_page(build(rows, [job(state="running")]))
    execution = page.executions[0]
    assert execution.status == "running"
    assert execution.background is True
    assert execution.exit_code is None


def test_a_launch_acknowledgement_with_a_finished_turn_is_unknown():
    rows = [
        call(1, call_id="c1", session="s1", command="npm run dev", background=True),
        result(2, call_id="c1", content="started", session="s1"),
    ]
    page = command_executions_page(build(rows, [job(state="succeeded")]))
    assert page.executions[0].status == "unknown"


def test_a_poll_that_settles_a_background_session_completes_it():
    rows = [
        call(1, call_id="c1", session="s1", command="npm run dev", background=True),
        result(2, call_id="c1", content="started", session="s1"),
        poll(3, call_id="p1", session="s1"),
        result(4, call_id="p1", content="listening", exit_code=0, second=4),
    ]
    page = command_executions_page(build(rows, [job(state="succeeded")]))
    assert page.executions[0].status == "completed"
    assert page.executions[0].output_preview == "listening"


def test_a_structured_exit_code_and_interruption_are_read():
    rows = [
        call(1, call_id="a"),
        result(2, call_id="a", structured_output={"exitCode": 4}),
        call(3, call_id="b", second=3),
        result(4, call_id="b", structured_output={"interrupted": True}, second=4),
    ]
    page = command_executions_page(build(rows))
    by_id = {execution.id: execution for execution in page.executions}
    assert by_id["1:a"].status == "failed" and by_id["1:a"].exit_code == 4
    assert by_id["1:b"].status == "failed" and by_id["1:b"].tool_error is True


def test_a_structured_shell_id_joins_a_later_poll():
    rows = [
        call(1, call_id="c1", command="npm run dev", background=True),
        result(2, call_id="c1", content="started", structured_output={"shellId": "s1"}),
        poll(3, call_id="p1", session="s1"),
        result(4, call_id="p1", content="done", exit_code=0, second=4),
    ]
    page = command_executions_page(build(rows))
    assert page.summary.total == 1
    assert page.executions[0].status == "completed"


def test_a_zero_duration_is_zero_not_one_millisecond():
    rows = [call(1, duration_ms=0), result(2, exit_code=0)]
    page = command_executions_page(build(rows))
    assert page.executions[0].duration_ms == 0
    assert page.summary.cumulative_ms == 0
    assert page.summary.durations_known == 1


def test_cumulative_duration_is_absent_when_nothing_was_measured():
    # A call with no result has no recorded or derivable duration at all.
    rows = [call(1, call_id="a")]
    page = command_executions_page(build(rows))
    assert page.summary.total == 1
    assert page.summary.durations_known == 0
    assert page.summary.duration_basis == "none"
    assert page.summary.cumulative_ms == 0
    # The count says nothing was measured; the client renders it as unknown.
    assert page.summary.durations_missing == 1
