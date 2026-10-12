'use client';

import { useEffect, useRef, useState } from 'react';

import {
  getAgentJobUsage,
  refreshAgentJobUsage,
  type AgentJobUsageApi,
  type AgentLedgerTotalsApi,
} from '@/lib/api/agents';

import type { AgentJob } from './types';
import {
  isOlderUsage,
  onAgentRecordsChanged,
  PUSH_FALLBACK_POLL_MS,
  useAgentPushLive,
} from './agentPush';

const POLL_INTERVAL_MS = 15_000;
const SCHEDULE_POLL_FLOOR_MS = 1_000;
const MAX_RETRY_INTERVAL_MS = 60_000;
const PERMANENT_REASONS = new Set([
  'attribution_unavailable',
  // Nothing was called: there is no usage to wait for.
  'no_model_calls',
  'usage_unconfigured',
  'internal_api_not_configured',
  'binding_changed',
  'unknown_grant',
  'usage_unsupported_schema',
  'usage_response_mismatch',
  'invalid_usage_query',
]);

interface LoadedUsage {
  jobId: string;
  data: AgentJobUsageApi | null;
  loading: boolean;
  requesting: boolean;
  failed: boolean;
  unsupported: boolean;
}

type DisplayTotals = Omit<AgentLedgerTotalsApi, 'calls'> & { calls: number | null };

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

function retryInterval(failures: number): number {
  return Math.min(POLL_INTERVAL_MS * 2 ** Math.max(0, failures - 1), MAX_RETRY_INTERVAL_MS);
}

function canRefresh(data: AgentJobUsageApi): boolean {
  return data.ledger.attempts.some((attempt) => !PERMANENT_REASONS.has(attempt.reason ?? ''));
}

function hasRefreshError(data: AgentJobUsageApi): boolean {
  return (
    data.ledger.status === 'stale' ||
    data.ledger.attempts.some((attempt) => attempt.reason && attempt.reason !== 'pending')
  );
}

function hasTransientError(data: AgentJobUsageApi): boolean {
  return (
    data.ledger.attempts.some(
      (attempt) =>
        attempt.reason && attempt.reason !== 'pending' && !PERMANENT_REASONS.has(attempt.reason),
    ) ||
    (data.ledger.status === 'stale' && canRefresh(data) && !hasPermanentError(data))
  );
}

function hasPermanentError(data: AgentJobUsageApi): boolean {
  return data.ledger.attempts.some((attempt) => PERMANENT_REASONS.has(attempt.reason ?? ''));
}

function preserveKnownUsage(
  previous: AgentJobUsageApi | null,
  next: AgentJobUsageApi,
): AgentJobUsageApi {
  // A failed read must not replace previously measured usage with a different
  // source. The server also keeps its snapshots, but retain them across a
  // temporarily unavailable/older API response as well.
  if (!previous?.ledger.totals || next.ledger.totals) return next;
  return {
    ...next,
    ledger: {
      ...next.ledger,
      status: 'stale',
      totals: previous.ledger.totals,
      unknown_rows: previous.ledger.unknown_rows,
      estimated_calls: previous.ledger.estimated_calls,
      queried_at: previous.ledger.queried_at,
    },
  };
}

