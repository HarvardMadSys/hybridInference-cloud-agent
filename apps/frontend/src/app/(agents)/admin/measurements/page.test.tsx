// @vitest-environment jsdom
import '@testing-library/jest-dom/vitest';

import { act, cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { useSyncExternalStore } from 'react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import type {
  MeasurementDetail,
  MeasurementPage,
  MeasurementRecord,
  MeasurementSummary,
  MeasurementSummaryGroup,
  ProfilingRun,
} from '@/lib/api/adminMeasurements';
import type { AgentAccessApi } from '@/lib/api/session';
import { APIError } from '@/lib/utils/errors';

const auth = vi.hoisted(() => ({ access: undefined as AgentAccessApi | undefined, id: 'admin-1' }));
// A reactive stand-in for the router: setting ``value`` notifies subscribers,
// which is how a real URL change re-renders the mounted page.
const search = vi.hoisted(() => {
  const listeners = new Set<() => void>();
  return {
    value: '',
    subscribe(listener: () => void) {
      listeners.add(listener);
      return () => listeners.delete(listener);
    },
    read: () => search.value,
    set(next: string) {
      search.value = next;
      for (const listener of listeners) listener();
    },
  };
});
vi.mock('@/components/providers', () => ({
  useAuth: () => ({ state: { user: { id: auth.id, role: 'admin', agent_access: auth.access } } }),
}));
vi.mock('@/components/providers/AuthProvider', async () => {
  const actual = await vi.importActual<typeof import('@/components/providers/AuthProvider')>(
    '@/components/providers/AuthProvider',
  );
  return { hasAgentPermission: actual.hasAgentPermission };
});
vi.mock('next/navigation', () => ({
  useSearchParams: () => new URLSearchParams(useSyncExternalStore(search.subscribe, search.read)),
}));
vi.mock('@/lib/api/adminMeasurements', () => ({
  listProfilingRuns: vi.fn(),
  listMeasurements: vi.fn(),
  summarizeProfilingRun: vi.fn(),
  getMeasurement: vi.fn(),
  getMeasurementWriterStatus: vi.fn(),
}));

import {
  getMeasurement,
  getMeasurementWriterStatus,
  listMeasurements,
  listProfilingRuns,
  summarizeProfilingRun,
} from '@/lib/api/adminMeasurements';
import AdminMeasurementsPage from './page';

function run(overrides: Partial<ProfilingRun> = {}): ProfilingRun {
  return {
    run_id: 'run-1',
    operations: ['files.list', 'files.open'],
    target: { host: 'host-a' },
    expires_at: '2026-09-17T11:00:00Z',
    config_version: 1,
    admission_instances: [{ instance: 'cp-1', max_operations: 500 }],
    created_at: '2026-09-17T10:00:00Z',
    received_at: '2026-09-17T10:00:05Z',
    ...overrides,
  };
}

function row(overrides: Partial<MeasurementSummary> = {}): MeasurementSummary {
  return {
    measurement_id: 'msm_1',
    interaction_id: 'msm_1',
    profiling_run_id: 'run-1',
    schema_version: 3,
    owner_id: 'user-1',
    job_id: 'ajob_1',
    thread_id: 'athr_1',
    sandbox_id: 'asbx_1',
    host_id: 'host-a',
    attempt_id: null,
    lease_generation: null,
    operation: 'files.list',
    request_kind: 'files.list',
    boundary_version: 1,
    coverage_scope: 'server_request',
    producer: 'control-plane',
    producer_instance_id: 'cp-1',
    duration_us: 2500,
    outcome: 'completed',
    collection_complete: true,
    end_to_end_complete: false,
    startup_path: null,
    runtime_id: null,
    runtime_version: null,
    execution_profile: null,
    observed_at: '2026-09-17T10:00:00Z',
    received_at: '2026-09-17T10:00:01Z',
    ...overrides,
  };
}

