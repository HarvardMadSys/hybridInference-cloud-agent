// @vitest-environment jsdom
import '@testing-library/jest-dom/vitest';

import { cleanup, render, screen, within } from '@testing-library/react';
import { afterEach, describe, expect, it } from 'vitest';

import type { AdminSessionStatistics } from '@/lib/api/adminRuns';

import { SessionStatistics } from './SessionStatistics';

afterEach(cleanup);

describe('turn statistics', () => {
  it('shows human-readable performance metrics separately from token totals with estimate provenance', () => {
    render(
      <SessionStatistics
        statistics={{
          model_seconds: 1667,
          tool_seconds: 6490,
          ttft_seconds: 2.9,
          output_tokens_per_second: 257,
          timing_source: 'message_events',
        }}
      />,
    );

    const section = within(screen.getByRole('region', { name: 'Turn statistics' }));
    expect(section.getByText('27m 47s')).toBeVisible();
    expect(section.getByText('108m 10s')).toBeVisible();
    expect(section.getByText('2.9s')).toBeVisible();
    expect(section.getByText('257 tok/s')).toBeVisible();
    expect(section.queryByText(/Estimates from recorded activity/)).not.toBeInTheDocument();
    expect(section.getByText('LLM time')).toHaveAttribute(
      'title',
      expect.stringContaining('overhead'),
    );
    expect(section.getByText('Tool time')).toHaveAttribute(
      'title',
      expect.stringContaining('Overlapping intervals are counted once'),
    );
    expect(section.getByText('Avg time to first token (TTFT)')).toHaveAttribute(
      'title',
      expect.stringContaining('averaged over runs with a recorded sample'),
    );
    expect(section.getByText('Tokens per second (TPS)')).toHaveAttribute(
      'title',
      expect.stringContaining('Runtime-reported output tokens'),
    );
  });

  it.each([
    undefined,
    null,
    {
      model_seconds: null,
      tool_seconds: null,
      ttft_seconds: null,
      output_tokens_per_second: null,
      timing_source: 'message_events',
    } satisfies AdminSessionStatistics,
  ])('does not turn absent statistics into zero: %j', (statistics) => {
    render(<SessionStatistics statistics={statistics} />);
    expect(screen.getAllByText('Unavailable')).toHaveLength(4);
    expect(screen.queryByText('0ms')).not.toBeInTheDocument();
    expect(screen.queryByText('0 tok/s')).not.toBeInTheDocument();
  });

  it('preserves measured zero and subsecond values when other metrics are missing', () => {
    render(
      <SessionStatistics
        statistics={{
          model_seconds: null,
          tool_seconds: 0,
          ttft_seconds: 0.25,
          output_tokens_per_second: 0,
          timing_source: 'message_events',
        }}
      />,
    );
    expect(screen.getByText('Unavailable')).toBeVisible();
    expect(screen.getByText('0ms')).toBeVisible();
    expect(screen.getByText('250ms')).toBeVisible();
    expect(screen.getByText('0 tok/s')).toBeVisible();
  });
});
