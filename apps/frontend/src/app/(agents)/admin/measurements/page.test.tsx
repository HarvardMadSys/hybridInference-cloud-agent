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
  ProfilingControlRun,
  ProfilingControlSnapshot,
  ProfilingControlStatus,
  ProfilingRun,
} from '@/lib/api/adminMeasurements';
import type { AgentRunnerHost } from '@/lib/api/admin';
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
// The module is mocked wholesale except for the pure mapper the panel uses to
// turn its checkboxes into root operation names: that mapping is the behaviour
// under test, so it stays real.
vi.mock('@/lib/api/adminMeasurements', async (importOriginal) => {
  const actual = await importOriginal<typeof import('@/lib/api/adminMeasurements')>();
  return {
    ...actual,
    listProfilingRuns: vi.fn(),
    listMeasurements: vi.fn(),
    summarizeProfilingRun: vi.fn(),
    getMeasurement: vi.fn(),
    getMeasurementWriterStatus: vi.fn(),
    getProfilingControl: vi.fn(),
    startProfilingRun: vi.fn(),
    stopProfilingRun: vi.fn(),
  };
});
vi.mock('@/lib/api/admin', async (importOriginal) => {
  const actual = await importOriginal<typeof import('@/lib/api/admin')>();
  return { ...actual, getAgentRunnerHosts: vi.fn() };
});

