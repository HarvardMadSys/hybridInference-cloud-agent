// @vitest-environment jsdom
import '@testing-library/jest-dom/vitest';

import { cleanup, render, screen, within } from '@testing-library/react';
import { afterEach, describe, expect, it } from 'vitest';

import type { ModelTtftApi } from '@/lib/api/agents';
import type { AdminSessionStatistics } from '@/lib/api/adminRuns';

import { SessionStatistics } from './SessionStatistics';

afterEach(cleanup);

const GATEWAY_TTFT: ModelTtftApi = {
  source: 'gateway_request_log',
  status: 'available',
  mean_ttft_ms: 10_400,
  samples: 4,
  requests_read: 5,
  sources_read: 1,
  sources_total: 1,
  complete: true,
  reason: null,
};

describe('turn statistics', () => {
  it('shows human-readable performance metrics separately from token totals with estimate provenance', () => {
    render(
      <SessionStatistics
        statistics={{
          model_seconds: 1667,
          tool_seconds: 6490,
          first_output_seconds: 96,
          output_tokens_per_second: 257,
          timing_source: 'message_events',
        }}
        modelTtft={GATEWAY_TTFT}
      />,
    );

    const section = within(screen.getByRole('region', { name: 'Turn statistics' }));
    expect(section.getByText('27m 47s')).toBeVisible();
    expect(section.getByText('108m 10s')).toBeVisible();
    expect(section.getByText('10.4s')).toBeVisible();
    expect(section.getByText('1m 36s')).toBeVisible();
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
    expect(section.getByText('Avg model time to first token (TTFT)')).toHaveAttribute(
      'title',
      expect.stringContaining('over 4 successful streamed requests'),
    );
    expect(section.getByText('Avg time to first output')).toHaveAttribute(
      'title',
      expect.stringMatching(
        /including sandbox, workspace and agent startup.*Not time to first token/,
      ),
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
      first_output_seconds: null,
      output_tokens_per_second: null,
      timing_source: 'message_events',
    } satisfies AdminSessionStatistics,
  ])('does not turn absent statistics into zero: %j', (statistics) => {
    render(<SessionStatistics statistics={statistics} />);
    expect(screen.getAllByText('Unavailable')).toHaveLength(5);
    expect(screen.queryByText('0ms')).not.toBeInTheDocument();
    expect(screen.queryByText('0 tok/s')).not.toBeInTheDocument();
  });

  it('preserves measured zero and subsecond values when other metrics are missing', () => {
    render(
      <SessionStatistics
        statistics={{
          model_seconds: null,
          tool_seconds: 0,
          first_output_seconds: 0.25,
          output_tokens_per_second: 0,
          timing_source: 'message_events',
        }}
      />,
    );
    expect(screen.getAllByText('Unavailable')).toHaveLength(2);
    expect(screen.getByText('0ms')).toBeVisible();
    expect(screen.getByText('250ms')).toBeVisible();
    expect(screen.getByText('0 tok/s')).toBeVisible();
  });

  it('never shows startup-inclusive first output as TTFT when the gateway has no timing', () => {
    render(
      <SessionStatistics
        statistics={{
          model_seconds: 10,
          tool_seconds: 1,
          first_output_seconds: 96,
          output_tokens_per_second: null,
          timing_source: 'message_events',
        }}
        modelTtft={{ ...GATEWAY_TTFT, status: 'not_recorded', mean_ttft_ms: null, samples: 0 }}
      />,
    );
    const ttft = screen.getByText('Avg model time to first token (TTFT)').closest('div');
    expect(within(ttft!).getByText('Unavailable')).toBeVisible();
    expect(screen.getByText('1m 36s')).toBeVisible();
  });

  it('marks a partial gateway sample as partial', () => {
    render(
      <SessionStatistics
        statistics={null}
        modelTtft={{ ...GATEWAY_TTFT, complete: false, reason: 'gateway_unavailable' }}
      />,
    );
    expect(screen.getByText('10.4s (partial)')).toBeVisible();
    expect(screen.getByText('Avg model time to first token (TTFT)')).toHaveAttribute(
      'title',
      expect.stringContaining('Partial: the gateway could not be read for every run.'),
    );
  });
});
