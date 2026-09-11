// @vitest-environment jsdom
import '@testing-library/jest-dom/vitest';

import { act, cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import {
  cancelAgentJob,
  createAgentTerminal,
  deleteAgentTerminal,
  getAgentAttemptTelemetry,
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
  renameAgentJob,
  pinAgentJob,
  unpinAgentJob,
  streamAgentTerminal,
  writeAgentTerminalInput,
  writeAgentJobFile,
  type AgentJobFilesApi,
  type AgentJobUsageApi,
} from '@/lib/api/agents';

import { announceAgentSessionsChanged } from '@/lib/api/agentSessionEvents';

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
  getAgentAttemptTelemetry: vi.fn(),
  getAgentThreadStats: vi.fn(),
  listAgentTerminals: vi.fn(),
  resizeAgentTerminal: vi.fn(),
  refreshAgentJobUsage: vi.fn(),
  restoreAgentJob: vi.fn(),
  renameAgentJob: vi.fn(),
  pinAgentJob: vi.fn(),
  unpinAgentJob: vi.fn(),
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
    egressDenials: 0,
    ...overrides,
  };
}

function openWorkspace() {
  fireEvent.click(screen.getByRole('button', { name: 'Open workspace' }));
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
  it.each([undefined, 'previous-job'])(
    'uses the same queue message with parent %s',
    async (parentJobId) => {
      render(<JobDetail job={makeJob({ state: 'queued', events: [], parentJobId })} />);
      expect(await screen.findByText('Waiting to start')).toBeInTheDocument();
      expect(screen.getByLabelText('Conversation status')).toHaveTextContent(/^Queued$/);
      expect(screen.queryByText(/Queued after (the )?current run/)).not.toBeInTheDocument();
    },
  );

  beforeEach(() => {
    navigation.push.mockReset();
    vi.mocked(cancelAgentJob).mockReset();
    vi.mocked(followUpAgentJob).mockReset();
    vi.mocked(forkAgentJob).mockReset();
    vi.mocked(restoreAgentJob).mockReset();
    vi.mocked(renameAgentJob).mockReset();
    vi.mocked(pinAgentJob).mockReset();
    vi.mocked(unpinAgentJob).mockReset();
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
    expect(within(turn).getByRole('button', { name: 'View execution' })).toBeInTheDocument();
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

  it.each(['done', 'needs_review'] as const)(
    'keeps successful %s replies free of completion metadata',
    (state) => {
      const stateNote = 'agent completed (shared workspace; publication snapshot pending)';
      render(<JobDetail job={makeJob({ state, stateNote, sandbox: 'io.containerd.kata.v2' })} />);
      expect(screen.getByLabelText('Conversation status')).toHaveTextContent(/^Idle$/);
      expect(screen.queryByText(stateNote)).not.toBeInTheDocument();
      expect(screen.queryByText(/^(Done|Completed|Outcome|Needs review)$/)).not.toBeInTheDocument();
      expect(screen.queryByRole('dialog', { name: 'Run details' })).not.toBeInTheDocument();
      expect(screen.queryByText('Sandbox')).not.toBeInTheDocument();
    },
  );

  it.each(['queued', 'running', 'failed'] as const)(
    'shows the %s conversation state without changing the current turn',
    (state) => {
      render(<JobDetail job={makeJob({ state })} />);
      expect(screen.getByLabelText('Conversation status')).toHaveTextContent(
        state[0].toUpperCase() + state.slice(1),
      );
      expect(screen.getByLabelText('Agent activity')).toHaveTextContent('I found the cause.');
      expect(screen.queryByText(/Execution history/)).not.toBeInTheDocument();
      if (state === 'failed') {
        expect(screen.queryByRole('button', { name: 'Stop' })).not.toBeInTheDocument();
      } else {
        expect(screen.getByRole('button', { name: 'Stop' })).toBeEnabled();
      }
    },
  );

  it('becomes idle after stopping while preserving the stopped result and follow-up', () => {
    const job = makeJob();
    const { rerender } = render(<JobDetail job={job} />);
    expect(screen.getByLabelText('Conversation status')).toHaveTextContent('Running');
    rerender(<JobDetail job={{ ...job, state: 'cancelled' }} />);
    expect(screen.getByLabelText('Conversation status')).toHaveTextContent(/^Idle$/);
    expect(screen.getByText('Run stopped')).toBeInTheDocument();
    expect(screen.getByLabelText('Add a follow-up')).toBeEnabled();
    expect(screen.queryByRole('button', { name: 'Stop' })).not.toBeInTheDocument();
  });

  it.each(['done', 'cancelled'] as const)(
    'shows ongoing conversation activity when the selected turn is %s',
    (state) => {
      const job = makeJob({ state, conversationState: 'running' });
      const { rerender } = render(<JobDetail job={job} />);
      expect(screen.getByLabelText('Conversation status')).toHaveTextContent(/^Running$/);
      // A historical turn does not gain a Stop action for another turn.
      expect(screen.queryByRole('button', { name: 'Stop' })).not.toBeInTheDocument();
      if (state === 'cancelled') expect(screen.getByText('Run stopped')).toBeInTheDocument();
      rerender(<JobDetail job={{ ...job, conversationState: 'idle' }} />);
      expect(screen.getByLabelText('Conversation status')).toHaveTextContent(/^Idle$/);
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

  it('shows a warning only when the sandbox actually blocked a request', () => {
    const { rerender } = render(<JobDetail job={makeJob()} />);
    const task = screen.getByRole('region', { name: 'Task progress' });
    expect(within(task).queryByRole('note')).not.toBeInTheDocument();
    rerender(<JobDetail job={makeJob({ egressDenials: 3 })} />);
    expect(within(task).getByRole('note')).toHaveTextContent(
      '3 network requests were blocked by this workspace’s network settings.',
    );
    rerender(
      <JobDetail
        job={makeJob({
          egressDenials: 3,
          events: [{ kind: 'egress_denied', host: 'example.com', attempts: 3, attemptNo: 1 }],
        })}
      />,
    );
    expect(within(task).getByText('Network request blocked')).toBeInTheDocument();
    expect(within(task).queryByRole('note')).not.toBeInTheDocument();
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
    expect(screen.getByLabelText('Conversation status')).toHaveTextContent(/^Idle$/);
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
    expect(screen.getByLabelText('Conversation status')).toHaveTextContent(/^Idle$/);
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
    expect(followUpAgentJob).not.toHaveBeenCalled();
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

    expect(screen.getByText(/We couldn’t reconnect this chat/i)).toBeInTheDocument();
    expect(screen.getByRole('textbox', { name: 'Add a follow-up' })).toBeDisabled();
    expect(screen.getByRole('button', { name: 'Send follow-up' })).toBeDisabled();
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
    expect(screen.queryByLabelText('Search steps')).toBeNull();

    fireEvent.click(screen.getByRole('button', { name: 'trajectory' }));

    expect(screen.getByLabelText('Search steps')).toBeInTheDocument();
    expect(screen.getByText('Bash')).toBeInTheDocument();

    fireEvent.click(screen.getByRole('button', { name: 'chat' }));

    expect(screen.queryByLabelText('Search steps')).toBeNull();
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

  it('opens the selected step in the right-hand region, not under the row', () => {
    render(<JobDetail job={makeJob()} events={STORED_CALL} />);

    fireEvent.click(screen.getByRole('button', { name: 'trajectory' }));
    fireEvent.click(screen.getByText('Bash'));

    const panel = screen.getByRole('region', { name: 'Step details' });
    expect(within(panel).getByText(/Tool · Turn 1 · Step 1/)).toBeInTheDocument();
    expect(within(panel).getByRole('tab', { name: 'Schema' })).toBeInTheDocument();
    // The result joined its call rather than becoming a second row.
    expect(within(panel).getByText(/2 passed/)).toBeInTheDocument();
    expect(screen.getAllByRole('tab')).toHaveLength(5);
  });

  it('keeps one right-hand region: opening the workspace closes the details', () => {
    render(<JobDetail job={makeJob()} events={STORED_CALL} />);

    fireEvent.click(screen.getByRole('button', { name: 'trajectory' }));
    fireEvent.click(screen.getByText('Bash'));
    expect(screen.getByRole('region', { name: 'Step details' })).toBeInTheDocument();

    openWorkspace();

    expect(screen.queryByRole('region', { name: 'Step details' })).not.toBeInTheDocument();
    expect(screen.getByRole('region', { name: 'Job workspace' })).toBeInTheDocument();

    fireEvent.click(screen.getByText('Bash'));

    expect(screen.getByRole('region', { name: 'Step details' })).toBeInTheDocument();
    expect(screen.queryByRole('region', { name: 'Job workspace' })).not.toBeInTheDocument();
  });

  it('closes the details on Escape without also stopping the run', () => {
    render(<JobDetail job={makeJob({ state: 'running' })} events={STORED_CALL} />);

    fireEvent.click(screen.getByRole('button', { name: 'trajectory' }));
    fireEvent.click(screen.getByText('Bash'));

    fireEvent.keyDown(document.body, { key: 'Escape' });

    expect(screen.queryByRole('region', { name: 'Step details' })).not.toBeInTheDocument();
    expect(cancelAgentJob).not.toHaveBeenCalled();
  });

  it('leaves Escape alone while the reader is typing', () => {
    render(<JobDetail job={makeJob()} events={STORED_CALL} />);

    fireEvent.click(screen.getByRole('button', { name: 'trajectory' }));
    fireEvent.click(screen.getByText('Bash'));

    const search = screen.getByLabelText('Search steps');
    search.focus();
    fireEvent.keyDown(search, { key: 'Escape' });

    expect(screen.getByRole('region', { name: 'Step details' })).toBeInTheDocument();
  });

  it('opens the turn the chat footer points at, even for an older turn', () => {
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
    expect(within(panel).getByRole('heading', { name: 'Run details' })).toBeInTheDocument();
    expect(within(panel).getByText('Run · Turn 1')).toBeInTheDocument();
  });

  it('closes the details when the reader goes back to the conversation', () => {
    render(<JobDetail job={makeJob()} events={STORED_CALL} />);

    fireEvent.click(screen.getByRole('button', { name: 'trajectory' }));
    fireEvent.click(screen.getByText('Bash'));
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