function runtimeTotals(runtime: AgentJobUsageApi['runtime'] | undefined): DisplayTotals | null {
  if (!runtime) return null;
  return {
    calls: null,
    tokens_in: runtime.normalized_input_tokens ?? null,
    tokens_out: runtime.output_tokens,
    cache_read: runtime.cache_read_tokens,
    cache_write: null,
    reasoning: null,
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
  const latestUsage = useRef<Pick<LoadedUsage, 'jobId' | 'data'> | null>(null);
  // While the owner's change stream flows, the control plane reads usage when
  // a model call finishes and pushes what it read: no refresh request and no
  // read from here, and the clock below is only a backstop.
  const pushLive = useAgentPushLive();

  useEffect(() => {
    let disposed = false;
    let controller: AbortController | undefined;
    let timer: ReturnType<typeof setTimeout> | undefined;
    let generation = 0;
    let readFailures = 0;
    let sourceFailures = 0;
    let sourceHasFailed = false;
    let lastRefreshAt = -Infinity;
    const active = job.state === 'running' || job.state === 'queued';
    const pollMs = pushLive ? PUSH_FALLBACK_POLL_MS : POLL_INTERVAL_MS;

    function schedule(delay = pollMs) {
      clearTimeout(timer);
      timer = setTimeout(() => void load(), delay);
    }

    function nextPollDelay(data: AgentJobUsageApi): number {
      // A turn that just ended can be waiting for its closing snapshot. Keep
      // that wait short even if the stream misses the accounting update.
      let delay =
        !active && (data.ledger.status === 'pending' || data.ledger.refreshing)
          ? POLL_INTERVAL_MS
          : pollMs;
      if (!active && data.ledger.next_refresh_at) {
        const due = Date.parse(data.ledger.next_refresh_at);
        if (Number.isFinite(due)) {
          // A late worker gets the normal terminal backstop, rather than a
          // one-second read loop for a schedule it cannot clear yet.
          delay =
            due > Date.now()
              ? Math.min(delay, Math.max(SCHEDULE_POLL_FLOOR_MS, due - Date.now()))
              : POLL_INTERVAL_MS;
        }
      }
      return delay;
    }

    async function load(refresh = false) {
      const requestGeneration = ++generation;
      controller?.abort();
      clearTimeout(timer);
      controller = new AbortController();
      const signal = controller.signal;
      if (refresh) {
        lastRefreshAt = Date.now();
        if (sourceHasFailed) sourceFailures = Math.min(sourceFailures + 1, 3);
      }
      setLoaded((previous) => ({
        jobId: job.id,
        data: previous?.jobId === job.id ? previous.data : null,
        loading: previous?.jobId !== job.id || previous.loading,
        requesting: refresh,
        failed: previous?.jobId === job.id && previous.failed,
        unsupported: false,
      }));

      try {
        const response = await (refresh
          ? refreshAgentJobUsage(job.id, signal)
          : getAgentJobUsage(job.id, signal));
        if (disposed || signal.aborted || requestGeneration !== generation) return;
        readFailures = 0;
        const data = show(response);
        // A 404 means this API is unsupported or the job is no longer visible.
        if (!data) return;

        sourceHasFailed = hasTransientError(data);
        if (!sourceHasFailed) sourceFailures = 0;
        const scheduled = Boolean(data.ledger.refreshing || data.ledger.next_refresh_at);
        const eligible = canRefresh(data);
        const needsSourceRefresh =
          !pushLive && (active || sourceHasFailed || data.ledger.status === 'pending');

        // Always read before requesting fresh accounting. This shares existing
        // work and leaves the server's delayed terminal checks at their due time.
        if (
          !refresh &&
          eligible &&
          needsSourceRefresh &&
          !scheduled &&
          Date.now() - lastRefreshAt >= retryInterval(sourceFailures)
        ) {
          void load(true);
          return;
        }
        if (
          refresh ||
          active ||
          scheduled ||
          data.ledger.status === 'pending' ||
          (eligible && needsSourceRefresh)
        ) {
          const remainingBackoff = retryInterval(sourceFailures) - (Date.now() - lastRefreshAt);
          const delay = nextPollDelay(data);
          schedule(
            sourceHasFailed && !scheduled && !refresh ? Math.max(delay, remainingBackoff) : delay,
          );
        }
      } catch {
        if (disposed || signal.aborted || requestGeneration !== generation) return;
        setLoaded((previous) => ({
          jobId: job.id,
          data: previous?.jobId === job.id ? previous.data : null,
          loading: false,
          requesting: false,
          failed: true,
          unsupported: false,
        }));
        // Network errors may clear without user action. Retry reads with a
        // capped rate while the job is displayed, including for completed runs.
        readFailures = Math.min(readFailures + 1, 3);
        schedule(retryInterval(readFailures));
      }
    }

    function show(data: AgentJobUsageApi | null): AgentJobUsageApi | null {
      const cached = latestUsage.current?.jobId === job.id ? latestUsage.current.data : null;
      // Display and scheduling both use the newer snapshot. A late read must
      // not cancel pending work or restart polling for usage already settled.
      const kept =
        data && cached && isOlderUsage(data, cached)
          ? cached
          : data
            ? preserveKnownUsage(cached, data)
            : cached;
      latestUsage.current = { jobId: job.id, data: kept };
      setLoaded({
        jobId: job.id,
        data: kept,
        loading: false,
        requesting: false,
        failed: false,
        unsupported: data === null,
      });
      return data === null ? null : kept;
    }

    // What the stream sent is shown unless something newer already is; a read
    // still on its way is shown after it only if it is newer. The clock goes on
    // as a backstop.
    function receive(data: AgentJobUsageApi) {
      const cached = latestUsage.current?.jobId === job.id ? latestUsage.current.data : null;
      // An older push must leave the newer snapshot's timer in place.
      if (cached && isOlderUsage(data, cached)) return;
      if (!data.version) {
        // Nothing orders the two: take the push as the newer.
        ++generation;
        controller?.abort();
      }
      readFailures = 0;
      const shown = show(data);
      if (!shown) return;
      sourceHasFailed = hasTransientError(shown);
      if (!sourceHasFailed) sourceFailures = 0;
      if (
        active ||
        shown.ledger.status === 'pending' ||
        shown.ledger.refreshing ||
        shown.ledger.next_refresh_at
      )
        schedule(nextPollDelay(shown));
      else clearTimeout(timer);
    }

    void load();
    const unsubscribe = onAgentRecordsChanged((change) => {
      // A changed job (it may have ended) is read whole: its usage depends on it.
      const reread = change.resync || change.jobs.some((pushed) => pushed.id === job.id);
      const pushed = change.usageValues[job.id];
      if (pushed && !reread) receive(pushed);
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

/** Only the current run's model calls and tokens; diagnostics stay in the API. */
export function LedgerUsage({ job, usage }: { job: AgentJob; usage: LoadedUsage | null }) {
  const result = usage?.jobId === job.id ? usage : null;
  const data = result?.data;
  const ledger = data?.ledger;
  const loading = !result || result.loading;
  const gatewayTotals = ledger?.totals;
  const runtime = data?.runtime;
  const fallback = !gatewayTotals && runtime;
  const totals = gatewayTotals ?? runtimeTotals(runtime);
  const hasValues =
    totals && (known(totals.calls) || known(totals.tokens_in) || known(totals.tokens_out));
  const failed = Boolean(result?.failed || result?.unsupported || (data && hasRefreshError(data)));
  const counting = Boolean(
    !failed &&
    ledger?.status !== 'unavailable' &&
    (ledger?.status === 'pending' ||
      ledger?.refreshing ||
      result?.requesting ||
      job.state === 'queued' ||
      job.state === 'running'),
  );
  const incomplete = Boolean(
    gatewayTotals &&
    ((ledger?.missing_attempts ?? 0) > 0 ||
      (ledger?.unknown_rows?.tokens_in ?? 0) > 0 ||
      (ledger?.unknown_rows?.tokens_out ?? 0) > 0),
  );
  const status = failed
    ? 'Update temporarily unavailable.'
    : counting
      ? 'Counting usage…'
      : ledger?.next_refresh_at
        ? 'Usage may change.'
        : null;

  // Nothing was called, so there is nothing to show: every attempt ended
  // before its agent could reach a model, or the turn never ran at all.
  const terminal = job.state !== 'running' && job.state !== 'queued';
  const nothingCalled =
    !loading &&
    !(totals && hasValues) &&
    Boolean(
      ledger && (ledger.status === 'available' || (terminal && ledger.attempts.length === 0)),
    );
  if (nothingCalled) return null;

  return (
    <section
      aria-label="Usage"
      className="flex flex-wrap items-baseline gap-x-2 gap-y-1 text-xs leading-5 text-gray-500 dark:text-gray-400"
    >
      {loading ? (
        <span role="status" aria-label="Loading usage">
          <span className="sr-only">Loading usage…</span>
          <span
            aria-hidden="true"
            className="inline-block h-3 w-52 max-w-full animate-pulse rounded bg-gray-100 dark:bg-white/5"
          />
        </span>
      ) : totals && hasValues ? (
        <>
          <UsageSummary totals={totals} label="Run usage" />
          {fallback ? <span>Reported by agent.</span> : null}
          {incomplete ? <span>Some usage is unavailable.</span> : null}
          {gatewayTotals && (ledger?.estimated_calls ?? 0) > 0 ? (
            <span>Some token counts are estimated.</span>
          ) : null}
          {gatewayTotals && status ? <span>{status}</span> : null}
        </>
      ) : (
        <span role="status">{counting ? 'Counting usage…' : 'Usage temporarily unavailable.'}</span>
      )}
    </section>
  );
}
