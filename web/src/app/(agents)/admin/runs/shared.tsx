import type { ReactNode } from 'react';
import { APIError } from '@/lib/utils/errors';

export function accessDenied(error: unknown): boolean {
  return error instanceof APIError && (error.statusCode === 401 || error.statusCode === 403);
}

export const CARD =
  'rounded-2xl border border-gray-200 bg-white shadow-sm dark:border-white/10 dark:bg-gray-900';
export const BUTTON =
  'rounded-lg border border-gray-200 px-3 py-1.5 text-sm font-medium text-gray-700 hover:bg-gray-50 disabled:opacity-40 dark:border-white/10 dark:text-gray-200 dark:hover:bg-white/5';
export const INPUT =
  'w-full rounded-lg border border-gray-200 bg-white px-3 py-2 text-sm text-gray-900 dark:border-white/10 dark:bg-gray-900 dark:text-gray-100';

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

export function RunState({ state }: { state: string }) {
  const style =
    state === 'succeeded'
      ? 'bg-emerald-50 text-emerald-700 dark:bg-emerald-500/10 dark:text-emerald-400'
      : state === 'failed'
        ? 'bg-red-50 text-red-700 dark:bg-red-500/10 dark:text-red-400'
        : state === 'running' || state === 'publishing'
          ? 'bg-blue-50 text-blue-700 dark:bg-blue-500/10 dark:text-blue-400'
          : 'bg-gray-100 text-gray-600 dark:bg-white/5 dark:text-gray-400';
  return (
    <span className={`inline-flex rounded-full px-2 py-0.5 text-xs font-medium ${style}`}>
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
