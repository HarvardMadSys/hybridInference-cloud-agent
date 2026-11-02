"""The slice of a stored event the context reducer reads.

A context profile needs a request's usage, a compaction's report, and how many
characters of conversation content an event holds, never the content itself.
This projection is computed in Postgres so a tool result that carried a whole
file moves as its length. Raw protocol rows are passed on only when they are
one of the few kinds that carry context evidence (see
:mod:`cloud_agent.context_profile`); every other raw row moves as nothing.
"""

from __future__ import annotations

#: Codex app-server notifications that carry context evidence.
_CODEX_METHODS = ("thread/tokenUsage/updated", "thread/compacted", "item/completed")
_CODEX_METHODS_SQL = ", ".join(f"'{method}'" for method in _CODEX_METHODS)

#: Stored OpenCode/Kilo bridge lines (and pi's raw compaction ends) that may
#: carry evidence. The match is a cheap prefilter; the reducer parses the line.
_RAW_TEXT_MARKERS_SQL = """(
    e.payload->>'text' LIKE '%"step-finish"%'
    OR e.payload->>'text' LIKE '%"session.compacted"%'
    OR e.payload->>'text' LIKE '%"type":"compaction"%'
    OR e.payload->>'reason' = 'unknown type ''compaction_end'''
)"""

_CODEX_NOTIFICATION_SQL = f"""
CASE WHEN e.event_type = 'raw'
      AND e.payload->>'protocol' = 'codex-app-server'
      AND e.payload->'notification'->>'method' IN ({_CODEX_METHODS_SQL})
      AND (e.payload->'notification'->>'method' <> 'item/completed'
           OR e.payload->'notification'->'params'->'item'->>'type' = 'contextCompaction')
     THEN jsonb_build_object(
         'method', e.payload->'notification'->'method',
         'params', jsonb_build_object(
             'turnId', e.payload->'notification'->'params'->'turnId',
             'tokenUsage', e.payload->'notification'->'params'->'tokenUsage',
             'item', jsonb_build_object(
                 'id', e.payload->'notification'->'params'->'item'->'id',
                 'type', e.payload->'notification'->'params'->'item'->'type'
             )
         )
     )
END
"""

#: Characters of conversation content an event holds, by its normalized kind.
_CHARS_SQL = """
CASE e.event_type
    WHEN 'message' THEN length(e.payload->>'text')
    WHEN 'thinking' THEN length(e.payload->>'text')
    WHEN 'tool_use' THEN COALESCE(length(e.payload->>'name'), 0)
                         + COALESCE(length((e.payload->'input')::text), 0)
    WHEN 'tool_result' THEN length(e.payload->>'content')
    WHEN 'user_message' THEN length(e.payload->>'text')
END
"""

#: A platform bound or an adapter clipped this event's content.
_TRUNCATED_SQL = """
CASE WHEN (e.event_type IN ('message', 'thinking', 'tool_use', 'tool_result')
           OR e.event_type = 'lifecycle'
              AND e.payload->>'phase' IN ('compaction_summary', 'context_injection'))
      AND (e.payload->>'truncated' = 'true' OR e.payload ? 'original_payload_bytes')
     THEN TRUE
END
"""

#: CJK and ASCII character classes, for the token estimate's counts
#: (:mod:`cloud_agent_shared.text_tokens`): what remains after removing every
#: character outside the class is the class's count.
CJK_CLASS = r"[^぀-ヿ㐀-䶿一-鿿가-힯豈-﫿＀-￯]"
ASCII_CLASS = r"[^\x01-\x7f]"


def token_estimate_sql(text: str) -> str:
    """The ``chars_cjk_v1`` estimate of ``text``, computed in Postgres."""
    return f"""(CEIL(length(regexp_replace({text}, '{ASCII_CLASS}', '', 'g')) / 4.0)
        + length(regexp_replace({text}, '{CJK_CLASS}', '', 'g'))
        + CEIL(GREATEST(length({text})
                        - length(regexp_replace({text}, '{ASCII_CLASS}', '', 'g'))
                        - length(regexp_replace({text}, '{CJK_CLASS}', '', 'g')), 0) / 2.0))"""


#: The stored text whose size a profile counts: conversation content, and the
#: compaction summaries and injected messages the CLI carries forward.
_COUNTED_TEXT_SQL = """
CASE e.event_type
    WHEN 'message' THEN e.payload->>'text'
    WHEN 'thinking' THEN e.payload->>'text'
    WHEN 'tool_use' THEN COALESCE(e.payload->>'name', '') || COALESCE((e.payload->'input')::text, '')
    WHEN 'tool_result' THEN e.payload->>'content'
    WHEN 'user_message' THEN e.payload->>'text'
    WHEN 'lifecycle' THEN CASE
        WHEN e.payload->>'phase' IN ('compaction_summary', 'context_injection')
            THEN e.payload->>'text'
        WHEN e.payload->>'phase' = 'compact_boundary' THEN e.payload->>'summary_text'
    END
END
"""

