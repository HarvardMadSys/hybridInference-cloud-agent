'use client';

// A conversation's Context view: how large the context the model was given
// was at every request, how it moved between turns, when the CLI compacted
// it, what entered, left or changed it, and what the stored text is estimated
// to weigh. Each request, turn, compaction and event opens its step in the
// trajectory, or its turn when the step has not been read into this page.
//
// The same view is the conversation's Context tab, the workspace panel's
// Context tab and what `/context` opens; they share one read of the data
// (see `threadContextData`). The panel is narrower, so it leaves the turn
// table and the capability notes to the full tab.

import { useState } from 'react';
import { useLocale } from '@/components/providers';
import type { AgentJob } from '../types';
import { ContextEvents } from './ContextEvents';
import { ContextGrowthChart } from './ContextGrowthChart';
import { RequestComposition } from './RequestComposition';
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
  /** `panel`: the narrow workspace panel, with a way to the full tab. */
  variant?: 'full' | 'panel';
  /** Whether it is showing; a hidden view does not keep the data fresh. */
  visible?: boolean;
  /** Open the conversation's full Context tab (panel only). */
  onOpenFull?: () => void;
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

function ContextViewBody({
  job,
  onOpenPoint,
  onOpenTurn,
  variant = 'full',
  visible = true,
  onOpenFull,
}: ContextViewProps) {
  const { t } = useLocale();
  const { key, view, missing, error, retry } = useThreadContextData(visible);
  const [selection, setSelection] = useState<{ key: string; index: number } | null>(null);
  const panel = variant === 'panel';

  if (!view) return <ContextState missing={missing} error={error} retry={retry} />;
  const nowMs = Date.parse(view.generated_at);
  const points = view.requests;
  // A selection belongs to the conversation it was made in.
  const selected = selection?.key === key ? selection.index : null;
  const setSelected = (index: number) => setSelection({ key, index });
  const point =
    points.find((candidate) => candidate.index === selected) ?? points[points.length - 1] ?? null;
  return (
    <div className={panel ? 'w-full pb-6' : 'mx-auto w-full max-w-5xl pb-8'}>
      {panel && onOpenFull ? <OpenFull onOpenFull={onOpenFull} /> : null}
      <ContextSummary view={view} nowMs={nowMs} />
      {points.length > 0 ? (
        <Section title={t('context.growth')}>
          <ContextGrowthChart
            points={points}
            turns={view.turns}
            selected={point?.index ?? null}
            onSelect={setSelected}
          />
          {point ? (
            <PointDetail point={point} onOpenPoint={onOpenPoint} onOpenTurn={onOpenTurn} />
          ) : null}
          {point ? (
            <RequestComposition
              jobId={job.id}
              point={point}
              onOpenPoint={onOpenPoint}
              onOpenTurn={onOpenTurn}
            />
          ) : null}
        </Section>
      ) : (
        <p className="mt-6 rounded-lg border border-border px-3 py-2 text-sm text-muted">
          {t('context.empty')}
        </p>
      )}
      <ContextEvents key={key} view={view} nowMs={nowMs} onOpenPoint={onOpenPoint} />
      <Compactions compactions={view.compactions} nowMs={nowMs} onOpenPoint={onOpenPoint} />
      <LargestChanges changes={view.largest_changes} points={points} onSelect={setSelected} />
      {panel ? null : <Turns turns={view.turns} onOpenTurn={onOpenTurn} />}
      {panel ? null : <Capabilities view={view} />}
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

function OpenFull({ onOpenFull }: { onOpenFull: () => void }) {
  const { t } = useLocale();
  return (
    <div className="mb-2 flex justify-end">
      <button
        type="button"
        onClick={onOpenFull}
        className="ui-button ui-button-ghost min-h-9 px-2 py-1 text-xs [@media(pointer:coarse)]:min-h-11"
      >
        {t('context.panel.openFull')}
      </button>
    </div>
  );
}
