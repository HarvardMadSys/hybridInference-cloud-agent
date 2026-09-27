'use client';

import Link from 'next/link';
import { useEffect, useRef, useState } from 'react';

import {
  getAdminSteps,
  type AdminRunFilters,
  type AdminStepsPage,
  type AdminStepTurn,
} from '@/lib/api/adminRuns';
import { getErrorMessage } from '@/lib/utils/errors';

import { cacheHitRate, durationLabel, modelLabel } from './LLMRequests';
import { accessDenied, BUTTON, CARD, dateTime, ErrorNotice, money, number } from './shared';

const UNAVAILABLE = '—';

/** Why a turn's requests could not be read, as the effect on this list. */
const UNREAD_REASONS: Record<string, string> = {
  attribution_unavailable: 'its requests were never attributed to a gateway ledger',
  binding_changed: 'its requests were attributed by a different gateway installation',
  invalid_window: 'its requests have no usable attribution window yet',
  gateway_unavailable: 'the gateway could not be read; refresh to try again',
};

/** A measured count, or a dash: a missing count is not a zero. */
function tokens(value: number | null): string {
  return typeof value === 'number' && Number.isFinite(value) && value >= 0
    ? number(value)
    : UNAVAILABLE;
}

function unreadReason(reason: string | null): string {
  return (reason && UNREAD_REASONS[reason]) ?? 'its requests could not be read';
}

/**
 * The steps level of the Sessions tab: every model request of the turns in
 * the selection, newest first.
 *
 * The gateway lists requests one run at a time, so a page reads the next few
 * turns and lists their requests together; "Load more" reads older turns. A
 * turn whose requests were not all read is named above the table, so a short
 * list never reads as fewer calls.
 */
