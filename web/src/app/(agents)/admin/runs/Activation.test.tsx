// @vitest-environment jsdom
import '@testing-library/jest-dom/vitest';

import { cleanup, render, screen, within } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import type { AgentAttemptTelemetryApi } from '@/lib/api/agents';

import { Activation } from './Activation';

const { getAdminAttemptResources } = vi.hoisted(() => ({
  getAdminAttemptResources: vi.fn(),
}));

vi.mock('@/lib/api/adminRuns', () => ({ getAdminAttemptResources }));

afterEach(cleanup);
beforeEach(() => {
  getAdminAttemptResources.mockReset();
});

function telemetry(overrides: Partial<AgentAttemptTelemetryApi> = {}): AgentAttemptTelemetryApi {
  return {
    attempt_id: 10,
    attempt_no: 1,
    continuity_mode: 'hot',
    context_status: 'live',
    duration_ms: 12_000,
    resources: null,
    activation: {
      total_ms: 4200,
      stages: { supervisor_acquire: 900, checkout: 2800, resident_start: 500 },
      failed_stage: null,
    },
    ...overrides,
  };
}

async function renderPanel(value: AgentAttemptTelemetryApi) {
  getAdminAttemptResources.mockResolvedValue(value);
  render(<Activation jobId="ajob_1" attemptId={10} onAccessDenied={() => {}} />);
  return within(await screen.findByRole('region', { name: 'Sandbox activation' }));
}

describe('activation panel', () => {
  it('leads with the heaviest stage, because that is the stage to look at', async () => {
    const section = await renderPanel(telemetry());

    expect(section.getByText('4.2s')).toBeVisible();
    const rows = section.getAllByRole('row').slice(1); // drop the header row
    expect(rows.map((row) => within(row).getAllByRole('cell')[0].textContent)).toEqual([
      'Repository import',
      'Container and networks',
      'Agent CLI start',
    ]);
    expect(within(rows[0]).getAllByRole('cell')[1]).toHaveTextContent('2.8s');
    expect(within(rows[0]).getAllByRole('cell')[2]).toHaveTextContent('67%');
  });

  it('says which continuity rung the total belongs to', async () => {
    const section = await renderPanel(telemetry());
    expect(section.getByText('Follow-up turn on the live Agent CLI process')).toBeInTheDocument();
  });

  it('names the continuity rung of a brand new chat instead', async () => {
    const section = await renderPanel(telemetry({ continuity_mode: null, context_status: 'new' }));
    expect(
      section.getByText('New chat: sandbox created and repository imported'),
    ).toBeInTheDocument();
  });

  it('marks a partial breakdown as partial rather than letting it read as fast', async () => {
    const section = await renderPanel(
      telemetry({
        activation: {
          total_ms: 31_000,
          stages: { supervisor_acquire: 30_800 },
          failed_stage: 'supervisor_acquire',
        },
      }),
    );

    expect(section.getByText(/The activation failed during Container and networks/)).toBeVisible();
    expect(section.getByText('31s')).toBeVisible();
  });

  it('renders a stage this build does not know rather than hiding it', async () => {
    const section = await renderPanel(
      telemetry({
        activation: {
          total_ms: 900,
          stages: { some_future_stage: 900 },
          failed_stage: null,
        },
      }),
    );
    // A newer host must not be able to measure something the page silently drops.
    expect(section.getByText('some_future_stage')).toBeVisible();
  });

  it('reports an unmeasured activation instead of an instant one', async () => {
    const section = await renderPanel(telemetry({ activation: null }));

    expect(section.getByText(/no activation measurement/)).toBeVisible();
    expect(section.queryByText('0ms')).not.toBeInTheDocument();
  });

  it('reports a total recorded without a per-stage breakdown', async () => {
    const section = await renderPanel(
      telemetry({ activation: { total_ms: 700, stages: {}, failed_stage: null } }),
    );

    expect(section.getByText('700ms')).toBeVisible();
    expect(section.getByText(/without a per-stage breakdown/)).toBeVisible();
  });
});
