// @vitest-environment jsdom
import '@testing-library/jest-dom/vitest';

import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { getAgentConfig, type AgentConfigApi, type AgentJobApi } from '@/lib/api/agents';
import { submitThreadMessage } from '@/lib/api/pendingMessages';
import { rewritePrompt } from '@/lib/api/promptRewrite';
import { JobDetail } from './JobDetail';
import { announceModelConnectionsChanged } from './modelConnectionEvents';
import type { AgentJob } from './types';

// A follow-up inherits its model *and* that model's source, and a switch
// between two sources of the same model name is a switch. Gateway-only
// helpers stay off a personal source.

vi.mock('next/navigation', () => ({
  useRouter: () => ({ push: vi.fn() }),
  usePathname: () => '/ajob_1',
}));
vi.mock('@/lib/api/agents', async (importOriginal) => ({
  ...(await importOriginal<typeof import('@/lib/api/agents')>()),
  getAgentConfig: vi.fn(),
  getAgentJobUsage: vi.fn().mockResolvedValue(null),
  refreshAgentJobUsage: vi.fn().mockResolvedValue(null),
  getAgentThreadStats: vi.fn().mockResolvedValue(null),
  getAgentThreadModelTtft: vi.fn().mockResolvedValue(null),
}));
vi.mock('@/lib/api/mcp', async (importOriginal) => ({
  ...(await importOriginal<typeof import('@/lib/api/mcp')>()),
  listMcpInvocations: vi.fn().mockResolvedValue({ invocations: [] }),
  getMcpThreadBindings: vi.fn().mockResolvedValue({ bindings: [] }),
  listMcpConnections: vi.fn().mockResolvedValue({ connections: [] }),
}));
vi.mock('@/lib/api/questions', () => ({
  listThreadQuestions: vi.fn().mockResolvedValue({ questions: [], attempt_id: 1 }),
  answerThreadQuestion: vi.fn(),
}));
vi.mock('@/lib/api/skills', () => ({ listSkills: vi.fn().mockResolvedValue([]) }));
vi.mock('@/lib/api/promptRewrite', () => ({ rewritePrompt: vi.fn() }));
vi.mock('@/lib/api/pendingMessages', async (importOriginal) => ({
  ...(await importOriginal<typeof import('@/lib/api/pendingMessages')>()),
  submitThreadMessage: vi.fn(),
  getThreadQueue: vi.fn().mockResolvedValue({
    thread_id: 'ajob_1',
    pending_messages: [],
    queue_paused_reason: null,
    latest_job_id: 'ajob_1',
    latest_turn_no: 1,
    latest_job_state: 'succeeded',
  }),
}));

const CONNECTION = 'mconn_0123456789abcdef0123456789abcdef';
const OTHER = 'mconn_fedcba9876543210fedcba9876543210';

const CONFIG: AgentConfigApi = {
  repos: ['owner/repository'],
  runtimes: ['codex'],
  models: ['shared'],
  model_reasoning_efforts: { shared: ['high'] },
  runtime_reasoning_efforts: { codex: ['high'] },
  model_connections_enabled: true,
  model_connections: [
    {
      id: CONNECTION,
      name: 'My provider',
      models: ['shared', 'mine'],
      model_runtimes: { shared: ['codex'], mine: ['codex'] },
    },
  ],
  github_connected: true,
  github_install_url: null,
};

function makeJob(overrides: Partial<AgentJob> = {}): AgentJob {
  return {
    id: 'ajob_1',
    title: 'Current task',
    prompt: 'Fix the issue',
    state: 'done',
    repo: 'owner/repository',
    baseRef: 'dev',
    baseSha: 'abcdef1',
    branch: 'agent/ajob_1',
    runtime: 'codex',
    model: 'shared',
    spentUsd: 0,
    hasLedger: false,
    timeoutLabel: '',
    sandbox: 'container',
    attempts: [{ no: 1, status: 'finished' }],
    currentAttemptNo: 1,
    events: [],
    diffFiles: [],
    diffStat: { add: 0, del: 0 },
    gates: [],
    usage: { tokensIn: '0', tokensOut: '0', cachePct: 0, turns: 1 },
    ...overrides,
  };
}

