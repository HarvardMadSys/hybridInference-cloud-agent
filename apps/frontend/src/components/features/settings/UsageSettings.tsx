'use client';

import Link from 'next/link';
import { useCallback, useEffect, useRef, useState } from 'react';
import { useLocale } from '@/components/providers';
import type { TranslationKey } from '@/lib/i18n/translate';
import {
  getOwnerContainers,
  type ContainerState,
  type OwnerContainer,
  type OwnerContainerList,
  type UsageRangePreset,
} from '@/lib/api/ownerUsage';
import { ContextInsights } from './ContextInsights';
import { UsageHistory } from './UsageHistory';
import { Card, LoadError, Tile, errorText } from './UsageParts';
import { STATE_ORDER, decimal, formatMemory, relativeTime } from './usagePresent';

/** Hosts sample every 15 seconds; reading faster shows nothing new. */
export const CONTAINER_REFRESH_MS = 15_000;
const PAGE = 50;
const MAX_PAGE = 100;

const DOT: Record<ContainerState, string> = {
  running: 'bg-status-success',
  starting: 'bg-status-warning',
  stopping: 'bg-status-warning',
  stopped: 'bg-muted',
  stored: 'bg-muted',
  deleting: 'bg-muted',
  unavailable: 'bg-status-error',
};

const STATE_LABEL: Record<ContainerState, TranslationKey> = {
  running: 'settings.usage.state.running',
  starting: 'settings.usage.state.starting',
  stopping: 'settings.usage.state.stopping',
  stopped: 'settings.usage.state.stopped',
  stored: 'settings.usage.state.stored',
  deleting: 'settings.usage.state.deleting',
  unavailable: 'settings.usage.state.unavailable',
};

const STATE_COUNT: Record<ContainerState, TranslationKey> = {
  running: 'settings.usage.stateCount.running',
  starting: 'settings.usage.stateCount.starting',
  stopping: 'settings.usage.stateCount.stopping',
  stopped: 'settings.usage.stateCount.stopped',
  stored: 'settings.usage.stateCount.stored',
  deleting: 'settings.usage.stateCount.deleting',
  unavailable: 'settings.usage.stateCount.unavailable',
};

/**
 * Your containers now, what your work used over a range, and how large each
 * conversation's context got over the same range.
 */
export function UsageSettings() {
  const [range, setRange] = useState<UsageRangePreset>('7d');
  return (
    <>
      <Containers />
      <UsageHistory range={range} onRangeChange={setRange} />
      <ContextInsights range={range} onRangeChange={setRange} />
    </>
  );
}

function Containers() {
  const { t, locale } = useLocale();
  const [list, setList] = useState<OwnerContainerList | null>(null);
  const [containers, setContainers] = useState<OwnerContainer[]>([]);
  const [cursor, setCursor] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loadingMore, setLoadingMore] = useState(false);
  const [nowMs, setNowMs] = useState(() => Date.now());
  // Only the most recently sent read may land: a refresh sent before "Show
  // more" must not replace the longer list that followed it.
  const latest = useRef(0);
  const shown = useRef(PAGE);

  const refresh = useCallback(
    async ({ quiet = false }: { quiet?: boolean } = {}) => {
      const request = ++latest.current;
      try {
        const page = await getOwnerContainers({
          limit: Math.min(MAX_PAGE, Math.max(PAGE, shown.current)),
        });
        // One read returns at most MAX_PAGE rows. Past that, read on until
        // every row "Show more" added is read again, or the refresh shortens
        // the list.
        let rows = page.containers;
        let next = page.next_cursor;
        while (
          next &&
          shown.current > MAX_PAGE &&
          rows.length < shown.current &&
          request === latest.current
        ) {
          const more = await getOwnerContainers({
            cursor: next,
            limit: Math.min(MAX_PAGE, shown.current - rows.length),
          });
          rows = [...rows, ...more.containers];
          next = more.next_cursor;
        }
        if (request !== latest.current) return;
        setList(page);
        setContainers(rows);
        setCursor(next);
        setNowMs(Date.now());
        setError(null);
      } catch (cause) {
        if (request === latest.current && !quiet) {
          setError(errorText(cause, t));
        }
      }
    },
    [t],
  );

  useEffect(() => {
    void refresh();
    const timer = window.setInterval(() => {
      if (document.visibilityState === 'visible') void refresh({ quiet: true });
    }, CONTAINER_REFRESH_MS);
    return () => window.clearInterval(timer);
  }, [refresh]);

  async function showMore() {
    if (!cursor || loadingMore) return;
    setLoadingMore(true);
    const request = ++latest.current;
    try {
      const page = await getOwnerContainers({ cursor, limit: PAGE });
      if (request !== latest.current) return;
      setContainers((current) => {
        const next = [...current, ...page.containers];
        shown.current = next.length;
        return next;
      });
      setCursor(page.next_cursor);
    } catch (cause) {
      if (request === latest.current) setError(errorText(cause, t));
    } finally {
      setLoadingMore(false);
    }
  }

  const title = t('settings.usage.containers');
  if (!list) {
    return (
      <Card id="settings-usage-containers" title={title}>
        {error ? (
          <LoadError message={error} onRetry={() => void refresh()} />
        ) : (
          <div
            className="mt-3 h-24 animate-pulse rounded-lg bg-surface-muted"
            aria-label={t('settings.usage.loading')}
          />
        )}
      </Card>
    );
  }

  const states = STATE_ORDER.filter((state) => (list.counts[state] ?? 0) > 0);
  const { current } = list;
  return (
    <Card id="settings-usage-containers" title={title}>
      <dl className="mt-3 grid grid-cols-1 gap-2 sm:grid-cols-3">
        <Tile
          label={t('settings.usage.total')}
          value={list.total_count.toLocaleString(locale)}
          detail={states
            .map((state) => t(STATE_COUNT[state], { count: list.counts[state] ?? 0 }))
            .join(' · ')}
        />
        <Tile
          label={t('settings.usage.cpuNow')}
          value={t('settings.usage.cores', { value: decimal(current.cpu_cores) })}
        />
        <Tile label={t('settings.usage.memoryNow')} value={formatMemory(current.memory_bytes)} />
      </dl>
      {current.unmeasured > 0 ? (
        <p className="mt-2 text-xs text-muted">
          {t('settings.usage.unmeasured', { count: current.unmeasured })}
        </p>
      ) : null}
      {error ? <LoadError message={error} onRetry={() => void refresh()} /> : null}
      {containers.length === 0 ? (
        <p className="mt-3 text-sm text-muted">{t('settings.usage.empty')}</p>
      ) : (
        <ul className="mt-3 divide-y divide-border rounded-lg border border-border">
          {containers.map((container) => (
            <ContainerRow key={container.id} container={container} nowMs={nowMs} />
          ))}
        </ul>
      )}
      {cursor ? (
        <button
          type="button"
          onClick={() => void showMore()}
          disabled={loadingMore}
          className="ui-button ui-button-secondary mt-3 min-h-11 disabled:opacity-60"
        >
          {t(loadingMore ? 'settings.usage.loadingMore' : 'settings.usage.showMore', {
            count: list.total_count - containers.length,
          })}
        </button>
      ) : null}
    </Card>
  );
}

