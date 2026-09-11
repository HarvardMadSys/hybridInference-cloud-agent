'use client';

// Run details, scoped to one turn.
//
// The previous drawer described "the run" while the page could move on to a
// later turn, so its numbers silently changed meaning. This one is opened from
// a specific turn and reads that job only: its own status, its own attempts,
// its own ledger snapshot.

import { useCallback, useEffect, useMemo, useRef, useState } from 'react';

import {
  getAgentJobUsage,
  refreshAgentJobUsage,
  type AgentJobApi,
  type AgentJobApiState,
  type AgentJobUsageApi,
  type AgentThreadEventApi,
} from '@/lib/api/agents';
import type { StoredEvent } from './trajectorySteps';

import {
  DetailRow,
  DetailRows,
  DetailSection,
  DownloadButton,
  EmptyNote,
  StoredRecord,
  TabPanel,
  recordedAt,
  recordedCount,
} from './StepDetailParts';
import { formatDuration } from './TurnFooter';
import type { AgentJob } from './types';

export type RunTab = 'overview' | 'usage' | 'raw';

export const RUN_TABS: Array<{ key: RunTab; label: string }> = [
  { key: 'overview', label: 'Overview' },
  { key: 'usage', label: 'Usage' },
  { key: 'raw', label: 'Raw' },
];

const STATE_LABEL: Record<AgentJobApiState, string> = {
  waiting: 'Waiting',
  queued: 'Queued',
  running: 'Running',
  publishing: 'Publishing',
  succeeded: 'Completed',
  failed: 'Failed',
  cancelled: 'Cancelled',
};

const LEDGER_LABEL: Record<AgentJobUsageApi['ledger']['status'], string> = {
  pending: 'Pending',
  available: 'Available',
  partial: 'Partial',
  unavailable: 'Unavailable',
  stale: 'Stale',
};

/**
 * One cached snapshot per job.
 *
 * The Chat transcript polls its own usage for the turn it is showing; the
 * inspector reads the same resource only when it has a reason to: the snapshot
 * is unknown, the reader asked for a refresh, or the ledger still has queued
 * work. A settled snapshot is reused, so opening the panel repeatedly is free.
 */
const usageCache = new Map<string, AgentJobUsageApi | null>();

/** How long to wait before reading a ledger that has not settled yet. */
const LEDGER_POLL_MS = 15_000;
/** Reads to spend on a queued refresh before leaving its state as it is. */
const MAX_LEDGER_POLLS = 8;

/**
 * Whether the ledger still has work queued for it.
 *
 * A refresh request only queues the work, and the answer to that request is the
 * snapshot from before the worker ran. Reading again — and again, until the
 * ledger settles — is what turns the queued answer into the finished one.
 */
function ledgerUnsettled(usage: AgentJobUsageApi | null): boolean {
  return Boolean(usage && (usage.ledger.refreshing || usage.ledger.status === 'pending'));
}

function useJobUsage(jobId: string): {
  usage: AgentJobUsageApi | null;
  loaded: boolean;
  refreshing: boolean;
  error: string | null;
  refresh: () => void;
} {
  const cached = usageCache.get(jobId);
  const [usage, setUsage] = useState<AgentJobUsageApi | null>(cached ?? null);
  const [loaded, setLoaded] = useState(cached !== undefined);
  const [refreshing, setRefreshing] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [read, setRead] = useState(0);
  const requested = useRef(false);
  const polls = useRef(0);
  const currentJob = useRef(jobId);

  useEffect(() => {
    if (currentJob.current !== jobId) {
      currentJob.current = jobId;
      requested.current = false;
      polls.current = 0;
    }
    const known = usageCache.get(jobId);
    const mustRead = requested.current || known === undefined || ledgerUnsettled(known);
    requested.current = false;
    setUsage(known ?? null);
    setLoaded(known !== undefined);
    if (!mustRead) return undefined;
    const controller = new AbortController();
    let timer: ReturnType<typeof setTimeout> | undefined;
    getAgentJobUsage(jobId, controller.signal)
      .then((result) => {
        if (controller.signal.aborted) return;
        usageCache.set(jobId, result);
        setUsage(result);
        setError(null);
        if (ledgerUnsettled(result) && polls.current < MAX_LEDGER_POLLS) {
          polls.current += 1;
          timer = setTimeout(() => setRead((value) => value + 1), LEDGER_POLL_MS);
        }
      })
      .catch((cause: unknown) => {
        if (!controller.signal.aborted) {
          setError(cause instanceof Error ? cause.message : 'Could not load usage');
        }
      })
      .finally(() => {
        if (!controller.signal.aborted) setLoaded(true);
      });
    return () => {
      controller.abort();
      if (timer) clearTimeout(timer);
    };
  }, [jobId, read]);

  const refresh = useCallback(() => {
    setRefreshing(true);
    polls.current = 0;
    refreshAgentJobUsage(jobId)
      .then((result) => {
        usageCache.set(jobId, result);
        setUsage(result);
      })
      .catch((cause: unknown) => {
        setError(cause instanceof Error ? cause.message : 'Could not refresh usage');
      })
      .finally(() => {
        setRefreshing(false);
        // The queued refresh has not run yet, so read the ledger again. Further
        // reads follow while it still reports work in flight.
        requested.current = true;
        setRead((value) => value + 1);
      });
  }, [jobId]);

  return { usage, loaded, refreshing, error, refresh };
}