function laterTurn(overrides: Partial<AgentJobApi> = {}): AgentJobApi {
  return {
    id: 'ajob_later',
    repo: 'owner/repository',
    base_sha: 'abcdef1',
    cancel_requested: false,
    current_attempt_id: null,
    published_pr_url: null,
    detail: null,
    metadata: null,
    created_at: null,
    updated_at: null,
    spent_usd: null,
    tokens_in: null,
    tokens_out: null,
    model_calls: null,
    turn_no: 2,
    state: 'succeeded',
    task_prompt: 'Later',
    runtime: 'codex',
    model: 'shared',
    ...overrides,
  };
}

async function send(prompt: string) {
  fireEvent.change(screen.getByLabelText('Add a follow-up'), { target: { value: prompt } });
  fireEvent.click(screen.getByRole('button', { name: /^(Send|Queue)$/ }));
}

/** The one message sent, checked to name no personal source: that is the Gateway. */
function gatewayCall() {
  expect(submitThreadMessage).toHaveBeenCalledTimes(1);
  const body = vi.mocked(submitThreadMessage).mock.calls[0][1];
  expect(body).not.toHaveProperty('model_connection_id');
  return body;
}

function chooseModel(name: string) {
  fireEvent.click(screen.getByLabelText('Model for this turn'));
  fireEvent.click(screen.getByRole('option', { name }));
}

beforeEach(() => {
  vi.mocked(getAgentConfig).mockReset().mockResolvedValue(CONFIG);
  vi.mocked(submitThreadMessage)
    .mockReset()
    .mockResolvedValue({ pending: null, job: { id: 'ajob_child' } } as never);
  vi.mocked(rewritePrompt).mockReset();
});
afterEach(() => cleanup());

describe('follow-up model sources', () => {
  it('inherits a personal source without naming it again', async () => {
    render(<JobDetail job={makeJob({ modelConnectionId: CONNECTION })} />);
    await waitFor(() =>
      expect(screen.getByLabelText('Model for this turn')).toHaveAccessibleDescription(
        'shared · My provider',
      ),
    );
    await send('Carry on');
    await waitFor(() =>
      expect(submitThreadMessage).toHaveBeenCalledWith('ajob_1', { prompt: 'Carry on' }),
    );
  });

  it('switches from a personal source to the Gateway model of the same name', async () => {
    render(<JobDetail job={makeJob({ modelConnectionId: CONNECTION })} />);
    await screen.findByLabelText('Model for this turn');
    await waitFor(() => expect(getAgentConfig).toHaveBeenCalled());
    chooseModel('shared');
    await send('Use the platform now');
    // The model goes even though its name is unchanged; no source is the Gateway.
    await waitFor(() =>
      expect(gatewayCall()).toMatchObject({ prompt: 'Use the platform now', model: 'shared' }),
    );
  });

  it('switches from the Gateway onto a personal source of the same name, without effort', async () => {
    render(<JobDetail job={makeJob({ reasoningEffort: 'high' })} />);
    await waitFor(() => expect(getAgentConfig).toHaveBeenCalled());
    chooseModel('shared · My provider');
    await send('Use my key');
    await waitFor(() =>
      expect(submitThreadMessage).toHaveBeenCalledWith('ajob_1', {
        prompt: 'Use my key',
        model: 'shared',
        model_connection_id: CONNECTION,
      }),
    );
  });

  it('inherits the source of the newest turn, not of the turn on screen', async () => {
    render(
      <JobDetail
        job={makeJob()}
        threadJobs={[laterTurn({ model: 'mine', model_connection_id: CONNECTION })]}
      />,
    );
    await waitFor(() =>
      expect(screen.getByLabelText('Model for this turn')).toHaveAccessibleDescription(
        'mine · My provider',
      ),
    );
    // Choosing the viewed turn's Gateway model is a change from the newest one.
    chooseModel('shared');
    await send('Back to the platform');
    await waitFor(() =>
      expect(gatewayCall()).toMatchObject({ prompt: 'Back to the platform', model: 'shared' }),
    );
  });

  it('holds Send while the inherited source is gone, rather than sending it to the Gateway', async () => {
    render(<JobDetail job={makeJob({ modelConnectionId: OTHER })} />);
    expect(
      await screen.findByText(/shared · personal connection \(unavailable\), which is unavailable/),
    ).toBeVisible();
    fireEvent.change(screen.getByLabelText('Add a follow-up'), { target: { value: 'Go on' } });
    expect(screen.getByRole('button', { name: /^(Send|Queue)$/ })).toBeDisabled();
    fireEvent.keyDown(screen.getByLabelText('Add a follow-up'), { key: 'Enter' });
    await waitFor(() => expect(screen.getByLabelText('Add a follow-up')).toBeEnabled());
    expect(submitThreadMessage).not.toHaveBeenCalled();
    chooseModel('shared');
    await send('Go on');
    await waitFor(() => expect(gatewayCall()).toMatchObject({ prompt: 'Go on', model: 'shared' }));
  });

  it('keeps the Gateway rewriter off for a personal source', async () => {
    render(<JobDetail job={makeJob({ modelConnectionId: CONNECTION })} />);
    await waitFor(() => expect(getAgentConfig).toHaveBeenCalled());
    fireEvent.change(screen.getByLabelText('Add a follow-up'), { target: { value: 'Tidy' } });
    expect(screen.getByRole('button', { name: 'Rewrite prompt' })).toBeDisabled();
    chooseModel('shared');
    expect(screen.getByRole('button', { name: 'Rewrite prompt' })).toBeEnabled();
  });
});