function ContainerRow({ container, nowMs }: { container: OwnerContainer; nowMs: number }) {
  const { t, locale } = useLocale();
  const [first, ...rest] = container.chats;
  const more = container.chat_count - 1;
  const reading = container.reading && !container.reading.stale ? container.reading : null;
  const cpuLimit = container.cpu_limit_cores === null ? '—' : decimal(container.cpu_limit_cores);
  const memoryLimit = formatMemory(container.memory_limit_bytes);
  let readingText = t('settings.usage.noReading');
  if (reading) {
    readingText = t('settings.usage.reading', {
      cpu: reading.cpu_cores === null ? '—' : decimal(reading.cpu_cores),
      cpuLimit,
      memory: formatMemory(reading.memory_bytes),
      memoryLimit,
    });
  }
  return (
    <li className="flex flex-col gap-1.5 px-3 py-2.5 text-sm sm:flex-row sm:items-start sm:justify-between sm:gap-4">
      <div className="min-w-0">
        <div className="flex min-w-0 flex-wrap items-center gap-x-2 gap-y-1">
          <span className="inline-flex shrink-0 items-center gap-1.5 text-xs font-medium text-muted">
            <span aria-hidden="true" className={`h-2 w-2 rounded-full ${DOT[container.state]}`} />
            {t(STATE_LABEL[container.state])}
          </span>
          {first ? (
            first.job_id ? (
              <Link
                href={`/${encodeURIComponent(first.job_id)}`}
                className="ui-focus-ring min-w-0 truncate rounded font-medium text-foreground hover:underline"
              >
                {first.title || t('settings.usage.untitled')}
              </Link>
            ) : (
              <span className="min-w-0 truncate font-medium text-foreground">
                {first.title || t('settings.usage.untitled')}
              </span>
            )
          ) : null}
          {first?.archived ? (
            <span className="rounded border border-border px-1.5 text-[11px] text-muted">
              {t('settings.usage.archived')}
            </span>
          ) : null}
          {more > 0 ? (
            <span
              className="text-xs text-muted"
              title={rest.map((chat) => chat.title || t('settings.usage.untitled')).join('\n')}
            >
              {t('settings.usage.moreChats', { count: more })}
            </span>
          ) : null}
        </div>
        {container.repo ? (
          <p className="mt-0.5 truncate text-xs text-muted">{container.repo}</p>
        ) : null}
      </div>
      <div className="shrink-0 text-xs text-muted sm:text-right">
        {container.state === 'running' ? (
          <>
            <p className="tabular-nums text-foreground">{readingText}</p>
            {container.idle_deadline ? (
              <p>
                {t('settings.usage.idleStop', {
                  when: relativeTime(container.idle_deadline, nowMs, locale),
                })}
              </p>
            ) : null}
          </>
        ) : (
          <>
            <p className="tabular-nums">
              {t('settings.usage.limits', { cpu: cpuLimit, memory: memoryLimit })}
            </p>
            <p>
              {t('settings.usage.lastUsed', {
                when: relativeTime(container.last_activity_at, nowMs, locale),
              })}
            </p>
          </>
        )}
      </div>
    </li>
  );
}
