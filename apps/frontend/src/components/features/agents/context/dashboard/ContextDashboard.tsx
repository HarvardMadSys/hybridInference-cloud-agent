'use client';

// The Context dashboard: Token Stats and Timing Stats on top, the selected
// request's window card and the trend (with the selected request's strip) on
// the left, the browser on the right.
// The arrangement follows bowenliang123/dsh-context's Context tab (Apache-2.0,
// commit 32982aa6aa2c70fbda6b81281de98a72ea625001). It measures its own width,
// not the window's (the main area narrows when the workspace is open): two
// columns from 760 px (the ~850 px sidebar of the reference screenshot), the
// top cards side by side from 600 px, one column below that.

import { useEffect, useMemo, useRef, useState, type RefObject } from 'react';
import {
  getThreadContextTiming,
  type ThreadContextApi,
  type ThreadTimingApi,
} from '@/lib/api/agents';
import type { OpenPoint, OpenTurn } from '../ContextSections';
import { ContextBrowser } from './ContextBrowser';
import { ContextTrend } from './ContextTrend';
import { SelectedRequest } from './SelectedRequest';
import { TimingStats } from './TimingStats';
import { TokenStats } from './TokenStats';
import { WindowCard } from './WindowCard';

export const TWO_COLUMNS_PX = 760;
export const TOP_PAIR_PX = 600;

export type DashboardLayout = 'wide' | 'medium' | 'narrow';

export function layoutFor(width: number): DashboardLayout {
  if (width >= TWO_COLUMNS_PX) return 'wide';
  if (width >= TOP_PAIR_PX) return 'medium';
  return 'narrow';
}

/** Timing is read when the dashboard shows and again when a turn settles, never polled. */
function useThreadTiming(jobId: string, conversationKey: string, settled: string) {
  const [state, setState] = useState<{ key: string; timing: ThreadTimingApi | null } | null>(null);
  useEffect(() => {
    const controller = new AbortController();
    getThreadContextTiming(jobId, controller.signal).then(
      (timing) => {
        if (!controller.signal.aborted) setState({ key: conversationKey, timing });
      },
      () => {
        if (!controller.signal.aborted) setState({ key: conversationKey, timing: null });
      },
    );
    return () => controller.abort();
  }, [jobId, conversationKey, settled]);
  const current = state?.key === conversationKey ? state : null;
  return { timing: current?.timing ?? null, loading: current === null };
}

export function ContextDashboard({
  jobId,
  conversationKey,
  view,
  nowMs,
  selected,
  onSelect,
  onOpenPoint,
  onOpenTurn,
  trendRef,
}: {
  jobId: string;
  conversationKey: string;
  view: ThreadContextApi;
  nowMs: number;
  selected: number | null;
  onSelect: (index: number) => void;
  onOpenPoint: OpenPoint;
  onOpenTurn: OpenTurn;
  /** Where the trend is, for what selects a request from further down. */
  trendRef?: RefObject<HTMLDivElement>;
}) {
  const ref = useRef<HTMLDivElement>(null);
  const [width, setWidth] = useState(0);
  useEffect(() => {
    const element = ref.current;
    if (!element || typeof ResizeObserver === 'undefined') return undefined;
    const observer = new ResizeObserver(([entry]) => setWidth(entry.contentRect.width));
    observer.observe(element);
    return () => observer.disconnect();
  }, []);
  const layout = layoutFor(width || TWO_COLUMNS_PX);
  const settled = `${view.turns.length}:${view.turns.at(-1)?.state ?? ''}`;
  const { timing, loading } = useThreadTiming(jobId, conversationKey, settled);
  const latency = useMemo(
    () =>
      new Map(
        (timing?.requests ?? []).flatMap((r) =>
          r.latency_ms !== null ? [[r.response_id, r.latency_ms] as const] : [],
        ),
      ),
    [timing],
  );
  const keys = view.category_keys ?? [];
  const point = view.requests.find((p) => p.index === selected) ?? view.requests.at(-1) ?? null;
  const top = (
    <div className={layout === 'narrow' ? 'grid gap-3' : 'grid grid-cols-2 gap-3'}>
      <TokenStats tokens={view.tokens} />
      <TimingStats timing={timing} loading={loading} />
    </div>
  );
  const windowCard = <WindowCard point={point} latest={view.latest} keys={keys} nowMs={nowMs} />;
  const trend = (
    <div ref={trendRef} className="min-w-0 scroll-mt-3">
      <ContextTrend
        jobId={jobId}
        conversationKey={conversationKey}
        points={view.requests}
        keys={keys}
        latency={latency}
        selected={point?.index ?? null}
        onSelect={onSelect}
        footer={<SelectedRequest point={point} onOpenPoint={onOpenPoint} onOpenTurn={onOpenTurn} />}
      />
    </div>
  );
  const browser = (
    <ContextBrowser
      jobId={jobId}
      conversationKey={conversationKey}
      points={view.requests}
      selected={point?.index ?? null}
      onSelect={onSelect}
      onOpenPoint={onOpenPoint}
      onOpenTurn={onOpenTurn}
    />
  );
  return (
    <div ref={ref} data-layout={layout} className="grid gap-3">
      {top}
      {layout === 'wide' ? (
        <div className="grid grid-cols-[minmax(0,1.15fr)_minmax(0,1fr)] items-start gap-3">
          <div className="grid min-w-0 gap-3">
            {windowCard}
            {trend}
          </div>
          {browser}
        </div>
      ) : (
        <>
          {windowCard}
          {trend}
          {browser}
        </>
      )}
    </div>
  );
}
