// @vitest-environment jsdom
import '@testing-library/jest-dom/vitest';

import { act, cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import {
  cancelAgentJob,
  createAgentTerminal,
  deleteAgentTerminal,
  followUpAgentJob,
  forkAgentJob,
  getAgentConfig,
  getAgentJobFiles,
  getAgentJobGit,
  getAgentJobUsage,
  getAgentThreadStats,
  listAgentTerminals,
  resizeAgentTerminal,
  refreshAgentJobUsage,
  restoreAgentJob,
  streamAgentTerminal,
  writeAgentTerminalInput,
  writeAgentJobFile,
  type AgentJobFilesApi,
  type AgentJobUsageApi,
} from '@/lib/api/agents';

import { JobDetail, WORKSPACE_WIDTH_STORAGE_KEY } from './JobDetail';
import type { AgentJob } from './types';

const navigation = vi.hoisted(() => ({ push: vi.fn() }));

vi.mock('next/navigation', () => ({
  useRouter: () => navigation,
}));

vi.mock('@/lib/api/agents', () => ({
  cancelAgentJob: vi.fn(),
  createAgentTerminal: vi.fn(),
  deleteAgentTerminal: vi.fn(),
  followUpAgentJob: vi.fn(),
  forkAgentJob: vi.fn(),
  getAgentConfig: vi.fn(),
  getAgentJobFiles: vi.fn(),
  getAgentJobGit: vi.fn(),
  getAgentJobUsage: vi.fn(),
  getAgentThreadStats: vi.fn(),
  listAgentTerminals: vi.fn(),
  resizeAgentTerminal: vi.fn(),
  refreshAgentJobUsage: vi.fn(),
  restoreAgentJob: vi.fn(),
  streamAgentTerminal: vi.fn(),
  writeAgentTerminalInput: vi.fn(),
  writeAgentJobFile: vi.fn(),
}));

const AGENT_CONFIG = {
  repos: ['owner/repository'],
  runtimes: ['claude-code'],
  models: ['qwen-test', 'glm-test'],
  setup_egress_tier: 'trusted',
  agent_egress_tier: 'platform_only',
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
    networkSetup: 'allowlist',
    networkAgent: 'platform relays only',
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
    eventCount: 3,
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
    diffLines: [{ marker: 'add', text: '+fixed' }],
    rawLines: ['{"event_type":"message"}'],
    gates: [],
    usage: { tokensIn: '1.2k', tokensOut: '80', cachePct: 0, turns: 2 },
    egressDenials: 0,
    ...overrides,
  };
}