function elapsedLabel(started: string | null, finished: string | null): string {
  if (!started || !finished) return 'Not recorded';
  const from = Date.parse(started);
  const to = Date.parse(finished);
  if (!Number.isFinite(from) || !Number.isFinite(to) || to < from) return 'Not recorded';
  return formatDuration(to - from) ?? 'Not recorded';
}

function UsageTotals({ usage }: { usage: AgentJobUsageApi }) {
  const totals = usage.ledger.totals;
  return (
    <>
      <DetailRows>
        <DetailRow label="Ledger">{LEDGER_LABEL[usage.ledger.status]}</DetailRow>
        {usage.ledger.status !== 'available' ? (
          <DetailRow label="Completeness" tone="muted">
            {usage.ledger.missing_attempts > 0
              ? `${usage.ledger.missing_attempts} attempt${usage.ledger.missing_attempts === 1 ? '' : 's'} not covered by the ledger`
              : 'The ledger has not finalized this turn yet.'}
          </DetailRow>
        ) : null}
        <DetailRow label="Model calls">{recordedCount(totals?.calls ?? null)}</DetailRow>
        <DetailRow label="Input tokens">{recordedCount(totals?.tokens_in ?? null)}</DetailRow>
        <DetailRow label="Output tokens">{recordedCount(totals?.tokens_out ?? null)}</DetailRow>
        <DetailRow label="Cache read">{recordedCount(totals?.cache_read ?? null)}</DetailRow>
        <DetailRow label="Cache write">{recordedCount(totals?.cache_write ?? null)}</DetailRow>
        <DetailRow label="Reasoning">{recordedCount(totals?.reasoning ?? null)}</DetailRow>
        <DetailRow label="Cost">
          {totals?.spent_usd ? `$${totals.spent_usd}` : 'Not recorded'}
        </DetailRow>
      </DetailRows>
      <p className="text-[11px] text-gray-400 dark:text-gray-500">
        These totals belong to this turn, including its retries. Conversation totals are not
        substituted when this turn&rsquo;s snapshot is incomplete.
      </p>
    </>
  );
}

