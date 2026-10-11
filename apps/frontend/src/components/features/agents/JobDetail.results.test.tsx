// @vitest-environment jsdom
import '@testing-library/jest-dom/vitest';

import { cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import {
  getAgentJobGit,
  type AgentJobApi,
  type AgentThreadApi,
  type AgentThreadEventApi,
} from '@/lib/api/agents';
import { toDisplayJob } from './adapt';
import { JobDetail } from './JobDetail';

vi.mock('next/navigation', () => ({
  useRouter: () => ({ push: vi.fn() }),
  usePathname: () => '/current-turn',
}));

vi.mock('@/lib/api/agents', async (importOriginal) => ({
  ...(await importOriginal<typeof import('@/lib/api/agents')>()),
  getAgentConfig: vi.fn().mockResolvedValue({
    repos: ['owner/repository'],
    runtimes: ['codex'],
    models: ['test-model'],
    github_connected: true,
    github_install_url: null,
  }),
  getAgentJobGit: vi.fn(),
  getAgentJobUsage: vi.fn().mockResolvedValue(null),
  refreshAgentJobUsage: vi.fn().mockResolvedValue(null),
  getAgentThreadStats: vi.fn().mockResolvedValue(null),
  listAgentTerminals: vi.fn().mockResolvedValue([]),
  getAgentJobFiles: vi.fn().mockResolvedValue({ path: '', kind: 'directory', entries: [] }),
}));

vi.mock('@/lib/api/questions', () => ({
  listThreadQuestions: vi.fn().mockResolvedValue({ questions: [] }),
  answerThreadQuestion: vi.fn(),
}));

vi.mock('@/lib/api/mcp', async (importOriginal) => ({
  ...(await importOriginal<typeof import('@/lib/api/mcp')>()),
  listMcpInvocations: vi.fn().mockResolvedValue({ invocations: [] }),
  getMcpThreadBindings: vi.fn().mockResolvedValue({ bindings: [] }),
  listMcpConnections: vi.fn().mockResolvedValue({ connections: [] }),
}));

vi.mock('@/lib/api/pendingMessages', async (importOriginal) => ({
  ...(await importOriginal<typeof import('@/lib/api/pendingMessages')>()),
  getThreadQueue: vi.fn().mockImplementation(async (threadId: string) => ({
    thread_id: threadId,
    pending_messages: [],
    queue_paused_reason: null,
    latest_job_id: null,
    latest_turn_no: null,
    latest_job_state: null,
  })),
}));

function rawJob(id: string, turn: number, overrides: Partial<AgentJobApi> = {}): AgentJobApi {
  return {
    id,
    thread_id: 'results-thread',
    turn_no: turn,
    repo: 'owner/repository',
    task_prompt: `Question for turn ${turn}`,
    title: 'Recorded result review',
    runtime: 'codex',
    model: 'test-model',
    base_ref: 'dev',
    base_sha: 'abcdef123',
    state: 'succeeded',
    conversation_state: 'idle',
    cancel_requested: false,
    current_attempt_id: turn * 100,
    published_pr_url: null,
    detail: null,
    metadata: null,
    created_at: null,
    updated_at: null,
    spent_usd: null,
    tokens_in: null,
    tokens_out: null,
    model_calls: null,
    ...overrides,
  };
}

function patch(path: string, added: string): string {
  return [
    `diff --git a/${path} b/${path}`,
    'index 111..222 100644',
    `--- a/${path}`,
    `+++ b/${path}`,
    '@@ -1 +1 @@',
    '-before',
    `+${added}`,
    '',
  ].join('\n');
}

function diffEvent(job: AgentJobApi, text: string, seq = 1): AgentThreadEventApi {
  return {
    id: job.turn_no! * 100 + seq,
    job_id: job.id,
    turn_no: job.turn_no!,
    attempt_id: job.current_attempt_id!,
    seq,
    event_type: 'diff',
    payload: { text },
    created_at: null,
  };
}

const LIVE_PATCH = patch('src/live-workspace.tsx', 'live workspace content');

function fixture(jobs = [rawJob('history-turn', 1), rawJob('current-turn', 2)]) {
  const current = jobs.find((job) => job.id === 'current-turn')!;
  const events = jobs.map((job) =>
    diffEvent(
      job,
      patch(
        job.id === 'history-turn' ? 'src/history.tsx' : 'src/current.tsx',
        `${job.id} recorded content`,
      ),
    ),
  );
  const thread: AgentThreadApi = {
    thread_id: current.thread_id!,
    jobs,
    messages: jobs.flatMap((job) => [
      {
        id: job.turn_no! * 10,
        role: 'user' as const,
        content: job.task_prompt,
        job_id: job.id,
        created_at: null,
      },
      {
        id: job.turn_no! * 10 + 1,
        role: 'assistant' as const,
        content: `Answer for ${job.id}`,
        job_id: job.id,
        created_at: null,
      },
    ]),
  };
  return {
    job: toDisplayJob(current, {
      patch: LIVE_PATCH,
      events: events.filter((event) => event.job_id === current.id),
      thread,
      threadEvents: events,
    }),
    events,
    threadJobs: jobs,
    userId: 'result-owner',
  };
}

beforeEach(() => {
  localStorage.clear();
  sessionStorage.clear();
  vi.mocked(getAgentJobGit)
    .mockReset()
    .mockResolvedValue({
      available: true,
      branch: 'agent/results-thread',
      changes: [{ code: 'M', path: 'src/live-workspace.tsx' }],
      patch: LIVE_PATCH,
      commits: [],
    });
});

afterEach(cleanup);

describe('recorded results in the conversation', () => {
  it('moves keyboard focus into the recorded workspace and restores the file opener when closed', async () => {
    render(<JobDetail {...fixture()} />);
    const opener = screen.getByRole('button', {
      name: 'Open recorded diff for src/history.tsx',
    });
    opener.focus();
    expect(opener).toHaveFocus();
    fireEvent.click(opener);

    const git = screen.getByRole('tab', { name: /^Git/ });
    expect(git).toHaveAttribute('aria-selected', 'true');
    expect(git).toHaveFocus();
    expect(screen.getByRole('region', { name: 'Recorded changes · Turn 1' })).toBeVisible();

    fireEvent.click(screen.getByRole('button', { name: 'Close workspace' }));
    await waitFor(() => expect(opener).toHaveFocus());
    expect(screen.queryByRole('region', { name: 'Job workspace' })).not.toBeInTheDocument();
  });

  it('opens a historical file from its producing turn without fetching the shared live diff', async () => {
    const props = fixture();
    render(<JobDetail {...props} />);
    const historical = screen.getByRole('region', { name: 'Turn 1' });
    const result = within(historical).getByRole('region', { name: 'Recorded turn results' });
    expect(result).toHaveAttribute('data-recorded-job-id', 'history-turn');
    expect(within(result).queryByText('src/live-workspace.tsx')).not.toBeInTheDocument();
    fireEvent.click(
      within(result).getByRole('button', { name: 'Open recorded diff for src/history.tsx' }),
    );
    const recorded = screen.getByRole('region', { name: 'Recorded changes · Turn 1' });
    expect(recorded).toHaveTextContent('src/history.tsx');
    expect(recorded).toHaveTextContent('+history-turn recorded content');
    expect(recorded).not.toHaveTextContent('live workspace content');
    expect(recorded).not.toHaveTextContent('+current-turn recorded content');
    expect(recorded.querySelector('[data-recorded-job-id]')).toHaveAttribute(
      'data-recorded-job-id',
      'history-turn',
    );
    await waitFor(() => expect(getAgentJobGit).not.toHaveBeenCalled());

    fireEvent.click(within(recorded).getByRole('button', { name: 'Back to current workspace' }));
    await waitFor(() => expect(getAgentJobGit).toHaveBeenCalledWith('current-turn'));
    expect(
      screen.queryByRole('region', { name: 'Recorded changes · Turn 1' }),
    ).not.toBeInTheDocument();
  });

  it('assigns each turn only its directly recorded PR, including historical turns', () => {
    const props = fixture([
      rawJob('history-turn', 1, {
        published_pr_url: 'https://github.com/owner/repository/pull/1',
      }),
      rawJob('current-turn', 2, {
        published_pr_url: 'https://github.com/owner/repository/pull/2',
      }),
    ]);
    render(<JobDetail {...props} />);
    for (const number of [1, 2]) {
      const turn = screen.getByRole('region', { name: `Turn ${number}` });
      const result = within(turn).getByRole('region', { name: 'Recorded turn results' });
      expect(within(result).getByRole('link', { name: 'Open pull request' })).toHaveAttribute(
        'href',
        `https://github.com/owner/repository/pull/${number}`,
      );
    }
  });

  it('does not inherit a later turn PR into the current or historical recorded result', () => {
    const props = fixture([
      rawJob('history-turn', 1),
      rawJob('current-turn', 2),
      rawJob('later-turn', 3, {
        published_pr_url: 'https://github.com/owner/repository/pull/3',
      }),
    ]);
    // The existing generic workspace action intentionally follows the shared
    // conversation's PR. Recorded result cards still belong to their own turn.
    expect(props.job.prUrl).toBe('https://github.com/owner/repository/pull/3');
    render(<JobDetail {...props} />);
    for (const number of [1, 2]) {
      const turn = screen.getByRole('region', { name: `Turn ${number}` });
      const result = within(turn).getByRole('region', { name: 'Recorded turn results' });
      expect(
        within(result).queryByRole('link', { name: 'Open pull request' }),
      ).not.toBeInTheDocument();
    }
  });

  it('keeps live workspace changes out of cards when the stored turn diff is clipped', () => {
    const props = fixture();
    const clippedEvents = props.events.map((event) => ({
      ...event,
      payload: { ...event.payload, truncated: true },
    }));
    render(<JobDetail {...props} events={clippedEvents} />);
    expect(props.job.diffFiles).toContain('src/live-workspace.tsx');
    expect(screen.queryByRole('region', { name: 'Recorded turn results' })).not.toBeInTheDocument();
  });

  it.each(['thread', 'account'] as const)(
    'clears an open recorded result when its owning %s changes',
    async (scope) => {
      const props = fixture();
      const { rerender } = render(<JobDetail {...props} />);
      fireEvent.click(
        screen.getByRole('button', { name: 'Open recorded diff for src/history.tsx' }),
      );
      expect(screen.getByRole('region', { name: 'Recorded changes · Turn 1' })).toBeInTheDocument();
      rerender(
        <JobDetail
          {...props}
          job={scope === 'thread' ? { ...props.job, threadId: 'other-thread' } : props.job}
          userId={scope === 'account' ? 'other-owner' : props.userId}
          events={[]}
          threadJobs={[]}
        />,
      );
      await waitFor(() =>
        expect(
          screen.queryByRole('region', { name: 'Recorded changes · Turn 1' }),
        ).not.toBeInTheDocument(),
      );
    },
  );
});
