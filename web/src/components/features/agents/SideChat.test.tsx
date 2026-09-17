// @vitest-environment jsdom
import '@testing-library/jest-dom/vitest';
import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import {
  cancelAgentJob,
  followUpAgentJob,
  forkAgentJob,
  getAgentJob,
  getAgentJobGit,
  getAgentJobThread,
  listAllAgentThreadEvents,
  streamAgentJob,
  type AgentConfigApi,
  type AgentJobApi,
  type AgentThreadEventApi,
} from '@/lib/api/agents';
import { toDisplayJob } from './adapt';
import { SideChat } from './SideChat';
import { conversationReadStorageKey } from './useConversationReadState';
import { sideChatStorageKey } from './useSideChatSession';

vi.mock('@/lib/api/agents', () => ({
  cancelAgentJob: vi.fn(),
  followUpAgentJob: vi.fn(),
  forkAgentJob: vi.fn(),
  getAgentJob: vi.fn(),
  getAgentJobGit: vi.fn(),
  getAgentJobThread: vi.fn(),
  listAllAgentThreadEvents: vi.fn(),
  streamAgentJob: vi.fn(),
}));
vi.mock('./mcp/McpApprovalNotice', () => ({
  McpApprovalNotice: ({
    jobId,
    active,
    onReview,
  }: {
    jobId: string;
    active: boolean;
    onReview: () => void;
  }) => (active ? <button onClick={onReview}>Review tools for {jobId}</button> : null),
}));
vi.mock('./mcp/McpSessionPanel', () => ({
  McpSessionPanel: ({ jobId, threadId }: { jobId: string; threadId: string }) => (
    <div aria-label="Side tool permissions">
      {jobId}:{threadId}
    </div>
  ),
}));

const anchor = {
  id: 'main-2',
  thread_id: 'main',
  sandbox_id: 'shared',
  workspace_shared: true,
  native_state: 'ready',
  native_fork_available: true,
  state: 'succeeded',
  conversation_state: 'idle',
  repo: 'owner/project',
  runtime: 'codex',
  model: 'model-a',
  task_prompt: 'Inherited question',
  title: 'Main task',
  title_source: 'manual',
  turn_no: 2,
  current_attempt_id: null,
  metadata: null,
  detail: null,
  spent_usd: null,
  tokens_in: null,
  tokens_out: null,
  model_calls: null,
  agent_egress_tier: 'platform_only',
  setup_egress_tier: 'trusted',
} as AgentJobApi;
const copied: AgentJobApi = {
  ...anchor,
  id: 'side-copy',
  thread_id: 'side',
  forked_from_job_id: 'main-2',
  fork_anchor_job_id: 'main-2',
};
const next: AgentJobApi = {
  ...copied,
  id: 'side-3',
  turn_no: 3,
  forked_from_job_id: null,
  task_prompt: 'Why keep the files?',
  state: 'running',
  conversation_state: 'running',
  current_attempt_id: 12,
};
const config = { models: ['model-a', 'model-b'] } as AgentConfigApi;
let jobs: AgentJobApi[];
let events: AgentThreadEventApi[];
let userId: string;
let sequence = 0;

beforeEach(() => {
  userId = `side-ui-${++sequence}`;
  vi.clearAllMocks();
  sessionStorage.clear();
  localStorage.clear();
  jobs = [copied];
  events = [];
  vi.mocked(forkAgentJob).mockResolvedValue(copied);
  vi.mocked(getAgentJob).mockImplementation(async (id) => {
    const job = id === anchor.id ? anchor : jobs.find((job) => job.id === id);
    if (!job) throw new Error(`Unknown fixture job ${id}`);
    return job;
  });
  vi.mocked(getAgentJobThread).mockImplementation(async () => ({
    thread_id: 'side',
    jobs: [...jobs],
    messages: [
      { id: 1, role: 'user', content: 'Inherited question', job_id: copied.id, created_at: null },
      {
        id: 2,
        role: 'assistant',
        content: 'Inherited answer',
        job_id: copied.id,
        created_at: null,
      },
    ],
  }));
  vi.mocked(listAllAgentThreadEvents).mockImplementation(async () => [...events]);
  vi.mocked(getAgentJobGit).mockResolvedValue({ available: false } as Awaited<
    ReturnType<typeof getAgentJobGit>
  >);
  vi.mocked(streamAgentJob).mockResolvedValue(undefined);
  vi.mocked(followUpAgentJob).mockImplementation(async (_id, body) => {
    const created = { ...next, task_prompt: body.prompt, model: body.model ?? next.model };
    jobs.push(created);
    return created;
  });
  vi.mocked(cancelAgentJob).mockImplementation(async (id) => {
    jobs = jobs.map((job) =>
      job.id === id
        ? { ...job, state: 'cancelled', conversation_state: 'failed', native_state: 'lost' }
        : job,
    );
    return { id, state: 'cancelled', cancel_requested: true };
  });
});
afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
});

function props(active = true) {
  return { sourceJob: toDisplayJob(anchor), sourceJobs: [anchor], userId, config, active };
}
async function ready() {
  await waitFor(() => expect(getAgentJobThread).toHaveBeenCalledWith('side-copy'));
  await waitFor(() => expect(screen.queryByRole('alert')).not.toBeInTheDocument());
  fireEvent.change(screen.getByRole('textbox', { name: 'Message side chat' }), {
    target: { value: 'Why keep the files?' },
  });
  await waitFor(() =>
    expect(screen.getByRole('button', { name: 'Send side chat message' })).toBeEnabled(),
  );
}
async function send() {
  await ready();
  fireEvent.click(screen.getByRole('button', { name: 'Send side chat message' }));
  await screen.findByRole('button', { name: 'Stop side chat' });
}

