// @vitest-environment jsdom
import '@testing-library/jest-dom/vitest';

import { cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { afterEach, beforeEach, expect, it, vi } from 'vitest';

import {
  createAgentJob,
  getAgentFileActivity,
  getAgentFileOperations,
  getAgentThreadContext,
  type ThreadContextApi,
} from '@/lib/api/agents';
import { submitThreadMessage } from '@/lib/api/pendingMessages';
import { JobDetail } from './JobDetail';
import type { AgentJob } from './types';

vi.mock('next/navigation', () => ({
  useRouter: () => ({ push: vi.fn() }),
  usePathname: () => '/job-a',
}));

vi.mock('@/lib/api/agents', async (importOriginal) => ({
  ...(await importOriginal<typeof import('@/lib/api/agents')>()),
  cancelAgentJob: vi.fn(),
  createAgentJob: vi.fn(),
  getAgentConfig: vi.fn().mockResolvedValue({
    repos: ['owner/repository'],
    runtimes: ['claude-code'],
    models: ['qwen-test'],
    github_connected: true,
  }),
  getAgentJobUsage: vi.fn().mockResolvedValue(null),
  refreshAgentJobUsage: vi.fn().mockResolvedValue(null),
  getAgentThreadStats: vi.fn().mockResolvedValue(null),
  getAgentThreadModelTtft: vi.fn().mockResolvedValue(null),
  getAgentThreadContext: vi.fn(),
  getRequestComposition: vi.fn().mockResolvedValue(null),
  // The workspace's own panels read nothing here: unavailable is enough.
  getAgentJobGit: vi.fn().mockRejectedValue(new Error('offline')),
  getAgentJobGitMetadata: vi.fn().mockRejectedValue(new Error('offline')),
  getThreadContextTiming: vi.fn().mockResolvedValue(null),
  getThreadContextDna: vi.fn().mockResolvedValue(null),
  // The File Activity section reads its own page; absent is enough here.
  getAgentFileActivity: vi.fn().mockResolvedValue(null),
  getAgentFileOperations: vi.fn().mockResolvedValue(null),
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
    thread_id: 'thread-a',
    pending_messages: [],
    queue_paused_reason: null,
    latest_job_id: 'job-a',
    latest_turn_no: 1,
    latest_job_state: 'succeeded',
    mid_turn_input_supported: true,
  }),
}));

function job(id: string, threadId: string): AgentJob {
  return {
    id,
    threadId,
    title: `Task ${threadId}`,
    prompt: 'Fix the issue',
    state: 'succeeded',
    repo: 'owner/repository',
    baseRef: 'dev',
    baseSha: 'abcdef1',
    branch: 'agent/task',
    runtime: 'claude-code',
    model: 'qwen-test',
    spentUsd: 0,
    hasLedger: false,
    timeoutLabel: '',
    sandbox: 'container',
    attempts: [{ no: 1, status: 'done' }],
    currentAttemptNo: 1,
    events: [],
    diffFiles: [],
    diffStat: { add: 0, del: 0 },
    gates: [],
    usage: { tokensIn: '0', tokensOut: '0', cachePct: 0, turns: 1 },
  } as unknown as AgentJob;
}

/** A conversation's view whose largest request names the conversation. */
function contextOf(threadId: string, peak: number): ThreadContextApi {
  const point = {
    index: 0,
    job_id: `job-${threadId}`,
    turn_no: 1,
    attempt_id: 1,
    seq: 1,
    event_id: 9,
    at: '2026-10-29T10:00:00Z',
    prompt_tokens: peak,
    cache_read_tokens: null,
    cache_write_tokens: null,
    uncached_input_tokens: null,
    output_tokens: 5,
    context_window: null,
    model: null,
    delta_tokens: null,
    after_compaction: false,
  };
  return {
    thread_id: threadId,
    generated_at: '2026-10-29T12:00:00Z',
    capabilities: [],
    latest: { point, window_share: null, compacted_after: false, turn_active: false },
    peak: point,
    turns: [],
    requests: [point],
    compactions: [],
    largest_changes: [],
    coverage: {
      turns: 1,
      attempts: 1,
      attempts_observed: 1,
      main_requests: 1,
      subagent_requests: 0,
      compactions: 0,
      compactions_unknown_outcome: 0,
      points_omitted: 0,
      unparsed_raw: 0,
    },
    events: [],
    event_counts: {},
    events_omitted: 0,
    event_capabilities: [],
  };
}

beforeEach(() => {
  vi.mocked(getAgentThreadContext).mockImplementation(async (jobId: string) =>
    jobId === 'job-b' ? contextOf('thread-b', 8_765) : contextOf('thread-a', 4_321),
  );
  // The File Activity section reads its own page; absent is enough here.
  vi.mocked(getAgentFileActivity).mockResolvedValue(null);
  vi.mocked(getAgentFileOperations).mockResolvedValue(null);
});

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
  sessionStorage.clear();
});

