// @vitest-environment jsdom
import '@testing-library/jest-dom/vitest';

import {
  act,
  cleanup,
  createEvent,
  fireEvent,
  render,
  screen,
  waitFor,
  within,
} from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import {
  cancelAgentJob,
  createAgentTerminal,
  deleteAgentTerminal,
  getAgentAttemptTelemetry,
  forkAgentJob,
  clearAgentGoal,
  getAgentGoal,
  getAgentJob,
  getAgentJobThread,
  listAllAgentThreadEvents,
  getAgentConfig,
  getAgentJobFiles,
  getAgentJobGit,
  getAgentJobUsage,
  getAgentThreadStats,
  listAgentConversations,
  listAgentTerminals,
  pauseAgentGoal,
  setAgentGoal,
  resumeAgentGoal,
  resizeAgentTerminal,
  refreshAgentJobUsage,
  restoreAgentJob,
  renameAgentJob,
  pinAgentJob,
  unpinAgentJob,
  streamAgentTerminal,
  writeAgentTerminalInput,
  writeAgentJobFile,
  type AgentJobApi,
  type AgentJobFilesApi,
  type AgentJobUsageApi,
  type AgentThreadEventApi,
} from '@/lib/api/agents';

import { announceAgentSessionsChanged } from '@/lib/api/agentSessionEvents';
import {
  decideMcpInvocation,
  getMcpThreadBindings,
  listMcpInvocations,
  updateMcpThreadBinding,
  type McpBindingApi,
  type McpInvocationApi,
} from '@/lib/api/mcp';
import { deleteAttachment, uploadAttachment } from '@/lib/api/attachments';
import {
  getThreadQueue,
  sendPendingMessageNow,
  submitThreadMessage,
  type PendingMessageApi,
} from '@/lib/api/pendingMessages';

import { APIError } from '@/lib/utils/errors';
import { listThreadQuestions } from '@/lib/api/questions';
import { rewritePrompt } from '@/lib/api/promptRewrite';

vi.mock('@/lib/api/promptRewrite', () => ({ rewritePrompt: vi.fn() }));

vi.mock('@/lib/api/questions', () => ({
  listThreadQuestions: vi
    .fn()
    .mockResolvedValue({ questions: [], job_id: 'ajob_1', attempt_id: 1 }),
  answerThreadQuestion: vi.fn(),
}));

import { installSideChatLocks } from './sideChatTestSupport';
import { FirstRunProvider } from './firstRun';
import { receiveHint } from './agentPush';
import { sideChatKeys } from './sideChatStorage';
import { JobDetail, WORKSPACE_WIDTH_STORAGE_KEY } from './JobDetail';
import { IDLE_QUEUE_POLL_MS } from './usePendingMessages';
import { watchPullRequestPrompt } from './watchPullRequest';
import type { AgentJob } from './types';
import { toDisplayEvent } from './adapt';
import { projectProtocolEvents } from './trajectoryProtocol';

const renderingWork = vi.hoisted(() => ({
  trajectory: vi.fn(),
  history: vi.fn(),
  current: vi.fn(),
}));

vi.mock('./trajectorySteps', async (importOriginal) => {
  const original = await importOriginal<typeof import('./trajectorySteps')>();
  return {
    ...original,
    buildTrajectorySteps: (...args: Parameters<typeof original.buildTrajectorySteps>) => {
      renderingWork.trajectory();
      return original.buildTrajectorySteps(...args);
    },
  };
});

vi.mock('./conversationTurnNavigation', async (importOriginal) => {
  const original = await importOriginal<typeof import('./conversationTurnNavigation')>();
  return {
    ...original,
    describeHistoryTurns: (...args: Parameters<typeof original.describeHistoryTurns>) => {
      renderingWork.history();
      return original.describeHistoryTurns(...args);
    },
    describeCurrentTurn: (...args: Parameters<typeof original.describeCurrentTurn>) => {
      renderingWork.current();
      return original.describeCurrentTurn(...args);
    },
  };
});

const navigation = vi.hoisted(() => ({ push: vi.fn() }));

vi.mock('next/navigation', () => ({
  useRouter: () => navigation,
  usePathname: () => '/ajob_1',
}));

vi.mock('@/lib/api/mcp', async (importOriginal) => ({
  ...(await importOriginal<typeof import('@/lib/api/mcp')>()),
  listMcpInvocations: vi.fn().mockResolvedValue({ invocations: [] }),
  decideMcpInvocation: vi.fn(),
  getMcpThreadBindings: vi.fn().mockResolvedValue({ bindings: [] }),
  listMcpConnections: vi.fn().mockResolvedValue({ connections: [] }),
  updateMcpThreadBinding: vi.fn(),
}));

vi.mock('@/lib/api/pendingMessages', async (importOriginal) => ({
  ...(await importOriginal<typeof import('@/lib/api/pendingMessages')>()),
  submitThreadMessage: vi.fn(),
  getThreadQueue: vi.fn(),
  editPendingMessage: vi.fn(),
  deletePendingMessage: vi.fn(),
  resumeThreadQueue: vi.fn(),
  sendPendingMessageNow: vi.fn(),
}));

/** The queue a conversation with nothing waiting reads back. */
function emptyQueue(
  threadId: string,
  latestJobId: string | null = null,
  latestJobState: string | null = latestJobId ? 'succeeded' : null,
) {
  return {
    thread_id: threadId,
    pending_messages: [],
    queue_paused_reason: null,
    latest_job_id: latestJobId,
    latest_turn_no: null,
    latest_job_state: latestJobState,
  };
}

vi.mock('@/lib/api/attachments', () => ({
  uploadAttachment: vi.fn(),
  deleteAttachment: vi.fn().mockResolvedValue(undefined),
}));

import { listSkills } from '@/lib/api/skills';
vi.mock('@/lib/api/skills', () => ({ listSkills: vi.fn() }));

vi.mock('@/lib/api/agents', () => ({
  cancelAgentJob: vi.fn(),
  createAgentTerminal: vi.fn(),
  deleteAgentTerminal: vi.fn(),
  forkAgentJob: vi.fn(),
  getAgentJob: vi.fn(),
  getAgentJobThread: vi.fn(),
  listAllAgentThreadEvents: vi.fn(),
  streamAgentJob: vi.fn().mockResolvedValue(undefined),
  getAgentConfig: vi.fn(),
  getAgentJobFiles: vi.fn(),
  getAgentJobGit: vi.fn(),
  getAgentJobUsage: vi.fn(),
  getAgentAttemptTelemetry: vi.fn(),
  getAgentThreadStats: vi.fn(),
  listAgentConversations: vi.fn(),
  listAgentTerminals: vi.fn(),
  resizeAgentTerminal: vi.fn(),
  refreshAgentJobUsage: vi.fn(),
  restoreAgentJob: vi.fn(),
  renameAgentJob: vi.fn(),
  pinAgentJob: vi.fn(),
  unpinAgentJob: vi.fn(),
  pauseAgentGoal: vi.fn(),
  setAgentGoal: vi.fn(),
  resumeAgentGoal: vi.fn(),
  clearAgentGoal: vi.fn(),
  getAgentGoal: vi.fn(),
  streamAgentTerminal: vi.fn(),
  writeAgentTerminalInput: vi.fn(),
  writeAgentJobFile: vi.fn(),
}));

const AGENT_CONFIG = {
  repos: ['owner/repository'],
  runtimes: ['claude-code'],
  models: ['qwen-test', 'glm-test'],
  github_connected: true,
  github_install_url: null,
};

function makeJob(overrides: Partial<AgentJob> = {}): AgentJob {
  return {
    id: 'ajob_1',
    title: 'Current task',
    prompt: 'Current task\nwith all of its detail.',
    state: 'running',
    repo: 'owner/repository',
    baseRef: 'dev',
    baseSha: 'abcdef1',
    branch: 'agent/ajob_1',
    runtime: 'claude-code',
    model: 'qwen-test',
    spentUsd: 0.1,
    hasLedger: true,
    timeoutLabel: '',
    sandbox: 'container',
    attempts: [{ no: 1, status: 'live' }],
    currentAttemptNo: 1,
    events: [
      { kind: 'lifecycle', text: 'started', attemptNo: 1 },
      {
        kind: 'tool_use',
        tool: 'Bash',
        detail: 'pytest -q',
        output: ['2 passed'],
        attemptNo: 1,
      },
      { kind: 'message', text: 'I found the **cause**.', attemptNo: 1 },
    ],
    diffFiles: ['src/example.ts'],
    diffFileDetails: [
      {
        path: 'src/example.ts',
        add: 1,
        del: 0,
        lines: [{ marker: 'add', text: '+fixed' }],
      },
    ],
    diffStat: { add: 1, del: 0 },
    gates: [],
    usage: { tokensIn: '1.2k', tokensOut: '80', cachePct: 0, turns: 2 },
    ...overrides,
  };
}

function openWorkspace() {
  fireEvent.click(screen.getByRole('button', { name: 'Open workspace' }));
}

function addSideChat() {
  fireEvent.click(screen.getByRole('button', { name: 'New Side Chat' }));
}

function sideChatFixture() {
  installSideChatLocks();
  const source = {
    id: 'tabs-main',
    thread_id: 'tabs-main-thread',
    sandbox_id: 'tabs-group',
    workspace_shared: true,
    native_state: 'ready',
    native_fork_available: true,
    turn_no: 1,
    state: 'succeeded',
    conversation_state: 'idle',
    task_prompt: 'Main',
    runtime: 'codex',
    model: 'qwen-test',
    repo: 'owner/repository',
    current_attempt_id: null,
  } as AgentJobApi;
  const jobs = new Map([[source.id, source]]);
  let forks = 0;
  vi.mocked(getAgentJob).mockImplementation(async (id) => jobs.get(id)!);
  vi.mocked(getAgentJobThread).mockImplementation(async (id) => {
    const job = jobs.get(id)!;
    return {
      thread_id: job.thread_id!,
      jobs: [...jobs.values()].filter((row) => row.thread_id === job.thread_id),
      messages: [],
    };
  });
  vi.mocked(listAllAgentThreadEvents).mockResolvedValue([]);
  vi.mocked(forkAgentJob).mockImplementation(async (id) => {
    const child = {
      ...jobs.get(id)!,
      id: `tabs-child-${++forks}`,
      thread_id: `tabs-thread-${forks}`,
      forked_from_job_id: id,
      fork_anchor_job_id: id,
    };
    jobs.set(child.id, child);
    return child;
  });
  vi.mocked(submitThreadMessage).mockImplementation(async (threadId, body) => {
    const latest = [...jobs.values()].filter((row) => row.thread_id === threadId).at(-1)!;
    const child = {
      ...latest,
      id: `${latest.id}-next`,
      turn_no: 2,
      forked_from_job_id: null,
      task_prompt: body.prompt,
    };
    jobs.set(child.id, child);
    return { pending: null, job: child };
  });
  return {
    source,
    jobs,
    job: makeJob({
      id: source.id,
      threadId: source.thread_id!,
      sandboxId: source.sandbox_id!,
      workspaceShared: true,
      state: 'done',
    }),
    userId: 'tabs-owner',
  };
}

function measuredUsage(): AgentJobUsageApi {
  const totals = {
    calls: 2,
    tokens_in: 1200,
    tokens_out: 80,
    cache_read: null,
    cache_write: null,
    reasoning: null,
    spent_usd: null,
  };
  return {
    ledger: {
      status: 'available',
      source: 'gateway_api_logs',
      attribution: 'grant_time_window',
      finalized: false,
      totals,
      unknown_rows: null,
      estimated_calls: 0,
      queried_at: null,
      missing_attempts: 0,
      attempts: [
        {
          attempt_id: 1,
          attempt_no: 1,
          status: 'available',
          reason: null,
          totals,
          unknown_rows: null,
          estimated_calls: 0,
          queried_at: null,
          windows: [],
        },
      ],
    },
    runtime: {
      source: 'runtime',
      input_tokens: null,
      output_tokens: null,
      cache_read_tokens: null,
    },
  };
}

const HISTORY = [
  { id: 1, role: 'user' as const, content: 'Earlier question', jobId: 'ajob_0', createdAt: null },
  {
    id: 2,
    role: 'assistant' as const,
    content: 'Earlier answer',
    jobId: 'ajob_0',
    createdAt: null,
  },
];

