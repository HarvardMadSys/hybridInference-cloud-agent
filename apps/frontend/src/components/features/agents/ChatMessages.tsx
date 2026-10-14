'use client';

import {
  createContext,
  Fragment,
  memo,
  useCallback,
  useContext,
  useEffect,
  useId,
  useRef,
  useState,
  type ReactNode,
} from 'react';
import toast from 'react-hot-toast';

import { Markdown } from '@/components/ui/Markdown';
import { useT } from '@/components/providers';
import { GoalIcon } from './ComposerPlusMenu';
import type { TranslationKey } from '@/lib/i18n/dictionaries/en';
import { lifecyclePhaseLabel } from './adapt';
import {
  eventBlockKey,
  messageBlockKey,
  type ConversationTurnDescription,
} from './conversationTurnNavigation';
import type { AgentEvent, AgentThreadMessage, GoalTurn } from './types';
import { ToolActivityDetails } from './ToolActivityDetails';
import { parseToolActivity, recordedAgentStates, recordedTasks } from './toolActivity';
import { toolNeedsAttention, toolSummary } from './toolSummary';
import type { TranscriptPresentation } from './transcriptPresentation';

export type InspectTranscriptEvent = (event: AgentEvent, jobId: string) => void;

/** The subagent a delegating call started, when its own conversation can be shown. */
export type SubagentOfCall = (
  jobId: string,
  event: Extract<AgentEvent, { kind: 'tool_use' }>,
) => { title: string; open: () => void } | undefined;

/**
 * Supplied by the page around the main conversation, so a delegating call can
 * switch to the subagent it started. Absent elsewhere, including inside a
 * subagent's own conversation.
 */
export const SubagentLinks = createContext<SubagentOfCall | null>(null);

function SubagentSwitch({
  event,
  jobId,
  compact = false,
}: {
  event: Extract<AgentEvent, { kind: 'tool_use' }>;
  jobId?: string;
  compact?: boolean;
}) {
  const subagentOf = useContext(SubagentLinks);
  const subagent = jobId && subagentOf ? subagentOf(jobId, event) : undefined;
  if (!subagent) return null;
  return (
    <button
      type="button"
      onClick={subagent.open}
      aria-label={`Switch subagent: ${subagent.title}`}
      title="Switch to this subagent"
      className="ui-focus-ring inline-flex shrink-0 items-center gap-0.5 rounded text-[11px] font-medium text-crimson hover:underline dark:text-crimson-light"
    >
      {compact ? null : 'Switch to subagent'}
      <Chevron />
    </button>
  );
}

function Chevron({ className = '' }: { className?: string }) {
  return (
    <svg
      aria-hidden="true"
      className={`h-4 w-4 ${className}`}
      fill="none"
      viewBox="0 0 24 24"
      stroke="currentColor"
      strokeWidth={2}
    >
      <path strokeLinecap="round" strokeLinejoin="round" d="m9 18 6-6-6-6" />
    </svg>
  );
}

function ToolIcon({ tool }: { tool: string }) {
  const normalized = tool.toLowerCase();
  let path =
    'M14.7 6.3a5 5 0 0 0-6.4 6.4l-5 5a2.12 2.12 0 0 0 3 3l5-5a5 5 0 0 0 6.4-6.4l-3 3-3-3 3-3Z';
  if (['read', 'search', 'grep', 'glob'].some((name) => normalized.includes(name))) {
    path =
      'M12 6.25c-2.5-2-6.5-2-9 0v12c2.5-2 6.5-2 9 0 2.5-2 6.5-2 9 0v-12c-2.5-2-6.5-2-9 0Zm0 0v12';
  } else if (
    ['bash', 'command', 'exec', 'shell', 'terminal'].some((name) => normalized.includes(name))
  ) {
    path =
      'm7 8 4 4-4 4m6 0h4M4 4h16a1 1 0 0 1 1 1v14a1 1 0 0 1-1 1H4a1 1 0 0 1-1-1V5a1 1 0 0 1 1-1Z';
  } else if (['edit', 'write', 'patch'].some((name) => normalized.includes(name))) {
    path =
      'M16.86 4.49a1.5 1.5 0 0 1 2.12 0l.53.53a1.5 1.5 0 0 1 0 2.12L8.53 18.12 4 19.5l1.38-4.53L16.86 4.49Z';
  }
  return (
    <svg
      aria-hidden="true"
      className="h-4 w-4 shrink-0 text-gray-500 dark:text-gray-400"
      fill="none"
      viewBox="0 0 24 24"
      stroke="currentColor"
      strokeWidth={2}
    >
      <path strokeLinecap="round" strokeLinejoin="round" d={path} />
    </svg>
  );
}

// What a first run's milestones mean, said once: on the first turn of the
// owner's first conversation, and never again. The rest need no gloss, and a
// later turn's publishing updates the draft PR rather than opening one.
const FIRST_RUN_CAPTIONS: Partial<Record<string, TranslationKey>> = {
  started: 'firstRun.started',
  workspace_preparing: 'firstRun.preparing',
  checked_out: 'firstRun.checkedOut',
  workspace_created: 'firstRun.workspaceCreated',
  publishing: 'firstRun.publishing',
  shared_snapshot_publishing: 'firstRun.publishing',
};

