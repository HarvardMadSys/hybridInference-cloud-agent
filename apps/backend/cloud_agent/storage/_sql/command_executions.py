"""The bounded projection the Command Executions read needs.

Command text and result text are bounded in Postgres, so one long recorded
command or a tool result that carried a whole file moves as its preview and
its length rather than its content. Nothing here derives a value the records
do not carry: an exit code stays null when none was recorded, and a cwd is
only what the call's input named.
"""

from __future__ import annotations

from cloud_agent.command_executions import (
    MAX_COMMAND_CHARS,
    MAX_OUTPUT_PREVIEW,
    runtime_command_tools,
)

#: The exact (runtime, tool name) pairs this build knows as commands. The query
#: filters by these pairs rather than by tool name alone, so one runtime's name
#: cannot admit another runtime's call and the database count agrees with what
#: the assembler accepts.
_RUNTIME_TOOLS = "VALUES " + ", ".join(
    f"('{runtime}', '{tool}')"
    for runtime in ("claude-code", "codex", "pi", "opencode", "kilo")
    for tool in sorted(runtime_command_tools(runtime) or ())
)

#: One runtime's own tool name, matched as a pair so a name cannot leak across.
_RUNTIME_FILTER = f"""(
    j.runtime, LOWER(COALESCE(e.payload->>'name', ''))
) IN (SELECT runtime, tool FROM ({_RUNTIME_TOOLS}) AS known(runtime, tool))"""

#: The command text a recorded call carried, in the three shapes the adapters
#: write: Claude's ``input.command``, Codex's top-level ``command``/``input``
#: string, and OpenCode/Kilo/pi's ``input.command``.
COMMAND_TEXT_SQL = f"""
    left(COALESCE(
        CASE WHEN jsonb_typeof(e.payload->'input') = 'object'
                  AND e.payload->'input'->>'command' IS NOT NULL
             THEN e.payload->'input'->>'command'
             WHEN jsonb_typeof(e.payload->'input') = 'object'
                  AND e.payload->'input'->>'cmd' IS NOT NULL
             THEN e.payload->'input'->>'cmd'
             WHEN jsonb_typeof(e.payload->'input') = 'string'
             THEN e.payload->>'input'
             WHEN e.payload->>'command' IS NOT NULL
             THEN e.payload->>'command'
        END, ''), {MAX_COMMAND_CHARS})
"""

CWD_SQL = """
    COALESCE(
        CASE WHEN jsonb_typeof(e.payload->'input') = 'object'
             THEN e.payload->'input'->>'cwd' END,
        e.payload->>'cwd')
"""

#: The command session a call or poll continues. Claude names it ``bash_id``
#: on a poll and ``shellId`` on the result that opened it; a runtime that
#: spells it ``session_id`` is read too. Nothing is derived.
SESSION_SQL = """
    COALESCE(
        CASE WHEN jsonb_typeof(e.payload->'input') = 'object'
             THEN COALESCE(e.payload->'input'->>'session_id',
                           e.payload->'input'->>'bash_id') END,
        e.payload->>'session_id',
        e.payload->>'bash_id',
        e.payload->>'shell_id',
        e.payload->>'shellId')
"""

BACKGROUND_SQL = """
    (COALESCE(
        CASE WHEN jsonb_typeof(e.payload->'input') = 'object'
             THEN e.payload->'input'->'run_in_background' END,
        e.payload->'run_in_background',
        CASE WHEN jsonb_typeof(e.payload->'input') = 'object'
             THEN e.payload->'input'->'background' END,
        e.payload->'background') = 'true'::jsonb
     OR e.payload->>'status' IN ('background', 'detached'))
"""