describe('JobDetail', () => {
  it('uses a rewritten follow-up only after the owner sends it', async () => {
    vi.mocked(rewritePrompt).mockResolvedValue({
      kind: 'prompt',
      prompt: 'Add regression coverage for CSV exports, including empty records.',
    });
    render(<JobDetail job={makeJob({ state: 'done' })} />);
    const box = screen.getByRole('textbox', { name: 'Add a follow-up' });
    fireEvent.change(box, { target: { value: 'Add tests' } });
    fireEvent.click(screen.getByRole('button', { name: 'Rewrite prompt' }));
    await waitFor(() =>
      expect(box).toHaveValue('Add regression coverage for CSV exports, including empty records.'),
    );
    expect(submitThreadMessage).not.toHaveBeenCalled();
    fireEvent.click(screen.getByRole('button', { name: 'Send' }));
    await waitFor(() =>
      expect(submitThreadMessage).toHaveBeenCalledWith(
        'ajob_1',
        expect.objectContaining({
          prompt: 'Add regression coverage for CSV exports, including empty records.',
        }),
      ),
    );
  });

  it('opens the latest live question while an older completed turn is being viewed', async () => {
    const job = makeJob({
      state: 'done',
      turnNo: 1,
      threadId: 'thread-questions',
      currentAttemptId: 1,
    });
    const latest = {
      id: 'newer-live-job',
      thread_id: 'thread-questions',
      turn_no: 2,
      state: 'running',
      current_attempt_id: 2,
      cancel_requested: false,
      native_state: 'ready',
      task_prompt: 'Next task',
    } as AgentJobApi;
    vi.mocked(listThreadQuestions).mockResolvedValue({
      job_id: latest.id,
      attempt_id: 2,
      questions: [
        {
          request_id: 'latest-question',
          questions: [
            {
              id: 'latest-choice',
              header: '',
              question: 'Ready to continue?',
              multiple: false,
              allow_custom: true,
              options: [],
            },
          ],
        },
      ],
    });
    const { rerender } = render(<JobDetail job={job} threadJobs={[latest]} />);
    expect(
      await screen.findByRole('dialog', { name: 'The agent has a question' }),
    ).toHaveTextContent('Ready to continue?');
    rerender(<JobDetail job={job} threadJobs={[{ ...latest, state: 'succeeded' }]} />);
    expect(screen.queryByRole('dialog')).not.toBeInTheDocument();
  });

  it('opens the active turn question and removes it when that turn settles', async () => {
    const job = makeJob({ threadId: 'thread-questions', currentAttemptId: 1 });
    vi.mocked(listThreadQuestions).mockResolvedValue({
      job_id: job.id,
      attempt_id: 1,
      questions: [
        {
          request_id: 'main-question',
          questions: [
            {
              id: 'main-choice',
              header: 'Choice',
              question: 'Which change should we make?',
              multiple: false,
              allow_custom: true,
              options: [],
            },
          ],
        },
      ],
    });
    const { rerender } = render(<JobDetail job={job} />);
    expect(
      await screen.findByRole('dialog', { name: 'The agent has a question' }),
    ).toBeInTheDocument();
    expect(listThreadQuestions).toHaveBeenCalledWith('thread-questions', expect.any(AbortSignal));
    const field = screen.getByRole('textbox', { name: 'Your answer' });
    fireEvent.change(field, { target: { value: 'Keep the current behavior.' } });
    fireEvent.keyDown(field, { key: 'j', ctrlKey: true });
    expect(screen.getByRole('button', { name: 'Open workspace' })).toHaveAttribute(
      'aria-expanded',
      'false',
    );
    expect(field).toHaveValue('Keep the current behavior.');
    rerender(<JobDetail job={{ ...job, state: 'done' }} />);
    expect(screen.queryByRole('dialog')).not.toBeInTheDocument();
  });

  it('creates independent side chats, preserves drafts and sends to the selected child', async () => {
    const { source, job, userId } = sideChatFixture();
    const props = { job, userId, threadJobs: [source] };
    const view = render(<JobDetail {...props} />);
    openWorkspace();
    expect(screen.queryByRole('tab', { name: /Side Chat/ })).not.toBeInTheDocument();
    addSideChat();
    await waitFor(() => expect(forkAgentJob).toHaveBeenCalledTimes(1));
    fireEvent.change(screen.getByRole('textbox', { name: 'Message side chat' }), {
      target: { value: 'First draft' },
    });
    addSideChat();
    await waitFor(() => expect(forkAgentJob).toHaveBeenCalledTimes(2));
    expect(screen.getByRole('tab', { name: 'Side Chat 2' })).toHaveFocus();
    expect(screen.getByRole('textbox', { name: 'Message side chat' })).toHaveValue('');
    fireEvent.change(screen.getByRole('textbox', { name: 'Message side chat' }), {
      target: { value: 'Second draft' },
    });
    await waitFor(() =>
      expect(screen.getByRole('button', { name: 'Send side chat message' })).toBeEnabled(),
    );
    fireEvent.click(screen.getByRole('button', { name: 'Send side chat message' }));
    await waitFor(() =>
      expect(submitThreadMessage).toHaveBeenCalledWith('tabs-thread-2', { prompt: 'Second draft' }),
    );
    fireEvent.click(screen.getByRole('tab', { name: 'Side Chat' }));
    expect(screen.getByRole('textbox', { name: 'Message side chat' })).toHaveValue('First draft');
    await waitFor(() =>
      expect(screen.getByRole('button', { name: 'Send side chat message' })).toBeEnabled(),
    );
    fireEvent.click(screen.getByRole('button', { name: 'Send side chat message' }));
    await waitFor(() =>
      expect(submitThreadMessage).toHaveBeenCalledWith('tabs-thread-1', { prompt: 'First draft' }),
    );
    expect(new Set(vi.mocked(forkAgentJob).mock.calls.map((call) => call[1]?.requestId)).size).toBe(
      2,
    );
    fireEvent.click(screen.getByRole('button', { name: 'Close Side Chat 2' }));
    expect(screen.getByRole('tab', { name: 'Side Chat' })).toHaveAttribute('aria-selected', 'true');
    view.unmount();
    render(<JobDetail {...props} />);
    openWorkspace();
    expect(screen.queryByRole('tab', { name: 'Side Chat 2' })).not.toBeInTheDocument();
    await waitFor(() => expect(getAgentJobThread).toHaveBeenCalledWith('tabs-child-1-next'));
    expect(forkAgentJob).toHaveBeenCalledTimes(2);
    expect(cancelAgentJob).not.toHaveBeenCalled();
  });

  it('starts a code review in a child tab for the displayed turn and reuses that review', async () => {
    const { source, job, userId, jobs } = sideChatFixture();
    const prUrl = 'https://github.com/owner/repository/pull/7';
    const reviewed = {
      ...job,
      nativeState: 'ready' as const,
      nativeForkAvailable: true,
      baseRef: 'original-base',
      baseSha: 'original-base-sha',
      branch: 'agent/original-change',
      prUrl,
    };
    const later = {
      ...source,
      id: 'tabs-later-main',
      turn_no: 2,
      base_ref: 'later-base',
      base_sha: 'later-base-sha',
      published_branch: 'agent/later-change',
      published_pr_url: 'https://github.com/owner/repository/pull/8',
    };
    jobs.set(later.id, later);
    render(<JobDetail job={reviewed} userId={userId} threadJobs={[source, later]} />);

    const review = screen.getByRole('button', { name: 'Review' });
    expect(review).toBeEnabled();
    expect(review.parentElement).toContainElement(
      screen.getByRole('link', { name: 'Open draft PR' }),
    );
    fireEvent.click(review);

    const tab = await screen.findByRole('tab', { name: 'Review' });
    expect(tab).toHaveAttribute('aria-selected', 'true');
    await waitFor(() => expect(submitThreadMessage).toHaveBeenCalledTimes(1));
    expect(forkAgentJob).toHaveBeenCalledExactlyOnceWith(
      source.id,
      expect.objectContaining({ requestId: expect.any(String) }),
    );
    const [threadId, body, options] = vi.mocked(submitThreadMessage).mock.calls[0];
    expect(threadId).toBe('tabs-thread-1');
    expect(body.prompt).toMatch(/review/i);
    expect(body.prompt).toMatch(/read.only/i);
    expect(body.prompt).toContain(reviewed.baseRef);
    expect(body.prompt).toContain(reviewed.baseSha);
    expect(body.prompt).toContain(reviewed.branch);
    expect(body.prompt).toContain(prUrl);
    expect(body.prompt).toMatch(/no Git remote, GitHub CLI, or GitHub credentials/);
    expect(body.prompt).toContain(`git diff ${reviewed.baseSha}`);
    expect(body.prompt).not.toContain(later.base_sha);
    expect(body.skill_ids).toEqual([]);
    expect(options).toEqual(expect.objectContaining({ requestId: expect.any(String) }));
    expect(navigation.push).not.toHaveBeenCalled();

    const childReads = vi.mocked(getAgentJobThread).mock.calls.length;
    fireEvent.click(screen.getByRole('tab', { name: /^Git/ }));
    fireEvent.click(review);
    expect(tab).toHaveAttribute('aria-selected', 'true');
    await waitFor(() =>
      expect(vi.mocked(getAgentJobThread).mock.calls.length).toBeGreaterThan(childReads),
    );
    expect(forkAgentJob).toHaveBeenCalledTimes(1);
    expect(submitThreadMessage).toHaveBeenCalledTimes(1);
    expect(cancelAgentJob).not.toHaveBeenCalled();
  });

  it('reviews a later turn against the original PR baseline while forking the displayed turn', async () => {
    const { source, job, userId, jobs } = sideChatFixture();
    const selected = { ...source, turn_no: 2, base_sha: 'previous-published-pr-commit' };
    const first = {
      ...source,
      id: 'tabs-original-turn',
      turn_no: 1,
      base_sha: 'original-pr-base-sha',
    };
    jobs.set(selected.id, selected);
    jobs.set(first.id, first);
    render(
      <JobDetail
        job={{
          ...job,
          turnNo: 2,
          nativeState: 'ready',
          nativeForkAvailable: true,
          baseSha: selected.base_sha,
          prUrl: 'https://github.com/owner/repository/pull/7',
        }}
        userId={userId}
        threadJobs={[first, selected]}
      />,
    );

    fireEvent.click(screen.getByRole('button', { name: 'Review' }));
    await waitFor(() => expect(submitThreadMessage).toHaveBeenCalledTimes(1));
    expect(forkAgentJob).toHaveBeenCalledExactlyOnceWith(
      selected.id,
      expect.objectContaining({ requestId: expect.any(String) }),
    );
    const [threadId, body] = vi.mocked(submitThreadMessage).mock.calls[0];
    expect(threadId).toBe('tabs-thread-1');
    expect(body.prompt).toContain(first.base_sha);
    expect(body.prompt).not.toContain(selected.base_sha);
    expect(navigation.push).not.toHaveBeenCalled();
  });

  it('starts code review beside the pull request link in the Git workspace', async () => {
    const { source, job, userId } = sideChatFixture();
    const prUrl = 'https://github.com/owner/repository/pull/7';
    render(
      <JobDetail
        job={{ ...job, nativeState: 'ready', nativeForkAvailable: true, prUrl }}
        userId={userId}
        threadJobs={[source]}
      />,
    );
    openWorkspace();
    const git = screen.getByLabelText('Git workspace');
    fireEvent.click(within(git).getByRole('tab', { name: 'review' }));
    const review = within(git).getByRole('button', { name: 'Review' });
    expect(review).toBeEnabled();
    expect(review.parentElement).toContainElement(
      within(git).getByRole('link', { name: 'Open pull request' }),
    );
    fireEvent.click(review);

    expect(await screen.findByRole('tab', { name: 'Review' })).toHaveAttribute(
      'aria-selected',
      'true',
    );
    await waitFor(() => expect(submitThreadMessage).toHaveBeenCalledTimes(1));
    expect(submitThreadMessage).toHaveBeenCalledWith(
      'tabs-thread-1',
      expect.objectContaining({ prompt: expect.stringContaining(prUrl) }),
      expect.objectContaining({ requestId: expect.any(String) }),
    );
    expect(navigation.push).not.toHaveBeenCalled();
  });

  it.each([
    ['archived', { archivedAt: '2026-09-10T00:00:00Z' }, 'tabs-owner'],
    ['lost', { nativeState: 'lost' }, 'tabs-owner'],
    ['not forkable', { nativeForkAvailable: false }, 'tabs-owner'],
    ['signed out', {}, undefined],
  ] as const)('disables code review when the source is %s', (_reason, overrides, userId) => {
    render(
      <JobDetail
        job={makeJob({
          state: 'done',
          threadId: 'tabs-main-thread',
          sandboxId: 'tabs-group',
          workspaceShared: true,
          nativeState: 'ready',
          nativeForkAvailable: true,
          prUrl: 'https://github.com/owner/repository/pull/7',
          ...overrides,
        })}
        userId={userId}
      />,
    );
    const review = screen.getByRole('button', { name: 'Review' });
    expect(review).toBeDisabled();
    fireEvent.click(review);
    expect(forkAgentJob).not.toHaveBeenCalled();
    expect(submitThreadMessage).not.toHaveBeenCalled();
  });

  it('watches the published PR in the same thread and follows the resulting turn', async () => {
    const prUrl = 'https://github.com/owner/repository/pull/7';
    vi.mocked(submitThreadMessage).mockResolvedValue({
      pending: null,
      job: { id: 'ajob_watch' },
    } as never);
    window.history.pushState(null, '', '/ajob_1');
    render(
      <JobDetail
        job={makeJob({
          state: 'done',
          threadId: 'original-thread',
          workspacePublicationState: 'published',
          prUrl,
        })}
        userId="alice"
      />,
    );

    const watch = screen.getByRole('button', { name: 'Watch PR and merge' });
    await waitFor(() => expect(watch).toBeEnabled());
    expect(watch.parentElement).toContainElement(
      screen.getByRole('link', { name: 'Open draft PR' }),
    );
    fireEvent.click(watch);

    await waitFor(() => expect(submitThreadMessage).toHaveBeenCalledTimes(1));
    const [threadId, body, options] = vi.mocked(submitThreadMessage).mock.calls[0];
    expect(threadId).toBe('original-thread');
    expect(body).toEqual({
      prompt: expect.any(String),
      require_idle: true,
      expected_latest_job_id: 'ajob_1',
    });
    expect(options).toEqual({ requestId: 'watch-pr:v2:original-thread:ajob_1' });
    expect(body.prompt).toContain(prUrl);
    expect(body.prompt).toContain('owner/repository');
    expect(body.prompt).toContain('agent/ajob_1');
    expect(body.prompt).toMatch(/checks|tests/i);
    expect(body.prompt).toMatch(/reviews|approvals/i);
    expect(body.prompt).toMatch(/latest.*head|head.*latest/i);
    expect(body.prompt).toMatch(/merge/i);
    expect(forkAgentJob).not.toHaveBeenCalled();
    await waitFor(() => expect(window.location.pathname).toBe('/ajob_watch'));
    expect(navigation.push).not.toHaveBeenCalled();
    expect(watch).toBeDisabled();
    fireEvent.click(watch);
    expect(submitThreadMessage).toHaveBeenCalledTimes(1);
  });

  it('watches from the Git PR action, prevents duplicate sends and applies a queued result', async () => {
    const prUrl = 'https://github.com/owner/repository/pull/7';
    let finishSend!: (value: Awaited<ReturnType<typeof submitThreadMessage>>) => void;
    vi.mocked(submitThreadMessage).mockReturnValue(
      new Promise((resolve) => {
        finishSend = resolve;
      }),
    );
    window.history.pushState(null, '', '/ajob_1');
    render(
      <JobDetail
        job={makeJob({ state: 'done', threadId: 'original-thread', prUrl })}
        userId="alice"
      />,
    );
    openWorkspace();
    const git = screen.getByLabelText('Git workspace');
    fireEvent.click(within(git).getByRole('tab', { name: 'review' }));
    const watch = within(git).getByRole('button', { name: 'Watch PR and merge' });
    await waitFor(() => expect(watch).toBeEnabled());
    expect(watch.parentElement).toContainElement(
      within(git).getByRole('link', { name: 'Open pull request' }),
    );
    fireEvent.click(watch);
    expect(watch).toBeDisabled();
    for (const button of screen.getAllByRole('button', { name: 'Watch PR and merge' })) {
      expect(button).toBeDisabled();
      fireEvent.click(button);
    }
    expect(submitThreadMessage).toHaveBeenCalledTimes(1);
    expect(submitThreadMessage).toHaveBeenCalledWith(
      'original-thread',
      {
        prompt: expect.stringContaining(prUrl),
        require_idle: true,
        expected_latest_job_id: 'ajob_1',
      },
      { requestId: 'watch-pr:v2:original-thread:ajob_1' },
    );
    const prompt = vi.mocked(submitThreadMessage).mock.calls[0][1].prompt;
    await act(async () =>
      finishSend({
        pending: {
          id: 'apmsg_watch',
          thread_id: 'original-thread',
          position: 1,
          state: 'queued',
          revision: 1,
          prompt,
        },
        job: null,
      }),
    );

    const queue = await screen.findByRole('region', { name: 'Queued messages' });
    expect(within(queue).getByTitle(prompt, { normalizer: (value) => value })).toBeInTheDocument();
    expect(within(queue).getByText('1 pending')).toBeInTheDocument();
    expect(window.location.pathname).toBe('/ajob_1');
    expect(navigation.push).not.toHaveBeenCalled();
    expect(forkAgentJob).not.toHaveBeenCalled();
    expect(watch).toBeDisabled();
    fireEvent.click(watch);
    expect(submitThreadMessage).toHaveBeenCalledTimes(1);
  });

  it('uses live idle and published state over stale thread rows and re-enables watching after a turn change', async () => {
    const job = makeJob({
      state: 'done',
      threadId: 'original-thread',
      conversationState: 'idle',
      workspacePublicationState: 'published',
      prUrl: 'https://github.com/owner/repository/pull/7',
    });
    const threadJobs = [
      {
        id: 'ajob_stale',
        thread_id: 'original-thread',
        turn_no: 2,
        state: 'running',
        conversation_state: 'running',
        workspace_publication_state: 'pending',
        runtime: 'claude-code',
        model: 'qwen-test',
      } as AgentJobApi,
    ];
    vi.mocked(submitThreadMessage).mockResolvedValue({
      pending: null,
      job: { id: 'ajob_watch' },
    } as never);
    window.history.pushState(null, '', '/ajob_1');
    const { rerender } = render(<JobDetail job={job} userId="alice" threadJobs={threadJobs} />);

    const watch = screen.getByRole('button', { name: 'Watch PR and merge' });
    await waitFor(() => expect(watch).toBeEnabled());
    fireEvent.click(watch);
    await waitFor(() => expect(window.location.pathname).toBe('/ajob_watch'));
    expect(watch).toBeDisabled();

    rerender(
      <JobDetail
        job={{ ...job, id: 'ajob_watch', turnNo: 3 }}
        userId="alice"
        threadJobs={threadJobs}
      />,
    );
    expect(screen.getByRole('button', { name: 'Watch PR and merge' })).toBeEnabled();
    rerender(<JobDetail job={job} userId="alice" threadJobs={threadJobs} />);
    const originalWatch = screen.getByRole('button', { name: 'Watch PR and merge' });
    expect(originalWatch).toBeEnabled();
    fireEvent.click(originalWatch);
    await waitFor(() => expect(submitThreadMessage).toHaveBeenCalledTimes(2));
  });

  it('reuses the queue latest turn across historical mounts without epochs and uses a new key after the latest turn changes', async () => {
    const job = makeJob({
      state: 'done',
      threadId: 'original-thread',
      conversationState: 'idle',
      workspacePublicationState: 'published',
      prUrl: 'https://github.com/owner/repository/pull/7',
    });
    let latestJobId = 'ajob_latest';
    vi.mocked(getThreadQueue).mockImplementation(async (threadId) =>
      emptyQueue(threadId, latestJobId),
    );
    const existingReceipt = { pending: null, job: { id: 'ajob_watch_shared' } } as never;
    vi.mocked(submitThreadMessage)
      .mockResolvedValueOnce(existingReceipt)
      .mockResolvedValueOnce(existingReceipt)
      .mockResolvedValueOnce({ pending: null, job: { id: 'ajob_watch_next' } } as never);
    const first = render(<JobDetail job={job} userId="alice" />);
    const watch = screen.getByRole('button', { name: 'Watch PR and merge' });
    await waitFor(() => expect(watch).toBeEnabled());
    fireEvent.click(watch);
    await waitFor(() => expect(window.location.pathname).toBe('/ajob_watch_shared'));
    const firstRequestId = vi.mocked(submitThreadMessage).mock.calls[0][2]?.requestId;
    expect(firstRequestId).toBe('watch-pr:v2:original-thread:ajob_latest');
    expect(vi.mocked(submitThreadMessage).mock.calls[0][1]).toMatchObject({
      require_idle: true,
      expected_latest_job_id: 'ajob_latest',
    });
    first.unmount();

    window.history.pushState(null, '', '/ajob_older');
    const second = render(
      <JobDetail job={{ ...job, id: 'ajob_older', turnNo: 2 }} userId="alice" />,
    );
    const historicalWatch = screen.getByRole('button', { name: 'Watch PR and merge' });
    await waitFor(() => expect(historicalWatch).toBeEnabled());
    fireEvent.click(historicalWatch);
    await waitFor(() => expect(submitThreadMessage).toHaveBeenCalledTimes(2));
    await waitFor(() => expect(window.location.pathname).toBe('/ajob_watch_shared'));
    const [, historicalBody, historicalOptions] = vi.mocked(submitThreadMessage).mock.calls[1];
    expect(historicalOptions?.requestId).toBe(firstRequestId);
    expect(historicalBody).toEqual(vi.mocked(submitThreadMessage).mock.calls[0][1]);

    latestJobId = 'ajob_published_next';
    second.rerender(
      <JobDetail job={{ ...job, id: 'ajob_published_next', turnNo: 3 }} userId="alice" />,
    );
    const priorReads = vi.mocked(getThreadQueue).mock.calls.length;
    receiveHint('queue', 'original-thread');
    await waitFor(() =>
      expect(vi.mocked(getThreadQueue).mock.calls.length).toBeGreaterThan(priorReads),
    );
    const nextWatch = screen.getByRole('button', { name: 'Watch PR and merge' });
    await waitFor(() => expect(nextWatch).toBeEnabled());
    fireEvent.click(nextWatch);
    await waitFor(() => expect(submitThreadMessage).toHaveBeenCalledTimes(3));
    await waitFor(() => expect(window.location.pathname).toBe('/ajob_watch_next'));
    const nextRequestId = vi.mocked(submitThreadMessage).mock.calls[2][2]?.requestId;
    expect(nextRequestId).toBe('watch-pr:v2:original-thread:ajob_published_next');
    expect(nextRequestId).not.toBe(firstRequestId);
    expect(vi.mocked(submitThreadMessage).mock.calls[2][1]).toMatchObject({
      require_idle: true,
      expected_latest_job_id: 'ajob_published_next',
    });
  });

  it.each([
    ['archived', { archivedAt: '2026-09-10T00:00:00Z' }, 'alice', undefined, undefined],
    ['lost native context', { nativeState: 'lost' }, 'alice', undefined, undefined],
    ['lost attempt context', { contextStatus: 'lost' }, 'alice', undefined, undefined],
    ['signed out', {}, undefined, undefined, undefined],
    ['queued', { state: 'queued' }, 'alice', undefined, undefined],
    ['running', { state: 'running' }, 'alice', undefined, undefined],
    ['a later queued turn', {}, 'alice', 'queued', undefined],
    ['a later running turn', {}, 'alice', 'running', undefined],
    [
      'pending publication',
      { workspacePublicationState: 'pending' },
      'alice',
      undefined,
      undefined,
    ],
    [
      'capturing publication',
      { workspacePublicationState: 'capturing' },
      'alice',
      undefined,
      undefined,
    ],
    [
      'captured publication',
      { workspacePublicationState: 'captured' },
      'alice',
      undefined,
      undefined,
    ],
    [
      'publishing changes',
      { workspacePublicationState: 'publishing' },
      'alice',
      undefined,
      undefined,
    ],
    ['failed publication', { workspacePublicationState: 'failed' }, 'alice', undefined, undefined],
    ['a later pending publication', {}, 'alice', 'succeeded', 'pending'],
    ['a later capturing publication', {}, 'alice', 'succeeded', 'capturing'],
    ['a later captured publication', {}, 'alice', 'succeeded', 'captured'],
    ['a later publishing turn', {}, 'alice', 'succeeded', 'publishing'],
    ['a later failed publication', {}, 'alice', 'succeeded', 'failed'],
  ] as const)(
    'disables PR watching when the conversation is %s',
    async (_reason, overrides, userId, latestState, latestPublicationState) => {
      const threadJobs: AgentJobApi[] = [];
      if (latestState) {
        threadJobs.push({
          id: 'ajob_later',
          thread_id: 'original-thread',
          turn_no: 2,
          state: latestState,
          runtime: 'claude-code',
          model: 'qwen-test',
          workspace_publication_state: latestPublicationState,
        } as AgentJobApi);
      }
      render(
        <JobDetail
          job={makeJob({
            state: 'done',
            threadId: 'original-thread',
            prUrl: 'https://github.com/owner/repository/pull/7',
            ...overrides,
          })}
          userId={userId}
          threadJobs={threadJobs}
        />,
      );
      openWorkspace();
      const git = screen.getByLabelText('Git workspace');
      fireEvent.click(within(git).getByRole('tab', { name: 'review' }));
      const watch = within(git).getByRole('button', { name: 'Watch PR and merge' });
      await act(async () => undefined);
      expect(watch).toBeDisabled();
      fireEvent.click(watch);
      expect(submitThreadMessage).not.toHaveBeenCalled();
      expect(forkAgentJob).not.toHaveBeenCalled();
    },
  );

  it('waits for the initial queue read before offering to watch a PR', async () => {
    let finishRead!: (value: Awaited<ReturnType<typeof getThreadQueue>>) => void;
    vi.mocked(getThreadQueue).mockReturnValue(
      new Promise((resolve) => {
        finishRead = resolve;
      }),
    );
    render(
      <JobDetail
        job={makeJob({
          state: 'done',
          threadId: 'original-thread',
          prUrl: 'https://github.com/owner/repository/pull/7',
        })}
        userId="alice"
      />,
    );

    const watch = screen.getByRole('button', { name: 'Watch PR and merge' });
    expect(watch).toBeDisabled();
    fireEvent.click(watch);
    expect(submitThreadMessage).not.toHaveBeenCalled();
    await act(async () => finishRead(emptyQueue('original-thread', 'ajob_1')));
    await waitFor(() => expect(watch).toBeEnabled());
  });

  it('keeps PR watching disabled when a successful queue read has no latest turn', async () => {
    vi.mocked(getThreadQueue).mockImplementation(async (threadId) => emptyQueue(threadId));
    render(
      <JobDetail
        job={makeJob({
          state: 'done',
          threadId: 'original-thread',
          prUrl: 'https://github.com/owner/repository/pull/7',
        })}
        userId="alice"
      />,
    );
    await act(async () => undefined);

    const watch = screen.getByRole('button', { name: 'Watch PR and merge' });
    expect(watch).toBeDisabled();
    fireEvent.click(watch);
    expect(submitThreadMessage).not.toHaveBeenCalled();
  });

  it.each([null, 'queued', 'running', 'publishing'])(
    'keeps PR watching disabled when the latest queue turn state is %s',
    async (latestJobState) => {
      vi.mocked(getThreadQueue).mockImplementation(async (threadId) =>
        emptyQueue(threadId, 'ajob_1', latestJobState),
      );
      render(
        <JobDetail
          job={makeJob({
            state: 'done',
            conversationState: 'idle',
            threadId: 'original-thread',
            prUrl: 'https://github.com/owner/repository/pull/7',
          })}
          userId="alice"
        />,
      );
      await act(async () => undefined);

      const watch = screen.getByRole('button', { name: 'Watch PR and merge' });
      expect(watch).toBeDisabled();
      fireEvent.click(watch);
      expect(submitThreadMessage).not.toHaveBeenCalled();
    },
  );

  it('refreshes a changed conversation after refusing the watch without retrying or disturbing the draft', async () => {
    let latestJobId = 'ajob_1';
    let latestJobState = 'succeeded';
    vi.mocked(getThreadQueue).mockImplementation(async (threadId) =>
      emptyQueue(threadId, latestJobId, latestJobState),
    );
    vi.mocked(submitThreadMessage).mockImplementation(async () => {
      latestJobId = 'ajob_newest';
      latestJobState = 'running';
      throw new APIError('conversation_changed', 'The conversation changed before watching', 409);
    });
    render(
      <JobDetail
        job={makeJob({
          state: 'done',
          conversationState: 'idle',
          threadId: 'original-thread',
          prUrl: 'https://github.com/owner/repository/pull/7',
        })}
        userId="alice"
      />,
    );
    const box = screen.getByLabelText('Add a follow-up');
    fireEvent.change(box, { target: { value: 'Keep this draft for later' } });
    const model = await screen.findByLabelText('Model for this turn');
    fireEvent.change(model, { target: { value: 'glm-test' } });
    const watch = screen.getByRole('button', { name: 'Watch PR and merge' });
    await waitFor(() => expect(watch).toBeEnabled());
    const initialReads = vi.mocked(getThreadQueue).mock.calls.length;
    fireEvent.click(watch);

    expect(await screen.findByText('The conversation changed before watching')).toBeInTheDocument();
    await waitFor(() =>
      expect(vi.mocked(getThreadQueue).mock.calls.length).toBeGreaterThan(initialReads),
    );
    expect(watch).toBeDisabled();
    expect(box).toHaveValue('Keep this draft for later');
    expect(model).toHaveValue('glm-test');
    expect(screen.queryByRole('region', { name: 'Queued messages' })).not.toBeInTheDocument();
    expect(submitThreadMessage).toHaveBeenCalledTimes(1);
    expect(submitThreadMessage).toHaveBeenCalledWith(
      'original-thread',
      {
        prompt: expect.stringContaining('https://github.com/owner/repository/pull/7'),
        require_idle: true,
        expected_latest_job_id: 'ajob_1',
      },
      { requestId: 'watch-pr:v2:original-thread:ajob_1' },
    );
    expect(forkAgentJob).not.toHaveBeenCalled();
  });

  it('retries an idle conversation queue after initial read failures before enabling PR watching', async () => {
    vi.useFakeTimers();
    try {
      let recovered = false;
      vi.mocked(getThreadQueue).mockImplementation(async (threadId) => {
        if (!recovered) throw new Error('Temporary queue outage');
        return emptyQueue(threadId, 'ajob_1');
      });
      render(
        <JobDetail
          job={makeJob({
            state: 'done',
            conversationState: 'idle',
            threadId: 'original-thread',
            prUrl: 'https://github.com/owner/repository/pull/7',
          })}
          userId="alice"
        />,
      );
      await act(async () => undefined);

      const watch = screen.getByRole('button', { name: 'Watch PR and merge' });
      const initialReads = vi.mocked(getThreadQueue).mock.calls.length;
      expect(initialReads).toBeGreaterThan(0);
      expect(watch).toBeDisabled();
      fireEvent.click(watch);
      expect(submitThreadMessage).not.toHaveBeenCalled();

      recovered = true;
      await act(async () => vi.advanceTimersByTimeAsync(IDLE_QUEUE_POLL_MS));
      expect(vi.mocked(getThreadQueue).mock.calls.length).toBeGreaterThan(initialReads);
      expect(watch).toBeEnabled();
      const verifiedReads = vi.mocked(getThreadQueue).mock.calls.length;
      await act(async () => vi.advanceTimersByTimeAsync(IDLE_QUEUE_POLL_MS));
      expect(getThreadQueue).toHaveBeenCalledTimes(verifiedReads);
    } finally {
      cleanup();
      vi.useRealTimers();
    }
  });

  it.each(['queued', 'running', 'publishing'] as const)(
    'polls a historical conversation while the latest queue turn is %s until it settles',
    async (initialState) => {
      vi.useFakeTimers();
      try {
        let latestJobState: string = initialState;
        vi.mocked(getThreadQueue).mockImplementation(async (threadId) =>
          emptyQueue(threadId, 'ajob_latest', latestJobState),
        );
        render(
          <JobDetail
            job={makeJob({
              state: 'done',
              conversationState: 'idle',
              threadId: 'original-thread',
              prUrl: 'https://github.com/owner/repository/pull/7',
            })}
            userId="alice"
          />,
        );
        await act(async () => undefined);

        const watch = screen.getByRole('button', { name: 'Watch PR and merge' });
        expect(watch).toBeDisabled();
        const initialReads = vi.mocked(getThreadQueue).mock.calls.length;
        latestJobState = 'succeeded';
        await act(async () => vi.advanceTimersByTimeAsync(IDLE_QUEUE_POLL_MS));

        expect(vi.mocked(getThreadQueue).mock.calls.length).toBeGreaterThan(initialReads);
        expect(watch).toBeEnabled();
        expect(submitThreadMessage).not.toHaveBeenCalled();
        const verifiedReads = vi.mocked(getThreadQueue).mock.calls.length;
        await act(async () => vi.advanceTimersByTimeAsync(IDLE_QUEUE_POLL_MS));
        expect(getThreadQueue).toHaveBeenCalledTimes(verifiedReads);
      } finally {
        cleanup();
        vi.useRealTimers();
      }
    },
  );

  it.each(['queued', 'held'] as const)(
    'keeps PR watching disabled while an unrelated %s message waits in a paused queue',
    async (state) => {
      let messages: PendingMessageApi[] = [
        {
          id: 'apmsg_unrelated',
          thread_id: 'original-thread',
          position: 1,
          state,
          revision: 1,
          prompt: 'Update the documentation next',
        },
      ];
      vi.mocked(getThreadQueue).mockImplementation(async (threadId) => ({
        ...emptyQueue(threadId, 'ajob_1'),
        pending_messages: messages,
        queue_paused_reason: messages.length ? 'stopped' : null,
      }));
      vi.mocked(submitThreadMessage).mockResolvedValue({
        pending: null,
        job: { id: 'ajob_watch' },
      } as never);
      render(
        <JobDetail
          job={makeJob({
            state: 'done',
            threadId: 'original-thread',
            prUrl: 'https://github.com/owner/repository/pull/7',
          })}
          userId="alice"
        />,
      );

      const queue = await screen.findByRole('region', { name: 'Queued messages' });
      expect(within(queue).getByText('Update the documentation next')).toBeInTheDocument();
      const watch = screen.getByRole('button', { name: 'Watch PR and merge' });
      expect(watch).toBeDisabled();
      fireEvent.click(watch);
      expect(submitThreadMessage).not.toHaveBeenCalled();

      messages = [];
      receiveHint('queue', 'original-thread');
      await waitFor(() => expect(watch).toBeEnabled());
      expect(screen.queryByRole('region', { name: 'Queued messages' })).not.toBeInTheDocument();
      fireEvent.click(watch);
      await waitFor(() =>
        expect(submitThreadMessage).toHaveBeenCalledWith(
          'original-thread',
          {
            prompt: expect.stringContaining('https://github.com/owner/repository/pull/7'),
            require_idle: true,
            expected_latest_job_id: 'ajob_1',
          },
          { requestId: 'watch-pr:v2:original-thread:ajob_1' },
        ),
      );
    },
  );

  it('does not resubmit a matching watch already waiting in the thread queue', async () => {
    const job = makeJob({
      state: 'done',
      threadId: 'original-thread',
      prUrl: 'https://github.com/owner/repository/pull/7',
    });
    vi.mocked(getThreadQueue).mockImplementation(async (threadId) => ({
      ...emptyQueue(threadId, 'ajob_1'),
      pending_messages: [
        {
          id: 'apmsg_existing_watch',
          thread_id: threadId,
          position: 1,
          state: 'queued',
          revision: 1,
          prompt: watchPullRequestPrompt(job),
        },
      ],
    }));
    render(<JobDetail job={job} userId="alice" />);

    await screen.findByRole('region', { name: 'Queued messages' });
    const watch = screen.getByRole('button', { name: 'Watch PR and merge' });
    expect(watch).toBeDisabled();
    fireEvent.click(watch);
    expect(submitThreadMessage).not.toHaveBeenCalled();
  });

  it('keeps the draft and its controls through a failed PR watch and a successful retry', async () => {
    const prUrl = 'https://github.com/owner/repository/pull/7';
    vi.mocked(listSkills).mockResolvedValue([
      {
        id: 7,
        name: 'Review',
        description: 'Review changes',
        revision: 2,
        source: 'created',
        source_url: null,
        source_revision: null,
        default_enabled: true,
        created_at: '',
        updated_at: '',
      },
    ]);
    vi.mocked(uploadAttachment).mockResolvedValue({
      id: 42,
      path: 'spec.txt',
      size_bytes: 12,
      content_type: 'text/plain',
    });
    vi.mocked(submitThreadMessage)
      .mockRejectedValueOnce(new Error('Could not start watching'))
      .mockResolvedValueOnce({ pending: null, job: { id: 'ajob_watch' } } as never)
      .mockResolvedValue({ pending: null, job: { id: 'ajob_draft' } } as never);
    const job = makeJob({
      state: 'done',
      threadId: 'original-thread',
      prUrl,
      skills: [{ id: 7, name: 'Review', description: 'Review changes', revision: 1 }],
    });
    const { container, rerender } = render(<JobDetail job={job} userId="alice" />);
    const box = screen.getByLabelText('Add a follow-up');
    fireEvent.change(box, { target: { value: 'Use this spec next' } });
    const model = await screen.findByLabelText('Model for this turn');
    fireEvent.change(model, { target: { value: 'glm-test' } });
    fireEvent.click(screen.getByRole('button', { name: 'Add to message' }));
    fireEvent.click(screen.getByRole('menuitem', { name: /Skills\s*1/ }));
    fireEvent.click(await screen.findByRole('switch'));
    fireEvent.click(screen.getByRole('button', { name: 'Apply' }));
    const file = new File(['requirements'], 'spec.txt');
    const input = container.querySelector<HTMLInputElement>('input[type="file"]')!;
    fireEvent.change(input, { target: { files: [file] } });
    await waitFor(() =>
      expect(screen.getByRole('button', { name: /^(Send|Queue)$/ })).toBeEnabled(),
    );

    const watch = screen.getByRole('button', { name: 'Watch PR and merge' });
    fireEvent.click(watch);
    expect(await screen.findByText('Could not start watching')).toBeInTheDocument();
    expect(watch).toBeEnabled();
    expect(box).toHaveValue('Use this spec next');
    expect(model).toHaveValue('glm-test');
    expect(screen.getByText('spec.txt')).toBeInTheDocument();
    fireEvent.click(watch);
    await waitFor(() => expect(submitThreadMessage).toHaveBeenCalledTimes(2));
    await waitFor(() => expect(window.location.pathname).toBe('/ajob_watch'));
    expect(watch).toBeDisabled();
    expect(screen.queryByText('Could not start watching')).not.toBeInTheDocument();
    expect(box).toHaveValue('Use this spec next');
    expect(model).toHaveValue('glm-test');
    expect(screen.getByText('spec.txt')).toBeInTheDocument();
    for (const [, body] of vi.mocked(submitThreadMessage).mock.calls) {
      expect(body).toEqual({
        prompt: expect.stringContaining(prUrl),
        require_idle: true,
        expected_latest_job_id: 'ajob_1',
      });
    }
    const firstRequestId = vi.mocked(submitThreadMessage).mock.calls[0][2]?.requestId;
    expect(firstRequestId).toBe('watch-pr:v2:original-thread:ajob_1');
    expect(vi.mocked(submitThreadMessage).mock.calls[1][2]?.requestId).toBe(firstRequestId);
    expect(deleteAttachment).not.toHaveBeenCalled();
    expect(forkAgentJob).not.toHaveBeenCalled();

    rerender(
      <JobDetail job={{ ...job, id: 'ajob_watch', turnNo: 2, state: 'running' }} userId="alice" />,
    );
    expect(box).toHaveValue('Use this spec next');
    expect(model).toHaveValue('glm-test');
    expect(screen.getByText('spec.txt')).toBeInTheDocument();
    expect(screen.getByRole('button', { name: /^(Send|Queue)$/ })).toBeEnabled();
    fireEvent.click(screen.getByRole('button', { name: /^(Send|Queue)$/ }));
    await waitFor(() =>
      expect(submitThreadMessage).toHaveBeenLastCalledWith('original-thread', {
        prompt: 'Use this spec next',
        model: 'glm-test',
        skill_ids: [],
        attachment_ids: [42],
      }),
    );
  });

  it('omits PR watching in both outcome and Git actions when there is no published PR', () => {
    render(<JobDetail job={makeJob({ state: 'done' })} userId="alice" />);
    expect(screen.queryByRole('button', { name: 'Watch PR and merge' })).not.toBeInTheDocument();
    openWorkspace();
    const git = screen.getByLabelText('Git workspace');
    fireEvent.click(within(git).getByRole('tab', { name: 'review' }));
    expect(screen.queryByRole('button', { name: 'Watch PR and merge' })).not.toBeInTheDocument();
  });

  it('closes all side tabs, remembers closure after refresh and can create a fresh chat', async () => {
    const { source, job, userId } = sideChatFixture();
    const props = { job, userId, threadJobs: [source] };
    const view = render(<JobDetail {...props} />);
    openWorkspace();
    addSideChat();
    await waitFor(() => expect(forkAgentJob).toHaveBeenCalledTimes(1));
    addSideChat();
    await waitFor(() => expect(forkAgentJob).toHaveBeenCalledTimes(2));
    fireEvent.click(screen.getByRole('button', { name: 'Close Side Chat 2' }));
    expect(screen.getByRole('tab', { name: 'Side Chat' })).toHaveFocus();
    fireEvent.click(screen.getByRole('button', { name: 'Close Side Chat' }));
    expect(screen.getByRole('tab', { name: /Git/ })).toHaveFocus();
    expect(screen.getByRole('tab', { name: /Git/ })).toHaveAttribute('aria-selected', 'true');
    expect(screen.queryByRole('textbox', { name: 'Message side chat' })).not.toBeInTheDocument();
    expect(localStorage.getItem(sideChatKeys(userId, source.thread_id!).session)).toContain(
      'tabs-child-1',
    );
    view.unmount();
    render(<JobDetail {...props} />);
    openWorkspace();
    expect(screen.queryByRole('tab', { name: /Side Chat/ })).not.toBeInTheDocument();
    addSideChat();
    await waitFor(() => expect(forkAgentJob).toHaveBeenCalledTimes(3));
    expect(screen.getByRole('tab', { name: 'Side Chat 3' })).toHaveAttribute(
      'aria-selected',
      'true',
    );
    expect(cancelAgentJob).not.toHaveBeenCalled();
  });

  it('preserves a pending fork receipt when its tab closes and starts a different operation for a new tab', async () => {
    const { source, job, userId, jobs } = sideChatFixture();
    let resolve!: (job: AgentJobApi) => void;
    vi.mocked(forkAgentJob).mockImplementationOnce(
      () =>
        new Promise((done) => {
          resolve = done;
        }),
    );
    render(<JobDetail job={job} userId={userId} threadJobs={[source]} />);
    openWorkspace();
    addSideChat();
    await waitFor(() => expect(forkAgentJob).toHaveBeenCalledTimes(1));
    fireEvent.click(screen.getByRole('button', { name: 'Close Side Chat' }));
    const child = {
      ...source,
      id: 'closed-child',
      thread_id: 'closed-thread',
      forked_from_job_id: source.id,
      fork_anchor_job_id: source.id,
    };
    jobs.set(child.id, child);
    await act(async () => resolve(child));
    expect(localStorage.getItem(sideChatKeys(userId, source.thread_id!).session)).toContain(
      'closed-child',
    );
    addSideChat();
    await waitFor(() => expect(forkAgentJob).toHaveBeenCalledTimes(2));
    expect(vi.mocked(forkAgentJob).mock.calls[0][1]?.requestId).not.toEqual(
      vi.mocked(forkAgentJob).mock.calls[1][1]?.requestId,
    );
    expect(cancelAgentJob).not.toHaveBeenCalled();
  });

  it('creates and focuses a side chat from one click on + without stopping the main conversation', async () => {
    render(<JobDetail job={makeJob()} />);
    openWorkspace();
    expect(screen.queryByRole('tab', { name: /Side Chat/ })).not.toBeInTheDocument();
    addSideChat();
    expect(screen.queryByRole('menu')).not.toBeInTheDocument();
    const tab = screen.getByRole('tab', { name: 'Side Chat' });
    expect(tab).toHaveFocus();
    expect(tab).toHaveAttribute('aria-selected', 'true');
    expect(cancelAgentJob).not.toHaveBeenCalled();
  });

  it('stops only the selected new side chat when Escape is pressed on its tab', async () => {
    const { source, job, userId, jobs } = sideChatFixture();
    vi.mocked(submitThreadMessage).mockImplementationOnce(async (threadId, body) => {
      const latest = [...jobs.values()].filter((row) => row.thread_id === threadId).at(-1)!;
      const next = {
        ...latest,
        id: 'running-side',
        forked_from_job_id: null,
        turn_no: 2,
        task_prompt: body.prompt,
        state: 'running' as const,
        conversation_state: 'running' as const,
      };
      jobs.set(next.id, next);
      return { pending: null, job: next };
    });
    render(<JobDetail job={{ ...job, state: 'running' }} userId={userId} threadJobs={[source]} />);
    openWorkspace();
    addSideChat();
    fireEvent.change(screen.getByRole('textbox', { name: 'Message side chat' }), {
      target: { value: 'Run in side chat' },
    });
    await waitFor(() =>
      expect(screen.getByRole('button', { name: 'Send side chat message' })).toBeEnabled(),
    );
    fireEvent.click(screen.getByRole('button', { name: 'Send side chat message' }));
    await screen.findByRole('button', { name: 'Stop side chat' });
    fireEvent.keyDown(screen.getByRole('tab', { name: 'Side Chat' }), { key: 'Escape' });
    await waitFor(() => expect(cancelAgentJob).toHaveBeenCalledExactlyOnceWith('running-side'));
  });

  it('lazily prepares a real side session and isolates Escape on the tab button', async () => {
    installSideChatLocks();
    const source = {
      id: 'real-main',
      thread_id: 'main-contract',
      sandbox_id: 'group',
      workspace_shared: true,
      native_state: 'ready',
      native_fork_available: true,
      turn_no: 1,
      state: 'succeeded',
      conversation_state: 'idle',
      task_prompt: 'Main',
      runtime: 'codex',
      model: 'qwen-test',
      repo: 'owner/repository',
      current_attempt_id: null,
    } as AgentJobApi;
    const child = {
      ...source,
      id: 'real-side',
      thread_id: 'child-contract',
      forked_from_job_id: source.id,
      fork_anchor_job_id: source.id,
    };
    vi.mocked(getAgentJob).mockImplementation(async (id) => (id === source.id ? source : child));
    vi.mocked(getAgentJobThread).mockResolvedValue({
      thread_id: child.thread_id,
      jobs: [child],
      messages: [],
    });
    vi.mocked(listAllAgentThreadEvents).mockResolvedValue([]);
    vi.mocked(forkAgentJob).mockResolvedValue(child);
    const job = makeJob({
      id: source.id,
      state: 'running',
      threadId: source.thread_id!,
      sandboxId: source.sandbox_id!,
      workspaceShared: true,
    });
    render(<JobDetail job={job} userId="integration-owner" threadJobs={[source]} />);
    expect(getAgentJob).not.toHaveBeenCalled();
    openWorkspace();
    expect(forkAgentJob).not.toHaveBeenCalled();
    addSideChat();
    const tab = screen.getByRole('tab', { name: 'Side Chat' });
    await waitFor(() => expect(forkAgentJob).toHaveBeenCalledTimes(1));
    fireEvent.keyDown(tab, { key: 'Escape' });
    fireEvent.keyDown(document.body, { key: 'Escape' });
    expect(cancelAgentJob).not.toHaveBeenCalled();
    fireEvent.click(screen.getByRole('tab', { name: 'Files' }));
    fireEvent.click(tab);
    expect(forkAgentJob).toHaveBeenCalledTimes(1);
  });

  it('keeps Side Chat in the existing workspace without handling its Escape as a main stop', async () => {
    render(<JobDetail job={makeJob()} />);
    openWorkspace();
    const workspace = screen.getByRole('region', { name: 'Job workspace' });
    const names = () =>
      within(within(workspace).getByRole('tablist', { name: 'Workspace views' }))
        .getAllByRole('tab')
        .map((tab) => tab.textContent?.replace(/\d+$/, '').trim());
    expect(names()).toEqual(['Git', 'Terminal', 'Files']);
    addSideChat();
    expect(names()).toEqual(['Git', 'Terminal', 'Files', 'Side Chat']);
    expect(screen.getByRole('textbox', { name: 'Message side chat' })).toBeVisible();
    fireEvent.keyDown(screen.getByRole('region', { name: 'Side chat messages' }), {
      key: 'Escape',
    });
    expect(cancelAgentJob).not.toHaveBeenCalled();
    fireEvent.click(within(workspace).getByRole('tab', { name: /^Git/ }));
    expect(screen.queryByRole('textbox', { name: 'Message side chat' })).not.toBeInTheDocument();
    expect(forkAgentJob).not.toHaveBeenCalled();
  });

  it('approves a tool call directly in the conversation without a Tools workspace tab', async () => {
    const invocation: McpInvocationApi = {
      id: 11,
      job_id: 'ajob_1',
      thread_id: 'thread-1',
      connection_id: 3,
      connection_label: 'Issue tracker',
      capability_name: 'get_issue',
      capability_kind: 'tool',
      capability_description: null,
      capability_annotations: {},
      status: 'pending',
      arguments_digest: 'digest',
      arguments: { number: 7 },
      created_at: null,
      expires_at: null,
      decided_at: null,
      result_summary: null,
      guidance: '',
    };
    vi.mocked(listMcpInvocations).mockResolvedValue({ invocations: [invocation] });
    vi.mocked(decideMcpInvocation).mockImplementation(async () => {
      const approved = { ...invocation, status: 'approved' as const };
      vi.mocked(listMcpInvocations).mockResolvedValue({ invocations: [approved] });
      return approved;
    });
    render(<JobDetail job={makeJob()} />);
    expect(await screen.findByText('Issue tracker wants to call get_issue')).toBeVisible();
    expect(screen.queryByRole('button', { name: 'Review tools' })).not.toBeInTheDocument();
    expect(screen.queryByRole('tab', { name: 'Tools', hidden: true })).not.toBeInTheDocument();
    expect(screen.queryByText('MCP servers')).not.toBeInTheDocument();
    fireEvent.click(screen.getByRole('button', { name: 'Approve' }));
    await waitFor(() => expect(decideMcpInvocation).toHaveBeenCalledWith(11, true));
    expect(await screen.findByText('get_issue · approved')).toBeVisible();
    expect(submitThreadMessage).not.toHaveBeenCalled();
  });
  it.each([undefined, 'previous-job'])(
    'uses the same queue message with parent %s',
    async (parentJobId) => {
      render(<JobDetail job={makeJob({ state: 'queued', events: [], parentJobId })} />);
      expect(await screen.findByText('Waiting to start')).toBeInTheDocument();
      expect(screen.queryByText(/Queued after (the )?current run/)).not.toBeInTheDocument();
    },
  );

  beforeEach(() => {
    vi.mocked(listThreadQuestions)
      .mockReset()
      .mockResolvedValue({ questions: [], job_id: 'ajob_1', attempt_id: 1 });
    vi.mocked(getAgentJob).mockReset();
    vi.mocked(getAgentJobThread).mockReset();
    vi.mocked(listAllAgentThreadEvents).mockReset();
    vi.mocked(listMcpInvocations).mockReset().mockResolvedValue({ invocations: [] });
    vi.mocked(getMcpThreadBindings).mockReset().mockResolvedValue({ bindings: [] });
    vi.mocked(updateMcpThreadBinding).mockReset();
    vi.mocked(uploadAttachment).mockReset();
    vi.mocked(deleteAttachment).mockClear();
    navigation.push.mockReset();
    vi.mocked(cancelAgentJob).mockReset();
    vi.mocked(submitThreadMessage).mockReset();
    vi.mocked(sendPendingMessageNow).mockReset();
    vi.mocked(getThreadQueue)
      .mockReset()
      .mockImplementation(async (threadId) => emptyQueue(threadId, 'ajob_1'));
    vi.mocked(forkAgentJob).mockReset();
    vi.mocked(restoreAgentJob).mockReset();
    vi.mocked(renameAgentJob).mockReset();
    vi.mocked(pauseAgentGoal).mockReset();
    vi.mocked(setAgentGoal).mockReset();
    vi.mocked(getAgentGoal).mockReset();
    vi.mocked(resumeAgentGoal).mockReset();
    vi.mocked(clearAgentGoal).mockReset();
    vi.mocked(pinAgentJob).mockReset();
    vi.mocked(unpinAgentJob).mockReset();
    vi.mocked(getAgentConfig).mockReset();
    vi.mocked(getAgentConfig).mockResolvedValue(AGENT_CONFIG);
    vi.mocked(listAgentConversations).mockReset();
    vi.mocked(getAgentJobFiles).mockReset();
    vi.mocked(getAgentJobFiles).mockResolvedValue({
      path: '',
      kind: 'directory',
      entries: [],
    });
    vi.mocked(getAgentJobGit).mockReset();
    vi.mocked(getAgentJobGit).mockResolvedValue({
      available: false,
      branch: '',
      changes: [],
      patch: '',
      commits: [],
    });
    // Absent by default: the stats bar is additive, and every case here
    // predates it. The bar's own file covers what it renders when present.
    vi.mocked(getAgentThreadStats).mockReset().mockResolvedValue(null);
    vi.mocked(getAgentJobUsage).mockReset().mockResolvedValue(null);
    vi.mocked(refreshAgentJobUsage).mockReset().mockResolvedValue(null);
    vi.mocked(createAgentTerminal).mockReset();
    vi.mocked(deleteAgentTerminal).mockReset();
    vi.mocked(listAgentTerminals).mockReset().mockResolvedValue([]);
    vi.mocked(resizeAgentTerminal).mockReset();
    vi.mocked(streamAgentTerminal).mockReset();
    vi.mocked(writeAgentTerminalInput).mockReset();
    vi.mocked(writeAgentJobFile).mockReset();
    sessionStorage.clear();
    localStorage.clear();
  });

  afterEach(() => cleanup());

  it('shows the latest turn selection when following up from an older URL', () => {
    render(
      <JobDetail
        job={makeJob({
          skills: [{ id: 7, name: 'Review', description: 'Review changes', revision: 1 }],
        })}
        threadJobs={[
          {
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
            state: 'queued',
            task_prompt: 'Later',
            runtime: 'claude-code',
            model: 'qwen-test',
            skills: [],
          },
        ]}
      />,
    );
    // The size is still the assertion: this job carries one skill and the
    // latest turn carries none, so a "1" here would mean the picker had gone
    // back to reading the URL's turn instead of the conversation's newest one.
    // (The control moved into the attach menu; what it reports did not change.)
    fireEvent.click(screen.getByRole('button', { name: 'Add to message' }));
    expect(screen.getByRole('menuitem', { name: /Skills\s*0/ })).toBeInTheDocument();
  });

  it('allows clearing saved skills for the next follow-up', async () => {
    vi.mocked(submitThreadMessage).mockResolvedValue({
      pending: null,
      job: { id: 'ajob_child' },
    } as never);
    vi.mocked(listSkills).mockResolvedValue([
      {
        id: 7,
        name: 'Review',
        description: 'Review changes',
        revision: 2,
        source: 'created',
        source_url: null,
        source_revision: null,
        default_enabled: true,
        created_at: '',
        updated_at: '',
      },
    ]);
    render(
      <JobDetail
        job={makeJob({
          skills: [{ id: 7, name: 'Review', description: 'Review changes', revision: 1 }],
        })}
      />,
    );
    fireEvent.click(screen.getByRole('button', { name: 'Add to message' }));
    fireEvent.click(screen.getByRole('menuitem', { name: /Skills\s*1/ }));
    fireEvent.click(await screen.findByRole('switch'));
    fireEvent.click(screen.getByRole('button', { name: 'Apply' }));
    fireEvent.change(screen.getByLabelText('Add a follow-up'), {
      target: { value: 'Continue without skills' },
    });
    fireEvent.keyDown(screen.getByLabelText('Add a follow-up'), { key: 'Enter' });
    await waitFor(() =>
      expect(submitThreadMessage).toHaveBeenCalledWith('ajob_1', {
        prompt: 'Continue without skills',
        skill_ids: [],
      }),
    );
  });

  it('puts the goal on the conversation, never on a message', async () => {
    // A goal is not something a message carries any more: `/goal <objective>`
    // writes it to the thread, and the next message is an ordinary one. The old
    // draft rode every follow-up after it — including one sent after the owner
    // paused the goal, which the write would silently reactivate.
    vi.mocked(submitThreadMessage).mockResolvedValue({
      pending: null,
      job: { id: 'ajob_child' },
    } as never);
    vi.mocked(setAgentGoal).mockResolvedValue({ thread_id: 'thread_9', goal: null, revision: 5 });
    render(
      <JobDetail
        job={makeJob({
          threadId: 'thread_9',
          goal: {
            objective: 'make `pytest` pass',
            state: 'active',
            revision: 4,
            roundsUsed: 2,
            maxRounds: null,
          },
        })}
      />,
    );
    const box = await screen.findByLabelText('Add a follow-up');

    fireEvent.click(screen.getByRole('button', { name: 'Add to message' }));
    fireEvent.click(within(screen.getByRole('menu')).getByRole('menuitem', { name: /Goal/ }));
    // The row starts a command in the box; it opens nothing.
    expect(box).toHaveValue('/goal ');

    fireEvent.change(box, { target: { value: '/goal make the suite pass' } });
    fireEvent.keyDown(box, { key: 'Enter' });
    await waitFor(() =>
      expect(setAgentGoal).toHaveBeenCalledWith('thread_9', {
        goal: { objective: 'make the suite pass' },
        revision: 4,
      }),
    );
    expect(submitThreadMessage).not.toHaveBeenCalled();

    fireEvent.change(box, { target: { value: 'first' } });
    fireEvent.keyDown(box, { key: 'Enter' });
    await waitFor(() => expect(submitThreadMessage).toHaveBeenCalledTimes(1));
    expect(vi.mocked(submitThreadMessage).mock.calls[0][1]).not.toHaveProperty('goal');
  });

  it('manages the goal through its conversation, not through the turn on screen', async () => {
    // A goal belongs to the conversation, and every route that touches it names
    // the thread. The turn in the URL is not that identity: a conversation
    // whose newest turn has been deleted still has a goal.
    vi.mocked(pauseAgentGoal).mockResolvedValue({
      thread_id: 'thread_9',
      goal: null,
      revision: 2,
    });
    render(
      <JobDetail
        job={makeJob({
          threadId: 'thread_9',
          goal: {
            objective: 'make `pytest` pass',
            state: 'active',
            revision: 1,
            roundsUsed: 2,
            maxRounds: 10,
          },
        })}
      />,
    );

    fireEvent.click(screen.getByRole('button', { name: 'Pause' }));
    await waitFor(() => expect(pauseAgentGoal).toHaveBeenCalledWith('thread_9'));
    expect(vi.mocked(pauseAgentGoal).mock.calls[0][0]).not.toBe('ajob_1');
  });

  it('opens the shared plus menu from the follow-up and restores keyboard focus', () => {
    render(<JobDetail job={makeJob({ state: 'done' })} />);
    const plus = screen.getByRole('button', { name: 'Add to message' });
    expect(screen.queryByRole('button', { name: /Skills for this conversation/ })).toBeNull();
    fireEvent.keyDown(plus, { key: 'ArrowDown' });
    expect(screen.getByRole('menuitem', { name: 'Upload files' })).toHaveFocus();
    expect(screen.getByRole('menuitem', { name: /Skills\s*0/ })).toBeInTheDocument();
    expect(screen.getByRole('menuitem', { name: /MCP/ })).toBeInTheDocument();
    fireEvent.keyDown(screen.getByRole('menu'), { key: 'Escape' });
    expect(plus).toHaveFocus();
    expect(screen.queryByRole('menu')).toBeNull();
  });

  it('waits for uploads, keeps them after a failed send, then clears them after success', async () => {
    let finishUpload!: (value: Awaited<ReturnType<typeof uploadAttachment>>) => void;
    vi.mocked(uploadAttachment).mockReturnValue(
      new Promise((resolve) => {
        finishUpload = resolve;
      }),
    );
    vi.mocked(submitThreadMessage)
      .mockRejectedValueOnce(new Error('Try sending again'))
      .mockResolvedValue({ pending: null, job: { id: 'ajob_child' } } as never);
    const { container } = render(<JobDetail job={makeJob()} />);
    const box = screen.getByLabelText('Add a follow-up');
    fireEvent.change(box, { target: { value: 'Use this spec' } });
    const file = new File(['requirements'], 'spec.txt');
    fireEvent.click(screen.getByRole('button', { name: 'Add to message' }));
    const input = container.querySelector<HTMLInputElement>('input[type="file"]')!;
    const pick = vi.spyOn(input, 'click');
    fireEvent.click(screen.getByRole('menuitem', { name: 'Upload files' }));
    expect(pick).toHaveBeenCalledOnce();
    fireEvent.change(input, { target: { files: [file] } });
    expect(uploadAttachment).toHaveBeenCalledWith(file, 'spec.txt');
    expect(screen.getByText('spec.txt')).toBeInTheDocument();
    expect(screen.getByRole('button', { name: /^(Send|Queue)$/ })).toBeDisabled();
    fireEvent.keyDown(box, { key: 'Enter' });
    expect(submitThreadMessage).not.toHaveBeenCalled();
    await act(async () =>
      finishUpload({ id: 42, path: 'spec.txt', size_bytes: 12, content_type: 'text/plain' }),
    );
    fireEvent.keyDown(box, { key: 'Enter' });
    expect(await screen.findByText('Try sending again')).toBeInTheDocument();
    expect(screen.getByText('spec.txt')).toBeInTheDocument();
    expect(box).toHaveValue('Use this spec');
    fireEvent.click(screen.getByRole('button', { name: /^(Send|Queue)$/ }));
    await waitFor(() => expect(box).toHaveValue(''));
    expect(submitThreadMessage).toHaveBeenLastCalledWith('ajob_1', {
      prompt: 'Use this spec',
      attachment_ids: [42],
    });
    expect(screen.queryByText('spec.txt')).toBeNull();
    expect(deleteAttachment).not.toHaveBeenCalled();
    fireEvent.change(box, { target: { value: 'Next message' } });
    fireEvent.keyDown(box, { key: 'Enter' });
    await waitFor(() =>
      expect(submitThreadMessage).toHaveBeenLastCalledWith('ajob_1', { prompt: 'Next message' }),
    );
  });

  it('pastes clipboard file images and waits for their uploads before sending a follow-up', async () => {
    let finishUpload!: (value: Awaited<ReturnType<typeof uploadAttachment>>) => void;
    vi.mocked(uploadAttachment).mockReturnValue(
      new Promise((resolve) => {
        finishUpload = resolve;
      }),
    );
    vi.mocked(submitThreadMessage).mockResolvedValue({
      pending: null,
      job: { id: 'ajob_child' },
    } as never);
    render(<JobDetail job={makeJob()} />);
    const box = screen.getByLabelText('Add a follow-up');
    fireEvent.change(box, { target: { value: 'Use this screenshot' } });
    const image = new File(['pixels'], 'image.png', { type: 'image/png' });
    const event = createEvent.paste(box, {
      clipboardData: {
        items: [],
        files: [image, new File(['notes'], 'notes.txt', { type: 'text/plain' })],
      },
    });
    fireEvent(box, event);

    expect(event.defaultPrevented).toBe(true);
    expect(uploadAttachment).toHaveBeenCalledOnce();
    const [uploaded, path] = vi.mocked(uploadAttachment).mock.calls[0];
    expect(uploaded).toEqual(
      expect.objectContaining({ name: path, type: 'image/png', size: image.size }),
    );
    expect(path).toMatch(/^pasted-image-[a-f0-9-]+\.png$/);
    expect(screen.getByText(path!)).toBeInTheDocument();
    expect(box).toHaveValue('Use this screenshot');
    expect(screen.getByRole('button', { name: 'Queue' })).toBeDisabled();
    fireEvent.keyDown(box, { key: 'Enter' });
    expect(submitThreadMessage).not.toHaveBeenCalled();

    await act(async () => {
      finishUpload({ id: 43, path: path!, size_bytes: image.size, content_type: image.type });
    });
    fireEvent.click(screen.getByRole('button', { name: 'Queue' }));
    await waitFor(() =>
      expect(submitThreadMessage).toHaveBeenCalledWith('ajob_1', {
        prompt: 'Use this screenshot',
        attachment_ids: [43],
      }),
    );
    await waitFor(() => expect(box).toHaveValue(''));
    expect(screen.queryByText(path!)).not.toBeInTheDocument();
    expect(deleteAttachment).not.toHaveBeenCalled();
  });

  it('leaves plain text follow-up pastes to the browser', () => {
    render(<JobDetail job={makeJob()} />);
    const box = screen.getByLabelText('Add a follow-up');
    fireEvent.change(box, { target: { value: 'Existing follow-up' } });
    const event = createEvent.paste(box, {
      clipboardData: {
        items: [{ kind: 'string', type: 'text/plain', getAsFile: () => null }],
        files: [],
        getData: () => 'Pasted text',
      },
    });
    fireEvent(box, event);
    expect(event.defaultPrevented).toBe(false);
    expect(uploadAttachment).not.toHaveBeenCalled();
    expect(box).toHaveValue('Existing follow-up');
  });

  it('does not upload pasted images after native context is lost', () => {
    render(<JobDetail job={makeJob({ state: 'cancelled', contextStatus: 'lost' })} />);
    const box = screen.getByLabelText('Add a follow-up');
    expect(box).toBeDisabled();
    fireEvent.paste(box, {
      clipboardData: {
        items: [],
        files: [new File(['pixels'], 'image.png', { type: 'image/png' })],
      },
    });
    expect(uploadAttachment).not.toHaveBeenCalled();
  });

  it('does not upload pasted images while a follow-up is being submitted', async () => {
    vi.mocked(submitThreadMessage).mockReturnValue(new Promise(() => {}));
    render(<JobDetail job={makeJob()} />);
    const box = screen.getByLabelText('Add a follow-up');
    fireEvent.change(box, { target: { value: 'Send the follow-up' } });
    fireEvent.click(screen.getByRole('button', { name: 'Queue' }));
    await waitFor(() => expect(submitThreadMessage).toHaveBeenCalledOnce());
    fireEvent.paste(box, {
      clipboardData: {
        items: [],
        files: [new File(['pixels'], 'image.png', { type: 'image/png' })],
      },
    });
    expect(uploadAttachment).not.toHaveBeenCalled();
  });

  it('accepts dropped files and removes them before sending', async () => {
    vi.mocked(uploadAttachment).mockResolvedValue({
      id: 12,
      path: 'notes.txt',
      size_bytes: 4,
      content_type: 'text/plain',
    });
    vi.mocked(submitThreadMessage).mockResolvedValue({
      pending: null,
      job: { id: 'ajob_child' },
    } as never);
    render(<JobDetail job={makeJob()} />);
    const box = screen.getByLabelText('Add a follow-up');
    const file = new File(['data'], 'notes.txt');
    fireEvent.drop(box.closest('form')!, {
      dataTransfer: { files: [file], items: [], types: ['Files'] },
    });
    await waitFor(() => expect(uploadAttachment).toHaveBeenCalledWith(file, 'notes.txt'));
    fireEvent.click(await screen.findByRole('button', { name: 'Remove notes.txt' }));
    await waitFor(() => expect(deleteAttachment).toHaveBeenCalledWith(12));
    fireEvent.change(box, { target: { value: 'Without the file' } });
    fireEvent.keyDown(box, { key: 'Enter' });
    await waitFor(() =>
      expect(submitThreadMessage).toHaveBeenCalledWith('ajob_1', { prompt: 'Without the file' }),
    );
  });

  it('uses current thread MCP bindings and preserves permissions when switching a server off', async () => {
    const binding: McpBindingApi = {
      connection_id: 7,
      connection_label: 'Notion',
      alias: 'notes',
      enabled: true,
      selection_pending: false,
      available: true,
      error: null,
      capabilities: [
        { kind: 'tool', name: 'search', description: '', input_schema: {}, annotations: {} },
      ],
      tools: { search: 'allow' },
      resources: {},
      prompts: {},
      config_revision: 1,
      authorization_epoch: 1,
      capability_revision: 1,
      policy_revision: 1,
    };
    vi.mocked(getMcpThreadBindings).mockResolvedValue({ bindings: [binding] });
    let finishSave!: (value: { bindings: McpBindingApi[] }) => void;
    vi.mocked(updateMcpThreadBinding).mockReturnValue(
      new Promise((resolve) => {
        finishSave = resolve;
      }),
    );
    render(<JobDetail job={makeJob({ threadId: 'thread-existing' })} />);
    fireEvent.change(screen.getByLabelText('Add a follow-up'), { target: { value: 'Continue' } });
    fireEvent.click(screen.getByRole('button', { name: 'Add to message' }));
    fireEvent.click(screen.getByRole('menuitem', { name: /MCP/ }));
    const toggle = await screen.findByRole('switch', { name: 'Notion' });
    expect(toggle).toHaveAttribute('aria-checked', 'true');
    expect(getMcpThreadBindings).toHaveBeenCalledWith('thread-existing');
    fireEvent.click(toggle);
    expect(updateMcpThreadBinding).toHaveBeenCalledWith('thread-existing', {
      connection_id: 7,
      alias: 'notes',
      enabled: false,
      use_defaults: false,
      tools: { search: 'allow' },
      resources: {},
      prompts: {},
    });
    expect(screen.getByRole('button', { name: /^(Send|Queue)$/ })).toBeDisabled();
    await act(async () => finishSave({ bindings: [{ ...binding, enabled: false }] }));
    expect(toggle).toHaveAttribute('aria-checked', 'false');
    expect(screen.getByRole('button', { name: /^(Send|Queue)$/ })).toBeEnabled();
  });

  it('replaces duplicate header metadata with a keyboard-accessible task menu', () => {
    render(<JobDetail job={makeJob({ turnNo: 3 })} />);
    const header = screen.getByRole('heading', { level: 1 }).closest('header')!;
    expect(within(header).queryByText(/turn 3/)).not.toBeInTheDocument();
    expect(within(header).queryByText('Run details')).not.toBeInTheDocument();
    const trigger = screen.getByRole('button', { name: 'Task actions' });
    fireEvent.click(trigger);
    expect(screen.queryByRole('menuitem', { name: 'Run details' })).not.toBeInTheDocument();
    expect(screen.getByRole('menuitem', { name: 'Rename' })).toHaveFocus();
    fireEvent.keyDown(document.activeElement!, { key: 'ArrowDown' });
    expect(screen.getByRole('menuitem', { name: 'Pin' })).toHaveFocus();
    expect(screen.getByRole('menuitem', { name: 'Fork' })).toBeDisabled();
    fireEvent.keyDown(document.activeElement!, { key: 'Escape' });
    expect(screen.queryByRole('menu')).not.toBeInTheDocument();
    expect(trigger).toHaveFocus();
    expect(cancelAgentJob).not.toHaveBeenCalled();
    fireEvent.click(trigger);
    fireEvent.pointerDown(document.body);
    expect(screen.queryByRole('menu')).not.toBeInTheDocument();
  });

  it('renames persistently while preserving the prompt and keeps failures editable', async () => {
    vi.mocked(renameAgentJob).mockRejectedValueOnce(new Error('Rename unavailable'));
    vi.mocked(renameAgentJob).mockResolvedValueOnce({ thread_id: 'thread-1', title: 'New title' });
    render(<JobDetail job={makeJob()} />);
    fireEvent.click(screen.getByRole('button', { name: 'Task actions' }));
    fireEvent.click(screen.getByRole('menuitem', { name: 'Rename' }));
    const input = screen.getByRole('textbox', { name: 'Task name' });
    expect(input).toHaveFocus();
    expect(input).toHaveAttribute('maxlength', '160');
    fireEvent.change(input, { target: { value: '   ' } });
    expect(screen.getByRole('button', { name: 'Save' })).toBeDisabled();
    fireEvent.change(input, { target: { value: ' New title ' } });
    fireEvent.click(screen.getByRole('button', { name: 'Save' }));
    expect(await screen.findByRole('alert')).toHaveTextContent('Rename unavailable');
    expect(screen.getByRole('heading', { level: 1 })).toHaveTextContent('Current task');
    fireEvent.click(screen.getByRole('button', { name: 'Save' }));
    await waitFor(() => expect(screen.queryByRole('dialog')).not.toBeInTheDocument());
    expect(renameAgentJob).toHaveBeenLastCalledWith('ajob_1', 'New title');
    expect(screen.getByRole('heading', { level: 1 })).toHaveTextContent('New title');
    expect(screen.getByText(/with all of its detail/)).toBeInTheDocument();
  });

  it('dismisses rename without stopping a live run and traps focus inside the dialog', () => {
    render(<JobDetail job={makeJob()} />);
    const trigger = screen.getByRole('button', { name: 'Task actions' });
    fireEvent.click(trigger);
    fireEvent.click(screen.getByRole('menuitem', { name: 'Rename' }));
    const input = screen.getByRole('textbox', { name: 'Task name' });
    fireEvent.keyDown(input, { key: 'Tab', shiftKey: true });
    expect(screen.getByRole('button', { name: 'Save' })).toHaveFocus();
    fireEvent.keyDown(document.activeElement!, { key: 'Escape' });
    expect(screen.queryByRole('dialog')).not.toBeInTheDocument();
    expect(trigger).toHaveFocus();
    expect(cancelAgentJob).not.toHaveBeenCalled();
    expect(renameAgentJob).not.toHaveBeenCalled();
  });

  it('pins and unpins the thread and follows sidebar pin changes', async () => {
    vi.mocked(pinAgentJob).mockResolvedValue({
      thread_id: 'thread-1',
      pinned: true,
      pinned_at: '2026-09-10',
    });
    vi.mocked(unpinAgentJob).mockResolvedValue({
      thread_id: 'thread-1',
      pinned: false,
      pinned_at: null,
    });
    render(<JobDetail job={makeJob({ threadId: 'thread-1' })} />);
    const trigger = screen.getByRole('button', { name: 'Task actions' });
    fireEvent.click(trigger);
    fireEvent.click(screen.getByRole('menuitem', { name: 'Pin' }));
    await waitFor(() => expect(screen.queryByRole('menu')).not.toBeInTheDocument());
    expect(pinAgentJob).toHaveBeenCalledWith('ajob_1');
    fireEvent.click(trigger);
    fireEvent.click(screen.getByRole('menuitem', { name: 'Unpin' }));
    await waitFor(() => expect(screen.queryByRole('menu')).not.toBeInTheDocument());
    expect(unpinAgentJob).toHaveBeenCalledWith('ajob_1');
    act(() => announceAgentSessionsChanged({ threadIds: ['thread-1'], pinnedAt: '2026-09-10' }));
    fireEvent.click(trigger);
    expect(screen.getByRole('menuitem', { name: 'Unpin' })).toBeInTheDocument();
  });

  it('uses the native fork action from the task menu', async () => {
    vi.spyOn(globalThis, 'confirm').mockReturnValue(true);
    vi.mocked(forkAgentJob).mockResolvedValue({ id: 'ajob_fork' } as never);
    render(
      <JobDetail
        job={makeJob({
          state: 'done',
          sandboxId: 'sandbox',
          workspaceShared: true,
          nativeForkAvailable: true,
        })}
      />,
    );
    fireEvent.click(screen.getByRole('button', { name: 'Task actions' }));
    fireEvent.click(screen.getByRole('menuitem', { name: 'Fork' }));
    await waitFor(() => expect(forkAgentJob).toHaveBeenCalledWith('ajob_1'));
    expect(navigation.push).toHaveBeenCalledWith('/ajob_fork');
  });

  it('keeps an archived conversation readable but requires unarchive before continuing', async () => {
    const onReload = vi.fn();
    const archived = makeJob({
      state: 'done',
      archivedAt: '2026-09-10T00:00:00Z',
      sandboxId: 'sandbox',
      workspaceShared: true,
      nativeForkAvailable: true,
    });
    vi.mocked(restoreAgentJob).mockResolvedValue({
      thread_id: 'thread-1',
      archived: false,
      archived_at: null,
    });
    const { rerender } = render(<JobDetail job={archived} onReload={onReload} />);
    expect(screen.getByText('I found the', { exact: false })).toBeInTheDocument();
    expect(screen.queryByRole('textbox', { name: 'Add a follow-up' })).not.toBeInTheDocument();
    expect(screen.queryByRole('button', { name: /Fork from here/ })).not.toBeInTheDocument();
    expect(screen.getByRole('link', { name: 'Archived sessions' })).toHaveAttribute(
      'href',
      '/archived',
    );
    openWorkspace();
    fireEvent.click(screen.getByRole('tab', { name: 'Terminal' }));
    expect(screen.getByText(/Unarchive this session to use its terminal/)).toBeInTheDocument();
    expect(listAgentTerminals).not.toHaveBeenCalled();
    fireEvent.click(screen.getByRole('button', { name: 'Unarchive and continue' }));
    await waitFor(() => expect(onReload).toHaveBeenCalledOnce());
    expect(restoreAgentJob).toHaveBeenCalledWith('ajob_1');
    // Only the refreshed server state reopens the composer.
    expect(screen.queryByRole('textbox', { name: 'Add a follow-up' })).not.toBeInTheDocument();
    rerender(<JobDetail job={{ ...archived, archivedAt: null }} onReload={onReload} />);
    expect(screen.getByRole('textbox', { name: 'Add a follow-up' })).toBeEnabled();
  });

  it.each([
    ['Connection lost', 'Connection lost'],
    [
      'native context unavailable',
      'The connection was interrupted before this response could be confirmed.',
    ],
  ])('keeps the archived view intact if unarchive fails: %s', async (detail, message) => {
    vi.mocked(restoreAgentJob).mockRejectedValue(new Error(detail));
    const onReload = vi.fn();
    render(<JobDetail job={makeJob({ archivedAt: '2026-09-10T00:00:00Z' })} onReload={onReload} />);
    expect(screen.getByText(/Archiving does not stop it/)).toBeInTheDocument();
    fireEvent.click(screen.getByRole('button', { name: 'Unarchive and continue' }));
    expect(await screen.findByRole('alert')).toHaveTextContent(message);
    expect(onReload).not.toHaveBeenCalled();
    expect(screen.queryByRole('textbox', { name: 'Add a follow-up' })).not.toBeInTheDocument();
    expect(cancelAgentJob).not.toHaveBeenCalled();
  });

  it('does not promise continuation when an archived session has lost native context', () => {
    const archived = makeJob({
      state: 'failed',
      archivedAt: '2026-09-10T00:00:00Z',
      nativeState: 'lost',
    });
    const { rerender } = render(<JobDetail job={archived} />);
    expect(screen.getByRole('button', { name: 'Unarchive' })).toBeInTheDocument();
    expect(screen.getByText(/we couldn’t reconnect it/)).toBeInTheDocument();
    rerender(<JobDetail job={{ ...archived, archivedAt: null }} />);
    expect(screen.getByRole('textbox', { name: 'Add a follow-up' })).toBeDisabled();
  });

  it('shows live agent activity while reading either chat or trajectory', () => {
    const job = makeJob({ liveNote: 'Running tool call' });
    const { rerender } = render(<JobDetail job={job} />);

    expect(screen.getByRole('status', { name: 'Agent status' })).toHaveTextContent(
      'Running tool call',
    );
    fireEvent.click(screen.getByRole('button', { name: 'trajectory' }));
    expect(screen.getByRole('status', { name: 'Agent status' })).toBeVisible();

    rerender(<JobDetail job={{ ...job, liveNote: 'Waiting for LLM response' }} />);
    expect(screen.getByRole('status', { name: 'Agent status' })).toHaveTextContent(
      'Waiting for LLM response',
    );
    expect(screen.queryByText('Running tool call')).not.toBeInTheDocument();
  });

  it.each(['done', 'cancelled'] as const)(
    'hides live agent activity after a running job becomes %s',
    (state) => {
      const job = makeJob({ liveNote: 'Running tool call' });
      const { rerender } = render(<JobDetail job={job} />);
      expect(screen.getByRole('status', { name: 'Agent status' })).toBeVisible();

      rerender(<JobDetail job={{ ...job, state }} />);

      expect(screen.queryByRole('status', { name: 'Agent status' })).not.toBeInTheDocument();
      expect(screen.queryByText('Running tool call')).not.toBeInTheDocument();
    },
  );

  it('renders the conversation first, including history and the full current prompt', () => {
    render(
      <JobDetail
        job={makeJob({
          threadMessages: [
            {
              id: 1,
              role: 'user',
              content: 'Earlier question',
              jobId: 'ajob_0',
              createdAt: null,
            },
            {
              id: 2,
              role: 'assistant',
              content: 'Earlier answer',
              jobId: 'ajob_0',
              createdAt: null,
            },
          ],
        })}
      />,
    );

    expect(screen.getByText('Earlier question')).toBeInTheDocument();
    expect(screen.getByText('Earlier answer')).toBeInTheDocument();
    expect(screen.getByText(/with all of its detail/)).toBeInTheDocument();
    expect(screen.getByText('Run started')).toBeInTheDocument();
    expect(screen.getByText('I found the', { exact: false })).toBeInTheDocument();
  });

  it("keeps a previous turn's tool activity after a follow-up is appended", () => {
    render(
      <JobDetail
        job={makeJob({
          turnNo: 2,
          threadMessages: HISTORY,
          historyTurns: [
            {
              jobId: 'ajob_0',
              turnNo: 1,
              forkable: true,
              messages: HISTORY,
              events: [
                { kind: 'message', text: 'I will inspect the repository.', attemptNo: 1 },
                {
                  kind: 'tool_use',
                  tool: 'Read',
                  detail: 'src/router.py',
                  output: ['router source'],
                  attemptNo: 1,
                },
                { kind: 'message', text: 'Earlier answer', attemptNo: 1 },
              ],
            },
          ],
        })}
      />,
    );

    const turn = screen.getByLabelText('Turn 1');
    const showCall = within(turn).getByRole('button', { name: 'Show tool call' });
    expect(showCall).toHaveAttribute('aria-expanded', 'false');
    expect(within(turn).getByText('Read: src/router.py')).toBeVisible();
    expect(within(turn).getByText('src/router.py', { selector: 'pre' })).not.toBeVisible();
    expect(within(turn).getByText('router source')).not.toBeVisible();
    fireEvent.click(showCall);
    expect(within(turn).getByText('router source')).toBeVisible();
    expect(within(turn).getByText('Earlier answer')).toBeInTheDocument();
    expect(screen.getAllByText('Earlier answer')).toHaveLength(1);
    expect(within(turn).getByRole('button', { name: 'View execution' })).toBeInTheDocument();
    expect(screen.getByText(/with all of its detail/)).toBeInTheDocument();
  });

  it('keeps follow-up startup quiet while retaining the running controls and real activity', () => {
    const startup = [
      'started',
      'workspace_preparing',
      'workspace_reused',
      'workspace_ready',
      'init',
    ].map((text) => ({ kind: 'lifecycle' as const, text, attemptNo: 1 }));
    const job = makeJob({ turnNo: 2, events: startup });
    const view = render(<JobDetail job={job} />);
    const activity = screen.getByLabelText('Agent activity');
    expect(activity).toBeEmptyDOMElement();
    expect(screen.queryByText('Continuing conversation…')).not.toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Stop' })).toBeEnabled();

    view.rerender(
      <JobDetail
        job={{
          ...job,
          events: [
            ...startup,
            { kind: 'thinking', text: '', attemptNo: 1 },
            { kind: 'lifecycle', text: 'Approaching model usage limit', attemptNo: 1 },
          ],
        }}
      />,
    );
    expect(screen.queryByText('Continuing conversation…')).not.toBeInTheDocument();
    expect(screen.getByLabelText('Agent reasoning')).toBeInTheDocument();
    expect(screen.getByText('Approaching model usage limit')).toBeInTheDocument();
    expect(activity).not.toHaveTextContent(/workspace|Run started|Agent ready/i);
  });

  it('keeps startup detail out of completed follow-ups and preserves their output', () => {
    render(
      <JobDetail
        job={makeJob({
          turnNo: 3,
          historyTurns: [
            {
              jobId: 'ajob_2',
              turnNo: 2,
              forkable: true,
              messages: [],
              events: [
                { kind: 'lifecycle', text: 'workspace_preparing', attemptNo: 1 },
                { kind: 'lifecycle', text: 'workspace_reused', attemptNo: 1 },
                { kind: 'message', text: 'Follow-up answer', attemptNo: 1 },
                { kind: 'lifecycle', text: 'result', attemptNo: 1 },
              ],
            },
          ],
        })}
      />,
    );
    const history = within(screen.getByLabelText('Turn 2 agent activity'));
    expect(history.queryByText(/workspace/i)).not.toBeInTheDocument();
    expect(history.getByText('Follow-up answer')).toBeInTheDocument();
    expect(history.getByText('Agent finished')).toBeInTheDocument();
  });

  it('shows reasoning as a status when no reasoning content was recorded', () => {
    render(
      <JobDetail
        job={makeJob({
          events: [{ kind: 'thinking', text: '', attemptNo: 1 }],
        })}
      />,
    );

    expect(screen.getByLabelText('Agent reasoning')).toHaveTextContent('Agent is reasoning');
    expect(screen.queryByText('Reasoning')).not.toBeInTheDocument();
  });

  it('keeps recorded reasoning collapsed and separate from the assistant reply', () => {
    render(
      <JobDetail
        job={makeJob({
          events: [
            { kind: 'thinking', text: 'Check the parser before editing.', attemptNo: 1 },
            { kind: 'message', text: 'Fixed the parser.', attemptNo: 1 },
          ],
        })}
      />,
    );

    const reasoning = screen.getByLabelText('Agent reasoning');
    expect(reasoning.tagName).toBe('DETAILS');
    expect(reasoning).not.toHaveAttribute('open');
    expect(within(reasoning).getByText('Check the parser before editing.')).toBeInTheDocument();
    expect(within(reasoning).queryByText('Fixed the parser.')).not.toBeInTheDocument();
    expect(screen.getByText('Fixed the parser.')).toBeVisible();
  });

  it('shows inline tool activity with its complete call and result available on demand', () => {
    const input = { command: 'pytest -q', cwd: '/workspace', timeout: 0 };
    render(
      <JobDetail
        job={makeJob({
          events: [
            {
              kind: 'tool_use',
              tool: 'Bash',
              detail: 'pytest -q',
              input,
              output: ['2 passed'],
              attemptNo: 1,
            },
          ],
        })}
      />,
    );

    const showCall = screen.getByRole('button', { name: 'Show tool call' });
    expect(showCall).toHaveAttribute('aria-expanded', 'false');
    expect(screen.getByText('Bash: pytest -q')).toBeVisible();
    expect(screen.getByTitle('Bash: pytest -q · Result received')).toBeVisible();
    const call = document.getElementById(showCall.getAttribute('aria-controls')!);
    expect(call).toHaveTextContent('"cwd": "/workspace"');
    expect(call).toHaveTextContent('"timeout": 0');
    expect(call).not.toBeVisible();
    expect(screen.getByText('2 passed')).not.toBeVisible();

    fireEvent.click(showCall);
    const hideCall = screen.getByRole('button', { name: 'Hide tool call' });
    expect(hideCall).toHaveAttribute('aria-expanded', 'true');
    expect(call).toBeVisible();
    expect(screen.getByText('2 passed')).toBeVisible();

    fireEvent.click(hideCall);
    expect(screen.getByRole('button', { name: 'Show tool call' })).toHaveAttribute(
      'aria-expanded',
      'false',
    );
    expect(screen.getByText('2 passed')).not.toBeVisible();
  });

  it('uses the real cancel endpoint and refreshes the job', async () => {
    const reload = vi.fn();
    vi.mocked(cancelAgentJob).mockResolvedValue({
      id: 'ajob_1',
      state: 'running',
      cancel_requested: true,
    });
    render(<JobDetail job={makeJob()} onReload={reload} />);

    fireEvent.click(screen.getByRole('button', { name: 'Stop' }));

    await waitFor(() => expect(cancelAgentJob).toHaveBeenCalledWith('ajob_1'));
    expect(reload).toHaveBeenCalledOnce();
  });

  it('stops an active run with Escape without intercepting the composer or repeating', async () => {
    vi.mocked(cancelAgentJob).mockResolvedValue({
      id: 'ajob_1',
      state: 'running',
      cancel_requested: true,
    });
    render(<JobDetail job={makeJob()} />);

    const composer = screen.getByLabelText('Add a follow-up');
    composer.focus();
    fireEvent.keyDown(composer, { key: 'Escape' });
    expect(cancelAgentJob).not.toHaveBeenCalled();

    composer.blur();
    fireEvent.keyDown(window, { key: 'Escape' });
    fireEvent.keyDown(window, { key: 'Escape' });
    fireEvent.keyDown(window, { key: 'Escape', repeat: true });

    await waitFor(() => expect(cancelAgentJob).toHaveBeenCalledWith('ajob_1'));
    expect(cancelAgentJob).toHaveBeenCalledTimes(1);
  });

  it('resets the Escape stop guard when navigation supplies a different active job', async () => {
    vi.mocked(cancelAgentJob).mockResolvedValue({
      id: 'ajob_1',
      state: 'running',
      cancel_requested: true,
    });
    const view = render(<JobDetail job={makeJob()} />);
    fireEvent.keyDown(window, { key: 'Escape' });
    await waitFor(() => expect(cancelAgentJob).toHaveBeenCalledWith('ajob_1'));

    view.rerender(<JobDetail job={makeJob({ id: 'ajob_2' })} />);
    fireEvent.keyDown(window, { key: 'Escape' });

    await waitFor(() => expect(cancelAgentJob).toHaveBeenCalledWith('ajob_2'));
    expect(cancelAgentJob).toHaveBeenCalledTimes(2);
  });

  it('queues a message sent while the run is active above the composer, without moving', async () => {
    const waiting = {
      id: 'apmsg_1',
      thread_id: 'ajob_1',
      position: 1,
      state: 'queued' as const,
      revision: 1,
      prompt: 'Now add a regression test',
    };
    vi.mocked(submitThreadMessage).mockResolvedValue({ pending: waiting, job: null });
    window.history.pushState(null, '', '/ajob_1');
    render(<JobDetail job={makeJob()} />);

    expect(screen.getByText(/sent after this turn/i)).toBeInTheDocument();
    const box = screen.getByLabelText('Add a follow-up');
    fireEvent.change(box, { target: { value: 'Now add a regression test' } });
    fireEvent.click(screen.getByRole('button', { name: /^(Send|Queue)$/ }));

    await waitFor(() =>
      expect(submitThreadMessage).toHaveBeenCalledWith('ajob_1', {
        prompt: 'Now add a regression test',
      }),
    );
    const queue = await screen.findByRole('region', { name: 'Queued messages' });
    expect(within(queue).getByText('Now add a regression test')).toBeInTheDocument();
    expect(within(queue).getByText('1 pending')).toBeInTheDocument();
    // The message waits; the page stays on the turn it was showing.
    expect(box).toHaveValue('');
    expect(window.location.pathname).toBe('/ajob_1');
    expect(navigation.push).not.toHaveBeenCalled();
  });

  it('adds the draft, and only the draft, to a live turn that can take it, on Cmd+Enter', async () => {
    const older = {
      id: 'apmsg_old',
      thread_id: 'ajob_1',
      position: 1,
      state: 'queued' as const,
      revision: 1,
      prompt: 'first, check the docs',
      attachments: [],
      last_error: null,
    };
    const drafted = { ...older, id: 'apmsg_new', position: 2, prompt: 'then run the linter' };
    let waiting: PendingMessageApi[] = [older];
    vi.mocked(getThreadQueue).mockImplementation(async (threadId) => ({
      ...emptyQueue(threadId),
      pending_messages: waiting,
      latest_job_id: 'ajob_1',
      latest_turn_no: 1,
      latest_job_state: 'running',
      mid_turn_input_supported: true,
    }));
    vi.mocked(submitThreadMessage).mockImplementation(async () => {
      waiting = [older, drafted];
      return { pending: drafted, job: null };
    });
    vi.mocked(sendPendingMessageNow).mockImplementation(async (_threadId, messageId) => {
      const sending = { ...drafted, state: 'sending' as const, revision: 2 };
      waiting = waiting.map((message) => (message.id === messageId ? sending : message));
      return { pending: sending, job: null };
    });
    render(<JobDetail job={makeJob()} />);

    // Once the live turn can take one, the composer says what the chord does.
    expect(await screen.findByText(/⌘\/Ctrl\+↵ to add to this turn/)).toBeInTheDocument();
    const box = screen.getByLabelText('Add a follow-up');
    fireEvent.change(box, { target: { value: 'then run the linter' } });
    fireEvent.keyDown(box, { key: 'Enter', metaKey: true });

    // The draft goes into the turn; the older message was left for after it.
    await waitFor(() => expect(sendPendingMessageNow).toHaveBeenCalledTimes(1));
    expect(submitThreadMessage).toHaveBeenCalledWith('ajob_1', { prompt: 'then run the linter' });
    expect(sendPendingMessageNow).toHaveBeenCalledWith('ajob_1', 'apmsg_new', 1);
    expect(window.location.pathname).not.toMatch(/apmsg/);
  });

  it('moves to the turn an idle conversation starts at once', async () => {
    vi.mocked(submitThreadMessage).mockResolvedValue({
      pending: null,
      job: { id: 'ajob_child' },
    } as never);
    render(<JobDetail job={makeJob({ state: 'done' })} />);

    fireEvent.change(screen.getByLabelText('Add a follow-up'), {
      target: { value: 'Now add a regression test' },
    });
    fireEvent.click(screen.getByRole('button', { name: /^(Send|Queue)$/ }));

    await waitFor(() => expect(window.location.pathname).toBe('/ajob_child'));
    expect(navigation.push).not.toHaveBeenCalled();
    expect(screen.queryByRole('region', { name: 'Queued messages' })).toBeNull();
  });

  it('sends the follow-up on Enter and keeps Shift+Enter a newline', async () => {
    vi.mocked(submitThreadMessage).mockResolvedValue({
      pending: null,
      job: { id: 'ajob_child' },
    } as never);
    render(<JobDetail job={makeJob()} />);

    const composer = screen.getByLabelText('Add a follow-up');
    fireEvent.change(composer, { target: { value: 'Now add a regression test' } });

    // Shift+Enter and Alt+Enter stay the newline, so the browser keeps them.
    expect(fireEvent.keyDown(composer, { key: 'Enter', shiftKey: true })).toBe(true);
    expect(fireEvent.keyDown(composer, { key: 'Enter', altKey: true })).toBe(true);
    expect(submitThreadMessage).not.toHaveBeenCalled();

    // An IME commits its candidate with Enter; that keypress is the word, not
    // the send.
    fireEvent.keyDown(composer, { key: 'Enter', isComposing: true });
    expect(submitThreadMessage).not.toHaveBeenCalled();

    fireEvent.keyDown(composer, { key: 'Enter' });

    await waitFor(() =>
      expect(submitThreadMessage).toHaveBeenCalledWith('ajob_1', {
        prompt: 'Now add a regression test',
      }),
    );
    expect(window.location.pathname).toBe('/ajob_child');
    expect(navigation.push).not.toHaveBeenCalled();
  });

  it('swallows Enter on an empty follow-up rather than growing a blank line', async () => {
    render(<JobDetail job={makeJob()} />);

    const composer = screen.getByLabelText('Add a follow-up');
    fireEvent.change(composer, { target: { value: '   ' } });

    // Enter is the send key on this composer and never the newline, so with
    // nothing to send it does nothing at all. Shift+Enter still breaks the line.
    expect(fireEvent.keyDown(composer, { key: 'Enter' })).toBe(false);
    expect(fireEvent.keyDown(composer, { key: 'Enter', shiftKey: true })).toBe(true);
    expect(submitThreadMessage).not.toHaveBeenCalled();
  });

  it('accepts the other send chord people arrive with', async () => {
    vi.mocked(submitThreadMessage).mockResolvedValue({
      pending: null,
      job: { id: 'ajob_child' },
    } as never);
    render(<JobDetail job={makeJob()} />);

    const composer = screen.getByLabelText('Add a follow-up');
    fireEvent.change(composer, { target: { value: 'Now add a regression test' } });
    fireEvent.keyDown(composer, { key: 'Enter', metaKey: true });

    await waitFor(() =>
      expect(submitThreadMessage).toHaveBeenCalledWith('ajob_1', {
        prompt: 'Now add a regression test',
      }),
    );
  });

  it('switches the model for the next turn without leaving the thread', async () => {
    vi.mocked(submitThreadMessage).mockResolvedValue({
      pending: null,
      job: { id: 'ajob_child' },
    } as never);
    render(<JobDetail job={makeJob()} />);

    const picker = await screen.findByLabelText('Model for this turn');
    expect(picker).toHaveValue('qwen-test');

    fireEvent.change(picker, { target: { value: 'glm-test' } });
    fireEvent.change(screen.getByLabelText('Add a follow-up'), {
      target: { value: 'Now try it on the other model' },
    });
    fireEvent.click(screen.getByRole('button', { name: /^(Send|Queue)$/ }));

    await waitFor(() =>
      expect(submitThreadMessage).toHaveBeenCalledWith('ajob_1', {
        prompt: 'Now try it on the other model',
        model: 'glm-test',
      }),
    );
  });

  it('keeps the running model selectable after the deployment stops offering it', async () => {
    vi.mocked(getAgentConfig).mockResolvedValue({ ...AGENT_CONFIG, models: ['glm-test'] });
    render(<JobDetail job={makeJob()} />);

    // Otherwise the control would show the deployment's first model while the
    // turn still ran on the parent's — the composer lying about the job again.
    expect(await screen.findByLabelText('Model for this turn')).toHaveValue('qwen-test');
  });

  it('names the inherited model when the model list will not load', async () => {
    vi.mocked(getAgentConfig).mockRejectedValue(new Error('config unavailable'));
    vi.mocked(submitThreadMessage).mockResolvedValue({
      pending: null,
      job: { id: 'ajob_child' },
    } as never);
    render(<JobDetail job={makeJob()} />);

    await waitFor(() => expect(getAgentConfig).toHaveBeenCalled());
    expect(screen.queryByLabelText('Model for this turn')).not.toBeInTheDocument();
    expect(screen.getByText(/Inherits claude-code · qwen-test/)).toBeInTheDocument();

    fireEvent.change(screen.getByLabelText('Add a follow-up'), {
      target: { value: 'Carry on' },
    });
    fireEvent.click(screen.getByRole('button', { name: /^(Send|Queue)$/ }));

    await waitFor(() =>
      expect(submitThreadMessage).toHaveBeenCalledWith('ajob_1', { prompt: 'Carry on' }),
    );
  });

  it.each(['done', 'needs_review'] as const)(
    'keeps successful %s replies free of completion metadata',
    (state) => {
      const stateNote = 'agent completed (shared workspace; publication snapshot pending)';
      render(<JobDetail job={makeJob({ state, stateNote, sandbox: 'io.containerd.kata.v2' })} />);
      expect(screen.queryByText(stateNote)).not.toBeInTheDocument();
      expect(screen.queryByText(/^(Done|Completed|Outcome|Needs review)$/)).not.toBeInTheDocument();
      expect(screen.queryByRole('dialog', { name: 'Run details' })).not.toBeInTheDocument();
      expect(screen.queryByText('Sandbox')).not.toBeInTheDocument();
    },
  );

  it.each(['queued', 'running', 'failed'] as const)(
    'keeps the current turn actionable while the conversation is %s',
    (state) => {
      render(<JobDetail job={makeJob({ state })} />);
      expect(screen.getByLabelText('Agent activity')).toHaveTextContent('I found the cause.');
      expect(screen.queryByText(/Execution history/)).not.toBeInTheDocument();
      if (state === 'failed') {
        expect(screen.queryByRole('button', { name: 'Stop' })).not.toBeInTheDocument();
      } else {
        expect(screen.getByRole('button', { name: 'Stop' })).toBeEnabled();
      }
    },
  );

  it('preserves the stopped result and follow-up after stopping', () => {
    const job = makeJob();
    const { rerender } = render(<JobDetail job={job} />);
    rerender(<JobDetail job={{ ...job, state: 'cancelled' }} />);
    expect(screen.getByText('Run stopped')).toBeInTheDocument();
    expect(screen.getByLabelText('Add a follow-up')).toBeEnabled();
    expect(screen.queryByRole('button', { name: 'Stop' })).not.toBeInTheDocument();
  });

  it.each(['done', 'cancelled'] as const)(
    'keeps a historical %s turn free of the Stop action',
    (state) => {
      const job = makeJob({ state, conversationState: 'running' });
      render(<JobDetail job={job} />);
      // A historical turn does not gain a Stop action for another turn.
      expect(screen.queryByRole('button', { name: 'Stop' })).not.toBeInTheDocument();
      if (state === 'cancelled') expect(screen.getByText('Run stopped')).toBeInTheDocument();
      expect(screen.getByLabelText('Agent activity')).toHaveTextContent('I found the cause.');
    },
  );

  it('shows artifacts as shared workspace changes and reuses the Git action', async () => {
    const prUrl = 'https://github.com/owner/repository/pull/7';
    vi.mocked(getAgentJobGit).mockResolvedValue({
      available: true,
      branch: 'agent/ajob_1',
      changes: [],
      patch: '',
      commits: [],
    });
    render(
      <JobDetail
        job={makeJob({
          state: 'done',
          workspaceShared: true,
          prUrl,
          prLabel: 'draft PR 7',
        })}
      />,
    );
    expect(screen.getByRole('button', { name: 'View changes' })).toBeInTheDocument();
    expect(screen.getByRole('link', { name: 'Open draft PR' })).toHaveAttribute('href', prUrl);
    fireEvent.click(screen.getByRole('button', { name: 'View changes' }));
    expect(screen.queryByRole('dialog', { name: 'Run details' })).not.toBeInTheDocument();
    expect(screen.getByRole('region', { name: 'Job workspace' })).toBeInTheDocument();
    await waitFor(() => expect(getAgentJobGit).toHaveBeenCalledWith('ajob_1'));
    const git = screen.getByLabelText('Git workspace');
    await waitFor(() => expect(within(git).getByRole('button', { name: 'Refresh' })).toBeEnabled());
    expect(git).toHaveTextContent('agent/ajob_1');
    expect(git).not.toHaveTextContent('Live workspace');
    expect(git).not.toHaveTextContent('Shared workspace');
  });

  function ownedConversation(id: string) {
    return {
      job: { id, thread_id: id } as AgentJobApi,
      title: 'Current task',
      archived: false,
      last_activity_at: null,
    };
  }

  // The conversation page reads the app's first-run answer, as the layout
  // provides it; it asks nothing itself.
  function inApp(job: AgentJob) {
    return render(
      <FirstRunProvider userId="alice">
        <JobDetail job={job} />
      </FirstRunProvider>,
    );
  }

  it('says where an owner’s first draft PR is, and that replies update it', async () => {
    vi.mocked(listAgentConversations).mockResolvedValue({
      conversations: [ownedConversation('ajob_1')],
      next_cursor: null,
    });
    const prUrl = 'https://github.com/owner/repository/pull/7';

    inApp(makeJob({ state: 'done', prUrl }));

    expect(await screen.findByText('Your first draft PR is ready')).toBeInTheDocument();
    expect(screen.getByText(/Reply below and the same PR is updated/)).toBeInTheDocument();
    expect(screen.getByRole('link', { name: 'Open draft PR' })).toHaveAttribute('href', prUrl);
    // Its milestones say what they mean, this once.
    expect(screen.getByRole('button', { name: 'Review' })).toBeInTheDocument();
    const watch = screen.getByRole('button', { name: 'Watch PR and merge' });
    expect(watch.parentElement).toContainElement(
      screen.getByRole('link', { name: 'Open draft PR' }),
    );
    expect(screen.getByText(/keeps going if you close this page/)).toBeInTheDocument();
  });

  it('keeps the plain actions for an owner with more than this conversation', async () => {
    vi.mocked(listAgentConversations).mockResolvedValue({
      conversations: [ownedConversation('ajob_1'), ownedConversation('ajob_0')],
      next_cursor: null,
    });

    inApp(makeJob({ state: 'done', prUrl: 'https://github.com/owner/repository/pull/7' }));

    await waitFor(() => expect(listAgentConversations).toHaveBeenCalled());
    expect(screen.getByRole('link', { name: 'Open draft PR' })).toBeInTheDocument();
    expect(screen.queryByText('Your first draft PR is ready')).not.toBeInTheDocument();
    expect(screen.queryByText(/keeps going if you close this page/)).not.toBeInTheDocument();
  });

  it('keeps a later turn of the first conversation plain', async () => {
    vi.mocked(listAgentConversations).mockResolvedValue({
      conversations: [ownedConversation('ajob_1')],
      next_cursor: null,
    });

    inApp(
      makeJob({ state: 'done', turnNo: 2, prUrl: 'https://github.com/owner/repository/pull/7' }),
    );

    await waitFor(() => expect(listAgentConversations).toHaveBeenCalled());
    expect(screen.getByRole('link', { name: 'Open draft PR' })).toBeInTheDocument();
    expect(screen.queryByText('Your first draft PR is ready')).not.toBeInTheDocument();
  });

  it('keeps the empty workspace free of change and absent-PR placeholders', () => {
    render(
      <JobDetail
        job={makeJob({
          state: 'done',
          workspaceDiffAvailable: true,
          diffFiles: [],
          diffStat: undefined,
        })}
      />,
    );
    expect(screen.queryByRole('button', { name: 'View changes' })).not.toBeInTheDocument();
    expect(screen.queryByText(/0 files changed|No pull request yet/)).not.toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Open workspace' })).toBeInTheDocument();
  });

  it('keeps an existing PR accessible when the workspace has no remaining changes', () => {
    render(
      <JobDetail
        job={makeJob({
          state: 'done',
          workspaceDiffAvailable: true,
          diffFiles: [],
          prUrl: 'https://github.com/owner/repository/pull/7',
        })}
      />,
    );
    const workspace = screen.getByRole('region', { name: 'Task progress' });
    expect(within(workspace).getByRole('link', { name: 'Open draft PR' })).toHaveAttribute(
      'href',
      'https://github.com/owner/repository/pull/7',
    );
    expect(
      within(workspace).queryByRole('button', { name: 'View changes' }),
    ).not.toBeInTheDocument();
  });

  it.each([
    ['publishing', 'Publishing changes…'],
    ['failed', 'Could not publish changes'],
  ] as const)('preserves a %s publication status without a diff', (state, message) => {
    render(
      <JobDetail
        job={makeJob({
          state: 'done',
          workspaceDiffAvailable: true,
          diffFiles: [],
          workspacePublicationState: state,
        })}
      />,
    );
    const workspace = screen.getByRole('region', { name: 'Task progress' });
    expect(within(workspace).getByText(message)).toBeVisible();
    expect(
      within(workspace).queryByRole('button', { name: 'View changes' }),
    ).not.toBeInTheDocument();
  });

  it('requires a new chat only when context loss is confirmed', () => {
    const job = makeJob({ state: 'cancelled' });
    const { rerender } = render(<JobDetail job={job} />);
    const outcome = screen.getByRole('region', { name: 'Task progress' });
    expect(within(outcome).getByText(/Send a follow-up to continue/)).toBeInTheDocument();
    expect(within(outcome).queryByRole('link', { name: 'New chat' })).not.toBeInTheDocument();
    rerender(<JobDetail job={{ ...job, nativeState: 'lost' }} />);
    expect(within(outcome).getByText(/We couldn’t reconnect this chat/)).toBeInTheDocument();
    expect(within(outcome).getByRole('link', { name: 'New chat' })).toHaveAttribute('href', '/');
    expect(within(outcome).queryByText(/Send a follow-up to continue/)).not.toBeInTheDocument();
  });

  it('keeps the failure reason and recovery guidance in the conversation', () => {
    render(<JobDetail job={makeJob({ state: 'failed', stateNote: 'Agent exited 1' })} />);
    const outcome = screen.getByRole('region', { name: 'Task progress' });
    expect(within(outcome).getByText('Agent exited 1')).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: 'View failure event' })).not.toBeInTheDocument();
    expect(screen.queryByRole('button', { name: 'Inspect raw events' })).not.toBeInTheDocument();
  });

  it('does not suggest a follow-up when the repository is over the import limit', () => {
    render(
      <JobDetail
        job={makeJob({
          state: 'failed',
          stateNote:
            'This repository is larger than this server allows (8.43 GiB; the limit is 1.00 GiB).',
          failureCause: 'repository_too_large',
        })}
      />,
    );
    const outcome = screen.getByRole('region', { name: 'Task progress' });
    expect(within(outcome).getByText(/larger than this server allows/)).toBeInTheDocument();
    expect(within(outcome).getByText(/A follow-up would fail the same way/)).toBeInTheDocument();
    expect(screen.queryByText(/send a follow-up to try again/)).not.toBeInTheDocument();
  });

  it('shows a site misconfiguration as an alert that sends the owner to an admin', () => {
    render(
      <JobDetail
        job={makeJob({
          state: 'failed',
          stateNote:
            'Site misconfigured: this chat uses connected tools, but the server that ran it ' +
            'has no MCP relay. Ask an admin to finish setting up the site.',
          failureCause: 'site_misconfigured',
        })}
      />,
    );
    const outcome = screen.getByRole('region', { name: 'Task progress' });
    const alert = within(outcome).getByRole('alert');
    expect(within(alert).getByText('Site misconfigured')).toBeInTheDocument();
    expect(
      within(alert).getByText(
        'This chat uses connected tools, but the server that ran it has no MCP relay. ' +
          'Ask an admin to finish setting up the site.',
      ),
    ).toBeInTheDocument();
    expect(within(outcome).queryByText('Run failed')).not.toBeInTheDocument();
    expect(screen.queryByText(/send a follow-up to try again/)).not.toBeInTheDocument();
  });

  it('keeps failure guidance and usage visible when no assistant reply was recorded', async () => {
    vi.mocked(getAgentJobUsage).mockResolvedValue(measuredUsage());
    render(<JobDetail job={makeJob({ state: 'failed', events: [], stateNote: undefined })} />);
    expect(screen.getByText(/No failure details were recorded/)).toBeInTheDocument();
    expect(screen.getByText(/send a follow-up to try again/)).toBeInTheDocument();
    expect(await screen.findByLabelText('Run usage')).toHaveTextContent('2 model calls');
    expect(screen.getAllByRole('region', { name: 'Usage' })).toHaveLength(1);
  });

  it('moves usage to the newest assistant reply without restarting accounting requests', async () => {
    const data = measuredUsage();
    vi.mocked(getAgentJobUsage).mockResolvedValue(data);
    vi.mocked(refreshAgentJobUsage).mockResolvedValue(data);
    const job = makeJob({
      threadMessages: HISTORY,
      events: [{ kind: 'message', text: 'Inspecting the source.', attemptNo: 1 }],
    });
    const { rerender } = render(<JobDetail job={job} />);
    await waitFor(() => expect(refreshAgentJobUsage).toHaveBeenCalledTimes(1));
    const firstReply = screen.getByText('Inspecting the source.').closest<HTMLElement>('.group')!;
    expect(within(firstReply).getByLabelText('Run usage')).toHaveTextContent('2 model calls');
    const historyReply = screen.getByText('Earlier answer').closest<HTMLElement>('.group')!;
    expect(within(historyReply).queryByLabelText('Run usage')).not.toBeInTheDocument();

    await act(async () => {
      rerender(
        <JobDetail
          job={{
            ...job,
            events: [...job.events, { kind: 'message', text: 'The fix is ready.', attemptNo: 1 }],
          }}
        />,
      );
    });
    const latestReply = screen.getByText('The fix is ready.').closest<HTMLElement>('.group')!;
    expect(within(latestReply).getByLabelText('Run usage')).toHaveTextContent('2 model calls');
    expect(within(firstReply).queryByLabelText('Run usage')).not.toBeInTheDocument();
    expect(screen.getAllByRole('region', { name: 'Usage' })).toHaveLength(1);
    expect(getAgentJobUsage).toHaveBeenCalledTimes(1);
    expect(refreshAgentJobUsage).toHaveBeenCalledTimes(1);
  });

  it('attaches usage to the current durable reply when event history is unavailable', async () => {
    vi.mocked(getAgentJobUsage).mockResolvedValue(measuredUsage());
    render(
      <JobDetail
        job={makeJob({
          state: 'done',
          events: [],
          threadMessages: [
            ...HISTORY,
            {
              id: 3,
              role: 'assistant',
              content: 'Current answer',
              jobId: 'ajob_1',
              createdAt: null,
            },
          ],
        })}
      />,
    );
    await screen.findByLabelText('Run usage');
    const currentReply = screen.getByText('Current answer').closest<HTMLElement>('.group')!;
    const historyReply = screen.getByText('Earlier answer').closest<HTMLElement>('.group')!;
    expect(within(currentReply).getByLabelText('Run usage')).toHaveTextContent('2 model calls');
    expect(within(historyReply).queryByLabelText('Run usage')).not.toBeInTheDocument();
    expect(screen.getAllByRole('region', { name: 'Usage' })).toHaveLength(1);
  });

  it('does not attribute current usage to a superseded attempt', async () => {
    vi.mocked(getAgentJobUsage).mockResolvedValue(measuredUsage());
    render(
      <JobDetail
        job={makeJob({
          state: 'done',
          currentAttemptNo: 2,
          attempts: [
            { no: 1, status: 'superseded' },
            { no: 2, status: 'finished' },
          ],
          events: [
            { kind: 'message', text: 'Superseded answer', attemptNo: 1 },
            { kind: 'message', text: 'Final answer', attemptNo: 2 },
          ],
        })}
      />,
    );
    expect(await screen.findByLabelText('Run usage')).toHaveTextContent('2 model calls');
    const history = screen.getByText('Execution history (2)').closest('details');
    expect(history).not.toHaveAttribute('open');
    expect(screen.getByText('Final answer')).toBeVisible();
    expect(screen.queryByText('Superseded answer')).not.toBeInTheDocument();
    fireEvent.click(screen.getByText('Execution history (2)'));
    expect(history).toHaveAttribute('open');
    fireEvent.click(screen.getByRole('button', { name: '1 · superseded' }));
    expect(screen.getByRole('button', { name: '1 · superseded' })).toHaveAttribute(
      'aria-pressed',
      'true',
    );
    expect(screen.getByText(/Viewing execution 1/)).toBeInTheDocument();
    expect(screen.getByText('Superseded answer')).toBeInTheDocument();
    expect(screen.queryByRole('region', { name: 'Usage' })).not.toBeInTheDocument();
    fireEvent.click(screen.getByText(/Execution history \(2\)/));
    await waitFor(() => expect(screen.getByText('Final answer')).toBeVisible());
    expect(history).not.toHaveAttribute('open');
    expect(screen.queryByText(/Viewing execution 1/)).not.toBeInTheDocument();
    expect(screen.getByLabelText('Run usage')).toHaveTextContent('2 model calls');
    expect(getAgentJobUsage).toHaveBeenCalledTimes(1);
    expect(refreshAgentJobUsage).not.toHaveBeenCalled();
    expect(cancelAgentJob).not.toHaveBeenCalled();
    expect(submitThreadMessage).not.toHaveBeenCalled();
  });

  it.each([null, 0])('preserves an absent or zero Gateway model-call count (%s)', async (turns) => {
    const measuredTotals = {
      calls: turns ?? 0,
      tokens_in: null,
      tokens_out: null,
      cache_read: null,
      cache_write: null,
      reasoning: null,
      spent_usd: null,
    };
    vi.mocked(getAgentJobUsage).mockResolvedValue({
      ledger: {
        status: turns === null ? 'unavailable' : 'available',
        source: 'gateway_api_logs',
        attribution: 'grant_time_window',
        finalized: false,
        totals: turns === null ? null : measuredTotals,
        unknown_rows: null,
        estimated_calls: 0,
        queried_at: null,
        missing_attempts: 0,
        attempts: [],
      },
      runtime: {
        source: 'runtime',
        input_tokens: null,
        output_tokens: null,
        cache_read_tokens: null,
      },
    } satisfies AgentJobUsageApi);
    const job = makeJob();
    render(<JobDetail job={{ ...job, usage: { ...job.usage, turns: 999 } }} />);
    await waitFor(() => expect(screen.queryByText('Loading usage…')).not.toBeInTheDocument());
    const usage = screen.getByRole('region', { name: 'Usage' });
    expect(
      within(usage).getByText(turns === null ? 'Usage temporarily unavailable.' : '0 model calls'),
    ).toBeInTheDocument();
    expect(within(usage).queryByText(/999/)).not.toBeInTheDocument();
  });

  it('wraps unbroken failure details inside the outcome card', () => {
    const stateNote = `agent exited 1: ${'x'.repeat(300)}`;
    render(<JobDetail job={makeJob({ state: 'failed', stateNote })} />);

    expect(screen.getByText(stateNote)).toHaveClass('[overflow-wrap:anywhere]');
  });

  describe('the transcript tool inspector', () => {
    function viewport() {
      vi.stubGlobal(
        'matchMedia',
        vi.fn((query: string) => ({
          matches: false,
          media: query,
          onchange: null,
          addListener: vi.fn(),
          removeListener: vi.fn(),
          addEventListener: vi.fn(),
          removeEventListener: vi.fn(),
          dispatchEvent: vi.fn(),
        })),
      );
    }

    afterEach(() => vi.unstubAllGlobals());

    function transcriptInspectorFixture() {
      vi.mocked(getAgentAttemptTelemetry).mockResolvedValue({
        attempt_id: 1,
        attempt_no: 1,
        continuity_mode: null,
        context_status: null,
        duration_ms: null,
        resources: null,
      });
      return {
        job: makeJob({
          state: 'done',
          events: [
            {
              kind: 'tool_use',
              tool: 'Bash',
              id: 't1',
              detail: 'pytest -q',
              input: { command: 'pytest -q' },
              output: ['2 passed'],
              resultState: 'received',
              attemptNo: 1,
              sourceEventId: 42,
            },
            {
              kind: 'message',
              text: 'The fix is ready.',
              attemptNo: 1,
              sourceEventId: 44,
            },
          ],
        }),
        stored: [
          {
            id: 42,
            job_id: 'ajob_1',
            turn_no: 1,
            attempt_id: 1,
            seq: 1,
            event_type: 'tool_use',
            payload: { id: 't1', name: 'Bash', input: { command: 'pytest -q' } },
            created_at: null,
          },
          {
            id: 43,
            job_id: 'ajob_1',
            turn_no: 1,
            attempt_id: 1,
            seq: 2,
            event_type: 'tool_result',
            payload: { tool_use_id: 't1', content: '2 passed', is_error: false },
            created_at: null,
          },
        ],
      };
    }

    it('moves keyboard focus into the transcript tool inspector and restores the original chip', async () => {
      viewport();
      const { job, stored } = transcriptInspectorFixture();
      render(<JobDetail job={job} events={stored} />);
      const chip = screen.getByRole('button', { name: 'Inspect Bash: pytest -q' });
      chip.focus();
      // A focused control's keyboard activation produces a click with detail 0.
      fireEvent.click(chip, { detail: 0 });

      const inspector = await screen.findByRole('region', { name: 'Step details' });
      const close = await within(inspector).findByRole('button', { name: 'Close details' });
      expect(close).toHaveFocus();
      expect(inspector).toHaveTextContent('pytest -q');
      expect(screen.queryByLabelText('Search steps')).not.toBeInTheDocument();

      fireEvent.keyDown(close, { key: 'Escape' });
      await waitFor(() => expect(chip).toHaveFocus());
      expect(screen.queryByRole('region', { name: 'Step details' })).not.toBeInTheDocument();
      expect(cancelAgentJob).not.toHaveBeenCalled();
      expect(submitThreadMessage).not.toHaveBeenCalled();
    });

    it('preserves the reading row while a narrow transcript tool inspector hides the chat', async () => {
      viewport();
      const observers: Array<{
        targets: Set<Element>;
        trigger: () => void;
        connected: boolean;
      }> = [];
      vi.stubGlobal(
        'ResizeObserver',
        class {
          targets = new Set<Element>();
          connected = true;
          constructor(private callback: ResizeObserverCallback) {
            observers.push(this);
          }
          observe(target: Element) {
            this.targets.add(target);
          }
          unobserve(target: Element) {
            this.targets.delete(target);
          }
          disconnect() {
            this.connected = false;
          }
          trigger = () => this.callback([], this as unknown as ResizeObserver);
        },
      );
      const { job, stored } = transcriptInspectorFixture();
      render(<JobDetail job={job} events={stored} />);
      const region = screen.getByRole('region', { name: 'Task progress' });
      const hidden = () => region.classList.contains('hidden');
      const rect = (top: number, height: number) => ({
        top,
        bottom: top + height,
        height,
        left: 0,
        right: hidden() ? 0 : 800,
        width: hidden() ? 0 : 800,
        x: 0,
        y: top,
        toJSON: () => ({}),
      });
      Object.defineProperties(region, {
        clientWidth: { configurable: true, get: () => (hidden() ? 0 : 800) },
        clientHeight: { configurable: true, get: () => (hidden() ? 0 : 600) },
        scrollHeight: { configurable: true, get: () => (hidden() ? 0 : 2000) },
        getBoundingClientRect: {
          configurable: true,
          value: () => rect(0, hidden() ? 0 : 600),
        },
      });
      const rowTops: Record<string, number> = {
        'ajob_1:prompt': 0,
        'ajob_1:e42': 600,
        'ajob_1:e44': 900,
      };
      for (const anchor of region.querySelectorAll<HTMLElement>(
        '[data-turn-key], [data-block-key]',
      )) {
        Object.defineProperty(anchor, 'getBoundingClientRect', {
          configurable: true,
          value: () =>
            hidden()
              ? rect(0, 0)
              : rect((rowTops[anchor.dataset.blockKey ?? ''] ?? 0) - region.scrollTop, 80),
        });
      }
      const settle = () =>
        act(async () => {
          await new Promise((resolve) => requestAnimationFrame(resolve));
        });
      const resizeTranscript = () =>
        act(() => {
          for (const observer of observers) {
            if (observer.connected && observer.targets.has(region)) observer.trigger();
          }
        });
      resizeTranscript();
      await settle();
      expect(region.scrollTop).toBe(1400);

      fireEvent.wheel(region, { deltaY: -850 });
      region.scrollTop = 550;
      fireEvent.scroll(region);
      await settle();
      const chip = screen.getByRole('button', { name: 'Inspect Bash: pytest -q' });
      chip.focus();
      fireEvent.click(chip, { detail: 0 });
      const inspector = await screen.findByRole('region', { name: 'Step details' });
      expect(hidden()).toBe(true);
      // A display:none viewport reports zero dimensions and may reset scrollTop.
      region.scrollTop = 0;
      resizeTranscript();
      await settle();
      fireEvent.click(within(inspector).getByRole('button', { name: 'Close details' }));
      await settle();
      await waitFor(() => expect(chip).toHaveFocus());
      expect(hidden()).toBe(false);
      expect(region.scrollTop).toBe(550);
      expect(cancelAgentJob).not.toHaveBeenCalled();
    });
  });

  describe('the floating tasks and subagents window', () => {
    function activityEvents(
      subject = 'Implement the activity window',
      attemptNo = 1,
    ): AgentJob['events'] {
      return [
        {
          kind: 'tool_use',
          tool: 'update_plan',
          detail: 'Update the plan',
          input: {
            plan: [
              { step: 'Inspect the transcript events', status: 'completed' },
              { step: subject, status: 'in_progress' },
            ],
          },
          resultState: 'received',
          output: ['Plan updated'],
          attemptNo,
          sourceEventId: attemptNo * 10 + 1,
        },
        {
          kind: 'tool_use',
          tool: 'spawn_agent',
          id: `spawn-review-${attemptNo}`,
          detail: 'Review window interactions',
          input: {
            task_name: 'Review window interactions',
            message: 'Check focus restoration and floating layout.',
          },
          output: [
            JSON.stringify({
              agent_id: `agent-ui-review-${attemptNo}`,
              agents_states: { [`agent-ui-review-${attemptNo}`]: { status: 'running' } },
            }),
          ],
          resultState: 'received',
          attemptNo,
          sourceEventId: attemptNo * 10 + 2,
        },
      ];
    }

    it('floats over the transcript and renders only populated sections', () => {
      const view = render(<JobDetail job={makeJob({ events: activityEvents() })} />);

      let activity = screen.getByRole('region', { name: 'Tasks and subagents' });
      expect(activity).toHaveAttribute('id', 'job-activity-window');
      expect(activity.parentElement).toHaveClass('absolute', 'pointer-events-none');
      expect(activity).toHaveClass('pointer-events-auto', 'max-w-sm', 'shadow-xl');
      expect(screen.getByRole('region', { name: 'Task progress' })).toBeVisible();
      expect(within(activity).getByRole('region', { name: 'Tasks overview' })).toBeVisible();
      expect(within(activity).getByRole('region', { name: 'Subagents overview' })).toBeVisible();
      const details = within(activity).getByRole('region', {
        name: 'Tasks and subagents details',
      });
      expect(details).toHaveAttribute('tabindex', '0');
      details.focus();
      expect(details).toHaveFocus();
      expect(within(activity).getByText('Implement the activity window')).toBeVisible();
      expect(within(activity).getAllByText('agent-ui-review-1')).toHaveLength(2);
      expect(screen.queryByRole('region', { name: 'Task activity' })).not.toBeInTheDocument();
      expect(screen.queryByRole('region', { name: 'Subagent activity' })).not.toBeInTheDocument();

      view.rerender(<JobDetail job={makeJob({ events: activityEvents().slice(0, 1) })} />);
      activity = screen.getByRole('region', { name: 'Tasks and subagents' });
      expect(within(activity).getByRole('region', { name: 'Tasks overview' })).toBeVisible();
      expect(
        within(activity).queryByRole('region', { name: 'Subagents overview' }),
      ).not.toBeInTheDocument();

      view.rerender(<JobDetail job={makeJob({ events: activityEvents().slice(1) })} />);
      activity = screen.getByRole('region', { name: 'Tasks and subagents' });
      expect(
        within(activity).queryByRole('region', { name: 'Tasks overview' }),
      ).not.toBeInTheDocument();
      expect(within(activity).getByRole('region', { name: 'Subagents overview' })).toBeVisible();
    });

    it('omits the toggle and window when recorded activity is empty', () => {
      const job = makeJob({
        events: [
          {
            kind: 'tool_use',
            tool: 'update_plan',
            detail: 'Empty plan',
            input: { plan: [] },
            attemptNo: 1,
          },
          {
            kind: 'tool_use',
            tool: 'list_agents',
            detail: 'No agents',
            input: {},
            output: [JSON.stringify({ agents_states: {} })],
            resultState: 'received',
            attemptNo: 1,
          },
        ],
      });
      const view = render(<JobDetail job={job} />);

      expect(screen.queryByRole('region', { name: 'Tasks and subagents' })).not.toBeInTheDocument();
      expect(
        screen.queryByRole('button', { name: /tasks and subagents/i }),
      ).not.toBeInTheDocument();

      view.rerender(<JobDetail job={{ ...job, events: activityEvents() }} />);
      expect(screen.getByRole('region', { name: 'Tasks and subagents' })).toBeVisible();

      view.rerender(<JobDetail job={job} />);
      expect(screen.queryByRole('region', { name: 'Tasks and subagents' })).not.toBeInTheDocument();
      expect(
        screen.queryByRole('button', { name: /tasks and subagents/i }),
      ).not.toBeInTheDocument();
    });

    it('closes, reopens, and returns focus without stopping the task', () => {
      render(<JobDetail job={makeJob({ events: activityEvents() })} />);

      const toggle = screen.getByRole('button', { name: 'Hide tasks and subagents' });
      expect(toggle).toHaveAttribute('aria-controls', 'job-activity-window');
      expect(toggle).toHaveAttribute('aria-expanded', 'true');
      const close = within(screen.getByRole('region', { name: 'Tasks and subagents' })).getByRole(
        'button',
        { name: 'Close tasks and subagents' },
      );
      close.focus();
      fireEvent.click(close);
      expect(screen.queryByRole('region', { name: 'Tasks and subagents' })).not.toBeInTheDocument();
      expect(toggle).toHaveFocus();

      fireEvent.click(toggle);
      const reopenedClose = within(
        screen.getByRole('region', { name: 'Tasks and subagents' }),
      ).getByRole('button', { name: 'Close tasks and subagents' });
      reopenedClose.focus();
      fireEvent.keyDown(reopenedClose, { key: 'Escape' });
      expect(screen.queryByRole('region', { name: 'Tasks and subagents' })).not.toBeInTheDocument();
      expect(toggle).toHaveFocus();
      expect(cancelAgentJob).not.toHaveBeenCalled();
    });

    it('follows the selected attempt instead of mixing activity', () => {
      render(
        <JobDetail
          job={makeJob({
            state: 'done',
            currentAttemptNo: 2,
            attempts: [
              { no: 1, status: 'superseded' },
              { no: 2, status: 'finished' },
            ],
            events: [
              ...activityEvents('Superseded implementation plan', 1),
              ...activityEvents('Current implementation plan', 2),
            ],
          })}
        />,
      );

      let activity = screen.getByRole('region', { name: 'Tasks and subagents' });
      expect(within(activity).getByText('Current implementation plan')).toBeVisible();
      expect(
        within(activity).queryByText('Superseded implementation plan'),
      ).not.toBeInTheDocument();
      fireEvent.click(screen.getByText('Execution history (2)'));
      fireEvent.click(screen.getByRole('button', { name: '1 · superseded' }));
      activity = screen.getByRole('region', { name: 'Tasks and subagents' });
      expect(within(activity).getByText('Superseded implementation plan')).toBeVisible();
      expect(within(activity).queryByText('Current implementation plan')).not.toBeInTheDocument();
    });

    it.each(['native', 'duplicate'] as const)(
      'opens %s subagent call details from chat without constructing a trajectory during overview rendering',
      async (kind) => {
        renderingWork.trajectory.mockClear();
        const currentAttemptNo = kind === 'native' ? 2 : 1;
        vi.mocked(getAgentAttemptTelemetry).mockResolvedValue({
          attempt_id: currentAttemptNo,
          attempt_no: currentAttemptNo,
          continuity_mode: null,
          context_status: null,
          duration_ms: null,
          resources: null,
        });
        function nativeRecord(
          id: number,
          attempt: number,
          worker: string,
          completed = false,
        ): AgentThreadEventApi {
          return {
            id,
            job_id: 'ajob_1',
            turn_no: 1,
            attempt_id: attempt,
            seq: completed ? 2 : 1,
            event_type: 'raw',
            created_at: null,
            payload: {
              protocol: 'codex-app-server',
              notification: {
                method: completed ? 'item/completed' : 'item/started',
                params: {
                  item: {
                    type: 'collabAgentToolCall',
                    id: 'native-reviewer',
                    tool: 'spawnAgent',
                    receiverThreadIds: [worker],
                    status: completed ? 'completed' : 'inProgress',
                    agentsStates: {
                      [worker]: { status: 'completed', message: 'Review complete' },
                    },
                  },
                },
              },
            },
          };
        }
        let stored: AgentThreadEventApi[];
        if (kind === 'native') {
          stored = [
            nativeRecord(699, 1, 'superseded-worker'),
            nativeRecord(700, 2, 'worker-one'),
            nativeRecord(701, 2, 'worker-one', true),
          ];
        } else {
          stored = ['First review', 'Second review'].map((task, index) => ({
            id: 700 + index,
            job_id: 'ajob_1',
            turn_no: 1,
            attempt_id: 1,
            seq: index + 1,
            event_type: 'tool_use',
            created_at: null,
            payload: {
              id: 'repeated-reviewer',
              name: 'spawn_agent',
              input: { task_name: task },
            },
          }));
        }
        const displayEvents = projectProtocolEvents(stored).map((event) => ({
          ...toDisplayEvent(event)!,
          attemptNo: event.attempt_id ?? currentAttemptNo,
        }));
        const attempts: AgentJob['attempts'] = [{ no: currentAttemptNo, status: 'live' }];
        if (kind === 'native') attempts.unshift({ no: 1, status: 'superseded' });
        render(
          <JobDetail
            job={makeJob({ currentAttemptNo, attempts, events: displayEvents })}
            events={stored}
          />,
        );
        const actions = screen.getAllByRole('button', { name: 'View subagent execution' });
        expect(renderingWork.trajectory).not.toHaveBeenCalled();
        fireEvent.click(actions.at(-1)!);
        const inspector = screen.getByRole('region', { name: 'Step details' });
        await within(inspector).findByRole('button', { name: 'Close details' });
        expect(renderingWork.trajectory).toHaveBeenCalledTimes(1);
        if (kind === 'native') {
          expect(inspector).toHaveTextContent('worker-one');
          expect(inspector).toHaveTextContent('spawn_agent');
          expect(inspector).toHaveTextContent('Review complete');
          expect(inspector).not.toHaveTextContent('superseded-worker');
          expect(inspector).not.toHaveTextContent('pairing below is not verified');
        } else {
          expect(inspector).toHaveTextContent('This call id was stored more than once');
          expect(inspector).toHaveTextContent('pairing below is not verified');
        }
      },
    );

    it('moves focus into subagent execution details and restores it to the toggle', async () => {
      render(
        <JobDetail
          job={makeJob({ events: activityEvents() })}
          events={
            [
              {
                id: 12,
                job_id: 'ajob_1',
                turn_no: 1,
                attempt_id: 1,
                seq: 1,
                event_type: 'tool_use',
                payload: {
                  id: 'spawn-review-1',
                  name: 'spawn_agent',
                  input: { task_name: 'Review window interactions' },
                },
                created_at: '2026-10-10T12:00:00.000Z',
              },
            ] as never
          }
        />,
      );

      const viewExecution = screen.getByRole('button', { name: 'View subagent execution' });
      viewExecution.focus();
      fireEvent.click(viewExecution);

      const closeDetails = screen.getByRole('button', { name: 'Close details' });
      expect(screen.getByRole('region', { name: 'Step details' })).toBeVisible();
      expect(closeDetails).toHaveFocus();

      fireEvent.keyDown(closeDetails, { key: 'Escape' });
      expect(screen.queryByRole('region', { name: 'Step details' })).not.toBeInTheDocument();
      await waitFor(() =>
        expect(screen.getByRole('button', { name: 'Hide tasks and subagents' })).toHaveFocus(),
      );
    });
  });

  it('keeps the task as the default view behind an icon-only, accessible workspace toggle', () => {
    render(<JobDetail job={makeJob()} />);

    const workspaceButton = screen.getByRole('button', { name: 'Open workspace' });
    const workspacePane = document.getElementById('job-workspace-pane');
    expect(workspaceButton).toHaveTextContent('');
    expect(workspaceButton).toHaveClass('h-9', 'w-9', 'justify-center');
    expect(workspaceButton).toHaveAttribute('aria-expanded', 'false');
    expect(workspaceButton).toHaveAttribute('aria-keyshortcuts', 'Meta+J Control+J');
    expect(workspacePane).toHaveClass('hidden');
    expect(workspacePane).not.toHaveClass('flex');
    expect(screen.queryByRole('region', { name: 'Job workspace' })).not.toBeInTheDocument();
    expect(screen.queryByRole('tab', { name: 'Activity' })).not.toBeInTheDocument();
    expect(screen.queryByRole('tab', { name: 'Environment' })).not.toBeInTheDocument();
    expect(screen.getByText(/with all of its detail/)).toBeInTheDocument();
    expect(screen.getByLabelText('Add a follow-up')).toBeInTheDocument();
  });

  it('defers a large diagnostic history until inspection and avoids derivation work while typing', async () => {
    renderingWork.trajectory.mockClear();
    renderingWork.history.mockClear();
    renderingWork.current.mockClear();
    vi.mocked(getAgentAttemptTelemetry).mockResolvedValue({
      attempt_id: 51,
      attempt_no: 1,
      continuity_mode: null,
      context_status: null,
      duration_ms: null,
      resources: null,
    });
    const historicalJobs = Array.from({ length: 50 }, (_, index) => ({
      id: `past-${index}`,
      state: 'succeeded',
      turn_no: index + 1,
      current_attempt_id: index + 1,
      task_prompt: `Completed task ${index}`,
      published_pr_url: null,
    })) as AgentJobApi[];
    const threadJobs = [
      ...historicalJobs,
      {
        id: 'ajob_1',
        state: 'running',
        turn_no: 51,
        current_attempt_id: 51,
        task_prompt: 'Current task',
        published_pr_url: null,
      } as AgentJobApi,
    ];
    const historicalEvents: AgentThreadEventApi[] = historicalJobs.flatMap((turn, index) =>
      Array.from({ length: 100 }, (_, offset) => ({
        id: index * 100 + offset,
        job_id: turn.id,
        turn_no: turn.turn_no!,
        attempt_id: turn.current_attempt_id!,
        seq: offset,
        event_type: 'message',
        payload: {
          text: `Stored historical reply ${index}/${offset}`,
          native_item_id: `native-reply-${index}`,
        },
        created_at: null,
      })),
    );
    const source: AgentThreadEventApi = {
      id: 6000,
      job_id: 'ajob_1',
      turn_no: 51,
      attempt_id: 51,
      seq: 1,
      event_type: 'tool_use',
      payload: { id: 'review-worker', name: 'spawn_agent', input: { task_name: 'Review scaling' } },
      created_at: null,
    };
    const job = makeJob({
      threadId: 'large-conversation',
      turnNo: 51,
      currentAttemptId: 51,
      historyTurns: historicalJobs.map((turn) => ({
        jobId: turn.id,
        turnNo: turn.turn_no!,
        forkable: false,
        state: 'done',
        events: [{ kind: 'message', text: `Completed task ${turn.turn_no}` }],
        messages: [],
      })),
      events: [
        {
          kind: 'tool_use',
          tool: 'spawn_agent',
          id: 'review-worker',
          input: { task_name: 'Review scaling' },
          detail: 'Review scaling',
          attemptNo: 1,
          sourceEventId: source.id,
        },
      ],
    });
    const events = [...historicalEvents, source];
    const { rerender } = render(<JobDetail job={job} events={events} threadJobs={threadJobs} />);
    await screen.findByRole('button', { name: 'View subagent execution' });
    expect(renderingWork.trajectory).not.toHaveBeenCalled();
    expect(renderingWork.history).toHaveBeenCalledTimes(1);
    expect(renderingWork.current).toHaveBeenCalledTimes(1);
    const composer = screen.getByRole('textbox', { name: 'Add a follow-up' });
    for (const value of ['h', 'he', 'hel', 'hello']) {
      fireEvent.change(composer, { target: { value } });
    }
    expect(composer).toHaveValue('hello');
    expect(renderingWork.trajectory).not.toHaveBeenCalled();
    expect(renderingWork.history).toHaveBeenCalledTimes(1);
    expect(renderingWork.current).toHaveBeenCalledTimes(1);

    fireEvent.click(screen.getByRole('button', { name: 'View subagent execution' }));
    expect(renderingWork.trajectory).toHaveBeenCalledTimes(1);
    expect(screen.getByRole('region', { name: 'Step details' })).toBeInTheDocument();
    await screen.findByRole('button', { name: 'Close details' });
    expect(renderingWork.trajectory).toHaveBeenCalledTimes(1);
    fireEvent.click(screen.getByRole('button', { name: 'Close details' }));
    fireEvent.click(screen.getByRole('button', { name: 'chat' }));

    const reply: AgentThreadEventApi = {
      ...source,
      id: 6001,
      seq: 2,
      event_type: 'message',
      payload: { text: 'Fresh streamed reply' },
    };
    const updatedJob = {
      ...job,
      events: [
        ...job.events,
        {
          kind: 'message' as const,
          text: 'Fresh streamed reply',
          sourceEventId: reply.id,
          attemptNo: 1,
        },
      ],
    };
    rerender(<JobDetail job={updatedJob} events={[...events, reply]} threadJobs={threadJobs} />);
    expect(screen.getByText('Fresh streamed reply')).toBeInTheDocument();
    expect(renderingWork.trajectory).toHaveBeenCalledTimes(1);
    expect(renderingWork.history).toHaveBeenCalledTimes(1);
    expect(renderingWork.current).toHaveBeenCalledTimes(2);

    fireEvent.click(screen.getByRole('button', { name: 'trajectory' }));
    expect(await screen.findByText('Fresh streamed reply')).toBeInTheDocument();
    expect(renderingWork.trajectory).toHaveBeenCalledTimes(2);
    const result: AgentThreadEventApi = {
      ...source,
      id: 6002,
      seq: 3,
      event_type: 'tool_result',
      payload: { tool_use_id: 'review-worker', content: 'Review finished', is_error: false },
    };
    rerender(
      <JobDetail job={updatedJob} events={[...events, reply, result]} threadJobs={threadJobs} />,
    );
    expect(renderingWork.trajectory).toHaveBeenCalledTimes(3);
    fireEvent.click(await screen.findByText('spawn_agent'));
    const inspector = screen.getByRole('region', { name: 'Step details' });
    await within(inspector).findByRole('button', { name: 'Close details' });
    expect(inspector).toHaveTextContent('Review finished');
    expect(renderingWork.trajectory).toHaveBeenCalledTimes(3);
  });

  it('initializes terminal and file panels only when selected and preserves them across closing', async () => {
    vi.mocked(getAgentJobFiles).mockImplementation(async (_jobId, path = '') => {
      if (path === '') {
        return {
          path: '',
          kind: 'directory',
          entries: [{ name: 'preview.txt', path: 'preview.txt', kind: 'file' }],
        };
      }
      return {
        path,
        kind: 'file',
        content: 'retained preview',
        size: 16,
        binary: false,
        truncated: false,
      };
    });
    render(<JobDetail job={makeJob({ state: 'done', threadId: 'retained-thread' })} />);

    expect(screen.queryByLabelText('Workspace terminal')).not.toBeInTheDocument();
    expect(screen.queryByLabelText('Workspace files')).not.toBeInTheDocument();
    expect(listAgentTerminals).not.toHaveBeenCalled();
    expect(getAgentJobFiles).not.toHaveBeenCalled();

    openWorkspace();
    expect(screen.queryByLabelText('Workspace terminal')).not.toBeInTheDocument();
    expect(screen.queryByLabelText('Workspace files')).not.toBeInTheDocument();
    expect(listAgentTerminals).not.toHaveBeenCalled();
    expect(getAgentJobFiles).not.toHaveBeenCalled();

    fireEvent.click(screen.getByRole('tab', { name: 'Terminal' }));
    const terminal = await screen.findByLabelText('Workspace terminal');
    await waitFor(() => expect(listAgentTerminals).toHaveBeenCalledTimes(1));
    fireEvent.click(screen.getByRole('button', { name: 'Close workspace' }));
    expect(screen.getByLabelText('Workspace terminal')).toBe(terminal);
    expect(terminal).not.toBeVisible();
    openWorkspace();
    expect(screen.getByLabelText('Workspace terminal')).toBe(terminal);
    expect(terminal).toBeVisible();
    expect(listAgentTerminals).toHaveBeenCalledTimes(1);
    expect(getAgentJobFiles).not.toHaveBeenCalled();

    fireEvent.click(screen.getByRole('tab', { name: 'Files' }));
    const files = await screen.findByLabelText('Workspace files');
    fireEvent.click(await within(files).findByRole('button', { name: /preview.txt/ }));
    await waitFor(() => expect(files).toHaveTextContent('retained preview'));
    fireEvent.click(screen.getByRole('button', { name: 'Close workspace' }));
    expect(screen.getByLabelText('Workspace files')).toBe(files);
    expect(files).not.toBeVisible();
    openWorkspace();
    expect(screen.getByLabelText('Workspace files')).toBe(files);
    expect(files).toBeVisible();
    expect(getAgentJobFiles).toHaveBeenLastCalledWith('ajob_1', 'preview.txt');
    await waitFor(() => expect(files).toHaveTextContent('retained preview'));
  });

  it.each(['thread', 'account'] as const)(
    'defers the new workspace and clears initialized panels when its %s changes',
    async (scope) => {
      const job = makeJob({ state: 'done', threadId: 'initial-thread' });
      const { rerender } = render(<JobDetail job={job} userId="initial-owner" />);
      openWorkspace();
      fireEvent.click(screen.getByRole('tab', { name: 'Terminal' }));
      const terminal = await screen.findByLabelText('Workspace terminal');
      await waitFor(() => expect(listAgentTerminals).toHaveBeenCalledTimes(1));

      rerender(
        <JobDetail
          job={scope === 'thread' ? { ...job, threadId: 'new-thread' } : job}
          userId={scope === 'account' ? 'new-owner' : 'initial-owner'}
        />,
      );
      expect(terminal).not.toBeInTheDocument();
      expect(screen.queryByLabelText('Workspace terminal')).not.toBeInTheDocument();
      expect(screen.queryByLabelText('Workspace files')).not.toBeInTheDocument();
      expect(screen.getByRole('button', { name: 'Open workspace' })).toHaveAttribute(
        'aria-expanded',
        'false',
      );
      expect(listAgentTerminals).toHaveBeenCalledTimes(1);
      expect(getAgentJobFiles).not.toHaveBeenCalled();

      rerender(<JobDetail job={job} userId="initial-owner" />);
      expect(screen.getByRole('button', { name: 'Open workspace' })).toHaveAttribute(
        'aria-expanded',
        'false',
      );
      expect(screen.queryByLabelText('Workspace terminal')).not.toBeInTheDocument();
      expect(listAgentTerminals).toHaveBeenCalledTimes(1);
    },
  );

  it('opens and closes from a stable control while preserving the selected tab', () => {
    render(<JobDetail job={makeJob()} />);

    const task = screen.getByRole('region', { name: 'Task progress' });
    const workspaceButton = screen.getByRole('button', { name: 'Open workspace' });
    const headerLayout = workspaceButton.parentElement;
    const headerClassName = headerLayout?.className;
    expect(headerLayout).toHaveClass('relative');
    expect(headerLayout).not.toHaveClass('max-w-4xl');
    expect(workspaceButton).toHaveClass('absolute', 'right-6');
    fireEvent.click(workspaceButton);

    const closeButton = screen.getByRole('button', { name: 'Close workspace' });
    expect(closeButton).toBe(workspaceButton);
    expect(closeButton.parentElement).toBe(headerLayout);
    expect(closeButton.parentElement?.className).toBe(headerClassName);
    expect(closeButton).toHaveTextContent('');
    expect(closeButton).toHaveAttribute('aria-expanded', 'true');
    expect(closeButton).toHaveAttribute('aria-controls', 'job-workspace-pane');
    expect(closeButton).toHaveClass('bg-gray-900', 'text-white');
    const workspacePane = screen.getByRole('region', { name: 'Job workspace' });
    expect(workspacePane).toHaveClass('flex');
    expect(workspacePane).not.toHaveClass('hidden');
    expect(screen.getByRole('tab', { name: /Git/ })).toHaveAttribute('aria-selected', 'true');
    expect(task).toBeInTheDocument();
    expect(task).toHaveClass('hidden', 'lg:flex', 'overflow-y-auto');
    expect(workspacePane).toHaveClass('overflow-hidden', 'lg:w-[var(--job-workspace-width)]');

    const terminalTab = screen.getByRole('tab', { name: 'Terminal' });
    fireEvent.click(terminalTab);
    expect(terminalTab).toHaveAttribute('aria-selected', 'true');
    fireEvent.click(terminalTab);
    expect(terminalTab).toHaveAttribute('aria-selected', 'true');
    expect(screen.getByRole('region', { name: 'Job workspace' })).toBeInTheDocument();

    fireEvent.click(screen.getByRole('button', { name: 'Close workspace' }));
    const reopenButton = screen.getByRole('button', { name: 'Open workspace' });
    expect(reopenButton).toBe(workspaceButton);
    expect(reopenButton.parentElement).toBe(headerLayout);
    expect(reopenButton.parentElement?.className).toBe(headerClassName);
    expect(reopenButton).toHaveTextContent('');
    expect(reopenButton).toHaveAttribute('aria-expanded', 'false');
    expect(workspacePane).toHaveClass('hidden');
    expect(workspacePane).not.toHaveClass('flex');
    expect(screen.queryByRole('region', { name: 'Job workspace' })).not.toBeInTheDocument();

    openWorkspace();
    expect(screen.getByRole('tab', { name: 'Terminal' })).toHaveAttribute('aria-selected', 'true');
  });

  it.each([
    { key: 'j', metaKey: true },
    { key: 'J', metaKey: true },
    { key: 'j', ctrlKey: true },
    { key: 'J', ctrlKey: true },
  ])('toggles the workspace and ignores repeats: %j', (chord) => {
    render(<JobDetail job={makeJob()} />);

    expect(fireEvent.keyDown(window, chord)).toBe(false);
    expect(screen.getByRole('button', { name: 'Close workspace' })).toHaveAttribute(
      'aria-expanded',
      'true',
    );
    expect(screen.getByRole('region', { name: 'Job workspace' })).toBeInTheDocument();

    expect(fireEvent.keyDown(window, { ...chord, repeat: true })).toBe(false);
    expect(screen.getByRole('region', { name: 'Job workspace' })).toBeInTheDocument();

    expect(fireEvent.keyDown(window, chord)).toBe(false);
    expect(screen.getByRole('button', { name: 'Open workspace' })).toHaveAttribute(
      'aria-expanded',
      'false',
    );
    expect(screen.queryByRole('region', { name: 'Job workspace' })).not.toBeInTheDocument();

    expect(fireEvent.keyDown(window, { ...chord, repeat: true })).toBe(false);
    expect(screen.queryByRole('region', { name: 'Job workspace' })).not.toBeInTheDocument();
  });

  it.each(['Git', 'Terminal', 'Files'])(
    'preserves the selected %s tab when toggled with the keyboard and button',
    (tab) => {
      render(<JobDetail job={makeJob({ state: 'done' })} />);
      openWorkspace();
      const workspaceTabs = within(screen.getByRole('tablist', { name: 'Workspace views' }));
      fireEvent.click(workspaceTabs.getByRole('tab', { name: new RegExp(`^${tab}`) }));

      fireEvent.keyDown(window, { key: 'j', metaKey: true });
      expect(screen.queryByRole('region', { name: 'Job workspace' })).not.toBeInTheDocument();
      fireEvent.keyDown(window, { key: 'j', ctrlKey: true });
      expect(workspaceTabs.getByRole('tab', { name: new RegExp(`^${tab}`) })).toHaveAttribute(
        'aria-selected',
        'true',
      );

      fireEvent.click(screen.getByRole('button', { name: 'Close workspace' }));
      fireEvent.keyDown(window, { key: 'j', metaKey: true });
      expect(workspaceTabs.getByRole('tab', { name: new RegExp(`^${tab}`) })).toHaveAttribute(
        'aria-selected',
        'true',
      );
    },
  );

  it.each([
    { key: 'j' },
    { key: 'j', metaKey: true, ctrlKey: true },
    { key: 'j', metaKey: true, altKey: true },
    { key: 'j', ctrlKey: true, altKey: true },
    { key: 'j', metaKey: true, shiftKey: true },
    { key: 'j', ctrlKey: true, shiftKey: true },
    { key: 'b', metaKey: true },
    { key: 'b', ctrlKey: true },
  ])('leaves nonmatching keyboard input alone: %j', (chord) => {
    render(<JobDetail job={makeJob()} />);

    expect(fireEvent.keyDown(window, chord)).toBe(true);
    expect(screen.queryByRole('region', { name: 'Job workspace' })).not.toBeInTheDocument();
  });

  it.each([{ metaKey: true }, { ctrlKey: true }])(
    'captures the workspace shortcut before a focused editor consumes it: %j',
    (modifier) => {
      render(<JobDetail job={makeJob()} />);
      const composer = screen.getByLabelText('Add a follow-up');
      // Terminal/editor handlers may stop bubbling or forward Ctrl+J as a newline.
      const editorKeyDown = vi.fn((event: KeyboardEvent) => event.stopPropagation());
      composer.addEventListener('keydown', editorKeyDown);
      composer.focus();

      try {
        expect(fireEvent.keyDown(composer, { key: 'j', ...modifier })).toBe(false);
        expect(screen.getByRole('region', { name: 'Job workspace' })).toBeInTheDocument();
        expect(fireEvent.keyDown(composer, { key: 'j', ...modifier, repeat: true })).toBe(false);
        expect(editorKeyDown).not.toHaveBeenCalled();

        expect(fireEvent.keyDown(composer, { key: 'j' })).toBe(true);
        expect(editorKeyDown).toHaveBeenCalledOnce();
        expect(screen.getByRole('region', { name: 'Job workspace' })).toBeInTheDocument();
      } finally {
        composer.removeEventListener('keydown', editorKeyDown);
      }
    },
  );

  it('removes the workspace shortcut when leaving the task', () => {
    const { unmount } = render(<JobDetail job={makeJob()} />);
    unmount();

    expect(fireEvent.keyDown(window, { key: 'j', metaKey: true })).toBe(true);
    expect(fireEvent.keyDown(window, { key: 'j', ctrlKey: true })).toBe(true);
  });

  it('resizes the workspace pane by dragging the seam, and remembers the width', () => {
    render(<JobDetail job={makeJob()} />);

    expect(screen.queryByRole('separator', { name: 'Resize workspace' })).not.toBeInTheDocument();
    openWorkspace();

    const pane = screen.getByRole('region', { name: 'Job workspace' });
    const seam = screen.getByRole('separator', { name: 'Resize workspace' });
    expect(seam).toHaveAttribute('aria-controls', 'job-workspace-pane');
    expect(pane.style.getPropertyValue('--job-workspace-width')).toBe('560px');

    // Dragging the seam left widens the workspace and narrows the transcript.
    fireEvent.pointerDown(seam, { button: 0, clientX: 800 });
    fireEvent.pointerMove(window, { clientX: 700 });
    fireEvent.pointerUp(window, { clientX: 700 });

    expect(pane.style.getPropertyValue('--job-workspace-width')).toBe('660px');
    expect(window.localStorage.getItem(WORKSPACE_WIDTH_STORAGE_KEY)).toBe('660');

    cleanup();
    render(<JobDetail job={makeJob()} />);
    openWorkspace();

    expect(
      screen
        .getByRole('region', { name: 'Job workspace' })
        .style.getPropertyValue('--job-workspace-width'),
    ).toBe('660px');
  });

  it('shows a selectable diff-first Git workspace with review and commit tabs', async () => {
    render(
      <JobDetail
        job={makeJob({
          state: 'done',
          prLabel: 'draft PR 44',
          prUrl: 'https://github.com/owner/repository/pull/44',
        })}
      />,
    );

    openWorkspace();

    await waitFor(() =>
      expect(screen.getByLabelText('Git workspace')).toHaveTextContent('Unavailable'),
    );
    expect(screen.getByLabelText('Git workspace')).toHaveTextContent('src/example.ts');
    fireEvent.click(screen.getByRole('tab', { name: 'review' }));
    expect(screen.getByRole('link', { name: 'Open pull request' })).toHaveAttribute(
      'href',
      'https://github.com/owner/repository/pull/44',
    );
    fireEvent.click(screen.getByRole('tab', { name: 'diff' }));
    expect(screen.getByText('+fixed')).toBeInTheDocument();
    await waitFor(() => expect(getAgentJobGit).toHaveBeenCalledWith('ajob_1'));
  });

  it('navigates Git views from the keyboard and associates the selected panel', async () => {
    render(<JobDetail job={makeJob({ state: 'done' })} />);
    openWorkspace();
    const tabs = within(screen.getByRole('tablist', { name: 'Git views' })).getAllByRole('tab');
    tabs[0].focus();
    expect(tabs.filter((tab) => tab.tabIndex === 0)).toEqual([tabs[0]]);
    fireEvent.keyDown(tabs[0], { key: 'ArrowLeft' });
    expect(tabs[2]).toHaveFocus();
    expect(tabs[2]).toHaveAttribute('aria-selected', 'true');
    expect(document.getElementById(tabs[2].getAttribute('aria-controls')!)).toHaveAttribute(
      'aria-labelledby',
      tabs[2].id,
    );
    fireEvent.keyDown(tabs[2], { key: 'Home' });
    expect(tabs[0]).toHaveFocus();
    fireEvent.keyDown(tabs[0], { key: 'ArrowRight' });
    expect(tabs[1]).toHaveFocus();
    fireEvent.keyDown(tabs[1], { key: 'End' });
    expect(tabs[2]).toHaveFocus();
  });

  it('selects full changed paths without a tall file list and exposes a keyboard-scrollable diff', async () => {
    render(
      <JobDetail
        job={makeJob({
          state: 'done',
          diffFileDetails: [
            {
              path: 'src/auth/index.ts',
              add: 1,
              del: 0,
              lines: [{ marker: 'add', text: '+auth' }],
            },
            { path: 'src/ui/index.ts', add: 1, del: 0, lines: [{ marker: 'add', text: '+ui' }] },
          ],
        })}
      />,
    );
    openWorkspace();
    const picker = screen.getByRole('combobox', { name: 'Changed file' });
    expect(
      within(picker)
        .getAllByRole('option')
        .map((option) => option.textContent),
    ).toEqual(['src/auth/index.ts (+1 −0)', 'src/ui/index.ts (+1 −0)']);
    fireEvent.change(picker, { target: { value: 'src/ui/index.ts' } });
    const diff = screen.getByRole('region', { name: 'Diff for src/ui/index.ts' });
    expect(diff).toHaveTextContent('+ui');
    expect(diff).not.toHaveTextContent('+auth');
    expect(diff).toHaveAttribute('tabindex', '0');
  });

  it('returns to the conversation from the workspace without losing its selected view', async () => {
    render(<JobDetail job={makeJob({ state: 'done' })} />);
    const trigger = screen.getByRole('button', { name: 'Open workspace' });
    trigger.focus();
    fireEvent.click(trigger);
    fireEvent.click(
      within(screen.getByRole('tablist', { name: 'Workspace views' })).getByRole('tab', {
        name: 'Files',
      }),
    );
    fireEvent.click(screen.getByRole('button', { name: 'Back to conversation' }));
    expect(screen.queryByRole('region', { name: 'Job workspace' })).not.toBeInTheDocument();
    await waitFor(() => expect(trigger).toHaveFocus());
    fireEvent.click(trigger);
    expect(
      within(screen.getByRole('tablist', { name: 'Workspace views' })).getByRole('tab', {
        name: 'Files',
      }),
    ).toHaveAttribute('aria-selected', 'true');
  });

  it('says nothing in the Git header while the live workspace is still answering', async () => {
    let settle: (value: Awaited<ReturnType<typeof getAgentJobGit>>) => void = () => {};
    vi.mocked(getAgentJobGit)
      .mockReset()
      .mockReturnValue(
        new Promise((resolve) => {
          settle = resolve;
        }),
      );

    render(<JobDetail job={makeJob({ state: 'done' })} />);
    openWorkspace();

    const git = screen.getByLabelText('Git workspace');
    expect(within(git).getByRole('button', { name: 'Refreshing…' })).toBeDisabled();
    expect(git).not.toHaveTextContent('Loading');
    expect(git).not.toHaveTextContent('Unavailable');

    await act(async () => {
      settle({
        available: true,
        branch: 'agent/ajob_1',
        changes: [],
        patch: '',
        commits: [],
      });
    });

    expect(within(git).getByRole('button', { name: 'Refresh' })).toBeEnabled();
    expect(git).toHaveTextContent('agent/ajob_1');
    expect(git).not.toHaveTextContent('Unavailable');
  });

  it('loads the terminal without warnings once the current workspace is ready', async () => {
    const view = render(
      <JobDetail
        job={makeJob({
          attempts: [
            { no: 1, status: 'superseded' },
            { no: 2, status: 'live' },
          ],
          currentAttemptNo: 2,
          state: 'queued',
          events: [{ kind: 'lifecycle', text: 'workspace_ready', attemptNo: 1 }],
        })}
      />,
    );

    openWorkspace();
    fireEvent.click(screen.getByRole('tab', { name: 'Terminal' }));

    const terminal = await screen.findByLabelText('Workspace terminal');
    expect(terminal).toHaveTextContent('Loading terminals…');
    expect(listAgentTerminals).not.toHaveBeenCalled();
    expect(within(terminal).queryByRole('note')).not.toBeInTheDocument();

    view.rerender(
      <JobDetail
        job={makeJob({
          events: [
            { kind: 'lifecycle', text: 'workspace_ready', attemptNo: 1 },
            { kind: 'lifecycle', text: 'started', attemptNo: 2 },
          ],
          attempts: [
            { no: 1, status: 'superseded' },
            { no: 2, status: 'live' },
          ],
          currentAttemptNo: 2,
        })}
      />,
    );
    expect(listAgentTerminals).not.toHaveBeenCalled();

    view.rerender(
      <JobDetail
        job={makeJob({
          events: [
            { kind: 'lifecycle', text: 'workspace_ready', attemptNo: 1 },
            { kind: 'lifecycle', text: 'started', attemptNo: 2 },
            { kind: 'lifecycle', text: 'workspace_ready', attemptNo: 2 },
          ],
          attempts: [
            { no: 1, status: 'superseded' },
            { no: 2, status: 'live' },
          ],
          currentAttemptNo: 2,
        })}
      />,
    );

    await waitFor(() =>
      expect(listAgentTerminals).toHaveBeenCalledWith('ajob_1', { includeLimits: true }),
    );
    expect(terminal).not.toHaveTextContent('pytest -q');
    expect(terminal).not.toHaveTextContent('2 passed');
    expect(terminal).not.toHaveTextContent('Terminal input is available after the agent finishes');
    expect(within(terminal).queryByRole('note')).not.toBeInTheDocument();
    expect(within(terminal).getByRole('button', { name: 'New terminal' })).toBeEnabled();
    expect(listAgentTerminals).toHaveBeenCalledTimes(1);

    view.rerender(
      <JobDetail
        job={makeJob({
          events: [
            { kind: 'lifecycle', text: 'workspace_ready', attemptNo: 2 },
            { kind: 'lifecycle', text: 'workspace_finalizing', attemptNo: 2 },
          ],
          attempts: [
            { no: 1, status: 'superseded' },
            { no: 2, status: 'live' },
          ],
          currentAttemptNo: 2,
        })}
      />,
    );
    expect(terminal).toHaveTextContent('Loading terminals…');
    expect(within(terminal).queryByRole('note')).not.toBeInTheDocument();
  });

  it('loads terminals for settled jobs without requiring a checkout event', async () => {
    render(<JobDetail job={makeJob({ state: 'done', events: [] })} />);

    openWorkspace();
    fireEvent.click(screen.getByRole('tab', { name: 'Terminal' }));

    await waitFor(() =>
      expect(listAgentTerminals).toHaveBeenCalledWith('ajob_1', { includeLimits: true }),
    );
    expect(screen.getByRole('button', { name: 'New terminal' })).toBeEnabled();
  });

  it('loads Files lazily and safely refuses to preview a symlink', async () => {
    vi.mocked(getAgentJobFiles)
      .mockResolvedValueOnce({
        path: '',
        kind: 'directory',
        entries: [{ name: 'latest', path: 'latest', kind: 'symlink', size: null, status: null }],
      })
      .mockResolvedValueOnce({ path: 'latest', kind: 'symlink' });
    render(<JobDetail job={makeJob()} />);

    expect(getAgentJobFiles).not.toHaveBeenCalled();
    openWorkspace();
    fireEvent.click(screen.getByRole('tab', { name: 'Files' }));

    await waitFor(() => expect(getAgentJobFiles).toHaveBeenCalledWith('ajob_1', ''));
    fireEvent.click(await screen.findByRole('button', { name: /latest/ }));
    await waitFor(() => expect(getAgentJobFiles).toHaveBeenCalledWith('ajob_1', 'latest'));
    expect(await screen.findByText('Symlinks are not previewed.')).toBeInTheDocument();
  });

  it('opens a changed text file and handles nullable file metadata', async () => {
    vi.mocked(getAgentJobFiles)
      .mockResolvedValueOnce({
        path: '',
        kind: 'directory',
        entries: [
          {
            name: 'README.md',
            path: 'README.md',
            kind: 'file',
            size: null,
            status: 'modified',
          },
        ],
      })
      .mockResolvedValueOnce({
        path: 'README.md',
        kind: 'file',
        content: '# Updated',
        size: null,
        binary: false,
        truncated: false,
        status: 'modified',
      });
    render(<JobDetail job={makeJob()} />);
    openWorkspace();
    fireEvent.click(screen.getByRole('tab', { name: 'Files' }));

    fireEvent.click(await screen.findByRole('button', { name: /README\.md/ }));

    expect(await screen.findByText('# Updated')).toBeInTheDocument();
    expect(screen.getByLabelText('Workspace files')).not.toHaveTextContent('null B');
  });

  it('reads a live workspace file without offering to edit it', async () => {
    vi.mocked(getAgentJobFiles)
      .mockResolvedValueOnce({
        path: '',
        kind: 'directory',
        entries: [{ name: 'README.md', path: 'README.md', kind: 'file' }],
        writable: true,
        source: 'workspace',
      })
      .mockResolvedValue({
        path: 'README.md',
        kind: 'file',
        content: '# Before',
        size: 8,
        binary: false,
        truncated: false,
        writable: true,
        source: 'workspace',
      });
    render(<JobDetail job={makeJob()} />);
    openWorkspace();
    fireEvent.click(screen.getByRole('tab', { name: 'Files' }));
    fireEvent.click(await screen.findByRole('button', { name: /README\.md/ }));

    const pane = screen.getByLabelText('Workspace files');
    await waitFor(() => expect(pane).toHaveTextContent('# Before'));
    // Writable is a fact about the workspace, not an invitation: the pane is a
    // reader, so nothing here can change the run's files.
    expect(pane).not.toHaveTextContent('Live workspace');
    expect(screen.queryByRole('button', { name: 'Edit' })).not.toBeInTheDocument();
    expect(screen.queryByRole('button', { name: 'Save' })).not.toBeInTheDocument();
    expect(screen.queryByRole('textbox', { name: 'Edit README.md' })).not.toBeInTheDocument();
    expect(writeAgentJobFile).not.toHaveBeenCalled();
  });

  it('expands a directory in place and keeps the tree around the open file', async () => {
    const files = async (_jobId: string, path = ''): Promise<AgentJobFilesApi> => {
      if (path === '')
        return {
          path: '',
          kind: 'directory',
          entries: [
            { name: 'README.md', path: 'README.md', kind: 'file', size: 12 },
            { name: 'src', path: 'src', kind: 'directory' },
          ],
        };
      if (path === 'src')
        return {
          path: 'src',
          kind: 'directory',
          entries: [
            { name: 'example.ts', path: 'src/example.ts', kind: 'file', status: 'modified' },
          ],
        };
      return {
        path: 'src/example.ts',
        kind: 'file',
        content: 'export const value = 1;',
        size: 24,
        binary: false,
        truncated: false,
      };
    };
    vi.mocked(getAgentJobFiles).mockImplementation(files);
    render(<JobDetail job={makeJob()} />);
    openWorkspace();
    fireEvent.click(screen.getByRole('tab', { name: 'Files' }));

    // A directory expands beneath itself rather than replacing the listing.
    fireEvent.click(await screen.findByRole('button', { name: /src/ }));
    await waitFor(() => expect(getAgentJobFiles).toHaveBeenCalledWith('ajob_1', 'src'));
    expect(await screen.findByRole('button', { name: /example\.ts/ })).toBeInTheDocument();
    expect(screen.getByRole('button', { name: /README\.md/ })).toBeInTheDocument();

    fireEvent.click(screen.getByRole('button', { name: /example\.ts/ }));

    // The viewer highlights the source, so assert on the pane's whole text.
    await waitFor(() =>
      expect(screen.getByLabelText('Workspace files')).toHaveTextContent('export const value = 1;'),
    );
    // The sibling entries survive opening a file — the tree is not replaced.
    expect(screen.getByRole('button', { name: /README\.md/ })).toBeInTheDocument();
  });

  it('gives the tree the whole pane when the workspace is too narrow for two', async () => {
    // The pane is user-resizable, so the layout follows the panel's own width.
    // jsdom reports 0 for every box; pretend the pane was dragged narrow.
    const clientWidth = Object.getOwnPropertyDescriptor(HTMLElement.prototype, 'clientWidth');
    Object.defineProperty(HTMLElement.prototype, 'clientWidth', {
      configurable: true,
      get: () => 420,
    });
    try {
      vi.mocked(getAgentJobFiles)
        .mockResolvedValueOnce({
          path: '',
          kind: 'directory',
          entries: [{ name: 'README.md', path: 'README.md', kind: 'file' }],
        })
        .mockResolvedValue({
          path: 'README.md',
          kind: 'file',
          content: '# Narrow',
          size: 8,
          binary: false,
          truncated: false,
        });
      render(<JobDetail job={makeJob()} />);
      openWorkspace();
      fireEvent.click(screen.getByRole('tab', { name: 'Files' }));
      const row = await screen.findByRole('button', { name: /README\.md/ });
      row.focus();
      fireEvent.click(row);

      // The file takes the pane rather than sharing it with a 200px sidebar…
      await waitFor(() =>
        expect(screen.getByLabelText('Workspace files')).toHaveTextContent('# Narrow'),
      );
      expect(screen.queryByRole('button', { name: /README\.md/ })).not.toBeInTheDocument();
      expect(screen.getByRole('region', { name: 'Preview of README.md' })).toHaveFocus();

      // …and the header button walks back to the tree instead of hiding it.
      const back = screen.getByRole('button', { name: 'Back to file list' });
      expect(back).toHaveTextContent('Back');
      fireEvent.click(back);
      expect(await screen.findByRole('button', { name: /README\.md/ })).toHaveFocus();
    } finally {
      if (clientWidth) Object.defineProperty(HTMLElement.prototype, 'clientWidth', clientWidth);
      else delete (HTMLElement.prototype as { clientWidth?: number }).clientWidth;
    }
  });

  it('opens a changed file from the Changes list', async () => {
    vi.mocked(getAgentJobFiles)
      .mockResolvedValueOnce({ path: '', kind: 'directory', entries: [] })
      .mockResolvedValueOnce({
        path: 'src/example.ts',
        kind: 'file',
        content: 'const fixed = true;',
        size: 20,
        binary: false,
        truncated: false,
        status: 'modified',
      });
    render(<JobDetail job={makeJob()} />);
    openWorkspace();
    fireEvent.click(screen.getByRole('tab', { name: 'Files' }));
    fireEvent.click(await screen.findByRole('tab', { name: /Changes/ }));

    const changedFile = screen.getByRole('button', { name: 'src/example.ts' });
    expect(within(changedFile).getByText('src')).toBeVisible();
    fireEvent.click(changedFile);

    await waitFor(() => expect(getAgentJobFiles).toHaveBeenCalledWith('ajob_1', 'src/example.ts'));
    expect(screen.getByRole('region', { name: 'Preview of src/example.ts' })).toBeVisible();
    expect(screen.getByLabelText('Workspace files')).toHaveTextContent('modified');
  });

  it('refreshes an open Files workspace once a running job finishes', async () => {
    vi.mocked(getAgentJobFiles).mockResolvedValue({ path: '', kind: 'directory', entries: [] });
    const { rerender } = render(<JobDetail job={makeJob({ state: 'running' })} />);
    openWorkspace();
    fireEvent.click(screen.getByRole('tab', { name: 'Files' }));
    await waitFor(() => expect(getAgentJobFiles).toHaveBeenCalledTimes(1));

    rerender(<JobDetail job={makeJob({ state: 'done' })} />);

    await waitFor(() => expect(getAgentJobFiles).toHaveBeenCalledTimes(2));
  });

  it('copies a message to the clipboard', async () => {
    const writeText = vi.fn().mockResolvedValue(undefined);
    Object.defineProperty(navigator, 'clipboard', { value: { writeText }, configurable: true });
    render(<JobDetail job={makeJob({ threadMessages: HISTORY })} />);

    fireEvent.click(screen.getAllByRole('button', { name: 'Copy message' })[0]);

    await waitFor(() => expect(writeText).toHaveBeenCalledWith('Earlier question'));
  });

  it('explains that a stopped run can continue from saved intermediate work', () => {
    render(<JobDetail job={makeJob({ state: 'cancelled' })} />);

    expect(screen.getByText(/send a follow-up to continue/i)).toBeInTheDocument();
    expect(screen.getByText(/restores any saved changes/i)).toBeInTheDocument();
  });

  it('does not offer a follow-up after an admitted turn lost native context', () => {
    render(<JobDetail job={makeJob({ state: 'cancelled', contextStatus: 'lost' })} />);

    expect(screen.getByText(/We couldn’t reconnect this chat/i)).toBeInTheDocument();
    expect(screen.getByRole('textbox', { name: 'Add a follow-up' })).toBeDisabled();
    expect(screen.getByRole('button', { name: /^(Send|Queue)$/ })).toBeDisabled();
    expect(screen.queryByText(/restores any saved changes/i)).toBeNull();
  });

  it('does not offer a follow-up from an older turn after the membership is lost', () => {
    render(
      <JobDetail job={makeJob({ state: 'done', contextStatus: 'live', nativeState: 'lost' })} />,
    );

    expect(screen.getByLabelText('Add a follow-up')).toBeDisabled();
    expect(screen.getByText(/We couldn’t reconnect this chat/)).toBeInTheDocument();
    expect(screen.getByText(/Start a new chat ·/)).toBeInTheDocument();
    expect(screen.getByRole('link', { name: 'New chat' })).toHaveAttribute('href', '/');
  });

  it('forks the settled current native session', async () => {
    vi.mocked(forkAgentJob).mockResolvedValue({ id: 'ajob_fork' } as never);
    vi.spyOn(globalThis, 'confirm').mockReturnValue(true);
    const { rerender } = render(
      <JobDetail
        job={makeJob({
          state: 'done',
          sandboxId: 'asbx_1',
          workspaceShared: true,
          nativeForkAvailable: true,
          events: [
            { kind: 'message', text: 'Intermediate update', attemptNo: 1 },
            { kind: 'message', text: 'Final answer', attemptNo: 1 },
          ],
        })}
      />,
    );

    expect(screen.getAllByRole('button', { name: 'Fork from here' })).toHaveLength(1);
    fireEvent.click(screen.getByRole('button', { name: 'Fork from here' }));

    await waitFor(() => expect(forkAgentJob).toHaveBeenCalledWith('ajob_1'));
    expect(navigation.push).toHaveBeenCalledWith('/ajob_fork');
    expect(screen.getByRole('button', { name: 'Fork from here' })).toBeDisabled();
    fireEvent.click(screen.getByRole('button', { name: 'Fork from here' }));
    expect(forkAgentJob).toHaveBeenCalledTimes(1);

    rerender(
      <JobDetail
        job={makeJob({
          id: 'ajob_fork',
          state: 'done',
          sandboxId: 'asbx_1',
          workspaceShared: true,
          nativeForkAvailable: true,
          events: [{ kind: 'message', text: 'Forked answer', attemptNo: 1 }],
        })}
      />,
    );
    await waitFor(() =>
      expect(screen.getByRole('button', { name: 'Fork from here' })).not.toBeDisabled(),
    );
  });

  it('does not fork the latest native turn from a superseded attempt view', () => {
    render(
      <JobDetail
        job={makeJob({
          state: 'done',
          sandboxId: 'asbx_1',
          workspaceShared: true,
          nativeForkAvailable: true,
          attempts: [
            { no: 1, status: 'superseded', note: 'lease expired' },
            { no: 2, status: 'finished' },
          ],
          currentAttemptNo: undefined,
          events: [
            { kind: 'message', text: 'Superseded answer', attemptNo: 1 },
            { kind: 'message', text: 'Final answer', attemptNo: 2 },
          ],
        })}
      />,
    );

    fireEvent.click(screen.getByText('Execution history (2)'));
    fireEvent.click(screen.getByRole('button', { name: '1 · superseded' }));
    expect(screen.queryByRole('button', { name: 'Fork from here' })).not.toBeInTheDocument();

    fireEvent.click(screen.getByRole('button', { name: '2 · finished' }));
    expect(screen.getByRole('button', { name: 'Fork from here' })).toBeInTheDocument();
  });

  it('offers current-session Fork when a successful run produced no pull request', () => {
    render(
      <JobDetail
        job={makeJob({
          state: 'needs_review',
          sandboxId: 'asbx_1',
          workspaceShared: true,
          nativeForkAvailable: true,
        })}
      />,
    );

    expect(screen.getByRole('button', { name: 'Fork from here' })).toBeInTheDocument();
  });

  it('forks a historical successful assistant boundary in the shared workspace', async () => {
    vi.mocked(forkAgentJob).mockResolvedValue({ id: 'ajob_historical_fork' } as never);
    vi.spyOn(globalThis, 'confirm').mockReturnValue(true);
    render(
      <JobDetail
        job={makeJob({
          state: 'done',
          turnNo: 2,
          sandboxId: 'asbx_1',
          workspaceShared: true,
          nativeForkAvailable: true,
          threadMessages: HISTORY,
          historyTurns: [
            {
              jobId: 'ajob_0',
              turnNo: 1,
              forkable: true,
              messages: HISTORY,
              events: [{ kind: 'message', text: 'Earlier answer', attemptNo: 1 }],
            },
          ],
        })}
      />,
    );

    fireEvent.click(
      within(screen.getByLabelText('Turn 1')).getByRole('button', {
        name: 'Fork from here',
      }),
    );

    await waitFor(() => expect(forkAgentJob).toHaveBeenCalledWith('ajob_0'));
    expect(navigation.push).toHaveBeenCalledWith('/ajob_historical_fork');
  });

  it('offers exactly one Fork action at the final assistant boundary of a turn', async () => {
    vi.mocked(forkAgentJob).mockResolvedValue({ id: 'ajob_boundary_fork' } as never);
    vi.spyOn(globalThis, 'confirm').mockReturnValue(true);
    render(
      <JobDetail
        job={makeJob({
          state: 'done',
          turnNo: 2,
          sandboxId: 'asbx_1',
          workspaceShared: true,
          nativeForkAvailable: true,
          historyTurns: [
            {
              jobId: 'ajob_0',
              turnNo: 1,
              forkable: true,
              messages: HISTORY,
              events: [
                { kind: 'message', text: 'Intermediate assistant update', attemptNo: 1 },
                {
                  kind: 'tool_use',
                  tool: 'Read',
                  detail: 'README.md',
                  output: [],
                  attemptNo: 1,
                },
                { kind: 'message', text: 'Final settled answer', attemptNo: 1 },
              ],
            },
          ],
        })}
      />,
    );

    const historicalTurn = within(screen.getByLabelText('Turn 1'));
    expect(historicalTurn.getAllByRole('button', { name: 'Fork from here' })).toHaveLength(1);
    expect(historicalTurn.queryByRole('button', { name: 'Edit & rewind' })).not.toBeInTheDocument();
    fireEvent.click(historicalTurn.getByRole('button', { name: 'Fork from here' }));

    await waitFor(() => expect(forkAgentJob).toHaveBeenCalledWith('ajob_0'));
  });

  it('skips a failed turn and rewinds its prompt from the nearest successful boundary', async () => {
    vi.mocked(forkAgentJob).mockResolvedValue({ id: 'ajob_rewind_failed' } as never);
    vi.spyOn(globalThis, 'confirm').mockReturnValue(true);
    render(
      <JobDetail
        job={makeJob({
          state: 'done',
          turnNo: 3,
          sandboxId: 'asbx_1',
          workspaceShared: true,
          nativeForkAvailable: true,
          historyTurns: [
            {
              jobId: 'ajob_success',
              turnNo: 1,
              forkable: true,
              messages: HISTORY,
              events: [{ kind: 'message', text: 'Settled answer', attemptNo: 1 }],
            },
            {
              jobId: 'ajob_failed',
              turnNo: 2,
              forkable: false,
              messages: [
                {
                  id: 3,
                  role: 'user',
                  content: 'Retry this failed prompt',
                  jobId: 'ajob_failed',
                  createdAt: null,
                },
                {
                  id: 4,
                  role: 'assistant',
                  content: 'Partial failed output',
                  jobId: 'ajob_failed',
                  createdAt: null,
                },
              ],
              events: [{ kind: 'message', text: 'Partial failed output', attemptNo: 1 }],
            },
          ],
        })}
      />,
    );

    const failedTurn = within(screen.getByLabelText('Turn 2'));
    expect(failedTurn.queryByRole('button', { name: 'Fork from here' })).not.toBeInTheDocument();
    fireEvent.click(failedTurn.getByRole('button', { name: 'Edit & rewind' }));

    await waitFor(() => expect(forkAgentJob).toHaveBeenCalledWith('ajob_success'));
    expect(sessionStorage.getItem('agent-rewind-draft-ajob_rewind_failed')).toBe(
      'Retry this failed prompt',
    );
  });

  it('forks before a user prompt and carries it to the child as a one-shot edit draft', async () => {
    vi.mocked(forkAgentJob).mockResolvedValue({ id: 'ajob_rewind' } as never);
    vi.spyOn(globalThis, 'confirm').mockReturnValue(true);
    const historicalTurn = {
      jobId: 'ajob_0',
      turnNo: 1,
      forkable: true,
      messages: HISTORY,
      events: [{ kind: 'message' as const, text: 'Earlier answer', attemptNo: 1 }],
    };
    const { rerender } = render(
      <JobDetail
        job={makeJob({
          state: 'done',
          turnNo: 2,
          sandboxId: 'asbx_1',
          workspaceShared: true,
          nativeForkAvailable: true,
          threadMessages: HISTORY,
          historyTurns: [historicalTurn],
        })}
      />,
    );

    fireEvent.click(screen.getByRole('button', { name: 'Edit & rewind' }));

    await waitFor(() => expect(forkAgentJob).toHaveBeenCalledWith('ajob_0'));
    expect(sessionStorage.getItem('agent-rewind-draft-ajob_rewind')).toBe(
      'Current task\nwith all of its detail.',
    );
    expect(navigation.push).toHaveBeenCalledWith('/ajob_rewind');

    rerender(
      <JobDetail
        job={makeJob({
          id: 'ajob_rewind',
          state: 'done',
          turnNo: 1,
          sandboxId: 'asbx_1',
          workspaceShared: true,
          nativeForkAvailable: true,
        })}
      />,
    );
    await waitFor(() =>
      expect(screen.getByRole('textbox', { name: 'Add a follow-up' })).toHaveValue(
        'Current task\nwith all of its detail.',
      ),
    );
    expect(sessionStorage.getItem('agent-rewind-draft-ajob_rewind')).toBeNull();
  });

  it('does not offer a synthetic fork when no retained sandbox exists', () => {
    render(<JobDetail job={makeJob({ threadMessages: HISTORY })} />);

    expect(screen.queryByRole('button', { name: 'Fork from here' })).not.toBeInTheDocument();
    expect(screen.queryByRole('button', { name: 'Edit & rewind' })).not.toBeInTheDocument();
  });

  // `/goal` on a conversation that already exists is the thread's own endpoint:
  // the owner's set is the authorization, and the platform schedules the first
  // round from it. Nothing is sent to the model, so nothing has to be typed
  // twice.
  describe('the goal command', () => {
    const OBJECTIVE = 'make the failing suite pass';

    async function composer(): Promise<HTMLElement> {
      render(
        <JobDetail
          job={makeJob({
            threadId: 'thread_9',
            goal: {
              objective: 'make `pytest` pass',
              state: 'active',
              revision: 4,
              roundsUsed: 2,
              maxRounds: 10,
            },
          })}
        />,
      );
      return screen.findByLabelText('Add a follow-up');
    }

    it('sets the conversation goal from /goal <objective>, without sending a message', async () => {
      vi.mocked(setAgentGoal).mockResolvedValue({
        thread_id: 'thread_9',
        goal: null,
        revision: 5,
      });
      const box = await composer();
      fireEvent.change(box, { target: { value: `/goal ${OBJECTIVE}` } });
      fireEvent.keyDown(box, { key: 'Enter' });

      await waitFor(() =>
        expect(setAgentGoal).toHaveBeenCalledWith('thread_9', {
          // No round bound: the goal the owner asked for is bounded by nothing,
          // and the field is absent rather than filled with a default.
          goal: { objective: OBJECTIVE },
          // The revision this page read, so a write from another tab is refused
          // rather than silently overwritten.
          revision: 4,
        }),
      );
      expect(vi.mocked(setAgentGoal).mock.calls[0][1].goal).not.toHaveProperty('max_rounds');
      // The command is not a prompt: the goal starts the work.
      expect(submitThreadMessage).not.toHaveBeenCalled();
      expect(navigation.push).not.toHaveBeenCalled();
      // Consumed, so the objective is not also left in the box.
      await waitFor(() => expect(box).toHaveValue(''));
    });

    it('reads the revision first when the conversation has no goal yet', async () => {
      vi.mocked(getAgentGoal).mockResolvedValue({ thread_id: 'thread_9', goal: null, revision: 7 });
      vi.mocked(setAgentGoal).mockResolvedValue({
        thread_id: 'thread_9',
        goal: null,
        revision: 8,
      });
      render(<JobDetail job={makeJob({ threadId: 'thread_9' })} />);
      const box = await screen.findByLabelText('Add a follow-up');
      fireEvent.change(box, { target: { value: `/goal ${OBJECTIVE}` } });
      fireEvent.keyDown(box, { key: 'Enter' });

      // Sending the write with no revision at all would mean "overwrite
      // whatever is there", which is how a first set loses someone else's.
      await waitFor(() => expect(getAgentGoal).toHaveBeenCalledWith('thread_9'));
      await waitFor(() =>
        expect(setAgentGoal).toHaveBeenCalledWith('thread_9', {
          goal: { objective: OBJECTIVE },
          revision: 7,
        }),
      );
    });

    it('asks for the objective when /goal carries none', async () => {
      const box = await composer();
      fireEvent.change(box, { target: { value: '/goal ' } });
      fireEvent.keyDown(box, { key: 'Enter' });

      // Nothing created, nothing sent, and nothing opened over the chat: the
      // token stays where the owner can finish the sentence.
      expect(await screen.findByText(/Add what .done. means/)).toBeInTheDocument();
      expect(screen.queryByRole('dialog')).not.toBeInTheDocument();
      expect(box).toHaveValue('/goal ');
      expect(setAgentGoal).not.toHaveBeenCalled();
      expect(submitThreadMessage).not.toHaveBeenCalled();
    });

    it('edits the goal on the band, in place, with no modal over the chat', async () => {
      vi.mocked(setAgentGoal).mockResolvedValue({
        thread_id: 'thread_9',
        goal: null,
        revision: 5,
      });
      await composer();
      fireEvent.click(screen.getByRole('button', { name: 'Goal options' }));
      fireEvent.click(screen.getByRole('menuitem', { name: 'Edit goal' }));

      expect(screen.queryByRole('dialog')).not.toBeInTheDocument();
      fireEvent.change(screen.getByLabelText('Objective'), {
        target: { value: 'ship the release notes' },
      });
      fireEvent.click(screen.getByRole('button', { name: 'Save' }));

      await waitFor(() =>
        expect(setAgentGoal).toHaveBeenCalledWith('thread_9', {
          goal: { objective: 'ship the release notes' },
          revision: 4,
        }),
      );
      expect(submitThreadMessage).not.toHaveBeenCalled();
    });

    it('starts the command inside the box and keeps the caret there', async () => {
      // The plus menu's Goal row is not a second interaction: it writes the
      // command into the message box, where the objective would have been typed
      // anyway, and leaves the caret after it.
      const box = await composer();
      fireEvent.click(screen.getByRole('button', { name: 'Add to message' }));
      fireEvent.click(within(screen.getByRole('menu')).getByRole('menuitem', { name: /Goal/ }));

      expect(screen.queryByRole('dialog')).not.toBeInTheDocument();
      expect(box).toHaveValue('/goal ');
      expect(box).toHaveFocus();
      expect(setAgentGoal).not.toHaveBeenCalled();
      expect(submitThreadMessage).not.toHaveBeenCalled();
    });

    it('keeps the command in the box when the write is refused', async () => {
      vi.mocked(setAgentGoal).mockRejectedValue(new Error('goal service unavailable'));
      const box = await composer();
      fireEvent.change(box, { target: { value: `/goal ${OBJECTIVE}` } });
      fireEvent.keyDown(box, { key: 'Enter' });

      expect(await screen.findByRole('alert')).toHaveTextContent('goal service unavailable');
      expect(box).toHaveValue(`/goal ${OBJECTIVE}`);
      expect(submitThreadMessage).not.toHaveBeenCalled();
    });

    it('re-reads the conversation after a version conflict instead of overwriting', async () => {
      const onReload = vi.fn();
      vi.mocked(setAgentGoal).mockRejectedValue(
        new APIError('goal_revision_conflict', 'This goal changed while you were editing it.'),
      );
      render(
        <JobDetail
          job={makeJob({
            threadId: 'thread_9',
            goal: {
              objective: 'make `pytest` pass',
              state: 'active',
              revision: 4,
              roundsUsed: 2,
              maxRounds: 10,
            },
          })}
          onReload={onReload}
        />,
      );
      const box = await screen.findByLabelText('Add a follow-up');
      fireEvent.change(box, { target: { value: `/goal ${OBJECTIVE}` } });
      fireEvent.keyDown(box, { key: 'Enter' });

      // The revision this page holds is what the server refused, so the goal
      // has to be read back before the owner can try again.
      await waitFor(() => expect(onReload).toHaveBeenCalled());
      expect(await screen.findByRole('alert')).toHaveTextContent(
        'This goal changed while you were editing it.',
      );
    });

    it('leaves a message that merely mentions the command to the model', async () => {
      vi.mocked(submitThreadMessage).mockResolvedValue({
        pending: null,
        job: { id: 'ajob_child' },
      } as never);
      const box = await composer();
      fireEvent.change(box, { target: { value: 'remember the /goal syntax we discussed' } });
      fireEvent.keyDown(box, { key: 'Enter' });

      // Messages are addressed to the conversation, which is `thread_9` here.
      await waitFor(() =>
        expect(submitThreadMessage).toHaveBeenCalledWith('thread_9', {
          prompt: 'remember the /goal syntax we discussed',
        }),
      );
      expect(setAgentGoal).not.toHaveBeenCalled();
    });

    it('keeps the band editor and its draft when the write is refused', async () => {
      vi.mocked(setAgentGoal).mockRejectedValueOnce(new Error('goal service unavailable'));
      await composer();
      fireEvent.click(screen.getByRole('button', { name: 'Goal options' }));
      fireEvent.click(screen.getByRole('menuitem', { name: 'Edit goal' }));
      fireEvent.change(screen.getByLabelText('Objective'), {
        target: { value: 'ship the release notes' },
      });
      fireEvent.click(screen.getByRole('button', { name: 'Save' }));

      // The editor stays where the retry can reach it, holding the words that
      // were refused and the reason they were.
      expect(await screen.findByRole('alert')).toHaveTextContent('goal service unavailable');
      expect(screen.getByLabelText('Objective')).toHaveValue('ship the release notes');

      vi.mocked(setAgentGoal).mockResolvedValue({ thread_id: 'thread_9', goal: null, revision: 5 });
      fireEvent.click(screen.getByRole('button', { name: 'Save' }));
      await waitFor(() => expect(screen.queryByLabelText('Objective')).not.toBeInTheDocument());
    });

    it('does not clear a message typed while the command is in flight', async () => {
      // The write is a round trip. Anything typed during it is the owner's next
      // message, and a command that finished late must not delete it.
      let land: (value: unknown) => void = () => {};
      vi.mocked(setAgentGoal).mockImplementation(
        () =>
          new Promise((resolve) => {
            land = resolve;
          }) as never,
      );
      const box = await composer();
      fireEvent.change(box, { target: { value: `/goal ${OBJECTIVE}` } });
      fireEvent.keyDown(box, { key: 'Enter' });
      await waitFor(() => expect(setAgentGoal).toHaveBeenCalled());

      fireEvent.change(box, { target: { value: 'and then update the docs' } });
      await act(async () => {
        land({ thread_id: 'thread_9', goal: null, revision: 5 });
      });

      expect(box).toHaveValue('and then update the docs');
    });

    it('keeps the band editor draft through the re-read a conflict forces', async () => {
      // The refusal makes the page re-read the conversation, which moves the
      // objective under the open editor. The new revision is what that re-read
      // is for; the owner's words are the retry, and they stay.
      vi.mocked(setAgentGoal).mockRejectedValueOnce(
        new APIError('goal_revision_conflict', 'This goal changed while you were editing it.'),
      );
      const withGoal = (overrides: { objective: string; revision: number }) =>
        makeJob({
          threadId: 'thread_9',
          goal: { state: 'active', roundsUsed: 2, maxRounds: null, ...overrides },
        });
      const { rerender } = render(
        <JobDetail job={withGoal({ objective: 'make `pytest` pass', revision: 4 })} />,
      );
      await screen.findByLabelText('Add a follow-up');
      fireEvent.click(screen.getByRole('button', { name: 'Goal options' }));
      fireEvent.click(screen.getByRole('menuitem', { name: 'Edit goal' }));
      fireEvent.change(screen.getByLabelText('Objective'), {
        target: { value: 'ship the release notes' },
      });
      fireEvent.click(screen.getByRole('button', { name: 'Save' }));
      await waitFor(() => expect(setAgentGoal).toHaveBeenCalledTimes(1));

      rerender(
        <JobDetail job={withGoal({ objective: 'the other tab’s objective', revision: 5 })} />,
      );

      expect(screen.getByLabelText('Objective')).toHaveValue('ship the release notes');

      vi.mocked(setAgentGoal).mockResolvedValue({ thread_id: 'thread_9', goal: null, revision: 6 });
      fireEvent.click(screen.getByRole('button', { name: 'Save' }));
      await waitFor(() =>
        expect(setAgentGoal).toHaveBeenLastCalledWith('thread_9', {
          goal: { objective: 'ship the release notes' },
          revision: 5,
        }),
      );
    });

    it('clears the goal from the band without touching the conversation', async () => {
      vi.mocked(clearAgentGoal).mockResolvedValue({
        thread_id: 'thread_9',
        cleared: true,
        revision: 5,
      });
      await composer();
      fireEvent.click(screen.getByRole('button', { name: 'Goal options' }));
      fireEvent.click(screen.getByRole('menuitem', { name: 'Clear goal' }));

      await waitFor(() => expect(clearAgentGoal).toHaveBeenCalledWith('thread_9'));
      // Clearing removes the goal and the rounds queued for it; the chat and
      // its history are not the client's to delete.
      expect(submitThreadMessage).not.toHaveBeenCalled();
      expect(cancelAgentJob).not.toHaveBeenCalled();
    });

    it('keeps goal-menu and editor Escape separate from the Stop shortcut', async () => {
      const box = await composer();
      const trigger = screen.getByRole('button', { name: 'Goal options' });
      fireEvent.click(trigger);
      fireEvent.keyDown(screen.getByRole('menuitem', { name: 'Edit goal' }), { key: 'Escape' });
      expect(screen.queryByRole('menu', { name: 'Goal options' })).not.toBeInTheDocument();
      expect(trigger).toHaveFocus();
      expect(cancelAgentJob).not.toHaveBeenCalled();

      fireEvent.click(trigger);
      fireEvent.click(screen.getByRole('menuitem', { name: 'Edit goal' }));
      // Escape also belongs to the editor when one of its buttons has focus.
      fireEvent.keyDown(screen.getByRole('button', { name: 'Save' }), { key: 'Escape' });
      expect(screen.queryByLabelText('Objective')).not.toBeInTheDocument();
      expect(cancelAgentJob).not.toHaveBeenCalled();
      expect(submitThreadMessage).not.toHaveBeenCalled();
      expect(box).toHaveValue('');

      fireEvent.keyDown(window, { key: 'Escape' });
      await waitFor(() => expect(cancelAgentJob).toHaveBeenCalledWith('ajob_1'));
    });

    it('keeps editing outside the message form and returns focus after clearing', async () => {
      const box = await composer();
      fireEvent.change(box, { target: { value: 'my next message' } });
      const trigger = screen.getByRole('button', { name: 'Goal options' });
      fireEvent.click(trigger);
      fireEvent.click(screen.getByRole('menuitem', { name: 'Edit goal' }));
      const objective = screen.getByLabelText('Objective');
      expect(objective.closest('form')).toBeNull();
      fireEvent.keyDown(objective, { key: 'Enter' });
      expect(submitThreadMessage).not.toHaveBeenCalled();
      fireEvent.click(screen.getByRole('button', { name: 'Cancel' }));
      vi.mocked(clearAgentGoal).mockResolvedValue({
        thread_id: 'thread_9',
        cleared: true,
        revision: 5,
      });
      fireEvent.click(trigger);
      fireEvent.click(screen.getByRole('menuitem', { name: 'Clear goal' }));
      await waitFor(() => expect(box).toHaveFocus());
      expect(box).toHaveValue('my next message');
      expect(cancelAgentJob).not.toHaveBeenCalled();
    });

    it('never turns a menu dismissal into a stop', async () => {
      // Two keys that both mean "not this": the menu closes on Escape, and the
      // header's own Stop ends the round. A composer that ran one into the
      // other would end a run every time someone changed their mind.
      const box = await composer();
      fireEvent.change(box, { target: { value: '/go' } });
      fireEvent.keyDown(box, { key: 'Escape' });
      expect(screen.queryByRole('listbox', { name: 'Commands' })).not.toBeInTheDocument();
      expect(cancelAgentJob).not.toHaveBeenCalled();

      // The shortcut itself is live; it is the menu that owns the key while it
      // is showing.
      fireEvent.keyDown(window, { key: 'Escape' });
      await waitFor(() => expect(cancelAgentJob).toHaveBeenCalledWith('ajob_1'));
    });

    it('offers the command in the box and stands down for ordinary prose', async () => {
      const box = await composer();
      fireEvent.change(box, { target: { value: '/go' } });
      expect(screen.getByRole('listbox', { name: 'Commands' })).toBeInTheDocument();

      fireEvent.change(box, { target: { value: '/goals are per conversation' } });
      expect(screen.queryByRole('listbox', { name: 'Commands' })).not.toBeInTheDocument();
    });
  });
});

