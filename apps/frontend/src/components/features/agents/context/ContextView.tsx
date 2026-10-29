'use client';

// A conversation's Context view: how large the context the model was given
// was at every request, how it moved between turns, when the CLI compacted
// it, and what the stored text is estimated to weigh. Each request, turn and
// compaction opens its step in the trajectory, or its turn when the step has
// not been read into this page.

import { useCallback, useEffect, useState } from 'react';
import { useLocale } from '@/components/providers';
import { getAgentThreadContext, type ThreadContextApi } from '@/lib/api/agents';
import { isActiveConversationState, isActiveJobState } from '../conversationJob';
import type { AgentJob } from '../types';
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

/** How often a running conversation's view is read again. */
export const CONTEXT_REFRESH_MS = 5_000;

export function ContextView({
  job,
  onOpenPoint,
  onOpenTurn,
}: {
  job: AgentJob;
  onOpenPoint: OpenPoint;
  onOpenTurn: OpenTurn;
}) {
  const { t } = useLocale();
  const [view, setView] = useState<ThreadContextApi | null>(null);
  const [missing, setMissing] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [attempt, setAttempt] = useState(0);
  const [selected, setSelected] = useState<number | null>(null);
  const active = isActiveJobState(job.state) || isActiveConversationState(job.conversationState);

  const load = useCallback(
    async (signal: AbortSignal) => {
      try {
        const next = await getAgentThreadContext(job.id, signal);
        if (signal.aborted) return;
        setView(next);
        setMissing(next === null);
        setError(null);
      } catch (cause) {
        if (signal.aborted) return;
        setError(cause instanceof Error && cause.message ? cause.message : t('context.loadError'));
      }
    },
    [job.id, t],
  );

  useEffect(() => {
    const controller = new AbortController();
    void load(controller.signal);
    return () => controller.abort();
  }, [load, attempt, job.state, job.conversationState]);

  // A running turn adds requests; read again while it runs, quietly.
  const running = active || Boolean(view?.latest?.turn_active);
  useEffect(() => {
    if (!running) return undefined;
    const controller = new AbortController();
    const timer = window.setInterval(() => {
      if (document.visibilityState === 'visible') void load(controller.signal);
    }, CONTEXT_REFRESH_MS);
    return () => {
      window.clearInterval(timer);
      controller.abort();
    };
  }, [running, load]);

  if (error && !view) {
    return (
      <div role="alert" className="ui-card p-4 text-sm">
        <p className="text-foreground">{t('context.loadError')}</p>
        <p className="mt-1 text-xs text-muted">{error}</p>
        <button
          type="button"
          onClick={() => setAttempt((value) => value + 1)}
          className="ui-button ui-button-secondary mt-3 min-h-11"
        >
          {t('context.retry')}
        </button>
      </div>
    );
  }
  if (missing) return <p className="p-4 text-sm text-muted">{t('context.notFound')}</p>;
  if (!view) {
    return (
      <p role="status" className="p-4 text-sm text-muted">
        {t('context.loading')}
      </p>
    );
  }
  const nowMs = Date.parse(view.generated_at);
  const points = view.requests;
  const point =
    points.find((candidate) => candidate.index === selected) ?? points[points.length - 1] ?? null;
  return (
    <div className="mx-auto w-full max-w-5xl pb-8">
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
      <Compactions compactions={view.compactions} nowMs={nowMs} onOpenPoint={onOpenPoint} />
      <LargestChanges changes={view.largest_changes} points={points} onSelect={setSelected} />
      <Turns turns={view.turns} onOpenTurn={onOpenTurn} />
      <Capabilities view={view} />
    </div>
  );
}
