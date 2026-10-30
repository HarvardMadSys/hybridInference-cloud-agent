// @vitest-environment jsdom
import '@testing-library/jest-dom/vitest';

import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { getAgentConfig, type AgentConfigApi, type AgentJobApi } from '@/lib/api/agents';
import { submitThreadMessage } from '@/lib/api/pendingMessages';
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
  runtime_reasoning_efforts: { codex: ['high'] },
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
      models: ['shared'],
      model_runtimes: { shared: ['codex'] },
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
    modelConnectionId: OTHER,
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

/** The one message sent. */
function sentBody() {
  expect(submitThreadMessage).toHaveBeenCalledTimes(1);
  return vi.mocked(submitThreadMessage).mock.calls[0][1];
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
});
afterEach(() => cleanup());

describe('follow-up model sources', () => {
  it('inherits its source without naming it again', async () => {
    render(<JobDetail job={makeJob({ modelConnectionId: CONNECTION })} />);
    // Two connections list `shared`, so the label names the one it is on.
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

  it("switches to another connection's model of the same name", async () => {
    render(<JobDetail job={makeJob({ modelConnectionId: CONNECTION })} />);
    await screen.findByLabelText('Model for this turn');
    await waitFor(() => expect(getAgentConfig).toHaveBeenCalled());
    chooseModel('shared · Other provider');
    await send('Use the other key');
    // The model goes even though its name is unchanged: the source changed.
    await waitFor(() =>
      expect(sentBody()).toEqual({
        prompt: 'Use the other key',
        model: 'shared',
        model_connection_id: OTHER,
      }),
    );
  });

  it('switches source without carrying an effort the connection does not take', async () => {
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
      expect(screen.getByLabelText('Model for this turn')).toHaveAccessibleDescription('mine'),
    );
    // Choosing the viewed turn's pair is a change from the newest one.
    chooseModel('shared · Other provider');
    await send('Back to the other key');
    await waitFor(() =>
      expect(sentBody()).toMatchObject({
        prompt: 'Back to the other key',
        model: 'shared',
        model_connection_id: OTHER,
      }),
    );
  });

  it('holds Send while the inherited source is gone, rather than sending to a namesake', async () => {
    render(
      <JobDetail job={makeJob({ modelConnectionId: 'mconn_ffffffffffffffffffffffffffffffff' })} />,
    );
    expect(
      await screen.findByText(/shared · personal connection \(unavailable\), which is unavailable/),
    ).toBeVisible();
    fireEvent.change(screen.getByLabelText('Add a follow-up'), { target: { value: 'Go on' } });
    expect(screen.getByRole('button', { name: /^(Send|Queue)$/ })).toBeDisabled();
    fireEvent.keyDown(screen.getByLabelText('Add a follow-up'), { key: 'Enter' });
    await waitFor(() => expect(screen.getByLabelText('Add a follow-up')).toBeEnabled());
    expect(submitThreadMessage).not.toHaveBeenCalled();
    chooseModel('shared · My provider');
    await send('Go on');
    await waitFor(() =>
      expect(sentBody()).toMatchObject({
        prompt: 'Go on',
        model: 'shared',
        model_connection_id: CONNECTION,
      }),
    );
  });
});

it('refreshes an explicitly switched personal source and blocks click, Enter and form submission after deletion', async () => {
  render(<JobDetail job={makeJob()} />);
  await screen.findByLabelText('Model for this turn');
  chooseModel('shared · My provider');
  fireEvent.change(screen.getByLabelText('Add a follow-up'), {
    target: { value: 'Keep this draft' },
  });
  vi.mocked(getAgentConfig).mockResolvedValue({
    ...CONFIG,
    model_connections: CONFIG.model_connections!.filter((entry) => entry.id !== CONNECTION),
  });
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
  await send('Back to the original');
  // This is the original turn's pair, so inheritance needs no override.
  await waitFor(() => expect(sentBody()).toEqual({ prompt: 'Back to the original' }));
});

it('gives setup guidance when no connection is left', async () => {
  vi.mocked(getAgentConfig).mockResolvedValue({
    ...CONFIG,
    model_connection_required: true,
    model_connections: [],
  });
  render(<JobDetail job={makeJob({ modelConnectionId: CONNECTION })} />);
  await screen.findByRole('link', { name: 'Set up a model connection' });
  expect(screen.getByRole('button', { name: /^(Send|Queue)$/ })).toBeDisabled();
});