describe('JobDetail reasoning effort', () => {
  const THINKING_CONFIG = {
    ...AGENT_CONFIG,
    runtime_reasoning_efforts: { 'claude-code': ['low', 'medium', 'high', 'xhigh', 'max'] },
    // Read as a yes/no, not as a vocabulary: the offered levels are the
    // platform's four.
    model_reasoning_efforts: { 'qwen-test': ['low', 'high', 'max'] },
  };

  beforeEach(() => {
    navigation.push.mockReset();
    vi.mocked(submitThreadMessage).mockReset();
    vi.mocked(getAgentConfig).mockReset();
    vi.mocked(getAgentConfig).mockResolvedValue(THINKING_CONFIG);
    vi.mocked(submitThreadMessage).mockResolvedValue({
      pending: null,
      job: { id: 'ajob_child' },
    } as never);
  });

  afterEach(() => cleanup());

  it('offers the platform levels for the turn it would run', async () => {
    render(<JobDetail job={makeJob()} />);

    const picker = await screen.findByLabelText('Reasoning for this turn');
    const offered = Array.from(picker.querySelectorAll('option')).map((o) => o.textContent);
    expect(offered).toEqual(['low', 'medium', 'high', 'max']);
  });

  it('leads with the inherited level, and the deepest when nothing was inherited', async () => {
    const { unmount } = render(<JobDetail job={makeJob({ reasoningEffort: 'low' })} />);
    expect(await screen.findByLabelText('Reasoning for this turn')).toHaveValue('low');
    unmount();

    render(<JobDetail job={makeJob()} />);
    expect(await screen.findByLabelText('Reasoning for this turn')).toHaveValue('max');
  });

  it('inherits silently when untouched, and sends an actual change', async () => {
    const { unmount } = render(<JobDetail job={makeJob({ reasoningEffort: 'high' })} />);

    // Untouched: the field is absent, which the backend reads as "inherit".
    fireEvent.change(screen.getByLabelText('Add a follow-up'), { target: { value: 'again' } });
    fireEvent.click(screen.getByRole('button', { name: /^(Send|Queue)$/ }));
    await waitFor(() =>
      expect(submitThreadMessage).toHaveBeenCalledWith('ajob_1', { prompt: 'again' }),
    );
    unmount();

    vi.mocked(submitThreadMessage).mockClear();
    render(<JobDetail job={makeJob({ reasoningEffort: 'high' })} />);
    fireEvent.change(await screen.findByLabelText('Reasoning for this turn'), {
      target: { value: 'low' },
    });
    fireEvent.change(screen.getByLabelText('Add a follow-up'), { target: { value: 'easy one' } });
    fireEvent.click(screen.getByRole('button', { name: /^(Send|Queue)$/ }));

    await waitFor(() =>
      expect(submitThreadMessage).toHaveBeenCalledWith('ajob_1', {
        prompt: 'easy one',
        reasoning_effort: 'low',
      }),
    );
  });

  it('sends the shown level on a thread that carried none', async () => {
    // The control cannot render blank, so a thread with no stored level shows
    // the deepest on offer — and what is shown is what is sent.
    render(<JobDetail job={makeJob()} />);
    await screen.findByLabelText('Reasoning for this turn');

    fireEvent.change(screen.getByLabelText('Add a follow-up'), { target: { value: 'go' } });
    fireEvent.click(screen.getByRole('button', { name: /^(Send|Queue)$/ }));

    await waitFor(() =>
      expect(submitThreadMessage).toHaveBeenCalledWith('ajob_1', {
        prompt: 'go',
        reasoning_effort: 'max',
      }),
    );
  });

  it('drops a level the switched-to model does not offer', async () => {
    render(<JobDetail job={makeJob({ reasoningEffort: 'high' })} />);

    // glm-test takes no effort, so the control disappears and the inherited
    // level must not ride along to a model that would refuse it.
    fireEvent.change(await screen.findByLabelText('Model for this turn'), {
      target: { value: 'glm-test' },
    });
    await waitFor(() =>
      expect(screen.queryByLabelText('Reasoning for this turn')).not.toBeInTheDocument(),
    );

    fireEvent.change(screen.getByLabelText('Add a follow-up'), { target: { value: 'switch' } });
    fireEvent.click(screen.getByRole('button', { name: /^(Send|Queue)$/ }));

    await waitFor(() =>
      expect(submitThreadMessage).toHaveBeenCalledWith('ajob_1', {
        prompt: 'switch',
        model: 'glm-test',
      }),
    );
  });
});

