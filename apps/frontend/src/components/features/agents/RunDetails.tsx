'use client';

// Run details, scoped to one turn.
//
// The previous drawer described "the run" while the page could move on to a
// later turn, so its numbers silently changed meaning. This one is opened from
// a specific turn and reads that job only: its own status, its own attempts,
// its own ledger snapshot.

import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { useRouter } from 'next/navigation';
import { useT } from '@/components/providers';
import type { TranslationKey } from '@/lib/i18n/dictionaries/en';

import {
  createAgentJob,
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
import { isOlderUsage, onAgentRecordsChanged } from './agentPush';
import { isRepositoryTooLarge, ownerFailureMessage } from './adapt';
import { hasLostContext, ownerRunNote } from './conversationJob';

import type { RunTab } from './trajectoryInspectorTabs';

export { RUN_TABS, type RunTab } from './trajectoryInspectorTabs';

const STATE_LABEL: Record<AgentJobApiState, TranslationKey> = {
  queued: 'turnNavigation.state.queued',
  running: 'turnNavigation.state.running',
  publishing: 'runDetails.publishing',
  succeeded: 'turnNavigation.state.needsReview',
  failed: 'turnNavigation.state.failed',
  cancelled: 'turnNavigation.state.cancelled',
};

const FALLBACK_STATE: Record<AgentJob['state'], AgentJobApiState> = {
  queued: 'queued',
  running: 'running',
  done: 'succeeded',
  needs_review: 'succeeded',
  failed: 'failed',
  cancelled: 'cancelled',
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
 * is unknown, the reader asked for a refresh, or the ledger has work queued or
 * scheduled. A settled snapshot with nothing pending is reused, so opening the
 * panel repeatedly is free.
 */
const usageCache = new Map<string, AgentJobUsageApi | null>();

/**
 * Cache `next` for a job unless what is cached is newer, and return what is
 * kept. Every write goes through here: a read, a refresh's answer and a push
 * can arrive in any order, and their versions say which is newer.
 */
function rememberUsage(jobId: string, next: AgentJobUsageApi | null): AgentJobUsageApi | null {
  const known = usageCache.get(jobId);
  const kept = next && known && isOlderUsage(next, known) ? known : next;
  usageCache.set(jobId, kept);
  return kept;
}

/** How long to wait before reading a ledger whose work has not finished. */
const LEDGER_POLL_MS = 15_000;
/** Floor for an overdue read, so a timestamp in the past cannot spin. */
const LEDGER_POLL_FLOOR_MS = 1_000;
/**
 * Reads to spend retrying work that should already have finished, and separate
 * scheduled reads to honor.
 *
 * The two are not the same kind of wait. A retry is a guess that the worker is
 * late; a schedule is the server naming the time it will have an answer, and a
 * terminal turn's schedule runs half a minute and then five minutes after it
 * finished. Spending one budget on both means polling towards a five-minute
 * refresh until the budget runs out before the answer exists.
 */
const MAX_LEDGER_RETRIES = 8;
const MAX_LEDGER_WAITS = 8;

/** A read the panel still owes this ledger, and what it is waiting for. */
interface LedgerRead {
  delayMs: number;
  /** True for a retry of overdue work, false for a schedule to wait out. */
  retry: boolean;
}

/**
 * When this ledger should be read again, or null when it has settled.
 *
 * A refresh request only queues the work, and the answer to that request is the
 * snapshot from before the worker ran, so a read after it is what turns the
 * queued answer into the finished one. The server also schedules its own
 * refreshes: an `available` snapshot whose `next_refresh_at` is still ahead is
 * going to change, and stopping at it is what left a stale total in the cache
 * for the next open.
 */
function ledgerNextRead(
  usage: AgentJobUsageApi | null | undefined,
  now: number,
): LedgerRead | null {
  if (!usage) return null;
  if (usage.ledger.refreshing || usage.ledger.status === 'pending') {
    return { delayMs: LEDGER_POLL_MS, retry: true };
  }
  if (!usage.ledger.next_refresh_at) return null;
  const due = Date.parse(usage.ledger.next_refresh_at);
  if (!Number.isFinite(due)) return null;
  // A schedule ahead is worth waiting out exactly: the answer does not exist
  // before it, so reading earlier only spends budget on the same snapshot.
  if (due > now) return { delayMs: due - now, retry: false };
  // Overdue, so the worker is late: read now, counted as a retry so a server
  // that keeps reporting an overdue schedule cannot spin.
  return { delayMs: LEDGER_POLL_FLOOR_MS, retry: true };
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
  const retries = useRef(0);
  const waits = useRef(0);
  const currentJob = useRef(jobId);
  // Raised by each usage pushed for this job without a version: a read that
  // left before it is taken to be older, and is dropped.
  const pushes = useRef(0);

  useEffect(() => {
    if (currentJob.current !== jobId) {
      currentJob.current = jobId;
      requested.current = false;
      retries.current = 0;
      waits.current = 0;
    }
    const known = usageCache.get(jobId);
    const mustRead =
      requested.current || known === undefined || ledgerNextRead(known, Date.now()) !== null;
    requested.current = false;
    setUsage(known ?? null);
    setLoaded(known !== undefined);
    if (!mustRead) return undefined;
    const controller = new AbortController();
    let timer: ReturnType<typeof setTimeout> | undefined;
    const pushesBefore = pushes.current;
    getAgentJobUsage(jobId, controller.signal)
      .then((read) => {
        if (controller.signal.aborted || pushes.current !== pushesBefore) return;
        // A usage pushed while this read was on its way may be the newer one.
        const result = rememberUsage(jobId, read);
        setUsage(result);
        setError(null);
        const next = ledgerNextRead(result, Date.now());
        const budget = next?.retry
          ? retries.current < MAX_LEDGER_RETRIES
          : waits.current < MAX_LEDGER_WAITS;
        if (next && budget) {
          if (next.retry) retries.current += 1;
          else waits.current += 1;
          timer = setTimeout(() => setRead((value) => value + 1), next.delayMs);
        }
      })
      .catch((cause: unknown) => {
        if (!controller.signal.aborted && pushes.current === pushesBefore) {
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

  // A pushed change to this job's usage is shown as sent, or read at once when
  // it came without a value, outside the budget above: it is the server saying
  // there is something new, not a guess.
  useEffect(
    () =>
      onAgentRecordsChanged((change) => {
        const pushed = change.usageValues[jobId];
        if (pushed && !change.resync) {
          if (!pushed.version) pushes.current += 1;
          // A read that left after this was computed may already show more.
          setUsage(rememberUsage(jobId, pushed));
          setError(null);
          setLoaded(true);
        } else if (change.resync || change.usage.includes(jobId)) {
          requested.current = true;
          setRead((value) => value + 1);
        }
      }),
    [jobId],
  );

  const refresh = useCallback(() => {
    setRefreshing(true);
    // A reader who asks for fresh accounting restarts the panel's own budget.
    retries.current = 0;
    waits.current = 0;
    refreshAgentJobUsage(jobId)
      .then((result) => {
        // The answer is cached usage as it was; a push may already be newer.
        const kept = rememberUsage(jobId, result);
        if (currentJob.current === jobId) setUsage(kept);
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
  const t = useT();
  const router = useRouter();
  const idPrefix = `run-${jobId}`;
  const { usage, loaded, refreshing, error, refresh } = useJobUsage(jobId);
  const [restarting, setRestarting] = useState(false);
  const [restartError, setRestartError] = useState<string | null>(null);

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

  // The loaded conversation can describe a different turn while a historical
  // row is unavailable. It must never supply that turn's status or metadata.
  const turnFallback = !job && fallback?.id === jobId ? fallback : null;
  const state = job?.state ?? (turnFallback ? FALLBACK_STATE[turnFallback.state] : null);
  const startedAt = job ? (job.started_at ?? null) : (turnFallback?.startedAt ?? null);
  const finishedAt = job ? (job.finished_at ?? null) : (turnFallback?.finishedAt ?? null);
  const attemptRows = job?.attempt_telemetry ?? [];
  const openUrl = job ? job.published_pr_url : (turnFallback?.prUrl ?? null);
  const contextLost = job
    ? job.native_state === 'lost' || job.context_status === 'lost'
    : Boolean(turnFallback && hasLostContext(turnFallback));
  const failedOrStopped = state === 'failed' || state === 'cancelled';
  const failureNote = failedOrStopped
    ? job?.detail
      ? ownerFailureMessage(job.detail)
      : turnFallback
        ? ownerRunNote(turnFallback)
        : state === 'failed'
          ? t('runDetails.failureMissing')
          : undefined
    : undefined;
  const guidance = contextLost
    ? t('runDetails.contextLost')
    : state === 'failed'
      ? isRepositoryTooLarge(job?.detail) || turnFallback?.failureCause === 'repository_too_large'
        ? t('runDetails.repositoryLimit')
        : /^Site misconfigured:/i.test(job?.detail ?? '') ||
            turnFallback?.failureCause === 'site_misconfigured'
          ? t('runDetails.siteMisconfigured')
          : t('runDetails.followUp')
      : state === 'cancelled'
        ? t('runDetails.stoppedFollowUp')
        : null;

  // "Restart in a new session" re-issues the same task (repo + prompt) as a
  // fresh job/thread, so it is offered only for a failed run and only when the
  // turn exposed enough to recreate it. A cancelled run is deliberately not
  // offered: its follow-up continues from saved intermediate changes instead.
  const repo = job?.repo ?? turnFallback?.repo;
  const prompt = (job?.task_prompt ?? turnFallback?.prompt ?? '').trim();
  const model = job?.model ?? turnFallback?.model;
  const restartable = state === 'failed' && Boolean(repo && prompt && model) && !restarting;

  async function restartInNewSession() {
    if (!repo || !prompt || !model) return;
    setRestarting(true);
    setRestartError(null);
    try {
      const restart = await createAgentJob({
        repo,
        task_prompt: prompt,
        model,
        ...((job?.reasoning_effort ?? turnFallback?.reasoningEffort)
          ? { reasoning_effort: job?.reasoning_effort ?? turnFallback?.reasoningEffort }
          : {}),
      });
      router.push(`/${restart.id}`);
    } catch (cause: unknown) {
      setRestarting(false);
      setRestartError(
        cause instanceof Error ? cause.message : 'Could not restart in a new session',
      );
    }
  }

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
          This export covers Turn {turnNo} only. The Session log button on the trajectory list
          exports the whole conversation.
        </p>
      </TabPanel>
    );
  }

  return (
    <TabPanel idPrefix={idPrefix} tab="overview">
      <section
        aria-label={t('runDetails.summary', { number: turnNo })}
        className={`rounded-xl border p-4 ${
          state === 'failed'
            ? 'border-red-200 bg-red-50/60 dark:border-red-500/25 dark:bg-red-500/5'
            : 'border-gray-200 bg-gray-50 dark:border-white/10 dark:bg-white/5'
        }`}
      >
        <div className="flex flex-wrap items-start justify-between gap-3">
          <div>
            <p className="text-xs text-gray-500 dark:text-gray-400">
              {t('turnNavigation.turn', { turn: turnNo })}
            </p>
            <h3
              className={`mt-1 text-lg font-semibold ${state === 'failed' ? 'text-red-800 dark:text-red-300' : 'text-gray-900 dark:text-gray-100'}`}
            >
              {state ? t(STATE_LABEL[state]) : t('runDetails.notRecorded')}
            </h3>
          </div>
          <div className="text-right">
            <p className="text-xs text-gray-500 dark:text-gray-400">{t('runDetails.elapsed')}</p>
            <p className="mt-1 text-sm font-medium tabular-nums text-gray-800 dark:text-gray-200">
              {elapsedLabel(startedAt, finishedAt)}
            </p>
          </div>
        </div>
        {failureNote ? (
          <p className="mt-3 text-sm leading-relaxed text-gray-800 [overflow-wrap:anywhere] dark:text-gray-200">
            {failureNote}
          </p>
        ) : null}
        {guidance ? (
          <p className="mt-2 text-xs leading-relaxed text-gray-600 dark:text-gray-300">
            {guidance}
          </p>
        ) : null}
        {restartable ? (
          <div className="mt-4">
            {restartError ? (
              <p className="text-[11px] text-red-600 dark:text-red-400">{restartError}</p>
            ) : null}
            <button
              type="button"
              onClick={restartInNewSession}
              disabled={restarting}
              className="ui-focus-ring inline-flex min-h-9 items-center rounded-lg border border-red-200 bg-white px-3 py-2 text-xs font-medium text-red-700 transition-colors hover:border-red-300 hover:bg-red-50 disabled:opacity-60 dark:border-red-500/30 dark:bg-transparent dark:text-red-300 dark:hover:border-red-500/50 dark:hover:bg-red-500/10"
            >
              {restarting ? t('runDetails.restarting') : t('runDetails.restart')}
            </button>
          </div>
        ) : null}
      </section>
      <DetailRows>
        <DetailRow label="Started">{recordedAt(startedAt)}</DetailRow>
        <DetailRow label="Finished">{recordedAt(finishedAt)}</DetailRow>
        <DetailRow label="Runtime">
          {job?.runtime ?? turnFallback?.runtime ?? 'Not recorded'}
        </DetailRow>
        <DetailRow label="Model">{job?.model ?? turnFallback?.model ?? 'Not recorded'}</DetailRow>
        <DetailRow label="Reasoning effort">
          {job?.reasoning_effort ?? turnFallback?.reasoningEffort ?? 'Auto'}
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
          className="ui-focus-ring inline-flex min-h-9 items-center rounded-lg border border-gray-200 px-3 py-2 text-xs font-medium text-gray-600 transition-colors hover:border-gray-300 hover:text-gray-900 dark:border-white/10 dark:text-gray-300 dark:hover:border-white/20 dark:hover:text-gray-50"
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