function record(overrides: Partial<MeasurementRecord> = {}): MeasurementRecord {
  return {
    schema_version: 3,
    measurement_id: 'msm_1',
    interaction_id: 'msm_1',
    profiling_run_id: 'run-1',
    operation: 'files.list',
    boundary_version: 1,
    coverage_scope: 'server_request',
    end_to_end_complete: false,
    producer: 'control-plane',
    producer_instance_id: 'cp-1',
    clock_domain_id: 'host-abc',
    producers: ['control-plane', 'broker'],
    timing_sources: ['control-plane'],
    outcome: 'completed',
    duration_us: 2500,
    collection_complete: true,
    boundary_frozen: true,
    observed_at: '2026-09-17T10:00:00Z',
    milestones: [{ name: 'request.received', offset_us: 0, source: 'control-plane' }],
    stages: [
      { stage: 'resolve', start_offset_us: 0, duration_us: 500 },
      {
        stage: 'read',
        start_offset_us: 500,
        duration_us: 2000,
        children: [{ stage: 'broker.fetch', duration_us: 1800, outcome: 'ok', source: 'broker' }],
      },
    ],
    facts: { request_kind: 'files.list' },
    ...overrides,
  };
}

function detail(overrides: Partial<MeasurementDetail> = {}): MeasurementDetail {
  return { ...row(), record: record(), payload_hash: 'sha256:abc', ...overrides };
}

function group(overrides: Partial<MeasurementSummaryGroup> = {}): MeasurementSummaryGroup {
  return {
    group: {
      operation: 'files.list',
      request_kind: 'files.list',
      boundary_version: 1,
      coverage_scope: 'server_request',
      startup_path: null,
      runtime_id: null,
      runtime_version: null,
      execution_profile: null,
    },
    sample_count: 30,
    interaction_count: 12,
    eligible_count: 25,
    incomplete_count: 3,
    failed_count: 2,
    p50_us: 2500,
    p95_us: 9000,
    min_us: 500,
    max_us: 20000,
    ...overrides,
  };
}

function page(overrides: Partial<MeasurementPage> = {}): MeasurementPage {
  return { measurements: [row()], count: 1, limit: 50, next_cursor: null, ...overrides };
}

beforeEach(() => {
  vi.clearAllMocks();
  search.value = '';
  auth.access = { allowed: true, permissions: ['agent.use', 'agent.admin'] };
  auth.id = 'admin-1';
  vi.mocked(listProfilingRuns).mockResolvedValue({ runs: [run()], count: 1, limit: 50 });
  vi.mocked(listMeasurements).mockResolvedValue(page());
  vi.mocked(summarizeProfilingRun).mockResolvedValue({ run_id: 'run-1', groups: [group()] });
  vi.mocked(getMeasurement).mockResolvedValue(detail());
  vi.mocked(getMeasurementWriterStatus).mockResolvedValue({
    scope: 'this_control_plane_process',
    running: true,
    pending: 0,
    in_flight: 0,
    queued_bytes: 0,
    counts: {
      stored: 1,
      duplicate: 0,
      rejected: 0,
      conflict: 0,
      dropped_full: 0,
      dropped_timeout: 0,
      dropped_shutdown: 0,
      failed: 0,
      reasons: {},
    },
  });
});
afterEach(cleanup);

