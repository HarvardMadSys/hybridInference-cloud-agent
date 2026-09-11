'use client';

// The trajectory: every stored event of the conversation, projected into the
// steps a reader can inspect.
//
// The chat tab beside this is a reading of the stream. This is the stream —
// unfolded, undeduplicated, including the rows no view knows how to render.
// Selecting a row no longer opens an inline panel: the detail goes to the
// right-hand inspector, so the list keeps its place while a step is read.

import { useEffect, useMemo, useRef } from 'react';

import type { AgentJobEventApi, AgentThreadEventApi } from '@/lib/api/agents';
import { filterSteps, type TrajectoryStep } from './trajectorySteps';
import { toSessionLog, type TrajectoryLane } from './trajectory';
import { formatDuration } from './TurnFooter';

const LANES: { key: TrajectoryLane; label: string; tone: string }[] = [
  { key: 'input', label: 'Input', tone: 'bg-sky-400' },
  { key: 'model', label: 'Model', tone: 'bg-violet-400' },
  { key: 'tools', label: 'Tools', tone: 'bg-amber-400' },
  { key: 'other', label: 'Other', tone: 'bg-gray-300 dark:bg-gray-600' },
];

const ACTOR_TONE: Record<TrajectoryStep['actor'], string> = {
  user: 'bg-sky-50 dark:bg-sky-500/10 text-sky-700 dark:text-sky-400',
  assistant: 'bg-violet-50 dark:bg-violet-500/10 text-violet-700 dark:text-violet-400',
  tool: 'bg-amber-50 dark:bg-amber-500/10 text-amber-700 dark:text-amber-400',
  context: 'bg-gray-100 dark:bg-white/5 text-gray-600 dark:text-gray-400',
  system: 'bg-gray-100 dark:bg-white/5 text-gray-600 dark:text-gray-400',
};

const STATUS_TONE: Partial<Record<TrajectoryStep['status'], string>> = {
  failed: 'text-rose-600 dark:text-rose-400',
  waiting: 'text-amber-600 dark:text-amber-400',
  not_recorded: 'text-gray-400 dark:text-gray-500',
};

/** A strip of one mark per step, so a long turn has a shape you can scan. */
function Minimap({
  steps,
  selectedKey,
  onSelect,
}: {
  steps: TrajectoryStep[];
  selectedKey: string | null;
  onSelect: (key: string) => void;
}) {
  const lanes = LANES.map((lane) => ({
    ...lane,
    count: steps.filter((step) => step.lane === lane.key).length,
  })).filter((lane) => lane.count > 0);

  return (
    <div className="flex gap-4">
      <div className="w-24 shrink-0 space-y-1.5">
        {lanes.map((lane) => (
          <div key={lane.key} className="flex h-5 items-center gap-2">
            <span aria-hidden="true" className={`h-1.5 w-1.5 rounded-full ${lane.tone}`} />
            <span className="text-[11px] font-medium text-gray-600 dark:text-gray-300">
              {lane.label}
            </span>
            <span className="ml-auto text-[10px] tabular-nums text-gray-400 dark:text-gray-500">
              {lane.count}
            </span>
          </div>
        ))}
      </div>
      <div className="min-w-0 flex-1 overflow-x-auto pb-1">
        <div className="space-y-1.5" style={{ minWidth: steps.length * 12 + 8 }}>
          {lanes.map((lane) => (
            <div
              key={lane.key}
              className="grid h-5 items-center gap-0.5 rounded-md bg-gray-50 px-1 dark:bg-white/[0.03]"
              style={{ gridTemplateColumns: `repeat(${steps.length}, minmax(0, 1fr))` }}
            >
              {steps.map((step) =>
                step.lane === lane.key ? (
                  <button
                    key={step.key}
                    type="button"
                    aria-label={`${step.label} (${step.kind}, step ${step.stepNo})`}
                    aria-pressed={selectedKey === step.key}
                    title={`${step.label} (${step.kind})${step.timing.durationMs !== null ? ` · ${formatDuration(step.timing.durationMs)}` : ''}`}
                    onClick={() => onSelect(step.key)}
                    className={`mx-auto h-3.5 w-full max-w-4 rounded-[3px] transition-opacity hover:opacity-100 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-gray-700 focus-visible:ring-offset-2 dark:focus-visible:ring-gray-200 dark:focus-visible:ring-offset-gray-900 ${
                      step.status === 'failed' ? 'bg-rose-400' : lane.tone
                    } ${selectedKey === step.key ? 'opacity-100 ring-1 ring-gray-700 ring-offset-1 dark:ring-gray-200 dark:ring-offset-gray-900' : selectedKey === null ? 'opacity-80' : 'opacity-60'}`}
                  />
                ) : (
                  <span key={step.key} aria-hidden="true" />
                ),
              )}
            </div>
          ))}
        </div>
      </div>
    </div>
  );
}

