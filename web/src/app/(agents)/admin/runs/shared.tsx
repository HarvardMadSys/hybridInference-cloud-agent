import type { ReactNode } from 'react';
import { APIError } from '@/lib/utils/errors';

export function accessDenied(error: unknown): boolean {
  return error instanceof APIError && (error.statusCode === 401 || error.statusCode === 403);
}

export const CARD =
  'rounded-2xl border border-gray-200 bg-white shadow-sm dark:border-white/10 dark:bg-gray-900';
// Focus rings replace the native outline, so they have to clear WCAG 1.4.11's
// 3:1 non-text contrast on their own. Solid crimson is 7.2:1 against the light
// surface and crimson-light is 3.4:1 against gray-900; the 40%-alpha variants
// this replaced measured 2.1:1 and 1.3:1. The 2px offset keeps the ring off the
// control's own border, which is too light in dark mode to sit against.
export const BUTTON =
  'rounded-lg border border-gray-200 px-3 py-1.5 text-sm font-medium text-gray-700 transition-colors hover:bg-gray-50 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-crimson focus-visible:ring-offset-2 focus-visible:ring-offset-white disabled:cursor-not-allowed disabled:opacity-40 dark:border-white/10 dark:text-gray-200 dark:hover:bg-white/5 dark:focus-visible:ring-crimson-light dark:focus-visible:ring-offset-gray-900';
export const INPUT =
  'w-full rounded-lg border border-gray-200 bg-white px-3 py-2 text-sm text-gray-900 shadow-sm transition-colors placeholder:text-gray-400 focus:border-crimson focus:outline-none focus:ring-2 focus:ring-crimson focus:ring-offset-2 focus:ring-offset-white dark:border-white/10 dark:bg-gray-900 dark:text-gray-100 dark:placeholder:text-gray-500 dark:focus:border-crimson-light dark:focus:ring-crimson-light dark:focus:ring-offset-gray-900';

export function AdminRequired() {
  return (
    <div className="flex flex-1 flex-col items-center justify-center gap-3 p-8">
      <h1 className="text-xl font-semibold text-gray-900 dark:text-gray-50">
        Admin access required
      </h1>
      <p className="text-sm text-gray-500 dark:text-gray-400">
        Runs across users are visible to admins only.
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
  publishing: {
    badge:
      'bg-blue-50 text-blue-700 ring-blue-600/20 dark:bg-blue-500/10 dark:text-blue-400 dark:ring-blue-500/25',
    dot: 'bg-blue-500',
  },
  waiting: {
    badge:
      'bg-gray-100 text-gray-600 ring-gray-500/20 dark:bg-white/5 dark:text-gray-400 dark:ring-white/15',
    dot: 'bg-gray-500 dark:bg-gray-400',
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

const RUN_STATE_FALLBACK = RUN_STATE_STYLES.waiting;

export function RunState({ state }: { state: string }) {
  const style = RUN_STATE_STYLES[state] ?? RUN_STATE_FALLBACK;
  return (
    <span
      className={`inline-flex items-center gap-1.5 rounded-full px-2 py-0.5 text-xs font-medium ring-1 ring-inset ${style.badge}`}
    >
      <span aria-hidden className={`h-1.5 w-1.5 shrink-0 rounded-full ${style.dot}`} />
      {state}
    </span>
  );
}

export function dateTime(value: string | null | undefined): string {
  if (!value || !Number.isFinite(Date.parse(value))) return 'Unavailable';
  return new Date(value).toLocaleString(undefined, { dateStyle: 'medium', timeStyle: 'medium' });
}

export function number(value: number | null | undefined): string {
  return typeof value === 'number' && Number.isFinite(value) && value >= 0
    ? value.toLocaleString()
    : 'Unavailable';
}
