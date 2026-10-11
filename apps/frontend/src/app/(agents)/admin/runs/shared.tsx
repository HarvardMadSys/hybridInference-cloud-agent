import type { ReactNode } from 'react';
import { APIError } from '@/lib/utils/errors';

export function accessDenied(error: unknown): boolean {
  return error instanceof APIError && (error.statusCode === 401 || error.statusCode === 403);
}

export const CARD = 'ui-card rounded-2xl';
// Shared semantic controls retain a solid, offset theme-aware focus indicator.
export const BUTTON = 'ui-button ui-button-secondary min-h-11';
//: A compact select for a toolbar, sized to sit beside a segmented control.
export const TOOLBAR_SELECT = 'ui-input min-h-11 pl-3 pr-8 transition-colors hover:border-accent';
//: An icon-only toolbar button; its label lives in aria-label and the tooltip.
export const ICON_BUTTON = 'ui-icon-button ui-button-ghost h-11 w-11 shrink-0';
export const INPUT = 'ui-input min-h-11 w-full placeholder:text-muted';

export function AdminRequired() {
  return (
    <div className="flex flex-1 flex-col items-center justify-center gap-3 p-8">
      <h1 className="text-xl font-semibold text-gray-900 dark:text-gray-50">
        Admin access required
      </h1>
      <p className="text-sm text-gray-500 dark:text-gray-400">
        Sessions and turns across users are visible to admins only.
      </p>
    </div>
  );
}

export function ErrorNotice({ children }: { children: ReactNode }) {
  return (
    <div
      role="alert"
      className="my-4 rounded-xl border border-red-200 bg-red-50 px-4 py-3 text-sm text-red-700 dark:border-red-500/25 dark:bg-red-500/10 dark:text-red-400"
    >
      {children}
    </div>
  );
}

// One tinted pill per state. The leading dot is decorative: it makes the
// column scannable at a glance without adding text the reader has to parse.
const RUN_STATE_STYLES: Record<string, { badge: string; dot: string }> = {
  succeeded: {
    badge:
      'bg-emerald-50 text-emerald-700 ring-emerald-600/20 dark:bg-emerald-500/10 dark:text-emerald-400 dark:ring-emerald-500/25',
    dot: 'bg-emerald-500',
  },
  failed: {
    badge:
      'bg-red-50 text-red-700 ring-red-600/20 dark:bg-red-500/10 dark:text-red-400 dark:ring-red-500/25',
    dot: 'bg-red-500',
  },
  running: {
    badge:
      'bg-blue-50 text-blue-700 ring-blue-600/20 dark:bg-blue-500/10 dark:text-blue-400 dark:ring-blue-500/25',
    dot: 'bg-blue-500',
  },
  queued: {
    badge:
      'bg-gray-100 text-gray-600 ring-gray-500/20 dark:bg-white/5 dark:text-gray-400 dark:ring-white/15',
    dot: 'bg-gray-500 dark:bg-gray-400',
  },
  cancelled: {
    badge:
      'bg-gray-100 text-gray-600 ring-gray-500/20 dark:bg-white/5 dark:text-gray-400 dark:ring-white/15',
    dot: 'bg-gray-500 dark:bg-gray-400',
  },
};

const RUN_STATE_FALLBACK = RUN_STATE_STYLES.queued;

export function RunState({ state, quiet = false }: { state: string; quiet?: boolean }) {
  const displayState = state === 'publishing' ? 'running' : state;
  const style = RUN_STATE_STYLES[displayState] ?? RUN_STATE_FALLBACK;
  if (quiet) {
    // In a list the row is already the frame: the dot carries the colour and
    // the word stays grey, so a column of states reads as one quiet line each.
    return (
      <span className="inline-flex items-center gap-1.5 text-xs text-gray-500 dark:text-gray-400">
        <span aria-hidden className={`h-1.5 w-1.5 shrink-0 rounded-full ${style.dot}`} />
        {displayState}
      </span>
    );
  }
  return (
    <span
      className={`inline-flex items-center gap-1.5 rounded-full px-2 py-0.5 text-xs font-medium ring-1 ring-inset ${style.badge}`}
    >
      <span aria-hidden className={`h-1.5 w-1.5 shrink-0 rounded-full ${style.dot}`} />
      {displayState}
    </span>
  );
}

/**
 * Render an instant in UTC and say so.
 *
 * Admin usage is grouped by UTC creation day, so a local-time rendering beside
 * those buckets would disagree with them. Every admin page uses this formatter
 * instead of the host locale's default.
 */
export function dateTime(value: string | null | undefined): string {
  if (!value || !Number.isFinite(Date.parse(value))) return 'Unavailable';
  return `${new Date(value).toLocaleString('en-US', {
    dateStyle: 'medium',
    timeStyle: 'medium',
    timeZone: 'UTC',
  })} UTC`;
}

/**
 * How long ago, as the short label a list row can afford: `4m ago`, `5h ago`,
 * `3d ago`, then a UTC calendar date. The exact instant belongs in a tooltip.
 */
export function ago(value: string | null | undefined, now: number = Date.now()): string {
  const time = Date.parse(value ?? '');
  if (!Number.isFinite(time)) return 'Unavailable';
  const minutes = Math.floor(Math.max(0, now - time) / 60_000);
  if (minutes < 1) return 'just now';
  if (minutes < 60) return `${minutes}m ago`;
  const hours = Math.floor(minutes / 60);
  if (hours < 24) return `${hours}h ago`;
  const days = Math.floor(hours / 24);
  if (days < 7) return `${days}d ago`;
  return new Date(time).toLocaleDateString('en-US', {
    month: 'short',
    day: 'numeric',
    timeZone: 'UTC',
  });
}

/** A UTC calendar day, without a time, for bucket labels. */
export function utcDate(value: string | null | undefined): string {
  if (!value || !Number.isFinite(Date.parse(value))) return 'Unavailable';
  return new Date(value).toLocaleDateString('en-US', { dateStyle: 'medium', timeZone: 'UTC' });
}

export function number(value: number | null | undefined): string {
  // Any finite number is shown, including a negative one. Hiding a value the
  // code produced behind "Unavailable" is how a wrong count goes unnoticed.
  return typeof value === 'number' && Number.isFinite(value)
    ? value.toLocaleString()
    : 'Unavailable';
}

/**
 * Format a recorded amount without losing what the Gateway reported.
 *
 * The value is a decimal string and is never parsed as a float: the display
 * keeps at most four places, a smaller non-zero amount reads as such rather
 * than as zero, and the exact string stays available for a tooltip. A true zero
 * is a zero; an unknown amount is not.
 */
export function money(value: string | null | undefined): { display: string; exact: string | null } {
  if (value == null || !/^\d+(\.\d+)?$/.test(value)) {
    return { display: 'Unavailable', exact: null };
  }
  if (/^0+(\.0*)?$/.test(value)) return { display: '$0', exact: value };
  const point = value.indexOf('.');
  const shown = point === -1 ? value : value.slice(0, point + 5);
  if (/^0(\.0*)?$/.test(shown)) return { display: '<$0.0001', exact: value };
  const [whole, fraction] = shown.split('.');
  const trimmed = fraction?.replace(/0+$/, '');
  return {
    display: `$${Number(whole).toLocaleString('en-US')}${trimmed ? `.${trimmed}` : ''}`,
    exact: value,
  };
}

/** A metric with its partly-unknown remainder kept visible instead of hidden. */
export function metric(value: number | null | undefined, unknown: number | undefined): string {
  if (typeof value !== 'number') return 'Unavailable';
  return unknown ? `${number(value)} (${number(unknown)} unknown)` : number(value);
}
