'use client';

import Link from 'next/link';
import { useCallback, useEffect, useRef, useState } from 'react';
import { useLocale } from '@/components/providers';
import { useSiteConfig } from '@/components/providers/SiteConfigProvider';
import type { TranslationKey } from '@/lib/i18n/translate';
import {
  getOwnerContainers,
  getOwnerResources,
  getOwnerTokens,
  type ContainerState,
  type OwnerContainer,
  type OwnerContainerList,
  type OwnerResourceHistory,
  type OwnerTokenUsage,
} from '@/lib/api/ownerUsage';
import {
  CHART_METRICS,
  STATE_ORDER,
  USAGE_PERIODS,
  chartDays,
  compactCount,
  coreHours,
  dayValue,
  decimal,
  duration,
  formatCost,
  formatDayValue,
  formatMemory,
  gibHours,
  partlyMeasured,
  relativeTime,
  shortDay,
  tickIndexes,
  tokenTotal,
  unrecordedTurns,
  utcMidnightLocal,
  type ChartDay,
  type ChartMetric,
  type UsagePeriod,
} from './usagePresent';

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

const DURATION_UNIT: Record<'hours' | 'minutes', TranslationKey> = {
  hours: 'settings.usage.hours',
  minutes: 'settings.usage.minutes',
};

const CHART_LABEL: Record<ChartMetric, TranslationKey> = {
  cost: 'settings.usage.chart.cost',
  tokens: 'settings.usage.chart.tokens',
  running: 'settings.usage.chart.running',
  cpu: 'settings.usage.chart.cpu',
  memory: 'settings.usage.chart.memory',
};

function errorText(cause: unknown): string | null {
  return cause instanceof Error && cause.message ? cause.message : null;
}

/** Your containers now, and what your work used over a period. */
export function UsageSettings() {
  return (
    <>
      <Containers />
      <History />
    </>
  );
}

