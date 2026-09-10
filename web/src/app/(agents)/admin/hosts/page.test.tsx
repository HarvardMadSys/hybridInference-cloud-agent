// @vitest-environment jsdom
import '@testing-library/jest-dom/vitest';

import { cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

const auth = vi.hoisted(() => ({ role: 'admin' }));

vi.mock('@/components/providers', () => ({
  useAuth: () => ({ state: { user: { user_name: 'Ada', role: auth.role } } }),
}));

// The real ranking, not a stub that always says yes: the point of two of these
// cases is that a non-admin is refused, and a `hasRole` mocked to true would
// pass them while the page was wide open.
vi.mock('@/components/providers/AuthProvider', async () => {
  const actual = await vi.importActual<typeof import('@/components/providers/AuthProvider')>(
    '@/components/providers/AuthProvider',
  );
  return { hasRole: actual.hasRole };
});

vi.mock('@/lib/api/admin', () => ({
  getAgentRunnerHosts: vi.fn(),
  setActiveAgentRunnerHost: vi.fn(),
  forgetAgentRunnerHost: vi.fn(),
  drainAgentRunnerHost: vi.fn(),
  undrainAgentRunnerHost: vi.fn(),
  removeAgentRunnerHost: vi.fn(),
  setAgentRuntimeVersion: vi.fn(),
}));

import {
  drainAgentRunnerHost,
  getAgentRunnerHosts,
  removeAgentRunnerHost,
  setActiveAgentRunnerHost,
  setAgentRuntimeVersion,
} from '@/lib/api/admin';

import { formatAge, formatSlots, runtimeMenu } from './format';
import AdminHostsPage from './page';

const mocked = {
  list: getAgentRunnerHosts as unknown as ReturnType<typeof vi.fn>,
  pin: setActiveAgentRunnerHost as unknown as ReturnType<typeof vi.fn>,
  drain: drainAgentRunnerHost as unknown as ReturnType<typeof vi.fn>,
  remove: removeAgentRunnerHost as unknown as ReturnType<typeof vi.fn>,
  setVersion: setAgentRuntimeVersion as unknown as ReturnType<typeof vi.fn>,
};

function host(over: Partial<Record<string, unknown>> = {}) {
  return {
    host: 'runner-a',
    active: false,
    last_worker_id: 'worker-1',
    first_seen_at: '2026-08-01T00:00:00Z',
    last_seen_at: '2026-08-05T00:00:00Z',
    seconds_since_seen: 12,
    status: 'online',
    enrolled: true,
    labels: {},
    capabilities: ['kata'],
    slots_total: 4,
    slots_used: 0,
    runner_version: '1.2.3',
    broker_url: 'http://runner-a:8092',
    last_heartbeat_at: '2026-08-05T00:00:00Z',
    sandbox_runtime: '',
    sandbox_image_digest: 'freeinference-cloud-agent-job@sha256:abcdef0123456789',
    runtime_versions: { 'claude-code': '2.1.220' },
    runtime_versions_available: { 'claude-code': ['2.1.220', '2.1.235'] },
    ...over,
  };
}

function pool(
  hosts: unknown[],
  active: string | null = null,
  runtimeVersions: Record<string, string> = {},
) {
  return { hosts, active_host: active, runtime_versions: runtimeVersions };
}

describe('AdminHostsPage', () => {
  beforeEach(() => {
    auth.role = 'admin';
    Object.values(mocked).forEach((m) => m.mockReset());
    mocked.list.mockResolvedValue(pool([host()]));
  });

  afterEach(() => {
    cleanup();
    vi.unstubAllGlobals();
  });

  it('refuses a non-admin without asking the control plane', async () => {
    auth.role = 'internal';
    render(<AdminHostsPage />);

    expect(await screen.findByText('Admin access required')).toBeInTheDocument();
    // Not merely hidden: a page that renders the refusal *and* fetches has told
    // the operator no while doing the thing anyway.
    expect(mocked.list).not.toHaveBeenCalled();
  });

  it('renders the pool a host that joined by polling would produce', async () => {
    // Every dynamic-pool field null/empty, which is exactly what a pre-Phase-G
    // row looks like. This must render rather than 500 the page an operator
    // opens to fix that host.
    mocked.list.mockResolvedValue(
      pool([
        host({
          host: 'legacy-box',
          enrolled: false,
          capabilities: [],
          slots_total: null,
          runner_version: null,
        }),
      ]),
    );
    render(<AdminHostsPage />);

    expect(await screen.findByText('legacy-box')).toBeInTheDocument();
    // No ceiling declared: the slot cell is the count alone, not "0 / null".
    expect(screen.getByText('0')).toBeInTheDocument();
  });

  it('groups deployments on one server while keeping actions scoped to their identity', async () => {
    const rows = [
      host({ host: 'gpu-box', labels: { physical_host: 'gpu-box' }, sandbox_runtime: 'runc' }),
      host({
        host: 'gpu-box-kata',
        labels: { physical_host: 'gpu-box' },
        sandbox_runtime: 'io.containerd.kata.v2',
      }),
    ];
    mocked.list.mockResolvedValue(pool(rows));
    mocked.pin.mockResolvedValue(pool(rows));
    mocked.drain.mockResolvedValue(pool(rows));
    render(<AdminHostsPage />);

    const server = await screen.findByRole('rowgroup', { name: 'gpu-box' });
    expect(within(server).getAllByRole('row')).toHaveLength(2);
    expect(within(server).getByRole('rowheader', { name: 'gpu-box' })).toHaveAttribute(
      'rowspan',
      '2',
    );
    const kata = within(server).getByText('Kata').closest('tr')!;
    fireEvent.click(within(kata).getByRole('button', { name: 'Pin' }));
    await waitFor(() => expect(mocked.pin).toHaveBeenCalledWith('gpu-box-kata'));
    await waitFor(() => expect(within(kata).getByRole('button', { name: 'Drain' })).toBeEnabled());
    fireEvent.click(within(kata).getByRole('button', { name: 'Drain' }));
    await waitFor(() => expect(mocked.drain).toHaveBeenCalledWith('gpu-box-kata'));
  });

  it('does not infer physical servers from deployment name suffixes', async () => {
    mocked.list.mockResolvedValue(
      pool([host({ host: 'gpu-box' }), host({ host: 'gpu-box-kata' })]),
    );
    render(<AdminHostsPage />);
    expect(await screen.findByRole('rowgroup', { name: 'gpu-box' })).toBeInTheDocument();
    expect(screen.getByRole('rowgroup', { name: 'gpu-box-kata' })).toBeInTheDocument();
  });

  it('pins new chats and reports that retained chats stay on their owning hosts', async () => {
    mocked.pin.mockResolvedValue(pool([host({ active: true })], 'runner-a'));
    render(<AdminHostsPage />);

    fireEvent.click(await screen.findByRole('button', { name: 'Pin' }));

    await waitFor(() => expect(mocked.pin).toHaveBeenCalledWith('runner-a'));
    expect(await screen.findByRole('status')).toHaveTextContent(
      /Existing retained chats stay on their owning hosts/,
    );
  });

  it('unpins through the null the API expects, not a sentinel string', async () => {
    mocked.list.mockResolvedValue(pool([host({ active: true })], 'runner-a'));
    mocked.pin.mockResolvedValue(pool([host()], null));
    render(<AdminHostsPage />);

    fireEvent.click(await screen.findByRole('button', { name: 'Any host' }));

    await waitFor(() => expect(mocked.pin).toHaveBeenCalledWith(null));
  });

  it('drains without touching the pin', async () => {
    mocked.drain.mockResolvedValue(pool([host({ status: 'draining' })]));
    render(<AdminHostsPage />);

    fireEvent.click(await screen.findByRole('button', { name: 'Drain' }));

    await waitFor(() => expect(mocked.drain).toHaveBeenCalledWith('runner-a'));
    expect(mocked.pin).not.toHaveBeenCalled();
    expect(await screen.findByRole('button', { name: 'Undrain' })).toBeInTheDocument();
  });

  it('removes a quiet host unforced', async () => {
    mocked.remove.mockResolvedValue(pool([]));
    render(<AdminHostsPage />);

    fireEvent.click(await screen.findByRole('button', { name: 'Remove' }));

    await waitFor(() => expect(mocked.remove).toHaveBeenCalledWith('runner-a', false));
  });

  it('makes the operator confirm before forcing a busy host out, and says why', async () => {
    mocked.list.mockResolvedValue(pool([host({ slots_used: 2 })]));
    const confirm = vi.fn().mockReturnValue(false);
    vi.stubGlobal('confirm', confirm);
    render(<AdminHostsPage />);

    fireEvent.click(await screen.findByRole('button', { name: 'Remove' }));

    expect(confirm).toHaveBeenCalledTimes(1);
    // The consequence has to be in the prompt: force does not stop the
    // attempts, it leaves them to their leases.
    expect(confirm.mock.calls[0][0]).toMatch(/does not stop them/);
    expect(mocked.remove).not.toHaveBeenCalled();

    confirm.mockReturnValue(true);
    mocked.remove.mockResolvedValue(pool([]));
    fireEvent.click(screen.getByRole('button', { name: 'Remove' }));
    await waitFor(() => expect(mocked.remove).toHaveBeenCalledWith('runner-a', true));
  });

  it('warns when the pinned host has gone quiet', async () => {
    mocked.list.mockResolvedValue(
      pool([host({ active: true, seconds_since_seen: 3600 })], 'runner-a'),
    );
    render(<AdminHostsPage />);

    expect(
      await screen.findByText(/New chats are pinned to it, so they will wait/),
    ).toBeInTheDocument();
  });

  it('does not warn when an unpinned host is quiet — nothing depends on it', async () => {
    mocked.list.mockResolvedValue(pool([host({ seconds_since_seen: 3600 })], null));
    render(<AdminHostsPage />);

    await screen.findByText('runner-a');
    expect(screen.queryByText(/the queue is stopped/)).not.toBeInTheDocument();
  });

  it('does not offer to enrol a host', async () => {
    // Removed on purpose, not lost. Enrolment binds a credential to a machine
    // so it cannot claim to be another one, and every runner in this pool is
    // ours — the control answered a question nobody operating this page asks.
    // The endpoint and its client are untouched for the rare new host.
    render(<AdminHostsPage />);

    await screen.findByText('runner-a');
    expect(screen.queryByRole('button', { name: 'Issue token' })).not.toBeInTheDocument();
    expect(screen.queryByText(/enrol/i)).not.toBeInTheDocument();
  });

  it('keeps the last good pool when a background refresh fails', async () => {
    vi.useFakeTimers();
    try {
      mocked.list.mockResolvedValueOnce(pool([host()]));
      render(<AdminHostsPage />);
      await vi.waitFor(() => expect(screen.getByText('runner-a')).toBeInTheDocument());

      mocked.list.mockRejectedValue(new Error('gateway down'));
      await vi.advanceTimersByTimeAsync(30_000);

      // Stale numbers are a smaller lie than an empty page.
      expect(screen.getByText('runner-a')).toBeInTheDocument();
      expect(screen.queryByText('gateway down')).not.toBeInTheDocument();
    } finally {
      vi.useRealTimers();
    }
  });
});

describe('formatting', () => {
  it('reports poll age in the largest unit that stays readable', () => {
    expect(formatAge(12)).toBe('12s ago');
    expect(formatAge(120)).toBe('2m ago');
    expect(formatAge(7200)).toBe('2h ago');
    expect(formatAge(172800)).toBe('2d ago');
  });

  it('omits the ceiling for a host that declares none', () => {
    expect(formatSlots({ slots_used: 2, slots_total: 4 } as never)).toBe('2 / 4');
    expect(formatSlots({ slots_used: 2, slots_total: null } as never)).toBe('2');
  });
});

describe('harness version selection', () => {
  beforeEach(() => {
    auth.role = 'admin';
    Object.values(mocked).forEach((m) => m.mockReset());
    mocked.list.mockResolvedValue(pool([host()]));
  });

  afterEach(cleanup);

  it('offers every version the pool holds, with the image default first', async () => {
    render(<AdminHostsPage />);

    const select = (await screen.findByLabelText('claude-code')) as HTMLSelectElement;
    expect([...select.options].map((o) => o.textContent)).toEqual([
      'Image default',
      '2.1.220',
      '2.1.235',
    ]);
    // Nothing selected means the image default — not "2.1.220 happens to be
    // first". The two differ the moment a host is rebuilt.
    expect(select.value).toBe('');
  });

  it('shows the deployment-wide selection, not a per-host value', async () => {
    mocked.list.mockResolvedValue(pool([host()], null, { 'claude-code': '2.1.235' }));
    render(<AdminHostsPage />);

    const select = (await screen.findByLabelText('claude-code')) as HTMLSelectElement;
    await waitFor(() => expect(select.value).toBe('2.1.235'));
  });

  it('sends the choice and takes the whole pool back', async () => {
    mocked.setVersion.mockResolvedValue(pool([host()], null, { 'claude-code': '2.1.235' }));
    render(<AdminHostsPage />);

    const select = await screen.findByLabelText('claude-code');
    fireEvent.change(select, { target: { value: '2.1.235' } });

    await waitFor(() => expect(mocked.setVersion).toHaveBeenCalledWith('claude-code', '2.1.235'));
    // The wording is the contract with the operator: this is not retroactive,
    // and a page that implied otherwise would be the bug.
    expect(
      await screen.findByText(/New claude-code threads will run 2\.1\.235/),
    ).toBeInTheDocument();
  });

  it('clears back to the image default with null, not an empty string', async () => {
    mocked.list.mockResolvedValue(pool([host()], null, { 'claude-code': '2.1.235' }));
    mocked.setVersion.mockResolvedValue(pool([host()]));
    render(<AdminHostsPage />);

    const select = await screen.findByLabelText('claude-code');
    fireEvent.change(select, { target: { value: '' } });

    await waitFor(() => expect(mocked.setVersion).toHaveBeenCalledWith('claude-code', null));
  });

  it('hides the selector when no host reports a menu', async () => {
    // A pool of pre-menu hosts. An empty control that cannot do anything is
    // worse than no control: it reads as broken rather than as absent.
    mocked.list.mockResolvedValue(pool([host({ runtime_versions_available: null })]));
    render(<AdminHostsPage />);

    await screen.findByText('runner-a');
    expect(screen.queryByText('Harness version')).not.toBeInTheDocument();
  });
});

describe('version and image cells', () => {
  it('unions menus across hosts without losing the default ordering', () => {
    const menu = runtimeMenu([
      host({ runtime_versions_available: { 'claude-code': ['2.1.220'] } }),
      host({
        runtime_versions_available: { 'claude-code': ['2.1.220', '2.1.235'], pi: ['0.73.1'] },
      }),
    ] as never);
    expect(menu).toEqual([
      ['claude-code', ['2.1.220', '2.1.235']],
      ['pi', ['0.73.1']],
    ]);
  });
});

describe('the harness section reads as a choice, not a list', () => {
  beforeEach(() => {
    auth.role = 'admin';
    Object.values(mocked).forEach((m) => m.mockReset());
    mocked.list.mockResolvedValue(pool([host()]));
  });

  afterEach(cleanup);

  it('is the runtimes and their controls, and no prose', async () => {
    // Everything this section could explain is already said elsewhere: the
    // versions on offer are in the select, what each host holds is in the
    // table below, that a running conversation keeps its version is in the
    // confirmation after the change, and how to add a version is deployment
    // documentation. Written here it was a third telling every time.
    render(<AdminHostsPage />);

    await screen.findByLabelText('claude-code');
    expect(screen.queryByText(/also built in/)).not.toBeInTheDocument();
    expect(screen.queryByText(/The image holds only/)).not.toBeInTheDocument();
    expect(screen.queryByText(/EXTRA_VERSIONS/)).not.toBeInTheDocument();
    expect(screen.queryByText(/Applies to new threads/)).not.toBeInTheDocument();
  });

  it('still offers a fixed runtime, compressed but not removed', async () => {
    // Pinning a single-version runtime is a weak choice, not an absent one:
    // the control stays, it just stops taking three lines to say nothing.
    mocked.list.mockResolvedValue(
      pool([
        host({
          runtime_versions_available: {
            'claude-code': ['2.1.220', '2.1.235'],
            codex: ['0.145.0'],
          },
        }),
      ]),
    );
    render(<AdminHostsPage />);

    const codex = (await screen.findByLabelText('codex')) as HTMLSelectElement;
    expect([...codex.options].map((o) => o.textContent)).toEqual(['Image default', '0.145.0']);
  });
});
