// @vitest-environment jsdom
import '@testing-library/jest-dom/vitest';

import { cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
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
import { composerDraftStorageKey } from './useComposerDraft';

// The reasoning control offers Auto and the levels the model's provider
// declares for the chosen agent (`/config` `model_efforts`), sends only what was
// chosen, keeps it in a draft, and never swaps an unavailable level for another.
// The regression it replaces: `offeredEfforts` answered `[]` for every model.

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
vi.mock('../githubAccess', () => ({ githubAccessUrl: vi.fn(), leaveFor: vi.fn() }));

const CONNECTION = 'mconn_0123456789abcdef0123456789abcdef';
const OTHER = 'mconn_fedcba9876543210fedcba9876543210';

/** Two connections both list `shared`; only the first speaks to Codex. */
function baseConfig(overrides: Partial<AgentConfigApi> = {}): AgentConfigApi {
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

/** `shared` on My provider declares high and max for Codex; `mine` declares nothing. */
function config(): AgentConfigApi {
  const base = baseConfig();
  const [mine, other] = base.model_connections!;
  return {
    ...base,
    model_connections: [
      {
        ...mine,
        model_efforts: {
          shared: { codex: { status: 'supported', levels: ['high', 'max'], reason: '' } },
          mine: { codex: { status: 'unknown', levels: [], reason: 'not_declared' } },
        },
      },
      other,
    ],
  };
}

async function onSharedWithCodex() {
  render(<TaskComposer />);
  await chooseRepository();
  choose('Agent', 'Codex');
  choose('Model', 'shared · My provider');
}

function reasoningOptions(): string[] {
  fireEvent.click(screen.getByLabelText('Reasoning'));
  const options = screen.getAllByRole('option').map((option) => option.textContent ?? '');
  fireEvent.keyDown(document.activeElement!, { key: 'Escape' });
  return options;
}

async function submittedBody() {
  fireEvent.change(screen.getByRole('textbox'), { target: { value: 'Think about it' } });
  await runWhenReady();
  await waitFor(() => expect(createAgentJob).toHaveBeenCalledTimes(1));
  return vi.mocked(createAgentJob).mock.calls[0][0];
}

describe('TaskComposer reasoning levels', () => {
  it('offers Auto and the declared levels, and starts on Auto', async () => {
    await onSharedWithCodex();
    expect(screen.getByLabelText('Reasoning')).toHaveAccessibleDescription('Auto');
    expect(reasoningOptions()).toEqual(['Auto', 'high', 'max']);
    // Auto sends nothing: the provider's default, as before.
    expect(await submittedBody()).not.toHaveProperty('reasoning_effort');
  });

  it('sends the chosen level exactly', async () => {
    await onSharedWithCodex();
    choose('Reasoning', 'max');
    expect(await submittedBody()).toMatchObject({
      model: 'shared',
      model_connection_id: CONNECTION,
      reasoning_effort: 'max',
    });
  });

  it('says when the provider declares nothing and offers Auto alone', async () => {
    render(<TaskComposer />);
    await chooseRepository();
    choose('Agent', 'Codex');
    choose('Model', 'mine');
    expect(reasoningOptions()).toEqual(['Auto']);
    expect(screen.getByText('Auto only')).toBeVisible();
    expect(screen.getByText(/does not declare which reasoning levels/)).toBeInTheDocument();
  });

  it('puts a level the new model does not offer back on Auto, visibly', async () => {
    await onSharedWithCodex();
    choose('Reasoning', 'high');
    choose('Model', 'mine');
    expect(
      await screen.findByText('high is not offered for this model; reasoning is back on Auto.'),
    ).toBeVisible();
    expect(screen.getByLabelText('Reasoning')).toHaveAccessibleDescription('Auto');
    expect(await submittedBody()).not.toHaveProperty('reasoning_effort');
  });

  it('keeps a chosen level in the draft across a visit', async () => {
    storeDraft({
      effort: 'max',
      runtime: 'codex',
      chosenModel: 'shared',
      chosenModelConnectionId: CONNECTION,
    });
    render(<TaskComposer />);
    await waitFor(() =>
      expect(screen.getByLabelText('Reasoning')).toHaveAccessibleDescription('max'),
    );
    expect(savedDraft()).toMatchObject({ effort: 'max' });
  });
});