function openWorkspace() {
  fireEvent.click(screen.getByRole('button', { name: 'Open workspace' }));
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
  beforeEach(() => {
    navigation.push.mockReset();
    vi.mocked(cancelAgentJob).mockReset();
    vi.mocked(followUpAgentJob).mockReset();
    vi.mocked(forkAgentJob).mockReset();
    vi.mocked(restoreAgentJob).mockReset();
    vi.mocked(getAgentConfig).mockReset();
    vi.mocked(getAgentConfig).mockResolvedValue(AGENT_CONFIG);
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

  it('keeps the archived view intact if unarchive fails', async () => {
    vi.mocked(restoreAgentJob).mockRejectedValue(new Error('Connection lost'));
    const onReload = vi.fn();
    render(<JobDetail job={makeJob({ archivedAt: '2026-09-10T00:00:00Z' })} onReload={onReload} />);
    expect(screen.getByText(/Archiving does not stop it/)).toBeInTheDocument();
    fireEvent.click(screen.getByRole('button', { name: 'Unarchive and continue' }));
    expect(await screen.findByRole('alert')).toHaveTextContent('Connection lost');
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
    expect(screen.getByText(/native context is unavailable/)).toBeInTheDocument();
    rerender(<JobDetail job={{ ...archived, archivedAt: null }} />);
    expect(screen.getByRole('textbox', { name: 'Add a follow-up' })).toBeDisabled();
  });

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
    const activity = within(turn).getByText('Read').closest('details');
    expect(activity).not.toBeNull();
    expect(activity).not.toHaveAttribute('open');
    expect(activity).toHaveTextContent('src/router.py');
    expect(activity).toHaveTextContent('router source');
    expect(within(turn).getByText('Earlier answer')).toBeInTheDocument();
    expect(screen.getAllByText('Earlier answer')).toHaveLength(1);
    expect(within(turn).getByText('Turn 1 · 1 tool call')).toBeInTheDocument();
    expect(screen.getByText(/with all of its detail/)).toBeInTheDocument();
  });

  it('shows reasoning as a status without rendering raw thinking text', () => {
    render(
      <JobDetail
        job={makeJob({
          events: [{ kind: 'thinking', text: 'private chain of thought', attemptNo: 1 }],
        })}
      />,
    );

    expect(screen.getByLabelText('Agent reasoning')).toHaveTextContent('Agent is reasoning');
    expect(screen.queryByText('private chain of thought')).not.toBeInTheDocument();
  });

  it('keeps tool activity collapsed with its result available on demand', () => {
    render(<JobDetail job={makeJob()} />);

    const activity = screen.getByText('Bash').closest('details');
    expect(activity).not.toBeNull();
    expect(activity).not.toHaveAttribute('open');
    expect(activity).toHaveTextContent('2 passed');

    fireEvent.click(screen.getByText('Bash'));
    expect(activity).toHaveAttribute('open');
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

  it('queues a follow-up even while the current run is active', async () => {
    vi.mocked(followUpAgentJob).mockResolvedValue({ id: 'ajob_child' } as never);
    render(<JobDetail job={makeJob()} />);

    expect(screen.getByText(/queued after this run/i)).toBeInTheDocument();
    fireEvent.change(screen.getByLabelText('Add a follow-up'), {
      target: { value: 'Now add a regression test' },
    });
    fireEvent.click(screen.getByRole('button', { name: 'Send follow-up' }));

    await waitFor(() =>
      expect(followUpAgentJob).toHaveBeenCalledWith('ajob_1', {
        prompt: 'Now add a regression test',
      }),
    );
    expect(navigation.push).toHaveBeenCalledWith('/ajob_child', { scroll: false });
  });

  it('sends the follow-up on Enter and keeps Shift+Enter a newline', async () => {
    vi.mocked(followUpAgentJob).mockResolvedValue({ id: 'ajob_child' } as never);
    render(<JobDetail job={makeJob()} />);

    const composer = screen.getByLabelText('Add a follow-up');
    fireEvent.change(composer, { target: { value: 'Now add a regression test' } });

    // Shift+Enter and Alt+Enter stay the newline, so the browser keeps them.
    expect(fireEvent.keyDown(composer, { key: 'Enter', shiftKey: true })).toBe(true);
    expect(fireEvent.keyDown(composer, { key: 'Enter', altKey: true })).toBe(true);
    expect(followUpAgentJob).not.toHaveBeenCalled();

    // An IME commits its candidate with Enter; that keypress is the word, not
    // the send.
    fireEvent.keyDown(composer, { key: 'Enter', isComposing: true });
    expect(followUpAgentJob).not.toHaveBeenCalled();

    fireEvent.keyDown(composer, { key: 'Enter' });

    await waitFor(() =>
      expect(followUpAgentJob).toHaveBeenCalledWith('ajob_1', {
        prompt: 'Now add a regression test',
      }),
    );
    expect(navigation.push).toHaveBeenCalledWith('/ajob_child', { scroll: false });
  });

  it('swallows Enter on an empty follow-up rather than growing a blank line', async () => {
    render(<JobDetail job={makeJob()} />);

    const composer = screen.getByLabelText('Add a follow-up');
    fireEvent.change(composer, { target: { value: '   ' } });

    // Enter is the send key on this composer and never the newline, so with
    // nothing to send it does nothing at all. Shift+Enter still breaks the line.
    expect(fireEvent.keyDown(composer, { key: 'Enter' })).toBe(false);
    expect(fireEvent.keyDown(composer, { key: 'Enter', shiftKey: true })).toBe(true);
    expect(followUpAgentJob).not.toHaveBeenCalled();
  });

  it('accepts the other send chord people arrive with', async () => {
    vi.mocked(followUpAgentJob).mockResolvedValue({ id: 'ajob_child' } as never);
    render(<JobDetail job={makeJob()} />);

    const composer = screen.getByLabelText('Add a follow-up');
    fireEvent.change(composer, { target: { value: 'Now add a regression test' } });
    fireEvent.keyDown(composer, { key: 'Enter', metaKey: true });

    await waitFor(() =>
      expect(followUpAgentJob).toHaveBeenCalledWith('ajob_1', {
        prompt: 'Now add a regression test',
      }),
    );
  });

  it('switches the model for the next turn without leaving the thread', async () => {
    vi.mocked(followUpAgentJob).mockResolvedValue({ id: 'ajob_child' } as never);
    render(<JobDetail job={makeJob()} />);

    const picker = await screen.findByLabelText('Model for this turn');
    expect(picker).toHaveValue('qwen-test');

    fireEvent.change(picker, { target: { value: 'glm-test' } });
    fireEvent.change(screen.getByLabelText('Add a follow-up'), {
      target: { value: 'Now try it on the other model' },
    });
    fireEvent.click(screen.getByRole('button', { name: 'Send follow-up' }));

    await waitFor(() =>
      expect(followUpAgentJob).toHaveBeenCalledWith('ajob_1', {
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
    vi.mocked(followUpAgentJob).mockResolvedValue({ id: 'ajob_child' } as never);
    render(<JobDetail job={makeJob()} />);

    await waitFor(() => expect(getAgentConfig).toHaveBeenCalled());
    expect(screen.queryByLabelText('Model for this turn')).not.toBeInTheDocument();
    expect(screen.getByText(/Inherits claude-code · qwen-test/)).toBeInTheDocument();

    fireEvent.change(screen.getByLabelText('Add a follow-up'), {
      target: { value: 'Carry on' },
    });
    fireEvent.click(screen.getByRole('button', { name: 'Send follow-up' }));

    await waitFor(() =>
      expect(followUpAgentJob).toHaveBeenCalledWith('ajob_1', { prompt: 'Carry on' }),
    );
  });

  it('moves diff, raw events, usage, and sandbox data into a secondary drawer', () => {
    render(<JobDetail job={makeJob()} />);

    expect(screen.queryByText('Input tokens')).not.toBeInTheDocument();
    expect(screen.queryByRole('button', { name: 'Details' })).not.toBeInTheDocument();
    expect(getAgentJobUsage).not.toHaveBeenCalled();
    fireEvent.click(screen.getByRole('button', { name: 'Run details' }));
    expect(screen.getByRole('dialog', { name: 'Run details' })).toBeInTheDocument();
    expect(screen.getByRole('status', { name: 'Loading usage' })).toBeInTheDocument();
    expect(screen.getByText('Sandbox')).toBeInTheDocument();
    expect(getAgentJobUsage).toHaveBeenCalledTimes(1);

    fireEvent.click(screen.getByRole('button', { name: /Raw events/ }));
    expect(screen.getByText('{"event_type":"message"}')).toBeInTheDocument();
  });

  it('leads run details with outcome, actual execution time and the turn number', () => {
    render(
      <JobDetail
        job={makeJob({
          state: 'done',
          createdAt: '2026-09-10T10:00:00Z',
          startedAt: '2026-09-10T10:05:00Z',
          finishedAt: '2026-09-10T10:06:05Z',
          turnNo: 3,
          sandbox: 'Isolated VM',
        })}
      />,
    );
    fireEvent.click(screen.getByRole('button', { name: 'Run details' }));
    const drawer = screen.getByRole('dialog', { name: 'Run details' });
    const outcome = within(drawer).getByRole('region', { name: 'Run outcome' });
    expect(within(outcome).getByText('Done')).toBeInTheDocument();
    expect(within(outcome).getByText('3')).toBeInTheDocument();
    expect(within(outcome).getByText('1m 5s')).toBeInTheDocument();
    expect(outcome.querySelector('time')).toHaveAttribute('dateTime', '2026-09-10T10:05:00Z');
    expect(within(drawer).getByText('Isolated VM')).toBeInTheDocument();
    for (const label of ['Attempts', 'Setup network', 'Agent network', 'Egress denials']) {
      expect(within(drawer).queryByText(label)).not.toBeInTheDocument();
    }
  });

  it.each(['done', 'needs_review'] as const)(
    'keeps %s completion in run details without redundant conversation status',
    (state) => {
      const stateNote = 'agent completed (shared workspace; publication snapshot pending)';
      render(
        <JobDetail
          job={makeJob({
            state,
            stateNote,
            sandbox: 'io.containerd.kata.v2',
          })}
        />,
      );
      expect(screen.queryByText(stateNote)).not.toBeInTheDocument();
      expect(screen.queryByText('Needs review')).not.toBeInTheDocument();
      expect(screen.queryByText('Completed')).not.toBeInTheDocument();
      expect(screen.queryByText('Done')).not.toBeInTheDocument();
      expect(screen.queryByText('Run completed')).not.toBeInTheDocument();
      expect(screen.queryByText('Run complete')).not.toBeInTheDocument();
      fireEvent.click(screen.getByRole('button', { name: 'Run details' }));
      const outcome = screen.getByRole('region', { name: 'Run outcome' });
      expect(
        within(outcome).getByText(state === 'needs_review' ? 'Completed' : 'Done'),
      ).toBeInTheDocument();
      expect(screen.getByText('Isolated VM')).toBeInTheDocument();
      expect(screen.queryByText('io.containerd.kata.v2')).not.toBeInTheDocument();
      expect(screen.queryByText(stateNote)).not.toBeInTheDocument();
    },
  );

  it('shows queued work as not started without relabeling its submission time', () => {
    render(
      <JobDetail
        job={makeJob({ state: 'queued', createdAt: '2026-09-10T10:00:00Z', attempts: [] })}
      />,
    );
    fireEvent.click(screen.getByRole('button', { name: 'Run details' }));
    const outcome = screen.getByRole('region', { name: 'Run outcome' });
    expect(within(outcome).getByText('Not started')).toBeInTheDocument();
    expect(within(outcome).queryByText('Elapsed')).not.toBeInTheDocument();
    expect(outcome.querySelector('time')).toBeNull();
  });

  it('keeps missing terminal timing unknown instead of counting through today', () => {
    render(
      <JobDetail
        job={makeJob({
          state: 'failed',
          createdAt: '2026-09-10T09:00:00Z',
          startedAt: '2026-09-10T10:00:00Z',
          finishedAt: null,
        })}
      />,
    );
    fireEvent.click(screen.getByRole('button', { name: 'Run details' }));
    const row = screen.getByText('Elapsed').parentElement!;
    expect(within(row).getByText('Not recorded')).toBeInTheDocument();
  });

  it('updates elapsed time while a run is active and stops when it finishes', () => {
    vi.useFakeTimers();
    vi.setSystemTime(new Date('2026-09-10T10:00:10Z'));
    try {
      const job = makeJob({ startedAt: '2026-09-10T10:00:00Z', finishedAt: null });
      const { rerender, unmount } = render(<JobDetail job={job} />);
      fireEvent.click(screen.getByRole('button', { name: 'Run details' }));
      expect(screen.getByText('10s')).toBeInTheDocument();
      act(() => vi.advanceTimersByTime(2000));
      expect(screen.getByText('12s')).toBeInTheDocument();
      rerender(<JobDetail job={{ ...job, state: 'done', finishedAt: '2026-09-10T10:00:11Z' }} />);
      act(() => vi.advanceTimersByTime(2000));
      expect(screen.getByText('11s')).toBeInTheDocument();
      unmount();
    } finally {
      vi.useRealTimers();
    }
  });

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
    fireEvent.click(screen.getByRole('button', { name: 'Run details' }));
    const artifacts = screen.getByRole('region', { name: 'Workspace changes' });
    expect(within(artifacts).getByText('1 file changed')).toBeInTheDocument();
    expect(within(artifacts).getByText('+1')).toBeInTheDocument();
    expect(within(artifacts).getByText('−0')).toBeInTheDocument();
    expect(within(artifacts).getByText(/other turns and chats sharing/)).toBeInTheDocument();
    expect(within(artifacts).getByRole('link', { name: 'Open draft PR' })).toHaveAttribute(
      'href',
      prUrl,
    );
    fireEvent.click(within(artifacts).getByRole('button', { name: 'View changes' }));
    expect(screen.queryByRole('dialog', { name: 'Run details' })).not.toBeInTheDocument();
    expect(screen.getByRole('region', { name: 'Job workspace' })).toBeInTheDocument();
    await waitFor(() => expect(getAgentJobGit).toHaveBeenCalledWith('ajob_1'));
    const git = screen.getByLabelText('Git workspace');
    await waitFor(() => expect(within(git).getByRole('button', { name: 'Refresh' })).toBeEnabled());
    expect(git).toHaveTextContent('agent/ajob_1');
    expect(git).not.toHaveTextContent('Live workspace');
    expect(git).not.toHaveTextContent('Shared workspace');
  });

  it.each([true, false])(
    'distinguishes an empty workspace from missing changes (%s)',
    (available) => {
      render(
        <JobDetail
          job={makeJob({
            state: 'needs_review',
            workspaceDiffAvailable: available,
            diffFiles: [],
            diffLines: [],
            diffStat: undefined,
          })}
        />,
      );
      fireEvent.click(screen.getByRole('button', { name: 'Run details' }));
      const artifacts = screen.getByRole('region', { name: 'Workspace changes' });
      expect(
        within(artifacts).getByText(
          available ? '0 files changed' : 'Workspace changes unavailable',
        ),
      ).toBeInTheDocument();
      expect(within(artifacts).getByRole('button', { name: 'View changes' })).toBeInTheDocument();
      expect(within(artifacts).queryByRole('button', { name: /Publish/ })).not.toBeInTheDocument();
      expect(
        within(artifacts).queryByRole('link', { name: 'Open draft PR' }),
      ).not.toBeInTheDocument();
    },
  );

  it('shows a warning only when the sandbox actually blocked a request', () => {
    const { rerender } = render(<JobDetail job={makeJob()} />);
    fireEvent.click(screen.getByRole('button', { name: 'Run details' }));
    const drawer = screen.getByRole('dialog', { name: 'Run details' });
    expect(within(drawer).queryByRole('note')).not.toBeInTheDocument();
    rerender(<JobDetail job={makeJob({ egressDenials: 3 })} />);
    expect(within(drawer).getByRole('note')).toHaveTextContent(
      '3 network requests were blocked by the sandbox.',
    );
  });

  it('requires a new chat only when context loss is confirmed', () => {
    const job = makeJob({ state: 'cancelled' });
    const { rerender } = render(<JobDetail job={job} />);
    fireEvent.click(screen.getByRole('button', { name: 'Run details' }));
    const outcome = screen.getByRole('region', { name: 'Run outcome' });
    expect(within(outcome).getByText(/Send a follow-up to continue/)).toBeInTheDocument();
    expect(within(outcome).queryByRole('link', { name: 'New chat' })).not.toBeInTheDocument();
    rerender(<JobDetail job={{ ...job, nativeState: 'lost' }} />);
    expect(within(outcome).getByText(/This chat can no longer be resumed/)).toBeInTheDocument();
    expect(within(outcome).getByRole('link', { name: 'New chat' })).toHaveAttribute('href', '/');
    expect(within(outcome).queryByText(/Send a follow-up to continue/)).not.toBeInTheDocument();
  });

  it('links a failure to its matching raw event and focuses it', () => {
    const failure = JSON.stringify({ event_type: 'error', payload: { detail: 'Agent exited 1' } });
    render(
      <JobDetail
        job={makeJob({
          state: 'failed',
          stateNote: 'Agent exited 1',
          rawLines: [
            JSON.stringify({ event_type: 'tool_result', payload: { is_error: true } }),
            failure,
          ],
        })}
      />,
    );
    fireEvent.click(screen.getByRole('button', { name: 'Run details' }));
    fireEvent.click(screen.getByRole('button', { name: 'View failure event' }));
    const selected = screen.getByLabelText('Failure event');
    expect(selected).toHaveTextContent(failure);
    expect(selected).toHaveFocus();
    expect(screen.getByRole('button', { name: /Raw events/ })).toHaveAttribute(
      'aria-pressed',
      'true',
    );
  });

  it('does not point a failure at an unrelated error or an earlier execution', () => {
    render(
      <JobDetail
        job={makeJob({
          state: 'failed',
          stateNote: 'Agent exited 1',
          currentAttemptNo: 2,
          attempts: [{ id: 2, no: 2, status: 'finished' }],
          rawLines: [
            JSON.stringify({
              attempt_id: 1,
              event_type: 'error',
              payload: { detail: 'Agent exited 1' },
            }),
            JSON.stringify({
              attempt_id: 2,
              event_type: 'error',
              payload: { detail: 'Temporary tool failure' },
            }),
          ],
        })}
      />,
    );
    fireEvent.click(screen.getByRole('button', { name: 'Run details' }));
    expect(screen.queryByRole('button', { name: 'View failure event' })).not.toBeInTheDocument();
    fireEvent.click(screen.getByRole('button', { name: 'Inspect raw events' }));
    expect(screen.queryByLabelText('Failure event')).not.toBeInTheDocument();
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
    fireEvent.click(screen.getByRole('button', { name: 'Run details' }));
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

    const terminal = screen.getByLabelText('Workspace terminal');
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

    await waitFor(() => expect(listAgentTerminals).toHaveBeenCalledWith('ajob_1'));
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

    await waitFor(() => expect(listAgentTerminals).toHaveBeenCalledWith('ajob_1'));
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
      fireEvent.click(await screen.findByRole('button', { name: /README\.md/ }));

      // The file takes the pane rather than sharing it with a 200px sidebar…
      await waitFor(() =>
        expect(screen.getByLabelText('Workspace files')).toHaveTextContent('# Narrow'),
      );
      expect(screen.queryByRole('button', { name: /README\.md/ })).not.toBeInTheDocument();

      // …and the header button walks back to the tree instead of hiding it.
      fireEvent.click(screen.getByRole('button', { name: 'Show file tree' }));
      expect(await screen.findByRole('button', { name: /README\.md/ })).toBeInTheDocument();
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

    fireEvent.click(screen.getByRole('button', { name: /example\.ts/ }));

    await waitFor(() => expect(getAgentJobFiles).toHaveBeenCalledWith('ajob_1', 'src/example.ts'));
    expect(await screen.findByText('src')).toBeInTheDocument();
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

    expect(screen.getByText(/This chat can no longer be resumed/i)).toBeInTheDocument();
    expect(screen.getByRole('textbox', { name: 'Add a follow-up' })).toBeDisabled();
    expect(screen.getByRole('button', { name: 'Send follow-up' })).toBeDisabled();
    expect(screen.queryByText(/restores any saved changes/i)).toBeNull();
  });

  it('does not offer a follow-up from an older turn after the membership is lost', () => {
    render(
      <JobDetail job={makeJob({ state: 'done', contextStatus: 'live', nativeState: 'lost' })} />,
    );

    expect(screen.getByLabelText('Add a follow-up')).toBeDisabled();
    expect(screen.getByText(/This chat can no longer be resumed/)).toBeInTheDocument();
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
    vi.mocked(followUpAgentJob).mockReset();
    vi.mocked(getAgentConfig).mockReset();
    vi.mocked(getAgentConfig).mockResolvedValue(THINKING_CONFIG);
    vi.mocked(followUpAgentJob).mockResolvedValue({ id: 'ajob_child' } as never);
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

  it('shows the effort a turn ran at, and nothing for turns that predate it', () => {
    const { unmount } = render(<JobDetail job={makeJob({ reasoningEffort: 'max' })} />);
    fireEvent.click(screen.getByRole('button', { name: 'Run details' }));
    expect(screen.getByText('Reasoning')).toBeInTheDocument();
    expect(screen.getByText('max')).toBeInTheDocument();
    unmount();

    render(<JobDetail job={makeJob()} />);
    fireEvent.click(screen.getByRole('button', { name: 'Run details' }));
    // An absent row is the honest display of a turn nobody chose a depth for,
    // which is every turn created before this control existed.
    expect(screen.queryByText('Reasoning')).not.toBeInTheDocument();
  });

  it('inherits silently when untouched, and sends an actual change', async () => {
    const { unmount } = render(<JobDetail job={makeJob({ reasoningEffort: 'high' })} />);

    // Untouched: the field is absent, which the backend reads as "inherit".
    fireEvent.change(screen.getByLabelText('Add a follow-up'), { target: { value: 'again' } });
    fireEvent.click(screen.getByRole('button', { name: 'Send follow-up' }));
    await waitFor(() =>
      expect(followUpAgentJob).toHaveBeenCalledWith('ajob_1', { prompt: 'again' }),
    );
    unmount();

    vi.mocked(followUpAgentJob).mockClear();
    render(<JobDetail job={makeJob({ reasoningEffort: 'high' })} />);
    fireEvent.change(await screen.findByLabelText('Reasoning for this turn'), {
      target: { value: 'low' },
    });
    fireEvent.change(screen.getByLabelText('Add a follow-up'), { target: { value: 'easy one' } });
    fireEvent.click(screen.getByRole('button', { name: 'Send follow-up' }));

    await waitFor(() =>
      expect(followUpAgentJob).toHaveBeenCalledWith('ajob_1', {
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
    fireEvent.click(screen.getByRole('button', { name: 'Send follow-up' }));

    await waitFor(() =>
      expect(followUpAgentJob).toHaveBeenCalledWith('ajob_1', {
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
    fireEvent.click(screen.getByRole('button', { name: 'Send follow-up' }));

    await waitFor(() =>
      expect(followUpAgentJob).toHaveBeenCalledWith('ajob_1', {
        prompt: 'switch',
        model: 'glm-test',
      }),
    );
  });
});

describe('the trajectory tab', () => {
  afterEach(() => cleanup());

  it('switches between the reading of the stream and the stream itself', () => {
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
    expect(screen.queryByLabelText('Search events')).toBeNull();

    fireEvent.click(screen.getByRole('button', { name: 'trajectory' }));

    expect(screen.getByLabelText('Search events')).toBeInTheDocument();
    expect(screen.getByText('Bash')).toBeInTheDocument();

    fireEvent.click(screen.getByRole('button', { name: 'chat' }));

    expect(screen.queryByLabelText('Search events')).toBeNull();
  });

  it('separates stored events by conversation turn', () => {
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

    expect(screen.getByText('Turn 1')).toBeInTheDocument();
    expect(screen.getByText('Turn 2')).toBeInTheDocument();
    expect(screen.getByText('Read')).toBeInTheDocument();
    expect(screen.getByText('Bash')).toBeInTheDocument();
  });

  it('still renders when no stored stream was handed down', () => {
    // Fixtures and older callers pass no events; the tab must degrade to its
    // empty state rather than throwing on an undefined list.
    render(<JobDetail job={makeJob()} />);

    fireEvent.click(screen.getByRole('button', { name: 'trajectory' }));

    expect(screen.getByText(/stored no events/)).toBeInTheDocument();
  });
});

describe('sending a follow-up', () => {
  afterEach(() => cleanup());

  it('clears the composer instead of leaving it disabled with the sent text', async () => {
    // A remount used to do this. Keying on the conversation rather than the
    // turn removed the remount, and with it the only thing that ever reset
    // either — the composer stayed disabled with the prompt still in it.
    vi.mocked(followUpAgentJob).mockResolvedValue({ id: 'ajob_next' } as never);
    render(<JobDetail job={makeJob()} />);
    const box = screen.getByPlaceholderText('Add a follow-up');

    fireEvent.change(box, { target: { value: 'and now the docs' } });
    fireEvent.submit(box.closest('form') as HTMLFormElement);

    await waitFor(() => expect(followUpAgentJob).toHaveBeenCalled());
    await waitFor(() => expect((box as HTMLTextAreaElement).value).toBe(''));
    expect(box).not.toBeDisabled();
  });
});
