// @vitest-environment jsdom
import '@testing-library/jest-dom/vitest';

import { act, cleanup, fireEvent, render, screen, within } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { getAdminSandbox, getAdminSandboxes, type AdminSandbox } from '@/lib/api/adminSandboxes';
import { SandboxResults } from './SandboxResults';
import { summarizeSandboxes } from './present';
import { selectSandboxes } from './inventory';
import { chooseOption } from '@/components/ui/__tests__/select';

vi.mock('@/lib/api/adminSandboxes', () => ({
  getAdminSandboxes: vi.fn(),
  getAdminSandbox: vi.fn(),
  archiveAdminSandbox: vi.fn(),
  deleteAdminSandbox: vi.fn(),
  restoreAdminSandbox: vi.fn(),
  stopAdminSandbox: vi.fn(),
}));
vi.mock('./present', async (original) => {
  const actual = await original<typeof import('./present')>();
  return { ...actual, summarizeSandboxes: vi.fn(actual.summarizeSandboxes) };
});
vi.mock('./inventory', async (original) => {
  const actual = await original<typeof import('./inventory')>();
  return { ...actual, selectSandboxes: vi.fn(actual.selectSandboxes) };
});

const observedAt = '2026-10-10T10:00:00Z';
function sandbox(index: number, age = 5): AdminSandbox {
  return {
    id: `sandbox-${index}`,
    generation: 1,
    state: 'running',
    origin_thread_id: `thread-${index}`,
    title: `Session ${index}`,
    repo: 'org/repo',
    user_id: 'owner',
    host: 'host',
    container_id: `container-${index}`,
    container_name: `container-${index}`,
    runtime_id: 'codex',
    runtime_version: '1',
    execution_profile: 'trusted-v1',
    memory_limit: '4g',
    cpu_limit: '2',
    member_count: 1,
    active_attempt_count: 0,
    terminal_count: 0,
    terminal_count_observed_at: observedAt,
    created_at: '2026-10-10T09:00:00Z',
    last_activity_at: '2026-10-10T09:59:00Z',
    idle_deadline: null,
    updated_at: observedAt,
    host_last_heartbeat_at: observedAt,
    resource_usage: {
      source: 'docker-stats',
      scope: 'container',
      sample_id: `sample-${index}`,
      sampled_at: observedAt,
      received_at: observedAt,
      cpu_window_seconds: 30,
      pids: 1,
      io_read_bytes: 0,
      io_write_bytes: 0,
      net_rx_bytes: 0,
      net_tx_bytes: 0,
      io_read_bytes_per_second: 0,
      io_write_bytes_per_second: 0,
      net_rx_bytes_per_second: 0,
      net_tx_bytes_per_second: 0,
      age_seconds: age,
      stale: false,
      cpu_cores: 0.25,
      memory_bytes: 1024,
      memory_limit_bytes: 4 * 1024 ** 3,
    },
  };
}

function inventory(rows: AdminSandbox[]) {
  vi.mocked(getAdminSandboxes).mockImplementation(async ({ cursor } = {}) => {
    const offset = Number(cursor ?? 0);
    return {
      sandboxes: rows.slice(offset, offset + 200),
      observed_at: observedAt,
      total_count: rows.length,
      next_cursor: offset + 200 < rows.length ? String(offset + 200) : null,
    };
  });
  vi.mocked(getAdminSandbox).mockResolvedValue({
    sandbox: rows[0],
    members: [],
    observed_at: observedAt,
  });
}

async function mount() {
  const view = render(<SandboxResults onDenied={() => {}} />);
  await act(async () => {});
  fireEvent.click(screen.getByRole('checkbox', { name: 'Auto-refresh' }));
  return view;
}

function cpuTotal() {
  return screen.getByText('Measured CPU', { selector: 'dt' }).nextElementSibling;
}

beforeEach(() => {
  vi.clearAllMocks();
  vi.useFakeTimers({ toFake: ['Date', 'performance', 'setInterval', 'clearInterval'] });
  vi.setSystemTime(new Date(observedAt));
});
afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
  vi.useRealTimers();
});

