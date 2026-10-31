import { describe, expect, it } from 'vitest';

import type { ContextComposition } from '@/lib/api/ownerUsage';
import {
  HEATMAP_WEEKS,
  activityLevel,
  heatmapWeeks,
  heatmapWindow,
  projectChoice,
  projectFilters,
  recordedSlices,
} from './contextInsightsPresent';

function day(date: string, turns: number) {
  return {
    day: date,
    turns,
    conversations: 1,
    main_requests: turns,
    compactions: 0,
    peak_prompt_tokens: null,
  };
}

describe('heatmapWeeks', () => {
  it('covers every UTC day of the range in Monday-first weeks', () => {
    // Wed 2026-10-07 to Thu 2026-10-29 (until is exclusive: the 30th starts at 00:00).
    const weeks = heatmapWeeks('2026-10-07T00:00:00Z', '2026-10-29T12:00:00Z', [
      day('2026-10-07', 2),
      day('2026-10-29', 8),
    ]);
    expect(weeks[0].start).toBe('2026-10-05');
    expect(weeks.at(-1)!.start).toBe('2026-10-26');
    const cells = weeks.flatMap((week) => week.cells);
    const inRange = cells.filter((cell) => cell.inRange);
    expect(inRange[0].day).toBe('2026-10-07');
    expect(inRange.at(-1)!.day).toBe('2026-10-29');
    expect(inRange).toHaveLength(23);
    // Monday and Tuesday of the first week are not part of the range.
    expect(cells.slice(0, 2).every((cell) => !cell.inRange)).toBe(true);
    expect(cells.find((cell) => cell.day === '2026-10-29')!.level).toBe(4);
    expect(cells.find((cell) => cell.day === '2026-10-07')!.level).toBe(1);
  });

  it('draws all of a long history in windows and never drops a day', () => {
    const activity = [day('2023-01-02', 1), day('2026-10-28', 3)];
    const weeks = heatmapWeeks('2023-01-02T00:00:00Z', '2026-10-29T12:00:00Z', activity);
    expect(weeks.length).toBeGreaterThan(190);
    const latest = heatmapWindow(weeks, 0);
    expect(latest.partial).toBe(true);
    expect(latest.weeks).toHaveLength(HEATMAP_WEEKS);
    expect(latest.weeks.at(-1)).toBe(weeks.at(-1));
    // Paging back reaches the very first week, and the windows together hold every week.
    const seen = new Set<string>();
    for (let page = 0; page < latest.pages; page += 1) {
      heatmapWindow(weeks, page).weeks.forEach((week) => seen.add(week.start));
    }
    expect(seen.size).toBe(weeks.length);
    expect(heatmapWindow(weeks, latest.pages + 5).weeks[0]).toBe(weeks[0]);
    const first = heatmapWindow(weeks, latest.pages - 1).weeks.flatMap((week) => week.cells);
    expect(first.find((cell) => cell.day === '2023-01-02')!.activity?.turns).toBe(1);
  });

  it('is one short week for today', () => {
    const weeks = heatmapWeeks('2026-10-29T00:00:00Z', '2026-10-29T12:00:00Z', []);
    expect(weeks).toHaveLength(1);
    expect(weeks[0].cells.filter((cell) => cell.inRange).map((cell) => cell.day)).toEqual([
      '2026-10-29',
    ]);
    expect(heatmapWindow(weeks, 0).partial).toBe(false);
  });

  it('gives any turn at least the lightest shade', () => {
    expect(activityLevel(0, 10)).toBe(0);
    expect(activityLevel(1, 1000)).toBe(1);
    expect(activityLevel(1000, 1000)).toBe(4);
  });
});

describe('project choice', () => {
  it('round-trips all, one repository and the no-repository bucket', () => {
    for (const filters of [
      { repo: null, temporary: false },
      { repo: 'acme/web', temporary: false },
      { repo: null, temporary: true },
    ]) {
      expect(projectFilters(projectChoice(filters))).toEqual(filters);
    }
    // A repository literally named "temporary" is still a repository.
    expect(projectFilters(projectChoice({ repo: 'temporary', temporary: false }))).toEqual({
      repo: 'temporary',
      temporary: false,
    });
  });
});

describe('composition slices', () => {
  const composition: ContextComposition = {
    recorded: {
      basis: 'chars_cjk_v1',
      categories: [
        { key: 'turn_prompts', tokens: 100 },
        { key: 'tool_results', tokens: 300 },
      ],
      total_tokens: 400,
      turns: 2,
      turns_profiled: 2,
      turns_truncated: 0,
    },
    provider: {
      requests: 3,
      prompt_tokens: 10_000,
      cache_read_tokens: 7_000,
      cache_write_tokens: 1_000,
      output_tokens: 50,
      output_unreported: 0,
      subagent_requests: 0,
      subagent_prompt_tokens: 0,
      subagent_output_tokens: 0,
    },
  };

  it('shares the recorded categories of what was recorded, never of reported input', () => {
    expect(recordedSlices(composition).map((slice) => [slice.key, slice.share])).toEqual([
      ['turn_prompts', 0.25],
      ['tool_results', 0.75],
    ]);
  });
});
