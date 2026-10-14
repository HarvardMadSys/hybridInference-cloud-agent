'use client';

import { useLayoutEffect, useRef } from 'react';
import type { OverviewSubagent } from './activityOverview';
import { EventList, EventRow, UserMessage, type InspectTranscriptEvent } from './ChatMessages';
import type { DelegationReply } from './toolActivity';
import { DelegationReplyDetails, Status } from './ToolActivityDetails';
import type { TranscriptPresentation } from './transcriptPresentation';
import { formatDuration } from './TurnFooter';
import type { AgentEvent } from './types';

/** The recorded state of a delegated agent; never inferred from its transcript. */
function subagentStatus(agent: OverviewSubagent): string | undefined {
  const state = agent.activity.states.at(-1)?.status;
  if (state) return state;
  const reply = agent.reply;
  if (reply?.state === 'received') return reply.status ?? 'completed';
  if (reply?.state === 'waiting') return 'running';
  if (reply?.state === 'failed' || reply?.state === 'cancelled') return reply.state;
  return undefined;
}

/** Slack for sub-pixel layout rounding when asking whether the end is reached. */
const END_EPSILON_PX = 2;

function atEnd(region: HTMLElement): boolean {
  return region.scrollHeight - region.scrollTop - region.clientHeight <= END_EPSILON_PX;
}

/**
 * One subagent read as its own conversation: the main agent's instructions,
 * then what the subagent recorded. Subagents are started by the coding agent,
 * so this view only reads; it has no composer.
 */
export function SubagentConversation({
  agent,
  events,
  jobId,
  presentation,
  onBack,
  onInspectEvent,
}: {
  agent: OverviewSubagent;
  /** This subagent's own rows, from `subagentDisplayEvents`. */
  events: AgentEvent[];
  jobId: string;
  presentation?: TranscriptPresentation;
  onBack: () => void;
  onInspectEvent?: InspectTranscriptEvent;
}) {
  const { activity, reply } = agent;
  const backRef = useRef<HTMLButtonElement>(null);
  // Whether the reader was at the end as of their last scroll. New records
  // follow only then; a reader who scrolled up is left where they are.
  const followingRef = useRef(true);
  // Opening a subagent starts at its instructions, wherever the Chat was read.
  // A layout effect declared first, so the follow below judges that position.
  useLayoutEffect(() => {
    const region = backRef.current?.closest<HTMLElement>('[role="region"]');
    region?.scrollTo?.({ top: 0 });
    backRef.current?.focus({ preventScroll: true });
    if (!region) return undefined;
    const onScroll = () => {
      followingRef.current = atEnd(region);
    };
    onScroll();
    region.addEventListener('scroll', onScroll, { passive: true });
    return () => region.removeEventListener('scroll', onScroll);
  }, [agent.key, jobId]);
  // Before paint, so a followed record never flashes in below the fold.
  useLayoutEffect(() => {
    const region = backRef.current?.closest<HTMLElement>('[role="region"]');
    if (region && followingRef.current && !atEnd(region)) region.scrollTop = region.scrollHeight;
  }, [events, reply]);

  const title = activity.description ?? activity.title;
  const status = subagentStatus(agent);
  const duration = formatDuration(agent.sourceEvent.durationMs);
  const meta = [activity.agentType, activity.model].filter((value): value is string => !!value);
  const answered = events.some((event) => event.kind === 'message');
  // Codex reports a finished agent's answer, or its error, as its state's message.
  const stateAnswer = activity.states.findLast((state) => state.message);
  let returned: DelegationReply | undefined = reply;
  if (!returned && stateAnswer) {
    returned = {
      state: stateAnswer.status === 'errored' ? 'failed' : 'received',
      text: stateAnswer.message,
    };
  }

  return (
    <section aria-label={`Subagent: ${title}`} className="flex-1">
      <header className="sticky top-0 z-10 -mx-1 -mt-4 mb-3 border-b border-gray-200 bg-gray-50 px-1 pb-3 pt-4 dark:border-white/10 dark:bg-gray-950">
        <button
          ref={backRef}
          type="button"
          onClick={onBack}
          className="ui-focus-ring rounded text-xs font-medium text-crimson hover:underline dark:text-crimson-light"
        >
          ← Back to conversation
        </button>
        <div className="mt-1.5 flex min-w-0 flex-wrap items-center gap-x-2 gap-y-1">
          <span className="text-[11px] font-medium uppercase tracking-wide text-violet-700 dark:text-violet-300">
            Subagent
          </span>
          <h2 className="min-w-0 break-words text-sm font-semibold text-gray-900 dark:text-gray-100">
            {title}
          </h2>
          {status ? <Status value={status} /> : null}
          {duration ? (
            <span className="text-[11px] tabular-nums text-gray-500 dark:text-gray-400">
              {duration}
            </span>
          ) : null}
        </div>
        {meta.length ? (
          <p className="mt-0.5 flex flex-wrap gap-x-3 text-xs text-gray-500 dark:text-gray-400">
            {meta.map((value) => (
              <span key={value}>{value}</span>
            ))}
          </p>
        ) : null}
      </header>

      {activity.prompt ? (
        <div aria-label="Instructions from the main agent">
          <p className="ml-auto w-fit max-w-[88%] text-[11px] text-gray-400 dark:text-gray-500">
            From the main agent
          </p>
          <UserMessage text={activity.prompt} presentation={presentation} />
        </div>
      ) : null}

      <div aria-label="Subagent activity" className="mt-2">
        <EventList
          events={events}
          presentation={presentation}
          completed={status === 'completed'}
          onInspectEvent={onInspectEvent}
          jobId={jobId}
          renderEvent={(event, _index, blockKey) => (
            <EventRow
              event={event}
              presentation={presentation}
              jobId={jobId}
              onInspectEvent={onInspectEvent}
              blockKey={blockKey}
            />
          )}
        />
      </div>

      {/* The subagent's last message is what it returned; show the returned
          text separately only when the transcript did not record it. */}
      {!answered && returned ? (
        <div className="mt-3">
          <DelegationReplyDetails reply={returned} />
        </div>
      ) : null}
      {!answered && !returned && events.length === 0 ? (
        <p className="py-2 text-[13px] text-gray-500 dark:text-gray-400">
          No activity has been recorded for this subagent.
        </p>
      ) : null}
    </section>
  );
}
