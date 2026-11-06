import type { Translator } from '@/lib/i18n/translate';

const MINUTE = 60_000;
const HOUR = 60 * MINUTE;
const DAY = 24 * HOUR;

/**
 * When a conversation last moved, as the short label a list row can afford:
 * `3m`, `2h`, `5d`, then a calendar date once "days ago" stops meaning much.
 *
 * Every piece comes from the dictionary — the date included — so the label
 * follows the reader's language without a second locale source, and renders
 * the same in every browser. Null when the time is unknown, so the row shows
 * nothing rather than "now".
 */
export function activityLabel(
  iso: string | null | undefined,
  now: number,
  t: Translator,
): string | null {
  const time = Date.parse(iso ?? '');
  if (!Number.isFinite(time)) return null;
  const elapsed = Math.max(0, now - time);
  if (elapsed < MINUTE) return t('sidebar.justNow');
  if (elapsed < HOUR) return t('sidebar.minutesAgo', { count: Math.floor(elapsed / MINUTE) });
  if (elapsed < DAY) return t('sidebar.hoursAgo', { count: Math.floor(elapsed / HOUR) });
  if (elapsed < 7 * DAY) return t('sidebar.daysAgo', { count: Math.floor(elapsed / DAY) });
  const date = new Date(time);
  const vars = { year: date.getFullYear(), month: date.getMonth() + 1, day: date.getDate() };
  return date.getFullYear() === new Date(now).getFullYear()
    ? t('sidebar.dateThisYear', vars)
    : t('sidebar.dateOtherYear', vars);
}