describe('the trajectory tab', () => {
  beforeEach(() => {
    vi.mocked(getAgentConfig).mockReset().mockResolvedValue(AGENT_CONFIG);
    vi.mocked(getAgentJobGit).mockReset().mockResolvedValue({
      available: false,
      branch: '',
      changes: [],
      patch: '',
      commits: [],
    });
    vi.mocked(getAgentJobUsage).mockReset().mockResolvedValue(null);
    vi.mocked(refreshAgentJobUsage).mockReset().mockResolvedValue(null);
    vi.mocked(getAgentThreadStats).mockReset().mockResolvedValue(null);
    vi.mocked(getAgentAttemptTelemetry).mockReset().mockResolvedValue({
      attempt_id: 1,
      attempt_no: 1,
      continuity_mode: null,
      context_status: null,
      duration_ms: null,
      resources: null,
    });
    localStorage.clear();
  });

  afterEach(() => cleanup());

  const STORED_CALL = [
    {
      id: 1,
      job_id: 'ajob_1',
      turn_no: 1,
      attempt_id: 1,
      seq: 1,
      event_type: 'tool_use',
      payload: { id: 't1', name: 'Bash', input: { command: 'pytest -q' } },
      created_at: '2026-08-19T10:00:00.000Z',
    },
    {
      id: 2,
      job_id: 'ajob_1',
      turn_no: 1,
      attempt_id: 1,
      seq: 2,
      event_type: 'tool_result',
      payload: { tool_use_id: 't1', content: '2 passed' },
      created_at: '2026-08-19T10:00:05.000Z',
    },
  ] as never;

  it('switches between the reading of the stream and the stream itself', async () => {
    const job = makeJob({ events: [{ kind: 'message', text: 'the build passes' }] });
    const stored = [
      {
        id: 1,
        attempt_id: 1,
        seq: 1,
        event_type: 'tool_use',
        payload: { id: 't1', name: 'Bash', detail: 'cargo build' },
        created_at: '2026-08-19T10:00:00.000Z',
      },
    ] as never;

    render(<JobDetail job={job} events={stored} />);

    // Chat first: the reading, with no trajectory chrome.
    expect(screen.queryByLabelText('Search steps')).toBeNull();

    fireEvent.click(screen.getByRole('button', { name: 'trajectory' }));

    expect(await screen.findByLabelText('Search steps')).toBeInTheDocument();
    expect(screen.getByText('Bash')).toBeInTheDocument();

    fireEvent.click(screen.getByRole('button', { name: 'chat' }));

    expect(screen.queryByLabelText('Search steps')).toBeNull();
  });

  it('separates stored events by conversation turn', async () => {
    const stored = [
      {
        id: 1,
        job_id: 'ajob_0',
        turn_no: 1,
        attempt_id: 1,
        seq: 1,
        event_type: 'tool_use',
        payload: { id: 't1', name: 'Read', detail: 'README.md' },
        created_at: null,
      },
      {
        id: 2,
        job_id: 'ajob_1',
        turn_no: 2,
        attempt_id: 2,
        seq: 1,
        event_type: 'tool_use',
        payload: { id: 't2', name: 'Bash', detail: 'pytest' },
        created_at: null,
      },
    ] as never;
    render(<JobDetail job={makeJob()} events={stored} />);

    fireEvent.click(screen.getByRole('button', { name: 'trajectory' }));

    expect(await screen.findByText('Turn 1')).toBeInTheDocument();
    expect(screen.getByText('Turn 2')).toBeInTheDocument();
    expect(screen.getByText('Read')).toBeInTheDocument();
    expect(screen.getByText('Bash')).toBeInTheDocument();
  });

  it('still renders when no stored stream was handed down', async () => {
    // Even without runtime events, the recorded turn input remains readable.
    render(<JobDetail job={makeJob()} />);

    fireEvent.click(screen.getByRole('button', { name: 'trajectory' }));

    expect(await screen.findByText('user')).toBeInTheDocument();
    expect(screen.getByText('Current task with all of its detail.')).toBeInTheDocument();
    expect(screen.getByText('1 step')).toBeInTheDocument();
  });

  it('opens the selected step in the right-hand region, not under the row', async () => {
    render(<JobDetail job={makeJob()} events={STORED_CALL} />);

    fireEvent.click(screen.getByRole('button', { name: 'trajectory' }));
    fireEvent.click(await screen.findByText('Bash'));
    await screen.findByRole('button', { name: 'Close details' });

    const panel = screen.getByRole('region', { name: 'Step details' });
    // The turn's User input is step 1; its joined tool call/result is step 2.
    expect(await screen.findByText('user')).toBeInTheDocument();
    expect(within(panel).getByText(/Tool · Turn 1 · Step 2/)).toBeInTheDocument();
    expect(within(panel).getByRole('tab', { name: 'Schema' })).toBeInTheDocument();
    // The result joined its call rather than becoming a second row.
    expect(within(panel).getByText(/2 passed/)).toBeInTheDocument();
    expect(screen.getAllByRole('tab')).toHaveLength(5);
  });

  it('keeps one right-hand region: opening the workspace closes the details', async () => {
    render(<JobDetail job={makeJob()} events={STORED_CALL} />);

    fireEvent.click(screen.getByRole('button', { name: 'trajectory' }));
    fireEvent.click(await screen.findByText('Bash'));
    await screen.findByRole('button', { name: 'Close details' });
    expect(screen.getByRole('region', { name: 'Step details' })).toBeInTheDocument();

    openWorkspace();

    expect(screen.queryByRole('region', { name: 'Step details' })).not.toBeInTheDocument();
    expect(screen.getByRole('region', { name: 'Job workspace' })).toBeInTheDocument();

    fireEvent.click(await screen.findByText('Bash'));
    await screen.findByRole('button', { name: 'Close details' });

    expect(screen.getByRole('region', { name: 'Step details' })).toBeInTheDocument();
    expect(screen.queryByRole('region', { name: 'Job workspace' })).not.toBeInTheDocument();
  });

  it('closes the details on Escape without also stopping the run', async () => {
    render(<JobDetail job={makeJob({ state: 'running' })} events={STORED_CALL} />);

    fireEvent.click(screen.getByRole('button', { name: 'trajectory' }));
    fireEvent.click(await screen.findByText('Bash'));
    await screen.findByRole('button', { name: 'Close details' });

    fireEvent.keyDown(document.body, { key: 'Escape' });

    expect(screen.queryByRole('region', { name: 'Step details' })).not.toBeInTheDocument();
    expect(cancelAgentJob).not.toHaveBeenCalled();
  });

  it('leaves Escape alone while the reader is typing', async () => {
    render(<JobDetail job={makeJob()} events={STORED_CALL} />);

    fireEvent.click(screen.getByRole('button', { name: 'trajectory' }));
    fireEvent.click(await screen.findByText('Bash'));
    await screen.findByRole('button', { name: 'Close details' });

    const search = screen.getByLabelText('Search steps');
    search.focus();
    fireEvent.keyDown(search, { key: 'Escape' });

    expect(screen.getByRole('region', { name: 'Step details' })).toBeInTheDocument();
  });

  it('opens the turn the chat footer points at, even for an older turn', async () => {
    const job = makeJob({
      turnNo: 2,
      threadMessages: HISTORY,
      historyTurns: [
        {
          jobId: 'ajob_0',
          turnNo: 1,
          forkable: true,
          messages: HISTORY,
          events: [{ kind: 'message', text: 'Earlier answer' }],
        },
      ],
    });
    render(<JobDetail job={job} events={STORED_CALL} threadJobs={[]} />);

    fireEvent.click(
      within(screen.getByLabelText('Turn 1')).getByRole('button', { name: 'View execution' }),
    );

    const panel = screen.getByRole('region', { name: 'Run details' });
    await within(panel).findByRole('button', { name: 'Close details' });
    expect(within(panel).getByRole('heading', { name: 'Run details' })).toBeInTheDocument();
    expect(within(panel).getByText('Run · Turn 1')).toBeInTheDocument();
  });

  it('closes the details when the reader goes back to the conversation', async () => {
    render(<JobDetail job={makeJob()} events={STORED_CALL} />);

    fireEvent.click(screen.getByRole('button', { name: 'trajectory' }));
    fireEvent.click(await screen.findByText('Bash'));
    await screen.findByRole('button', { name: 'Close details' });
    fireEvent.click(screen.getByRole('button', { name: 'chat' }));

    expect(screen.queryByRole('region', { name: 'Step details' })).not.toBeInTheDocument();
  });
});