describe('admin measurements page', () => {
  it.each<AgentAccessApi | undefined>([
    undefined,
    { allowed: false, permissions: ['agent.admin'] },
    { allowed: true, permissions: ['agent.use'] },
  ])('does not read stored measurements without a current admin permission: %j', (access) => {
    auth.access = access;
    render(<AdminMeasurementsPage />);
    expect(screen.getByText('Admin access required')).toBeInTheDocument();
    expect(listProfilingRuns).not.toHaveBeenCalled();
    expect(listMeasurements).not.toHaveBeenCalled();
    expect(summarizeProfilingRun).not.toHaveBeenCalled();
    expect(getMeasurement).not.toHaveBeenCalled();
    expect(getMeasurementWriterStatus).not.toHaveBeenCalled();
  });

  it('reads the newest experiments and one page of samples, and says what it covers', async () => {
    render(<AdminMeasurementsPage />);
    await screen.findByText('msm_1');

    expect(vi.mocked(listProfilingRuns).mock.calls[0][0]).toEqual({ limit: 50 });
    // No experiment in the URL reads across experiments; the filter is absent
    // rather than sent as an empty selector.
    expect(vi.mocked(listMeasurements).mock.calls[0][0]).toMatchObject({ run_id: undefined });
    // A distribution is about one experiment, so nothing asks for one yet.
    expect(summarizeProfilingRun).not.toHaveBeenCalled();
    expect(getMeasurement).not.toHaveBeenCalled();
    expect(getMeasurementWriterStatus).toHaveBeenCalledTimes(1);

    const nav = screen.getByRole('navigation', { name: 'Admin sections' });
    expect(within(nav).getByRole('link', { name: 'Measurements' })).toHaveAttribute(
      'aria-current',
      'page',
    );
    // Server coverage is stated before any number is read.
    expect(screen.getByText('What these measurements cover')).toBeInTheDocument();
    expect(screen.getByText(/Server-side measurements only/)).toBeInTheDocument();
    expect(screen.getByText(/Browser-to-server time is not measured/)).toBeInTheDocument();
    expect(screen.getByRole('heading', { name: 'Local persistence writer' })).toBeInTheDocument();
    expect(screen.getByText('Running')).toBeInTheDocument();
  });

  it('links an experiment, and once selected reads its manifest, samples and distribution', async () => {
    render(<AdminMeasurementsPage />);
    await screen.findByText('msm_1');
    const experiments = screen.getByRole('heading', { name: 'Experiments' }).closest('section')!;
    expect(within(experiments).getByRole('link', { name: 'run-1' })).toHaveAttribute(
      'href',
      '/admin/measurements?run=run-1',
    );

    search.set('run=run-1');
    await act(async () => {});
    await waitFor(() => expect(summarizeProfilingRun).toHaveBeenCalledTimes(1));
    expect(vi.mocked(summarizeProfilingRun).mock.calls[0][0]).toBe('run-1');
    expect(vi.mocked(listMeasurements).mock.calls.at(-1)?.[0]).toMatchObject({ run_id: 'run-1' });
    expect(await screen.findByText('cp-1 (max_operations=500)')).toBeInTheDocument();
  });

  it('does not render an empty page as zero latency', async () => {
    vi.mocked(listMeasurements).mockResolvedValue(
      page({
        measurements: [],
        count: 0,
        empty_reason:
          'no stored samples match this selection; the experiment may be off, unselected, undelivered',
      }),
    );
    render(<AdminMeasurementsPage />);
    await screen.findByText(/No stored samples match this selection/);
    expect(screen.getByText(/An empty page is not zero latency/)).toBeInTheDocument();
    expect(screen.getByText(/collection switched off/)).toBeInTheDocument();
    expect(screen.getByText(/older build/)).toBeInTheDocument();
    // No row, and no table of zeros standing in for one.
    expect(screen.queryByText('0 µs')).not.toBeInTheDocument();
    expect(screen.queryByRole('link', { name: 'Open record' })).not.toBeInTheDocument();
  });

  it('never presents a page as the population', async () => {
    vi.mocked(listMeasurements).mockResolvedValue(
      page({ count: 50, limit: 50, next_cursor: 'msm_50' }),
    );
    render(<AdminMeasurementsPage />);
    await screen.findByText(/50 of at most 50 rows/);
    expect(screen.getByText(/A page is not the population/)).toBeInTheDocument();
    expect(screen.getByText(/More rows follow/)).toBeInTheDocument();
  });

  it('pages with the cursor in the link, and offers the way back to the first page', async () => {
    vi.mocked(listMeasurements).mockResolvedValue(
      page({ count: 50, limit: 50, next_cursor: 'msm_50' }),
    );
    render(<AdminMeasurementsPage />);
    await screen.findByRole('link', { name: 'Next page' });
    expect(screen.getByRole('link', { name: 'Next page' })).toHaveAttribute(
      'href',
      '/admin/measurements?after=msm_50',
    );
    expect(screen.queryByRole('link', { name: 'First page' })).not.toBeInTheDocument();

    search.set('run=run-1&after=msm_50');
    await act(async () => {});
    await screen.findByRole('link', { name: 'First page' });
    expect(vi.mocked(listMeasurements).mock.calls.at(-1)?.[0]).toMatchObject({
      run_id: 'run-1',
      after: 'msm_50',
    });
    expect(screen.getByRole('link', { name: 'First page' })).toHaveAttribute(
      'href',
      '/admin/measurements?run=run-1',
    );
  });

  it('states each row’s completeness rather than calling a partial row whole', async () => {
    vi.mocked(listMeasurements).mockResolvedValue(
      page({
        measurements: [
          row({ measurement_id: 'msm_ok' }),
          row({
            measurement_id: 'msm_partial',
            collection_complete: false,
            outcome: 'failed',
          }),
        ],
        count: 2,
      }),
    );
    render(<AdminMeasurementsPage />);
    await screen.findByText('msm_partial');
    expect(screen.getAllByText('Collection closed · Server-side only').length).toBe(1);
    expect(screen.getByText('Collection incomplete · Server-side only')).toBeInTheDocument();
    expect(screen.getByText('Failed')).toBeInTheDocument();
  });

  it('expands one frozen record with its stages, shares and attached children', async () => {
    render(<AdminMeasurementsPage />);
    await screen.findByText('msm_1');
    // A link click needs a mounted app router; what the page promises is the
    // URL, and the URL is what re-reads the record.
    expect(screen.getByRole('link', { name: 'Open record' })).toHaveAttribute(
      'href',
      '/admin/measurements?measurement=msm_1',
    );
    search.set('measurement=msm_1');
    await act(async () => {});

    expect(await screen.findByText('files.list — frozen record')).toBeInTheDocument();
    expect(vi.mocked(getMeasurement).mock.calls[0][0]).toBe('msm_1');
    const stages = screen.getByRole('heading', { name: 'Stage timings' }).parentElement!;
    expect(within(stages).getByText('resolve')).toBeInTheDocument();
    expect(within(stages).getByText('read')).toBeInTheDocument();
    expect(within(stages).getByText('broker.fetch')).toBeInTheDocument();
    // 500 µs and 2000 µs are their shares of the 2500 µs record.
    expect(within(stages).getByText('20%')).toBeInTheDocument();
    expect(within(stages).getByText('80%')).toBeInTheDocument();
    // An attached child has no share of this record's timeline and says so.
    expect(within(stages).getByText('attached')).toBeInTheDocument();
    expect(within(stages).getByText('Not on this timeline')).toBeInTheDocument();
    expect(within(stages).getByText(/not part of the total above/)).toBeInTheDocument();
  });

  it('links a record to the exact attempt and interaction it belongs to', async () => {
    vi.mocked(getMeasurement).mockResolvedValue(detail({ attempt_id: 42 }));
    render(<AdminMeasurementsPage />);
    await screen.findByText('msm_1');
    search.set('measurement=msm_1');
    await act(async () => {});

    // Two different precise keys, not a job-wide read: the attempt names this
    // execution and the interaction names the one logical interaction.
    const interaction = await screen.findByRole('link', { name: 'msm_1' });
    expect(interaction).toHaveAttribute('href', expect.stringContaining('interaction_id=msm_1'));
    const attempt = screen.getByRole('link', { name: '42' });
    expect(attempt).toHaveAttribute('href', expect.stringContaining('attempt_id=42'));
  });

  it('says an unattributed startup and a non-startup differ when neither has an attempt', async () => {
    // A file or terminal request genuinely has no attempt; a startup whose
    // attribution never arrived is a different absence. Rendering both as
    // "no attempt" would hide which one an operator is looking at.
    vi.mocked(getMeasurement).mockResolvedValue(detail({ attempt_id: null }));
    render(<AdminMeasurementsPage />);
    await screen.findByText('msm_1');
    search.set('measurement=msm_1');
    await act(async () => {});
    expect(await screen.findByText('Not a startup record')).toBeInTheDocument();
  });

  it('shows a partial record as partial, with its failure stage and missing fragments', async () => {
    vi.mocked(getMeasurement).mockResolvedValue(
      detail({
        collection_complete: false,
        outcome: 'failed',
        end_to_end_complete: false,
        record: record({
          collection_complete: false,
          outcome: 'failed',
          failure_stage: 'sandbox.start',
          truncated: true,
          missing_fragments: ['broker:call-2:sandbox.start'],
          missing_producers: ['broker'],
          truncated_fragments: ['broker:call-1'],
          stages: [
            {
              stage: 'sandbox.start',
              start_offset_us: 0,
              duration_us: 1200,
              children: [
                {
                  stage: 'broker.wake',
                  duration_us: 900,
                  source: 'broker',
                  call_id: 'call-1',
                  outcome: 'failed',
                  truncated: true,
                },
              ],
            },
          ],
        }),
      }),
    );
    render(<AdminMeasurementsPage />);
    await screen.findByText('msm_1');
    search.set('measurement=msm_1');
    await act(async () => {});

    const notice = await screen.findByRole('alert');
    expect(within(notice).getByText(/does not describe a whole operation/)).toBeInTheDocument();
    expect(within(notice).getByText(/Failed at stage “sandbox.start”/)).toBeInTheDocument();
    expect(within(notice).getByText(/broker:call-1/)).toBeInTheDocument();
    expect(
      within(notice).getByText(/never answered: broker:call-2:sandbox.start/),
    ).toBeInTheDocument();
    expect(within(notice).getByText(/never reported: broker/)).toBeInTheDocument();
    // Both completeness flags stay visible, and neither is collapsed into the other.
    const recordSection = screen
      .getByRole('heading', { name: 'files.list — frozen record' })
      .closest('section')!;
    // The outcome is shown beside them, not instead of them.
    expect(within(recordSection).getAllByText('Failed').length).toBeGreaterThan(1);
    expect(within(recordSection).getByText('Outcome')).toBeInTheDocument();
    expect(within(recordSection).getByText('Failure stage')).toBeInTheDocument();
    expect(within(recordSection).getAllByText('sandbox.start').length).toBeGreaterThan(0);
    expect(within(recordSection).getByText('Collection')).toBeInTheDocument();
    expect(within(recordSection).getByText(/Collection did not close/)).toBeInTheDocument();
    expect(within(recordSection).getByText('End to end')).toBeInTheDocument();
    expect(within(recordSection).getByText(/Not covered: server-side only/)).toBeInTheDocument();
    expect(
      within(recordSection).getByText(/Observed up to the collection deadline/),
    ).toBeInTheDocument();
    expect(within(recordSection).getByText('call call-1')).toBeInTheDocument();
  });

  it('does not claim completeness for a record with no reasons listed', async () => {
    render(<AdminMeasurementsPage />);
    await screen.findByText('msm_1');
    search.set('measurement=msm_1');
    await act(async () => {});
    await screen.findByText('Nothing flags this record as partial');
    expect(screen.getByText(/That is not a claim of completeness/)).toBeInTheDocument();
    // The record is still not end-to-end, and says so beside the "no reasons" line.
    expect(screen.getByText(/Not covered: server-side only/)).toBeInTheDocument();
  });

  it('keeps an unknown coverage scope visibly unknown', async () => {
    vi.mocked(listMeasurements).mockResolvedValue(
      page({ measurements: [row({ coverage_scope: 'browser_interaction' })] }),
    );
    render(<AdminMeasurementsPage />);
    await screen.findByText('browser_interaction (unknown scope)');
    // The notice above names both known scopes; the row is what must not claim one.
    const measured = screen.getByText('browser_interaction (unknown scope)').closest('tr')!;
    expect(within(measured).queryByText('Server request')).not.toBeInTheDocument();
  });

  it('aggregates in SQL over the whole experiment and says what was excluded', async () => {
    search.value = 'run=run-1';
    render(<AdminMeasurementsPage />);
    await screen.findByText(/Computed in SQL over the whole experiment/);
    expect(screen.getByText(/not filtered by the form above/)).toBeInTheDocument();
    expect(
      screen.getByText(/3 with collection incomplete and 2 not successful/),
    ).toBeInTheDocument();
    expect(screen.getByText(/may count one sample twice/)).toBeInTheDocument();
    expect(
      screen.getByText(/30 stored sample\(s\), 12 interaction\(s\), 25 eligible/),
    ).toBeInTheDocument();
    expect(
      screen.getByText(/excluded: 3 with collection incomplete, 2 not successful/),
    ).toBeInTheDocument();
    const distribution = screen
      .getByRole('heading', { name: 'Distribution over the whole experiment' })
      .closest('section')!;
    expect(within(distribution).getByText('operation=files.list')).toBeInTheDocument();
    expect(within(distribution).getByText('startup_path=Not a startup')).toBeInTheDocument();
    expect(within(distribution).getByText('coverage_scope=server_request')).toBeInTheDocument();
  });

  it('flags a percentile computed from too few samples, and unknown percentiles as unknown', async () => {
    search.value = 'run=run-1';
    vi.mocked(summarizeProfilingRun).mockResolvedValue({
      run_id: 'run-1',
      groups: [
        group({ eligible_count: 3, p50_us: null, p95_us: null, min_us: null, max_us: null }),
      ],
    });
    render(<AdminMeasurementsPage />);
    await screen.findByText(/Fewer than 20 eligible samples/);
    expect(screen.getAllByText('Unavailable').length).toBeGreaterThanOrEqual(4);
    expect(screen.queryByText('0 µs')).not.toBeInTheDocument();
  });

  it('says an experiment with no stored samples has nothing to compute, not zero', async () => {
    search.value = 'run=run-1';
    vi.mocked(summarizeProfilingRun).mockResolvedValue({ run_id: 'run-1', groups: [] });
    render(<AdminMeasurementsPage />);
    await screen.findByText(/returned no groups at all/);
    expect(screen.getByText(/Percentiles are not zero/)).toBeInTheDocument();
  });

  it('applies filters, drops the cursor, and writes the selection to the address bar', async () => {
    search.value = 'run=run-1&after=msm_9';
    const replace = vi.spyOn(window.history, 'replaceState');
    render(<AdminMeasurementsPage />);
    await screen.findByText('msm_1');

    fireEvent.change(screen.getByLabelText('Operations'), {
      target: { value: 'files.list, files.open' },
    });
    fireEvent.change(screen.getByLabelText('Collection'), { target: { value: 'false' } });
    fireEvent.click(screen.getByRole('button', { name: 'Apply filters' }));

    await waitFor(() => expect(listMeasurements).toHaveBeenCalledTimes(2));
    // A cursor is a position in one ordering; a new selection cannot resume it.
    expect(vi.mocked(listMeasurements).mock.calls[1][0]).toMatchObject({
      run_id: 'run-1',
      operation: ['files.list', 'files.open'],
      collection_complete: false,
      after: undefined,
    });
    await waitFor(() => expect(replace).toHaveBeenCalled());
    const written = String(replace.mock.calls.at(-1)?.[2]);
    expect(written).toContain('operation=files.list');
    expect(written).toContain('operation=files.open');
    expect(written).toContain('collection_complete=false');
    expect(written).not.toContain('after=');
    replace.mockRestore();
  });

  it('filters by an attempt and by an interaction as two distinct keys', async () => {
    search.value = 'run=run-1';
    const replace = vi.spyOn(window.history, 'replaceState');
    render(<AdminMeasurementsPage />);
    await screen.findByText('msm_1');

    fireEvent.change(screen.getByLabelText('Attempt ID'), { target: { value: '42' } });
    fireEvent.change(screen.getByLabelText('Interaction ID'), { target: { value: 'aint_1' } });
    fireEvent.click(screen.getByRole('button', { name: 'Apply filters' }));

    await waitFor(() => expect(listMeasurements).toHaveBeenCalledTimes(2));
    expect(vi.mocked(listMeasurements).mock.calls[1][0]).toMatchObject({
      run_id: 'run-1',
      attempt_id: 42,
      interaction_id: 'aint_1',
    });
    const written = String(replace.mock.calls.at(-1)?.[2]);
    expect(written).toContain('attempt_id=42');
    expect(written).toContain('interaction_id=aint_1');
    replace.mockRestore();
  });

  it('refuses an attempt id that is not a number instead of reading every attempt', async () => {
    search.value = 'run=run-1';
    render(<AdminMeasurementsPage />);
    await screen.findByText('msm_1');

    fireEvent.change(screen.getByLabelText('Attempt ID'), { target: { value: 'latest' } });
    fireEvent.click(screen.getByRole('button', { name: 'Apply filters' }));

    await screen.findByText(/attempt_id=latest, which is not an attempt number/);
    // Nothing was re-read: a widened page under the old heading would be the
    // one outcome this page exists to avoid.
    expect(listMeasurements).toHaveBeenCalledTimes(1);
  });

  it('refuses an attempt_id link it cannot serve, before reading anything', async () => {
    search.value = 'run=run-1&attempt_id=latest';
    render(<AdminMeasurementsPage />);
    await screen.findByRole('alert');
    expect(screen.getByText(/not an attempt number/)).toBeInTheDocument();
    expect(listMeasurements).not.toHaveBeenCalled();
  });

  it('reports a revoked session instead of keeping the records it already read', async () => {
    render(<AdminMeasurementsPage />);
    await screen.findByText('msm_1');
    vi.mocked(listMeasurements).mockRejectedValueOnce(new APIError('INVALID_TOKEN', 'no', 403));
    fireEvent.click(screen.getByRole('button', { name: 'Apply filters' }));
    await screen.findByText('Admin access required');
    expect(screen.queryByText('msm_1')).not.toBeInTheDocument();
  });

  it('shows a failed read as an error and keeps the previous successful one', async () => {
    render(<AdminMeasurementsPage />);
    await screen.findByText('msm_1');
    vi.mocked(listMeasurements).mockRejectedValueOnce(new APIError('SERVER_ERROR', 'boom', 500));
    fireEvent.click(screen.getByRole('button', { name: 'Apply filters' }));
    await screen.findByRole('alert');
    expect(screen.getByText(/last refresh failed/)).toBeInTheDocument();
    expect(screen.getByText('msm_1')).toBeInTheDocument();
  });

  it('reads nothing at all for a link it cannot serve, rather than widening it', async () => {
    search.value = 'run=run-1&host=host-a';
    const replace = vi.spyOn(window.history, 'replaceState');
    render(<AdminMeasurementsPage />);
    await screen.findByRole('alert');
    expect(screen.getByText(/Unsupported filter: host/)).toBeInTheDocument();
    expect(listProfilingRuns).not.toHaveBeenCalled();
    expect(listMeasurements).not.toHaveBeenCalled();
    // Writing the default selection back would replace the link being reported.
    await act(async () => {});
    expect(replace).not.toHaveBeenCalled();
    expect(screen.getByRole('link', { name: 'Read all experiments instead' })).toHaveAttribute(
      'href',
      '/admin/measurements',
    );
    replace.mockRestore();
  });

  it('says when the selected experiment is not in the catalogue page it read', async () => {
    search.value = 'run=run-not-listed';
    render(<AdminMeasurementsPage />);
    await screen.findByText(/is not among the 1 catalogue rows read here/);
    expect(screen.getByText(/a wider catalogue would be a different question/)).toBeInTheDocument();
  });

  it('says which experiment its summary belongs to and reads the whole of it', async () => {
    search.value = 'run=run-1';
    render(<AdminMeasurementsPage />);
    await screen.findByText('Distribution over the whole experiment');
    expect(vi.mocked(summarizeProfilingRun).mock.calls[0][1]).toEqual([]);
  });

  it('re-reads every panel on Refresh without losing the selection', async () => {
    search.value = 'run=run-1&operation=files.list';
    render(<AdminMeasurementsPage />);
    await screen.findByText('msm_1');
    expect(listProfilingRuns).toHaveBeenCalledTimes(1);
    fireEvent.click(screen.getByRole('button', { name: 'Refresh' }));
    await waitFor(() => expect(listProfilingRuns).toHaveBeenCalledTimes(2));
    expect(vi.mocked(listMeasurements).mock.calls.at(-1)?.[0]).toMatchObject({
      run_id: 'run-1',
      operation: ['files.list'],
    });
    await waitFor(() => expect(summarizeProfilingRun).toHaveBeenCalledTimes(2));
  });
});
