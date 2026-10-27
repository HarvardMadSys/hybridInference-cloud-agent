// @vitest-environment jsdom
import '@testing-library/jest-dom/vitest';

import { cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { getAgentConfig, getAgentJobFiles, getAgentJobGit } from '@/lib/api/agents';
import { getThreadQueue } from '@/lib/api/pendingMessages';

import { JobDetail } from './JobDetail';
import type { AgentDiffFile, AgentEvent, AgentHistoryTurn, AgentJob } from './types';
import { chooseOption } from '@/components/ui/__tests__/select';

// What an interaction draws again, counted where the page's costly parts render.
const drawn = vi.hoisted(() => ({
  currentTurn: 0,
  workspace: 0,
  links: 0,
  storedSubagentLookups: 0,
  storedEvents: new Set<unknown>(),
}));

vi.mock('./ChatMessages', async (importOriginal) => {
  const original = await importOriginal<typeof import('./ChatMessages')>();
  return {
    ...original,
    // The page draws the current turn's list; stored turns draw their own.
    EventList: (props: Parameters<typeof original.EventList>[0]) => {
      drawn.currentTurn += 1;
      return original.EventList(props);
    },
  };
});

vi.mock('./WorkspaceTabBar', async (importOriginal) => {
  const original = await importOriginal<typeof import('./WorkspaceTabBar')>();
  return {
    ...original,
    WorkspaceTabBar: (props: Parameters<typeof original.WorkspaceTabBar>[0]) => {
      drawn.workspace += 1;
      return original.WorkspaceTabBar(props);
    },
  };
});

// Every drawn workspace link reads its path.
vi.mock('@/lib/workspaceLink', async (importOriginal) => {
  const original = await importOriginal<typeof import('@/lib/workspaceLink')>();
  return {
    ...original,
    workspaceLinkPath: (href: string) => {
      drawn.links += 1;
      return original.workspaceLinkPath(href);
    },
  };
});

vi.mock('./activityOverview', async (importOriginal) => {
  const original = await importOriginal<typeof import('./activityOverview')>();
  return {
    ...original,
    subagentStartedBy: (...args: Parameters<typeof original.subagentStartedBy>) => {
      if (drawn.storedEvents.has(args[1])) drawn.storedSubagentLookups += 1;
      return original.subagentStartedBy(...args);
    },
  };
});

const navigation = vi.hoisted(() => ({ push: vi.fn() }));
vi.mock('next/navigation', () => ({
  useRouter: () => navigation,
  usePathname: () => '/job-live',
}));

vi.mock('@/lib/api/agents', async (importOriginal) => ({
  ...(await importOriginal<typeof import('@/lib/api/agents')>()),
  getAgentConfig: vi.fn(),
  getAgentJobUsage: vi.fn().mockResolvedValue(null),
  refreshAgentJobUsage: vi.fn().mockResolvedValue(null),
  getAgentThreadStats: vi.fn().mockResolvedValue(null),
  listAgentTerminals: vi.fn().mockResolvedValue([]),
  getAgentJobFiles: vi.fn(),
  getAgentJobGit: vi.fn(),
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

vi.mock('@/lib/api/skills', () => ({ listSkills: vi.fn().mockResolvedValue({ skills: [] }) }));

vi.mock('@/lib/api/attachments', () => ({
  uploadAttachment: vi.fn(),
  deleteAttachment: vi.fn().mockResolvedValue(undefined),
}));

vi.mock('@/lib/api/pendingMessages', async (importOriginal) => ({
  ...(await importOriginal<typeof import('@/lib/api/pendingMessages')>()),
  getThreadQueue: vi.fn(),
  submitThreadMessage: vi.fn(),
  sendPendingMessageNow: vi.fn(),
}));

const JOB_ID = 'job-live';
const THREAD_ID = 'thread-live';
const OWNER = 'owner';

function command(prefix: string, sourceEventId: number): AgentEvent {
  return {
    kind: 'tool_use',
    tool: 'Bash',
    detail: `npm test -- ${prefix}`,
    input: { command: `npm test -- ${prefix}` },
    sourceEventId,
    attemptNo: 1,
    resultState: 'received',
    output: ['ok'],
  };
}

function turnEvents(prefix: string, base: number): AgentEvent[] {
  return [
    {
      kind: 'tool_use',
      tool: 'spawn_agent',
      id: `${prefix}-agent`,
      input: { task_name: `Review ${prefix}` },
      detail: `Review ${prefix}`,
      sourceEventId: base,
      attemptNo: 1,
      resultState: 'received',
      output: [`{"agent_id":"${prefix}-worker"}`],
    },
    command(prefix, base + 1),
    {
      kind: 'message',
      text: `Done with ${prefix}: see [app.ts](file:///workspace/src/app.ts).`,
      sourceEventId: base + 2,
      attemptNo: 1,
    },
  ];
}

// As the display adapter hands them over: stored turns, unchanged rows and the
// parsed patch keep their identity from one streamed event to the next.
const STORED: AgentHistoryTurn[] = Array.from({ length: 4 }, (_, index) => ({
  jobId: `past-${index}`,
  turnNo: index + 1,
  forkable: false,
  state: 'done',
  messages: [],
  events: turnEvents(`past-${index}`, index * 10),
}));
const CURRENT = turnEvents('current', 100);
const LOCKFILE: AgentDiffFile = {
  path: 'package-lock.json',
  add: 4001,
  del: 0,
  lines: Array.from({ length: 4001 }, (_, index) => ({
    marker: 'add',
    text: `+    "dependency-${index}": "^1.${index}.0",`,
  })),
};
const SOURCE: AgentDiffFile = {
  path: 'src/app.ts',
  add: 1,
  del: 0,
  lines: [{ marker: 'add', text: '+export const ready = true;' }],
};
const DIFF_FILES = [LOCKFILE.path, SOURCE.path];
const DIFF_DETAILS = [LOCKFILE, SOURCE];

function conversation(streamed: AgentEvent[] = []): AgentJob {
  return {
    id: JOB_ID,
    threadId: THREAD_ID,
    title: 'Current task',
    prompt: 'Keep going',
    state: 'running',
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
    attempts: [{ no: 1, status: 'live' }],
    currentAttemptNo: 1,
    turnNo: STORED.length + 1,
    historyTurns: STORED,
    events: [...CURRENT, ...streamed],
    diffFiles: DIFF_FILES,
    diffFileDetails: DIFF_DETAILS,
    diffStat: { add: 4002, del: 0 },
    gates: [],
    usage: { tokensIn: '0', tokensOut: '0', cachePct: 0, turns: 1 },
  };
}

function resetCounts() {
  drawn.currentTurn = 0;
  drawn.workspace = 0;
  drawn.links = 0;
  drawn.storedSubagentLookups = 0;
}

/** Open the workspace on its Git tab, wait for it, and close it again. */
async function retainWorkspace() {
  fireEvent.click(screen.getByRole('button', { name: 'Open workspace' }));
  const git = await screen.findByLabelText('Git workspace');
  await waitFor(() => expect(git).toHaveTextContent('Unavailable'));
  fireEvent.click(screen.getByRole('button', { name: 'Close workspace' }));
  expect(git).not.toBeVisible();
}

beforeEach(() => {
  localStorage.clear();
  sessionStorage.clear();
  vi.clearAllMocks();
  drawn.storedEvents = new Set(STORED.flatMap((turn) => turn.events));
  vi.mocked(getAgentConfig).mockResolvedValue({
    repos: ['owner/repository'],
    runtimes: ['claude-code'],
    models: ['qwen-test'],
    github_connected: true,
    github_install_url: null,
  } as Awaited<ReturnType<typeof getAgentConfig>>);
  vi.mocked(getThreadQueue).mockImplementation(async (threadId) => ({
    thread_id: threadId,
    pending_messages: [],
    queue_paused_reason: null,
    latest_job_id: JOB_ID,
    latest_turn_no: STORED.length + 1,
    latest_job_state: 'running',
    mid_turn_input_supported: true,
  }));
  vi.mocked(getAgentJobGit).mockResolvedValue({
    available: false,
  } as Awaited<ReturnType<typeof getAgentJobGit>>);
  vi.mocked(getAgentJobFiles).mockImplementation(async (_jobId, path = '') => {
    if (path === '') {
      return {
        path: '',
        kind: 'directory',
        entries: [{ name: 'src', path: 'src', kind: 'directory' }],
      };
    }
    return {
      path,
      kind: 'file',
      content: 'export const ready = true;',
      size: 26,
      binary: false,
      truncated: false,
    };
  });
});

afterEach(() => cleanup());

describe('job page rendering work', () => {
  it('draws neither the transcript, the workspace, nor any link again while the owner types', async () => {
    render(<JobDetail job={conversation()} userId={OWNER} />);
    await screen.findByLabelText('Model for this turn');
    await retainWorkspace();

    resetCounts();
    const composer = screen.getByRole('textbox', { name: 'Add a follow-up' });
    for (const value of ['h', 'he', 'hel', 'hell', 'hello']) {
      fireEvent.change(composer, { target: { value } });
    }

    expect(composer).toHaveValue('hello');
    // The composer itself still follows the text: a running turn takes it next.
    expect(screen.getByRole('button', { name: 'Queue' })).toBeEnabled();
    expect(drawn).toMatchObject({
      currentTurn: 0,
      workspace: 0,
      links: 0,
      storedSubagentLookups: 0,
    });
  });

  it('leaves the retained workspace and the settled turns undrawn while the current turn streams', async () => {
    const view = render(<JobDetail job={conversation()} userId={OWNER} />);
    await screen.findByLabelText('Model for this turn');
    await retainWorkspace();

    resetCounts();
    let streamed: AgentEvent[] = [];
    for (let index = 0; index < 3; index += 1) {
      streamed = [...streamed, command(`streamed-${index}`, 200 + index)];
      view.rerender(<JobDetail job={conversation(streamed)} userId={OWNER} />);
    }

    expect(screen.getByText('Bash: npm test -- streamed-2')).toBeInTheDocument();
    expect(drawn.currentTurn).toBeGreaterThan(0);
    expect(drawn).toMatchObject({ workspace: 0, links: 0, storedSubagentLookups: 0 });
    // The retained panels still answer, with the stream's latest state.
    fireEvent.click(screen.getByRole('button', { name: 'Open workspace' }));
    expect(screen.getByLabelText('Git workspace')).toBeVisible();
  });

  it('still opens a subagent from a settled turn after the conversation has moved on', async () => {
    const view = render(<JobDetail job={conversation()} userId={OWNER} />);
    view.rerender(<JobDetail job={conversation([command('streamed', 200)])} userId={OWNER} />);

    fireEvent.click(
      within(screen.getByRole('region', { name: 'Turn 2' })).getByRole('button', {
        name: 'Switch subagent: Review past-1',
      }),
    );
    expect(screen.getByRole('region', { name: 'Subagent: Review past-1' })).toBeInTheDocument();
  });

  it('opens a file named in a settled turn with the workspace as it is now', async () => {
    render(<JobDetail job={conversation()} userId={OWNER} />);
    const turn = within(screen.getByRole('region', { name: 'Turn 1' }));

    fireEvent.click(turn.getByRole('button', { name: 'app.ts' }));
    const files = await screen.findByLabelText('Workspace files');
    await waitFor(() => expect(getAgentJobFiles).toHaveBeenCalledWith(JOB_ID, 'src/app.ts'));

    // Asked again once the workspace was closed, the link reopens the same file.
    fireEvent.click(screen.getByRole('button', { name: 'Close workspace' }));
    expect(files).not.toBeVisible();
    vi.mocked(getAgentJobFiles).mockClear();
    fireEvent.click(turn.getByRole('button', { name: 'app.ts' }));
    expect(screen.getByLabelText('Workspace files')).toBe(files);
    expect(files).toBeVisible();
    await waitFor(() => expect(getAgentJobFiles).toHaveBeenCalledWith(JOB_ID, 'src/app.ts'));
  });

  it('mounts the Git panel only once its tab is opened, then keeps it', async () => {
    sessionStorage.setItem(
      `agents.workspace.tabs.${OWNER}:${THREAD_ID}`,
      JSON.stringify({ selected: 'files', chats: [], nextNumber: 1 }),
    );
    render(<JobDetail job={conversation()} userId={OWNER} />);

    fireEvent.click(screen.getByRole('button', { name: 'Open workspace' }));
    await screen.findByLabelText('Workspace files');
    expect(screen.queryByLabelText('Git workspace')).not.toBeInTheDocument();

    fireEvent.click(screen.getByRole('tab', { name: /^Git/ }));
    const git = await screen.findByLabelText('Git workspace');
    fireEvent.click(screen.getByRole('tab', { name: 'Files' }));
    expect(screen.getByLabelText('Git workspace')).toBe(git);
    expect(git).not.toBeVisible();
  });

  it('draws a large diff up to the preview limit and says where it stops', async () => {
    render(<JobDetail job={conversation()} userId={OWNER} />);
    fireEvent.click(screen.getByRole('button', { name: 'Open workspace' }));

    const diff = await screen.findByLabelText('Diff for package-lock.json');
    expect(diff.querySelectorAll('code > span')).toHaveLength(4000);
    expect(diff).toHaveTextContent('"dependency-3999"');
    expect(diff).not.toHaveTextContent('"dependency-4000"');
    // Formatted as the panel formats it, in whatever locale the run has.
    expect(
      screen.getByText(
        `Diff limited to the first ${(4000).toLocaleString()} of ${(4001).toLocaleString()} lines.`,
      ),
    ).toBeInTheDocument();

    chooseOption(screen.getByRole('combobox', { name: 'Changed file' }), /^src\/app\.ts /);
    expect(screen.getByLabelText('Diff for src/app.ts')).toHaveTextContent(
      '+export const ready = true;',
    );
    expect(screen.queryByText(/Diff limited/)).not.toBeInTheDocument();
  });
});
