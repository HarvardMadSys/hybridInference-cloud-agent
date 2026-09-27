// @vitest-environment jsdom
import '@testing-library/jest-dom/vitest';

import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import type { MeasurementDetail, MeasurementSummary } from '@/lib/api/adminMeasurements';
import { APIError } from '@/lib/utils/errors';

const measurements = vi.hoisted(() => ({
  listMeasurements: vi.fn(),
  getMeasurement: vi.fn(),
}));
vi.mock('@/lib/api/adminMeasurements', () => ({
  listMeasurements: measurements.listMeasurements,
  getMeasurement: measurements.getMeasurement,
}));
vi.mock('next/link', () => ({
  default: ({ children, href }: { children: React.ReactNode; href: string }) => (
    <a href={href}>{children}</a>
  ),
}));

import { getMeasurement, listMeasurements } from '@/lib/api/adminMeasurements';
import { StartupMeasurement } from './StartupMeasurement';

function row(overrides: Partial<MeasurementSummary> = {}): MeasurementSummary {
  return {
    measurement_id: 'msm_1',
    interaction_id: 'msm_1',
    profiling_run_id: 'run-1',
    schema_version: 3,
    owner_id: 'user-1',
    job_id: 'job-1',
    thread_id: 'thread-1',
    sandbox_id: 'asbx_1',
    host_id: 'host-a',
    attempt_id: 7,
    lease_generation: 1,
    operation: 'attempt.startup',
    request_kind: 'attempt.startup',
    boundary_version: 1,
    coverage_scope: 'host_startup',
    producer: 'runner',
    producer_instance_id: 'runner-1',
    duration_us: 250_000,
    outcome: 'dispatched',
    collection_complete: true,
    end_to_end_complete: false,
    startup_path: 'new_group',
    runtime_id: 'claude-code',
    runtime_version: '2.1.220',
    execution_profile: 'trusted-v1',
    observed_at: '2026-09-18T08:00:00Z',
    received_at: '2026-09-18T08:00:01Z',
    ...overrides,
  };
}

function detail(overrides: Partial<MeasurementDetail> = {}): MeasurementDetail {
  return {
    ...row(),
    payload_hash: 'hash-1',
    record: {
      schema_version: 3,
      measurement_id: 'msm_1',
      interaction_id: 'msm_1',
      profiling_run_id: 'run-1',
      operation: 'attempt.startup',
      boundary_version: 1,
      coverage_scope: 'host_startup',
      producer: 'runner',
      producer_instance_id: 'runner-1',
      duration_us: 250_000,
      outcome: 'dispatched',
      collection_complete: true,
      end_to_end_complete: false,
      startup_path: 'new_group',
      observed_at: '2026-09-18T08:00:00Z',
      stages: [
        { stage: 'claim', start_offset_us: 0, duration_us: 200_000 },
        { stage: 'prompt_dispatch', start_offset_us: 200_000, duration_us: 50_000 },
      ],
      spans: [],
      milestones: [],
      missing_fragments: [],
      truncated_fragments: [],
      // A count, not a list: the record reports how many spans collection gave
      // up on, and zero is a statement rather than an absence.
      dropped_spans: 0,
    },
    ...overrides,
  };
}

function renderPanel(onAccessDenied: () => void = () => undefined) {
  return render(<StartupMeasurement jobId="job-1" attemptId={7} onAccessDenied={onAccessDenied} />);
}

async function pageOf(rows: MeasurementSummary[], emptyReason?: string) {
  vi.mocked(listMeasurements).mockResolvedValue({
    measurements: rows,
    count: rows.length,
    limit: 5,
    next_cursor: null,
    ...(emptyReason ? { empty_reason: emptyReason } : {}),
  });
}

beforeEach(() => {
  vi.clearAllMocks();
});
afterEach(cleanup);