function LifecycleRow({
  text,
  blockKey,
  firstRun = false,
}: {
  text: string;
  blockKey?: string;
  firstRun?: boolean;
}) {
  const t = useT();
  const { text: label, milestone } = lifecyclePhaseLabel(text);
  const attention = lifecycleNeedsAttention(text);

  // A phase this build does not recognise is a diagnostic, not an achievement:
  // no tick, muted, and clearly a passthrough of what the runtime said.
  if (!milestone) {
    return (
      <div
        data-block-key={blockKey}
        className="flex items-center gap-2.5 py-1 text-xs text-gray-400 dark:text-gray-500"
      >
        <span className="h-1 w-1 rounded-full bg-gray-300 dark:bg-gray-600" />
        <span className="font-mono">{label}</span>
      </div>
    );
  }

  const caption = firstRun ? FIRST_RUN_CAPTIONS[text.trim().toLowerCase()] : undefined;
  const row = (
    <div
      data-block-key={caption ? undefined : blockKey}
      className={`flex items-center gap-2.5 ${caption ? 'pt-2' : 'py-2'} text-[13px] text-gray-500 dark:text-gray-400`}
    >
      <span className="flex h-4 w-4 items-center justify-center rounded-full bg-gray-100 dark:bg-white/5 text-gray-500 dark:text-gray-400">
        <svg
          aria-hidden="true"
          className="h-3 w-3"
          fill="none"
          viewBox="0 0 24 24"
          stroke="currentColor"
          strokeWidth={2.5}
        >
          <path
            strokeLinecap="round"
            strokeLinejoin="round"
            d={attention ? 'm7 7 10 10M7 17 17 7' : 'm6 12 4 4 8-9'}
          />
        </svg>
      </span>
      <span className="capitalize">{label}</span>
    </div>
  );
  if (!caption) return row;
  return (
    <div data-block-key={blockKey} className="pb-2">
      {row}
      <p className="ml-[26px] mt-0.5 text-xs leading-relaxed text-gray-400 dark:text-gray-500">
        {t(caption)}
      </p>
    </div>
  );
}

type ToolActivityItem = {
  event: Extract<AgentEvent, { kind: 'tool_use' }>;
  blockKey?: string;
};

type DisclosureState = {
  expanded: boolean;
  presentation?: TranscriptPresentation;
};

// A compact process body is unmounted while folded. Its small state store
// remembers the reader's disclosures without retaining the transcript DOM.
const ProcessDisclosureState = createContext<Map<string, DisclosureState> | null>(null);

function useToolDisclosure(key: string, presentation?: TranscriptPresentation) {
  const states = useContext(ProcessDisclosureState);
  const [expanded, setExpanded] = useState(() => {
    const saved = states?.get(key);
    return saved && saved.presentation === presentation
      ? saved.expanded
      : presentation === 'detailed';
  });
  const previousPresentation = useRef(presentation);
  useEffect(() => {
    if (previousPresentation.current === presentation) return;
    previousPresentation.current = presentation;
    setExpanded(presentation === 'detailed');
  }, [presentation]);
  useEffect(() => {
    states?.set(key, { expanded, presentation });
  }, [states, key, expanded, presentation]);
  return [expanded, setExpanded] as const;
}

function toolResultLabel(event: ToolActivityItem['event'], t: ReturnType<typeof useT>): string {
  return t(
    event.resultState === 'unknown'
      ? 'transcript.callUnknown'
      : event.resultState === 'cancelled'
        ? 'transcript.callInterrupted'
        : event.outputIsError || event.resultState === 'failed'
          ? 'transcript.callFailed'
          : event.resultState === 'not_recorded'
            ? 'transcript.callNotRecorded'
            : event.resultState === 'waiting'
              ? 'transcript.callWaiting'
              : event.resultState === 'received' || event.output !== undefined
                ? 'transcript.callReceived'
                : 'transcript.callNotRecorded',
  );
}

