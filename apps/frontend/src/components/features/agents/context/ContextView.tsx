'use client';

// A conversation's Context view, top to bottom: a line of counts, the
// conversation's tokens and time, the selected request's window and the trend
// of every request beside the browser of what it carried, then the context
// events (compactions among them). One selection links the trend, the
// browser and the events; each request, compaction and event opens its step
// in the trajectory, or its turn when the step has not been read into this page.
//
// It is the conversation's main Context tab, beside Chat and Trajectory, and
// what `/context` opens; both use the page's one read of the data (see
// `threadContextData`).

import { useRef, useState } from 'react';
import { useLocale } from '@/components/providers';
import type { AgentJob } from '../types';
import { ContextEvents } from './ContextEvents';
import { ContextOverview } from './ContextOverview';
import { ContextDashboard } from './dashboard/ContextDashboard';
import type { OpenPoint, OpenTurn } from './ContextSections';
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
  const { key, view, missing, error, retry } = useThreadContextData(true);
  const [selection, setSelection] = useState<{ key: string; index: number } | null>(null);
  const trendRef = useRef<HTMLDivElement>(null);

  if (!view) return <ContextState missing={missing} error={error} retry={retry} />;
  const nowMs = Date.parse(view.generated_at);
  const points = view.requests;
  // A selection belongs to the conversation it was made in.
  const selected = selection?.key === key ? selection.index : null;
  const setSelected = (index: number) => setSelection({ key, index });
  const point =
    points.find((candidate) => candidate.index === selected) ?? points[points.length - 1] ?? null;
  // An event picked far below the trend brings the trend into view.
  const showInTrend = (index: number) => {
    setSelected(index);
    trendRef.current?.scrollIntoView?.({ block: 'nearest', behavior: 'smooth' });
  };
  return (
    <div className="mx-auto grid w-full max-w-6xl gap-3 pb-8">
      <ContextOverview view={view} />
      <ContextDashboard
        jobId={job.id}
        conversationKey={key}
        view={view}
        nowMs={nowMs}
        selected={point?.index ?? null}
        onSelect={setSelected}
        onOpenPoint={onOpenPoint}
        onOpenTurn={onOpenTurn}
        trendRef={trendRef}
      />
      <ContextEvents
        key={key}
        view={view}
        nowMs={nowMs}
        onOpenPoint={onOpenPoint}
        onSelect={showInTrend}
      />
    </div>
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