#: The attempts and calls this read covers, newest first. One row per call
#: event; a repeated event of the same identity is merged by the assembler.
CALLS_SQL = f"""
    /* command_executions_calls */
    WITH owned AS MATERIALIZED (
        SELECT j.thread_id
        FROM agent_jobs j
        JOIN agent_threads t ON t.id = j.thread_id
        WHERE j.id = $1 AND j.user_id = $2 AND t.deleted_at IS NULL
    )
    SELECT e.id AS event_id, e.job_id, e.attempt_id, e.event_type,
           e.seq, e.created_at AS at,
           j.turn_no, j.state AS job_state, j.runtime,
           (j.fork_source_job_id IS NOT NULL) AS copied,
           a.attempt_no, a.status AS attempt_status,
           {COMMAND_TEXT_SQL.strip()} AS command_text,
           length(COALESCE(
               CASE WHEN jsonb_typeof(e.payload->'input') = 'object'
                         AND e.payload->'input'->>'command' IS NOT NULL
                    THEN e.payload->'input'->>'command'
                    WHEN jsonb_typeof(e.payload->'input') = 'object'
                         AND e.payload->'input'->>'cmd' IS NOT NULL
                    THEN e.payload->'input'->>'cmd'
                    WHEN jsonb_typeof(e.payload->'input') = 'string'
                    THEN e.payload->>'input'
                    WHEN e.payload->>'command' IS NOT NULL
                    THEN e.payload->>'command'
               END, '')) AS command_chars,
           e.payload->>'name' AS tool_name,
           e.payload->>'id' AS call_id,
           {CWD_SQL.strip()} AS cwd,
           CASE WHEN jsonb_typeof(e.payload->'input') = 'object'
                THEN e.payload->'input'->>'description' END AS description,
           {SESSION_SQL.strip()} AS session_id,
           {BACKGROUND_SQL.strip()} AS background,
           r.duration_ms, r.timing_source
    FROM owned
    JOIN agent_jobs j ON j.thread_id = owned.thread_id AND j.user_id = $2
    JOIN agent_attempts a ON a.job_id = j.id
    JOIN agent_job_events e ON e.attempt_id = a.id AND e.event_type = 'tool_use'
    LEFT JOIN agent_tool_call_resources r
           ON r.attempt_id = a.id AND r.tool_use_id = e.payload->>'id'
    WHERE j.fork_source_job_id IS NULL
      AND {_RUNTIME_FILTER}
      AND ({COMMAND_TEXT_SQL.strip()}) <> ''
    ORDER BY e.id DESC
    LIMIT $3
"""

#: The results and session polls that answer the covered calls. A result is
#: matched to its call by the identity the assembler reads, or to the session a
#: poll continues; never by command text. Poll tool names are matched here so
#: a session's later polls are in the projection at all.
RESULTS_SQL = f"""
    /* command_executions_results */
    SELECT e.id AS event_id, e.job_id, e.attempt_id, e.event_type,
           e.seq, e.created_at AS at,
           e.payload->>'tool_use_id' AS tool_use_id,
           e.payload->>'is_error' AS is_error,
           CASE WHEN e.payload->>'exit_code' ~ '^-?[0-9]{{1,9}}$'
                THEN (e.payload->>'exit_code')::integer END AS exit_code,
           left(COALESCE(e.payload->>'content', ''), {MAX_OUTPUT_PREVIEW}) AS content,
           length(COALESCE(e.payload->>'content', '')) AS content_chars,
           e.payload->>'status' AS status,
           {SESSION_SQL.strip()} AS session_id,
           {BACKGROUND_SQL.strip()} AS background
    FROM agent_job_events e
    JOIN agent_jobs j ON j.id = e.job_id
    JOIN agent_threads t ON t.id = j.thread_id
    WHERE e.attempt_id = ANY($1::bigint[])
      AND j.user_id = $4 AND t.deleted_at IS NULL
      AND j.thread_id = (
          SELECT owner_job.thread_id FROM agent_jobs owner_job
          WHERE owner_job.id = $5 AND owner_job.user_id = $4
      )
      AND (
          e.event_type = 'tool_result'
          OR (e.event_type = 'tool_use'
              AND LOWER(COALESCE(e.payload->>'name', '')) = ANY($2::text[]))
      )
    ORDER BY e.id
    LIMIT $3
"""

#: Command calls the conversation records in total, for honest coverage.
TOTAL_SQL = f"""
    /* command_executions_total */
    WITH owned AS MATERIALIZED (
        SELECT j.thread_id
        FROM agent_jobs j
        JOIN agent_threads t ON t.id = j.thread_id
        WHERE j.id = $1 AND j.user_id = $2 AND t.deleted_at IS NULL
    )
    SELECT COUNT(DISTINCT (e.attempt_id,
                           COALESCE(e.payload->>'id', 'seq' || e.seq::text)))::bigint
    FROM owned
    JOIN agent_jobs j ON j.thread_id = owned.thread_id AND j.user_id = $2
    JOIN agent_attempts a ON a.job_id = j.id
    JOIN agent_job_events e ON e.attempt_id = a.id AND e.event_type = 'tool_use'
    WHERE j.fork_source_job_id IS NULL
      AND {_RUNTIME_FILTER}
      AND ({COMMAND_TEXT_SQL.strip()}) <> ''
"""

#: Every turn of the conversation, for counts and copied-turn reporting.
TURNS_SQL = """
    /* command_executions_turns */
    WITH owned AS MATERIALIZED (
        SELECT j.thread_id
        FROM agent_jobs j
        JOIN agent_threads t ON t.id = j.thread_id
        WHERE j.id = $1 AND j.user_id = $2 AND t.deleted_at IS NULL
    )
    SELECT j.thread_id, j.id AS job_id, j.turn_no, j.state, j.runtime,
           (j.fork_source_job_id IS NOT NULL) AS copied
    FROM owned
    JOIN agent_jobs j ON j.thread_id = owned.thread_id AND j.user_id = $2
    ORDER BY j.turn_no
"""

__all__ = [
    "CALLS_SQL",
    "RESULTS_SQL",
    "TOTAL_SQL",
    "TURNS_SQL",
]
