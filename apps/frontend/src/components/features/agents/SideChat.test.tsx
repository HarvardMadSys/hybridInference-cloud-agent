// @vitest-environment jsdom
import receiptContract from '../../../../../../tests/fixtures/side_chat_job_receipt.json';
import { installSideChatLocks } from './sideChatTestSupport';
import '@testing-library/jest-dom/vitest';
import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import {
  cancelAgentJob,
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
import {
  getThreadQueue,
  submitThreadMessage,
  type PendingMessageApi,
} from '@/lib/api/pendingMessages';
import { toDisplayJob } from './adapt';
import { SideChat } from './SideChat';
import { conversationReadStorageKey } from './useConversationReadState';
import { sideChatStorageKey } from './useSideChatSession';
import { answerThreadQuestion, listThreadQuestions } from '@/lib/api/questions';

vi.mock('@/lib/api/questions', () => ({
  listThreadQuestions: vi.fn(),
  answerThreadQuestion: vi.fn(),
}));

vi.mock('@/lib/api/agents', () => ({
  cancelAgentJob: vi.fn(),
  forkAgentJob: vi.fn(),
  getAgentJob: vi.fn(),
  getAgentJobGit: vi.fn(),
  getAgentJobThread: vi.fn(),
  listAllAgentThreadEvents: vi.fn(),
  streamAgentJob: vi.fn(),
}));
vi.mock('@/lib/api/pendingMessages', async (importOriginal) => ({
  ...(await importOriginal<typeof import('@/lib/api/pendingMessages')>()),
  submitThreadMessage: vi.fn(),
  getThreadQueue: vi.fn(),
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
} as AgentJobApi;
const copied: AgentJobApi = {
  ...anchor,
  ...receiptContract,
  id: 'side-copy',
  forked_from_job_id: 'main-2',
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
let waiting: PendingMessageApi[];
const userId = 'side-test-user';

beforeEach(() => {
  vi.resetAllMocks();
  vi.mocked(listThreadQuestions).mockResolvedValue({
    questions: [],
    job_id: 'side-3',
    attempt_id: 12,
  });
  sessionStorage.clear();
  localStorage.clear();
  installSideChatLocks();
  jobs = [copied];
  events = [];
  waiting = [];
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
  // A message to an idle side chat is its next turn at once; one sent while a
  // side turn runs waits in the queue, as the control plane answers.
  vi.mocked(submitThreadMessage).mockImplementation(async (threadId, body) => {
    const live = jobs.some(
      (job) => job.thread_id === threadId && (job.state === 'running' || job.state === 'queued'),
    );
    if (live) {
      const pending: PendingMessageApi = {
        id: `apmsg-${waiting.length + 1}`,
        thread_id: threadId,
        position: waiting.length + 1,
        state: 'queued',
        revision: 1,
        prompt: body.prompt,
      };
      waiting.push(pending);
      return { pending, job: null };
    }
    const created = { ...next, task_prompt: body.prompt, model: body.model ?? next.model };
    jobs.push(created);
    return { pending: null, job: created };
  });
  vi.mocked(getThreadQueue).mockImplementation(async (threadId) => ({
    thread_id: threadId,
    pending_messages: waiting.filter((row) => row.thread_id === threadId),
    queue_paused_reason: null,
    latest_job_id: jobs.at(-1)?.id ?? null,
    latest_turn_no: jobs.at(-1)?.turn_no ?? null,
    latest_job_state: jobs.at(-1)?.state ?? null,
  }));
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
  it('opens and answers the child agent question without sending a chat message or stopping the child', async () => {
    vi.mocked(listThreadQuestions).mockResolvedValue({
      job_id: next.id,
      attempt_id: next.current_attempt_id!,
      questions: [
        {
          request_id: 'side-question',
          questions: [
            {
              id: 'files',
              header: 'Files',
              question: 'Keep the files?',
              multiple: false,
              allow_custom: false,
              options: [{ label: 'Keep files', description: '' }],
            },
          ],
        },
      ],
    });
    vi.mocked(answerThreadQuestion).mockImplementation(async () => {
      vi.mocked(listThreadQuestions).mockResolvedValue({
        job_id: next.id,
        attempt_id: 12,
        questions: [],
      });
      return { accepted: true, reason: null };
    });
    render(<SideChat {...props()} />);
    await send();
    expect(
      await screen.findByRole('dialog', { name: 'The agent has a question' }),
    ).toBeInTheDocument();
    fireEvent.keyDown(screen.getByRole('button', { name: 'Answer later' }), { key: 'Escape' });
    expect(cancelAgentJob).not.toHaveBeenCalled();
    fireEvent.click(screen.getByRole('button', { name: 'Review question' }));
    fireEvent.click(screen.getByRole('radio', { name: 'Keep files' }));
    const visibility = vi.spyOn(document, 'visibilityState', 'get');
    visibility.mockReturnValue('hidden');
    await act(async () => {
      document.dispatchEvent(new Event('visibilitychange'));
    });
    expect(screen.getByRole('radio', { name: 'Keep files' })).toBeChecked();
    visibility.mockReturnValue('visible');
    await act(async () => {
      document.dispatchEvent(new Event('visibilitychange'));
    });
    expect(screen.getByRole('radio', { name: 'Keep files' })).toBeChecked();
    fireEvent.click(screen.getByRole('button', { name: 'Send answer' }));
    await waitFor(() =>
      expect(answerThreadQuestion).toHaveBeenCalledWith('side', {
        job_id: next.id,
        attempt_id: 12,
        request_id: 'side-question',
        answers: { files: ['Keep files'] },
      }),
    );
    expect(submitThreadMessage).toHaveBeenCalledTimes(1);
  });

  it('stops the child with Escape without interrupting a focused draft', async () => {
    render(<SideChat {...props()} />);
    await send();
    fireEvent.keyDown(screen.getByRole('textbox'), { key: 'Escape' });
    expect(cancelAgentJob).not.toHaveBeenCalled();
    fireEvent.keyDown(document.body, { key: 'Escape' });
    await waitFor(() => expect(cancelAgentJob).toHaveBeenCalledExactlyOnceWith('side-3'));
  });

  it('keeps a draft and explains an Enter submission while preparing', async () => {
    vi.mocked(forkAgentJob).mockReturnValueOnce(new Promise(() => {}));
    render(<SideChat {...props()} />);
    fireEvent.change(screen.getByRole('textbox'), { target: { value: 'Still preparing' } });
    fireEvent.keyDown(screen.getByRole('textbox'), { key: 'Enter' });
    expect(await screen.findByRole('alert')).toHaveTextContent('Preparing side chat…');
    expect(screen.getByRole('textbox')).toHaveValue('Still preparing');
    expect(submitThreadMessage).not.toHaveBeenCalled();
  });

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
    expect(JSON.parse(localStorage.getItem(sideChatStorageKey(userId, 'main'))!)).toMatchObject({
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
    // Addressed to the side chat's own conversation, never to the main one.
    expect(submitThreadMessage).toHaveBeenCalledExactlyOnceWith('side', {
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
    expect(handler.signal?.aborted).toBe(true);
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

describe('Side Chat review regressions', () => {
  it('preserves model and effort choices made while the native fork is preparing', async () => {
    let resolve!: (row: AgentJobApi) => void;
    vi.mocked(forkAgentJob).mockReturnValueOnce(
      new Promise((r) => {
        resolve = r;
      }),
    );
    render(
      <SideChat
        {...props()}
        config={{
          ...config,
          runtime_reasoning_efforts: { codex: ['low', 'medium', 'high'] },
          model_reasoning_efforts: { 'model-a': ['high'], 'model-b': ['high'] },
        }}
      />,
    );
    await waitFor(() => expect(forkAgentJob).toHaveBeenCalledTimes(1));
    fireEvent.change(screen.getByRole('combobox', { name: 'Side chat model' }), {
      target: { value: 'model-b' },
    });
    fireEvent.change(screen.getByRole('combobox', { name: 'Side chat reasoning' }), {
      target: { value: 'low' },
    });
    await act(async () => {
      resolve(copied);
    });
    await ready();
    fireEvent.click(screen.getByRole('button', { name: 'Send side chat message' }));
    await waitFor(() =>
      expect(submitThreadMessage).toHaveBeenCalledWith(copied.thread_id, {
        prompt: 'Why keep the files?',
        model: 'model-b',
        reasoning_effort: 'low',
      }),
    );
  });
  it('queues an Enter submission behind the active side turn', async () => {
    render(<SideChat {...props()} />);
    await send();
    fireEvent.change(screen.getByRole('textbox', { name: 'Message side chat' }), {
      target: { value: 'Next side question' },
    });
    fireEvent.keyDown(screen.getByRole('textbox', { name: 'Message side chat' }), {
      key: 'Enter',
    });
    await waitFor(() =>
      expect(submitThreadMessage).toHaveBeenLastCalledWith('side', {
        prompt: 'Next side question',
      }),
    );
    expect(screen.getByText('Queued after this run')).toBeVisible();
    // It waits above the side chat's composer, editable, while the turn runs.
    const queue = await screen.findByRole('region', { name: 'Queued messages' });
    expect(queue).toHaveTextContent('Next side question');
    expect(screen.getByRole('textbox', { name: 'Message side chat' })).toHaveValue('');
  });
  it('adopts a newer side turn found in the thread snapshot', async () => {
    jobs.push({ ...next, id: 'side-external', task_prompt: 'From another window' });
    render(<SideChat {...props()} />);
    expect(await screen.findByText('From another window')).toBeVisible();
    expect(screen.getByRole('region', { name: 'Turn 1' })).toBeVisible();
    expect(screen.queryByRole('region', { name: 'Turn 3' })).not.toBeInTheDocument();
    expect(submitThreadMessage).not.toHaveBeenCalled();
  });
  it('reports invalid external turns without sending to them', async () => {
    jobs.push({ ...next, sandbox_id: 'foreign', id: 'invalid-external' });
    render(<SideChat {...props()} />);
    expect(await screen.findByRole('alert')).toHaveTextContent('does not belong');
    expect(submitThreadMessage).not.toHaveBeenCalled();
  });
  it('closes tool review when a new job arrives instead of silently repointing it', async () => {
    const { announceAgentSessionsChanged } = await import('@/lib/api/agentSessionEvents');
    render(<SideChat {...props()} />);
    await send();
    fireEvent.click(screen.getByRole('button', { name: 'Review tools for side-3' }));
    jobs.push({ ...next, id: 'side-4', turn_no: 4, task_prompt: 'External follow-up' });
    act(() => announceAgentSessionsChanged({ threadIds: ['side'] }));
    expect(await screen.findByText('External follow-up')).toBeVisible();
    expect(screen.queryByLabelText('Side tool permissions')).not.toBeInTheDocument();
  });
  it('shows only the latest attempt after the active attempt id is released', async () => {
    jobs.push({
      ...next,
      current_attempt_id: null,
      state: 'cancelled',
      conversation_state: 'idle',
    });
    events = [11, 12].map((id) => ({
      id,
      seq: id,
      attempt_id: id,
      job_id: next.id,
      turn_no: 3,
      event_type: 'message',
      payload: { text: id === 11 ? 'Abandoned attempt' : 'Latest attempt' },
      created_at: null,
    }));
    render(<SideChat {...props()} />);
    expect(await screen.findByText('Latest attempt')).toBeVisible();
    expect(screen.queryByText('Abandoned attempt')).not.toBeInTheDocument();
  });
  it('suppresses successful publication bookkeeping and empty execution footers', async () => {
    jobs.push({
      ...next,
      state: 'succeeded',
      conversation_state: 'idle',
      detail: 'agent completed (shared workspace; publication snapshot pending)',
    });
    render(<SideChat {...props()} />);
    await screen.findByText(next.task_prompt);
    expect(screen.queryByText(/publication snapshot pending/)).not.toBeInTheDocument();
    expect(screen.getByRole('region', { name: 'Turn 1' }).querySelector('.mb-6.mt-3')).toBeNull();
  });
  it('does not parse read receipts or redescribe the job on composer keystrokes', async () => {
    render(<SideChat {...props()} />);
    await ready();
    const spy = vi.spyOn(Storage.prototype, 'getItem');
    for (const value of ['a', 'ab', 'abc'])
      fireEvent.change(screen.getByRole('textbox'), { target: { value } });
    expect(
      spy.mock.calls.filter(([key]) => key === conversationReadStorageKey(userId)),
    ).toHaveLength(0);
  });
});