describe('the startup panel on a turn detail', () => {
  it('asks for this attempt start-up and nothing wider', async () => {
    await pageOf([]);
    renderPanel();

    await screen.findByText(/No saved startup measurement/);
    const filters = vi.mocked(listMeasurements).mock.calls[0][0];
    expect(filters.job_id).toBe('job-1');
    // The precise key, not a job-wide read that happens to contain this row.
    expect(filters.attempt_id).toBe(7);
    expect(filters.operation).toEqual(['attempt.startup']);
  });

  it('says nothing was stored without rendering a zero duration', async () => {
    await pageOf([], 'the experiment may be off, unselected, undelivered');
    renderPanel();

    await screen.findByText(/No saved startup measurement for this turn/);
    expect(screen.getByText(/may be off, unselected, undelivered/)).toBeInTheDocument();
    // The absence has causes the response cannot distinguish, and the panel
    // says so rather than implying the startup was instantaneous. The sentence
    // is split by an emphasised "not", so it is matched across elements.
    expect(
      screen.getByText(
        (_content, element) =>
          element?.tagName === 'P' &&
          /is not a fast startup and not a failed one/.test(element.textContent ?? ''),
      ),
    ).toBeInTheDocument();
    expect(screen.queryByText('0 ms')).not.toBeInTheDocument();
  });

  it('shows the duration, outcome and real startup path of a stored record', async () => {
    await pageOf([row()]);
    vi.mocked(getMeasurement).mockResolvedValue(detail());
    renderPanel();

    await screen.findByText('250 ms');
    expect(screen.getByText('Dispatched')).toBeInTheDocument();
    expect(screen.getByText('new_group')).toBeInTheDocument();
    expect(screen.getByText(/Runner-rooted: it covers the claim/)).toBeInTheDocument();
  });

  it('states the coverage the record actually reached, not end-to-end latency', async () => {
    await pageOf([row()]);
    vi.mocked(getMeasurement).mockResolvedValue(detail());
    renderPanel();

    await screen.findByText(/Host startup/);
    expect(screen.getByText(/not claimed \(server-side only\)/)).toBeInTheDocument();
  });

  it('does not invent a startup path the record could not name', async () => {
    await pageOf([row({ startup_path: null })]);
    renderPanel();

    await screen.findByText('Not reported');
    expect(screen.queryByText('new_group')).not.toBeInTheDocument();
  });

  it('does not read an unknown startup path as one of the four', async () => {
    await pageOf([row({ startup_path: 'unknown' })]);
    renderPanel();

    await screen.findByText('Unclassified');
  });

  it('lists every reason a partial record is partial', async () => {
    await pageOf([row({ collection_complete: false, outcome: 'failed' })]);
    vi.mocked(getMeasurement).mockResolvedValue(
      detail({
        collection_complete: false,
        outcome: 'failed',
        record: {
          ...detail().record,
          collection_complete: false,
          outcome: 'failed',
          failure_stage: 'container_start',
          missing_fragments: ['broker:c1:resident_start'],
        },
      }),
    );
    renderPanel();

    // The same fact appears in the summary line and in the partial-reason list,
    // which is deliberate: the reason is where a reader looks for the cause.
    await screen.findAllByText(/Collection did not close/);
    expect(screen.getByText(/Outcome Failed at stage “container_start”/)).toBeInTheDocument();
    expect(screen.getByText(/broker:c1:resident_start/)).toBeInTheDocument();
  });

  it('says a second record is a retry rather than averaging the two', async () => {
    await pageOf([row(), row({ measurement_id: 'msm_2', duration_us: 400_000 })]);
    vi.mocked(getMeasurement).mockResolvedValue(detail());
    renderPanel();

    await screen.findByText(/2 startup records are stored for this turn/);
    expect(screen.getByText(/rather than\s+average them|rather than/)).toBeInTheDocument();
  });

  it('keeps the summary when the detail read fails, instead of blanking the panel', async () => {
    await pageOf([row()]);
    vi.mocked(getMeasurement).mockRejectedValue(new APIError('SERVER_ERROR', 'boom', 500));
    renderPanel();

    await screen.findByText('250 ms');
    expect(screen.queryByRole('alert')).not.toBeInTheDocument();
  });

  it('hands a refused detail read to the page instead of keeping the records', async () => {
    // Revocation is not a failed detail read. Swallowing it would leave the
    // summary of a session that is no longer allowed to see it on screen and
    // would tell the page above nothing.
    await pageOf([row()]);
    vi.mocked(getMeasurement).mockRejectedValue(new APIError('INVALID_TOKEN', 'no', 403));
    const onAccessDenied = vi.fn();
    renderPanel(onAccessDenied);

    await waitFor(() => expect(onAccessDenied).toHaveBeenCalledTimes(1));
  });

  it('hands a refused list read to the page too', async () => {
    vi.mocked(listMeasurements).mockRejectedValue(new APIError('INVALID_TOKEN', 'no', 401));
    const onAccessDenied = vi.fn();
    renderPanel(onAccessDenied);

    await waitFor(() => expect(onAccessDenied).toHaveBeenCalledTimes(1));
  });

  it('reports a failed list read rather than showing an empty panel', async () => {
    vi.mocked(listMeasurements).mockRejectedValue(new APIError('SERVER_ERROR', 'boom', 500));
    renderPanel();

    await screen.findByRole('alert');
    expect(screen.queryByText(/No saved startup measurement/)).not.toBeInTheDocument();
  });

  it('links to the full selection so the same rows can be read in context', async () => {
    await pageOf([row()]);
    vi.mocked(getMeasurement).mockResolvedValue(detail());
    renderPanel();

    await screen.findByText(/Every stored record for this turn/);
    const link = screen.getByRole('link', { name: /Every stored record for this turn/ });
    expect(link).toHaveAttribute('href', expect.stringContaining('attempt_id=7'));
    expect(link).toHaveAttribute('href', expect.stringContaining('job_id=job-1'));
  });

  it('re-reads the attempt when the refresh button is pressed', async () => {
    await pageOf([row()]);
    vi.mocked(getMeasurement).mockResolvedValue(detail());
    renderPanel();
    await screen.findByText('250 ms');

    fireEvent.click(screen.getByRole('button', { name: 'Refresh' }));

    await waitFor(() => expect(listMeasurements).toHaveBeenCalledTimes(2));
  });
});
