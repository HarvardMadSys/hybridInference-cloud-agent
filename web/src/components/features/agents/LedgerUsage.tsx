'use client';

import { useEffect, useRef, useState } from 'react';

import {
  getAgentJobUsage,
  refreshAgentJobUsage,
  type AgentJobUsageApi,
  type AgentLedgerStatus,
  type AgentLedgerTotalsApi,
} from '@/lib/api/agents';

import type { AgentJob } from './types';

const POLL_INTERVAL_MS = 15_000;
const MAX_POLLS = 20;

// The metrics that decide whether a snapshot is partial. Reasoning is
// informational: the Gateway records a reasoning breakdown only when an
// upstream reports a positive one, and never on its Anthropic-messages
// surface, so an unreported value is named below rather than degrading the
// whole snapshot.
const CORE_METRICS = ['tokens_in', 'tokens_out', 'cache_read', 'cache_write', 'spent_usd'] as const;

const STATUS_LABELS: Record<AgentLedgerStatus, string> = {
  pending: 'Pending',
  available: 'Available snapshot',
  partial: 'Partial snapshot',
  unavailable: 'Unavailable',
  stale: 'Cached snapshot',
};

const REASON_LABELS: Record<string, string> = {
  attribution_unavailable: 'This attempt has no recorded usage attribution.',
  pending: 'Waiting for a usage snapshot.',
  usage_unsupported_schema: 'This Gateway does not support usage snapshots yet.',
  usage_invalid_response: 'The Gateway returned an unusable usage snapshot.',
  usage_response_mismatch: 'The Gateway returned an unusable usage snapshot.',
  usage_unavailable: 'The Gateway ledger is unavailable.',
  usage_unconfigured: 'Gateway usage snapshots are not configured.',
  ledger_unavailable: 'The Gateway ledger is unavailable.',
  gateway_unavailable: 'The Gateway could not be reached.',
  binding_changed: 'Usage cannot be refreshed through the current Gateway connection.',
  unknown_grant: 'The Gateway could not find this attempt’s usage grant.',
  refresh_failed: 'The latest usage refresh failed.',
};

interface LoadedUsage {
  jobId: string;
  data: AgentJobUsageApi | null;
  loading: boolean;
  requesting: boolean;
  failed: boolean;
  unsupported: boolean;
  pollingPaused: boolean;
}

function count(value: number | null | undefined): string {
  return typeof value === 'number' && Number.isFinite(value) && value >= 0
    ? value.toLocaleString('en-US')
    : '—';
}

function timestamp(value: string): string {
  const date = new Date(value);
  if (!Number.isFinite(date.getTime())) return '—';
  return `${date.toLocaleString('en-GB', {
    timeZone: 'UTC',
    year: 'numeric',
    month: 'short',
    day: '2-digit',
    hour: '2-digit',
    minute: '2-digit',
    second: '2-digit',
    hour12: false,
  })} UTC`;
}

function UsageRow({
  label,
  value,
  unknown = 0,
}: {
  label: string;
  value: string;
  unknown?: number;
}) {
  return (
    <div className="flex items-start justify-between gap-4 py-1.5 text-[13px]">
      <dt className="text-gray-500 dark:text-gray-400">{label}</dt>
      <dd className="text-right text-gray-800 dark:text-gray-200">
        <span className="break-all tabular-nums">{value}</span>
        {unknown > 0 ? (
          <span className="block text-xs text-amber-700 dark:text-amber-400">
            {count(unknown)} request{unknown === 1 ? '' : 's'} missing this value
          </span>
        ) : null}
      </dd>
    </div>
  );
}

