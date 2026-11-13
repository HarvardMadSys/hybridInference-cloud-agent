'use client';

// One line over the dashboard with the counts no card below repeats: the
// conversation's main-agent requests (and the subagents' own), its largest
// request, and live occupancy, which no CLI here reports. Compactions are
// counted on the Context events filter, the latest request on the window card.

import { useLocale } from '@/components/providers';
import type { ThreadContextApi } from '@/lib/api/agents';
import { compactCount } from '@/components/features/settings/usagePresent';

function Fact({ label, value, title }: { label: string; value: string; title?: string }) {
  return (
    <div className="flex min-w-0 items-baseline gap-1.5" title={title}>
      <dt className="text-muted">{label}</dt>
      <dd className="font-medium tabular-nums text-foreground">{value}</dd>
    </div>
  );
}

export function ContextOverview({ view }: { view: ThreadContextApi }) {
  const { t, locale } = useLocale();
  // With nothing observed, a count of requests is unknown, not 0.
  const observed = view.coverage.attempts_observed > 0;
  const subagent = view.coverage.subagent_requests;
  let requests = observed ? view.coverage.main_requests.toLocaleString(locale) : '—';
  if (observed && subagent > 0) {
    requests += ` · ${t('context.requestsDetail', { count: subagent.toLocaleString(locale) })}`;
  }
  let peak = compactCount(view.peak?.prompt_tokens);
  if (view.peak) peak += ` · ${t('context.peakDetail', { turn: view.peak.turn_no })}`;
  return (
    <div className="text-xs">
      <dl className="flex flex-wrap gap-x-5 gap-y-1">
        <Fact label={t('context.requests')} value={requests} />
        <Fact label={t('context.peak')} value={peak} />
        <Fact
          label={t('context.live')}
          value={t('context.liveNotCollected')}
          title={t('context.liveDetail')}
        />
      </dl>
      {view.latest?.turn_active ? (
        <p role="status" className="mt-1 text-muted">
          {t('context.turnActive')}
        </p>
      ) : null}
    </div>
  );
}