function RichToolActivity({
  event,
  blockKey,
  presentation,
  jobId,
  onInspectEvent,
}: {
  event: Extract<AgentEvent, { kind: 'tool_use' }>;
  blockKey?: string;
  presentation?: TranscriptPresentation;
  jobId?: string;
  onInspectEvent?: InspectTranscriptEvent;
}) {
  const t = useT();
  const disclosureId = useId();
  const [detailsExpanded, setDetailsExpanded] = useToolDisclosure(
    `call:${blockKey ?? disclosureId}`,
    presentation,
  );
  const activity = parseToolActivity(event.tool, event.input);
  const resultLabel = toolResultLabel(event, t);
  if (activity && presentation !== 'compact') {
    return (
      <section
        data-block-key={blockKey}
        aria-label={activity.kind === 'tasks' ? 'Task activity' : 'Subagent activity'}
        className={`my-2 min-w-0 rounded-lg border border-gray-200 bg-gray-50 dark:border-gray-700 dark:bg-gray-800 ${event.parentToolUseId ? 'ml-4 border-l-2' : ''}`}
      >
        <div className="flex flex-wrap items-center gap-2 border-b border-gray-200 px-3 py-2 dark:border-gray-700">
          <ToolIcon tool={event.tool} />
          <span className="text-[13px] font-medium text-gray-700 dark:text-gray-300">
            {activity.kind === 'tasks' ? 'Tasks' : 'Subagent'}
          </span>
          {onInspectEvent && jobId ? (
            <button
              type="button"
              onClick={() => onInspectEvent(event, jobId)}
              aria-label={t('transcript.inspectTool', { summary: toolSummary(event) })}
              className="ui-focus-ring min-w-0 truncate rounded text-left text-xs text-gray-600 hover:text-gray-900 dark:text-gray-300 dark:hover:text-gray-100"
            >
              {toolSummary(event)}
            </button>
          ) : (
            <span className="break-all font-mono text-[11px] text-gray-500 dark:text-gray-400">
              {toolSummary(event)}
            </span>
          )}
          <span
            className={`ml-auto text-[11px] ${event.outputIsError || event.resultState === 'failed' ? 'text-red-700 dark:text-red-300' : 'text-gray-500 dark:text-gray-400'}`}
          >
            {resultLabel}
          </span>
          {activity.kind === 'delegation' ? <SubagentSwitch event={event} jobId={jobId} /> : null}
        </div>
        <div className="space-y-1.5 px-3 py-2">
          <ToolActivityDetails
            activity={activity}
            output={event.output?.join('\n')}
            isError={event.outputIsError}
          />
          <details
            className="group"
            open={detailsExpanded}
            onToggle={(event) => setDetailsExpanded(event.currentTarget.open)}
          >
            <summary className="cursor-pointer text-xs text-gray-500 dark:text-gray-400">
              Call details
            </summary>
            {detailsExpanded ? (
              <>
                <pre className="mt-2 max-h-48 overflow-auto whitespace-pre-wrap break-words rounded-md bg-gray-950 p-3 font-mono text-xs text-gray-200">
                  {event.input === undefined ? event.detail : JSON.stringify(event.input, null, 2)}
                </pre>
                {event.output !== undefined ? (
                  <pre
                    className={`mt-2 max-h-72 overflow-auto whitespace-pre-wrap break-words rounded-md p-3 font-mono text-xs ${event.outputIsError ? 'bg-red-950 text-red-100' : 'bg-gray-950 text-gray-200'}`}
                  >
                    {event.output.join('\n')}
                  </pre>
                ) : null}
              </>
            ) : null}
          </details>
        </div>
      </section>
    );
  }
  return (
    <ToolActivity tools={[{ event, blockKey }]} {...{ presentation, jobId, onInspectEvent }} />
  );
}

function ToolActivityBody({
  tools,
  presentation,
  jobId,
  onInspectEvent,
}: {
  tools: ToolActivityItem[];
  presentation?: TranscriptPresentation;
  jobId?: string;
  onInspectEvent?: InspectTranscriptEvent;
}) {
  const t = useT();
  const detailsId = useId();
  const [expanded, setExpanded] = useToolDisclosure(
    `tools:${tools[0]?.blockKey ?? detailsId}`,
    presentation,
  );
  const label = tools.length === 1 ? 'tool call' : 'tool calls';

  return (
    <div className={`my-1 min-w-0 ${tools[0]?.event.parentToolUseId ? 'ml-4' : ''}`}>
      <div className="flex min-w-0 items-center gap-2 py-1 text-xs text-gray-500 dark:text-gray-400">
        <ul
          role="list"
          aria-label="Tool calls"
          tabIndex={0}
          className="flex min-w-0 flex-1 flex-nowrap items-center gap-1.5 overflow-x-auto rounded-md focus-visible:outline-crimson"
        >
          {tools.map(({ event, blockKey }, index) => {
            // Compact shows the tool alone; richer views add what it ran on.
            const label = presentation === 'compact' ? event.tool : toolSummary(event);
            return (
              <li
                key={blockKey ?? index}
                data-block-key={blockKey}
                title={`${toolSummary(event)} · ${toolResultLabel(event, t)}`}
                className="inline-flex shrink-0 items-center gap-1.5 rounded-md bg-gray-100 px-2 py-1 dark:bg-gray-800"
              >
                <ToolIcon tool={event.tool} />
                {event.parentToolUseId ? (
                  <span
                    title={`Delegated by ${event.parentToolUseId}`}
                    className="text-[10px] text-gray-600 dark:text-gray-400"
                  >
                    Subagent tool
                  </span>
                ) : null}
                {onInspectEvent && jobId ? (
                  <button
                    type="button"
                    onClick={() => onInspectEvent(event, jobId)}
                    aria-label={t('transcript.inspectTool', { summary: toolSummary(event) })}
                    className="ui-focus-ring max-w-80 truncate rounded text-left font-medium text-gray-700 hover:text-gray-900 dark:text-gray-300 dark:hover:text-gray-100"
                  >
                    {label}
                  </button>
                ) : (
                  <span className="max-w-80 truncate font-medium text-gray-700 dark:text-gray-300">
                    {label}
                  </span>
                )}
                {event.diffStat ? (
                  <span className="max-w-20 truncate font-mono" title={event.diffStat}>
                    {event.diffStat}
                  </span>
                ) : null}
                <span
                  className={
                    event.outputIsError || event.resultState === 'failed'
                      ? 'text-rose-600 dark:text-rose-400'
                      : 'text-gray-500 dark:text-gray-400'
                  }
                >
                  {toolResultLabel(event, t)}
                </span>
                <SubagentSwitch event={event} jobId={jobId} compact />
              </li>
            );
          })}
        </ul>
        <button
          type="button"
          aria-expanded={expanded}
          aria-controls={detailsId}
          onClick={() => setExpanded((value) => !value)}
          className="inline-flex shrink-0 items-center gap-1 rounded-md px-1.5 py-1 text-[11px] transition-colors hover:bg-gray-100 hover:text-gray-900 focus-visible:outline-crimson dark:hover:bg-white/10 dark:hover:text-gray-100"
        >
          <Chevron className={`transition-transform ${expanded ? 'rotate-90' : ''}`} />
          {expanded ? `Hide ${label}` : `Show ${label}`}
        </button>
      </div>
      <div
        id={detailsId}
        hidden={!expanded}
        className="mt-1 space-y-3 rounded-lg border border-gray-200 bg-gray-50 px-3 py-2.5 dark:border-gray-700 dark:bg-gray-800"
      >
        {expanded &&
          tools.map(({ event, blockKey }, index) => {
            const call =
              event.input === undefined
                ? event.detail
                : typeof event.input === 'string'
                  ? event.input
                  : JSON.stringify(event.input, null, 2);
            return (
              <section
                key={blockKey ?? index}
                className="space-y-2 border-b border-gray-200 pb-3 last:border-b-0 last:pb-0 dark:border-gray-700"
              >
                <p className="flex items-center gap-1.5 text-xs font-medium text-gray-700 dark:text-gray-300">
                  <ToolIcon tool={event.tool} />
                  <span>
                    {event.tool} · Call {index + 1}
                  </span>
                </p>
                <p className="text-xs text-gray-500 dark:text-gray-400">Tool call</p>
                <pre className="max-h-72 overflow-auto whitespace-pre-wrap rounded-md bg-gray-100 px-3 py-2.5 font-mono text-xs leading-relaxed text-gray-700 dark:bg-gray-900 dark:text-gray-300">
                  {call}
                </pre>
                <p className="text-xs text-gray-500 dark:text-gray-400">Result</p>
                {event.output !== undefined ? (
                  <pre
                    className={`max-h-72 overflow-auto whitespace-pre-wrap rounded-md px-3 py-2.5 font-mono text-xs leading-relaxed ${
                      event.outputIsError ? 'bg-red-950 text-red-100' : 'bg-gray-950 text-gray-200'
                    }`}
                  >
                    {event.output.join('\n') || 'No output text recorded.'}
                  </pre>
                ) : (
                  <p className="text-xs text-gray-400 dark:text-gray-500">
                    {event.resultState && event.resultState !== 'waiting'
                      ? toolResultLabel(event, t)
                      : 'Waiting for the tool result…'}
                  </p>
                )}
              </section>
            );
          })}
      </div>
    </div>
  );
}