import { getAgentRunnerHosts } from '@/lib/api/admin';
import {
  getMeasurement,
  getMeasurementWriterStatus,
  getProfilingControl,
  listMeasurements,
  listProfilingRuns,
  startProfilingRun,
  stopProfilingRun,
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

function controlRun(overrides: Partial<ProfilingControlRun> = {}): ProfilingControlRun {
  return {
    run_id: 'prun-1',
    operations: ['attempt.startup', 'terminal.open'],
    target: { host: 'host-a' },
    expires_at: '2026-09-21T10:10:00Z',
    started_at: '2026-09-21T10:00:00Z',
    stopped_at: null,
    stop_reason: null,
    lifecycle: 'active',
    source: 'admin',
    created_by: 'admin-1',
    control_revision: 4,
    seconds_remaining: 600,
    ...overrides,
  };
}

function control(overrides: Partial<ProfilingControlSnapshot> = {}): ProfilingControlSnapshot {
  return {
    deployment: 'test',
    mode: 'admin',
    revision: 3,
    configured: true,
    server_time: '2026-09-21T10:00:00Z',
    status: 'off',
    run: null,
    run_active: false,
    last_stopped_run: null,
    stored_samples: 0,
    budget_scope: 'producer_instance',
    budget_note: 'Each producer enforces its own operation budget.',
    sync_note:
      'the configuration reaches the control plane, runners and brokers through a background ' +
      'sync; allow up to 30 seconds for a change to take effect everywhere',
    operations: {
      selectable: {
        sandbox_startup: ['attempt.startup'],
        terminal_open: ['terminal.open'],
        file_access: ['files.list', 'files.open'],
      },
      all: ['attempt.startup', 'terminal.open', 'files.list', 'files.open'],
    },
    bounds: {
      min_duration_seconds: 60,
      max_duration_seconds: 3600,
      default_max_operations: 100,
      hard_max_operations: 1000,
    },
    ...overrides,
  };
}

function host(overrides: Partial<AgentRunnerHost> = {}): AgentRunnerHost {
  return {
    host: 'host-a',
    active: true,
    last_worker_id: 'worker-1',
    first_seen_at: '2026-09-21T09:00:00Z',
    last_seen_at: '2026-09-21T10:00:00Z',
    seconds_since_seen: 2,
    status: 'online',
    enrolled: true,
    labels: {},
    capabilities: [],
    slots_total: 2,
    slots_used: 0,
    runner_version: '1.0.0',
    broker_url: null,
    last_heartbeat_at: '2026-09-21T10:00:00Z',
    sandbox_runtime: 'io.containerd.kata.v2',
    sandbox_image_digest: null,
    runtime_versions: null,
    runtime_versions_available: null,
    capacity_epoch: null,
    capacity: null,
    ...overrides,
  };
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
  // The control card reads on mount in every test; an unmocked read would
  // reject and the page would render a state no test asked for.
  vi.mocked(getProfilingControl).mockResolvedValue(control());
  vi.mocked(getAgentRunnerHosts).mockResolvedValue({
    hosts: [host({ host: 'host-a' }), host({ host: 'host-b' })],
    active_host: null,
    runtime_versions: {},
    observed_at: '2026-09-21T10:00:00Z',
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

describe('measurement collection control', () => {
  /**
   * The control row the reads below would see. A command writes it, the next
   * poll reads it back: without that, a poll firing after a write would answer
   * with the state the page held before it and the write would look lost.
   */
  let live: ProfilingControlSnapshot;

  beforeEach(() => {
    live = control();
    vi.mocked(getProfilingControl).mockImplementation(async () => live);
  });

  function card(): HTMLElement {
    return screen.getByRole('heading', { name: 'Measurement collection' }).closest('section')!;
  }

  /** Open the inline form and wait until the host menu it reads has arrived. */
  async function openForm(): Promise<void> {
    fireEvent.click(await screen.findByRole('button', { name: 'Enable measurement' }));
    await screen.findByRole('option', { name: 'host-a' });
  }

  /** The form's own primary button; the toggle is gone while the form is open. */
  function submit(): void {
    fireEvent.click(screen.getByRole('button', { name: 'Enable measurement' }));
  }

  it('says Off and offers exactly one button, labelled Enable measurement', async () => {
    render(<AdminMeasurementsPage />);
    await screen.findByRole('button', { name: 'Enable measurement' });

    expect(within(card()).getByText('Disabled')).toBeInTheDocument();
    // One button, never two: nothing here offers to stop what is not running.
    expect(within(card()).getAllByRole('button')).toHaveLength(1);
    expect(within(card()).queryByRole('button', { name: /disable/i })).not.toBeInTheDocument();
  });

  it.each<[ProfilingControlStatus, string, RegExp]>([
    ['unavailable', 'Unavailable', /This deployment is not configured for Admin control/],
    [
      'unmanaged',
      'Managed by deployment configuration',
      /set by this deployment's own configuration/,
    ],
  ])('renders %s without a button at all, and explains why', async (status, line, explanation) => {
    // What the control plane answers for a deployment it cannot command: no
    // selectable groups and no bounds, because there is no command to build.
    // The card must say why rather than crash on the shape or offer a switch.
    live = control({
      mode: status === 'unmanaged' ? 'environment' : 'disabled',
      configured: false,
      status,
      operations: { selectable: {}, all: [] } as unknown as ProfilingControlSnapshot['operations'],
      bounds: {} as unknown as ProfilingControlSnapshot['bounds'],
    });
    render(<AdminMeasurementsPage />);
    await within(card()).findByText(explanation);

    // The one-line status is configuration, and it is not "Disabled".
    const header = within(card()).getByRole('heading', {
      name: 'Measurement collection',
    }).parentElement!;
    expect(within(header).getByText(line)).toBeInTheDocument();
    expect(within(card()).queryByText('Disabled')).not.toBeInTheDocument();
    // Not one button: not "Enable measurement", and not a disabled one either.
    expect(within(card()).queryAllByRole('button')).toHaveLength(0);
  });

  it('opens the inline form on Enable measurement, with every group on and no host chosen', async () => {
    render(<AdminMeasurementsPage />);
    await openForm();

    expect(screen.queryByRole('dialog')).not.toBeInTheDocument();
    expect(screen.getByRole('checkbox', { name: 'Sandbox startup' })).toBeChecked();
    expect(screen.getByRole('checkbox', { name: 'Terminal opening' })).toBeChecked();
    expect(screen.getByRole('checkbox', { name: 'File access' })).toBeChecked();
    // Nothing is preselected: the admin chooses, and there is no host default.
    expect(screen.getByLabelText('Host')).toHaveValue('');
    expect(screen.getByLabelText('Duration')).toHaveValue('10');
    expect(screen.getByRole('option', { name: 'Choose a host…' })).toBeInTheDocument();
    expect(screen.getByRole('option', { name: 'All hosts' })).toBeInTheDocument();
    expect(screen.getByRole('option', { name: 'host-a' })).toBeInTheDocument();
    expect(screen.getByRole('option', { name: 'host-b' })).toBeInTheDocument();
    expect(within(card()).queryByRole('button', { name: /disable/i })).not.toBeInTheDocument();
  });

  it('sends the chosen groups, host and duration with the revision it read, and draws the answer', async () => {
    vi.mocked(startProfilingRun).mockImplementation(async () => {
      live = control({
        revision: 4,
        status: 'on',
        run_active: true,
        run: controlRun({ operations: ['attempt.startup', 'files.list', 'files.open'] }),
        stored_samples: 0,
      });
      return { control: live, run_id: 'prun-1', revision: 4 };
    });
    render(<AdminMeasurementsPage />);
    await openForm();

    fireEvent.change(screen.getByLabelText('Host'), { target: { value: 'host-a' } });
    fireEvent.click(screen.getByRole('checkbox', { name: 'Terminal opening' }));
    submit();

    await waitFor(() => expect(startProfilingRun).toHaveBeenCalledTimes(1));
    const body = vi.mocked(startProfilingRun).mock.calls[0][0];
    expect(body.operations).toEqual(['attempt.startup', 'files.list', 'files.open']);
    expect(body.target).toEqual({ host: 'host-a' });
    expect(body.duration_seconds).toBe(600);
    expect(body.expected_revision).toBe(3);
    expect(typeof body.request_id).toBe('string');
    expect(body.request_id.length).toBeGreaterThan(0);

    // What is drawn is the snapshot the server returned, not a local guess.
    expect(await screen.findByText('Enabled')).toBeInTheDocument();
    expect(screen.getByText('Operations: Sandbox startup, File access')).toBeInTheDocument();
    expect(screen.getByText('Host: host-a')).toBeInTheDocument();
    expect(screen.getByText(/^Expires at: /)).toBeInTheDocument();
    expect(screen.getByText('Time left: 10:00')).toBeInTheDocument();
    expect(screen.getByText('Stored samples: 0')).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Disable measurement' })).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: 'Enable measurement' })).not.toBeInTheDocument();
  });

  it('flips the single button to Disable measurement once the run is in force', async () => {
    live = control({ status: 'on', run_active: true, run: controlRun() });
    render(<AdminMeasurementsPage />);
    await screen.findByRole('button', { name: 'Disable measurement' });

    expect(within(card()).getByText('Enabled')).toBeInTheDocument();
    expect(within(card()).getAllByRole('button')).toHaveLength(1);
    expect(
      within(card()).queryByRole('button', { name: 'Enable measurement' }),
    ).not.toBeInTheDocument();
  });

  it('sends an all-hosts target as null when All hosts is the choice', async () => {
    vi.mocked(startProfilingRun).mockImplementation(async () => {
      live = control({
        revision: 4,
        status: 'on',
        run_active: true,
        run: controlRun({ target: {}, operations: ['attempt.startup'] }),
      });
      return { control: live, run_id: 'prun-1', revision: 4 };
    });
    render(<AdminMeasurementsPage />);
    await openForm();

    const allHosts = screen.getByRole('option', { name: 'All hosts' }) as HTMLOptionElement;
    fireEvent.change(screen.getByLabelText('Host'), { target: { value: allHosts.value } });
    submit();

    await waitFor(() => expect(startProfilingRun).toHaveBeenCalledTimes(1));
    expect(vi.mocked(startProfilingRun).mock.calls[0][0].target).toEqual({ host: null });
    expect(await screen.findByText('Host: All hosts')).toBeInTheDocument();
  });

  it('refuses a submission with no host or no group instead of guessing one', async () => {
    render(<AdminMeasurementsPage />);
    await openForm();

    submit();
    await screen.findByText(/Choose a host, or All hosts/);
    expect(startProfilingRun).not.toHaveBeenCalled();

    fireEvent.click(screen.getByRole('checkbox', { name: 'Sandbox startup' }));
    fireEvent.click(screen.getByRole('checkbox', { name: 'Terminal opening' }));
    fireEvent.click(screen.getByRole('checkbox', { name: 'File access' }));
    submit();
    await screen.findByText(/Choose at least one thing to measure/);
    expect(startProfilingRun).not.toHaveBeenCalled();
  });

  it('keeps the form and its values when an enable fails, and says why', async () => {
    vi.mocked(startProfilingRun).mockRejectedValue(
      new APIError('storage_unavailable', 'measurement storage is unavailable', 503),
    );
    render(<AdminMeasurementsPage />);
    await openForm();

    fireEvent.click(screen.getByRole('checkbox', { name: 'File access' }));
    fireEvent.change(screen.getByLabelText('Host'), { target: { value: 'host-b' } });
    fireEvent.change(screen.getByLabelText('Duration'), { target: { value: '30' } });
    submit();

    await screen.findByText('measurement storage is unavailable');
    // The form is still open with the values that produced the failure.
    expect(screen.getByRole('checkbox', { name: 'File access' })).not.toBeChecked();
    expect(screen.getByLabelText('Host')).toHaveValue('host-b');
    expect(screen.getByLabelText('Duration')).toHaveValue('30');
    expect(screen.getByRole('button', { name: 'Enable measurement' })).toBeInTheDocument();
    // And nothing was drawn as though a run had begun.
    expect(within(card()).getByText('Disabled')).toBeInTheDocument();
    expect(within(card()).queryByRole('button', { name: /disable/i })).not.toBeInTheDocument();
  });

  it('retries an identical enable with the same request id instead of a second run', async () => {
    vi.mocked(startProfilingRun)
      .mockRejectedValueOnce(new APIError('NETWORK_ERROR', 'offline'))
      .mockImplementationOnce(async () => {
        live = control({ revision: 4, status: 'on', run_active: true, run: controlRun() });
        return { control: live, run_id: 'prun-1', revision: 4 };
      });
    render(<AdminMeasurementsPage />);
    await openForm();
    fireEvent.change(screen.getByLabelText('Host'), { target: { value: 'host-a' } });
    submit();
    await screen.findByText(/Network error/);

    submit();
    await waitFor(() => expect(startProfilingRun).toHaveBeenCalledTimes(2));
    const [first, second] = vi.mocked(startProfilingRun).mock.calls;
    // The same command, so the server can recognise the retry: a new id here
    // would be a second run asked for by one admin click.
    expect(second[0]).toEqual(first[0]);
    expect(second[0].request_id).toBe(first[0].request_id);
  });

  it('stops the named run without a confirmation dialog, and draws the answer', async () => {
    live = control({ status: 'on', run_active: true, run: controlRun() });
    vi.mocked(stopProfilingRun).mockImplementation(async () => {
      live = control({
        revision: 6,
        status: 'off',
        run: null,
        run_active: false,
        last_stopped_run: controlRun({
          stop_reason: 'stopped_by_admin',
          stopped_at: '2026-09-21T10:03:00Z',
        }),
      });
      return { control: live, run_id: 'prun-1', revision: 6 };
    });
    const confirm = vi.spyOn(window, 'confirm');
    try {
      render(<AdminMeasurementsPage />);
      fireEvent.click(await screen.findByRole('button', { name: 'Disable measurement' }));

      await waitFor(() => expect(stopProfilingRun).toHaveBeenCalledTimes(1));
      expect(vi.mocked(stopProfilingRun).mock.calls[0][0]).toBe('prun-1');
      expect(vi.mocked(stopProfilingRun).mock.calls[0][1]).toEqual({
        request_id: expect.any(String),
      });
      // No dialog stands between the click and the command.
      expect(confirm).not.toHaveBeenCalled();
      expect(screen.queryByRole('dialog')).not.toBeInTheDocument();

      expect(await screen.findByText('Disabled')).toBeInTheDocument();
      expect(screen.getByText(/Last experiment: prun-1/)).toBeInTheDocument();
      expect(screen.getByText(/Stop reason: stopped_by_admin/)).toBeInTheDocument();
      expect(screen.getByRole('button', { name: 'Enable measurement' })).toBeInTheDocument();
      expect(screen.queryByRole('button', { name: 'Disable measurement' })).not.toBeInTheDocument();
    } finally {
      confirm.mockRestore();
    }
  });

  it('shows the pending verb and disables the button while a write is in flight', async () => {
    live = control({ status: 'on', run_active: true, run: controlRun() });
    let release: () => void = () => {};
    vi.mocked(stopProfilingRun).mockImplementation(async () => {
      await new Promise<void>((resolve) => {
        release = resolve;
      });
      live = control({ revision: 6, status: 'off', run: null, run_active: false });
      return { control: live, run_id: 'prun-1', revision: 6 };
    });
    render(<AdminMeasurementsPage />);
    fireEvent.click(await screen.findByRole('button', { name: 'Disable measurement' }));

    await waitFor(() => expect(screen.getByRole('button', { name: 'Disabling…' })).toBeDisabled());
    await act(async () => {
      release();
    });
    await screen.findByRole('button', { name: 'Enable measurement' });
  });

  it('hints that nothing has been stored yet while a live run has no samples', async () => {
    live = control({ status: 'on', run_active: true, run: controlRun(), stored_samples: 0 });
    render(<AdminMeasurementsPage />);
    await screen.findByText('Stored samples: 0');

    expect(screen.getByText(/No matching operation has happened yet/)).toBeInTheDocument();
    expect(screen.getByText(/use a sandbox, open a terminal or browse files/)).toBeInTheDocument();
    expect(screen.getByText(/A stored sample is one server-side record/)).toBeInTheDocument();

    live = control({ status: 'on', run_active: true, run: controlRun(), stored_samples: 7 });
    fireEvent.click(screen.getByRole('button', { name: 'Refresh' }));
    await screen.findByText('Stored samples: 7');
    expect(screen.queryByText(/No matching operation has happened yet/)).not.toBeInTheDocument();
  });

  it('repeats the server’s sync note and folds the diagnostics away behind Details', async () => {
    render(<AdminMeasurementsPage />);
    await screen.findByRole('button', { name: 'Enable measurement' });

    // A change takes a few seconds to reach every process, and the card says
    // so in the server's own words instead of implying instant agreement.
    expect(within(card()).getByText(/allow up to 30 seconds/)).toBeInTheDocument();

    const details = card().querySelector('details')!;
    expect(details).not.toHaveAttribute('open');
    expect(within(details).getByText('Details')).toBeInTheDocument();
    expect(within(details).getByText('Mode')).toBeInTheDocument();
    expect(within(details).getByText('admin')).toBeInTheDocument();
    expect(within(details).getByText('Revision')).toBeInTheDocument();
    expect(within(details).getByText('test')).toBeInTheDocument();
    expect(within(details).getByText('producer_instance')).toBeInTheDocument();
  });

  it('clears the page for a revoked control read rather than showing a state', async () => {
    vi.mocked(getProfilingControl).mockRejectedValue(new APIError('INVALID_TOKEN', 'no', 403));
    render(<AdminMeasurementsPage />);
    await screen.findByText('Admin access required');
    expect(screen.queryByText('Measurement collection')).not.toBeInTheDocument();
    expect(screen.queryByText('Disabled')).not.toBeInTheDocument();
  });

  it('keeps the last successful read when a background poll fails, flagged as stale', async () => {
    vi.mocked(getProfilingControl).mockRejectedValueOnce(new APIError('SERVER_ERROR', 'boom', 500));
    render(<AdminMeasurementsPage />);
    // The first read failed and nothing has been shown yet, so the card says
    // so instead of inventing an Off state.
    await screen.findByRole('alert');
    expect(
      within(card()).queryByRole('button', { name: 'Enable measurement' }),
    ).not.toBeInTheDocument();

    fireEvent.click(screen.getByRole('button', { name: 'Refresh' }));
    await within(card()).findByText('Disabled');

    vi.mocked(getProfilingControl).mockRejectedValueOnce(new APIError('SERVER_ERROR', 'boom', 500));
    fireEvent.click(screen.getByRole('button', { name: 'Refresh' }));
    await screen.findByText(/showing the last successful read/);
    // The last good state is still on screen, and still says Off.
    expect(within(card()).getByText('Disabled')).toBeInTheDocument();
    expect(within(card()).getByRole('button', { name: 'Enable measurement' })).toBeEnabled();
  });

  it('re-reads a live run within five seconds and an idle one only at thirty', async () => {
    vi.useFakeTimers();
    try {
      live = control({ status: 'on', run_active: true, run: controlRun() });
      render(<AdminMeasurementsPage />);
      await act(async () => {
        await vi.advanceTimersByTimeAsync(50);
      });
      const liveReads = vi.mocked(getProfilingControl).mock.calls.length;
      await act(async () => {
        await vi.advanceTimersByTimeAsync(5000);
      });
      expect(vi.mocked(getProfilingControl).mock.calls.length).toBeGreaterThan(liveReads);
    } finally {
      vi.useRealTimers();
    }
  });

  it('leaves an idle deployment alone until the thirty-second poll', async () => {
    vi.useFakeTimers();
    try {
      render(<AdminMeasurementsPage />);
      await act(async () => {
        await vi.advanceTimersByTimeAsync(50);
      });
      const idleReads = vi.mocked(getProfilingControl).mock.calls.length;
      await act(async () => {
        await vi.advanceTimersByTimeAsync(5000);
      });
      // Off is read every thirty seconds: a five-second poll would have fired.
      expect(vi.mocked(getProfilingControl).mock.calls.length).toBe(idleReads);
      await act(async () => {
        await vi.advanceTimersByTimeAsync(25000);
      });
      expect(vi.mocked(getProfilingControl).mock.calls.length).toBeGreaterThan(idleReads);
    } finally {
      vi.useRealTimers();
    }
  });

  it('reads nothing while the tab is hidden, and reads at once when it returns', async () => {
    live = control({ status: 'on', run_active: true, run: controlRun() });
    render(<AdminMeasurementsPage />);
    await screen.findByRole('button', { name: 'Disable measurement' });
    const before = vi.mocked(getProfilingControl).mock.calls.length;

    Object.defineProperty(document, 'hidden', { configurable: true, value: true });
    try {
      await act(async () => {
        document.dispatchEvent(new Event('visibilitychange'));
      });
      expect(vi.mocked(getProfilingControl).mock.calls.length).toBe(before);

      Object.defineProperty(document, 'hidden', { configurable: true, value: false });
      await act(async () => {
        document.dispatchEvent(new Event('visibilitychange'));
      });
      // Coming back is not a reason to wait out the poll interval: whatever is
      // on screen was read before the tab was left.
      await waitFor(() =>
        expect(vi.mocked(getProfilingControl).mock.calls.length).toBeGreaterThan(before),
      );
    } finally {
      Object.defineProperty(document, 'hidden', { configurable: true, value: false });
    }
  });
});
