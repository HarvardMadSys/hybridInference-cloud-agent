'use client';

import { useEffect, useState } from 'react';
import { useLocale } from '@/components/providers';
import { useSiteConfig } from '@/components/providers/SiteConfigProvider';
import type { TranslationKey } from '@/lib/i18n/translate';
import {
  getOwnerResources,
  getOwnerTokens,
  type OwnerResourceHistory,
  type OwnerTokenUsage,
  type UsageRangePreset,
} from '@/lib/api/ownerUsage';
import type { AdminUsageAggregate } from '@/lib/api/adminUsage';
import { UsageChart } from './UsageChart';
import { Card, LoadError, RangeNote, RangeSelect, Tile, errorText } from './UsageParts';
import {
  cachedShare,
  chartDays,
  compactCount,
  coreHours,
  decimal,
  duration,
  formatCost,
  formatShare,
  gibHours,
  partlyMeasured,
  reasoningPartly,
  tokenTotal,
  utcMidnightLocal,
} from './usagePresent';

const DURATION_UNIT: Record<'hours' | 'minutes', TranslationKey> = {
  hours: 'settings.usage.hours',
  minutes: 'settings.usage.minutes',
};

/** Both halves of the range's history; each one fails on its own. */
function useHistory(range: UsageRangePreset, attempt: number) {
  const { t } = useLocale();
  const [tokens, setTokens] = useState<OwnerTokenUsage | null>(null);
  const [resources, setResources] = useState<OwnerResourceHistory | null>(null);
  const [tokenError, setTokenError] = useState<string | null>(null);
  const [resourceError, setResourceError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    const controller = new AbortController();
    setLoading(true);
    setTokenError(null);
    setResourceError(null);
    // Model usage can be refused while container history still reads.
    const failed = (clear: () => void, report: (message: string) => void) => (cause: unknown) => {
      if (controller.signal.aborted) return;
      clear();
      report(errorText(cause, t));
    };
    const reads = [
      getOwnerTokens(range, controller.signal).then(
        setTokens,
        failed(() => setTokens(null), setTokenError),
      ),
      getOwnerResources(range, controller.signal).then(
        setResources,
        failed(() => setResources(null), setResourceError),
      ),
    ];
    void Promise.all(reads).then(() => {
      if (!controller.signal.aborted) setLoading(false);
    });
    return () => controller.abort();
  }, [range, attempt, t]);
  return { tokens, resources, tokenError, resourceError, loading };
}

/** The range's model usage and container use, with the chart and the model table. */
export function UsageHistory({
  range,
  onRangeChange,
}: {
  range: UsageRangePreset;
  onRangeChange: (range: UsageRangePreset) => void;
}) {
  const { t } = useLocale();
  const [attempt, setAttempt] = useState(0);
  const { tokens, resources, tokenError, resourceError, loading } = useHistory(range, attempt);
  const retry = () => setAttempt((value) => value + 1);
  const period = tokens?.period ?? resources?.period ?? null;
  return (
    <Card
      id="settings-usage-history"
      title={t('settings.usage.history')}
      action={<RangeSelect id="settings-usage-range" value={range} onChange={onRangeChange} />}
    >
      <RangeNote period={period} />
      {tokenError ? <LoadError message={tokenError} onRetry={retry} /> : null}
      {resourceError ? <LoadError message={resourceError} onRetry={retry} /> : null}
      <HistoryTiles tokens={tokens} resources={resources} loading={loading} />
      {tokens || resources ? (
        <UsageChart
          days={chartDays(tokens?.days, resources?.days)}
          bucket={period?.bucket ?? 'day'}
        />
      ) : null}
      {tokens ? <UsageByModel usage={tokens} /> : null}
      <DayNote generatedAt={resources?.generated_at ?? tokens?.generated_at} />
    </Card>
  );
}

function HistoryTiles({
  tokens,
  resources,
  loading,
}: {
  tokens: OwnerTokenUsage | null;
  resources: OwnerResourceHistory | null;
  loading: boolean;
}) {
  const { t } = useLocale();
  const totals = resources?.totals;
  const summary = tokens?.summary;
  const hours = (seconds: number) => {
    const { unit, value } = duration(seconds);
    return t(DURATION_UNIT[unit], { value });
  };
  let tokenDetail: string | undefined;
  if (summary?.totals) {
    tokenDetail = t('settings.usage.tokensInOut', {
      in: compactCount(summary.totals.tokens_in),
      out: compactCount(summary.totals.tokens_out),
    });
  }
  const dash = (value: string | null) => value ?? '—';
  return (
    <>
      <dl
        aria-busy={loading}
        className={`mt-3 grid grid-cols-2 gap-2 lg:grid-cols-4 ${loading ? 'opacity-60' : ''}`}
      >
        <Tile
          label={t('settings.usage.cost')}
          value={dash(summary ? formatCost(summary.totals?.spent_usd) : null)}
          detail={t('settings.usage.costEstimate')}
        />
        <Tile
          label={t('settings.usage.tokens')}
          value={dash(summary ? compactCount(tokenTotal(summary)) : null)}
          detail={tokenDetail}
        />
        <CacheTile usage={summary} />
        <ReasoningTile usage={summary} />
        <Tile
          label={t('settings.usage.runningTime')}
          value={dash(totals ? hours(totals.running_seconds) : null)}
        />
        <Tile
          label={t('settings.usage.cpuTime')}
          value={dash(
            totals ? t('settings.usage.coreHours', { value: decimal(coreHours(totals)) }) : null,
          )}
        />
        <Tile
          label={t('settings.usage.memoryTime')}
          value={dash(
            totals ? t('settings.usage.gbHours', { value: decimal(gibHours(totals)) }) : null,
          )}
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
    </>
  );
}

/** When a UTC day starts in the reader's own time, and where the daily limit lives. */
function DayNote({ generatedAt }: { generatedAt: string | undefined }) {
  const { t, locale } = useLocale();
  const { branding } = useSiteConfig();
  // Only after a history loads: the server's render has no reader time zone,
  // and the first browser render has to match it.
  const dayStart = generatedAt ? utcMidnightLocal(Date.parse(generatedAt), locale) : null;
  return (
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
  );
}

/** How much of the input was read from the cache; a dash unless both sides are fully known. */
function CacheTile({ usage }: { usage: AdminUsageAggregate | undefined }) {
  const { t } = useLocale();
  const share = usage ? cachedShare(usage) : null;
  let detail: string | undefined;
  if (share !== null && usage?.totals) {
    detail = t('settings.usage.cacheReadOf', {
      read: compactCount(usage.totals.cache_read),
      in: compactCount(usage.totals.tokens_in),
    });
  }
  return (
    <Tile label={t('settings.usage.cacheHitRate')} value={formatShare(share)} detail={detail} />
  );
}

/**
 * Reasoning tokens. They are a part of output tokens, so the token total holds
 * them already; they are shown on their own and never added to it again.
 */
function ReasoningTile({ usage }: { usage: AdminUsageAggregate | undefined }) {
  const { t } = useLocale();
  const partly = usage ? reasoningPartly(usage) : false;
  return (
    <Tile
      label={t('settings.usage.reasoning')}
      value={compactCount(usage?.totals?.reasoning)}
      detail={t(partly ? 'settings.usage.reasoningPartly' : 'settings.usage.reasoningInOutput')}
    />
  );
}

function UsageByModel({ usage }: { usage: OwnerTokenUsage }) {
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
