'use client';

// The assistant detail: what one native message produced.
//
// Everything here belongs to that message alone. Turn totals are deliberately
// absent — a per-turn number placed on one step reads as this step's cost, and
// the ledger already answers the per-turn question in Run details.

import { Markdown } from '@/components/ui/Markdown';

import {
  DetailRow,
  DetailRows,
  DetailSection,
  EmptyNote,
  StoredRecord,
  TabPanel,
  TruncationNote,
  copyText,
  recordedAt,
  recordedCount,
} from './StepDetailParts';
import { formatDuration } from './TurnFooter';
import { STATUS_LABEL, type TrajectoryStep } from './trajectorySteps';

export type AssistantTab = 'summary' | 'preview' | 'raw';

export const ASSISTANT_TABS: Array<{ key: AssistantTab; label: string }> = [
  { key: 'summary', label: 'Summary' },
  { key: 'preview', label: 'Preview' },
  { key: 'raw', label: 'Raw' },
];

/** Milliseconds from the first to the last record of one message, when readable. */
function recordedSpan(step: TrajectoryStep): number | null {
  const times = step.sourceEvents
    .map((event) => (event.created_at ? Date.parse(event.created_at) : NaN))
    .filter((value) => Number.isFinite(value));
  if (times.length < 2) return null;
  const span = Math.max(...times) - Math.min(...times);
  return span >= 0 ? span : null;
}

function ToolCallList({
  tools,
  onSelectStep,
}: {
  tools: TrajectoryStep[];
  onSelectStep?: (key: string) => void;
}) {
  if (tools.length === 0) return null;
  return (
    <ul className="space-y-1">
      {tools.map((tool) => (
        <li key={tool.key}>
          <button
            type="button"
            onClick={() => onSelectStep?.(tool.key)}
            className="flex w-full items-baseline gap-2 rounded border border-gray-200 px-2 py-1 text-left transition-colors hover:bg-gray-50 dark:border-white/10 dark:hover:bg-white/5"
          >
            <span className="shrink-0 font-medium text-gray-800 dark:text-gray-200">
              {tool.label}
            </span>
            <span className="min-w-0 flex-1 truncate font-mono text-[11px] text-gray-500 dark:text-gray-400">
              {tool.detail}
            </span>
            {tool.status === 'failed' ? (
              <span className="shrink-0 text-[11px] text-rose-600 dark:text-rose-400">Failed</span>
            ) : null}
          </button>
        </li>
      ))}
    </ul>
  );
}

