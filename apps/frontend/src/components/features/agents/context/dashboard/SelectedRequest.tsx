'use client';

// The selected request under the trend: its reported input, the signed change
// from the request before it, the provider's cache split and output, and the
// way to its step or turn. A strip of the Trend card, not a panel of its own:
// the Window card and the Browser beside it already show what it carried.

import { useLocale } from '@/components/providers';
import type { ContextPointApi } from '@/lib/api/agents';
import { compactCount } from '@/components/features/settings/usagePresent';
import type { OpenPoint, OpenTurn } from '../ContextSections';
import { signedTokens } from '../contextPresent';

const BUTTON = 'ui-button min-h-8 px-2 text-[11px] [@media(pointer:coarse)]:min-h-11';

export function SelectedRequest({
  point,
  onOpenPoint,
  onOpenTurn,
}: {
  point: ContextPointApi | null;
  onOpenPoint: OpenPoint;
  onOpenTurn: OpenTurn;
}) {
  const { t } = useLocale();
  if (!point) return null;
  const facts = [
    t('context.cacheSplit', {
      read: compactCount(point.cache_read_tokens),
      write: compactCount(point.cache_write_tokens),
      uncached: compactCount(point.uncached_input_tokens),
    }),
    t('context.output', { tokens: compactCount(point.output_tokens) }),
  ];
  if (point.model) facts.push(point.model);
  return (
    <div
      role="group"
      aria-label={t('dashboard.selected')}
      className="mt-2 flex flex-wrap items-center justify-between gap-2 rounded-lg border border-border bg-surface-muted/40 px-2.5 py-1.5 text-[11px]"
    >
      <div className="min-w-0">
        <p className="tabular-nums text-foreground">
          <span className="font-medium" title={t('dashboard.window.observedNote')}>
            {t('context.point', {
              index: point.index + 1,
              turn: point.turn_no,
              tokens: compactCount(point.prompt_tokens),
            })}
          </span>
          {point.delta_tokens !== null ? (
            <span className="text-muted">
              {' · '}
              {t('context.delta', { delta: signedTokens(point.delta_tokens, compactCount) })}
            </span>
          ) : null}
          {point.after_compaction ? (
            <span className="text-muted">{` · ${t('context.change.afterCompaction')}`}</span>
          ) : null}
        </p>
        <p className="tabular-nums text-muted" title={t('dashboard.selected.providerTip')}>
          {facts.join(' · ')}
        </p>
      </div>
      <div className="flex shrink-0 gap-1.5">
        <button
          type="button"
          onClick={() =>
            onOpenPoint({ jobId: point.job_id, turnNo: point.turn_no, eventId: point.event_id })
          }
          className={`${BUTTON} ui-button-secondary`}
        >
          {t('context.openStep')}
        </button>
        <button
          type="button"
          onClick={() => onOpenTurn(point.job_id, point.turn_no)}
          className={`${BUTTON} ui-button-ghost`}
        >
          {t('context.openTurn')}
        </button>
      </div>
    </div>
  );
}
