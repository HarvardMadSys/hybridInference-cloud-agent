'use client';

// The selected request's context, Current-Context style. Layout adapted from
// bowenliang123/dsh-context `currentComposition.tsx` (Apache-2.0, commit
// 32982aa6aa2c70fbda6b81281de98a72ea625001). dsh-context draws one bar because
// it knows every category's size; here two tracks keep two kinds of number
// apart:
//
// 1. Observed (exact): the request's input as the provider reported it, on the
//    window the CLI reported. No window, no percentage.
// 2. Recorded (estimate): the stored content before the request, by category.
//    Where it covers less than the observed input, a hatch marks the part no
//    record explains (system prompt, tool schemas, clipped text…) without a
//    number or a category; where it is larger, the card says so.
//
// It is the latest *observed* request (or the one selected), never live.

import { useLocale } from '@/components/providers';
import type { ContextPointApi, LatestContextApi } from '@/lib/api/agents';
import { compactCount, relativeTime } from '@/components/features/settings/usagePresent';
import { DashCard, shareText } from './DashCard';
import { labelOf, swatchOf } from './palette';

export interface Coverage {
  parts: { key: string; tokens: number; widthPct: number }[];
  estimate: number;
  /** Hatched width, % of the track: what no record explains. */
  uncoveredPct: number;
  exceeds: boolean;
}

/** Track 2's geometry: on the observed size, or on the estimate when it is larger. */
export function coverageOf(
  keys: string[],
  categories: number[] | null | undefined,
  observed: number,
): Coverage | null {
  if (!categories || categories.length !== keys.length) return null;
  const estimate = categories.reduce((sum, value) => sum + Math.max(0, value), 0);
  if (estimate <= 0) return null;
  const scale = Math.max(estimate, observed);
  return {
    parts: keys
      .map((key, index) => ({
        key,
        tokens: Math.max(0, categories[index]),
        widthPct: (Math.max(0, categories[index]) / scale) * 100,
      }))
      .filter((part) => part.tokens > 0),
    estimate,
    uncoveredPct: estimate < observed ? ((observed - estimate) / observed) * 100 : 0,
    exceeds: estimate > observed,
  };
}

export function WindowCard({
  point,
  latest,
  keys,
  nowMs,
}: {
  point: ContextPointApi | null;
  latest: LatestContextApi | null;
  keys: string[];
  nowMs: number;
}) {
  const { t, locale } = useLocale();
  if (!point) {
    return (
      <DashCard title={t('dashboard.window.title')}>
        <p className="text-xs text-muted">{t('context.empty')}</p>
      </DashCard>
    );
  }
  const isLatest = latest?.point.index === point.index;
  const window = point.context_window && point.context_window > 0 ? point.context_window : null;
  const observed = point.prompt_tokens;
  const usedPct = window ? Math.min(100, (observed / window) * 100) : null;
  const meta = [
    t('context.compactionTurn', { turn: point.turn_no }),
    point.at ? relativeTime(point.at, nowMs, locale) : null,
    point.model,
  ];
  const size = window
    ? ` / ${compactCount(window)} ${t('dashboard.window.tokens')}`
    : ` ${t('dashboard.window.tokens')}`;
  return (
    <DashCard
      title={t(isLatest ? 'dashboard.window.latest' : 'dashboard.window.selected')}
      aside={<span className="text-[11px] text-muted">{meta.filter(Boolean).join(' · ')}</span>}
    >
      <p className="tabular-nums text-foreground">
        <b className="text-lg">{compactCount(observed)}</b>
        <span className="text-xs text-muted">{size}</span>
        {usedPct !== null ? (
          <span className="ml-2 text-sm font-semibold">{`${usedPct.toFixed(1)}%`}</span>
        ) : null}
      </p>
      <p className="text-[11px] text-muted">{t('dashboard.window.observedNote')}</p>
      <ObservedTrack observed={observed} window={window} />
      <RecordedTrack coverage={coverageOf(keys, point.categories, observed)} observed={observed} />
      {isLatest && latest?.compacted_after ? (
        <p className="mt-1 text-[11px] text-muted">{t('context.compactedAfter')}</p>
      ) : null}
    </DashCard>
  );
}

