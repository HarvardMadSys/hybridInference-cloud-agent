// Formatting shared by the agent views and the admin pages.
//
// A plain module rather than a client component, so a page can import a
// formatter without reaching into a component.

/**
 * Round the way the reader will see it, so callers can branch on the printed
 * value rather than the one before rounding — the gap between the two is how
 * "60.0s" and "1024MB" happen.
 *
 * The decimal is kept below 100 because that is where it still carries
 * meaning — "18.2s" of CPU is a different statement from "18s" — and dropped
 * above it, where a tenth of a gigabyte is noise in a footer.
 */
export function rounded(value: number): number {
  return value >= 100 ? Math.round(value) : Math.round(value * 10) / 10;
}

/** `rounded`, printed without a trailing ".0". */
export function trim(value: number): string {
  const shown = rounded(value);
  return Number.isInteger(shown) ? String(shown) : shown.toFixed(1);
}

/**
 * A span of time as a reader would say it: `420ms`, `4.2s`, `2m 5s`.
 *
 * A missing span returns null rather than a zero, so "not reported" never
 * renders as "0ms".
 */
export function formatDuration(ms: number | null | undefined): string | null {
  if (typeof ms !== 'number' || !Number.isFinite(ms) || ms < 0) return null;
  if (ms < 1000) return `${Math.round(ms)}ms`;
  // Rounding is decided once, before the branch: 59.96s rounds to 60, and a
  // branch taken on the unrounded value renders it as "60.0s" — or, one
  // minute later, as "1m 60s".
  const seconds = rounded(ms / 1000);
  if (seconds < 60) return `${trim(seconds)}s`;
  const minutes = Math.floor(seconds / 60);
  const rest = Math.round(seconds - minutes * 60);
  return rest ? `${minutes}m ${rest}s` : `${minutes}m`;
}

/** `Sep 23`, for a UTC calendar date or an instant, as a toolbar can afford it. */
export function shortDay(value: string | null | undefined): string {
  const time = Date.parse(value ?? '');
  if (!Number.isFinite(time)) return '—';
  return new Date(time).toLocaleDateString('en-US', {
    month: 'short',
    day: 'numeric',
    timeZone: 'UTC',
  });
}