function Card({
  id,
  title,
  action,
  children,
}: {
  id: string;
  title: string;
  action?: React.ReactNode;
  children: React.ReactNode;
}) {
  return (
    <section aria-labelledby={id} className="ui-card mt-4 bg-surface p-3 sm:p-4">
      <div className="flex flex-wrap items-center justify-between gap-3">
        <h2 id={id} className="text-base font-semibold text-foreground">
          {title}
        </h2>
        {action}
      </div>
      {children}
    </section>
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
        if (request !== latest.current) return;
        setList(page);
        setContainers(page.containers);
        setCursor(page.next_cursor);
        setNowMs(Date.now());
        setError(null);
      } catch (cause) {
        if (request === latest.current && !quiet) {
          setError(errorText(cause) ?? t('settings.usage.loadError'));
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
      if (request === latest.current) setError(errorText(cause) ?? t('settings.usage.loadError'));
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

function History() {
  const { t, locale } = useLocale();
  const { branding } = useSiteConfig();
  const [period, setPeriod] = useState<UsagePeriod>(7);
  const [tokens, setTokens] = useState<OwnerTokenUsage | null>(null);
  const [resources, setResources] = useState<OwnerResourceHistory | null>(null);
  const [tokenError, setTokenError] = useState<string | null>(null);
  const [resourceError, setResourceError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const [attempt, setAttempt] = useState(0);

  useEffect(() => {
    const controller = new AbortController();
    setLoading(true);
    setTokenError(null);
    setResourceError(null);
    // Each half fails on its own: model usage can be refused for a selection
    // too large to sum while container history still reads.
    const reads = [
      getOwnerTokens(period, controller.signal).then(setTokens, (cause: unknown) => {
        if (controller.signal.aborted) return;
        setTokens(null);
        setTokenError(errorText(cause) ?? t('settings.usage.loadError'));
      }),
      getOwnerResources(period, controller.signal).then(setResources, (cause: unknown) => {
        if (controller.signal.aborted) return;
        setResources(null);
        setResourceError(errorText(cause) ?? t('settings.usage.loadError'));
      }),
    ];
    void Promise.all(reads).then(() => {
      if (!controller.signal.aborted) setLoading(false);
    });
    return () => controller.abort();
  }, [period, attempt, t]);

  const totals = resources?.totals;
  const hours = (seconds: number) => {
    const { unit, value } = duration(seconds);
    return t(DURATION_UNIT[unit], { value });
  };
  const summary = tokens?.summary;
  // Only after a history loads: the server's render has no reader time zone,
  // and the first browser render has to match it.
  const generatedAt = resources?.generated_at ?? tokens?.generated_at;
  const dayStart = generatedAt ? utcMidnightLocal(Date.parse(generatedAt), locale) : null;
  const unrecorded = summary ? unrecordedTurns(summary) : 0;
  let tokenDetail: string | undefined;
  if (summary?.totals) {
    tokenDetail = t('settings.usage.tokensInOut', {
      in: compactCount(summary.totals.tokens_in),
      out: compactCount(summary.totals.tokens_out),
    });
  }
  const periods = (
    <div
      role="group"
      aria-label={t('settings.usage.period')}
      className="flex gap-1 rounded-lg border border-border bg-surface-muted p-1"
    >
      {USAGE_PERIODS.map((days) => (
        <button
          key={days}
          type="button"
          aria-pressed={period === days}
          onClick={() => setPeriod(days)}
          className={`ui-focus-ring min-h-9 rounded-md px-3 text-sm font-medium transition-colors [@media(pointer:coarse)]:min-h-11 ${
            period === days
              ? 'bg-surface text-foreground shadow-subtle'
              : 'text-muted hover:bg-surface-hover hover:text-foreground'
          }`}
        >
          {t('settings.usage.days', { count: days })}
        </button>
      ))}
    </div>
  );
  return (
    <Card id="settings-usage-history" title={t('settings.usage.history')} action={periods}>
      {tokenError ? (
        <LoadError message={tokenError} onRetry={() => setAttempt((value) => value + 1)} />
      ) : null}
      {resourceError ? (
        <LoadError message={resourceError} onRetry={() => setAttempt((value) => value + 1)} />
      ) : null}
      <dl
        aria-busy={loading}
        className={`mt-3 grid grid-cols-2 gap-2 lg:grid-cols-5 ${loading ? 'opacity-60' : ''}`}
      >
        <Tile
          label={t('settings.usage.cost')}
          value={summary ? formatCost(summary.totals?.spent_usd) : '—'}
          detail={t('settings.usage.costEstimate')}
        />
        <Tile
          label={t('settings.usage.tokens')}
          value={summary ? compactCount(tokenTotal(summary)) : '—'}
          detail={tokenDetail}
        />
        <Tile
          label={t('settings.usage.runningTime')}
          value={totals ? hours(totals.running_seconds) : '—'}
        />
        <Tile
          label={t('settings.usage.cpuTime')}
          value={
            totals ? t('settings.usage.coreHours', { value: decimal(coreHours(totals)) }) : '—'
          }
        />
        <Tile
          label={t('settings.usage.memoryTime')}
          value={totals ? t('settings.usage.gbHours', { value: decimal(gibHours(totals)) }) : '—'}
        />
      </dl>
      {totals && partlyMeasured(totals) ? (
        <p className="mt-2 text-xs text-muted">
          {t('settings.usage.partlyMeasured', {
            measured: hours(totals.measured_seconds),
            running: hours(totals.running_seconds),
          })}
        </p>
      ) : null}
      {unrecorded > 0 ? (
        <p className="mt-2 text-xs text-muted">
          {t('settings.usage.unrecorded', { count: unrecorded })}
        </p>
      ) : null}
      {tokens || resources ? (
        <DailyChart days={chartDays(tokens?.days, resources?.days)} locale={locale} />
      ) : null}
      {tokens ? <ByModel usage={tokens} /> : null}
      <p className="mt-4 text-xs text-muted">
        {dayStart
          ? t('settings.usage.utcNoteLocal', { time: dayStart })
          : t('settings.usage.utcNote')}
        {branding.gatewayUrl ? (
          <>
            {' '}
            <a
              href={`${branding.gatewayUrl}/dashboard`}
              className="ui-focus-ring rounded font-medium text-accent hover:underline"
            >
              {t('settings.usage.dashboardLink')}
            </a>
          </>
        ) : null}
      </p>
    </Card>
  );
}

function DailyChart({ days, locale }: { days: ChartDay[]; locale: string }) {
  const { t } = useLocale();
  const [metric, setMetric] = useState<ChartMetric>('cost');
  const [pointed, setPointed] = useState<number | null>(null);
  if (days.length === 0) return null;
  const values = days.map((day) => dayValue(day, metric));
  const peak = Math.max(0, ...values.map((value) => value ?? 0));
  const ticks = new Set(tickIndexes(days.length));
  const phoneTicks = new Set(tickIndexes(days.length, 4));
  const shown = pointed !== null && pointed < days.length ? pointed : days.length - 1;
  const label = t(CHART_LABEL[metric]);
  const reading = (index: number) => {
    const day = days[index];
    const name = `${shortDay(day.date, locale)}${day.whole ? '' : ` (${t('settings.usage.today')})`}`;
    const value = values[index];
    return `${name} · ${value === null ? t('settings.usage.notRecorded') : formatDayValue(metric, value)}`;
  };
  return (
    <section aria-label={t('settings.usage.chartLabel', { metric: label })} className="mt-5">
      <div className="flex flex-wrap items-center justify-between gap-3">
        <div
          role="group"
          aria-label={t('settings.usage.chartMetric')}
          className="flex flex-wrap gap-1 rounded-lg border border-border bg-surface-muted p-1"
        >
          {CHART_METRICS.map((item) => (
            <button
              key={item}
              type="button"
              aria-pressed={item === metric}
              onClick={() => setMetric(item)}
              className={`ui-focus-ring min-h-8 rounded-md px-2.5 text-xs font-medium transition-colors [@media(pointer:coarse)]:min-h-11 ${
                item === metric
                  ? 'bg-surface text-foreground shadow-subtle'
                  : 'text-muted hover:bg-surface-hover hover:text-foreground'
              }`}
            >
              {t(CHART_LABEL[item])}
            </button>
          ))}
        </div>
        <p aria-hidden="true" className="text-xs tabular-nums text-muted">
          {reading(shown)}
        </p>
      </div>
      <div aria-hidden="true" className="relative mt-3 h-36 border-b border-border">
        <div className="absolute inset-x-0 top-0 border-t border-dashed border-border" />
        <span className="absolute left-0 top-1 text-[11px] tabular-nums text-muted">
          {formatDayValue(metric, peak)}
        </span>
        <div className="flex h-full items-end gap-[2px] pt-6">
          {days.map((day, index) => {
            const value = values[index];
            const height = peak > 0 && value ? Math.max(2, (value / peak) * 100) : 0;
            return (
              <div
                key={day.date}
                onMouseEnter={() => setPointed(index)}
                onMouseLeave={() => setPointed(null)}
                className="group flex h-full min-w-0 flex-1 items-end justify-center"
              >
                {value === null ? (
                  // Unknown, not zero: marked rather than drawn as an empty bar.
                  <span className="block w-full max-w-8 border-t-2 border-dotted border-muted" />
                ) : (
                  <span
                    className={`block w-full max-w-8 rounded-t-[4px] transition-colors ${
                      index === pointed ? 'bg-accent' : 'bg-accent/40 group-hover:bg-accent'
                    } ${day.whole ? '' : 'opacity-60'}`}
                    style={{ height: `${height}%` }}
                  />
                )}
              </div>
            );
          })}
        </div>
      </div>
      <div aria-hidden="true" className="relative mt-1.5 h-4 text-[11px] tabular-nums text-muted">
        {days.map((day, index) =>
          ticks.has(index) ? (
            <span
              key={day.date}
              className={`absolute top-0 whitespace-nowrap ${phoneTicks.has(index) ? '' : 'max-sm:hidden'}`}
              style={tickPosition(index, days.length)}
            >
              {shortDay(day.date, locale)}
            </span>
          ) : null,
        )}
      </div>
      <table className="sr-only">
        <caption>{t('settings.usage.chartLabel', { metric: label })}</caption>
        <tbody>
          {days.map((day, index) => (
            <tr key={day.date}>
              <td>{reading(index)}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </section>
  );
}

/** Where a day's label sits: under its bar, kept inside the chart at both ends. */
function tickPosition(index: number, total: number): React.CSSProperties {
  if (index === 0) return { left: 0 };
  if (index === total - 1) return { right: 0 };
  return { left: `${((index + 0.5) / total) * 100}%`, transform: 'translateX(-50%)' };
}

function ByModel({ usage }: { usage: OwnerTokenUsage }) {
  const { t } = useLocale();
  const rows = [...usage.by_model.groups, ...(usage.by_model.other ? [usage.by_model.other] : [])];
  if (rows.length === 0) {
    return <p className="mt-4 text-sm text-muted">{t('settings.usage.noData')}</p>;
  }
  const other = usage.by_model.other;
  return (
    <div className="mt-5 overflow-x-auto">
      <table className="w-full min-w-[28rem] text-left text-sm">
        <caption className="mb-1 text-left text-sm font-semibold text-foreground">
          {t('settings.usage.byModel')}
        </caption>
        <thead className="text-xs text-muted">
          <tr className="border-b border-border">
            <th scope="col" className="py-1.5 pr-3 font-medium">
              {t('settings.usage.model')}
            </th>
            <th scope="col" className="py-1.5 pr-3 text-right font-medium">
              {t('settings.usage.cost')}
            </th>
            <th scope="col" className="py-1.5 pr-3 text-right font-medium">
              {t('settings.usage.tokensIn')}
            </th>
            <th scope="col" className="py-1.5 pr-3 text-right font-medium">
              {t('settings.usage.tokensOut')}
            </th>
            <th scope="col" className="py-1.5 text-right font-medium">
              {t('settings.usage.turnsColumn')}
            </th>
          </tr>
        </thead>
        <tbody className="tabular-nums">
          {rows.map((group) => (
            <tr
              key={group === other ? '\u0000other' : (group.key ?? '\u0000unknown')}
              className="border-b border-border last:border-0"
            >
              <th
                scope="row"
                className="max-w-[16rem] truncate py-1.5 pr-3 font-normal text-foreground"
              >
                {group === other
                  ? t('settings.usage.otherModels')
                  : (group.key ?? t('settings.usage.unknownModel'))}
              </th>
              <td className="py-1.5 pr-3 text-right">
                {formatCost(group.usage.totals?.spent_usd)}
              </td>
              <td className="py-1.5 pr-3 text-right">
                {compactCount(group.usage.totals?.tokens_in)}
              </td>
              <td className="py-1.5 pr-3 text-right">
                {compactCount(group.usage.totals?.tokens_out)}
              </td>
              <td className="py-1.5 text-right">{group.usage.run_count.toLocaleString('en-US')}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

function Tile({ label, value, detail }: { label: string; value: string; detail?: string }) {
  return (
    <div className="min-w-0 rounded-lg border border-border px-3 py-2">
      <dt className="text-xs text-muted">{label}</dt>
      <dd className="mt-0.5 break-words text-base font-semibold tabular-nums text-foreground sm:text-lg">
        {value}
      </dd>
      {detail ? <dd className="mt-0.5 truncate text-xs text-muted">{detail}</dd> : null}
    </div>
  );
}

function LoadError({ message, onRetry }: { message: string; onRetry: () => void }) {
  const { t } = useLocale();
  return (
    <div
      role="alert"
      className="mt-3 flex flex-wrap items-center justify-between gap-3 rounded-lg border border-red-200 bg-red-50 px-3 py-2 text-sm text-red-700 dark:border-red-500/25 dark:bg-red-500/10 dark:text-red-400"
    >
      <span className="min-w-0 break-words">{message}</span>
      <button type="button" onClick={onRetry} className="ui-button ui-focus-ring min-h-11 shrink-0">
        {t('settings.usage.retry')}
      </button>
    </div>
  );
}
