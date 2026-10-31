"""The slice of a stored event the File Activity reducer reads.

Everything the reducer needs is folded here, in Postgres, so a whole-file write
or a multi-file patch never moves as a tool payload that can be hundreds of
kilobytes. The projection is a pure function of the stored row: it invents no
path, no count and no delta.

**One vocabulary across the pinned runtimes.** Each adapter records its
arguments under its own names — Claude Code's ``file_path``/``old_string``/
``new_string``, pi's ``path``/``oldText``/``newText``, Codex's ``apply_patch``
body and its ``fileChange`` notifications, OpenCode/Kilo's ``filePath`` and
``tool`` part names. The projection keeps a whitelist of those keys under one
name each, so the reducer reads one shape. A key no pinned runtime records is
not read, and an unrecognized event yields nothing rather than a guess.

**Bounded text, exact line counts.** A recorded content, edit side or patch
body moves in full only up to ``$3`` characters; beyond that it moves as a
truncated preview and ``*_clipped`` says so, and the reducer treats a structure
it cannot see whole (a patch, a MultiEdit pair) as unknown. The line counts of
the three principal text fields are computed here, on the whole recorded text,
so a clipped preview never produces a wrong number.

**One result per call.** A call's result is joined by LATERAL with the oldest
matching ``tool_result``, so a duplicated result row can never multiply one
call into two operations.
"""

from __future__ import annotations

from cloud_agent.file_activity.models import MAX_RECORDED_TEXT

#: Characters of a recorded text this projection returns. A request may lower
#: it; it may never raise it above the module constant.
PREVIEW_CHARS = MAX_RECORDED_TEXT

#: The recorded arguments object of a tool call, as one JSON value.
_INPUT = "(CASE WHEN jsonb_typeof(e.payload->'input') = 'object' THEN e.payload->'input' END)"

#: The recorded tool name, and its last ``__``/``.`` segment, lower-cased.
_TOOL = "COALESCE(e.payload->>'name', '')"
_SHORT_TOOL = "lower(regexp_replace(" + _TOOL + ", '^.*(__|\\.)', ''))"

#: The tool names a pinned runtime records for a file action, as the reducer
#: classifies them. Kept beside the reducer's sets; the two must agree, and
#: ``tests/unit/test_file_activity.py`` checks them against one another.
ACTION_TOOL_NAMES = (
    "read",
    "read_file",
    "readfile",
    "read_text_file",
    "view_file",
    "write",
    "write_file",
    "writefile",
    "create_file",
    "createfile",
    "edit",
    "edit_file",
    "editfile",
    "multiedit",
    "multi_edit",
    "str_replace",
    "str_replace_editor",
    "notebookedit",
    "notebook_edit",
    "apply_patch",
    "applypatch",
    "patch",
    "patch_file",
    "grep",
    "search",
    "search_files",
    "text_search",
    "find",
    "find_files",
    "glob",
    "list",
    "ls",
    "list_files",
    "file_search",
    "view_image",
    "read_image",
    "image",
    "images",
    "screenshot",
)
ACTION_TOOLS = ", ".join(f"'{name}'" for name in ACTION_TOOL_NAMES)

#: Whether this row is a Codex ``fileChange`` notification worth reading.
FILE_CHANGE_SQL = """
(e.event_type = 'raw'
 AND e.payload->>'protocol' = 'codex-app-server'
 AND e.payload->'notification'->'params'->'item'->>'type' = 'fileChange')
"""

#: A lifecycle row that recorded the cwd its turn started in.
CWD_SQL = "(e.event_type = 'lifecycle' AND jsonb_typeof(e.payload->'cwd') = 'string')"

#: Argument keys any pinned runtime records, kept as they are.
_WHITELIST = (
    "file_path",
    "filePath",
    "path",
    "file",
    "notebook_path",
    "file_path_alt",
    "pattern",
    "glob",
    "query",
    "directory",
    "replace_all",
)


def _string(key: str) -> str:
    """``input.<key>`` as text, only when the JSON value is a string."""
    return f"(CASE WHEN jsonb_typeof({_INPUT}->'{key}') = 'string' THEN {_INPUT}->>'{key}' END)"


def _first(*keys: str) -> str:
    """The first recorded spelling of one text field, across runtimes."""
    return f"COALESCE({', '.join(_string(key) for key in keys)})"


_CONTENT = _first("content", "file_text", "text")
_OLD = _first("old_string", "old_str", "oldText")
_NEW = _first("new_string", "new_str", "newText")

#: The apply_patch body: a string input, or a ``patch``/``input`` argument.
_PATCH = f"""COALESCE(
    CASE WHEN jsonb_typeof(e.payload->'input') = 'string' THEN e.payload->>'input' END,
    {_string("patch")}, {_string("input")})"""


