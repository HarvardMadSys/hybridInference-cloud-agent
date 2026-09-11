'use client';

// The right-hand inspector: one assistant message, one tool call, or one turn.
//
// The panel is the only place these details appear. The list stays on the left
// and keeps its own scroll position, so reading a step never means losing the
// place in the turn the reader was scanning.

import { useEffect, useMemo, useState } from 'react';

import { getAgentAttemptTelemetry, type AgentJobApi } from '@/lib/api/agents';

import { AssistantStepDetails, ASSISTANT_TABS, type AssistantTab } from './AssistantStepDetails';
import {
  DetailRow,
  DetailRows,
  EmptyNote,
  StoredRecord,
  TabPanel,
  TabStrip,
  TruncationNote,
  ValueBlock,
  recordedAt,
} from './StepDetailParts';
import { RunDetails, RUN_TABS, type RunTab } from './RunDetails';
import {
  ToolStepDetails,
  TOOL_TABS,
  type ToolCallTelemetry,
  type ToolTab,
} from './ToolStepDetails';
import { STATUS_LABEL, type StoredEvent, type TrajectoryStep } from './trajectorySteps';
import type { AgentJob } from './types';

export type EventTab = 'summary' | 'raw';
export type StepTab = AssistantTab | ToolTab | EventTab;

export type { RunTab };

export type InspectorSelection =
  | { kind: 'step'; stepKey: string; tab: StepTab }
  /** `turnNo` comes from whatever opened the panel, so a turn stays itself
   *  even when the conversation's rows have not been read yet. */
  | { kind: 'run'; jobId: string; turnNo: number; tab: RunTab };

/** The tabs a step of this kind actually has. */
export function tabsForKind(kind: TrajectoryStep['kind']): Array<{ key: StepTab; label: string }> {
  if (kind === 'assistant') return ASSISTANT_TABS;
  if (kind === 'tool') return TOOL_TABS;
  return [
    { key: 'summary', label: 'Summary' },
    { key: 'raw', label: 'Raw' },
  ];
}

/** Whether a tab belongs to the newly selected step, or must fall back. */
export function normalizeStepTab(kind: TrajectoryStep['kind'], tab: StepTab): StepTab {
  return tabsForKind(kind).some((item) => item.key === tab) ? tab : 'summary';
}

/**
 * The sandbox's own measurement of the selected call.
 *
 * It is an enrichment: when the attempt has no telemetry, or the request
 * fails, the Timing tab falls back to the stored event times rather than
 * blocking the panel. A call id that the attempt measured more than once names
 * no single measurement, so none is reported: picking one of them would
 * attribute a duration to a call the reader cannot tell apart.
 */
function useToolCallTelemetry(
  jobId: string,
  attemptId: number,
  callId: string,
  enabled: boolean,
): ToolCallTelemetry | null {
  const key = enabled && callId ? `${jobId}:${attemptId}:${callId}` : null;
  const [loaded, setLoaded] = useState<{ key: string; value: ToolCallTelemetry | null } | null>(
    null,
  );

  useEffect(() => {
    if (key === null) return undefined;
    const controller = new AbortController();
    getAgentAttemptTelemetry(jobId, attemptId, controller.signal)
      .then((data) => {
        if (controller.signal.aborted || data.attempt_id !== attemptId) return;
        const matches = (data.resources?.tool_calls ?? []).filter(
          (call) => call.tool_use_id === callId,
        );
        const match = matches.length === 1 ? matches[0] : undefined;
        if (!match) {
          setLoaded({ key, value: null });
          return;
        }
        setLoaded({
          key,
          value: {
            timingSource: match.timing_source === 'exact' ? 'exact' : 'message',
            durationMs: match.duration_ms,
          },
        });
      })
      .catch(() => {
        if (!controller.signal.aborted) setLoaded({ key, value: null });
      });
    return () => controller.abort();
  }, [attemptId, callId, jobId, key]);

  return loaded?.key === key ? loaded.value : null;
}