const ToolActivity = memo(
  ToolActivityBody,
  (previous, next) =>
    previous.presentation === next.presentation &&
    previous.jobId === next.jobId &&
    previous.onInspectEvent === next.onInspectEvent &&
    previous.tools.length === next.tools.length &&
    previous.tools.every(
      (tool, index) =>
        tool.event === next.tools[index].event && tool.blockKey === next.tools[index].blockKey,
    ),
);

/** Consecutive reasoning and calls share one activity group. */
export function EventList({
  events,
  jobId,
  renderEvent,
  showActivityDetails = true,
  presentation,
  completed = false,
  onInspectEvent,
}: {
  events: AgentEvent[];
  jobId: string;
  /** Ungrouped rows; reasoning and tool groups use the shared transcript views. */
  renderEvent: (event: AgentEvent, index: number, blockKey: string) => ReactNode;
  /** Keep task/delegation calls compact when their overview is shown elsewhere. */
  showActivityDetails?: boolean;
  presentation?: TranscriptPresentation;
  /** Only a recorded completed turn may fold. Failed/interrupted/unknown turns stay open. */
  completed?: boolean;
  onInspectEvent?: InspectTranscriptEvent;
}) {
  const rows: {
    key: string;
    node: ReactNode;
    foldable: boolean;
    process: boolean;
    count: number;
    attemptNo?: number;
  }[] = [];
  const richActivity =
    presentation === 'detailed' || (presentation !== 'compact' && showActivityDetails);
  const isCompactActivity = (event: AgentEvent): boolean =>
    event.kind === 'thinking' ||
    (event.kind === 'tool_use' && (!richActivity || !parseToolActivity(event.tool, event.input)));

  for (let index = 0; index < events.length; index += 1) {
    const event = events[index];
    const blockKey = eventBlockKey(jobId, event, index);
    if (!isCompactActivity(event)) {
      rows.push({
        key: blockKey,
        node:
          event.kind === 'tool_use' && presentation !== undefined ? (
            <RichToolActivity
              event={event}
              blockKey={blockKey}
              {...{ presentation, jobId, onInspectEvent }}
            />
          ) : (
            renderEvent(event, index, blockKey)
          ),
        foldable: processMayFold(event),
        process: isProcessEvent(event),
        count: 1,
        attemptNo: event.attemptNo,
      });
      continue;
    }
    const parentToolUseId = event.kind === 'tool_use' ? event.parentToolUseId : undefined;
    const foldable = processMayFold(event);
    const tools: ToolActivityItem[] = [];
    const reasoning: {
      event: Extract<AgentEvent, { kind: 'thinking' }>;
      index: number;
      blockKey: string;
    }[] = [];
    let nextIndex = index;
    while (nextIndex < events.length) {
      const next = events[nextIndex];
      if (
        !isCompactActivity(next) ||
        next.attemptNo !== event.attemptNo ||
        // Completed activity can fold around a call that still needs attention.
        // Keep the same partitions in every mode so reading anchors stay put.
        (completed && processMayFold(next) !== foldable) ||
        (next.kind === 'tool_use' ? next.parentToolUseId : undefined) !== parentToolUseId
      )
        break;
      const nextBlockKey = eventBlockKey(jobId, next, nextIndex);
      if (next.kind === 'thinking') {
        reasoning.push({ event: next, index: nextIndex, blockKey: nextBlockKey });
      } else if (next.kind === 'tool_use') {
        tools.push({ event: next, blockKey: nextBlockKey });
      }
      nextIndex += 1;
    }
    index = nextIndex - 1;
    const firstReasoning = reasoning[0];
    const reasoningBlocks = reasoning.flatMap(
      ({ event: thought }) => thought.reasoningBlocks ?? [{ text: thought.text }],
    );
    rows.push({
      key: blockKey,
      node: (
        <Fragment>
          {firstReasoning ? (
            <div key="reasoning" data-block-key={firstReasoning.blockKey} className="relative">
              {/* The group owns its anchors outside the collapsed disclosure;
                  the shared view keeps standalone and grouped reasoning identical. */}
              <ReasoningMessage
                event={{
                  ...firstReasoning.event,
                  text: reasoningBlocks.map((block) => block.text).join('\n\n'),
                  reasoningBlocks,
                }}
              />
              {/* Grouped reasoning keeps each reading anchor at its shared section. */}
              {reasoning.slice(1).map((item) => (
                <span
                  key={item.blockKey}
                  data-block-key={item.blockKey}
                  aria-hidden="true"
                  className="pointer-events-none absolute inset-0"
                />
              ))}
            </div>
          ) : null}
          {tools.length > 0 ? (
            <ToolActivity key="tools" tools={tools} {...{ presentation, jobId, onInspectEvent }} />
          ) : null}
        </Fragment>
      ),
      foldable: tools.every(({ event: tool }) => processMayFold(tool)),
      process: true,
      count: tools.length + reasoning.length,
      attemptNo: event.attemptNo,
    });
  }
  const content: ReactNode[] = [];
  for (let index = 0; index < rows.length; index += 1) {
    const row = rows[index];
    if (!row.process) {
      content.push(<Fragment key={row.key}>{row.node}</Fragment>);
      continue;
    }
    const process = [row];
    while (
      index + 1 < rows.length &&
      rows[index + 1].process &&
      (!completed || rows[index + 1].foldable === row.foldable) &&
      rows[index + 1].attemptNo === row.attemptNo
    ) {
      process.push(rows[++index]);
    }
    content.push(
      <ProcessDisclosure
        key={row.key}
        enabled={completed && presentation === 'compact' && process.every((item) => item.foldable)}
        count={process.reduce((count, item) => count + item.count, 0)}
      >
        {process.map((item) => (
          <Fragment key={item.key}>{item.node}</Fragment>
        ))}
      </ProcessDisclosure>,
    );
  }
  return <>{content}</>;
}