def line_count_sql(text_sql: str) -> str:
    """File lines a recorded string holds, exactly as the reducer counts them.

    ``""`` holds none; a trailing newline ends the last line rather than
    starting an empty one.
    """
    return f"""CASE WHEN COALESCE(length({text_sql}), 0) = 0 THEN 0 ELSE
        length({text_sql}) - length(replace({text_sql}, E'\\n', ''))
        + CASE WHEN right({text_sql}, 1) = E'\\n' THEN 0 ELSE 1 END END"""


#: The bounded arguments object the reducer reads: only whitelisted keys, each
#: string clipped, so no payload detail beyond them moves.
_INPUT_JSON = f"""CASE WHEN {_INPUT} IS NOT NULL THEN (
        SELECT COALESCE(jsonb_object_agg(entry.name, CASE
                   WHEN jsonb_typeof(entry.value) = 'string'
                   THEN to_jsonb(left(entry.value #>> '{{}}', $3::integer))
                   ELSE entry.value END), '{{}}'::jsonb)
        FROM jsonb_each({_INPUT}) AS entry(name, value)
        WHERE entry.name IN ({", ".join(f"'{name}'" for name in _WHITELIST)})
    ) END"""

#: A MultiEdit's pairs, each side clipped, with the whole side's length so a
#: clipped pair can be recognized instead of summed.
_EDITS_JSON = f"""CASE WHEN jsonb_typeof({_INPUT}->'edits') = 'array' THEN (
        SELECT jsonb_agg(jsonb_build_object(
            'old_string', left(COALESCE(pair.value->>'old_string', pair.value->>'old_str'), $3::integer),
            'new_string', left(COALESCE(pair.value->>'new_string', pair.value->>'new_str'), $3::integer),
            'replace_all', COALESCE((pair.value->'replace_all')::boolean, FALSE),
            'old_chars', length(COALESCE(pair.value->>'old_string', pair.value->>'old_str')),
            'new_chars', length(COALESCE(pair.value->>'new_string', pair.value->>'new_str'))
        ))
        FROM jsonb_array_elements({_INPUT}->'edits') AS pair(value)
    ) END"""

PAYLOAD_SQL = f"""
jsonb_strip_nulls(jsonb_build_object(
    'id', e.id,
    'attempt_id', e.attempt_id,
    'seq', e.seq,
    'job_id', e.job_id,
    'turn_no', j.turn_no,
    'created_at', e.created_at,
    'tool', {_TOOL},
    'tool_id', e.payload->>'id',
    'attempt_no', a.attempt_no,
    'attempt_running', (a.status = 'running'),
    'superseded', (a.status = 'superseded'),
    'subagent', (e.payload ? 'parent_tool_use_id' OR e.payload ? 'agent_thread_id'),
    'cwd', CASE WHEN {CWD_SQL.strip()} THEN e.payload->>'cwd' END,
    'file_change_method', e.payload->'notification'->>'method',
    -- The native item's own identity and outcome, read from the notification
    -- directly: the bounded body below may be clipped, but these three small
    -- fields still say which item it is and how it ended.
    'file_change_item_id', e.payload->'notification'->'params'->'item'->>'id',
    'file_change_item_status', e.payload->'notification'->'params'->'item'->>'status',
    'file_change_item_type', e.payload->'notification'->'params'->'item'->>'type',
    'file_change', CASE WHEN {FILE_CHANGE_SQL.strip()}
                        THEN left((e.payload->'notification')::text, $3::integer) END,
    -- The stored text was longer than the bound: its changes are incomplete,
    -- and the reducer reports that rather than reading it as an empty item.
    'file_change_clipped', CASE WHEN {FILE_CHANGE_SQL.strip()}
                                THEN length((e.payload->'notification')::text) > $3::integer
                                END,
    'input', {_INPUT_JSON},
    'input_text', CASE WHEN jsonb_typeof(e.payload->'input') = 'string'
                       THEN left(e.payload->>'input', $3::integer) END,
    'edits', {_EDITS_JSON},
    'patch', CASE WHEN e.event_type = 'tool_use' THEN left({_PATCH}, $3::integer) END,
    'patch_source_chars', length({_PATCH}),
    'content_lines', CASE WHEN {_CONTENT} IS NOT NULL THEN {line_count_sql(_CONTENT)} END,
    'edit_added_lines', CASE WHEN {_NEW} IS NOT NULL THEN {line_count_sql(_NEW)} END,
    'edit_removed_lines', CASE WHEN {_OLD} IS NOT NULL THEN {line_count_sql(_OLD)} END
))
"""

#: A row is read when it is a tool call a pinned runtime records, a Codex file
#: change, or a lifecycle row carrying the turn's cwd. Every other stored event
#: — a shell call, a message, a usage report — is skipped in Postgres.
#: An argument key that names a file, an edit's text or a search's scope. A
#: tool call carrying none of them (a shell command, a task) is not read.
_FILE_ARG_KEYS = (
    "file_path",
    "filePath",
    "file_path_alt",
    "notebook_path",
    "path",
    "file",
    "old_string",
    "new_string",
    "old_str",
    "new_str",
    "oldText",
    "newText",
    "content",
    "file_text",
    "patch",
    "pattern",
    "glob",
    "query",
    "directory",
    "edits",
)