describe('sending a follow-up', () => {
  afterEach(() => cleanup());

  it('clears the composer instead of leaving it disabled with the sent text', async () => {
    // A remount used to do this. Keying on the conversation rather than the
    // turn removed the remount, and with it the only thing that ever reset
    // either — the composer stayed disabled with the prompt still in it.
    vi.mocked(submitThreadMessage).mockResolvedValue({
      pending: null,
      job: { id: 'ajob_next' },
    } as never);
    render(<JobDetail job={makeJob()} />);
    const box = screen.getByLabelText('Add a follow-up');

    fireEvent.change(box, { target: { value: 'and now the docs' } });
    fireEvent.submit(box.closest('form') as HTMLFormElement);

    await waitFor(() => expect(submitThreadMessage).toHaveBeenCalled());
    await waitFor(() => expect((box as HTMLTextAreaElement).value).toBe(''));
    expect(box).not.toBeDisabled();
  });
});

describe('conversation turn navigation', () => {
  beforeEach(() => {
    navigation.push.mockReset();
    vi.mocked(forkAgentJob).mockReset();
  });
  afterEach(() => cleanup());

  const EARLIER_TURN = {
    jobId: 'ajob_0',
    turnNo: 1,
    forkable: false,
    messages: [
      {
        id: 1,
        role: 'user' as const,
        content: 'Earlier question',
        jobId: 'ajob_0',
        createdAt: null,
      },
      {
        id: 2,
        role: 'assistant' as const,
        content: 'Earlier answer',
        jobId: 'ajob_0',
        createdAt: null,
      },
    ],
    events: [{ kind: 'message' as const, text: 'Earlier answer', attemptNo: 1, sourceEventId: 11 }],
  };

  // Turn two of a conversation that already has turn one behind it.
  function multiTurnJob(overrides: Partial<AgentJob> = {}) {
    return makeJob({ turnNo: 2, historyTurns: [EARLIER_TURN], ...overrides });
  }

  // A settled, shared conversation, where the stored turns offer actions.
  function forkableJob(historyForkable: boolean) {
    return multiTurnJob({
      state: 'done',
      sandboxId: 'asbx_1',
      workspaceShared: true,
      nativeForkAvailable: true,
      historyTurns: [{ ...EARLIER_TURN, forkable: historyForkable }],
    });
  }

  function renderNavigableChat() {
    const view = render(<JobDetail job={multiTurnJob()} />);
    // jsdom supplies no layout. Give the Chat a wide reading area so its
    // actual rail, rather than a separate directory, can be exercised.
    const region = screen.getByRole('region', { name: 'Task progress' });
    Object.defineProperty(region, 'clientWidth', { configurable: true, value: 1024 });
    Object.defineProperty(region, 'clientHeight', { configurable: true, value: 800 });
    return view;
  }

  it('lists exactly the turns the transcript renders, in order', async () => {
    renderNavigableChat();
    const rail = await screen.findByRole('navigation', { name: 'Conversation turns' });
    const marks = within(rail).getAllByRole('button');
    expect(marks).toHaveLength(2);
    expect(marks[0]).toHaveAccessibleName('Go to turn 1');
    expect(marks[1]).toHaveAccessibleName('Go to turn 2');
    fireEvent.focus(marks[0]);
    expect(screen.getByRole('tooltip')).toHaveTextContent('Earlier question');
    expect(screen.getByRole('tooltip')).toHaveTextContent('Earlier answer');
    // Which turn the reader is on is measured a frame after layout rather than
    // guessed from the focused preview.
    await waitFor(() => expect(marks[1]).toHaveAttribute('aria-current', 'true'));
  });

  it('offers forking on a stored turn as soon as the API says it is ready', () => {
    // Nothing about the turn's structure changes when the platform finishes
    // preparing its native session; a cache keyed on counts would keep serving
    // the stored turn it derived before that, with no Fork button on it.
    const { container, rerender } = render(<JobDetail job={forkableJob(false)} />);
    const storedTurn = () => {
      const section = container.querySelector('[data-turn-key="ajob_0"]');
      if (section === null) throw new Error('the stored turn is not rendered');
      return within(section as HTMLElement);
    };
    expect(storedTurn().queryByRole('button', { name: 'Fork from here' })).not.toBeInTheDocument();

    rerender(<JobDetail job={forkableJob(true)} />);
    expect(storedTurn().getByRole('button', { name: 'Fork from here' })).toBeInTheDocument();
  });

  it('renders a stored row that changed without the list growing', () => {
    // A tool result folding into its call leaves the row count alone and
    // rewrites the row's content; the body must show what the events say now.
    const withOutput = {
      ...EARLIER_TURN,
      events: [
        {
          kind: 'tool_use' as const,
          tool: 'Bash',
          detail: 'pytest -q',
          output: ['711 tests passed in the stored turn'],
          attemptNo: 1,
          sourceEventId: 11,
        },
      ],
    };
    const withoutOutput = {
      ...withOutput,
      events: [{ ...withOutput.events[0], output: undefined }],
    };
    const { rerender } = render(
      <JobDetail job={multiTurnJob({ historyTurns: [withoutOutput] })} />,
    );
    expect(screen.getByText('Waiting for the tool result…')).toBeInTheDocument();

    rerender(<JobDetail job={multiTurnJob({ historyTurns: [withOutput] })} />);
    expect(screen.getByText('711 tests passed in the stored turn')).toBeInTheDocument();
    expect(screen.queryByText('Waiting for the tool result…')).not.toBeInTheDocument();
  });

  it('anchors every rendered turn and row for the scroll controller', () => {
    const { container } = render(<JobDetail job={multiTurnJob()} />);

    // The rail addresses turns by job id; the reading position is restored by
    // row, so both identities have to be on the elements the transcript draws.
    const turnKeys = [...container.querySelectorAll('[data-turn-key]')].map((element) =>
      element.getAttribute('data-turn-key'),
    );
    expect(turnKeys).toEqual(['ajob_0', 'ajob_1']);
    expect(container.querySelector('[data-block-key="ajob_0:m1"]')).not.toBeNull();
    expect(container.querySelector('[data-block-key="ajob_0:e11"]')).not.toBeNull();
    expect(container.querySelector('[data-block-key="ajob_1:prompt"]')).not.toBeNull();
  });

  it('jumps inside the chat without navigating, forking or switching attempts', async () => {
    renderNavigableChat();
    const rail = await screen.findByRole('navigation', { name: 'Conversation turns' });
    fireEvent.click(within(rail).getAllByRole('button')[0]);

    expect(screen.queryByRole('dialog')).not.toBeInTheDocument();
    expect(navigation.push).not.toHaveBeenCalled();
    expect(forkAgentJob).not.toHaveBeenCalled();
    // The queued frame that recalibrates the mark runs outside this commit.
    await waitFor(() =>
      expect(screen.queryByText('That turn could not be located.')).not.toBeInTheDocument(),
    );
  });

  it('takes the rail away with the Chat view', async () => {
    renderNavigableChat();
    expect(
      await screen.findByRole('navigation', { name: 'Conversation turns' }),
    ).toBeInTheDocument();

    fireEvent.click(screen.getByRole('button', { name: 'trajectory' }));
    expect(
      screen.queryByRole('navigation', { name: 'Conversation turns' }),
    ).not.toBeInTheDocument();
  });
});
