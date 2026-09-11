'use client';

// The tool detail: what one call actually asked for and what came back.
//
// Payload and Result are read from the stored events, never from the row
// preview. A field that was not recorded says so; a field that was recorded as
// false or 0 is shown as false or 0.

import { useMemo } from 'react';

import {
  DetailRow,
  DetailRows,
  DetailSection,
  EmptyNote,
  StoredRecord,
  TabPanel,
  TextBlock,
  TruncationNote,
  ValueBlock,
  copyText,
  recordedAt,
} from './StepDetailParts';
import { formatDuration } from './TurnFooter';
import { STATUS_LABEL, type TrajectoryStep } from './trajectorySteps';

export type ToolTab = 'summary' | 'payload' | 'result' | 'schema' | 'timing';

export const TOOL_TABS: Array<{ key: ToolTab; label: string }> = [
  { key: 'summary', label: 'Summary' },
  { key: 'payload', label: 'Payload' },
  { key: 'result', label: 'Result' },
  { key: 'schema', label: 'Schema' },
  { key: 'timing', label: 'Timing' },
];

/** The sandbox's own measurement of one call, when the attempt recorded it. */
export interface ToolCallTelemetry {
  timingSource: 'exact' | 'message';
  durationMs: number | null;
}

// eslint-disable-next-line no-control-regex
const ANSI_PATTERN = /[\u001b\u009b][[()#;?]*(?:[0-9]{1,4}(?:;[0-9]{0,4})*)?[0-9A-ORZcf-nqry=><]/g;

/** Terminal colors are data, not instructions: drop the escapes, show the text. */
function stripAnsi(text: string): { text: string; stripped: boolean } {
  if (!ANSI_PATTERN.test(text)) return { text, stripped: false };
  return { text: text.replace(ANSI_PATTERN, ''), stripped: true };
}

function resultEmptyText(step: TrajectoryStep): string {
  if (!step.result) return '';
  if (step.result.text) return step.result.text;
  return step.result.eventIds.length ? 'Tool completed with no output.' : '';
}

function ResultState({ step }: { step: TrajectoryStep }) {
  if (step.result) {
    const { text, stripped } = stripAnsi(resultEmptyText(step));
    return (
      <>
        <TextBlock text={text} what="Result" maxHeight="max-h-40" />
        {stripped ? (
          <p className="text-[11px] text-gray-400 dark:text-gray-500">
            Terminal control sequences were removed; the text is what the tool printed.
          </p>
        ) : null}
      </>
    );
  }
  if (step.status === 'waiting') {
    return <EmptyNote>Waiting for result…</EmptyNote>;
  }
  return <EmptyNote>Result was not recorded.</EmptyNote>;
}

export function ToolStepDetails({
  step,
  tab,
  parent,
  telemetry,
  onSelectStep,
  onSelectTab,
}: {
  step: TrajectoryStep;
  tab: ToolTab;
  parent?: TrajectoryStep;
  telemetry?: ToolCallTelemetry | null;
  onSelectStep?: (key: string) => void;
  onSelectTab: (tab: ToolTab) => void;
}) {
  const idPrefix = `step-${step.key}`;
  const callEvent = step.sourceEvents[0];
  const resultEvents = useMemo(
    () => step.sourceEvents.filter((event) => event.event_type === 'tool_result'),
    [step.sourceEvents],
  );
  // A step whose records disagree names no single call, so the attempt's own
  // measurement cannot be attributed to it either. Reading it anyway is what
  // put the first duplicate's duration back under a status the projection had
  // already declined to claim.
  const exactTiming =
    !step.ambiguity && telemetry?.timingSource === 'exact' && telemetry.durationMs !== null;

  const timingRows = (
    <DetailRows>
      <DetailRow label="Started" tone={exactTiming || !step.timing.startedAt ? 'muted' : 'default'}>
        {exactTiming ? 'Not recorded' : recordedAt(step.timing.startedAt)}
      </DetailRow>
      <DetailRow
        label="Duration"
        tone={step.timing.durationMs === null && !exactTiming ? 'muted' : 'default'}
      >
        {exactTiming
          ? formatDuration(telemetry.durationMs)
          : step.timing.durationMs === null
            ? 'Not recorded'
            : formatDuration(step.timing.durationMs)}
      </DetailRow>
      {!exactTiming && step.timing.finishedAt ? (
        <DetailRow label="Finished">{recordedAt(step.timing.finishedAt)}</DetailRow>
      ) : null}
      <DetailRow
        label="Timing source"
        tone={exactTiming || step.timing.source ? 'default' : 'muted'}
      >
        {exactTiming
          ? 'Tool execution hooks'
          : step.timing.source === 'session_timestamps'
            ? 'Session timestamps'
            : 'Not recorded'}
      </DetailRow>
    </DetailRows>
  );

  if (tab === 'payload') {
    return (
      <TabPanel idPrefix={idPrefix} tab="payload">
        {step.payloadValue !== undefined ? (
          <ValueBlock value={step.payloadValue} what="Arguments" />
        ) : step.payloadPreview ? (
          <>
            <TextBlock text={step.payloadPreview} what="Preview" />
            <p className="text-[11px] text-gray-400 dark:text-gray-500">
              Only a one-line preview of the arguments was stored for this call; the complete
              argument object was not recorded.
            </p>
          </>
        ) : (
          <EmptyNote>Payload was not recorded.</EmptyNote>
        )}
        {step.truncated && callEvent ? <TruncationNote payload={callEvent.payload ?? {}} /> : null}
        {callEvent ? (
          <DetailSection title="Stored call event">
            <StoredRecord
              title={`#${callEvent.id} · ${callEvent.event_type} · seq ${callEvent.seq} · attempt ${callEvent.attempt_id}`}
              event={callEvent}
            />
          </DetailSection>
        ) : null}
        <button
          type="button"
          onClick={() =>
            void copyText(
              JSON.stringify(step.payloadValue ?? step.payloadPreview ?? null, null, 2) ?? '',
              'Arguments',
            )
          }
          className="rounded border border-gray-200 px-2 py-0.5 text-[11px] text-gray-500 hover:border-gray-300 hover:text-gray-900 dark:border-white/10 dark:text-gray-400 dark:hover:border-white/20 dark:hover:text-gray-100"
        >
          Copy arguments
        </button>
      </TabPanel>
    );
  }

  if (tab === 'result') {
    return (
      <TabPanel idPrefix={idPrefix} tab="result">
        {step.result ? (
          <>
            <TruncationNote payload={resultEvents[0]?.payload ?? {}} />
            <ResultState step={step} />
            <DetailRows>
              <DetailRow label="Error flag" tone={step.result.isError ? 'error' : 'default'}>
                {step.result.isError ? 'true' : 'false'}
              </DetailRow>
              {step.result.exitCode !== null ? (
                <DetailRow
                  label="Exit code"
                  tone={step.result.exitCode === 0 ? 'default' : 'error'}
                >
                  {step.result.exitCode}
                </DetailRow>
              ) : null}
            </DetailRows>
            {step.ambiguity === 'duplicate_result' ? (
              <p role="note" className="text-[11px] text-amber-700 dark:text-amber-300">
                More than one result was stored for this call. All of them are kept below, so the
                pairing is not verified and neither a status nor a duration is claimed.
              </p>
            ) : null}
            {resultEvents.map((event) => (
              <DetailSection key={event.id} title="Stored result event">
                <StoredRecord
                  title={`#${event.id} · ${event.event_type} · seq ${event.seq} · attempt ${event.attempt_id}`}
                  event={event}
                />
              </DetailSection>
            ))}
          </>
        ) : (
          <>
            <ResultState step={step} />
            {resultEvents.length === 0 ? (
              <p className="text-[11px] text-gray-400 dark:text-gray-500">
                {step.status === 'waiting'
                  ? 'This call was issued on an attempt that is still running.'
                  : 'The attempt that issued this call has ended without storing a result.'}
              </p>
            ) : null}
          </>
        )}
      </TabPanel>
    );
  }

  if (tab === 'schema') {
    return (
      <TabPanel idPrefix={idPrefix} tab="schema">
        <DetailRows>
          <DetailRow label="Tool">
            <span className="font-mono">{step.toolName ?? step.label}</span>
          </DetailRow>
        </DetailRows>
        <EmptyNote>Tool schema was not recorded for this call.</EmptyNote>
        <p className="text-[11px] text-gray-400 dark:text-gray-500">
          The definition the model received is not stored with the call, so it is not reconstructed
          from the arguments or from the tool that happens to carry this name today.
        </p>
      </TabPanel>
    );
  }

  if (tab === 'timing') {
    return (
      <TabPanel idPrefix={idPrefix} tab="timing">
        {timingRows}
        <p className="text-[11px] text-gray-400 dark:text-gray-500">
          {exactTiming
            ? 'The duration comes from the sandbox hooks that bracket the call. The exact start and finish boundaries are not exposed here.'
            : step.ambiguity
              ? 'The records for this call disagree, so no measurement is attributed to it: not the sandbox timing and not the interval between the stored records.'
              : step.timing.source === 'session_timestamps'
                ? 'This interval runs from the stored call record to the stored result record. It includes queueing and runtime or forwarding delay, so it is not pure tool execution time.'
                : 'No evidence for this call\u2019s execution window was recorded.'}
        </p>
      </TabPanel>
    );
  }

  const resultText = step.result ? stripAnsi(resultEmptyText(step)).text : '';

  return (
    <TabPanel idPrefix={idPrefix} tab="summary">
      <DetailRows>
        <DetailRow label="Hierarchy">
          {parent && onSelectStep ? (
            <button
              type="button"
              onClick={() => onSelectStep(parent.key)}
              className="text-left text-crimson hover:underline dark:text-crimson-light"
            >
              Assistant message ›
            </button>
          ) : (
            <span className="text-gray-400 dark:text-gray-500">Not recorded</span>
          )}
        </DetailRow>
        <DetailRow
          label="Status"
          tone={
            step.status === 'failed' ? 'error' : step.status === 'waiting' ? 'muted' : 'default'
          }
        >
          {STATUS_LABEL[step.status]}
        </DetailRow>
      </DetailRows>
      {step.ambiguity === 'duplicate_call' ? (
        <p role="note" className="text-[11px] text-amber-700 dark:text-amber-300">
          This call id was stored more than once in the same attempt. Every record is kept, and the
          pairing below is not verified.
        </p>
      ) : null}
      {step.ambiguity === 'duplicate_result' ? (
        <p role="note" className="text-[11px] text-amber-700 dark:text-amber-300">
          More than one result was stored for this call. Every record is kept, so neither the status
          nor the duration above is claimed.
        </p>
      ) : null}

      <DetailSection title="Payload" onOpen={() => onSelectTab('payload')}>
        {step.payloadValue !== undefined ? (
          <ValueBlock value={step.payloadValue} what="Arguments" maxHeight="max-h-40" />
        ) : step.payloadPreview ? (
          <TextBlock text={step.payloadPreview} what="Preview" maxHeight="max-h-40" />
        ) : (
          <EmptyNote>Payload was not recorded.</EmptyNote>
        )}
      </DetailSection>

      <DetailSection title="Result" onOpen={() => onSelectTab('result')}>
        {step.result ? (
          <>
            <TextBlock text={resultText} what="Result" maxHeight="max-h-40" />
            <TruncationNote payload={resultEvents[0]?.payload ?? {}} />
          </>
        ) : (
          <ResultState step={step} />
        )}
      </DetailSection>

      <DetailSection title="Schema" onOpen={() => onSelectTab('schema')}>
        <p className="text-[12px] text-gray-800 dark:text-gray-200">
          <span className="font-mono">{step.toolName ?? step.label}</span>
          <span className="text-gray-400 dark:text-gray-500"> · description not recorded</span>
        </p>
        <EmptyNote>Tool schema was not recorded for this call.</EmptyNote>
      </DetailSection>

      <DetailSection title="Timing" onOpen={() => onSelectTab('timing')}>
        {timingRows}
      </DetailSection>
    </TabPanel>
  );
}
