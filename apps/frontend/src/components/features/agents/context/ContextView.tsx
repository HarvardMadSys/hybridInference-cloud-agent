'use client';

// A conversation's Context view: how large the context the model was given
// was at every request, how it moved between turns, when the CLI compacted
// it, what entered, left or changed it, and what the stored text is estimated
// to weigh. Each request, turn, compaction and event opens its step in the
// trajectory, or its turn when the step has not been read into this page.
//
// It is the conversation's main Context tab, beside Chat and Trajectory, and
// what `/context` opens; both use the page's one read of the data (see
// `threadContextData`).

import { useState } from 'react';
import { useLocale } from '@/components/providers';
import type { AgentJob } from '../types';
import type { ThreadContextApi } from '@/lib/api/agents';
import { ContextEvents } from './ContextEvents';
import { ContextGrowthChart } from './ContextGrowthChart';
import { ContextDashboard } from './dashboard/ContextDashboard';
import {
  Capabilities,
  Compactions,
  ContextSummary,
  LargestChanges,
  PointDetail,
  Section,
  Turns,
  type OpenPoint,
  type OpenTurn,
} from './ContextSections';
import {
  ThreadContextProvider,
  useHasThreadContextStore,
  useThreadContextData,
} from './threadContextData';

export { CONTEXT_REFRESH_MS } from './threadContextData';

type ContextViewProps = {
  job: Pick<AgentJob, 'id' | 'threadId' | 'state' | 'conversationState'>;
  onOpenPoint: OpenPoint;
  onOpenTurn: OpenTurn;
};

export function ContextView(props: ContextViewProps) {
  // The page provides the shared data; a view on its own reads its own.
  if (useHasThreadContextStore()) return <ContextViewBody {...props} />;
  return (
    <ThreadContextProvider job={props.job}>
      <ContextViewBody {...props} />
    </ThreadContextProvider>
  );
}

function ContextViewBody({ job, onOpenPoint, onOpenTurn }: ContextViewProps) {
  const { t } = useLocale();
  const { key, view, missing, error, retry } = useThreadContextData(true);
  const [selection, setSelection] = useState<{ key: string; index: number } | null>(null);

  if (!view) return <ContextState missing={missing} error={error} retry={retry} />;
  const nowMs = Date.parse(view.generated_at);
  const points = view.requests;
  // A selection belongs to the conversation it was made in.
  const selected = selection?.key === key ? selection.index : null;
  const setSelected = (index: number) => setSelection({ key, index });
  const point =
    points.find((candidate) => candidate.index === selected) ?? points[points.length - 1] ?? null;
  return (
    <div className="mx-auto w-full max-w-6xl pb-8">
      <ContextDashboard
        jobId={job.id}
        conversationKey={key}
        view={view}
        nowMs={nowMs}
        selected={point?.index ?? null}
        onSelect={setSelected}
        onOpenPoint={onOpenPoint}
        onOpenTurn={onOpenTurn}
      />
      {point ? (
        <Section title={t('dashboard.selected')}>
          <PointDetail point={point} onOpenPoint={onOpenPoint} onOpenTurn={onOpenTurn} />
        </Section>
      ) : null}
      <ContextEvents key={key} view={view} nowMs={nowMs} onOpenPoint={onOpenPoint} />
      <Compactions compactions={view.compactions} nowMs={nowMs} onOpenPoint={onOpenPoint} />
      <LargestChanges changes={view.largest_changes} points={points} onSelect={setSelected} />
      <ReferenceSections
        view={view}
        nowMs={nowMs}
        selected={point?.index ?? null}
        onSelect={setSelected}
        onOpenTurn={onOpenTurn}
      />
    </div>
  );
}

/** The reference sections below the dashboard: totals, the raw chart, turns, capabilities. */
function ReferenceSections({
  view,
  nowMs,
  selected,
  onSelect,
  onOpenTurn,
}: {
  view: ThreadContextApi;
  nowMs: number;
  selected: number | null;
  onSelect: (index: number) => void;
  onOpenTurn: OpenTurn;
}) {
  const { t } = useLocale();
  return (
    <>
      <ContextSummary view={view} nowMs={nowMs} />
      {view.requests.length > 0 ? (
        <Section title={t('dashboard.raw')}>
          <ContextGrowthChart
            points={view.requests}
            turns={view.turns}
            selected={selected}
            onSelect={onSelect}
          />
        </Section>
      ) : null}
      <Turns turns={view.turns} onOpenTurn={onOpenTurn} />
      <Capabilities view={view} />
    </>
  );
}

/** Not drawn yet: still loading, absent, or failed with a way to retry. */
function ContextState({
  missing,
  error,
  retry,
}: {
  missing: boolean;
  error: string | null;
  retry: () => void;
}) {
  const { t } = useLocale();
  if (error) {
    return (
      <div role="alert" className="ui-card p-4 text-sm">
        <p className="text-foreground">{t('context.loadError')}</p>
        <p className="mt-1 text-xs text-muted">{error}</p>
        <button
          type="button"
          onClick={retry}
          className="ui-button ui-button-secondary mt-3 min-h-11"
        >
          {t('context.retry')}
        </button>
      </div>
    );
  }
  if (missing) return <p className="p-4 text-sm text-muted">{t('context.notFound')}</p>;
  return (
    <p role="status" className="p-4 text-sm text-muted">
      {t('context.loading')}
    </p>
  );
}