/** Fold classified process rows; input, replies and incomplete/error records stay readable. */
function processMayFold(event: AgentEvent): boolean {
  if (event.kind === 'tool_use') {
    if (toolNeedsAttention(event)) return false;
    const activity = parseToolActivity(event.tool, event.input);
    if (activity) {
      const output = event.output?.join('\n');
      const states =
        activity.kind === 'tasks'
          ? [...activity.tasks, ...recordedTasks(output)]
          : [...activity.states, ...recordedAgentStates(output)];
      // A successful tool response does not establish that its delegated work succeeded.
      if (states.some(({ status }) => !['completed', 'deleted', 'shutdown'].includes(status ?? '')))
        return false;
    }
    return true;
  }
  if (event.kind === 'thinking' || event.kind === 'usage') return true;
  if (event.kind === 'terminal') return event.isError === false;
  if (event.kind === 'lifecycle')
    return lifecyclePhaseLabel(event.text).milestone && !lifecycleNeedsAttention(event.text);
  return false;
}

function isProcessEvent(event: AgentEvent): boolean {
  return (
    event.kind === 'tool_use' ||
    event.kind === 'thinking' ||
    event.kind === 'usage' ||
    event.kind === 'terminal' ||
    (event.kind === 'lifecycle' &&
      lifecyclePhaseLabel(event.text).milestone &&
      !lifecycleNeedsAttention(event.text))
  );
}

function lifecycleNeedsAttention(text: string): boolean {
  const phase = text.trim().toLowerCase();
  return phase === 'cancelled_by_owner' || phase.includes('superseded');
}

