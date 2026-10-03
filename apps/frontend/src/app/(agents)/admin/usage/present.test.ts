import { describe, expect, it } from 'vitest';

import { money, metric, number, utcDate } from '@/app/(agents)/admin/runs/shared';
import {
  activityHref,
  presetKey,
  runsHref,
  selectionFromQuery,
  urlFor,
  windowFor,
} from '@/app/(agents)/admin/usage/present';

describe('admin usage window', () => {
  const now = new Date('2026-09-10T12:00:00Z');

  it('builds an absolute window from a preset relative to one instant', () => {
    expect(windowFor(7, now)).toEqual({
      since: '2026-09-03T12:00:00.000Z',
      until: '2026-09-10T12:00:00.000Z',
    });
  });

  it('reads the whitelisted filters back out of a URL', () => {
    expect(
      selectionFromQuery('since=2026-09-01T00:00:00Z&until=2026-09-08T00:00:00Z&model=a').filters,
    ).toEqual({
      since: '2026-09-01T00:00:00Z',
      until: '2026-09-08T00:00:00Z',
      model: 'a',
    });
  });

  it('reports an unusable link instead of quietly widening it', () => {
    // A missing end, an inverted window and an unknown parameter are all
    // errors: answering with a default window would describe other runs.
    for (const query of [
      'since=2026-09-01T00:00:00Z',
      'until=2026-09-01T00:00:00Z',
      'since=2026-09-08T00:00:00Z&until=2026-09-01T00:00:00Z',
      'since=2026-09-01T00:00:00Z&until=2026-09-08T00:00:00Z&host=x',
      'model=a',
    ]) {
      const selection = selectionFromQuery(query);
      expect(selection.filters, query).toBeNull();
      expect(selection.error, query).toBeTruthy();
      expect(selection.linked, query).toBe(true);
    }
  });

  it('treats a URL with no selection as no selection, not as an error', () => {
    expect(selectionFromQuery('')).toEqual({ filters: null, error: null, linked: false });
  });

  it('writes the selection under the path it is actually served on', () => {
    expect(urlFor('/agents/admin/usage', { since: 'a', until: 'b', model: 'm' })).toBe(
      '/agents/admin/usage?since=a&until=b&model=m',
    );
  });

  it('names the window by its span, not by the last option clicked', () => {
    // A linked one-day window is Custom: reporting it as seven days would
    // re-anchor it to seven the next time any other filter changed.
    const oneDay = { since: '2026-09-09T00:00:00.000Z', until: '2026-09-10T00:00:00.000Z' };
    expect(presetKey(oneDay, now)).toBe('custom');
    expect(presetKey(windowFor(30, now), now)).toBe('30');
  });

  it('recognizes its own presets and calls anything else custom', () => {
    const seven = windowFor(7, now);
    expect(presetKey(seven, new Date(now.getTime() + 500))).toBe('7');
    expect(presetKey({ ...seven, since: '2026-08-01T00:00:00Z' }, now)).toBe('custom');
    expect(presetKey({}, now)).toBe('custom');
  });

  it('keeps every applied filter when linking to a group', () => {
    const filters = {
      since: '2026-09-03T00:00:00Z',
      until: '2026-09-10T00:00:00Z',
      repo: 'org/repo',
    };
    const href = runsHref(filters, { dimension: 'model', value: 'model a/b' })!;
    const params = new URLSearchParams(href.slice(href.indexOf('?') + 1));
    expect(params.get('since')).toBe(filters.since);
    expect(params.get('until')).toBe(filters.until);
    expect(params.get('repo')).toBe('org/repo');
    expect(params.get('model')).toBe('model a/b');
  });

  it('links a day row to that day rather than to the whole selection', () => {
    const href = runsHref(
      { since: '2026-09-03T00:00:00Z', until: '2026-09-10T00:00:00Z' },
      { dimension: 'day', since: '2026-09-04T00:00:00Z', until: '2026-09-05T00:00:00Z' },
    )!;
    const params = new URLSearchParams(href.slice(href.indexOf('?') + 1));
    expect(params.get('since')).toBe('2026-09-04T00:00:00Z');
    expect(params.get('until')).toBe('2026-09-05T00:00:00Z');
  });

  it('opens Activity on the whole selection, leaving out empty filters', () => {
    const href = activityHref({
      since: '2026-09-21T00:00:00.000Z',
      until: '2026-09-28T00:00:00.000Z',
      model: 'glm-5.1',
      repo: '',
    });
    expect(href).toBe(
      '/admin/activity?since=2026-09-21T00%3A00%3A00.000Z&until=2026-09-28T00%3A00%3A00.000Z&model=glm-5.1',
    );
  });

  it('offers no link for an unknown identity or one no filter can express', () => {
    expect(runsHref({}, { dimension: 'model', value: null })).toBeNull();
    expect(runsHref({}, { dimension: 'user_id', value: '' })).toBeNull();
    expect(runsHref({}, { dimension: 'user_id', value: 'x'.repeat(257) })).toBeNull();
    expect(runsHref({}, { dimension: 'day' })).toBeNull();
  });
});

describe('admin money and date formatting', () => {
  it('keeps four places and never rounds the exact value away', () => {
    expect(money('0.00978569')).toEqual({ display: '$0.0097', exact: '0.00978569' });
    expect(money('12.5')).toEqual({ display: '$12.5', exact: '12.5' });
    expect(money('1234.5678')).toEqual({ display: '$1,234.5678', exact: '1234.5678' });
  });

  it('separates a tiny non-zero amount from a true zero', () => {
    expect(money('0.00001').display).toBe('<$0.0001');
    expect(money('0.00001').exact).toBe('0.00001');
    expect(money('0').display).toBe('$0');
    expect(money('0.00').display).toBe('$0');
  });

  it('never renders an unknown amount as a zero', () => {
    expect(money(null).display).toBe('Unavailable');
    expect(money(undefined).display).toBe('Unavailable');
    expect(money('not-a-number').display).toBe('Unavailable');
    expect(money('-1').display).toBe('Unavailable');
  });

  it('renders an instant in UTC and says so', () => {
    expect(utcDate('2026-09-03T23:30:00Z')).toBe('Sep 3, 2026');
    expect(number(1234)).toBe('1,234');
    expect(number(null)).toBe('Unavailable');
  });

  it('states how much of a metric is unknown next to its known sum', () => {
    expect(metric(120, 0)).toBe('120');
    expect(metric(120, undefined)).toBe('120');
    expect(metric(120, 3)).toBe('120 (3 unknown)');
    expect(metric(null, 3)).toBe('Unavailable');
  });
});