function ObservedTrack({ observed, window }: { observed: number; window: number | null }) {
  const { t } = useLocale();
  let label = t('contextInsights.windowNotReported');
  let note = t('dashboard.window.noWindow');
  if (window) {
    label = t('dashboard.window.observedAria', {
      used: compactCount(observed),
      window: compactCount(window),
    });
    note = t('dashboard.window.free', { free: compactCount(Math.max(0, window - observed)) });
  }
  return (
    <div className="mt-2">
      <p className="text-[11px] text-muted">{t('dashboard.window.observedTrack')}</p>
      <div
        role="img"
        aria-label={label}
        className="relative mt-0.5 h-2.5 overflow-hidden rounded-full bg-surface-muted"
      >
        {/* No reported window: an empty track, never a full one. */}
        {window ? (
          <span
            className="absolute inset-y-0 left-0 rounded-full bg-foreground/70"
            style={{ width: `${Math.min(100, (observed / window) * 100)}%` }}
          />
        ) : null}
      </div>
      <p className="mt-0.5 text-[11px] text-muted">{note}</p>
    </div>
  );
}

const HATCH =
  'bg-[repeating-linear-gradient(45deg,transparent_0_3px,rgb(148_163_184/0.6)_3px_5px)]';

function RecordedTrack({ coverage, observed }: { coverage: Coverage | null; observed: number }) {
  const { t } = useLocale();
  if (!coverage) {
    return (
      <div className="mt-2">
        <p className="text-[11px] text-muted">{t('dashboard.window.recordedTrack')}</p>
        <p className="text-[11px] text-muted">{t('dashboard.window.noRecorded')}</p>
      </div>
    );
  }
  const uncovered = coverage.uncoveredPct > 0;
  return (
    <div className="mt-2">
      <p className="text-[11px] text-muted">{t('dashboard.window.recordedTrack')}</p>
      <div
        role="img"
        aria-label={t('dashboard.window.recordedTrack')}
        className="mt-0.5 flex h-2.5 overflow-hidden rounded-full bg-surface-muted"
      >
        {coverage.parts.map((part) => (
          <span
            key={part.key}
            className={`h-full ${swatchOf(part.key).bg}`}
            style={{ width: `${part.widthPct}%` }}
          />
        ))}
        {uncovered ? (
          <span className={`h-full ${HATCH}`} style={{ width: `${coverage.uncoveredPct}%` }} />
        ) : null}
      </div>
      <ul className="mt-1 grid grid-cols-1 gap-x-3 gap-y-0.5 text-[11px] sm:grid-cols-2">
        {coverage.parts.map((part) => (
          <li key={part.key} className="flex items-center justify-between gap-2">
            <span className="flex min-w-0 items-center gap-1.5 text-muted">
              <span
                aria-hidden="true"
                className={`h-2 w-2 shrink-0 rounded-full ${swatchOf(part.key).bg}`}
              />
              <span className="truncate">{t(labelOf(part.key))}</span>
            </span>
            <span className="tabular-nums text-foreground">
              ≈{compactCount(part.tokens)}{' '}
              <span className="text-muted">{shareText(part.tokens, coverage.estimate)}</span>
            </span>
          </li>
        ))}
        {uncovered ? (
          <li className="flex items-center gap-1.5 text-muted sm:col-span-2">
            <span
              aria-hidden="true"
              className="h-2 w-2 shrink-0 rounded-full border border-dashed border-muted"
            />
            {t('dashboard.window.uncovered')}
          </li>
        ) : null}
      </ul>
      <p className="mt-1 text-[11px] text-muted">
        {t(coverage.exceeds ? 'dashboard.window.exceeds' : 'dashboard.window.estimate', {
          estimate: compactCount(coverage.estimate),
          observed: compactCount(observed),
        })}
      </p>
    </div>
  );
}
