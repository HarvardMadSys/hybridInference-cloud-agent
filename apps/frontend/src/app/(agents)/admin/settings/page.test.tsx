// @vitest-environment jsdom
import '@testing-library/jest-dom/vitest';

import { cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import type { AgentRunnerHost, AgentRunnerHostPool, HostSettingDefinition } from '@/lib/api/admin';
import type { AgentAccessApi } from '@/lib/api/session';
import { APIError } from '@/lib/utils/errors';

const auth = vi.hoisted(() => ({ id: 'admin-a', access: undefined as AgentAccessApi | undefined }));
vi.mock('@/components/providers', () => ({
  useAuth: () => ({ state: { user: { id: auth.id, role: 'free', agent_access: auth.access } } }),
}));
vi.mock('@/components/providers/AuthProvider', async () => {
  const actual = await vi.importActual<typeof import('@/components/providers/AuthProvider')>(
    '@/components/providers/AuthProvider',
  );
  return { hasAgentPermission: actual.hasAgentPermission };
});
vi.mock('@/lib/api/admin', () => ({
  getAgentRunnerHosts: vi.fn(),
  setAgentHostSettings: vi.fn(),
  setAgentRuntimeVersion: vi.fn(),
  HOST_SETTINGS_CAPABILITY: 'host-settings-v1',
}));

import { getAgentRunnerHosts, setAgentHostSettings, setAgentRuntimeVersion } from '@/lib/api/admin';

import AdminSettingsPage from './page';

const list = vi.mocked(getAgentRunnerHosts);
const setLimit = vi.mocked(setAgentHostSettings);
const setVersion = vi.mocked(setAgentRuntimeVersion);

const definitions: HostSettingDefinition[] = [
  {
    key: 'agent_timeout_s',
    env: 'AGENT_TIMEOUT_S',
    label: 'Turn time limit',
    description: 'How long one agent turn may run before the host stops it.',
    unit: 'seconds',
    default: 3600,
    minimum: 60,
    maximum: 86400,
    scope: 'runner',
  },
  {
    key: 'source_import_max_mib',
    env: 'AGENT_SOURCE_IMPORT_MAX_MIB',
    label: 'Repository size limit',
    description: 'Maximum checkout size.',
    unit: 'MiB',
    default: 1024,
    minimum: 1,
    maximum: 65536,
    scope: 'runner',
  },
  {
    key: 'chat_idle_timeout_s',
    env: 'AGENT_CHAT_IDLE_TIMEOUT_S',
    label: 'Sandbox idle timeout',
    description: 'When an idle sandbox stops.',
    unit: 'seconds',
    default: 1800,
    minimum: 60,
    maximum: 604800,
    scope: 'both',
  },
  {
    key: 'shared_resident_capacity',
    env: 'AGENT_SHARED_RESIDENT_CAPACITY',
    label: 'Sessions per sandbox',
    description: 'Capacity of a new sandbox. Existing sandboxes keep theirs.',
    unit: 'count',
    default: 8,
    minimum: 2,
    maximum: 128,
    scope: 'runner',
  },
  {
    key: 'terminal_idle_ttl_s',
    env: 'AGENT_TERMINAL_IDLE_TTL_SECONDS',
    label: 'Terminal idle timeout',
    description: 'When an idle terminal closes.',
    unit: 'seconds',
    default: 1800,
    minimum: 60,
    maximum: 86400,
    scope: 'broker',
  },
  {
    key: 'terminal_hard_ttl_s',
    env: 'AGENT_TERMINAL_HARD_TTL_SECONDS',
    label: 'Terminal max lifetime',
    description: 'Maximum terminal lifetime.',
    unit: 'seconds',
    default: 28800,
    minimum: 60,
    maximum: 86400,
    scope: 'broker',
  },
  {
    key: 'terminal_max_sessions',
    env: 'AGENT_TERMINAL_MAX_SESSIONS',
    label: 'Terminals per server',
    description: 'How many interactive terminals one server may hold open at once.',
    unit: 'count',
    default: 64,
    minimum: 1,
    maximum: 4096,
    scope: 'broker',
  },
];

function host(over: Partial<AgentRunnerHost> = {}): AgentRunnerHost {
  return {
    host: 'runner-a',
    status: 'online',
    enrolled: true,
    active: false,
    sandbox_runtime: 'io.containerd.kata.v2',
    labels: {},
    capabilities: ['host-settings-v1'],
    runtime_versions: { 'claude-code': '2.1.220' },
    runtime_versions_available: { 'claude-code': ['2.1.220', '2.1.235'] },
    settings_desired: {},
    settings_applied: { agent_timeout_s: 3600, terminal_max_sessions: 64 },
    settings_local: { agent_timeout_s: 3600, terminal_max_sessions: 64 },
    ...over,
  } as AgentRunnerHost;
}

function pool(over: Partial<AgentRunnerHostPool> = {}): AgentRunnerHostPool {
  return {
    hosts: [host()],
    active_host: null,
    runtime_versions: {},
    execution_profile: 'untrusted-kata-v1',
    observed_at: '2026-10-08T00:00:00Z',
    host_settings: {},
    host_setting_definitions: definitions,
    ...over,
  };
}

function submit(control: HTMLElement) {
  fireEvent.submit(control.closest('form')!);
}

beforeEach(() => {
  auth.id = 'admin-a';
  auth.access = { allowed: true, permissions: ['agent.use', 'agent.admin'] };
  vi.clearAllMocks();
  list.mockReset().mockResolvedValue(pool());
  setLimit.mockReset();
  setVersion.mockReset();
  vi.spyOn(document, 'hidden', 'get').mockReturnValue(false);
});

afterEach(() => {
  cleanup();
  vi.useRealTimers();
  vi.restoreAllMocks();
});

describe('admin Settings', () => {
  it('requires an explicit admin permission before fetching', () => {
    auth.access = { allowed: true, permissions: ['agent.use'] };
    render(<AdminSettingsPage />);
    expect(screen.getByText('Admin access required')).toBeInTheDocument();
    expect(list).not.toHaveBeenCalled();
  });

  it('admits a free account with admin permission and shows versions and sandbox settings', async () => {
    render(<AdminSettingsPage />);
    await screen.findByLabelText('Claude Code');
    expect(screen.getByRole('link', { name: 'Settings' })).toHaveAttribute('aria-current', 'page');
    expect(screen.getAllByRole('heading', { level: 2 }).map((h) => h.textContent)).toEqual([
      'Agent versions',
      'Sandbox',
    ]);
    expect(screen.queryByLabelText('New sandbox location')).not.toBeInTheDocument();
    expect(screen.queryByText('Runtime settings')).not.toBeInTheDocument();
    expect(
      screen.getByText(/Existing sessions and their forks keep their version/),
    ).toBeInTheDocument();
    expect(screen.queryByText(/EXTRA_VERSIONS/)).not.toBeInTheDocument();
  });

  it('recovers from an initial read failure without claiming the settings are empty', async () => {
    list.mockRejectedValueOnce(new Error('Service unavailable'));
    render(<AdminSettingsPage />);
    expect(await screen.findByRole('alert')).toHaveTextContent('Service unavailable');
    expect(
      screen.queryByText('No server has reported available versions yet.'),
    ).not.toBeInTheDocument();
    fireEvent.click(screen.getByRole('button', { name: 'Retry' }));
    await screen.findByLabelText('Claude Code');
    expect(screen.queryByRole('alert')).not.toBeInTheDocument();
  });

  it('lists a setting this frontend has never heard of with the others', async () => {
    list.mockResolvedValue(
      pool({
        host_setting_definitions: [
          ...definitions,
          { ...definitions[0], key: 'future_limit', label: 'Future limit' },
        ],
      }),
    );
    render(<AdminSettingsPage />);
    await screen.findByLabelText('Future limit for all servers');
    const card = screen.getByRole('heading', { name: 'Sandbox' }).parentElement!;
    expect(within(card).getByLabelText('Future limit for all servers')).toBeInTheDocument();
  });
});

describe('agent versions', () => {
  it('offers the server default before every version the pool holds', async () => {
    render(<AdminSettingsPage />);
    const select = await screen.findByLabelText<HTMLSelectElement>('Claude Code');
    expect([...select.options].map((option) => option.textContent)).toEqual([
      'Server default (2.1.220)',
      '2.1.220',
      '2.1.235',
    ]);
    expect(select).toHaveValue('');
    expect(within(select.closest('form')!).getByRole('button', { name: 'Save' })).toBeDisabled();
  });

  it('shows the global selection rather than the installed server default', async () => {
    list.mockResolvedValue(pool({ runtime_versions: { 'claude-code': '2.1.235' } }));
    render(<AdminSettingsPage />);
    expect(await screen.findByLabelText('Claude Code')).toHaveValue('2.1.235');
  });

  it('waits for Save, sends the choice, and takes the returned snapshot', async () => {
    setVersion.mockResolvedValue(pool({ runtime_versions: { 'claude-code': '2.1.235' } }));
    render(<AdminSettingsPage />);
    const select = await screen.findByLabelText('Claude Code');
    fireEvent.change(select, { target: { value: '2.1.235' } });
    expect(setVersion).not.toHaveBeenCalled();
    submit(select);
    await waitFor(() => expect(setVersion).toHaveBeenCalledWith('claude-code', '2.1.235'));
    expect(await screen.findByRole('status')).toHaveTextContent(
      'New Claude Code sandboxes use 2.1.235. Existing sessions keep their version.',
    );
    expect(screen.getByLabelText('Claude Code')).toHaveValue('2.1.235');
    expect(
      within(screen.getByLabelText('Claude Code').closest('form')!).getByRole('button', {
        name: 'Save',
      }),
    ).toBeDisabled();
  });

  it('clears to the server default with null, not an empty version string', async () => {
    list.mockResolvedValue(pool({ runtime_versions: { 'claude-code': '2.1.235' } }));
    setVersion.mockResolvedValue(pool());
    render(<AdminSettingsPage />);
    const select = await screen.findByLabelText('Claude Code');
    fireEvent.change(select, { target: { value: '' } });
    submit(select);
    await waitFor(() => expect(setVersion).toHaveBeenCalledWith('claude-code', null));
  });

  it('explains an unavailable menu without hiding the section', async () => {
    list.mockResolvedValue(pool({ hosts: [host({ runtime_versions_available: null })] }));
    render(<AdminSettingsPage />);
    await screen.findByText('No server has reported available versions yet.');
    expect(screen.getByRole('heading', { name: 'Agent versions' })).toBeInTheDocument();
    expect(screen.queryByLabelText('Claude Code')).not.toBeInTheDocument();
  });

  it('keeps a single-version agent selectable', async () => {
    list.mockResolvedValue(
      pool({ hosts: [host({ runtime_versions_available: { codex: ['0.145.0'] } })] }),
    );
    render(<AdminSettingsPage />);
    const select = await screen.findByLabelText<HTMLSelectElement>('Codex');
    expect([...select.options].map((option) => option.textContent)).toEqual([
      'Server default',
      '0.145.0',
    ]);
  });

  it('keeps an unavailable saved version visible and lets the admin clear it', async () => {
    list.mockResolvedValue(pool({ hosts: [], runtime_versions: { 'claude-code': '2.1.235' } }));
    setVersion.mockResolvedValue(pool({ hosts: [] }));
    render(<AdminSettingsPage />);
    const select = await screen.findByLabelText('Claude Code');
    expect(select).toHaveValue('2.1.235');
    expect(screen.getByText('2.1.235 (unavailable)')).toBeInTheDocument();
    fireEvent.change(select, { target: { value: '' } });
    submit(select);
    await waitFor(() => expect(setVersion).toHaveBeenCalledWith('claude-code', null));
  });

  it('names a server that lacks the chosen version, before and after Save', async () => {
    const lacking = host({
      host: 'runner-b',
      runtime_versions_available: { 'claude-code': ['2.1.220'] },
    });
    list.mockResolvedValue(pool({ hosts: [host(), lacking] }));
    setVersion.mockResolvedValue(
      pool({ hosts: [host(), lacking], runtime_versions: { 'claude-code': '2.1.235' } }),
    );
    render(<AdminSettingsPage />);
    const select = await screen.findByLabelText('Claude Code');
    expect(screen.queryByText(/Not on runner-b/)).not.toBeInTheDocument();
    fireEvent.change(select, { target: { value: '2.1.235' } });
    expect(
      screen.getByText('Not on runner-b: new sandboxes there would use 2.1.220.'),
    ).toBeInTheDocument();
    submit(select);
    expect(
      await screen.findByText('Not on runner-b: new sandboxes there use 2.1.220.'),
    ).toBeInTheDocument();
  });

  it('says when the servers do not agree on a default', async () => {
    list.mockResolvedValue(
      pool({
        hosts: [host(), host({ host: 'runner-b', runtime_versions: { 'claude-code': '2.1.235' } })],
      }),
    );
    render(<AdminSettingsPage />);
    const select = await screen.findByLabelText<HTMLSelectElement>('Claude Code');
    expect(select.options[0]).toHaveTextContent('Server default (varies by server)');
  });
});

describe('global limits', () => {
  it('does not offer controls when no limits were reported', async () => {
    list.mockResolvedValue(pool({ host_setting_definitions: undefined }));
    render(<AdminSettingsPage />);
    await screen.findByLabelText('Claude Code');
    expect(screen.queryByLabelText('Turn time limit for all servers')).not.toBeInTheDocument();
  });

  it('saves one value and reports actual application separately', async () => {
    setLimit.mockResolvedValue(pool({ host_settings: { agent_timeout_s: 7200 } }));
    render(<AdminSettingsPage />);
    const input = await screen.findByLabelText('Turn time limit for all servers');
    expect(screen.getAllByText('Each server uses its own .env value, or 1 h')).toHaveLength(1);
    expect(screen.getByLabelText('Turn time limit for all servers unit')).toHaveValue('h');
    fireEvent.change(input, { target: { value: '2' } });
    submit(input);
    await waitFor(() => expect(setLimit).toHaveBeenCalledWith({ agent_timeout_s: 7200 }));
    expect(await screen.findByText('Applied on 0 of 1 server')).toBeInTheDocument();
    expect(screen.getByText('2 h')).toBeInTheDocument();
  });

  it('rejects an out-of-range value without sending it', async () => {
    render(<AdminSettingsPage />);
    const input = await screen.findByLabelText('Turn time limit for all servers');
    fireEvent.change(input, { target: { value: '30' } });
    submit(input);
    expect(await screen.findByRole('alert')).toHaveTextContent('Enter a value from 1 min to 1 d.');
    expect(setLimit).not.toHaveBeenCalled();
  });

  it('clears a global value so servers fall back to their own defaults', async () => {
    list.mockResolvedValue(pool({ host_settings: { terminal_max_sessions: 16 } }));
    setLimit.mockResolvedValue(pool());
    render(<AdminSettingsPage />);
    fireEvent.click(await screen.findByRole('button', { name: 'Use server defaults' }));
    await waitFor(() => expect(setLimit).toHaveBeenCalledWith({ terminal_max_sessions: null }));
  });

  it('reports unsupported servers for each setting', async () => {
    list.mockResolvedValue(
      pool({
        hosts: [host({ capabilities: [], settings_applied: null })],
        host_setting_definitions: [definitions[0], definitions[6]],
      }),
    );
    render(<AdminSettingsPage />);
    await screen.findByLabelText('Turn time limit for all servers');
    expect(screen.getAllByText(/runner-a needs a redeploy/)).toHaveLength(2);
  });

  it('retains a draft after a rejected save so it can be retried', async () => {
    setLimit.mockRejectedValueOnce(new Error('Could not save'));
    setLimit.mockResolvedValueOnce(pool({ host_settings: { agent_timeout_s: 7200 } }));
    render(<AdminSettingsPage />);
    const input = await screen.findByLabelText('Turn time limit for all servers');
    fireEvent.change(input, { target: { value: '2' } });
    submit(input);
    expect(await screen.findByRole('alert')).toHaveTextContent('Could not save');
    expect(input).toHaveValue(2);
    submit(input);
    expect(await screen.findByText('Applied on 0 of 1 server')).toBeInTheDocument();
    expect(setLimit).toHaveBeenCalledTimes(2);
  });

  it('takes an amount in the unit the admin picks', async () => {
    setLimit.mockResolvedValue(pool({ host_settings: { agent_timeout_s: 5400 } }));
    render(<AdminSettingsPage />);
    const input = await screen.findByLabelText('Turn time limit for all servers');
    fireEvent.change(screen.getByLabelText('Turn time limit for all servers unit'), {
      target: { value: 'min' },
    });
    expect(input).toHaveAttribute('placeholder', '60');
    fireEvent.change(input, { target: { value: '90' } });
    submit(input);
    await waitFor(() => expect(setLimit).toHaveBeenCalledWith({ agent_timeout_s: 5400 }));
  });

  it('warns before a shorter terminal lifetime closes open terminals', async () => {
    render(<AdminSettingsPage />);
    const input = await screen.findByLabelText('Terminal max lifetime for all servers');
    fireEvent.change(input, { target: { value: '4' } });
    expect(screen.getByText('Open terminals older than 4 h will close.')).toBeInTheDocument();
    fireEvent.change(input, { target: { value: '12' } });
    expect(screen.queryByText(/Open terminals older than/)).not.toBeInTheDocument();
  });

  it('names the server that sets its own value', async () => {
    list.mockResolvedValue(
      pool({ hosts: [host({ settings_desired: { terminal_max_sessions: 128 } })] }),
    );
    render(<AdminSettingsPage />);
    expect(await screen.findByText(/runner-a sets its own \(128\)/)).toBeInTheDocument();
  });
});

describe('polling and account boundaries', () => {
  it('preserves drafts while polling, then updates application status', async () => {
    vi.useFakeTimers();
    render(<AdminSettingsPage />);
    await vi.waitFor(() => expect(screen.getByLabelText('Claude Code')).toBeInTheDocument());
    fireEvent.change(screen.getByLabelText('Claude Code'), { target: { value: '2.1.235' } });
    fireEvent.change(screen.getByLabelText('Turn time limit for all servers'), {
      target: { value: '2' },
    });
    await vi.advanceTimersByTimeAsync(30_000);
    expect(screen.getByLabelText('Claude Code')).toHaveValue('2.1.235');
    expect(screen.getByLabelText('Turn time limit for all servers')).toHaveValue(2);

    setLimit.mockResolvedValue(pool({ host_settings: { agent_timeout_s: 7200 } }));
    submit(screen.getByLabelText('Turn time limit for all servers'));
    await vi.waitFor(() =>
      expect(screen.getByText('Applied on 0 of 1 server')).toBeInTheDocument(),
    );
    list.mockResolvedValue(
      pool({
        host_settings: { agent_timeout_s: 7200 },
        hosts: [host({ settings_applied: { agent_timeout_s: 7200 } })],
      }),
    );
    await vi.advanceTimersByTimeAsync(30_000);
    expect(screen.getByText('In use on 1 server')).toBeInTheDocument();
  });

  it('discards a read that started before a successful save', async () => {
    vi.useFakeTimers();
    render(<AdminSettingsPage />);
    await vi.waitFor(() => expect(screen.getByLabelText('Claude Code')).toBeInTheDocument());
    let finishPoll!: (value: AgentRunnerHostPool) => void;
    list.mockImplementationOnce(
      () =>
        new Promise((resolve) => {
          finishPoll = resolve;
        }),
    );
    await vi.advanceTimersByTimeAsync(30_000);
    setVersion.mockResolvedValue(pool({ runtime_versions: { 'claude-code': '2.1.235' } }));
    const select = screen.getByLabelText('Claude Code');
    fireEvent.change(select, { target: { value: '2.1.235' } });
    submit(select);
    await vi.waitFor(() =>
      expect(screen.getByRole('status')).toHaveTextContent('sandboxes use 2.1.235'),
    );
    finishPoll(pool());
    await vi.advanceTimersByTimeAsync(0);
    expect(screen.getByLabelText('Claude Code')).toHaveValue('2.1.235');
  });

  it('keeps the last snapshot on a failed poll and labels the failure', async () => {
    vi.useFakeTimers();
    render(<AdminSettingsPage />);
    await vi.waitFor(() => expect(screen.getByLabelText('Claude Code')).toBeInTheDocument());
    list.mockRejectedValue(new Error('Connection lost'));
    await vi.advanceTimersByTimeAsync(30_000);
    expect(screen.getByRole('alert')).toHaveTextContent('Connection lost');
    expect(screen.getByLabelText('Claude Code')).toBeInTheDocument();
  });

  it('clears protected data and stops polling when admin access is revoked', async () => {
    vi.useFakeTimers();
    render(<AdminSettingsPage />);
    await vi.waitFor(() => expect(screen.getByLabelText('Claude Code')).toBeInTheDocument());
    list.mockRejectedValue(new APIError('FORBIDDEN', 'Forbidden', 403));
    await vi.advanceTimersByTimeAsync(30_000);
    expect(screen.getByText('Admin access required')).toBeInTheDocument();
    expect(screen.queryByLabelText('Claude Code')).not.toBeInTheDocument();
    await vi.advanceTimersByTimeAsync(60_000);
    expect(list).toHaveBeenCalledTimes(2);
  });

  it("does not apply the old account's pending save after switching accounts", async () => {
    let finishSave!: (value: AgentRunnerHostPool) => void;
    setVersion.mockImplementationOnce(
      () =>
        new Promise((resolve) => {
          finishSave = resolve;
        }),
    );
    const view = render(<AdminSettingsPage />);
    const select = await screen.findByLabelText('Claude Code');
    fireEvent.change(select, { target: { value: '2.1.235' } });
    submit(select);
    auth.id = 'admin-b';
    view.rerender(<AdminSettingsPage />);
    expect(await screen.findByLabelText('Claude Code')).toHaveValue('');
    finishSave(pool({ runtime_versions: { 'claude-code': '2.1.235' } }));
    await waitFor(() => expect(screen.getByLabelText('Claude Code')).toHaveValue(''));
    expect(screen.queryByText(/sandboxes use/)).not.toBeInTheDocument();
  });
});