describe('sandbox inventory clock isolation', () => {
  it('ticks ages without recomputing 2,000 rows, inventory selection, or fleet totals', async () => {
    let rowQuotaReads = 0;
    const rows = Array.from({ length: 2_000 }, (_, index) => {
      const row = sandbox(index);
      Object.defineProperty(row, 'cpu_limit', {
        get: () => {
          rowQuotaReads += 1;
          return '2';
        },
      });
      return row;
    });
    inventory(rows);
    const view = await mount();
    expect(view.container.querySelectorAll('tbody > tr')).toHaveLength(2_000);
    expect(rowQuotaReads).toBeGreaterThanOrEqual(2_000);
    const reads = rowQuotaReads;
    vi.mocked(summarizeSandboxes).mockClear();
    vi.mocked(selectSandboxes).mockClear();
    const firstRow = view.container.querySelector('tbody > tr')!;
    expect(firstRow).toHaveTextContent('Sample · 5s old');

    await act(async () => vi.advanceTimersByTimeAsync(1_000));

    expect(firstRow).toHaveTextContent('Sample · 6s old');
    expect(rowQuotaReads).toBe(reads);
    expect(summarizeSandboxes).not.toHaveBeenCalled();
    expect(selectSandboxes).not.toHaveBeenCalled();
    expect(getAdminSandboxes).toHaveBeenCalledTimes(10);
  }, 30_000);

  it('updates totals only when retained samples cross their individual freshness deadlines', async () => {
    inventory([sandbox(0, 89), sandbox(1, 80)]);
    await mount();
    vi.mocked(summarizeSandboxes).mockClear();
    expect(cpuTotal()).toHaveTextContent('0.50 cores');
    await act(async () => vi.advanceTimersByTimeAsync(1_000));
    expect(summarizeSandboxes).not.toHaveBeenCalled();
    expect(cpuTotal()).toHaveTextContent('0.50 cores');
    await act(async () => vi.advanceTimersByTimeAsync(1_000));
    expect(summarizeSandboxes).toHaveBeenCalledTimes(1);
    expect(cpuTotal()).toHaveTextContent('0.25 cores');
    await act(async () => vi.advanceTimersByTimeAsync(8_000));
    expect(summarizeSandboxes).toHaveBeenCalledTimes(1);
    await act(async () => vi.advanceTimersByTimeAsync(1_000));
    expect(summarizeSandboxes).toHaveBeenCalledTimes(2);
    expect(cpuTotal()).toHaveTextContent('Unavailable');
  });

  it('pauses hidden clock work and catches up age, heartbeat, and sample freshness on return', async () => {
    inventory([sandbox(0)]);
    await mount();
    const visibility = vi.spyOn(document, 'visibilityState', 'get').mockReturnValue('hidden');
    act(() => document.dispatchEvent(new Event('visibilitychange')));
    vi.mocked(summarizeSandboxes).mockClear();
    await act(async () => vi.advanceTimersByTimeAsync(91_000));
    expect(summarizeSandboxes).not.toHaveBeenCalled();
    expect(screen.getByText('Sample · 5s old')).toBeInTheDocument();
    visibility.mockReturnValue('visible');
    act(() => document.dispatchEvent(new Event('visibilitychange')));
    expect(screen.getByText('Stale sample · 1m 36s old')).toBeInTheDocument();
    expect(screen.getByText('Stale')).toBeInTheDocument();
    expect(cpuTotal()).toHaveTextContent('Unavailable');
    expect(summarizeSandboxes).toHaveBeenCalledTimes(1);
  });

  it('keeps row expansion and selection while individual and batch reviews age out', async () => {
    inventory([sandbox(0)]);
    await mount();
    fireEvent.click(screen.getByRole('button', { name: 'Show details for Session 0' }));
    fireEvent.click(screen.getByRole('checkbox', { name: 'Select sandbox sandbox-0' }));
    chooseOption(
      screen.getByRole('combobox', { name: 'Batch sandbox action' }),
      'Stop idle sandboxes',
    );
    fireEvent.click(screen.getByRole('button', { name: 'Review selected sandboxes' }));
    await act(async () => {});
    expect(screen.getByRole('button', { name: 'Stop idle sandbox' })).toBeEnabled();
    const management = screen.getByRole('region', { name: 'Sandbox management' });
    expect(screen.getByRole('button', { name: 'Apply to 1 sandboxes' })).toBeEnabled();

    await act(async () => vi.advanceTimersByTimeAsync(91_000));

    expect(screen.getByRole('button', { name: 'Hide details for Session 0' })).toHaveAttribute(
      'aria-expanded',
      'true',
    );
    expect(screen.getByRole('checkbox', { name: 'Select sandbox sandbox-0' })).toBeChecked();
    expect(screen.getByRole('combobox', { name: 'Batch sandbox action' })).toHaveTextContent(
      'Stop idle sandboxes',
    );
    expect(screen.getByRole('region', { name: 'Sandbox management' })).toBe(management);
    expect(within(management).getByRole('button', { name: 'Stop idle sandbox' })).toBeDisabled();
    expect(
      within(management).getByText('Refresh details before changing this sandbox.'),
    ).toBeInTheDocument();
    expect(screen.getByRole('list', { name: 'Sandbox batch review' })).toHaveTextContent(
      'Refresh details before changing this sandbox.',
    );
    expect(screen.getByRole('button', { name: 'Apply to 0 sandboxes' })).toBeDisabled();
  });
});