function ProcessDisclosure({
  enabled,
  count,
  children,
}: {
  enabled: boolean;
  count: number;
  children: ReactNode;
}) {
  const t = useT();
  const [expanded, setExpanded] = useState(false);
  useEffect(() => {
    setExpanded(false);
  }, [enabled]);
  const bodyId = useId();
  const states = useRef(new Map<string, DisclosureState>());
  const collapsed = enabled && !expanded;
  return (
    <section data-transcript-process data-transcript-process-collapsed={collapsed || undefined}>
      {enabled ? (
        <button
          type="button"
          aria-expanded={expanded}
          aria-controls={bodyId}
          onClick={() => setExpanded((value) => !value)}
          className="my-2 inline-flex items-center gap-1.5 rounded-md px-2 py-1 text-xs text-gray-500 hover:bg-gray-100 focus-visible:outline-crimson dark:text-gray-400 dark:hover:bg-white/5"
        >
          <Chevron className={expanded ? 'rotate-90' : ''} />
          {t(expanded ? 'transcript.hideProcess' : 'transcript.showProcess', { count })}
        </button>
      ) : null}
      <div id={bodyId} hidden={collapsed} aria-label={t('transcript.process')}>
        <ProcessDisclosureState.Provider value={states.current}>
          {collapsed ? null : children}
        </ProcessDisclosureState.Provider>
      </div>
    </section>
  );
}

export function CopyIcon() {
  return (
    <svg
      aria-hidden="true"
      className="h-3.5 w-3.5"
      fill="none"
      viewBox="0 0 24 24"
      stroke="currentColor"
      strokeWidth={2}
    >
      <rect x="9" y="9" width="11" height="11" rx="2" />
      <path strokeLinecap="round" d="M5 15V7a2 2 0 0 1 2-2h8" />
    </svg>
  );
}

function ForkIcon() {
  return (
    <svg
      aria-hidden="true"
      className="h-3.5 w-3.5"
      fill="none"
      viewBox="0 0 24 24"
      stroke="currentColor"
      strokeWidth={2}
    >
      <circle cx="6" cy="5" r="2" />
      <circle cx="18" cy="5" r="2" />
      <circle cx="6" cy="19" r="2" />
      <path strokeLinecap="round" d="M6 7v10m12-10v3a4 4 0 0 1-4 4h-4" />
    </svg>
  );
}

export function RewindIcon() {
  return (
    <svg
      aria-hidden="true"
      className="h-3.5 w-3.5"
      fill="none"
      viewBox="0 0 24 24"
      stroke="currentColor"
      strokeWidth={2}
    >
      <path
        strokeLinecap="round"
        strokeLinejoin="round"
        d="M9 15 3 9l6-6M3 9h12a6 6 0 0 1 0 12h-3"
      />
    </svg>
  );
}

export async function copyMessage(content: string) {
  try {
    await navigator.clipboard.writeText(content);
    toast.success('Message copied');
  } catch {
    toast.error('Could not copy the message');
  }
}

export function MessageActionButton({
  label,
  onClick,
  disabled,
  children,
}: {
  label: string;
  onClick: () => void;
  disabled?: boolean;
  children: ReactNode;
}) {
  return (
    <button
      type="button"
      onClick={onClick}
      disabled={disabled}
      aria-label={label}
      title={label}
      className="ui-focus-ring inline-flex min-h-9 items-center justify-center gap-1.5 rounded-lg px-2 py-1.5 text-xs text-gray-500 dark:text-gray-400 hover:bg-gray-100 dark:hover:bg-white/10 hover:text-gray-700 dark:hover:text-gray-200 disabled:opacity-40"
    >
      {children}
    </button>
  );
}

/** Actions stay visible on touch; hover and keyboard focus reveal them on desktop. */
export function MessageActions({
  align = 'start',
  className = '',
  children,
}: {
  align?: 'start' | 'end';
  className?: string;
  children: ReactNode;
}) {
  return (
    <div
      className={`mt-1 flex flex-wrap gap-1 opacity-100 transition-opacity md:opacity-0 focus-within:opacity-100 group-hover:opacity-100 [@media(hover:none)]:!opacity-100 [@media(pointer:coarse)]:!opacity-100 ${
        align === 'end' ? 'justify-end' : ''
      } ${className}`}
    >
      {children}
    </div>
  );
}

/** Compact messages keep Copy beside the content without reserving an action row. */
export function MessageContent({
  text,
  presentation,
}: {
  text: string;
  presentation?: TranscriptPresentation;
}) {
  if (presentation !== 'compact') return <Markdown text={text} />;
  return (
    <div className="flex items-start gap-2">
      <div className="min-w-0 flex-1">
        <Markdown text={text} />
      </div>
      <button
        type="button"
        aria-label="Copy message"
        title="Copy message"
        onClick={() => void copyMessage(text)}
        className="ui-focus-ring inline-flex h-6 w-6 shrink-0 items-center justify-center rounded-md text-gray-500 transition-opacity hover:bg-gray-100 hover:text-gray-700 md:opacity-0 group-hover:opacity-100 focus-visible:opacity-100 dark:text-gray-400 dark:hover:bg-white/10 dark:hover:text-gray-200 [@media(hover:none)]:!opacity-100 [@media(pointer:coarse)]:!opacity-100 [@media(pointer:coarse)]:h-11 [@media(pointer:coarse)]:w-11"
      >
        <CopyIcon />
      </button>
    </div>
  );
}

