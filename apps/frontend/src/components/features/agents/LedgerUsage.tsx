'use client';

import { useEffect, useRef, useState } from 'react';

import {
  getAgentJobUsage,
  type AgentJobUsageApi,
  type AgentUsageTotalsApi,
} from '@/lib/api/agents';

import { useT } from '@/components/providers';
import type { AgentJob } from './types';
import {
  isOlderUsage,
  onAgentRecordsChanged,
  PUSH_FALLBACK_POLL_MS,
  useAgentPushLive,
} from './agentPush';

const POLL_INTERVAL_MS = 15_000;
const MAX_RETRY_INTERVAL_MS = 60_000;

interface LoadedUsage {
  jobId: string;
  data: AgentJobUsageApi | null;
  loading: boolean;
  failed: boolean;
}

type DisplayTotals = AgentUsageTotalsApi;

function known(value: number | null | undefined): value is number {
  return typeof value === 'number' && Number.isFinite(value) && value >= 0;
}

function count(value: number): string {
  return value.toLocaleString('en-US');
}

function compactCount(value: number): string {
  return value
    .toLocaleString('en-US', { notation: 'compact', maximumFractionDigits: 1 })
    .toLowerCase();
}

function runtimeTotals(runtime: AgentJobUsageApi['runtime'] | undefined): DisplayTotals | null {
  if (!runtime) return null;
  return {
    calls: null,
    tokens_in: runtime.normalized_input_tokens ?? null,
    tokens_out: runtime.output_tokens,
    cache_read: runtime.cache_read_tokens,
    cache_write: runtime.cache_write_tokens ?? null,
    reasoning: runtime.reasoning_tokens ?? null,
    // Agents report no cost: the owner's provider bills the turn.
    spent_usd: null,
  };
}

function UsageSummary({
  totals,
  label,
  className,
}: {
  totals: DisplayTotals;
  label: string;
  className?: string;
}) {
  const values: Array<{ value: number; label: string }> = [];
  if (known(totals.calls)) {
    values.push({ value: totals.calls, label: totals.calls === 1 ? 'model call' : 'model calls' });
  }
  if (known(totals.tokens_in)) values.push({ value: totals.tokens_in, label: 'input tokens' });
  if (known(totals.tokens_out)) values.push({ value: totals.tokens_out, label: 'output tokens' });
  return values.length > 0 ? (
    <p aria-label={label} className={className}>
      {values.map((item, index) => (
        <span key={item.label}>
          {index > 0 ? ' · ' : ''}
          <span title={`${count(item.value)} ${item.label}`}>
            {compactCount(item.value)} {item.label}
          </span>
        </span>
      ))}
    </p>
  ) : (
    <p aria-label={label} className={className}>
      Token totals are unavailable.
    </p>
  );
}

// Owned by the job view, independently of which message holds the summary.
export function useLedgerUsage(job: AgentJob): LoadedUsage | null {
  const [loaded, setLoaded] = useState<LoadedUsage | null>(null);
  const latest = useRef<Pick<LoadedUsage, 'jobId' | 'data'> | null>(null);
  // While the owner's change stream flows, the clock below is only a backstop.
  const pushLive = useAgentPushLive();

  useEffect(() => {
    let disposed = false;
    let controller: AbortController | undefined;
    let timer: ReturnType<typeof setTimeout> | undefined;
    let generation = 0;
    let failures = 0;
    const active = job.state === 'running' || job.state === 'queued';
    const pollMs = pushLive ? PUSH_FALLBACK_POLL_MS : POLL_INTERVAL_MS;

    function schedule(delay: number) {
      clearTimeout(timer);
      timer = setTimeout(() => void load(), delay);
    }

    function show(data: AgentJobUsageApi | null) {
      const cached = latest.current?.jobId === job.id ? latest.current.data : null;
      // A late read must not replace a newer snapshot.
      const kept = data && cached && isOlderUsage(data, cached) ? cached : (data ?? cached);
      latest.current = { jobId: job.id, data: kept };
      setLoaded({ jobId: job.id, data: kept, loading: false, failed: false });
    }

    async function load() {
      const requestGeneration = ++generation;
      controller?.abort();
      clearTimeout(timer);
      controller = new AbortController();
      const signal = controller.signal;
      setLoaded((previous) => ({
        jobId: job.id,
        data: previous?.jobId === job.id ? previous.data : null,
        loading: previous?.jobId !== job.id || previous.loading,
        failed: previous?.jobId === job.id && previous.failed,
      }));
      try {
        const response = await getAgentJobUsage(job.id, signal);
        if (disposed || signal.aborted || requestGeneration !== generation) return;
        failures = 0;
        show(response);
        if (response && active) schedule(pollMs);
      } catch {
        if (disposed || signal.aborted || requestGeneration !== generation) return;
        setLoaded((previous) => ({
          jobId: job.id,
          data: previous?.jobId === job.id ? previous.data : null,
          loading: false,
          failed: true,
        }));
        // Network errors may clear without user action. Retry reads with a
        // capped rate while the job is displayed, including for completed runs.
        failures = Math.min(failures + 1, 3);
        schedule(Math.min(POLL_INTERVAL_MS * 2 ** (failures - 1), MAX_RETRY_INTERVAL_MS));
      }
    }

    void load();
    const unsubscribe = onAgentRecordsChanged((change) => {
      // A changed job (it may have ended) is read whole: its usage depends on it.
      const reread = change.resync || change.jobs.some((pushed) => pushed.id === job.id);
      const pushed = change.usageValues[job.id];
      if (pushed && !reread) show(pushed);
      else if (reread || change.usage.includes(job.id)) void load();
    });
    return () => {
      disposed = true;
      unsubscribe();
      clearTimeout(timer);
      controller?.abort();
    };
  }, [job.id, job.state, pushLive]);

  // Effects run after render; never expose the previous job in that first frame.
  return loaded?.jobId === job.id ? loaded : null;
}

/** The runtime's counters, and that the owner's provider bills the turn. */
export function LedgerUsage({ usage }: { usage: LoadedUsage | null }) {
  const t = useT();
  const totals = runtimeTotals(usage?.data?.runtime);
  const hasValues = totals && (known(totals.tokens_in) || known(totals.tokens_out));
  return (
    <section
      aria-label="Usage"
      className="flex flex-wrap items-baseline gap-x-2 gap-y-1 text-xs leading-5 text-gray-500 dark:text-gray-400"
    >
      {totals && hasValues ? (
        <>
          <UsageSummary totals={totals} label="Run usage" />
          <span>Reported by agent.</span>
        </>
      ) : null}
      <span>{t('modelSource.usageExternal')}</span>
    </section>
  );
}
