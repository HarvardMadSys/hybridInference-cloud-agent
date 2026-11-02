// How the Context insights' heatmap, project filter and composition read.
// Pure, so the date and window rules are tested without a browser.
//
// Days are UTC days, the usage history's own. The heatmap covers the whole
// range; when the range has more weeks than fit, a window of them is drawn
// and says which, and the totals above it still cover the whole range.

import type {
  ContextActivityDay,
  ContextComposition,
  ContextInsightsFilters,
  ContextProject,
  RecordedCategory,
} from '@/lib/api/ownerUsage';

const DAY_MS = 86_400_000;

/** Weeks one heatmap view draws before it pages. */
export const HEATMAP_WEEKS = 26;

export interface HeatmapCell {
  /** `YYYY-MM-DD`, a UTC day. */
  day: string;
  /** False for the days of a first or last week that fall outside the range. */
  inRange: boolean;
  activity: ContextActivityDay | null;
  /** 0 for no turns, then 1–4 by turns against the busiest day of the range. */
  level: number;
}

export interface HeatmapWeek {
  /** The week's Monday. */
  start: string;
  cells: HeatmapCell[];
}

export function utcDay(ms: number): string {
  return new Date(ms).toISOString().slice(0, 10);
}

function dayMs(day: string): number {
  return Date.parse(`${day}T00:00:00Z`);
}

/** 1–4 by share of the busiest day; any turn at all is at least 1. */
export function activityLevel(turns: number, busiest: number): number {
  if (turns <= 0 || busiest <= 0) return 0;
  return Math.min(4, Math.max(1, Math.ceil((turns / busiest) * 4)));
}

/**
 * Every week, Monday to Sunday, that holds a day of `[since, until)`, with the
 * activity of each day. `until` is exclusive, as the server sends it.
 */
export function heatmapWeeks(
  since: string,
  until: string,
  activity: ContextActivityDay[],
): HeatmapWeek[] {
  const first = dayMs(utcDay(Date.parse(since)));
  const last = dayMs(utcDay(Date.parse(until) - 1));
  if (!Number.isFinite(first) || !Number.isFinite(last) || last < first) return [];
  const byDay = new Map(activity.map((entry) => [entry.day, entry]));
  const busiest = Math.max(0, ...activity.map((entry) => entry.turns));
  // getUTCDay: Sunday 0. Weeks start on Monday, as the server's ISO weeks do.
  const monday = first - ((new Date(first).getUTCDay() + 6) % 7) * DAY_MS;
  const weeks: HeatmapWeek[] = [];
  for (let start = monday; start <= last; start += 7 * DAY_MS) {
    const cells: HeatmapCell[] = [];
    for (let offset = 0; offset < 7; offset += 1) {
      const ms = start + offset * DAY_MS;
      const day = utcDay(ms);
      const entry = byDay.get(day) ?? null;
      cells.push({
        day,
        inRange: ms >= first && ms <= last,
        activity: entry,
        level: entry ? activityLevel(entry.turns, busiest) : 0,
      });
    }
    weeks.push({ start: utcDay(start), cells });
  }
  return weeks;
}

/** The weeks one view draws: the latest by default, `page` windows earlier. */
export function heatmapWindow(
  weeks: HeatmapWeek[],
  page: number,
  size = HEATMAP_WEEKS,
): { weeks: HeatmapWeek[]; page: number; pages: number; partial: boolean } {
  const pages = Math.max(1, Math.ceil(weeks.length / size));
  const clamped = Math.min(Math.max(0, page), pages - 1);
  const end = weeks.length - clamped * size;
  return {
    weeks: weeks.slice(Math.max(0, end - size), end),
    page: clamped,
    pages,
    partial: pages > 1,
  };
}

/** The project filter as one select value: all, one repository, or none. */
export type ProjectChoice = '' | 'temporary' | `repo:${string}`;

export function projectChoice(
  filters: Pick<ContextInsightsFilters, 'repo' | 'temporary'>,
): ProjectChoice {
  if (filters.repo) return `repo:${filters.repo}`;
  return filters.temporary ? 'temporary' : '';
}

export function projectFilters(choice: string): Pick<ContextInsightsFilters, 'repo' | 'temporary'> {
  if (choice === 'temporary') return { repo: null, temporary: true };
  if (choice.startsWith('repo:')) return { repo: choice.slice(5), temporary: false };
  return { repo: null, temporary: false };
}

/** Repositories by name, then the temporary workspaces as their own bucket. */
export function orderedProjects(projects: ContextProject[]): ContextProject[] {
  return [
    ...projects
      .filter((project) => project.repo !== null)
      .sort((a, b) => (a.repo ?? '').localeCompare(b.repo ?? '')),
    ...projects.filter((project) => project.repo === null),
  ];
}

export interface CompositionSlice {
  key: RecordedCategory;
  tokens: number;
  share: number;
}

/** The recorded categories as shares of what was recorded. */
export function recordedSlices(composition: ContextComposition): CompositionSlice[] {
  const kept = composition.recorded.categories.filter((part) => part.tokens > 0);
  const total = kept.reduce((sum, part) => sum + part.tokens, 0);
  return kept.map((part) => ({ ...part, share: total > 0 ? part.tokens / total : 0 }));
}

/** Whether any filter narrows the list. */
export function hasFilters(filters: ContextInsightsFilters): boolean {
  return Boolean(filters.q.trim() || filters.repo || filters.temporary || filters.day);
}

export const NO_FILTERS: ContextInsightsFilters = {
  q: '',
  repo: null,
  temporary: false,
  day: null,
};
