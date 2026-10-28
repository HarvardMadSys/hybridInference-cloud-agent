// How a gateway-measured model TTFT is shown, wherever it is shown.
//
// The value is the gateway's own time from receiving a model request to the
// first streamed token, averaged over requests. It never includes sandbox,
// workspace or CLI startup, and a summary without a measured request shows
// nothing: no event-derived interval stands in for it.

import type { ModelTtftApi } from '@/lib/api/agents';

import { formatDuration } from './TurnFooter';

/** Why a summary covers only part of its scope, in the words a reader needs. */
const GAPS: Record<string, string> = {
  sources_truncated: 'only the most recent runs were read',
  request_page_truncated: 'a run made more requests than one read covers',
  gateway_unavailable: 'the gateway could not be read for every run',
  request_details_unsupported: 'the gateway does not report per-request detail for every run',
  binding_changed: 'some runs were recorded by another gateway installation',
  attribution_unavailable: 'some runs have no gateway attribution',
  invalid_window: 'a run has no attribution window yet',
};

export interface ModelTtftDisplay {
  /** The mean, formatted: "10.4s". */
  value: string;
  /** False when the mean covers only part of the scope. */
  complete: boolean;
  /** What the mean is over, and what it leaves out. */
  title: string;
}

/** The display for a summary, or null when no request measured a first token. */
export function modelTtftDisplay(
  summary: ModelTtftApi | null | undefined,
): ModelTtftDisplay | null {
  if (!summary || summary.status !== 'available' || summary.samples <= 0) return null;
  const mean = summary.mean_ttft_ms;
  if (typeof mean !== 'number' || !Number.isFinite(mean) || mean <= 0) return null;
  const value = formatDuration(mean);
  if (!value) return null;
  const requests = `${summary.samples} successful streamed request${summary.samples === 1 ? '' : 's'}`;
  const gap = summary.complete
    ? ''
    : ` Partial: ${GAPS[summary.reason ?? ''] ?? 'not every run could be read'}.`;
  return {
    value,
    complete: summary.complete,
    title:
      'Mean time from the gateway receiving a model request to its first streamed token, ' +
      `over ${requests}. Sandbox, workspace and agent startup are not included.${gap}`,
  };
}