export function AssistantStepDetails({
  step,
  tab,
  tools,
  onSelectStep,
  onSelectTab,
}: {
  step: TrajectoryStep;
  tab: AssistantTab;
  tools: TrajectoryStep[];
  onSelectStep?: (key: string) => void;
  onSelectTab: (tab: AssistantTab) => void;
}) {
  const idPrefix = `step-${step.key}`;
  const blocks = step.blocks ?? [];
  const usage = step.usage;
  const span = recordedSpan(step);
  const hasContent = blocks.some((block) => block.text.trim().length > 0);

  if (tab === 'preview') {
    return (
      <TabPanel idPrefix={idPrefix} tab="preview">
        {blocks.map((block, index) =>
          block.type === 'thinking' ? (
            <details key={index} className="rounded border border-gray-200 dark:border-white/10">
              <summary className="cursor-pointer px-3 py-2 text-[12px] text-gray-500 dark:text-gray-400">
                Thinking (recorded)
              </summary>
              <div className="border-t border-gray-100 px-3 py-2 text-[12px] leading-relaxed text-gray-700 dark:border-white/5 dark:text-gray-300">
                <Markdown text={block.text} />
              </div>
            </details>
          ) : (
            <div key={index} className="text-[13px] leading-relaxed">
              <Markdown text={block.text} />
            </div>
          ),
        )}
        {!hasContent && blocks.length === 0 ? (
          <EmptyNote>
            {tools.length ? '(tool call only)' : 'No message content was recorded for this step.'}
          </EmptyNote>
        ) : null}
        {tools.length ? (
          <DetailSection title="Tool calls">
            <ToolCallList tools={tools} onSelectStep={onSelectStep} />
          </DetailSection>
        ) : null}
        <p className="text-[11px] text-gray-400 dark:text-gray-500">
          This is the content the platform stored. A runtime may keep only some of the response
          blocks, so this is not a claim to reproduce the complete model output.
        </p>
      </TabPanel>
    );
  }

  if (tab === 'raw') {
    return (
      <TabPanel idPrefix={idPrefix} tab="raw">
        <div className="flex items-center justify-between gap-2">
          <p className="text-gray-500 dark:text-gray-400">
            {step.sourceEvents.length} stored record
            {step.sourceEvents.length === 1 ? '' : 's'} compose this step.
          </p>
          <button
            type="button"
            onClick={() =>
              void copyText(
                step.sourceEvents.map((event) => JSON.stringify(event)).join('\n'),
                'Records',
              )
            }
            className="rounded border border-gray-200 px-2 py-0.5 text-[11px] text-gray-500 hover:border-gray-300 hover:text-gray-900 dark:border-white/10 dark:text-gray-400 dark:hover:border-white/20 dark:hover:text-gray-100"
          >
            Copy all
          </button>
        </div>
        <TruncationNote payload={step.sourceEvents[0]?.payload ?? {}} />
        <div className="space-y-1.5">
          {step.sourceEvents.map((event) => (
            <StoredRecord
              key={event.id}
              title={`#${event.id} · ${event.event_type} · seq ${event.seq} · attempt ${event.attempt_id}`}
              event={event}
            />
          ))}
        </div>
        <p className="text-[11px] text-gray-400 dark:text-gray-500">
          These are the events the control plane stored after the runtime normalized them. When the
          raw provider response was not kept, this cannot show it.
        </p>
      </TabPanel>
    );
  }

  return (
    <TabPanel idPrefix={idPrefix} tab="summary">
      <DetailRows>
        <DetailRow label="Source">
          {step.nativeMessageId ? (
            <span className="font-mono">{step.nativeMessageId}</span>
          ) : (
            <span className="text-gray-400 dark:text-gray-500">Not recorded</span>
          )}
        </DetailRow>
        <DetailRow
          label="Status"
          tone={
            step.status === 'failed' ? 'error' : step.status === 'completed' ? 'default' : 'muted'
          }
        >
          {STATUS_LABEL[step.status]}
        </DetailRow>
        <DetailRow label="Tokens">{recordedCount(usage?.outputTokens)}</DetailRow>
        <DetailRow label="Reasoning">{recordedCount(usage?.reasoningTokens)}</DetailRow>
        <DetailRow label="Content">{recordedCount(usage?.contentTokens)}</DetailRow>
      </DetailRows>
      {step.nativeMessageId ? (
        <p className="text-[11px] text-gray-400 dark:text-gray-500">
          Source is the native message identity recorded with this content. It is not a Gateway
          request id, and no Gateway request was recorded for it.
        </p>
      ) : null}

      <DetailSection title="Preview" onOpen={() => onSelectTab('preview')}>
        {hasContent ? (
          <div className="max-h-40 overflow-hidden text-[12px] leading-relaxed text-gray-700 dark:text-gray-300">
            <Markdown text={blocks.find((block) => block.type === 'text')?.text ?? ''} />
          </div>
        ) : (
          <EmptyNote>
            {tools.length ? '(tool call only)' : 'No message content was recorded.'}
          </EmptyNote>
        )}
        {blocks.some((block) => block.type === 'thinking') ? (
          <p className="text-[11px] text-gray-400 dark:text-gray-500">
            {blocks.filter((block) => block.type === 'thinking').length} recorded thinking block
            {blocks.filter((block) => block.type === 'thinking').length === 1 ? '' : 's'} — open
            Preview to read them.
          </p>
        ) : null}
        <ToolCallList tools={tools} onSelectStep={onSelectStep} />
      </DetailSection>

      <DetailSection title="Request timing">
        <DetailRows>
          <DetailRow label="Started" tone="muted">
            Not recorded
          </DetailRow>
          <DetailRow label="Total duration" tone={span === null ? 'muted' : 'default'}>
            {span === null ? 'Not recorded' : formatDuration(span)}
          </DetailRow>
          <DetailRow label="TTFT" tone="muted">
            First token unavailable
          </DetailRow>
          <DetailRow label="Generation" tone="muted">
            Not recorded
          </DetailRow>
          <DetailRow label="Throughput" tone="muted">
            Not recorded
          </DetailRow>
        </DetailRows>
        <p className="text-[11px] text-gray-400 dark:text-gray-500">
          {span === null
            ? 'This run stored no request start or first-token time, so only the message content is timed.'
            : `The duration above is the interval between recorded blocks, not the model request. Recorded at ${recordedAt(step.at)}.`}
        </p>
      </DetailSection>
    </TabPanel>
  );
}