export function TrajectoryView({
  events,
  steps,
  query,
  onQueryChange,
  selectedKey,
  onSelectStep,
  onOpenRun,
  focusJobId,
}: {
  events: (AgentJobEventApi | AgentThreadEventApi)[];
  steps: TrajectoryStep[];
  query: string;
  onQueryChange: (value: string) => void;
  selectedKey: string | null;
  onSelectStep: (key: string) => void;
  onOpenRun: (jobId: string, turnNo: number) => void;
  /** A turn opened from Chat: bring its header into view without selecting it. */
  focusJobId?: string | null;
}) {
  const visible = useMemo(() => filterSteps(steps, query), [steps, query]);

  useEffect(() => {
    if (!focusJobId) return;
    const node = document.querySelector<HTMLElement>(`[data-turn-job="${focusJobId}"]`);
    node?.scrollIntoView?.({ block: 'nearest' });
  }, [focusJobId]);

  // A selection can come from the minimap, from a hierarchy link, or from the
  // chat's "View execution"; in every case the row must end up on screen once.
  // The filter is a dependency only so a row the search was hiding is reached
  // when it comes back: a step arriving over SSE must not drag a reader who has
  // scrolled somewhere else back to the selection.
  const scrolledKey = useRef<string | null>(null);
  useEffect(() => {
    if (!selectedKey) {
      scrolledKey.current = null;
      return;
    }
    if (scrolledKey.current === selectedKey) return;
    const escaped =
      typeof CSS !== 'undefined' && typeof CSS.escape === 'function'
        ? CSS.escape(selectedKey)
        : selectedKey.replace(/["\\]/g, '\\$&');
    const node = document.querySelector<HTMLElement>(`[data-step-key="${escaped}"]`);
    // Not in the list right now: a search is hiding it. Leave the record unset
    // so the row is still brought into view if the filter stops hiding it.
    if (!node) return;
    node.scrollIntoView?.({ block: 'nearest' });
    scrolledKey.current = selectedKey;
  }, [selectedKey, visible]);

  function download() {
    const blob = new Blob([toSessionLog(events)], { type: 'application/x-ndjson' });
    const url = URL.createObjectURL(blob);
    const link = document.createElement('a');
    link.href = url;
    link.download = 'session-log.ndjson';
    link.click();
    URL.revokeObjectURL(url);
  }

  if (steps.length === 0) {
    return (
      <p className="py-8 text-center text-[13px] text-gray-500 dark:text-gray-400">
        This conversation stored no events. A thread that ran before event capture, or one whose
        first turn never started, has nothing to show here.
      </p>
    );
  }

  return (
    <section aria-label="Trajectory" className="space-y-3">
      <div className="rounded-xl border border-gray-200 bg-white shadow-[0_1px_2px_rgba(17,24,39,0.025)] dark:border-white/10 dark:bg-gray-900">
        <div className="flex items-center justify-between gap-3 border-b border-gray-100 px-4 py-3 dark:border-white/5">
          <h2 className="flex items-center gap-2 text-[12px] font-medium text-gray-700 dark:text-gray-200">
            <svg
              aria-hidden="true"
              viewBox="0 0 16 16"
              fill="none"
              stroke="currentColor"
              strokeWidth="1.25"
              strokeLinecap="round"
              className="h-3.5 w-3.5 text-gray-400 dark:text-gray-500"
            >
              <path d="M2.5 2.5v11h11M5.5 5h3M9.5 8h3M5.5 11h2" />
            </svg>
            Execution overview
          </h2>
          <span className="rounded-md bg-gray-50 px-2 py-0.5 text-[10px] tabular-nums text-gray-500 dark:bg-white/5 dark:text-gray-400">
            {steps.length} {steps.length === 1 ? 'step' : 'steps'} · {events.length}{' '}
            {events.length === 1 ? 'event' : 'events'}
          </span>
        </div>
        <div className="px-4 pb-3 pt-3.5">
          <Minimap steps={steps} selectedKey={selectedKey} onSelect={onSelectStep} />
        </div>
      </div>

      <div className="flex items-center gap-2">
        <input
          type="search"
          value={query}
          onChange={(event) => onQueryChange(event.target.value)}
          placeholder="Search steps"
          aria-label="Search steps"
          className="min-w-0 flex-1 rounded-lg border border-gray-200 dark:border-white/10 px-2.5 py-1.5 text-[13px] outline-none focus:border-gray-300"
        />
        {visible.length !== steps.length ? (
          <span className="shrink-0 text-[12px] tabular-nums text-gray-500 dark:text-gray-400">
            {visible.length} of {steps.length}
          </span>
        ) : null}
        <button
          type="button"
          onClick={download}
          className="shrink-0 rounded-lg border border-gray-200 dark:border-white/10 px-2.5 py-1.5 text-[12px] text-gray-600 dark:text-gray-400 transition-colors hover:border-gray-300 dark:hover:border-white/20 hover:text-gray-900 dark:hover:text-gray-100"
        >
          Session log
        </button>
      </div>

      <div className="overflow-hidden rounded-lg border border-gray-200 dark:border-white/10">
        {visible.map((step, index) => {
          const open = selectedKey === step.key;
          const startsTurn = index === 0 || visible[index - 1].turnNo !== step.turnNo;
          const labelTone = STATUS_TONE[step.status] ?? 'text-gray-800 dark:text-gray-200';
          return (
            <div
              key={step.key}
              className="border-b border-gray-100 last:border-b-0 dark:border-white/5"
            >
              {startsTurn && steps.some((other) => other.turnNo !== step.turnNo) ? (
                <div
                  data-turn-job={step.jobId}
                  className="flex items-center gap-2 bg-gray-50 px-3 py-1 dark:bg-gray-950"
                >
                  <span className="text-[11px] font-medium uppercase tracking-wide text-gray-400 dark:text-gray-500">
                    Turn {step.turnNo}
                  </span>
                  {step.jobId ? (
                    <button
                      type="button"
                      onClick={() => onOpenRun(step.jobId, step.turnNo)}
                      className="ml-auto rounded px-1.5 py-0.5 text-[11px] text-gray-500 transition-colors hover:bg-white hover:text-gray-900 dark:text-gray-400 dark:hover:bg-white/10 dark:hover:text-gray-100"
                    >
                      Run details ›
                    </button>
                  ) : null}
                </div>
              ) : null}
              <button
                type="button"
                data-step-key={step.key}
                onClick={() => onSelectStep(step.key)}
                aria-current={open ? 'true' : undefined}
                className={`flex w-full items-baseline gap-2 px-3 py-1.5 text-left text-[12px] transition-colors hover:bg-gray-50 dark:hover:bg-white/5 ${
                  open ? 'bg-gray-50 dark:bg-gray-950' : ''
                }`}
              >
                <span
                  className={`w-16 shrink-0 rounded px-1.5 py-0.5 text-center text-[10px] font-medium uppercase tracking-wide ${ACTOR_TONE[step.actor]}`}
                >
                  {step.actor}
                </span>
                <span className={`w-28 shrink-0 truncate font-medium ${labelTone}`}>
                  {step.label}
                </span>
                <span className="min-w-0 flex-1 truncate font-mono text-gray-500 dark:text-gray-400">
                  {step.detail}
                </span>
                {step.status === 'failed' ? (
                  <span className="shrink-0 text-[11px] text-rose-600 dark:text-rose-400">
                    Failed
                  </span>
                ) : step.status === 'waiting' ? (
                  <span className="shrink-0 text-[11px] text-amber-600 dark:text-amber-400">
                    Waiting
                  </span>
                ) : null}
                {step.timing.durationMs !== null ? (
                  <span className="shrink-0 tabular-nums text-gray-400 dark:text-gray-500">
                    {formatDuration(step.timing.durationMs)}
                  </span>
                ) : null}
              </button>
            </div>
          );
        })}
        {visible.length === 0 ? (
          <p className="px-3 py-6 text-center text-[12px] text-gray-500 dark:text-gray-400">
            No step matches “{query}”.
          </p>
        ) : null}
      </div>
    </section>
  );
}