export function StepResults({
  filters,
  onLoading,
  onDenied,
}: {
  filters: AdminRunFilters;
  onLoading: (loading: boolean) => void;
  onDenied: () => void;
}) {
  const [page, setPage] = useState<AdminStepsPage | null>(null);
  const [loading, setLoading] = useState(true);
  const [moreLoading, setMoreLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [revision, setRevision] = useState(0);
  const moreRequest = useRef<AbortController | null>(null);

  useEffect(() => onLoading(loading), [loading, onLoading]);

  useEffect(() => {
    const controller = new AbortController();
    moreRequest.current?.abort();
    setMoreLoading(false);
    setPage(null);
    setLoading(true);
    setError(null);
    getAdminSteps(filters, controller.signal)
      .then((next) => {
        if (!controller.signal.aborted) setPage(next);
      })
      .catch((e: unknown) => {
        if (!controller.signal.aborted) {
          if (accessDenied(e)) onDenied();
          else setError(getErrorMessage(e));
        }
      })
      .finally(() => {
        if (!controller.signal.aborted) setLoading(false);
      });
    return () => {
      controller.abort();
      moreRequest.current?.abort();
    };
  }, [filters, revision, onDenied]);

  async function loadMore() {
    if (!page?.next_cursor || moreLoading) return;
    moreRequest.current?.abort();
    const controller = new AbortController();
    moreRequest.current = controller;
    setMoreLoading(true);
    setError(null);
    try {
      const next = await getAdminSteps(
        { ...filters, since: page.since, until: page.until, cursor: page.next_cursor },
        controller.signal,
      );
      if (!controller.signal.aborted)
        setPage((previous) => (previous ? appendPage(previous, next) : next));
    } catch (e) {
      if (!controller.signal.aborted) {
        if (accessDenied(e)) onDenied();
        else setError(getErrorMessage(e));
      }
    } finally {
      if (!controller.signal.aborted) setMoreLoading(false);
    }
  }

  const turns = new Map((page?.turns ?? []).map((turn) => [turn.job_id, turn]));
  const unread = (page?.turns ?? []).filter(
    (turn) => turn.coverage === 'partial' || turn.coverage === 'unavailable',
  );
  return (
    <>
      {error && (
        <ErrorNotice>
          {error}
          {!page && (
            <button
              type="button"
              className="ml-3 underline"
              onClick={() => setRevision((value) => value + 1)}
            >
              Retry
            </button>
          )}
        </ErrorNotice>
      )}
      {loading ? (
        <p role="status" className="mt-8 text-sm text-gray-500 dark:text-gray-400">
          Loading steps…
        </p>
      ) : (
        page && (
          <>
            <p className="mt-6 text-xs text-gray-500 dark:text-gray-400">
              Turns created {dateTime(page.since)} – {dateTime(page.until)} · requests of the{' '}
              {page.turns.length === 1 ? 'newest turn' : `newest ${page.turns.length} turns`} so far
            </p>
            {unread.length > 0 && (
              <ul className="mt-2 space-y-1 text-xs text-amber-700 dark:text-amber-400">
                {unread.map((turn) => (
                  <li key={turn.job_id}>
                    <TurnLink turn={turn} />{' '}
                    {turn.coverage === 'partial'
                      ? 'made more requests than a page lists; its own page lists all of them.'
                      : `is not listed: ${unreadReason(turn.reason)}.`}
                  </li>
                ))}
              </ul>
            )}
            {page.turns.length === 0 ? (
              <p className="mt-8 text-sm text-gray-500 dark:text-gray-400">
                No turns match these filters.
              </p>
            ) : page.steps.length === 0 ? (
              <p className="mt-8 text-sm text-gray-500 dark:text-gray-400">
                These turns have no recorded model requests.
              </p>
            ) : (
              <StepsTable page={page} turns={turns} />
            )}
            {page.next_cursor && (
              <div className="mt-5 flex justify-center">
                <button
                  type="button"
                  className={BUTTON}
                  disabled={moreLoading}
                  onClick={() => void loadMore()}
                >
                  {moreLoading ? 'Loading…' : 'Load more steps'}
                </button>
              </div>
            )}
          </>
        )
      )}
    </>
  );
}

/** Older turns follow the ones on screen, so their requests go after them. */
function appendPage(previous: AdminStepsPage, next: AdminStepsPage): AdminStepsPage {
  return {
    ...next,
    turns: [...previous.turns, ...next.turns],
    steps: [...previous.steps, ...next.steps],
  };
}

function TurnLink({ turn }: { turn: AdminStepTurn }) {
  return (
    <Link
      href={`/admin/runs/${encodeURIComponent(turn.job_id)}`}
      className="font-medium text-crimson hover:underline dark:text-crimson-light"
    >
      Turn {turn.turn_no}
    </Link>
  );
}

function StepsTable({ page, turns }: { page: AdminStepsPage; turns: Map<string, AdminStepTurn> }) {
  return (
    <div className={`${CARD} mt-4 overflow-x-auto`}>
      <table aria-label="Steps" className="w-full min-w-[72rem] text-left text-sm tabular-nums">
        <thead className="border-b border-gray-100 bg-gray-50/70 text-xs uppercase text-gray-500 dark:border-white/5 dark:bg-white/[0.02] dark:text-gray-400">
          <tr className="[&>th]:whitespace-nowrap [&>th]:px-4 [&>th]:py-3 [&>th]:font-medium">
            <th scope="col" title="When the request started, in UTC">
              Time
            </th>
            <th scope="col">Turn</th>
            <th scope="col">User</th>
            <th scope="col">Model</th>
            <th scope="col" className="text-right">
              HTTP
            </th>
            <th scope="col" className="text-right" title="Input tokens, including cache">
              Input
            </th>
            <th scope="col" className="text-right">
              Output
            </th>
            <th
              scope="col"
              className="text-right"
              title="Cache read tokens / input tokens (including cache)"
            >
              Cache hit rate
            </th>
            <th scope="col" className="text-right" title="Time to first token">
              TTFT
            </th>
            <th scope="col" className="text-right">
              Duration
            </th>
            <th scope="col" className="text-right">
              Cost
            </th>
          </tr>
        </thead>
        <tbody className="divide-y divide-gray-100 dark:divide-white/5">
          {page.steps.map((step) => {
            const turn = turns.get(step.job_id);
            const model = modelLabel(step);
            const hitRate = cacheHitRate(step);
            const amount = money(step.spent_usd);
            return (
              <tr
                key={`${step.job_id}:${step.request_id}`}
                className="align-top hover:bg-gray-50/70 dark:hover:bg-white/[0.03] [&>td]:px-4 [&>td]:py-3"
              >
                <td className="whitespace-nowrap text-xs text-gray-600 dark:text-gray-300">
                  <time dateTime={step.request_started_at} title={step.request_id}>
                    {dateTime(step.request_started_at)}
                  </time>
                </td>
                <td className="max-w-64">
                  {turn ? (
                    <>
                      <TurnLink turn={turn} />
                      <span
                        className="mt-1 block truncate text-xs text-gray-500 dark:text-gray-400"
                        title={turn.repo}
                      >
                        {turn.repo}
                      </span>
                    </>
                  ) : (
                    UNAVAILABLE
                  )}
                </td>
                <td className="max-w-40">
                  <span
                    className="block truncate font-mono text-xs text-gray-600 dark:text-gray-300"
                    title={turn?.user_id}
                  >
                    {turn?.user_id ?? UNAVAILABLE}
                  </span>
                </td>
                <td className="max-w-48">
                  <span className="block truncate" title={model.title ?? model.text}>
                    {model.text}
                  </span>
                </td>
                <td className="text-right">{step.status_code ?? UNAVAILABLE}</td>
                <td className="text-right">{tokens(step.tokens_in)}</td>
                <td className="text-right">{tokens(step.tokens_out)}</td>
                <td className="text-right">
                  {hitRate === null ? UNAVAILABLE : `${hitRate.toFixed(1)}%`}
                </td>
                <td className="whitespace-nowrap text-right">{durationLabel(step.ttft_ms)}</td>
                <td className="whitespace-nowrap text-right">{durationLabel(step.latency_ms)}</td>
                <td className="text-right" title={amount.exact ?? undefined}>
                  {amount.exact === null ? UNAVAILABLE : amount.display}
                </td>
              </tr>
            );
          })}
        </tbody>
      </table>
    </div>
  );
}