CONTEXT_EVENT_PAYLOAD_SQL = f"""
jsonb_strip_nulls(jsonb_build_object(
    'usage', CASE WHEN jsonb_typeof(e.payload->'usage') = 'object' THEN e.payload->'usage' END,
    'usage_id', e.payload->'usage_id',
    'usage_scope', e.payload->'usage_scope',
    'parent_tool_use_id', e.payload->'parent_tool_use_id',
    'agent_thread_id', e.payload->'agent_thread_id',
    'phase', CASE WHEN e.event_type = 'lifecycle' THEN e.payload->'phase' END,
    'model', CASE WHEN e.event_type IN ('lifecycle', 'usage') THEN e.payload->'model' END,
    'compaction', e.payload->'compaction',
    'snapshot', CASE WHEN e.event_type = 'lifecycle' AND e.payload->>'phase' = 'context_snapshot'
                     THEN jsonb_build_object(
                         'source', e.payload->'snapshot'->'source',
                         'status', e.payload->'snapshot'->'status',
                         'total_tokens', e.payload->'snapshot'->'total_tokens',
                         'context_window', e.payload->'snapshot'->'context_window')
                     -- Claude's own start-up report: names of the tools and
                     -- MCP servers it offers, no sizes (context_request).
                     WHEN e.event_type = 'lifecycle' AND e.payload->>'phase' = 'init'
                          AND jsonb_typeof(e.payload->'harness') = 'object'
                     THEN jsonb_build_object('source', 'claude_init', 'status', 'ok') END,
    'completed', CASE WHEN e.event_type = 'lifecycle' THEN e.payload->'completed' END,
    'context_windows', e.payload->'context_windows',
    'input_tokens', CASE WHEN e.event_type = 'usage' THEN e.payload->'input_tokens' END,
    'output_tokens', CASE WHEN e.event_type = 'usage' THEN e.payload->'output_tokens' END,
    'cache_read_tokens', CASE WHEN e.event_type = 'usage' THEN e.payload->'cache_read_tokens' END,
    'cache_write_tokens', CASE WHEN e.event_type = 'usage' THEN e.payload->'cache_write_tokens' END,
    'notification', {_CODEX_NOTIFICATION_SQL.strip()},
    'reason', CASE WHEN e.event_type = 'raw' AND e.payload->>'reason' LIKE 'unknown type ''compaction%'
                   THEN e.payload->'reason' END,
    'raw_text', CASE WHEN e.event_type = 'raw' AND e.payload->>'protocol' IS NULL
                      AND {_RAW_TEXT_MARKERS_SQL.strip()}
                     THEN to_jsonb(left(e.payload->>'text', 4000)) END,
    'chars', {_CHARS_SQL.strip()},
    'cjk', length(regexp_replace({_COUNTED_TEXT_SQL.strip()}, '{CJK_CLASS}', '', 'g')),
    'ascii', length(regexp_replace({_COUNTED_TEXT_SQL.strip()}, '{ASCII_CLASS}', '', 'g')),
    'text_chars', CASE WHEN e.event_type = 'lifecycle'
                       THEN length({_COUNTED_TEXT_SQL.strip()}) END,
    'truncated', {_TRUNCATED_SQL.strip()},
    -- Context events (cloud_agent.context_events): what the platform wrapped
    -- the prompt in at claim, what the CLI injected or pruned, the mode it
    -- started in, and the plan-mode tools. Sizes and names, never content.
    'context_injections', CASE WHEN e.event_type = 'lifecycle' AND e.payload->>'phase' = 'started'
                               THEN e.payload->'context_injections' END,
    'injection', CASE WHEN e.event_type = 'lifecycle' AND e.payload->>'phase' = 'context_injection'
                      THEN jsonb_build_object(
                          'source', e.payload->'source', 'label', e.payload->'label',
                          'chars', e.payload->'chars') END,
    'prune', CASE WHEN e.event_type = 'lifecycle' AND e.payload->>'phase' = 'context_prune'
                  THEN jsonb_build_object(
                      'source', e.payload->'source', 'tool', e.payload->'tool',
                      'call_id', e.payload->'call_id', 'chars', e.payload->'chars') END,
    'permission_mode', CASE WHEN e.event_type = 'lifecycle' AND e.payload->>'phase' = 'init'
                            THEN e.payload->'harness'->'permission_mode' END,
    'tool_name', CASE WHEN e.event_type = 'tool_use'
                           AND e.payload->>'name' IN ('EnterPlanMode', 'ExitPlanMode', 'Skill')
                      THEN e.payload->'name' END,
    -- Timing (cloud_agent.context_running): a call's id and its result's
    -- reference pair them; a Claude block's streaming duration on the host.
    'tool_id', CASE e.event_type WHEN 'tool_use' THEN e.payload->'id'
                                 WHEN 'tool_result' THEN e.payload->'tool_use_id' END,
    'block_ms', CASE WHEN jsonb_typeof(e.payload->'block_ms') = 'number'
                     THEN e.payload->'block_ms' END
))
"""