function AssistantMessage({
  text,
  extraActions,
  footer,
  blockKey,
  presentation,
}: {
  text: string;
  extraActions?: ReactNode;
  footer?: ReactNode;
  /** Stable row identity, so a layout change never moves a reader off it. */
  blockKey?: string;
  presentation?: TranscriptPresentation;
}) {
  const actions =
    presentation !== 'compact' || extraActions ? (
      <MessageActions>
        {presentation !== 'compact' ? (
          <MessageActionButton label="Copy message" onClick={() => void copyMessage(text)}>
            <CopyIcon />
            Copy
          </MessageActionButton>
        ) : null}
        {extraActions}
      </MessageActions>
    ) : null;
  return (
    <div data-block-key={blockKey} className="group py-3 text-gray-800 dark:text-gray-200">
      <MessageContent text={text} presentation={presentation} />
      {footer ? (
        <div className="mt-2 flex flex-wrap items-center justify-between gap-x-4 gap-y-1">
          {footer}
          {actions}
        </div>
      ) : (
        actions
      )}
    </div>
  );
}

export function ThreadTurn({
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
  onRewind?: () => void;
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

export function UserMessage({
  text,
  blockKey,
  busy,
  onRewind,
  presentation,
}: {
  text: string;
  blockKey?: string;
  busy?: boolean;
  onRewind?: () => void;
  presentation?: TranscriptPresentation;
}) {
  return (
    <div data-block-key={blockKey} className="group my-2 ml-auto w-fit max-w-[88%]">
      <div className="flex min-w-0 items-start gap-2 rounded-xl bg-gray-100 px-3 py-2 text-sm leading-6 text-gray-800 dark:bg-white/5 dark:text-gray-200">
        <div className="min-w-0 flex-1">
          <MessageContent text={text} presentation={presentation} />
        </div>
        {presentation !== 'compact' ? (
          <MessageActions align="end" className="!mt-0 shrink-0">
            <MessageActionButton label="Copy message" onClick={() => void copyMessage(text)}>
              <CopyIcon />
              Copy
            </MessageActionButton>
          </MessageActions>
        ) : null}
      </div>
      {onRewind ? (
        <MessageActions align="end" className="mt-0">
          <MessageActionButton label="Edit & rewind" onClick={onRewind} disabled={busy}>
            <RewindIcon />
            Edit & rewind
          </MessageActionButton>
        </MessageActions>
      ) : null}
    </div>
  );
}

function ReasoningMessage({
  event,
  blockKey,
}: {
  event: Extract<AgentEvent, { kind: 'thinking' }>;
  blockKey?: string;
}) {
  const states = useContext(ProcessDisclosureState);
  const details = useRef<HTMLDetailsElement | null>(null);
  const disclosureKey = `reasoning:${blockKey ?? event.sourceEventId ?? 'group'}`;
  const rememberDetails = useCallback(
    (element: HTMLDetailsElement | null) => {
      if (element === null && details.current) {
        states?.set(disclosureKey, { expanded: details.current.open });
      }
      details.current = element;
    },
    [states, disclosureKey],
  );
  const blocks = (event.reasoningBlocks ?? [{ text: event.text, channel: undefined }]).filter(
    (block) => block.text.trim(),
  );
  if (blocks.length) {
    return (
      <details
        ref={rememberDetails}
        open={states?.get(disclosureKey)?.expanded}
        data-block-key={blockKey}
        aria-label="Agent reasoning"
        className="group my-2 rounded-lg border border-gray-200 bg-gray-50 dark:border-white/10 dark:bg-white/[0.025]"
      >
        <summary className="flex cursor-pointer list-none items-center gap-2 px-3 py-2 text-[13px] text-gray-500 dark:text-gray-400">
          <Chevron className="transition-transform group-open:rotate-90" />
          Reasoning
        </summary>
        <div className="space-y-3 border-t border-gray-200 px-3 py-3 text-gray-600 dark:border-white/10 dark:text-gray-400">
          {blocks.map((block, index) => (
            <div key={`${block.channel ?? 'text'}:${index}`}>
              {blocks.length > 1 && block.channel ? (
                <p className="mb-1 text-xs font-medium">
                  {block.channel === 'summary' ? 'Summary' : 'Detailed reasoning'}
                </p>
              ) : null}
              <Markdown text={block.text} />
            </div>
          ))}
        </div>
      </details>
    );
  }
  return (
    <div
      data-block-key={blockKey}
      aria-label="Agent reasoning"
      className="flex items-center gap-2.5 py-2 text-[13px] text-gray-400 dark:text-gray-500"
    >
      <span className="h-1.5 w-1.5 rounded-full bg-gray-300 dark:bg-gray-600" />
      Agent is reasoning
    </div>
  );
}

export function EventRow({
  event,
  onFork,
  busy,
  footer,
  blockKey,
  firstRun,
  presentation,
  jobId,
  onInspectEvent,
}: {
  event: AgentEvent;
  onFork?: () => void;
  busy?: boolean;
  footer?: ReactNode;
  /** Stable row identity, so a layout change never moves a reader off it. */
  blockKey?: string;
  /** The first turn of the owner's first conversation: milestones say what they mean. */
  firstRun?: boolean;
  presentation?: TranscriptPresentation;
  jobId?: string;
  onInspectEvent?: InspectTranscriptEvent;
}) {
  const t = useT();
  if (event.kind === 'lifecycle')
    return <LifecycleRow text={event.text} blockKey={blockKey} firstRun={firstRun} />;
  if (event.kind === 'thinking') return <ReasoningMessage event={event} blockKey={blockKey} />;
  if (event.kind === 'message') {
    return (
      <AssistantMessage
        text={event.text}
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
  if (event.kind === 'user_message')
    return <UserMessage text={event.text} blockKey={blockKey} presentation={presentation} />;
  if (event.kind === 'tool_use')
    return (
      <RichToolActivity
        event={event}
        blockKey={blockKey}
        {...{ presentation, jobId, onInspectEvent }}
      />
    );
  if (event.kind === 'tool_result') {
    return (
      <details
        data-block-key={blockKey}
        className="my-1 rounded-lg border border-gray-200 dark:border-white/10 bg-white dark:bg-gray-900"
      >
        <summary
          className={`cursor-pointer px-3 py-2 text-[13px] ${event.isError ? 'text-rose-600 dark:text-rose-400' : 'text-gray-500 dark:text-gray-400'}`}
        >
          Unmatched tool result
          {event.cancelled
            ? ` · ${t('transcript.callInterrupted')}`
            : event.isError
              ? ` · ${t('transcript.callFailed')}`
              : ''}
        </summary>
        <pre className="m-3 max-h-72 overflow-auto whitespace-pre-wrap rounded-md bg-gray-950 px-3 py-2.5 font-mono text-xs text-gray-200">
          {event.text || 'Command completed with no output.'}
        </pre>
      </details>
    );
  }
  if (event.kind === 'terminal') {
    return (
      <pre
        data-block-key={blockKey}
        className="my-1 max-h-72 overflow-auto whitespace-pre-wrap rounded-md bg-gray-950 px-3 py-2.5 font-mono text-xs text-gray-200"
      >
        {event.text}
      </pre>
    );
  }
  return (
    <p data-block-key={blockKey} className="py-1 text-[11px] text-gray-400 dark:text-gray-500">
      {event.text}
    </p>
  );
}

export function HistoryTurn({
  turn,
  rewindAnchorJobId,
  busy,
  onFork,
  onRewind,
  onOpenRun,
  showActivityDetails = true,
  presentation,
  onInspectEvent,
  results,
}: {
  turn: ConversationTurnDescription;
  rewindAnchorJobId?: string;
  busy: boolean;
  onFork?: (jobId: string) => void;
  onRewind?: (jobId: string, prompt: string) => void;
  onOpenRun?: (jobId: string, turnNo: number) => void;
  showActivityDetails?: boolean;
  presentation?: TranscriptPresentation;
  onInspectEvent?: InspectTranscriptEvent;
  /** Recorded artifacts stay within this turn's navigation boundary. */
  results?: ReactNode;
}) {
  // What the transcript renders is decided once, by the turn description the
  // navigation rail also reads.
  const { jobId } = turn;
  const lastEventMessageIndex = turn.events.reduce(
    (last, event, index) => (event.kind === 'message' ? index : last),
    -1,
  );
  const lastFallbackMessageId = turn.fallbackResponses.at(-1)?.id;

  return (
    <section aria-label={`Turn ${turn.displayNo}`} data-turn-key={turn.key}>
      {turn.promptCard !== null ? (
        <UserMessage
          text={turn.promptCard}
          blockKey={`${jobId}:prompt`}
          presentation={presentation}
        />
      ) : null}
      {/* The prompt opens the turn; a message sent into it reads where it arrived. */}
      {turn.messages
        .filter((message) => message.role === 'user' && !message.midTurn)
        .map((message) => (
          <ThreadTurn
            key={message.id}
            message={message}
            goalTurn={turn.goalTurn}
            presentation={presentation}
            blockKey={messageBlockKey(jobId, message.id)}
            busy={busy}
            onRewind={
              rewindAnchorJobId && onRewind
                ? () => onRewind(rewindAnchorJobId, message.content)
                : undefined
            }
          />
        ))}
      <div aria-label={`Turn ${turn.displayNo} agent activity`} className="mt-2">
        <EventList
          events={turn.events}
          jobId={jobId}
          showActivityDetails={showActivityDetails}
          presentation={presentation}
          completed={turn.state === 'done' || turn.state === 'needs_review'}
          onInspectEvent={onInspectEvent}
          renderEvent={(event, index, blockKey) => (
            <EventRow
              event={event}
              blockKey={blockKey}
              busy={busy}
              presentation={presentation}
              jobId={jobId}
              onInspectEvent={onInspectEvent}
              onFork={
                event.kind === 'message' && index === lastEventMessageIndex && onFork
                  ? () => onFork(turn.jobId)
                  : undefined
              }
            />
          )}
        />
        {turn.fallbackResponses.map((message) => (
          <ThreadTurn
            key={message.id}
            message={message}
            blockKey={messageBlockKey(jobId, message.id)}
            presentation={presentation}
            busy={busy}
            onFork={
              message.id === lastFallbackMessageId && onFork ? () => onFork(turn.jobId) : undefined
            }
          />
        ))}
      </div>
      {results}
      {onOpenRun ? (
        <div className="mb-4 mt-2 flex justify-end text-[11px] text-gray-400 dark:text-gray-500">
          <button
            type="button"
            onClick={() => onOpenRun(turn.jobId, turn.displayNo)}
            className="rounded px-1.5 py-0.5 transition-colors hover:bg-gray-100 hover:text-gray-900 dark:hover:bg-white/10 dark:hover:text-gray-100"
          >
            View execution
          </button>
        </div>
      ) : null}
    </section>
  );
}