/** The main Context view's button, beside Chat and Trajectory. */
function contextView(): HTMLElement {
  return within(screen.getByRole('navigation', { name: 'Run views' })).getByRole('button', {
    name: 'context',
  });
}

function workspacePane(): HTMLElement {
  return document.getElementById('job-workspace-pane')!;
}

it('/context opens the main Context view and sends nothing', async () => {
  render(<JobDetail job={job('job-a', 'thread-a')} />);
  const box = await screen.findByLabelText('Add a follow-up');
  fireEvent.change(box, { target: { value: '/context' } });
  // The menu offers it with the other commands.
  expect(screen.getByRole('option', { name: /Context/ })).toBeInTheDocument();
  fireEvent.keyDown(box, { key: 'Enter' });

  expect(contextView()).toHaveAttribute('aria-current', 'page');
  expect(await screen.findAllByText('4,321')).not.toHaveLength(0);
  // It is the main view, not a workspace tab: the workspace stays closed.
  expect(workspacePane()).toHaveAttribute('hidden');
  expect(document.getElementById('workspace-panel-context')).toBeNull();
  // An app command: no message, no new turn, and the token is gone.
  expect(submitThreadMessage).not.toHaveBeenCalled();
  expect(createAgentJob).not.toHaveBeenCalled();
  expect(box).toHaveValue('');
});

it('keeps the draft that followed the command for the chat', async () => {
  render(<JobDetail job={job('job-a', 'thread-a')} />);
  const box = await screen.findByLabelText('Add a follow-up');
  fireEvent.change(box, { target: { value: '/context then fix the login test' } });
  fireEvent.keyDown(box, { key: 'Enter' });
  await waitFor(() => expect(contextView()).toHaveAttribute('aria-current', 'page'));
  expect(submitThreadMessage).not.toHaveBeenCalled();

  fireEvent.click(
    within(screen.getByRole('navigation', { name: 'Run views' })).getByRole('button', {
      name: 'chat',
    }),
  );
  expect(screen.getByLabelText('Add a follow-up')).toHaveValue('then fix the login test');
  expect(submitThreadMessage).not.toHaveBeenCalled();
});

it('choosing the menu row by keyboard opens it, and Escape only closes the menu', async () => {
  render(<JobDetail job={job('job-a', 'thread-a')} />);
  const box = await screen.findByLabelText('Add a follow-up');
  fireEvent.change(box, { target: { value: '/' } });
  fireEvent.keyDown(box, { key: 'Escape' });
  expect(screen.queryByRole('option', { name: /Context/ })).not.toBeInTheDocument();
  expect(contextView()).not.toHaveAttribute('aria-current');
  fireEvent.change(box, { target: { value: '/c' } });
  fireEvent.keyDown(box, { key: 'Enter' });
  expect(contextView()).toHaveAttribute('aria-current', 'page');
  expect(box).toHaveValue('');
});

it('leaves an open workspace on its own tab, which has no Context entry', async () => {
  render(<JobDetail job={job('job-a', 'thread-a')} />);
  const box = await screen.findByLabelText('Add a follow-up');
  fireEvent.click(screen.getByRole('button', { name: 'Open workspace' }));
  const workspace = screen.getByRole('region', { name: 'Job workspace' });
  const tabs = within(workspace).getByRole('tablist', { name: 'Workspace views' });
  expect(within(tabs).queryByRole('tab', { name: 'Context' })).not.toBeInTheDocument();
  fireEvent.click(within(tabs).getByRole('tab', { name: 'Terminal' }));

  fireEvent.change(box, { target: { value: '/context' } });
  fireEvent.keyDown(box, { key: 'Enter' });
  expect(contextView()).toHaveAttribute('aria-current', 'page');
  expect(await screen.findAllByText('4,321')).not.toHaveLength(0);
  // The workspace is not opened, closed or switched by it.
  expect(workspacePane()).not.toHaveAttribute('hidden');
  expect(within(tabs).getByRole('tab', { name: 'Terminal' })).toHaveAttribute(
    'aria-selected',
    'true',
  );
  expect(getAgentThreadContext).toHaveBeenCalledTimes(1);
});

it('another conversation never shows the previous one’s context', async () => {
  const { rerender } = render(<JobDetail job={job('job-a', 'thread-a')} />);
  const box = await screen.findByLabelText('Add a follow-up');
  fireEvent.change(box, { target: { value: '/context' } });
  fireEvent.keyDown(box, { key: 'Enter' });
  expect(await screen.findAllByText('4,321')).not.toHaveLength(0);

  rerender(<JobDetail job={job('job-b', 'thread-b')} />);
  fireEvent.click(contextView());
  expect(screen.queryAllByText('4,321')).toHaveLength(0);
  expect(await screen.findAllByText('8,765')).not.toHaveLength(0);
  expect(screen.queryAllByText('4,321')).toHaveLength(0);
  expect(getAgentThreadContext).toHaveBeenLastCalledWith('job-b', expect.anything());
});
