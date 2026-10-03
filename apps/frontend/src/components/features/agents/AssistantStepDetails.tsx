'use client';

// The assistant detail: what one native message produced.
//
// Everything here belongs to that message alone. Turn totals are deliberately
// absent — a per-turn number placed on one step reads as this step's cost, and
// the ledger already answers the per-turn question in Run details.

import { useMessageRequest } from './useMessageRequest';
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
  const { detail, loading, retry } = useMessageRequest(step, tab === 'summary');
  const request = detail?.status === 'available' ? detail.request : null;
  const outputTokens = request ? request.tokens_out : usage?.outputTokens;
  const reasoningTokens = request ? request.reasoning : usage?.reasoningTokens;
  const generationMs =
    request?.latency_ms != null && request.ttft_ms != null && request.latency_ms >= request.ttft_ms
      ? request.latency_ms - request.ttft_ms
      : null;
  const throughput =
    generationMs && request?.tokens_out != null ? (request.tokens_out * 1000) / generationMs : null;
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
        {!hasContent && !tools.length ? (
          <EmptyNote>No message content was recorded for this step.</EmptyNote>
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
          {step.nativeMessageId || step.nativeItemId ? (
            <span className="font-mono">{step.nativeMessageId ?? step.nativeItemId}</span>
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
        <DetailRow label="Output tokens">{recordedCount(outputTokens)}</DetailRow>
        {request?.tokens_in != null ? (
          <DetailRow label="Input tokens">{recordedCount(request.tokens_in)}</DetailRow>
        ) : null}
        {reasoningTokens != null ? (
          <DetailRow label="Reasoning tokens">{recordedCount(reasoningTokens)}</DetailRow>
        ) : null}
        {usage?.contentTokens != null ? (
          <DetailRow label={request ? 'Content tokens (runtime)' : 'Content tokens'}>
            {recordedCount(usage.contentTokens)}
          </DetailRow>
        ) : null}
        <DetailRow label="Usage source">
          {request ? (request.usage_estimated ? 'Gateway estimate' : 'Gateway') : 'Runtime report'}
        </DetailRow>
        {request?.spent_usd != null ? (
          <DetailRow label="Cost">${request.spent_usd}</DetailRow>
        ) : null}
      </DetailRows>

      <DetailSection title="Preview" onOpen={() => onSelectTab('preview')}>
        {hasContent ? (
          <div className="max-h-40 overflow-hidden text-[12px] leading-relaxed text-gray-700 dark:text-gray-300">
            <Markdown text={blocks.find((block) => block.type === 'text')?.text ?? ''} />
          </div>
        ) : !tools.length ? (
          <EmptyNote>No message content was recorded.</EmptyNote>
        ) : null}
        {blocks.some((block) => block.type === 'thinking') ? (
          <p className="text-[11px] text-gray-400 dark:text-gray-500">
            {blocks.filter((block) => block.type === 'thinking').length} recorded thinking block
            {blocks.filter((block) => block.type === 'thinking').length === 1 ? '' : 's'} — open
            Preview to read them.
          </p>
        ) : null}
        <ToolCallList tools={tools} onSelectStep={onSelectStep} />
      </DetailSection>

      {request ? (
        <DetailSection title="Request timing">
          <DetailRows>
            <DetailRow label="Gateway request">
              <span className="font-mono">{request.request_id}</span>
            </DetailRow>
            <DetailRow label="Started">{recordedAt(request.request_started_at)}</DetailRow>
            {request.latency_ms !== null ? (
              <DetailRow label="Total duration">{formatDuration(request.latency_ms)}</DetailRow>
            ) : null}
            {request.ttft_ms !== null ? (
              <DetailRow label="TTFT">{formatDuration(request.ttft_ms)}</DetailRow>
            ) : null}
            {generationMs !== null ? (
              <DetailRow label="Generation">{formatDuration(generationMs)}</DetailRow>
            ) : null}
            {throughput !== null ? (
              <DetailRow label="Throughput">
                {throughput.toLocaleString(undefined, { maximumFractionDigits: 1 })} tok/s
              </DetailRow>
            ) : null}
          </DetailRows>
          <p className="text-[11px] text-gray-400 dark:text-gray-500">
            Gateway measurements for this message. Generation is the interval from first token to
            request completion; throughput is output tokens divided by that interval.
          </p>
        </DetailSection>
      ) : (
        <div className="text-[12px] text-gray-500 dark:text-gray-400">
          <p role="status">
            {loading
              ? 'Loading request statistics…'
              : detail?.status === 'ambiguous'
                ? 'Multiple requests share this message identity; statistics cannot be attributed.'
                : detail?.reason === 'message_correlation_unsupported'
                  ? 'This gateway does not yet support message request statistics.'
                  : detail?.status === 'unavailable'
                    ? 'Request statistics are temporarily unavailable.'
                    : 'No request statistics were recorded for this message.'}
          </p>
          {step.nativeMessageId && !loading ? (
            <button
              type="button"
              onClick={retry}
              className="mt-1 text-crimson hover:underline dark:text-crimson-light"
            >
              Retry request statistics
            </button>
          ) : null}
        </div>
      )}
    </TabPanel>
  );
}
