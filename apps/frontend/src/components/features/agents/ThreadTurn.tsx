'use client';

import { useRef, useState, type ComponentProps, type ReactNode } from 'react';
import { Markdown } from '@/components/ui/Markdown';
import { useT } from '@/components/providers';
import type { TranslationKey } from '@/lib/i18n/dictionaries/en';
import { getAgentThreadMessage } from '@/lib/api/agents/threads';
import { GoalIcon } from './ComposerPlusMenu';
import { AssistantMessage, ForkIcon, MessageActionButton, UserMessage } from './ChatMessages';
import type { AgentThreadMessage, GoalTurn } from './types';
import type { TranscriptPresentation } from './transcriptPresentation';

/** A preview stays explicit, and each expansion fetches only one bounded chunk. */
export function ThreadTurn(props: ComponentProps<typeof ThreadTurnBody>) {
  const { message } = props;
  const [expanded, setExpanded] = useState<{
    id: number;
    content: string;
    next: number | null;
  } | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const current = useRef(message.id);
  current.current = message.id;
  const held = expanded?.id === message.id ? expanded : null;
  const content = held?.content ?? message.content;
  const next = held ? held.next : message.nextOffset;
  const incomplete = message.contentTruncated && next != null;
  async function load() {
    if (loading || next == null) return;
    setLoading(true);
    setError(null);
    try {
      const chunk = await getAgentThreadMessage(message.jobId, message.id, next);
      if (current.current === message.id)
        setExpanded({
          id: message.id,
          content: content + chunk.content,
          next: chunk.next_offset ?? null,
        });
    } catch (cause) {
      if (current.current === message.id)
        setError(cause instanceof Error ? cause.message : 'Could not load message');
    } finally {
      if (current.current === message.id) setLoading(false);
    }
  }
  return (
    <>
      <ThreadTurnBody
        {...props}
        message={{ ...message, content }}
        onRewind={!incomplete && props.onRewind ? () => props.onRewind?.(content) : undefined}
      />
      {incomplete && (
        <div className="px-3 pb-3 text-xs text-muted">
          <span>Message preview. </span>
          <button
            type="button"
            className="underline"
            disabled={loading}
            onClick={() => void load()}
          >
            {loading ? 'Loading…' : 'Load more of this message'}
          </button>
          {error && <p role="alert">{error}</p>}
        </div>
      )}
    </>
  );
}

function ThreadTurnBody({
  message,
  busy,
  onFork,
  onRewind,
  footer,
  blockKey,
  goalTurn,
  presentation,
}: {
  message: AgentThreadMessage;
  footer?: ReactNode;
  busy?: boolean;
  onFork?: () => void;
  onRewind?: (content?: string) => void;
  blockKey?: string;
  /** How this turn came to exist, and what it reported about the goal. */
  goalTurn?: GoalTurn;
  presentation?: TranscriptPresentation;
}) {
  const t = useT();
  if (message.role === 'assistant') {
    return (
      <AssistantMessage
        text={message.content}
        footer={footer}
        blockKey={blockKey}
        presentation={presentation}
        extraActions={
          onFork ? (
            <MessageActionButton label="Fork from here" onClick={onFork} disabled={busy}>
              <ForkIcon />
              Fork from here
            </MessageActionButton>
          ) : undefined
        }
      />
    );
  }
  if (goalTurn?.source === 'goal') {
    // Not a bubble, because nobody wrote it. The round's body is the
    // instruction the platform generated, and it stays in the transcript for
    // anyone who wants to read it — collapsed, because it is the same standing
    // text every round and would otherwise be most of what the conversation
    // appears to be.
    const report = reportKey(goalTurn);
    return (
      <div data-block-key={blockKey} className="my-4">
        <details className="group rounded-lg border border-dashed border-gray-300 px-3 py-2 dark:border-white/15">
          <summary className="flex cursor-pointer list-none items-center gap-2 text-xs text-gray-500 dark:text-gray-400">
            <GoalIcon className="h-3.5 w-3.5 shrink-0" />
            <span className="min-w-0 flex-1 truncate">
              {goalTurn.kind === 'repair'
                ? t('goal.turnRepair')
                : t('goal.turnWork', { round: goalTurn.round ?? 1 })}
            </span>
            {report ? (
              <span className="shrink-0 rounded bg-gray-100 px-1.5 py-0.5 text-[10px] dark:bg-white/10">
                {t(report)}
              </span>
            ) : null}
            {goalTurn.reportDetail ? (
              <span
                className="min-w-0 max-w-64 shrink-0 truncate text-[10px] text-gray-400 dark:text-gray-500"
                title={goalTurn.reportDetail}
              >
                {goalTurn.reportDetail}
              </span>
            ) : null}
          </summary>
          <div className="mt-2 text-xs text-gray-500 dark:text-gray-400">
            <Markdown text={message.content} />
          </div>
        </details>
      </div>
    );
  }
  return <UserMessage text={message.content} {...{ blockKey, busy, onRewind, presentation }} />;
}

/**
 * The badge for what a round said about the goal, or nothing when there is
 * nothing to say.
 *
 * Read from the platform's own parse and never from the message text: a report
 * it could not read is shown as exactly that, and is never a completion. An
 * `absent` parse is a round that reliably made no report, which is a round that
 * simply keeps working.
 */
function reportKey(turn: GoalTurn): TranslationKey | undefined {
  if (turn.reportParse === 'invalid' || turn.reportParse === 'unavailable') {
    return 'goal.reportUnreadable';
  }
  if (turn.reportParse !== 'valid') return undefined;
  if (turn.reportOutcome === 'complete') return 'goal.reportComplete';
  if (turn.reportOutcome === 'needs_input') return 'goal.reportNeedsInput';
  return undefined;
}