function EventStepDetails({
  step,
  tab,
  idPrefix,
}: {
  step: TrajectoryStep;
  tab: EventTab;
  idPrefix: string;
}) {
  if (tab === 'raw') {
    return (
      <TabPanel idPrefix={idPrefix} tab="raw">
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
      </TabPanel>
    );
  }
  const payload = step.sourceEvents[0]?.payload ?? {};
  return (
    <TabPanel idPrefix={idPrefix} tab="summary">
      <DetailRows>
        <DetailRow label="Event type">
          <span className="font-mono">{step.sourceEvents[0]?.event_type ?? step.label}</span>
        </DetailRow>
        <DetailRow label="Status" tone={step.status === 'failed' ? 'error' : 'muted'}>
          {STATUS_LABEL[step.status]}
        </DetailRow>
        <DetailRow label="Recorded at">{recordedAt(step.at)}</DetailRow>
        <DetailRow label="Seq">{step.sourceEvents[0]?.seq ?? 'Not recorded'}</DetailRow>
        <DetailRow label="Attempt">{step.attemptId}</DetailRow>
      </DetailRows>
      <TruncationNote payload={payload} />
      <ValueBlock value={payload} what="Payload" />
    </TabPanel>
  );
}

export function TrajectoryInspector({
  selection,
  steps,
  events,
  jobs,
  currentJob,
  query,
  onClearQuery,
  onSelectTab,
  onSelectStep,
  onClose,
  onOpenGitDiff,
}: {
  selection: InspectorSelection;
  steps: TrajectoryStep[];
  events: StoredEvent[];
  jobs: AgentJobApi[];
  currentJob: AgentJob;
  /** The live search, so the panel can say when it hides the selected step. */
  query: string;
  onClearQuery: () => void;
  onSelectTab: (tab: StepTab | RunTab) => void;
  onSelectStep: (key: string) => void;
  onClose: () => void;
  onOpenGitDiff: () => void;
}) {
  const byKey = useMemo(() => new Map(steps.map((step) => [step.key, step])), [steps]);
  const step = selection.kind === 'step' ? byKey.get(selection.stepKey) : undefined;
  const toolStep = step?.kind === 'tool' ? step : undefined;
  const telemetry = useToolCallTelemetry(
    toolStep?.jobId ?? '',
    toolStep?.attemptId ?? 0,
    toolStep?.callId ?? '',
    Boolean(toolStep && toolStep.status !== 'not_recorded'),
  );

  if (selection.kind === 'run') {
    const threadJob = jobs.find((item) => item.id === selection.jobId);
    const turnNo = selection.turnNo || threadJob?.turn_no || currentJob.turnNo || 1;
    return (
      <div className="flex min-h-0 flex-1 flex-col">
        <header className="flex items-start gap-2 border-b border-gray-200 px-4 py-3 dark:border-white/10">
          <div className="min-w-0 flex-1">
            <p className="text-[10px] font-medium uppercase tracking-wide text-gray-400 dark:text-gray-500">
              Run · Turn {turnNo}
            </p>
            <h2 className="truncate text-[13px] font-semibold text-gray-900 dark:text-gray-50">
              Run details
            </h2>
          </div>
          <CloseButton onClose={onClose} />
        </header>
        <TabStrip
          idPrefix={`run-${selection.jobId}`}
          tabs={RUN_TABS}
          active={selection.tab}
          onSelect={(tab) => onSelectTab(tab)}
        />
        <RunDetails
          jobId={selection.jobId}
          turnNo={turnNo}
          tab={selection.tab}
          job={threadJob}
          fallback={threadJob ? null : currentJob}
          events={events}
          onOpenGitDiff={onOpenGitDiff}
        />
      </div>
    );
  }

  if (!step) {
    return (
      <div className="flex min-h-0 flex-1 flex-col">
        <header className="flex items-start gap-2 border-b border-gray-200 px-4 py-3 dark:border-white/10">
          <h2 className="min-w-0 flex-1 text-[13px] font-semibold text-gray-900 dark:text-gray-50">
            Step details
          </h2>
          <CloseButton onClose={onClose} />
        </header>
        <div className="p-4">
          <EmptyNote>This step is no longer in the stored trajectory.</EmptyNote>
        </div>
      </div>
    );
  }

  const idPrefix = `step-${step.key}`;
  const tools = (step.toolKeys ?? [])
    .map((key) => byKey.get(key))
    .filter((item): item is TrajectoryStep => Boolean(item));
  const parent = step.parentKey ? byKey.get(step.parentKey) : undefined;
  const kindLabel =
    step.kind === 'assistant' ? 'Assistant' : step.kind === 'tool' ? 'Tool' : 'Event';
  const hiddenByQuery = !`${step.label} ${step.detail} ${step.kind}`
    .toLowerCase()
    .includes(query.trim().toLowerCase());

  return (
    <div className="flex min-h-0 flex-1 flex-col">
      <header className="flex items-start gap-2 border-b border-gray-200 px-4 py-3 dark:border-white/10">
        <div className="min-w-0 flex-1">
          <p className="text-[10px] font-medium uppercase tracking-wide text-gray-400 dark:text-gray-500">
            {kindLabel} · Turn {step.turnNo} · Step {step.stepNo}
          </p>
          <h2 className="truncate text-[13px] font-semibold text-gray-900 dark:text-gray-50">
            {step.label}
          </h2>
        </div>
        <CloseButton onClose={onClose} />
      </header>
      {hiddenByQuery ? (
        <p
          role="note"
          className="border-b border-amber-200 bg-amber-50 px-4 py-2 text-[11px] text-amber-800 dark:border-amber-500/25 dark:bg-amber-500/10 dark:text-amber-300"
        >
          The current search hides this step in the list.{' '}
          <button type="button" onClick={onClearQuery} className="font-medium underline">
            Clear search
          </button>
        </p>
      ) : null}
      <TabStrip
        idPrefix={idPrefix}
        tabs={tabsForKind(step.kind)}
        active={normalizeStepTab(step.kind, selection.tab)}
        onSelect={(tab) => onSelectTab(tab)}
      />
      {step.kind === 'assistant' ? (
        <AssistantStepDetails
          step={step}
          tab={normalizeStepTab(step.kind, selection.tab) as AssistantTab}
          tools={tools}
          onSelectStep={onSelectStep}
          onSelectTab={(tab) => onSelectTab(tab)}
        />
      ) : step.kind === 'tool' ? (
        <ToolStepDetails
          step={step}
          tab={normalizeStepTab(step.kind, selection.tab) as ToolTab}
          parent={parent}
          telemetry={telemetry}
          onSelectStep={onSelectStep}
          onSelectTab={(tab) => onSelectTab(tab)}
        />
      ) : (
        <EventStepDetails
          step={step}
          tab={normalizeStepTab(step.kind, selection.tab) as EventTab}
          idPrefix={idPrefix}
        />
      )}
    </div>
  );
}

function CloseButton({ onClose }: { onClose: () => void }) {
  return (
    <button
      type="button"
      onClick={onClose}
      aria-label="Close details"
      title="Close details (Esc)"
      className="inline-flex h-7 w-7 shrink-0 items-center justify-center rounded-md text-gray-400 transition-colors hover:bg-gray-100 hover:text-gray-900 dark:text-gray-500 dark:hover:bg-white/10 dark:hover:text-gray-100"
    >
      <svg
        aria-hidden="true"
        className="h-4 w-4"
        fill="none"
        viewBox="0 0 24 24"
        stroke="currentColor"
        strokeWidth={1.8}
      >
        <path strokeLinecap="round" strokeLinejoin="round" d="M6 6l12 12M18 6L6 18" />
      </svg>
    </button>
  );
}
