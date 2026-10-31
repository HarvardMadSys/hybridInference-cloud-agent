// @vitest-environment jsdom
import '@testing-library/jest-dom/vitest';

import { act, cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { createAgentJob, getAgentConfig, listRepoBranches } from '@/lib/api/agents';
import type { AgentConfigApi } from '@/lib/api/agents';
import { listEnvironments } from '@/lib/api/environments';
import { getInsights } from '@/lib/api/insights';
import { listMcpConnections } from '@/lib/api/mcp';
import { listSkills } from '@/lib/api/skills';
import { listSshServers } from '@/lib/api/sshServers';
import { getUserProfile } from '@/lib/api/userProfile';

import { TaskComposer } from './TaskComposer';
import { announceModelConnectionsChanged } from './modelConnectionEvents';
import { composerDraftStorageKey } from './useComposerDraft';

// A model is chosen together with the source it runs on. These cover the new
// task composer's half of that: what it offers, what it sends, what it keeps
// in a draft, and that a personal choice never quietly becomes the Gateway's.

const navigation = vi.hoisted(() => ({ push: vi.fn() }));
vi.mock('next/navigation', () => ({ useRouter: () => navigation, usePathname: () => '/' }));
vi.mock('@/components/providers/AuthProvider', () => ({
  useAuth: () => ({ state: { user: { id: 'alice' } } }),
}));
vi.mock('@/lib/api/agents', () => ({
  createAgentJob: vi.fn(),
  createGitHubRepository: vi.fn(),
  getAgentConfig: vi.fn(),
  listAgentConversations: vi.fn(),
  listRepoBranches: vi.fn(),
}));
vi.mock('@/lib/api/environments', () => ({
  listEnvironments: vi.fn(),
  getGlobalEnvironment: vi.fn(),
}));
vi.mock('@/lib/api/insights', () => ({ getInsights: vi.fn() }));
vi.mock('@/lib/api/userProfile', () => ({ getUserProfile: vi.fn() }));
vi.mock('@/lib/api/skills', () => ({ listSkills: vi.fn() }));
vi.mock('@/lib/api/mcp', () => ({
  listMcpConnections: vi.fn(),
  getMcpConnectionCapabilities: vi.fn(),
}));
vi.mock('@/lib/api/sshServers', () => ({ listSshServers: vi.fn() }));
vi.mock('./githubAccess', () => ({ githubAccessUrl: vi.fn(), leaveFor: vi.fn() }));

const CONNECTION = 'mconn_0123456789abcdef0123456789abcdef';
const OTHER = 'mconn_fedcba9876543210fedcba9876543210';

/** Two connections both list `shared`; only the first speaks to Codex. */
function config(overrides: Partial<AgentConfigApi> = {}): AgentConfigApi {
  return {
    repos: ['owner/repository'],
    runtimes: ['claude-code', 'codex'],
    runtime_reasoning_efforts: { 'claude-code': ['high'], codex: ['high'] },
    default_model: 'shared',
    default_model_connection_id: OTHER,
    model_connections_enabled: true,
    model_connections: [
      {
        id: CONNECTION,
        name: 'My provider',
        models: ['shared', 'mine'],
        model_runtimes: { shared: ['codex'], mine: ['codex'] },
      },
      {
        id: OTHER,
        name: 'Other provider',
        models: ['shared', 'other-only'],
        model_runtimes: { shared: ['claude-code'], 'other-only': ['claude-code'] },
      },
    ],
    github_connected: true,
    github_install_url: null,
    ...overrides,
  };
}

function choose(label: string, option: string) {
  fireEvent.click(screen.getByLabelText(label));
  fireEvent.click(screen.getByRole('option', { name: option }));
}

async function chooseRepository() {
  fireEvent.click(await screen.findByRole('button', { name: /^Repository:/ }));
  const dialog = await screen.findByRole('dialog', { name: 'Repository' });
  fireEvent.click(within(dialog).getByRole('button', { name: 'owner/repository' }));
  await screen.findByLabelText('Branch');
}

async function runWhenReady() {
  await waitFor(() => expect(screen.getByRole('button', { name: /^Run/ })).toBeEnabled());
  fireEvent.click(screen.getByRole('button', { name: /^Run/ }));
}

function savedDraft(): Record<string, unknown> {
  const stored = JSON.parse(localStorage.getItem(composerDraftStorageKey('alice', 'new-task'))!);
  return JSON.parse(stored.value);
}

function storeDraft(fields: Record<string, unknown>) {
  localStorage.setItem(
    composerDraftStorageKey('alice', 'new-task'),
    JSON.stringify({
      version: 1,
      revision: 1,
      value: JSON.stringify({ task: 'Kept', repo: '', effort: '', branch: '', ...fields }),
    }),
  );
}

beforeEach(() => {
  localStorage.clear();
  sessionStorage.clear();
  navigation.push.mockReset();
  vi.mocked(getInsights).mockRejectedValue(new Error('unavailable'));
  vi.mocked(getUserProfile).mockRejectedValue(new Error('unavailable'));
  vi.mocked(listSkills).mockResolvedValue([]);
  vi.mocked(listMcpConnections).mockResolvedValue({ connections: [], templates: [] });
  vi.mocked(listSshServers).mockResolvedValue({ servers: [] } as never);
  vi.mocked(listEnvironments).mockResolvedValue({
    environments: [],
    globals: {
      id: null,
      name: 'Global',
      repo: null,
      revision: 0,
      variables: [],
      setup_script: '',
      start_script: '',
      verify_script: '',
      actions: [],
    },
  });
  vi.mocked(listRepoBranches)
    .mockReset()
    .mockResolvedValue({ default: 'dev', branches: ['dev'] });
  vi.mocked(createAgentJob)
    .mockReset()
    .mockResolvedValue({ id: 'ajob_1' } as never);
  vi.mocked(getAgentConfig).mockReset().mockResolvedValue(config());
});
afterEach(cleanup);

describe('TaskComposer model sources', () => {
  it('offers a connection only to the agents that speak its protocol', async () => {
    render(<TaskComposer />);
    await chooseRepository();
    fireEvent.click(screen.getByLabelText('Model'));
    // `shared` is listed by both connections, so it names the one it is on.
    expect(screen.getAllByRole('option').map((option) => option.textContent)).toEqual([
      'shared · Other provider',
      'other-only',
    ]);
    fireEvent.keyDown(document.activeElement!, { key: 'Escape' });
    choose('Agent', 'codex');
    fireEvent.click(screen.getByLabelText('Model'));
    // The remembered pick stays in view, marked, rather than becoming its namesake.
    expect(screen.getAllByRole('option').map((option) => option.textContent)).toEqual([
      'shared · Other provider (unavailable)',
      'shared · My provider',
      'mine',
    ]);
  });

  it('sends a personal model with its connection and no effort', async () => {
    render(<TaskComposer />);
    await chooseRepository();
    choose('Agent', 'codex');
    choose('Model', 'shared · My provider');
    expect(screen.getByLabelText('Model')).toHaveAccessibleDescription('shared · My provider');
    fireEvent.change(screen.getByRole('textbox'), { target: { value: 'Run on my key' } });
    await runWhenReady();
    await waitFor(() => expect(createAgentJob).toHaveBeenCalledTimes(1));
    const body = vi.mocked(createAgentJob).mock.calls[0][0];
    expect(body).toMatchObject({
      runtime: 'codex',
      model: 'shared',
      model_connection_id: CONNECTION,
    });
    // Effort stays Auto on a personal connection, whatever the Gateway model offers.
    expect(body).not.toHaveProperty('reasoning_effort');
  });

  it('keeps a personal pick and its source in the draft across a visit', async () => {
    const first = render(<TaskComposer />);
    await chooseRepository();
    choose('Agent', 'codex');
    choose('Model', 'shared · My provider');
    await waitFor(() =>
      expect(savedDraft()).toMatchObject({
        chosenModel: 'shared',
        chosenModelConnectionId: CONNECTION,
      }),
    );
    first.unmount();
    render(<TaskComposer />);
    await waitFor(() =>
      expect(screen.getByLabelText('Model')).toHaveAccessibleDescription('shared · My provider'),
    );
  });

  it('holds Run on a pick whose connection is gone, instead of a namesake', async () => {
    // The draft's connection has since been deleted.
    storeDraft({
      repo: 'owner/repository',
      runtime: 'codex',
      chosenModel: 'shared',
      chosenModelConnectionId: 'mconn_ffffffffffffffffffffffffffffffff',
    });
    render(<TaskComposer />);
    expect(
      await screen.findByText(/shared · personal connection \(unavailable\) cannot run with codex/),
    ).toBeVisible();
    expect(screen.getByLabelText('Model')).toHaveAccessibleDescription(
      'shared · personal connection (unavailable)',
    );
    expect(screen.getByRole('button', { name: /^Run/ })).toBeDisabled();
    choose('Model', 'shared · My provider');
    expect(screen.queryByText(/cannot run with/)).toBeNull();
    expect(screen.getByLabelText('Model')).toHaveValue('shared');
  });

  it('starts on a remembered connection, and asks again for an agent it cannot serve', async () => {
    vi.mocked(getAgentConfig).mockResolvedValue(
      config({ default_model: 'mine', default_model_connection_id: CONNECTION }),
    );
    render(<TaskComposer />);
    await chooseRepository();
    // Claude Code is the first agent, and no connection serves it.
    expect(screen.getByLabelText('Agent')).toHaveValue('claude-code');
    expect(screen.getByLabelText('Model')).toHaveAccessibleDescription('mine (unavailable)');
    expect(screen.getByRole('button', { name: /^Run/ })).toBeDisabled();
    choose('Agent', 'codex');
    expect(screen.getByLabelText('Model')).toHaveAccessibleDescription('mine');
  });

  it('restores a personal pick from the GitHub return receipt', async () => {
    sessionStorage.setItem(
      'agents.composer.draft:alice',
      JSON.stringify({
        value: {
          task: 'Back from GitHub',
          repo: 'owner/repository',
          runtime: 'codex',
          chosenModel: 'mine',
          chosenModelConnectionId: CONNECTION,
        },
        savedAt: Date.now(),
      }),
    );
    render(<TaskComposer />);
    await waitFor(() => expect(screen.getByLabelText('Model')).toHaveAccessibleDescription('mine'));
  });

  it('offers a Messages connection to Claude Code only, and runs Claude Code on it', async () => {
    const messages = 'mconn_fedcba9876543210fedcba9876543210';
    vi.mocked(getAgentConfig).mockResolvedValue(
      config({
        model_connections: [
          {
            id: CONNECTION,
            name: 'My provider',
            models: ['shared', 'mine'],
            model_runtimes: { shared: ['codex'], mine: ['codex'] },
          },
          {
            id: messages,
            name: 'Claude gateway',
            models: ['shared'],
            model_runtimes: { shared: ['claude-code'] },
          },
        ],
      }),
    );
    render(<TaskComposer />);
    await chooseRepository();
    expect(screen.getByLabelText('Agent')).toHaveValue('claude-code');
    fireEvent.click(screen.getByLabelText('Model'));
    expect(screen.getAllByRole('option').map((option) => option.textContent)).toEqual([
      'shared · Claude gateway',
    ]);
    fireEvent.click(screen.getByRole('option', { name: 'shared · Claude gateway' }));
    fireEvent.change(screen.getByRole('textbox'), { target: { value: 'Run Claude on my key' } });
    await runWhenReady();
    await waitFor(() => expect(createAgentJob).toHaveBeenCalledTimes(1));
    const body = vi.mocked(createAgentJob).mock.calls[0][0];
    expect(body).toMatchObject({
      runtime: 'claude-code',
      model: 'shared',
      model_connection_id: messages,
    });
    expect(body).not.toHaveProperty('reasoning_effort');
  });

  it('marks a Messages pick unavailable once the agent is switched to one it cannot serve', async () => {
    const messages = 'mconn_fedcba9876543210fedcba9876543210';
    vi.mocked(getAgentConfig).mockResolvedValue(
      config({
        model_connections: [
          {
            id: messages,
            name: 'Claude gateway',
            models: ['shared'],
            model_runtimes: { shared: ['claude-code'] },
          },
        ],
      }),
    );
    render(<TaskComposer />);
    await chooseRepository();
    choose('Model', 'shared');
    choose('Agent', 'codex');
    expect(screen.getByLabelText('Model')).toHaveAccessibleDescription('shared (unavailable)');
    expect(screen.getByRole('button', { name: /^Run/ })).toBeDisabled();
  });

  it('runs Codex on a model verified only for Chat, without saying how it is reached', async () => {
    const chat = 'mconn_0f0f0f0f0f0f0f0f0f0f0f0f0f0f0f0f';
    vi.mocked(getAgentConfig).mockResolvedValue(
      config({
        model_connections: [
          {
            id: chat,
            name: 'Chat provider',
            models: ['chat-model', 'new-model'],
            model_runtimes: { 'chat-model': ['claude-code', 'codex', 'kilo', 'opencode', 'pi'] },
          },
        ],
      }),
    );
    render(<TaskComposer />);
    await chooseRepository();
    choose('Agent', 'codex');
    fireEvent.click(screen.getByLabelText('Model'));
    const options = screen.getAllByRole('option').map((option) => option.textContent);
    // The unverified model is not offered; no label names an API.
    expect(options).toContain('chat-model');
    expect(options.some((option) => option?.includes('new-model'))).toBe(false);
    expect(options.some((option) => option?.includes('Chat Completions'))).toBe(false);
    fireEvent.click(screen.getByRole('option', { name: 'chat-model' }));
    fireEvent.change(screen.getByRole('textbox'), { target: { value: 'Run Codex on Chat' } });
    await runWhenReady();
    await waitFor(() => expect(createAgentJob).toHaveBeenCalledTimes(1));
    expect(vi.mocked(createAgentJob).mock.calls[0][0]).toMatchObject({
      runtime: 'codex',
      model: 'chat-model',
      model_connection_id: chat,
    });
    // The stored source is the same pair; the plan is the server's to resolve.
    expect(vi.mocked(createAgentJob).mock.calls[0][0]).not.toHaveProperty('reasoning_effort');
  });

  it('offers each model only to the agents its own verification allows', async () => {
    const both = 'mconn_1e1e1e1e1e1e1e1e1e1e1e1e1e1e1e1e';
    vi.mocked(getAgentConfig).mockResolvedValue(
      config({
        model_connections: [
          {
            id: both,
            name: 'Mixed',
            models: ['messages-only', 'chat-model'],
            model_runtimes: {
              'messages-only': ['claude-code'],
              'chat-model': ['claude-code', 'codex', 'kilo', 'opencode', 'pi'],
            },
          },
        ],
      }),
    );
    render(<TaskComposer />);
    await chooseRepository();
    choose('Agent', 'codex');
    fireEvent.click(screen.getByLabelText('Model'));
    expect(screen.getByRole('option', { name: 'chat-model' })).toBeInTheDocument();
    expect(screen.queryByRole('option', { name: 'messages-only' })).not.toBeInTheDocument();
    fireEvent.keyDown(document.activeElement!, { key: 'Escape' });
    choose('Agent', 'claude-code');
    fireEvent.click(screen.getByLabelText('Model'));
    expect(screen.getByRole('option', { name: 'messages-only' })).toBeInTheDocument();
    expect(screen.getByRole('option', { name: 'chat-model' })).toBeInTheDocument();
  });
});

describe('local account model onboarding', () => {
  it('offers an actionable setup link and nothing to run', async () => {
    vi.mocked(getAgentConfig).mockResolvedValue(
      config({
        model_connection_required: true,
        default_model: null,
        model_connections: [],
      }),
    );
    render(<TaskComposer />);
    expect(await screen.findByRole('link', { name: 'Set up a model connection' })).toHaveAttribute(
      'href',
      '/settings/my-settings#settings-connections',
    );
    expect(screen.getByText(/Your provider manages billing and usage limits/)).toBeInTheDocument();
    expect(screen.getByRole('button', { name: /^Run/ })).toBeDisabled();
  });

  it('honors an explicit disabled gate even if a stale response still contains personal models', async () => {
    vi.mocked(getAgentConfig).mockResolvedValue(
      config({
        model_connection_required: true,
        model_connections_enabled: false,
        default_model: 'shared',
        default_model_connection_id: CONNECTION,
      }),
    );
    render(<TaskComposer />);
    await screen.findByText('Model connections are unavailable');
    expect(
      screen.queryByRole('link', { name: 'Set up a model connection' }),
    ).not.toBeInTheDocument();
    expect(screen.getByRole('button', { name: /^Run/ })).toBeDisabled();
  });

  it('refreshes setup after a verified save, then holds a disabled same-name source without fallback', async () => {
    vi.mocked(getAgentConfig).mockResolvedValue(
      config({
        model_connection_required: true,
        default_model: null,
        model_connections: [],
      }),
    );
    render(<TaskComposer />);
    await chooseRepository();
    fireEvent.change(screen.getByRole('textbox'), { target: { value: 'Keep my unsent task' } });
    choose('Agent', 'codex');
    vi.mocked(getAgentConfig).mockResolvedValue(config({ default_model: null }));
    act(() => announceModelConnectionsChanged());
    await waitFor(() =>
      expect(
        screen.queryByRole('link', { name: 'Set up a model connection' }),
      ).not.toBeInTheDocument(),
    );
    choose('Model', 'shared · My provider');
    await waitFor(() => expect(screen.getByRole('button', { name: /^Run/ })).toBeEnabled());
    vi.mocked(getAgentConfig).mockResolvedValue(config({ model_connections: [] }));
    act(() => announceModelConnectionsChanged());
    await waitFor(() =>
      expect(screen.getByLabelText('Model')).toHaveAccessibleDescription(
        'shared · personal connection (unavailable)',
      ),
    );
    expect(screen.getByRole('textbox')).toHaveValue('Keep my unsent task');
    expect(screen.getByRole('button', { name: /^Run/ })).toBeDisabled();
    fireEvent.keyDown(screen.getByRole('textbox'), { key: 'Enter' });
    expect(createAgentJob).not.toHaveBeenCalled();
  });
});