/** Mounted only inside the usage drawer, with one owner-scoped request for every retry. */
export function LedgerUsage({ job }: { job: AgentJob }) {
  const [loaded, setLoaded] = useState<LoadedUsage | null>(null);
  const [scope, setScope] = useState<{ jobId: string; attemptId: number } | null>(null);
  const refreshRef = useRef<(() => void) | null>(null);

  useEffect(() => {
    let disposed = false;
    let controller: AbortController | undefined;
    let timer: ReturnType<typeof setTimeout> | undefined;
    let generation = 0;
    let polls = 0;
    let checkedInitialRefresh = false;
    const active = job.state === 'running' || job.state === 'queued';

    async function load(refresh = false) {
      const requestGeneration = ++generation;
      controller?.abort();
      clearTimeout(timer);
      controller = new AbortController();
      const signal = controller.signal;
      setLoaded((previous) => ({
        jobId: job.id,
        data: previous?.jobId === job.id ? previous.data : null,
        loading: previous?.jobId !== job.id || previous.loading,
        requesting: refresh,
        failed: false,
        unsupported: false,
        pollingPaused: false,
      }));

      try {
        const data = await (refresh
          ? refreshAgentJobUsage(job.id, signal)
          : getAgentJobUsage(job.id, signal));
        if (disposed || signal.aborted || requestGeneration !== generation) return;
        const keepPolling = Boolean(
          // A throttled POST can queue work a few seconds in the future, so
          // read again even when its immediate cached response is unchanged.
          data &&
          (refresh ||
            active ||
            data.ledger.refreshing ||
            data.ledger.next_refresh_at != null ||
            data.ledger.status === 'pending'),
        );
        const pollingPaused = keepPolling && polls >= MAX_POLLS;
        setLoaded((previous) => ({
          jobId: job.id,
          data: data ?? (previous?.jobId === job.id ? previous.data : null),
          loading: false,
          requesting: false,
          failed: false,
          unsupported: data === null,
          pollingPaused,
        }));
        if (!checkedInitialRefresh) {
          checkedInitialRefresh = true;
          // Running attempts are refreshed on demand. Read first so opening
          // historical or unsupported jobs never queues useless work.
          if (
            active &&
            !refresh &&
            data &&
            !data.ledger.refreshing &&
            data.ledger.attempts.some(
              (attempt) => attempt.reason === 'pending' || attempt.windows.length > 0,
            )
          ) {
            void load(true);
            return;
          }
        }
        if (keepPolling && !pollingPaused) {
          timer = setTimeout(() => {
            polls += 1;
            void load();
          }, POLL_INTERVAL_MS);
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
          pollingPaused: false,
        }));
      }
    }

    refreshRef.current = () => {
      polls = 0;
      void load(true);
    };
    void load();
    return () => {
      disposed = true;
      clearTimeout(timer);
      controller?.abort();
      refreshRef.current = null;
    };
  }, [job.id, job.state]);

  // Do not render another job's cached totals during the frame before effect cleanup.
  const result = loaded?.jobId === job.id ? loaded : null;
  const data = result?.data;
  const ledger = data?.ledger;
  const selected =
    scope?.jobId === job.id
      ? ledger?.attempts.find((attempt) => attempt.attempt_id === scope.attemptId)
      : undefined;
  const summary = selected ?? ledger;
  const reason =
    selected?.reason ?? (ledger?.attempts.length === 1 ? ledger.attempts[0].reason : null);
  const missingAttempts = selected ? (selected.totals ? 0 : 1) : (ledger?.missing_attempts ?? 0);
  const unknownRows = summary?.unknown_rows;
  const partial =
    summary?.status === 'partial' ||
    missingAttempts > 0 ||
    CORE_METRICS.some((key) => (unknownRows?.[key] ?? 0) > 0);
  const reasoningUnknown = summary?.totals ? (unknownRows?.reasoning ?? 0) : 0;
  const failed = result?.failed || result?.unsupported;
  const status = failed && summary?.totals ? 'stale' : (summary?.status ?? 'unavailable');
  const loading = !result || result.loading;
  const refreshing = Boolean(
    result?.requesting ||
    (ledger?.refreshing && !result?.pollingPaused && !result?.failed && !result?.unsupported),
  );
  const metrics: Array<[Exclude<keyof AgentLedgerTotalsApi, 'calls' | 'spent_usd'>, string]> = [
    ['tokens_in', 'Input tokens'],
    ['tokens_out', 'Output tokens'],
    ['cache_read', 'Cache read tokens'],
    ['cache_write', 'Cache write tokens'],
    ['reasoning', 'Reasoning tokens'],
  ];

  return (
    <section aria-label="Usage">
      <div className="flex items-center justify-between gap-3">
        <h3 className="text-xs font-semibold uppercase tracking-wide text-gray-400 dark:text-gray-500">
          Usage
        </h3>
        <button
          type="button"
          onClick={() => refreshRef.current?.()}
          disabled={loading || refreshing}
          className="rounded-md px-2 py-1 text-xs font-medium text-gray-600 hover:bg-gray-100 disabled:opacity-50 dark:text-gray-300 dark:hover:bg-white/5"
          aria-label="Refresh usage"
        >
          {refreshing ? 'Refreshing…' : 'Refresh'}
        </button>
      </div>

      <p className="mt-1 text-xs text-gray-500 dark:text-gray-400">
        This run includes all its attempts and retries. Other conversation turns are separate.
      </p>
      {ledger && ledger.attempts.length > 0 ? (
        <label className="mt-3 flex items-center gap-3 text-xs text-gray-600 dark:text-gray-400">
          Gateway scope
          <select
            className="min-w-0 flex-1 rounded-md border border-gray-200 bg-white px-2 py-1.5 text-gray-800 dark:border-white/10 dark:bg-gray-900 dark:text-gray-200"
            value={selected?.attempt_id ?? 'all'}
            onChange={(event) =>
              setScope(
                event.target.value === 'all'
                  ? null
                  : { jobId: job.id, attemptId: Number(event.target.value) },
              )
            }
          >
            <option value="all">All attempts and retries</option>
            {ledger.attempts.map((attempt) => (
              <option key={attempt.attempt_id} value={attempt.attempt_id}>
                Attempt {attempt.attempt_no} · {STATUS_LABELS[attempt.status]}
              </option>
            ))}
          </select>
        </label>
      ) : null}

      <section aria-label="Gateway ledger" className="mt-3">
        <div className="flex flex-wrap items-center justify-between gap-2 text-[13px]">
          <h4 className="font-medium text-gray-800 dark:text-gray-200">
            Gateway ledger{selected ? ` · attempt ${selected.attempt_no}` : ' · all attempts'}
          </h4>
          {!loading ? (
            <span className="text-xs text-gray-500 dark:text-gray-400">
              {STATUS_LABELS[status]}
            </span>
          ) : null}
        </div>

        {loading ? (
          <p role="status" className="mt-2 text-xs text-gray-500 dark:text-gray-400">
            Loading usage…
          </p>
        ) : null}
        {result?.unsupported ? (
          <p role="status" className="mt-2 text-xs text-gray-500 dark:text-gray-400">
            Ledger unavailable on this deployment.{data ? ' Showing the last cached snapshot.' : ''}
          </p>
        ) : null}
        {result?.failed ? (
          <p role="status" className="mt-2 text-xs text-amber-700 dark:text-amber-400">
            Usage could not be refreshed.
            {data ? ' Showing the last cached snapshot.' : ' Try again.'}
          </p>
        ) : null}
        {reason && reason !== 'pending' ? (
          <p className="mt-2 text-xs text-gray-500 dark:text-gray-400">
            {REASON_LABELS[reason] ?? 'Usage is unavailable for this attempt.'}
          </p>
        ) : null}
        {status === 'stale' && !failed ? (
          <p className="mt-2 text-xs text-amber-700 dark:text-amber-400">
            The latest refresh failed. Showing the last cached snapshot.
          </p>
        ) : null}
        {status === 'pending' && !loading ? (
          <p role="status" className="mt-2 text-xs text-gray-500 dark:text-gray-400">
            Waiting for a usage snapshot.
          </p>
        ) : null}
        {partial && !loading ? (
          <p className="mt-2 text-xs text-amber-700 dark:text-amber-400">
            {summary?.totals
              ? 'Partial totals: only known values are included.'
              : 'Usage totals are unavailable.'}
            {missingAttempts > 0
              ? ` ${count(missingAttempts)} attempt${missingAttempts === 1 ? '' : 's'} without a usable snapshot.`
              : ''}
          </p>
        ) : null}
        {!partial && !loading && reasoningUnknown > 0 ? (
          <p className="mt-2 text-xs text-gray-500 dark:text-gray-400">
            Core usage is complete. The Gateway did not report reasoning tokens for{' '}
            {count(reasoningUnknown)} call{reasoningUnknown === 1 ? '' : 's'}.
          </p>
        ) : null}

        <dl className="mt-2 divide-y divide-gray-100 dark:divide-white/5">
          <UsageRow label="Model calls" value={count(summary?.totals?.calls)} />
          <UsageRow
            label="Spend (USD)"
            value={summary?.totals?.spent_usd != null ? `$${summary.totals.spent_usd}` : '—'}
            unknown={summary?.unknown_rows?.spent_usd}
          />
          {metrics.map(([key, label]) => (
            <UsageRow
              key={key}
              label={label}
              value={count(summary?.totals?.[key])}
              unknown={summary?.unknown_rows?.[key]}
            />
          ))}
        </dl>

        {(summary?.estimated_calls ?? 0) > 0 ? (
          <p className="mt-2 text-xs text-amber-700 dark:text-amber-400">
            {count(summary?.estimated_calls)} call
            {summary?.estimated_calls === 1 ? ' includes' : 's include'} estimated usage.
          </p>
        ) : null}
        {summary?.queried_at ? (
          <p className="mt-2 text-xs text-gray-500 dark:text-gray-400">
            Oldest snapshot fetched{' '}
            <time dateTime={summary.queried_at} title={summary.queried_at}>
              {timestamp(summary.queried_at)}
            </time>
          </p>
        ) : null}
        {ledger ? (
          <p className="mt-2 text-xs text-gray-500 dark:text-gray-400">
            Refreshable snapshot from Gateway request logs, attributed by request start time. Late
            logs may change these totals.
          </p>
        ) : null}
        {result?.pollingPaused ? (
          <p role="status" className="mt-2 text-xs text-gray-500 dark:text-gray-400">
            Automatic updates paused. Refresh to check again.
          </p>
        ) : null}
        {selected && selected.windows.length > 0 ? (
          <div className="mt-3 rounded-md bg-gray-50 p-3 text-xs text-gray-500 dark:bg-gray-950 dark:text-gray-400">
            <p>
              Attributed by request start time; each window includes its start and excludes its end.
            </p>
            <ul className="mt-2 space-y-2" aria-label="Request start windows">
              {selected.windows.map((window, index) => (
                <li key={`${window.since}:${window.until}:${index}`}>
                  <time dateTime={window.since} title={window.since}>
                    {timestamp(window.since)}
                  </time>
                  <span className="mx-1">→</span>
                  <time dateTime={window.until} title={window.until}>
                    {timestamp(window.until)}
                  </time>
                </li>
              ))}
            </ul>
          </div>
        ) : null}
      </section>

      <section
        aria-label="Runtime reported usage"
        className="mt-4 border-t border-gray-100 pt-3 dark:border-white/5"
      >
        <h4 className="text-[13px] font-medium text-gray-800 dark:text-gray-200">
          Runtime reported · all attempts
        </h4>
        <dl className="mt-2 divide-y divide-gray-100 dark:divide-white/5">
          <UsageRow label="Reported input tokens" value={count(data?.runtime.input_tokens)} />
          <UsageRow label="Reported output tokens" value={count(data?.runtime.output_tokens)} />
          <UsageRow
            label="Reported cache read tokens"
            value={count(data?.runtime.cache_read_tokens)}
          />
        </dl>
        <p className="mt-2 text-xs text-gray-500 dark:text-gray-400">
          Gateway input includes cached tokens.
          {job.runtime === 'claude-code'
            ? ' Claude Code reports uncached input separately, so these counts use different definitions.'
            : ' The runtime reports its token counts independently.'}
        </p>
      </section>
    </section>
  );
}