export function RunDetails({
  jobId,
  turnNo,
  tab,
  job,
  fallback,
  events,
  onOpenGitDiff,
}: {
  jobId: string;
  turnNo: number;
  tab: RunTab;
  job?: AgentJobApi | null;
  fallback?: AgentJob | null;
  events: StoredEvent[];
  onOpenGitDiff: () => void;
}) {
  const idPrefix = `run-${jobId}`;
  const { usage, loaded, refreshing, error, refresh } = useJobUsage(jobId);

  const runEvents = useMemo(
    () => (events as AgentThreadEventApi[]).filter((event) => event.job_id === jobId),
    [events, jobId],
  );
  const attempts = useMemo(() => {
    const grouped = new Map<number, AgentThreadEventApi[]>();
    for (const event of runEvents) {
      grouped.set(event.attempt_id, [...(grouped.get(event.attempt_id) ?? []), event]);
    }
    return [...grouped.entries()].sort(([a], [b]) => a - b);
  }, [runEvents]);

  const state = job?.state ?? null;
  const startedAt = job?.started_at ?? fallback?.startedAt ?? null;
  const finishedAt = job?.finished_at ?? fallback?.finishedAt ?? null;
  const attemptRows = job?.attempt_telemetry ?? [];
  const openUrl = job?.published_pr_url ?? fallback?.prUrl ?? null;

  if (tab === 'usage') {
    if (!loaded) {
      return (
        <TabPanel idPrefix={idPrefix} tab="usage">
          <EmptyNote>Loading usage…</EmptyNote>
        </TabPanel>
      );
    }
    if (!usage) {
      return (
        <TabPanel idPrefix={idPrefix} tab="usage">
          <EmptyNote>
            {error ?? 'This deployment has no billing ledger, so usage was not recorded.'}
          </EmptyNote>
          <button
            type="button"
            onClick={refresh}
            disabled={refreshing}
            className="rounded border border-gray-200 px-2 py-0.5 text-[11px] text-gray-500 hover:border-gray-300 hover:text-gray-900 disabled:opacity-50 dark:border-white/10 dark:text-gray-400 dark:hover:border-white/20 dark:hover:text-gray-100"
          >
            {refreshing ? 'Refreshing…' : 'Refresh'}
          </button>
        </TabPanel>
      );
    }
    return (
      <TabPanel idPrefix={idPrefix} tab="usage">
        <div className="flex items-center justify-between gap-2">
          <h4 className="text-[11px] font-medium uppercase tracking-wide text-gray-500 dark:text-gray-400">
            Turn {turnNo} usage
          </h4>
          <button
            type="button"
            onClick={refresh}
            disabled={refreshing}
            className="rounded border border-gray-200 px-2 py-0.5 text-[11px] text-gray-500 hover:border-gray-300 hover:text-gray-900 disabled:opacity-50 dark:border-white/10 dark:text-gray-400 dark:hover:border-white/20 dark:hover:text-gray-100"
          >
            {refreshing ? 'Refreshing…' : 'Refresh'}
          </button>
        </div>
        {error ? <p className="text-[11px] text-red-600 dark:text-red-400">{error}</p> : null}
        <UsageTotals usage={usage} />
        <DetailSection title="Reported by the runtime">
          <DetailRows>
            <DetailRow label="Input tokens">{recordedCount(usage.runtime.input_tokens)}</DetailRow>
            <DetailRow label="Output tokens">
              {recordedCount(usage.runtime.output_tokens)}
            </DetailRow>
            <DetailRow label="Cache read">
              {recordedCount(usage.runtime.cache_read_tokens)}
            </DetailRow>
          </DetailRows>
          <p className="text-[11px] text-gray-400 dark:text-gray-500">
            The runtime counters are the harness&rsquo; own report and can differ from the Gateway
            ledger above.
          </p>
        </DetailSection>
        {usage.ledger.attempts.length ? (
          <DetailSection title="By attempt">
            <ul className="space-y-2">
              {usage.ledger.attempts.map((attempt) => (
                <li
                  key={attempt.attempt_id}
                  className="rounded border border-gray-200 px-2.5 py-2 dark:border-white/10"
                >
                  <p className="text-[12px] font-medium text-gray-800 dark:text-gray-200">
                    Attempt {attempt.attempt_no} · {LEDGER_LABEL[attempt.status]}
                  </p>
                  <p className="mt-0.5 text-[11px] text-gray-500 dark:text-gray-400">
                    {recordedCount(attempt.totals?.calls ?? null)} calls ·{' '}
                    {recordedCount(attempt.totals?.tokens_in ?? null)} in ·{' '}
                    {recordedCount(attempt.totals?.tokens_out ?? null)} out ·{' '}
                    {attempt.totals?.spent_usd
                      ? `$${attempt.totals.spent_usd}`
                      : 'cost not recorded'}
                  </p>
                  {attempt.reason ? (
                    <p className="mt-0.5 text-[11px] text-gray-400 dark:text-gray-500">
                      {attempt.reason}
                    </p>
                  ) : null}
                </li>
              ))}
            </ul>
          </DetailSection>
        ) : null}
      </TabPanel>
    );
  }

  if (tab === 'raw') {
    const log = runEvents.map((event) => JSON.stringify(event)).join('\n');
    return (
      <TabPanel idPrefix={idPrefix} tab="raw">
        <div className="flex items-center justify-between gap-2">
          <p className="text-gray-500 dark:text-gray-400">
            {runEvents.length} stored event{runEvents.length === 1 ? '' : 's'} for Turn {turnNo},
            grouped by attempt.
          </p>
          <DownloadButton text={log} filename={`run-turn-${turnNo}-events.ndjson`} />
        </div>
        {attempts.length === 0 ? (
          <EmptyNote>This turn stored no events.</EmptyNote>
        ) : (
          attempts.map(([attemptId, list]) => (
            <DetailSection key={attemptId} title={`Attempt ${attemptId} · ${list.length} events`}>
              <div className="space-y-1.5">
                {list.map((event) => (
                  <StoredRecord
                    key={event.id}
                    title={`#${event.id} · ${event.event_type} · seq ${event.seq}`}
                    event={event}
                  />
                ))}
              </div>
            </DetailSection>
          ))
        )}
        <p className="text-[11px] text-gray-400 dark:text-gray-500">
          This export is the run log for Turn {turnNo} only. The Session log button on the
          trajectory list exports the whole conversation.
        </p>
      </TabPanel>
    );
  }

  return (
    <TabPanel idPrefix={idPrefix} tab="overview">
      <DetailRows>
        <DetailRow
          label="Status"
          tone={state === 'failed' ? 'error' : state === 'succeeded' ? 'default' : 'muted'}
        >
          {state ? STATE_LABEL[state] : 'Not recorded'}
        </DetailRow>
        <DetailRow label="Turn">{turnNo}</DetailRow>
        <DetailRow label="Started">{recordedAt(startedAt)}</DetailRow>
        <DetailRow label="Finished">{recordedAt(finishedAt)}</DetailRow>
        <DetailRow label="Elapsed">{elapsedLabel(startedAt, finishedAt)}</DetailRow>
        <DetailRow label="Runtime">{job?.runtime ?? fallback?.runtime ?? 'Not recorded'}</DetailRow>
        <DetailRow label="Model">{job?.model ?? fallback?.model ?? 'Not recorded'}</DetailRow>
        <DetailRow label="Reasoning effort">
          {job?.reasoning_effort ?? fallback?.reasoningEffort ?? 'Auto'}
        </DetailRow>
      </DetailRows>

      {attemptRows.length ? (
        <DetailSection title="Attempts">
          <ul className="space-y-1">
            {attemptRows.map((attempt) => (
              <li
                key={attempt.attempt_id}
                className="flex flex-wrap items-baseline gap-x-3 gap-y-0.5 rounded border border-gray-200 px-2.5 py-1.5 text-[12px] dark:border-white/10"
              >
                <span className="font-medium text-gray-800 dark:text-gray-200">
                  Attempt {attempt.attempt_no}
                </span>
                <span className="text-gray-500 dark:text-gray-400">
                  {attempt.continuity_mode ?? 'continuity not recorded'}
                </span>
                <span className="text-gray-500 dark:text-gray-400">
                  {attempt.context_status ?? 'context not recorded'}
                </span>
                <span className="ml-auto tabular-nums text-gray-500 dark:text-gray-400">
                  {formatDuration(attempt.duration_ms) ?? 'duration not recorded'}
                </span>
              </li>
            ))}
          </ul>
        </DetailSection>
      ) : null}

      <DetailSection title="Workspace">
        <button
          type="button"
          onClick={onOpenGitDiff}
          className="rounded border border-gray-200 px-2 py-1 text-[11px] text-gray-600 transition-colors hover:border-gray-300 hover:text-gray-900 dark:border-white/10 dark:text-gray-300 dark:hover:border-white/20 dark:hover:text-gray-50"
        >
          Open current Git diff
        </button>
        <p className="text-[11px] text-gray-400 dark:text-gray-500">
          The workspace diff is the current shared workspace, not this turn&rsquo;s changes.
        </p>
        {openUrl ? (
          <a
            href={openUrl}
            target="_blank"
            rel="noopener noreferrer"
            className="text-[11px] font-medium text-crimson hover:underline dark:text-crimson-light"
          >
            Open draft pull request
          </a>
        ) : null}
      </DetailSection>
    </TabPanel>
  );
}