_FILE_ARGS_SQL = f"""EXISTS (
        SELECT 1 FROM jsonb_object_keys(COALESCE({_INPUT}, '{{}}'::jsonb)) AS arg(name)
        WHERE arg.name IN ({", ".join(f"'{name}'" for name in _FILE_ARG_KEYS)})
    )"""

CANDIDATE_SQL = f"""(
    (e.event_type = 'tool_use'
     AND ({_SHORT_TOOL} IN ({ACTION_TOOLS})
          OR jsonb_typeof(e.payload->'input') = 'string'
          OR {_FILE_ARGS_SQL}))
    OR {FILE_CHANGE_SQL.strip()}
    OR {CWD_SQL.strip()}
)"""

FILE_ACTIVITY_EVENT_SQL = rf"""
    /* file_activity_events */
    SELECT e.id, e.event_type, e.attempt_id, e.seq, e.job_id, e.created_at,
           {PAYLOAD_SQL} AS payload,
           result.id AS result_id,
           (result.payload->>'is_error')::boolean AS result_error,
           left(COALESCE(result.payload->>'content', ''), 4000) AS result_content,
           -- A structural skeleton of the result, for image evidence. Bulky
           -- string values of the payload fields (a base64 body, a text body)
           -- are replaced by a marker, so the block's own type and media type
           -- survive without the body moving. The string token is JSON's own
           -- grammar — the valid escape set, or \u plus four hex digits, and
           -- no raw control characters — so valid input stays valid and
           -- malformed input stays malformed for the reducer's json.loads.
           left(regexp_replace(regexp_replace(regexp_replace(
                 COALESCE(result.payload->>'content', ''),
                 '"data"[ \t\r\n]*:[ \t\r\n]*"(?:[^"\\[:cntrl:]]|\\["\\/bfnrt]|\\u[0-9A-Fa-f]{{4}})*"',
                 '"data":""', 'g'),
                 '"text"[ \t\r\n]*:[ \t\r\n]*"(?:[^"\\[:cntrl:]]|\\["\\/bfnrt]|\\u[0-9A-Fa-f]{{4}})*"',
                 '"text":""', 'g'),
                 '"content"[ \t\r\n]*:[ \t\r\n]*"(?:[^"\\[:cntrl:]]|\\["\\/bfnrt]|\\u[0-9A-Fa-f]{{4}})*"',
                 '"content":""', 'g'),
                 4000) AS result_skeleton
    FROM agent_job_events e
    JOIN agent_jobs j ON j.id = e.job_id AND j.thread_id = $1 AND j.user_id = $2
    JOIN agent_threads t ON t.id = j.thread_id AND t.deleted_at IS NULL
    LEFT JOIN agent_attempts a ON a.id = e.attempt_id
    LEFT JOIN LATERAL (
        SELECT r.id, r.payload
        FROM agent_job_events r
        WHERE r.attempt_id = e.attempt_id
          AND r.event_type = 'tool_result'
          AND r.payload->>'tool_use_id' = e.payload->>'id'
        ORDER BY r.id
        LIMIT 1
    ) result ON TRUE
    WHERE {CANDIDATE_SQL}
      AND e.id < $4
    ORDER BY e.id DESC
    LIMIT $5
"""

#: The cwd each attempt recorded, for the attempts a recent window holds.
#: A turn's ``started`` row is older than its tool calls, so a newest-first
#: window can cut it off; reading these few rows by attempt keeps relative
#: paths resolvable without widening the window.
FILE_ACTIVITY_CWD_SQL = """
    /* file_activity_cwds */
    SELECT DISTINCT ON (e.attempt_id) e.attempt_id, e.payload->>'cwd' AS cwd
    FROM agent_job_events e
    JOIN agent_jobs j ON j.id = e.job_id AND j.thread_id = $1 AND j.user_id = $2
    JOIN agent_threads t ON t.id = j.thread_id AND t.deleted_at IS NULL
    WHERE e.event_type = 'lifecycle'
      AND e.attempt_id = ANY($3::bigint[])
      AND jsonb_typeof(e.payload->'cwd') = 'string'
      AND length(e.payload->>'cwd') <= 4096
    ORDER BY e.attempt_id, e.id
"""

FILE_ACTIVITY_COVERAGE_SQL = """
    /* file_activity_coverage */
    SELECT COUNT(DISTINCT j.id) AS turns,
           COUNT(DISTINCT e.attempt_id) AS attempts
    FROM agent_jobs j
    JOIN agent_threads t ON t.id = j.thread_id AND t.deleted_at IS NULL
    LEFT JOIN agent_job_events e ON e.job_id = j.id
    WHERE j.thread_id = $1 AND j.user_id = $2
"""

__all__ = [
    "ACTION_TOOL_NAMES",
    "FILE_ACTIVITY_COVERAGE_SQL",
    "FILE_ACTIVITY_CWD_SQL",
    "FILE_ACTIVITY_EVENT_SQL",
    "PREVIEW_CHARS",
    "line_count_sql",
]