it('refreshes an explicitly switched personal source and blocks click, Enter and form submission after deletion', async () => {
  render(<JobDetail job={makeJob()} />);
  await screen.findByLabelText('Model for this turn');
  chooseModel('shared · My provider');
  fireEvent.change(screen.getByLabelText('Add a follow-up'), {
    target: { value: 'Keep this draft' },
  });
  vi.mocked(getAgentConfig).mockResolvedValue({ ...CONFIG, model_connections: [] });
  act(() => announceModelConnectionsChanged());
  await waitFor(() =>
    expect(screen.getByLabelText('Model for this turn')).toHaveAccessibleDescription(
      'shared · personal connection (unavailable)',
    ),
  );
  expect(screen.getByRole('button', { name: /^(Send|Queue)$/ })).toBeDisabled();
  fireEvent.click(screen.getByRole('button', { name: /^(Send|Queue)$/ }));
  fireEvent.keyDown(screen.getByLabelText('Add a follow-up'), { key: 'Enter' });
  fireEvent.submit(screen.getByLabelText('Add a follow-up').closest('form')!);
  await waitFor(() =>
    expect(screen.getByLabelText('Add a follow-up')).toHaveValue('Keep this draft'),
  );
  expect(submitThreadMessage).not.toHaveBeenCalled();
  chooseModel('shared');
  await waitFor(() => expect(screen.getByRole('button', { name: /^(Send|Queue)$/ })).toBeEnabled());
  await send('Explicitly use Gateway');
  // This is the original turn's Gateway pair, so inheritance needs no override.
  await waitFor(() => expect(gatewayCall()).toMatchObject({ prompt: 'Explicitly use Gateway' }));
  expect(gatewayCall()).not.toHaveProperty('model');
});

it('hides Rewrite and gives local account setup guidance when its connection is gone', async () => {
  vi.mocked(getAgentConfig).mockResolvedValue({
    ...CONFIG,
    gateway_inference: false,
    model_connection_required: true,
    models: [],
    model_connections: [],
  });
  render(<JobDetail job={makeJob({ modelConnectionId: CONNECTION })} />);
  await screen.findByRole('link', { name: 'Set up a model connection' });
  expect(screen.queryByRole('button', { name: 'Rewrite prompt' })).not.toBeInTheDocument();
  expect(screen.getByRole('button', { name: /^(Send|Queue)$/ })).toBeDisabled();
});