describe('Side Chat', () => {
  it('stays lazy and initially hides the inherited transcript without an empty-state explanation', async () => {
    const view = render(<SideChat {...props(false)} />);
    expect(forkAgentJob).not.toHaveBeenCalled();
    view.rerender(<SideChat {...props()} />);
    await ready();
    expect(screen.queryByText('Inherited question')).not.toBeInTheDocument();
    expect(screen.queryByText('Inherited answer')).not.toBeInTheDocument();
    expect(screen.queryByText(/inherited|shared workspace/i)).not.toBeInTheDocument();
    expect(
      screen
        .getByRole('region', { name: 'Side chat messages' })
        .querySelectorAll('[data-turn-key]'),
    ).toHaveLength(0);
  });

  it('keeps drafts and the same native child while tabs close and the main chat advances', async () => {
    const view = render(<SideChat {...props()} />);
    await ready();
    view.rerender(<SideChat {...props(false)} />);
    const laterMain = { ...anchor, id: 'main-3', turn_no: 3, task_prompt: 'Later main question' };
    view.rerender(
      <SideChat
        {...props()}
        sourceJob={toDisplayJob(laterMain)}
        sourceJobs={[anchor, laterMain]}
      />,
    );
    expect(screen.getByRole('textbox')).toHaveValue('Why keep the files?');
    expect(screen.queryByText('Later main question')).not.toBeInTheDocument();
    expect(forkAgentJob).toHaveBeenCalledTimes(1);
    expect(JSON.parse(sessionStorage.getItem(sideChatStorageKey(userId, 'main'))!)).toMatchObject({
      anchorJobId: 'main-2',
      jobId: 'side-copy',
    });
  });

  it('submits only the new prompt to the child, streams its response, and leaves the main URL alone', async () => {
    const push = vi.spyOn(window.history, 'pushState');
    render(<SideChat {...props()} />);
    await ready();
    fireEvent.change(screen.getByRole('combobox', { name: 'Side chat model' }), {
      target: { value: 'model-b' },
    });
    fireEvent.click(screen.getByRole('button', { name: 'Send side chat message' }));
    await screen.findByRole('button', { name: 'Stop side chat' });
    expect(followUpAgentJob).toHaveBeenCalledExactlyOnceWith('side-copy', {
      prompt: 'Why keep the files?',
      model: 'model-b',
    });
    expect(push).not.toHaveBeenCalled();
    await waitFor(() => expect(streamAgentJob).toHaveBeenCalledWith('side-3', expect.anything()));
    const handler = vi.mocked(streamAgentJob).mock.calls.at(-1)![1];
    await act(async () => {
      handler.onEvent({
        id: 17,
        seq: 17,
        job_id: 'side-3',
        turn_no: 3,
        attempt_id: 12,
        event_type: 'message',
        payload: { text: 'Only this side reply.' },
        created_at: null,
      } as AgentThreadEventApi);
    });
    expect(await screen.findByText('Only this side reply.')).toBeVisible();
    expect(screen.queryByText('Inherited answer')).not.toBeInTheDocument();
    expect(screen.getByRole('textbox')).toHaveValue('');
  });

  it('stops only the child and preserves an unsent draft when its native context is lost', async () => {
    render(<SideChat {...props()} />);
    await send();
    fireEvent.change(screen.getByRole('textbox'), { target: { value: 'Unsent next question' } });
    fireEvent.click(screen.getByRole('button', { name: 'Stop side chat' }));
    await waitFor(() => expect(cancelAgentJob).toHaveBeenCalledExactlyOnceWith('side-3'));
    await screen.findByText('Unable to reconnect this side chat.');
    expect(screen.getByRole('textbox')).toHaveValue('Unsent next question');
    expect(screen.getByRole('textbox')).toBeDisabled();
  });

  it('reviews tool permissions for the child, not the main conversation', async () => {
    render(<SideChat {...props()} />);
    await send();
    fireEvent.click(screen.getByRole('button', { name: 'Review tools for side-3' }));
    expect(screen.getByLabelText('Side tool permissions')).toHaveTextContent('side-3:side');
    fireEvent.click(screen.getByRole('button', { name: 'Back to side chat' }));
    expect(screen.getByRole('textbox', { name: 'Message side chat' })).toBeVisible();
  });

  it('does not mark a reply read while the tab is hidden', async () => {
    const view = render(<SideChat {...props()} />);
    await send();
    view.rerender(<SideChat {...props(false)} />);
    jobs = jobs.map((job) =>
      job.id === next.id ? { ...job, state: 'succeeded', conversation_state: 'idle' } : job,
    );
    const handler = vi.mocked(streamAgentJob).mock.calls.at(-1)![1];
    await act(async () => {
      handler.onFinished?.({ state: 'succeeded', published_pr_url: null });
    });
    await waitFor(() =>
      expect(screen.queryByRole('button', { name: 'Stop side chat' })).not.toBeInTheDocument(),
    );
    const hiddenReceipt = JSON.parse(localStorage.getItem(conversationReadStorageKey(userId))!);
    expect(hiddenReceipt.receipts.side.jobId).not.toBe('side-3');
    view.rerender(<SideChat {...props()} />);
    await waitFor(() => {
      const receipt = JSON.parse(localStorage.getItem(conversationReadStorageKey(userId))!);
      expect(receipt.receipts.side.jobId).toBe('side-3');
    });
  });

  it('isolates drafts and associations across signed-in users', async () => {
    const view = render(<SideChat {...props()} />);
    await ready();
    view.rerender(<SideChat {...props(false)} userId="other-user" />);
    expect(screen.getByRole('textbox')).toHaveValue('');
    expect(forkAgentJob).toHaveBeenCalledTimes(1);
  });
});
