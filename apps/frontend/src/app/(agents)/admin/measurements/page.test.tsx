// @vitest-environment jsdom
import '@testing-library/jest-dom/vitest';

// Setup notices have their own tests; this page uses a configured deployment.
vi.mock('@/lib/api/adminSettings', () => ({
  getAgentAdminSettings: async () => ({
    revision: 1,
    definitions: [],
    values: {},
    secret_configured: {},
    bootstrap: [],
    setup: { required: false, groups: [], bootstrap_missing: [] },
  }),
}));

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
        children: [
          { stage: 'sandbox_manager.fetch', duration_us: 1800, outcome: 'ok', source: 'broker' },
        ],
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
      'the configuration reaches the control plane, runners and sandbox managers through a background ' +
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
    drain_fence_epoch: '00000000-0000-0000-0000-000000000001',
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
    sandbox_manager_url: null,
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

  async function timings(): Promise<HTMLElement> {
    return (await screen.findByRole('heading', { name: 'Timings' })).closest('section')!;
  }

  function samples(): HTMLElement {
    return screen.getByRole('heading', { name: 'Samples' }).closest('section')!;
  }

  /** The row button that expands a sample, by its operation label. */
  async function sampleButton(label: string): Promise<HTMLElement> {
    return within(samples()).findByRole('button', { name: label });
  }

  it('reads the newest run, its timings and its samples, and says they are server-side', async () => {
    render(<AdminMeasurementsPage />);
    await sampleButton('File listing');

    expect(vi.mocked(listProfilingRuns).mock.calls[0][0]).toEqual({ limit: 50 });
    // The page is about one run, the newest, until the admin picks another.
    expect(vi.mocked(summarizeProfilingRun).mock.calls[0].slice(0, 2)).toEqual(['run-1', []]);
    expect(vi.mocked(listMeasurements).mock.calls[0][0]).toMatchObject({ run_id: 'run-1' });
    expect(getMeasurement).not.toHaveBeenCalled();

    const nav = screen.getByRole('navigation', { name: 'Admin sections' });
    expect(within(nav).getByRole('link', { name: 'Measurements' })).toHaveAttribute(
      'aria-current',
      'page',
    );
    // What every number covers is said once, where the page says what it is.
    expect(screen.getByText('timed on the server')).toHaveAttribute(
      'title',
      expect.stringMatching(/none of these is the wait a user saw/),
    );
    expect(within(await timings()).getByText('Run Sep 17, 10:00 UTC · host-a')).toBeInTheDocument();
    expect(
      within(await timings()).getByRole('rowheader', { name: 'File listing' }),
    ).toBeInTheDocument();
    // A writer that lost nothing is not something to read about.
    expect(screen.queryByText(/lost on this server/)).not.toBeInTheDocument();
  });

  it('reads no samples until it knows which run is the newest', async () => {
    let answer: (value: Awaited<ReturnType<typeof listProfilingRuns>>) => void = () => {};
    vi.mocked(listProfilingRuns).mockReturnValue(
      new Promise((resolve) => {
        answer = resolve;
      }),
    );
    render(<AdminMeasurementsPage />);
    await act(async () => {});
    // Reading every run first and the newest one later would flash a list the
    // page is not about.
    expect(listMeasurements).not.toHaveBeenCalled();

    await act(async () => answer({ runs: [run({ run_id: 'run-7' })], count: 1, limit: 50 }));
    await waitFor(() => expect(listMeasurements).toHaveBeenCalledTimes(1));
    expect(vi.mocked(listMeasurements).mock.calls[0][0]).toMatchObject({ run_id: 'run-7' });
  });

  it('reads every run for a link that names records, and shows what narrows it', async () => {
    search.value = 'job_id=ajob_1&attempt_id=42';
    render(<AdminMeasurementsPage />);
    await within(samples()).findByText('From every run, only');

    expect(vi.mocked(listMeasurements).mock.calls[0][0]).toMatchObject({
      job_id: 'ajob_1',
      attempt_id: 42,
    });
    expect(vi.mocked(listMeasurements).mock.calls[0][0].run_id).toBeUndefined();
    // A run's timings are not what this link asked about.
    expect(summarizeProfilingRun).not.toHaveBeenCalled();
    expect(screen.queryByRole('heading', { name: 'Timings' })).not.toBeInTheDocument();

    fireEvent.click(screen.getByRole('button', { name: 'Remove filter: Startup #42' }));
    await waitFor(() => expect(listMeasurements).toHaveBeenCalledTimes(2));
    expect(vi.mocked(listMeasurements).mock.calls[1][0]).toMatchObject({ job_id: 'ajob_1' });
    expect(vi.mocked(listMeasurements).mock.calls[1][0].attempt_id).toBeUndefined();

    // With nothing naming records, the page is about the newest run again.
    fireEvent.click(screen.getByRole('button', { name: 'Remove filter: Turn ajob_1' }));
    await waitFor(() => expect(listMeasurements).toHaveBeenCalledTimes(3));
    expect(vi.mocked(listMeasurements).mock.calls[2][0]).toMatchObject({ run_id: 'run-1' });
    await waitFor(() => expect(summarizeProfilingRun).toHaveBeenCalledTimes(1));
  });

  it('reads nothing at all for a link it cannot serve, rather than widening it', async () => {
    search.value = 'run=run-1&host=host-a';
    const replace = vi.spyOn(window.history, 'replaceState');
    render(<AdminMeasurementsPage />);
    await screen.findByText(/Unsupported filter: host\. Nothing was read\./);
    expect(listProfilingRuns).not.toHaveBeenCalled();
    expect(listMeasurements).not.toHaveBeenCalled();
    // Writing the default selection back would replace the link being reported.
    await act(async () => {});
    expect(replace).not.toHaveBeenCalled();
    expect(screen.getByRole('link', { name: 'Show all measurements' })).toHaveAttribute(
      'href',
      '/admin/measurements',
    );
    replace.mockRestore();
  });

  it('refuses an attempt_id link it cannot serve, before reading anything', async () => {
    search.value = 'attempt_id=latest';
    render(<AdminMeasurementsPage />);
    await screen.findByText(/not an attempt number/);
    expect(listMeasurements).not.toHaveBeenCalled();
  });

  it('opens an old paged link at its first page', async () => {
    search.value = 'run=run-1&after=msm_9';
    const replace = vi.spyOn(window.history, 'replaceState');
    render(<AdminMeasurementsPage />);
    await sampleButton('File listing');
    expect(vi.mocked(listMeasurements).mock.calls[0][0].after).toBeUndefined();
    await waitFor(() => expect(replace).toHaveBeenCalled());
    expect(String(replace.mock.calls.at(-1)?.[2])).toBe('/?run=run-1');
    replace.mockRestore();
  });

  it('names each sample by what it measured, and flags only the ones that need a look', async () => {
    vi.mocked(listMeasurements).mockResolvedValue(
      page({
        measurements: [
          row({
            measurement_id: 'msm_start',
            operation: 'attempt.startup',
            request_kind: 'attempt.startup',
            coverage_scope: 'host_startup',
            startup_path: 'new_group',
            outcome: 'dispatched',
            duration_us: 12_401_792,
          }),
          row({ measurement_id: 'msm_failed', outcome: 'failed' }),
          row({
            measurement_id: 'msm_partial',
            operation: 'files.open',
            request_kind: 'files.read',
            collection_complete: false,
          }),
        ],
        count: 3,
      }),
    );
    render(<AdminMeasurementsPage />);
    const startup = await sampleButton('Sandbox startup · new sandbox');
    const row1 = startup.closest('tr')!;
    expect(within(row1).getByText('12.40 s')).toHaveAttribute('title', '12,401,792 µs');
    expect(within(row1).getByText('OK')).toBeInTheDocument();
    expect(
      within((await sampleButton('File listing')).closest('tr')!).getByText('Failed'),
    ).toBeInTheDocument();
    expect(
      within((await sampleButton('File opening · read file')).closest('tr')!).getByText(
        'Partly measured',
      ),
    ).toBeInTheDocument();
  });

  it('keeps an unknown coverage scope visibly unknown', async () => {
    vi.mocked(listMeasurements).mockResolvedValue(
      page({ measurements: [row({ coverage_scope: 'browser_interaction' })] }),
    );
    render(<AdminMeasurementsPage />);
    expect(
      await sampleButton('File listing · browser_interaction (unknown scope)'),
    ).toBeInTheDocument();
  });

  it('filters by operation and writes the selection to the address bar', async () => {
    const replace = vi.spyOn(window.history, 'replaceState');
    render(<AdminMeasurementsPage />);
    await sampleButton('File listing');

    fireEvent.change(within(samples()).getByLabelText('Operation'), {
      target: { value: 'files.open' },
    });
    await waitFor(() => expect(listMeasurements).toHaveBeenCalledTimes(2));
    expect(vi.mocked(listMeasurements).mock.calls[1][0]).toMatchObject({
      run_id: 'run-1',
      operation: ['files.open'],
    });
    await waitFor(() => expect(replace).toHaveBeenCalled());
    expect(String(replace.mock.calls.at(-1)?.[2])).toBe('/?operation=files.open');
    // The run's timings do not depend on the list's filter.
    expect(summarizeProfilingRun).toHaveBeenCalledTimes(1);
    replace.mockRestore();
  });

  it('shows several linked operations rather than pretending to one of them', async () => {
    search.value = 'operation=files.list&operation=files.open';
    render(<AdminMeasurementsPage />);
    await sampleButton('File listing');
    expect(within(samples()).getByLabelText('Operation')).toHaveDisplayValue(
      'File listing, File opening',
    );
  });

  it('expands a sample into where its time went, without re-reading the list', async () => {
    const replace = vi.spyOn(window.history, 'replaceState');
    render(<AdminMeasurementsPage />);
    const button = await sampleButton('File listing');
    expect(button).toHaveAttribute('aria-expanded', 'false');

    fireEvent.click(button);
    const timeline = await screen.findByRole('table', { name: 'Where the time went' });
    expect(vi.mocked(getMeasurement).mock.calls[0][0]).toBe('msm_1');
    expect(listMeasurements).toHaveBeenCalledTimes(1);
    expect(button).toHaveAttribute('aria-expanded', 'true');
    await waitFor(() => expect(String(replace.mock.calls.at(-1)?.[2])).toBe('/?measurement=msm_1'));

    // 500 µs and 2000 µs are their shares of the 2500 µs record.
    expect(within(timeline).getByRole('rowheader', { name: 'Resolve' })).toBeInTheDocument();
    const read = within(timeline).getByRole('rowheader', { name: 'Read' }).closest('tr')!;
    expect(within(read).getByText('2 ms')).toBeInTheDocument();
    expect(within(read).getByText('80%')).toBeInTheDocument();
    // Another process's duration sits under its stage, with no share of this one.
    const fetch = within(timeline).getByText('Sandbox Manager fetch').closest('tr')!;
    expect(within(fetch).getByText('· Sandbox Manager')).toBeInTheDocument();
    expect(within(fetch).getByText('1.8 ms')).toBeInTheDocument();
    expect(fetch.querySelectorAll('td')[2]).toHaveTextContent('');
    expect(within(timeline).getByRole('rowheader', { name: 'Total' })).toBeInTheDocument();
    // Everything else the record carries is one click away, not on the page.
    expect(screen.getByText('Raw record')).toBeInTheDocument();
    expect(screen.getByText(/"payload_hash": "sha256:abc"/)).not.toBeVisible();

    fireEvent.click(button);
    await waitFor(() =>
      expect(screen.queryByRole('table', { name: 'Where the time went' })).not.toBeInTheDocument(),
    );
    await waitFor(() => expect(String(replace.mock.calls.at(-1)?.[2])).toBe('/'));
    replace.mockRestore();
  });

  it('shows time no stage covers as its own row', async () => {
    vi.mocked(getMeasurement).mockResolvedValue(
      detail({
        operation: 'attempt.startup',
        request_kind: 'attempt.startup',
        coverage_scope: 'host_startup',
        attempt_id: 52,
        record: record({
          operation: 'attempt.startup',
          coverage_scope: 'host_startup',
          duration_us: 7_034_534,
          spans: [
            {
              span_id: 's1',
              name: 'claim',
              ordinal: 1,
              start_offset_us: 32,
              duration_us: 325_559,
              outcome: 'ok',
            },
          ],
          stages: [
            { stage: 'validate', start_offset_us: 5_617_043, duration_us: 950_341 },
            { stage: 'prompt_dispatch', start_offset_us: 6_567_384, duration_us: 467_150 },
          ],
        }),
      }),
    );
    search.value = 'measurement=msm_1';
    render(<AdminMeasurementsPage />);
    const timeline = await screen.findByRole('table', { name: 'Where the time went' });
    const gap = within(timeline).getByRole('rowheader', { name: 'No stage recorded' });
    expect(gap).toHaveAttribute('title', expect.stringMatching(/no stage or span/));
    expect(within(gap.closest('tr')!).getByText('5.29 s')).toBeInTheDocument();
    expect(within(gap.closest('tr')!).getByText('75.2%')).toBeInTheDocument();
    expect(
      screen.getByText(/runner claiming the turn to the prompt reaching the agent/),
    ).toBeInTheDocument();
    // Attempts are an engineering fallback; the admin never names them.
    expect(screen.queryByText(/Attempt/)).not.toBeInTheDocument();
  });

  it('lists every reason a sample is partial, and marks a floor as a floor', async () => {
    vi.mocked(getMeasurement).mockResolvedValue(
      detail({
        collection_complete: false,
        outcome: 'failed',
        record: record({
          collection_complete: false,
          outcome: 'failed',
          failure_stage: 'sandbox.start',
          missing_fragments: ['sandbox_manager:call-2:sandbox.start'],
          missing_producers: ['broker'],
          truncated_fragments: ['sandbox_manager:call-1'],
          stages: [
            {
              stage: 'sandbox.start',
              start_offset_us: 0,
              duration_us: 2500,
              children: [
                {
                  stage: 'sandbox_manager.wake',
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
    search.value = 'measurement=msm_1';
    render(<AdminMeasurementsPage />);

    const notice = await screen.findByRole('alert');
    expect(within(notice).getByText(/Collection did not close/)).toBeInTheDocument();
    expect(within(notice).getByText(/Failed at stage “sandbox.start”/)).toBeInTheDocument();
    expect(within(notice).getByText(/sandbox_manager:call-1/)).toBeInTheDocument();
    expect(
      within(notice).getByText(/never answered: sandbox_manager:call-2:sandbox.start/),
    ).toBeInTheDocument();
    expect(within(notice).getByText(/never reported: Sandbox Manager/)).toBeInTheDocument();
    const timeline = screen.getByRole('table', { name: 'Where the time went' });
    const wake = within(timeline).getByText('Sandbox Manager wake').closest('tr')!;
    expect(within(wake).getByText('Failed')).toBeInTheDocument();
    expect(within(wake).getByText('≥ 900 µs')).toHaveAttribute(
      'title',
      expect.stringMatching(/a floor/),
    );
  });

  it('says nothing is wrong by saying nothing', async () => {
    search.value = 'measurement=msm_1';
    render(<AdminMeasurementsPage />);
    await screen.findByRole('table', { name: 'Where the time went' });
    expect(screen.queryByRole('alert')).not.toBeInTheDocument();
  });

  it('links a sample to its conversation and turn, and a terminal opening to its other requests', async () => {
    vi.mocked(listMeasurements).mockResolvedValue(
      page({
        measurements: [
          row({
            measurement_id: 'msm_t',
            interaction_id: 'aint_1',
            operation: 'terminal.open',
            request_kind: 'terminal.create',
          }),
        ],
      }),
    );
    vi.mocked(getMeasurement).mockResolvedValue(
      detail({
        measurement_id: 'msm_t',
        interaction_id: 'aint_1',
        operation: 'terminal.open',
        request_kind: 'terminal.create',
      }),
    );
    render(<AdminMeasurementsPage />);
    fireEvent.click(await sampleButton('Terminal opening · create terminal'));

    expect(await screen.findByRole('link', { name: 'Session' })).toHaveAttribute(
      'href',
      '/admin/sessions/athr_1',
    );
    expect(screen.getByRole('link', { name: 'Turn' })).toHaveAttribute(
      'href',
      '/admin/runs/ajob_1',
    );
    // A file or terminal request has no attempt, and that is not worth a word.
    expect(screen.queryByText(/Attempt/)).not.toBeInTheDocument();

    fireEvent.click(screen.getByRole('button', { name: 'Every request of this terminal opening' }));
    await waitFor(() => expect(listMeasurements).toHaveBeenCalledTimes(2));
    expect(vi.mocked(listMeasurements).mock.calls[1][0]).toMatchObject({
      run_id: 'run-1',
      interaction_id: 'aint_1',
    });
    expect(within(samples()).getByText('Only')).toBeInTheDocument();
    expect(
      within(samples()).getByRole('button', { name: 'Remove filter: Interaction aint_1' }),
    ).toBeInTheDocument();
  });

  it('opens a sample a link names even when it is not on the page it read', async () => {
    vi.mocked(getMeasurement).mockResolvedValue(detail({ measurement_id: 'msm_9' }));
    search.value = 'measurement=msm_9';
    render(<AdminMeasurementsPage />);
    expect(await within(samples()).findByText('Sample msm_9')).toBeInTheDocument();
    await screen.findByRole('table', { name: 'Where the time went' });
    expect(vi.mocked(getMeasurement).mock.calls[0][0]).toBe('msm_9');

    fireEvent.click(within(samples()).getByRole('button', { name: 'Close' }));
    await waitFor(() => expect(screen.queryByText('Sample msm_9')).not.toBeInTheDocument());
  });

  it('follows the address bar when a link changes it', async () => {
    render(<AdminMeasurementsPage />);
    await sampleButton('File listing');
    search.set('measurement=msm_1');
    await screen.findByRole('table', { name: 'Where the time went' });
    expect(getMeasurement).toHaveBeenCalledTimes(1);
    // Opening a sample is not a new question about the run.
    expect(listMeasurements).toHaveBeenCalledTimes(1);
    expect(listProfilingRuns).toHaveBeenCalledTimes(1);
  });

  it('says there are no samples rather than showing a zero', async () => {
    vi.mocked(listMeasurements).mockResolvedValue(
      page({ measurements: [], count: 0, empty_reason: 'no stored samples match this selection' }),
    );
    render(<AdminMeasurementsPage />);
    const empty = await within(samples()).findByText(/No samples yet\./);
    expect(empty).toHaveAttribute('title', 'no stored samples match this selection');
    expect(screen.queryByText('0 µs')).not.toBeInTheDocument();

    fireEvent.change(within(samples()).getByLabelText('Operation'), {
      target: { value: 'files.open' },
    });
    expect(
      await within(samples()).findByText('No samples match these filters.'),
    ).toBeInTheDocument();
  });

  it('loads more samples after the last one it has', async () => {
    vi.mocked(listMeasurements)
      .mockResolvedValueOnce(page({ next_cursor: 'msm_1' }))
      .mockResolvedValueOnce(
        page({
          measurements: [
            row({ measurement_id: 'msm_2', operation: 'files.open', request_kind: 'files.open' }),
          ],
          next_cursor: null,
        }),
      );
    render(<AdminMeasurementsPage />);
    fireEvent.click(await screen.findByRole('button', { name: 'Load more samples' }));
    await sampleButton('File opening');
    expect(vi.mocked(listMeasurements).mock.calls[1][0]).toMatchObject({
      run_id: 'run-1',
      after: 'msm_1',
    });
    expect(await sampleButton('File listing')).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: 'Load more samples' })).not.toBeInTheDocument();
  });

  it('summarizes a run with what was left out counted, not hidden', async () => {
    render(<AdminMeasurementsPage />);
    const row = (
      await within(await timings()).findByRole('rowheader', { name: 'File listing' })
    ).closest('tr')!;
    expect(within(row).getByText('30')).toBeInTheDocument();
    const excluded = within(row).getByText('(5 not counted)');
    expect(excluded).toHaveAttribute(
      'title',
      expect.stringMatching(
        /3 with collection incomplete, 2 not successful.*one sample can be both/,
      ),
    );
    expect(within(row).getByText('500 µs')).toBeInTheDocument();
    expect(within(row).getByText('2.5 ms')).toBeInTheDocument();
    expect(within(row).getByText('9 ms')).toBeInTheDocument();
    expect(within(row).getByText('20 ms')).toBeInTheDocument();
    expect(screen.queryByText(/rough figure/)).not.toBeInTheDocument();
  });

  it('renders a percentile with nothing to compute from as a dash, and flags small samples', async () => {
    vi.mocked(summarizeProfilingRun).mockResolvedValue({
      run_id: 'run-1',
      groups: [
        group({ eligible_count: 0, p50_us: null, p95_us: null, min_us: null, max_us: null }),
      ],
    });
    render(<AdminMeasurementsPage />);
    const row = (
      await within(await timings()).findByRole('rowheader', { name: 'File listing' })
    ).closest('tr')!;
    const dashes = within(row).getAllByText('—');
    expect(dashes).toHaveLength(4);
    expect(dashes[0]).toHaveAttribute('title', expect.stringMatching(/nothing to compute/));
    expect(
      screen.getByText(/fewer than 20 counted samples gives only a rough figure/),
    ).toBeInTheDocument();
  });

  it('offers the other runs when there are several, and reads the one picked', async () => {
    vi.mocked(listProfilingRuns).mockResolvedValue({
      runs: [
        run({ run_id: 'run-2', created_at: '2026-09-18T09:30:00Z', target: {} }),
        run({ run_id: 'run-1' }),
      ],
      count: 2,
      limit: 50,
    });
    const replace = vi.spyOn(window.history, 'replaceState');
    render(<AdminMeasurementsPage />);
    const picker = await within(await timings()).findByLabelText('Run');
    expect(picker).toHaveDisplayValue('Sep 18, 09:30 UTC · all hosts');
    expect(vi.mocked(summarizeProfilingRun).mock.calls[0][0]).toBe('run-2');

    fireEvent.change(picker, { target: { value: 'run-1' } });
    await waitFor(() => expect(summarizeProfilingRun).toHaveBeenCalledTimes(2));
    expect(vi.mocked(summarizeProfilingRun).mock.calls[1][0]).toBe('run-1');
    await waitFor(() =>
      expect(vi.mocked(listMeasurements).mock.calls.at(-1)?.[0]).toMatchObject({ run_id: 'run-1' }),
    );
    await waitFor(() => expect(String(replace.mock.calls.at(-1)?.[2])).toBe('/?run=run-1'));
    replace.mockRestore();
  });

  it('shows lost samples only when this server lost some', async () => {
    vi.mocked(getMeasurementWriterStatus).mockResolvedValue({
      scope: 'this_control_plane_process',
      running: true,
      pending: 0,
      in_flight: 0,
      queued_bytes: 0,
      counts: {
        stored: 4,
        duplicate: 0,
        rejected: 0,
        conflict: 0,
        dropped_full: 2,
        dropped_timeout: 1,
        dropped_shutdown: 0,
        failed: 0,
        reasons: {},
      },
    });
    render(<AdminMeasurementsPage />);
    const lost = await screen.findByText(/3 samples were lost on this server/);
    expect(lost).toHaveAttribute(
      'title',
      expect.stringMatching(/2 dropped with the queue full, 1 timed out.*not across every replica/),
    );
  });

  it('reports a revoked session instead of keeping the records it already read', async () => {
    render(<AdminMeasurementsPage />);
    await sampleButton('File listing');
    vi.mocked(listMeasurements).mockRejectedValueOnce(new APIError('INVALID_TOKEN', 'no', 403));
    fireEvent.change(within(samples()).getByLabelText('Operation'), {
      target: { value: 'files.open' },
    });
    await screen.findByText('Admin access required');
    expect(screen.queryByRole('button', { name: 'File listing' })).not.toBeInTheDocument();
  });

  it('shows a failed refresh as an error and keeps the previous read', async () => {
    render(<AdminMeasurementsPage />);
    await sampleButton('File listing');
    vi.mocked(listMeasurements).mockRejectedValueOnce(new APIError('SERVER_ERROR', 'boom', 500));
    fireEvent.click(screen.getByRole('button', { name: 'Refresh' }));
    await within(samples()).findByText(/showing the previous read/);
    expect(await sampleButton('File listing')).toBeInTheDocument();
  });

  it('drops the previous list when a new filter’s read fails, rather than showing it under the new one', async () => {
    render(<AdminMeasurementsPage />);
    await sampleButton('File listing');
    vi.mocked(listMeasurements).mockRejectedValueOnce(new APIError('list_failed', 'boom', 500));
    fireEvent.change(within(samples()).getByLabelText('Operation'), {
      target: { value: 'files.open' },
    });
    await within(samples()).findByText('boom');
    expect(
      within(samples()).queryByRole('button', { name: 'File listing' }),
    ).not.toBeInTheDocument();
    expect(within(samples()).queryByText(/showing the previous read/)).not.toBeInTheDocument();
  });

  it('re-reads every panel on Refresh without losing the selection', async () => {
    search.value = 'run=run-1&operation=files.list&measurement=msm_1';
    render(<AdminMeasurementsPage />);
    await screen.findByRole('table', { name: 'Where the time went' });
    fireEvent.click(screen.getByRole('button', { name: 'Refresh' }));
    await waitFor(() => expect(listProfilingRuns).toHaveBeenCalledTimes(2));
    await waitFor(() => expect(summarizeProfilingRun).toHaveBeenCalledTimes(2));
    await waitFor(() => expect(getMeasurement).toHaveBeenCalledTimes(2));
    await waitFor(() => expect(getMeasurementWriterStatus).toHaveBeenCalledTimes(2));
    expect(vi.mocked(listMeasurements).mock.calls.at(-1)?.[0]).toMatchObject({
      run_id: 'run-1',
      operation: ['files.list'],
    });
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
    fireEvent.click(await screen.findByRole('button', { name: 'Turn on' }));
    await screen.findByRole('option', { name: 'host-a' });
  }

  /** The form's own primary button; the toggle is gone while the form is open. */
  function submit(): void {
    fireEvent.click(screen.getByRole('button', { name: 'Turn on' }));
  }

  it('says off and offers exactly one button, labelled Turn on', async () => {
    render(<AdminMeasurementsPage />);
    await screen.findByRole('button', { name: 'Turn on' });

    expect(within(card()).getByText('Measurement off')).toBeInTheDocument();
    // One button, never two: nothing here offers to stop what is not running.
    expect(within(card()).getAllByRole('button')).toHaveLength(1);
    expect(within(card()).queryByRole('button', { name: /disable/i })).not.toBeInTheDocument();
  });

  it.each<[ProfilingControlStatus, string, RegExp]>([
    ['unavailable', 'Measurement unavailable', /not set up for Admin control/],
    [
      'unmanaged',
      'Measurement set by deployment configuration',
      /own configuration decides what is measured/,
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

    // The one-line status is configuration, and it is not "off".
    expect(within(card()).getByText(line)).toBeInTheDocument();
    expect(within(card()).queryByText('Measurement off')).not.toBeInTheDocument();
    // Not one button: not "Turn on", and not a disabled one either.
    expect(within(card()).queryAllByRole('button')).toHaveLength(0);
  });

  it('opens the inline form on Turn on, with every group on and no host chosen', async () => {
    render(<AdminMeasurementsPage />);
    await openForm();

    expect(screen.queryByRole('dialog')).not.toBeInTheDocument();
    expect(screen.getByRole('checkbox', { name: 'Sandbox startup' })).toBeChecked();
    expect(screen.getByRole('checkbox', { name: 'Terminal opening' })).toBeChecked();
    expect(screen.getByRole('checkbox', { name: 'File access' })).toBeChecked();
    // Nothing is preselected: the admin chooses, and there is no host default.
    expect(screen.getByLabelText('Host')).toHaveValue('');
    expect(screen.queryByLabelText('Duration')).not.toBeInTheDocument();
    expect(screen.getByRole('option', { name: 'Choose a host…' })).toBeInTheDocument();
    expect(screen.getByRole('option', { name: 'All hosts' })).toBeInTheDocument();
    expect(screen.getByRole('option', { name: 'host-a' })).toBeInTheDocument();
    expect(screen.getByRole('option', { name: 'host-b' })).toBeInTheDocument();
    expect(within(card()).queryByRole('button', { name: /disable/i })).not.toBeInTheDocument();
  });

  it('offers enrolled online runners without obsolete registrations', async () => {
    vi.mocked(getAgentRunnerHosts).mockResolvedValue({
      hosts: [
        host(),
        host({ host: 'old-runner', status: 'offline' }),
        host({ host: 'not-enrolled', enrolled: false }),
        host({ host: 'draining-runner', status: 'draining' }),
      ],
      active_host: null,
      runtime_versions: {},
      observed_at: '2026-09-22T00:00:00Z',
    });
    render(<AdminMeasurementsPage />);
    await openForm();
    expect(screen.getByRole('option', { name: 'host-a' })).toBeInTheDocument();
    expect(screen.getByRole('option', { name: 'draining-runner' })).toBeInTheDocument();
    expect(screen.queryByRole('option', { name: 'old-runner' })).not.toBeInTheDocument();
    expect(screen.queryByRole('option', { name: 'not-enrolled' })).not.toBeInTheDocument();
  });

  it('turns on the chosen operations until turned off, with the revision it read', async () => {
    vi.mocked(startProfilingRun).mockImplementation(async () => {
      live = control({
        revision: 4,
        status: 'on',
        run_active: true,
        run: controlRun({
          operations: ['attempt.startup', 'files.list', 'files.open'],
          expires_at: null,
          seconds_remaining: 0,
        }),
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
    expect(body.duration_seconds).toBeNull();
    expect(body.expected_revision).toBe(3);
    expect(typeof body.request_id).toBe('string');
    expect(body.request_id.length).toBeGreaterThan(0);

    // What is drawn is the snapshot the server returned, not a local guess.
    expect(await within(card()).findByText('Measurement on')).toBeInTheDocument();
    expect(within(card()).getByText('Sandbox startup, File access')).toBeInTheDocument();
    expect(within(card()).getByText('host-a')).toBeInTheDocument();
    expect(within(card()).getByText('since Sep 21, 10:00 UTC')).toBeInTheDocument();
    // Collecting until turned off: no deadline and no countdown.
    expect(within(card()).queryByText(/stops/)).not.toBeInTheDocument();
    expect(within(card()).getByText('0 samples')).toBeInTheDocument();
    // A change takes a while to reach every process, and right after one the
    // card says so in the server's words instead of implying instant agreement.
    expect(within(card()).getByText(/allow up to 30 seconds/)).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Turn off' })).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: 'Turn on' })).not.toBeInTheDocument();
  });

  it('flips the single button to Turn off once the run is in force', async () => {
    live = control({ status: 'on', run_active: true, run: controlRun() });
    render(<AdminMeasurementsPage />);
    await screen.findByRole('button', { name: 'Turn off' });

    expect(within(card()).getByText('Measurement on')).toBeInTheDocument();
    expect(within(card()).getAllByRole('button')).toHaveLength(1);
    expect(within(card()).queryByRole('button', { name: 'Turn on' })).not.toBeInTheDocument();
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
    expect(await within(card()).findByText('All hosts')).toBeInTheDocument();
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
    submit();

    await screen.findByText('measurement storage is unavailable');
    // The form is still open with the values that produced the failure.
    expect(screen.getByRole('checkbox', { name: 'File access' })).not.toBeChecked();
    expect(screen.getByLabelText('Host')).toHaveValue('host-b');
    expect(screen.queryByLabelText('Duration')).not.toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Turn on' })).toBeInTheDocument();
    // And nothing was drawn as though a run had begun.
    expect(within(card()).getByText('Measurement off')).toBeInTheDocument();
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
      fireEvent.click(await screen.findByRole('button', { name: 'Turn off' }));

      await waitFor(() => expect(stopProfilingRun).toHaveBeenCalledTimes(1));
      expect(vi.mocked(stopProfilingRun).mock.calls[0][0]).toBe('prun-1');
      expect(vi.mocked(stopProfilingRun).mock.calls[0][1]).toEqual({
        request_id: expect.any(String),
      });
      // No dialog stands between the click and the command.
      expect(confirm).not.toHaveBeenCalled();
      expect(screen.queryByRole('dialog')).not.toBeInTheDocument();

      expect(await within(card()).findByText('Measurement off')).toBeInTheDocument();
      // An admin's stop is the ordinary one and needs no reason beside it.
      expect(within(card()).getByText('last run stopped Sep 21, 10:03 UTC')).toHaveAttribute(
        'title',
        'prun-1',
      );
      expect(screen.getByRole('button', { name: 'Turn on' })).toBeInTheDocument();
      expect(screen.queryByRole('button', { name: 'Turn off' })).not.toBeInTheDocument();
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
    fireEvent.click(await screen.findByRole('button', { name: 'Turn off' }));

    await waitFor(() =>
      expect(screen.getByRole('button', { name: 'Turning off…' })).toBeDisabled(),
    );
    await act(async () => {
      release();
    });
    await screen.findByRole('button', { name: 'Turn on' });
  });

  it('hints that nothing has been stored yet while a live run has no samples', async () => {
    live = control({ status: 'on', run_active: true, run: controlRun(), stored_samples: 0 });
    render(<AdminMeasurementsPage />);
    const count = await within(card()).findByText('0 samples');
    // What one sample is, where the count is.
    expect(count).toHaveAttribute('title', expect.stringMatching(/one server-side record/));
    expect(
      within(card()).getByText(/No samples yet\. Use a sandbox, open a terminal or browse files/),
    ).toBeInTheDocument();
    // A deadline, when the run has one, counts down on the same line.
    expect(within(card()).getByText('stops in 10:00')).toBeInTheDocument();

    live = control({ status: 'on', run_active: true, run: controlRun(), stored_samples: 7 });
    fireEvent.click(screen.getByRole('button', { name: 'Refresh' }));
    await within(card()).findByText('7 samples');
    expect(within(card()).queryByText(/No samples yet/)).not.toBeInTheDocument();
  });

  it('names an unusual stop reason beside the last run', async () => {
    live = control({
      last_stopped_run: controlRun({ stop_reason: 'expired', stopped_at: '2026-09-21T10:10:00Z' }),
    });
    render(<AdminMeasurementsPage />);
    expect(
      await within(card()).findByText('last run stopped Sep 21, 10:10 UTC (expired)'),
    ).toBeInTheDocument();
  });

  it('keeps the sync note to a tooltip until a change is made here, and no diagnostics', async () => {
    render(<AdminMeasurementsPage />);
    await screen.findByRole('button', { name: 'Turn on' });

    expect(within(card()).getByText('Measurement off')).toHaveAttribute(
      'title',
      expect.stringMatching(/^The configuration reaches .*allow up to 30 seconds.*\.$/),
    );
    expect(within(card()).queryByText(/allow up to 30 seconds/)).not.toBeInTheDocument();

    expect(card().querySelector('details')).toBeNull();
    expect(within(card()).queryByText('Mode')).not.toBeInTheDocument();
    expect(within(card()).queryByText('Revision')).not.toBeInTheDocument();
    expect(within(card()).queryByText('producer_instance')).not.toBeInTheDocument();
  });

  it('clears the page for a revoked control read rather than showing a state', async () => {
    vi.mocked(getProfilingControl).mockRejectedValue(new APIError('INVALID_TOKEN', 'no', 403));
    render(<AdminMeasurementsPage />);
    await screen.findByText('Admin access required');
    expect(screen.queryByText('Measurement collection')).not.toBeInTheDocument();
    expect(screen.queryByText('Measurement off')).not.toBeInTheDocument();
  });

  it('keeps the last successful read when a background poll fails, flagged as stale', async () => {
    vi.mocked(getProfilingControl).mockRejectedValueOnce(new APIError('SERVER_ERROR', 'boom', 500));
    render(<AdminMeasurementsPage />);
    // The first read failed and nothing has been shown yet, so the card says
    // so instead of inventing an Off state.
    await screen.findByRole('alert');
    expect(within(card()).queryByRole('button', { name: 'Turn on' })).not.toBeInTheDocument();

    fireEvent.click(screen.getByRole('button', { name: 'Refresh' }));
    await within(card()).findByText('Measurement off');

    vi.mocked(getProfilingControl).mockRejectedValueOnce(new APIError('SERVER_ERROR', 'boom', 500));
    fireEvent.click(screen.getByRole('button', { name: 'Refresh' }));
    await screen.findByText(/showing the last successful read/);
    // The last good state is still on screen, and still says Off.
    expect(within(card()).getByText('Measurement off')).toBeInTheDocument();
    expect(within(card()).getByRole('button', { name: 'Turn on' })).toBeEnabled();
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
    await screen.findByRole('button', { name: 'Turn off' });
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
