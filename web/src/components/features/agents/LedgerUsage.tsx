'use client';

import { useEffect, useState } from 'react';

import {
  getAgentJobUsage,
  refreshAgentJobUsage,
  type AgentJobUsageApi,
  type AgentLedgerTotalsApi,
  type AgentLedgerUnknownRowsApi,
} from '@/lib/api/agents';

import type { AgentJob } from './types';

const POLL_INTERVAL_MS = 15_000;
const MAX_RETRY_INTERVAL_MS = 60_000;
const PERMANENT_REASONS = new Set([
  'attribution_unavailable',
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

function timestamp(value: string): string {
  const date = new Date(value);
  if (!Number.isFinite(date.getTime())) return 'unknown time';
  const today = new Date();
  const sameDay =
    date.getFullYear() === today.getFullYear() &&
    date.getMonth() === today.getMonth() &&
    date.getDate() === today.getDate();
  return date.toLocaleString([], {
    ...(sameDay ? {} : { month: 'short', day: 'numeric' }),
    ...(date.getFullYear() === today.getFullYear() ? {} : { year: 'numeric' }),
    hour: '2-digit',
    minute: '2-digit',
  });
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

function UsageRow({
  label,
  value,
  incomplete = false,
}: {
  label: string;
  value: string;
  incomplete?: boolean;
}) {
  return (
    <div className="flex items-start justify-between gap-4 py-1.5 text-[13px]">
      <dt className="text-gray-500 dark:text-gray-400">{label}</dt>
      <dd className="text-right text-gray-800 dark:text-gray-200">
        <span className="break-all tabular-nums">{value}</span>
        {incomplete ? (
          <span className="ml-1 text-xs text-gray-500 dark:text-gray-400"> (incomplete)</span>
        ) : null}
      </dd>
    </div>
  );
}

/** A user's usage summary; accounting diagnostics remain in the owner API. */
export function LedgerUsage({ job }: { job: AgentJob }) {
  const [loaded, setLoaded] = useState<LoadedUsage | null>(null);

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

    function schedule(delay = POLL_INTERVAL_MS) {
      clearTimeout(timer);
      timer = setTimeout(() => void load(), delay);
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
        const data = await (refresh
          ? refreshAgentJobUsage(job.id, signal)
          : getAgentJobUsage(job.id, signal));
        if (disposed || signal.aborted || requestGeneration !== generation) return;
        readFailures = 0;
        setLoaded((previous) => {
          const cached = previous?.jobId === job.id ? previous.data : null;
          return {
            jobId: job.id,
            data: data ? preserveKnownUsage(cached, data) : cached,
            loading: false,
            requesting: false,
            failed: false,
            unsupported: data === null,
          };
        });
        // A 404 means this API is unsupported or the job is no longer visible.
        if (!data) return;

        sourceHasFailed = hasTransientError(data);
        if (!sourceHasFailed) sourceFailures = 0;
        const scheduled = Boolean(data.ledger.refreshing || data.ledger.next_refresh_at);
        const eligible = canRefresh(data);
        const needsSourceRefresh = active || sourceHasFailed || data.ledger.status === 'pending';

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
          data.conversation?.active ||
          data.conversation?.refreshing ||
          data.conversation?.next_refresh_at ||
          (eligible && needsSourceRefresh)
        ) {
          const remainingBackoff = retryInterval(sourceFailures) - (Date.now() - lastRefreshAt);
          schedule(
            sourceHasFailed && !scheduled && !refresh
              ? Math.max(POLL_INTERVAL_MS, remainingBackoff)
              : POLL_INTERVAL_MS,
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
        // capped rate while the drawer is open, including for completed runs.
        readFailures = Math.min(readFailures + 1, 3);
        schedule(retryInterval(readFailures));
      }
    }

    void load();
    return () => {
      disposed = true;
      clearTimeout(timer);
      controller?.abort();
    };
  }, [job.id, job.state]);

  // Effects run after render; never expose the previous job in that first frame.
  const result = loaded?.jobId === job.id ? loaded : null;
  const data = result?.data;
  const ledger = data?.ledger;
  const loading = !result || result.loading;
  const gatewayTotals = ledger?.totals;
  const runtime = data?.runtime;
  const fallback = !gatewayTotals && runtime;
  const totals = gatewayTotals ?? runtimeTotals(runtime);
  const hasValues =
    totals &&
    (known(totals.calls) ||
      known(totals.tokens_in) ||
      known(totals.tokens_out) ||
      known(totals.cache_read));
  const failed = Boolean(result?.failed || result?.unsupported || (data && hasRefreshError(data)));
  const counting = Boolean(
    !failed &&
    !hasValues &&
    (ledger?.status === 'pending' || ledger?.refreshing || job.state === 'queued'),
  );
  const incomplete = Boolean(
    gatewayTotals &&
    ((ledger?.missing_attempts ?? 0) > 0 ||
      (ledger?.unknown_rows?.tokens_in ?? 0) > 0 ||
      (ledger?.unknown_rows?.tokens_out ?? 0) > 0),
  );
  const metrics: Array<[Exclude<keyof AgentLedgerTotalsApi, 'calls' | 'spent_usd'>, string]> = [
    ['tokens_in', 'Input tokens'],
    ['tokens_out', 'Output tokens'],
    ['cache_read', 'Cache read tokens'],
    ['cache_write', 'Cache write tokens'],
    ['reasoning', 'Reasoning tokens'],
  ];
  const unknown: AgentLedgerUnknownRowsApi | null = gatewayTotals
    ? (ledger?.unknown_rows ?? null)
    : null;
  const conversation = data?.conversation;

  return (
    <section aria-label="Usage">
      <h3 className="text-xs font-semibold uppercase tracking-wide text-gray-400 dark:text-gray-500">
        Usage
      </h3>
      {loading ? (
        <div role="status" aria-label="Loading usage" className="mt-3">
          <span className="sr-only">Loading usage…</span>
          <div className="h-4 w-4/5 animate-pulse rounded bg-gray-100 dark:bg-white/5" />
        </div>
      ) : (
        <>
          {hasValues && totals ? (
            <UsageSummary
              totals={totals}
              label="Run usage"
              className="mt-2 text-[13px] font-medium leading-6 text-gray-800 dark:text-gray-200"
            />
          ) : (
            <p role="status" className="mt-2 text-[13px] text-gray-500 dark:text-gray-400">
              {counting ? 'Counting usage…' : 'Usage temporarily unavailable.'}
            </p>
          )}
          {fallback && hasValues ? (
            <p className="mt-1 text-xs text-gray-500 dark:text-gray-400">Reported by agent.</p>
          ) : null}
          {incomplete ? (
            <p className="mt-1 text-xs text-gray-500 dark:text-gray-400">
              Some usage is unavailable.
            </p>
          ) : null}
          {gatewayTotals && ledger?.queried_at ? (
            <p className="mt-1 text-xs text-gray-500 dark:text-gray-400">
              Updated at{' '}
              <time dateTime={ledger.queried_at} title={ledger.queried_at}>
                {timestamp(ledger.queried_at)}
              </time>
              {failed
                ? ' · Update temporarily unavailable.'
                : result?.requesting || ledger.refreshing
                  ? ' · Updating…'
                  : ledger.next_refresh_at
                    ? ' · Usage may change.'
                    : ''}
            </p>
          ) : null}
          {conversation && conversation.turn_count > 1 ? (
            <div className="mt-3 text-xs leading-5 text-gray-500 dark:text-gray-400">
              <p>
                Conversation total
                {conversation.incomplete ? ' (incomplete)' : ''}
              </p>
              {conversation.totals ? (
                <UsageSummary totals={conversation.totals} label="Conversation usage" />
              ) : (
                <p>Usage temporarily unavailable.</p>
              )}
              {conversation.queried_at ? (
                <p>
                  Updated at{' '}
                  <time dateTime={conversation.queried_at} title={conversation.queried_at}>
                    {timestamp(conversation.queried_at)}
                  </time>
                </p>
              ) : null}
            </div>
          ) : null}
          {totals && hasValues ? (
            <details className="mt-3">
              <summary className="cursor-pointer text-xs font-medium text-gray-600 hover:text-gray-900 dark:text-gray-400 dark:hover:text-gray-200">
                Usage details
              </summary>
              <dl className="mt-2 divide-y divide-gray-100 dark:divide-white/5">
                {metrics.map(([key, label]) =>
                  known(totals[key]) ? (
                    <UsageRow
                      key={key}
                      label={label}
                      value={count(totals[key])}
                      incomplete={(unknown?.[key] ?? 0) > 0}
                    />
                  ) : null,
                )}
                {totals.spent_usd != null ? (
                  <UsageRow
                    label="Estimated inference cost"
                    value={`$${totals.spent_usd}`}
                    incomplete={(unknown?.spent_usd ?? 0) > 0}
                  />
                ) : null}
              </dl>
              {gatewayTotals && (ledger?.estimated_calls ?? 0) > 0 ? (
                <p className="mt-2 text-xs text-gray-500 dark:text-gray-400">
                  Some token counts are estimated.
                </p>
              ) : null}
            </details>
          ) : null}
        </>
      )}
    </section>
  );
}
