// @vitest-environment jsdom
import '@testing-library/jest-dom/vitest';

import { cleanup, render, screen } from '@testing-library/react';
import { afterEach, describe, expect, it } from 'vitest';

import { ago, RunState } from './shared';

afterEach(cleanup);

describe('list row helpers', () => {
  const now = Date.parse('2026-09-28T12:00:00Z');

  it('says how long ago in the few characters a row can afford', () => {
    expect(ago('2026-09-28T11:59:40Z', now)).toBe('just now');
    expect(ago('2026-09-28T11:56:00Z', now)).toBe('4m ago');
    expect(ago('2026-09-28T07:00:00Z', now)).toBe('5h ago');
    expect(ago('2026-09-25T12:00:00Z', now)).toBe('3d ago');
    // Past a week, "days ago" stops meaning much: the UTC date instead.
    expect(ago('2026-09-14T23:30:00Z', now)).toBe('Sep 14');
    expect(ago(null, now)).toBe('Unavailable');
  });

  it('shows a quiet state as a dot and a word, without a pill', () => {
    render(<RunState state="failed" quiet />);
    const state = screen.getByText('failed');
    expect(state.className).not.toMatch(/ring-1|rounded-full/);
    expect(state.querySelector('[aria-hidden]')).toHaveClass('rounded-full');
  });
});
