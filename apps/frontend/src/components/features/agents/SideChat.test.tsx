// @vitest-environment jsdom
import receiptContract from '../../../../../../tests/fixtures/side_chat_job_receipt.json';
import { installSideChatLocks } from './sideChatTestSupport';
import '@testing-library/jest-dom/vitest';
import {
  act,
  cleanup,
  createEvent,
  fireEvent,
  render,
  screen,
  waitFor,
} from '@testing-library/react';
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
import { codeReviewFixPrompt } from './codeReview';
import { sideChatKeys } from './sideChatStorage';
import { SideChat } from './SideChat';
import { conversationReadStorageKey } from './useConversationReadState';
import { sideChatStorageKey } from './useSideChatSession';
import { answerThreadQuestion, listThreadQuestions } from '@/lib/api/questions';
import type { SideChatTab } from './useWorkspaceTabs';
import { deleteAttachment, uploadAttachment, type AttachmentApi } from '@/lib/api/attachments';

vi.mock('@/lib/api/attachments', () => ({
  uploadAttachment: vi.fn(),
  deleteAttachment: vi.fn(),
}));

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
  vi.mocked(uploadAttachment).mockImplementation(async (file, path) => ({
    id: 42,
    path: path ?? file.name,
    size_bytes: file.size,
    content_type: file.type,
  }));
  vi.mocked(deleteAttachment).mockResolvedValue(undefined);
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

function pasteImage() {
  const image = new File(['image bytes'], 'image.png', { type: 'image/png' });
  const event = createEvent.paste(screen.getByRole('textbox', { name: 'Message side chat' }), {
    clipboardData: {
      items: [{ kind: 'file', type: image.type, getAsFile: () => image }],
      files: [image],
    },
  });
  fireEvent(screen.getByRole('textbox', { name: 'Message side chat' }), event);
  return event;
}

describe('Side Chat', () => {
  it('waits for a pasted image upload before sending its attachment ID and clears it after success', async () => {
    let resolveUpload!: (attachment: AttachmentApi) => void;
    vi.mocked(uploadAttachment).mockReturnValueOnce(
      new Promise((resolve) => {
        resolveUpload = resolve;
      }),
    );
    render(<SideChat {...props()} />);
    await ready();
    const paste = pasteImage();
    expect(paste.defaultPrevented).toBe(true);
    expect(uploadAttachment).toHaveBeenCalledTimes(1);
    const [file, path] = vi.mocked(uploadAttachment).mock.calls[0]!;
    expect(file.name).toMatch(/^pasted-image-.+\.png$/);
    expect(path).toBe(file.name);
    expect(screen.getByText(file.name)).toBeVisible();
    expect(screen.getByText('uploading…')).toBeVisible();
    expect(screen.getByRole('button', { name: 'Send side chat message' })).toBeDisabled();
    fireEvent.keyDown(screen.getByRole('textbox', { name: 'Message side chat' }), {
      key: 'Enter',
    });
    expect(submitThreadMessage).not.toHaveBeenCalled();
    await act(async () => {
      resolveUpload({ id: 42, path: file.name, size_bytes: file.size, content_type: file.type });
    });
    expect(screen.getByRole('button', { name: 'Send side chat message' })).toBeEnabled();
    fireEvent.click(screen.getByRole('button', { name: 'Send side chat message' }));
    await waitFor(() =>
      expect(submitThreadMessage).toHaveBeenCalledExactlyOnceWith('side', {
        prompt: 'Why keep the files?',
        attachment_ids: [42],
      }),
    );
    await waitFor(() => expect(screen.queryByText(file.name)).not.toBeInTheDocument());
    expect(screen.getByRole('textbox', { name: 'Message side chat' })).toHaveValue('');
  });

  it('keeps a pasted image and draft when sending fails so the same attachment can be retried', async () => {
    vi.mocked(submitThreadMessage).mockRejectedValueOnce(new Error('Try sending again'));
    render(<SideChat {...props()} />);
    await ready();
    pasteImage();
    const [file] = vi.mocked(uploadAttachment).mock.calls[0]!;
    await waitFor(() =>
      expect(screen.getByRole('button', { name: 'Send side chat message' })).toBeEnabled(),
    );
    fireEvent.click(screen.getByRole('button', { name: 'Send side chat message' }));
    expect(await screen.findByRole('alert')).toHaveTextContent('Try sending again');
    expect(screen.getByRole('textbox', { name: 'Message side chat' })).toHaveValue(
      'Why keep the files?',
    );
    expect(screen.getByText(file.name)).toBeVisible();
    fireEvent.click(screen.getByRole('button', { name: 'Send side chat message' }));
    await waitFor(() => expect(submitThreadMessage).toHaveBeenCalledTimes(2));
    expect(submitThreadMessage).toHaveBeenLastCalledWith('side', {
      prompt: 'Why keep the files?',
      attachment_ids: [42],
    });
    expect(uploadAttachment).toHaveBeenCalledTimes(1);
    await waitFor(() => expect(screen.queryByText(file.name)).not.toBeInTheDocument());
  });

  it('removes a pasted image before sending and preserves ordinary text paste', async () => {
    render(<SideChat {...props()} />);
    await ready();
    const textPaste = createEvent.paste(
      screen.getByRole('textbox', { name: 'Message side chat' }),
      {
        clipboardData: {
          items: [{ kind: 'string', type: 'text/plain', getAsFile: () => null }],
          files: [],
        },
      },
    );
    fireEvent(screen.getByRole('textbox', { name: 'Message side chat' }), textPaste);
    expect(textPaste.defaultPrevented).toBe(false);
    expect(uploadAttachment).not.toHaveBeenCalled();
    pasteImage();
    const [file] = vi.mocked(uploadAttachment).mock.calls[0]!;
    await waitFor(() =>
      expect(screen.getByRole('button', { name: 'Send side chat message' })).toBeEnabled(),
    );
    fireEvent.click(screen.getByRole('button', { name: `Remove ${file.name}` }));
    await waitFor(() => expect(deleteAttachment).toHaveBeenCalledExactlyOnceWith(42));
    fireEvent.click(screen.getByRole('button', { name: 'Send side chat message' }));
    await waitFor(() =>
      expect(submitThreadMessage).toHaveBeenCalledExactlyOnceWith('side', {
        prompt: 'Why keep the files?',
      }),
    );
  });

  it('clears staged images together with side chat drafts', async () => {
    render(<SideChat {...props()} />);
    await ready();
    pasteImage();
    const [file] = vi.mocked(uploadAttachment).mock.calls[0]!;
    expect(screen.getByText(file.name)).toBeVisible();
    act(() => {
      window.dispatchEvent(new CustomEvent('agents:side-chat-drafts-cleared', { detail: userId }));
    });
    expect(screen.getByRole('textbox', { name: 'Message side chat' })).toHaveValue('');
    expect(screen.queryByText(file.name)).not.toBeInTheDocument();
  });

  it('does not stage another image while the message is being submitted', async () => {
    let releaseSubmission!: () => void;
    const submit = vi.mocked(submitThreadMessage).getMockImplementation()!;
    vi.mocked(submitThreadMessage).mockImplementationOnce(async (...args) => {
      await new Promise<void>((resolve) => {
        releaseSubmission = resolve;
      });
      return submit(...args);
    });
    render(<SideChat {...props()} />);
    await ready();
    fireEvent.click(screen.getByRole('button', { name: 'Send side chat message' }));
    const paste = pasteImage();
    expect(paste.defaultPrevented).toBe(false);
    expect(uploadAttachment).not.toHaveBeenCalled();
    await act(async () => releaseSubmission());
    await screen.findByRole('button', { name: 'Stop side chat' });
  });

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
    pasteImage();
    expect(uploadAttachment).not.toHaveBeenCalled();
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
    fireEvent.click(screen.getByRole('button', { name: 'Side chat model' }));
    fireEvent.click(screen.getByRole('option', { name: 'model-b' }));
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

  it('updates live agent activity as its child starts and completes a tool call', async () => {
    vi.mocked(streamAgentJob).mockReturnValue(new Promise(() => {}));
    const started: AgentThreadEventApi = {
      id: 18,
      seq: 18,
      job_id: next.id,
      turn_no: next.turn_no!,
      attempt_id: 12,
      event_type: 'lifecycle',
      payload: { phase: 'init' },
      created_at: null,
    };
    events = [started];
    render(<SideChat {...props()} />);
    await send();
    expect(await screen.findByRole('status', { name: 'Agent status' })).toHaveTextContent(
      'Waiting for LLM response',
    );
    await waitFor(() => expect(streamAgentJob).toHaveBeenCalledWith(next.id, expect.anything()));
    const handler = vi.mocked(streamAgentJob).mock.calls.at(-1)![1];
    const toolCall: AgentThreadEventApi = {
      ...started,
      id: 19,
      seq: 19,
      event_type: 'tool_use',
      payload: { id: 'tool', name: 'Bash', input: { command: 'pwd' } },
    };
    await act(async () => {
      events.push(toolCall);
      handler.onEvent(toolCall);
    });
    expect(screen.getByRole('status', { name: 'Agent status' })).toHaveTextContent(
      'Running tool call',
    );

    const toolResult: AgentThreadEventApi = {
      ...started,
      id: 20,
      seq: 20,
      event_type: 'tool_result',
      payload: { tool_use_id: 'tool', content: 'done' },
    };
    await act(async () => {
      events.push(toolResult);
      handler.onEvent(toolResult);
    });
    expect(screen.getByRole('status', { name: 'Agent status' })).toHaveTextContent(
      'Waiting for LLM response',
    );

    await act(async () => {
      jobs = jobs.map((job) =>
        job.id === next.id ? { ...job, state: 'succeeded', conversation_state: 'idle' } : job,
      );
      handler.onFinished?.({ state: 'succeeded', published_pr_url: null });
    });
    await waitFor(() =>
      expect(screen.queryByRole('status', { name: 'Agent status' })).not.toBeInTheDocument(),
    );
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
    fireEvent.click(screen.getByRole('button', { name: 'Side chat model' }));
    fireEvent.click(screen.getByRole('option', { name: 'model-b' }));
    fireEvent.click(screen.getByRole('button', { name: 'Side chat reasoning' }));
    fireEvent.click(screen.getByRole('option', { name: 'low' }));
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

describe('fixing completed side chat reviews', () => {
  const reviewTab: SideChatTab = {
    id: 'side-chat-review',
    number: 1,
    anchor: { jobId: anchor.id, sandboxId: 'shared' },
    reviewPrompt: 'Review these changes without editing files.',
  };
  const reviewJob: AgentJobApi = {
    ...next,
    task_prompt: reviewTab.reviewPrompt!,
    state: 'succeeded',
    conversation_state: 'idle',
    current_attempt_id: null,
  };
  const fixJob: AgentJobApi = {
    ...next,
    id: 'side-fix',
    turn_no: 4,
    task_prompt: codeReviewFixPrompt,
  };

  function restoreReview(job = reviewJob, tab = reviewTab) {
    jobs = [...[copied].filter((row) => row.id !== job.id), job];
    localStorage.setItem(
      sideChatKeys(userId, 'main', tab.id).session,
      JSON.stringify({
        anchorJobId: anchor.id,
        sandboxId: 'shared',
        operation: { id: 'review-operation', state: 'unknown' },
        jobId: job.id,
        threadId: 'side',
        reviewSent: true,
      }),
    );
  }

  it('offers fixes after the live review succeeds and starts one fix turn in the same child', async () => {
    vi.mocked(streamAgentJob).mockReturnValue(new Promise(() => {}));
    render(<SideChat {...props()} tab={reviewTab} />);
    await screen.findByRole('button', { name: 'Stop side chat' });
    expect(screen.queryByRole('button', { name: 'Fix issues' })).not.toBeInTheDocument();
    await waitFor(() => expect(streamAgentJob).toHaveBeenCalledWith(next.id, expect.anything()));
    const handler = vi.mocked(streamAgentJob).mock.calls.at(-1)![1];
    await act(async () => {
      jobs = jobs.map((job) => {
        if (job.id !== next.id) return job;
        return {
          ...job,
          state: 'succeeded',
          conversation_state: 'idle',
          published_pr_url: 'https://github.com/owner/project/pull/1',
        };
      });
      handler.onFinished?.({ state: 'succeeded', published_pr_url: null });
    });
    const fixButton = await screen.findByRole('button', { name: 'Fix issues' });
    await waitFor(() => expect(fixButton).toBeEnabled());
    fireEvent.change(screen.getByRole('textbox', { name: 'Message side chat' }), {
      target: { value: 'Unsent follow-up question' },
    });
    pasteImage();
    const [file] = vi.mocked(uploadAttachment).mock.calls[0]!;
    await waitFor(() =>
      expect(screen.getByRole('button', { name: 'Send side chat message' })).toBeEnabled(),
    );
    fireEvent.click(screen.getByRole('button', { name: 'Side chat model' }));
    fireEvent.click(screen.getByRole('option', { name: 'model-b' }));
    let finishFix!: (result: { pending: null; job: AgentJobApi }) => void;
    vi.mocked(submitThreadMessage).mockReturnValueOnce(
      new Promise((resolve) => (finishFix = resolve)),
    );
    fireEvent.click(fixButton);
    const starting = await screen.findByRole('button', { name: 'Starting fixes…' });
    expect(starting).toBeDisabled();
    fireEvent.click(starting);
    expect(submitThreadMessage).toHaveBeenCalledTimes(2);
    expect(submitThreadMessage).toHaveBeenLastCalledWith(
      'side',
      { prompt: codeReviewFixPrompt, skill_ids: [] },
      { requestId: `review-fix-${next.id}` },
    );
    await act(async () => {
      jobs.push(fixJob);
      finishFix({ pending: null, job: fixJob });
    });
    await waitFor(() => expect(streamAgentJob).toHaveBeenCalledWith(fixJob.id, expect.anything()));
    expect(screen.queryByRole('button', { name: 'Fix issues' })).not.toBeInTheDocument();
    expect(forkAgentJob).toHaveBeenCalledTimes(1);
    expect(vi.mocked(submitThreadMessage).mock.calls.map(([threadId]) => threadId)).toEqual([
      'side',
      'side',
    ]);
    expect(screen.getByRole('textbox', { name: 'Message side chat' })).toHaveValue(
      'Unsent follow-up question',
    );
    expect(screen.getByText(file.name)).toBeVisible();
    expect(screen.getByRole('button', { name: 'Side chat model' })).toHaveValue('model-b');
  });

  it('restores a completed review after refresh without creating a child or resending the review', async () => {
    restoreReview();
    const first = render(<SideChat {...props()} tab={reviewTab} />);
    await waitFor(() => expect(screen.getByRole('button', { name: 'Fix issues' })).toBeEnabled());
    first.unmount();
    render(<SideChat {...props()} tab={reviewTab} />);
    await waitFor(() => expect(screen.getByRole('button', { name: 'Fix issues' })).toBeEnabled());
    expect(toDisplayJob(reviewJob).state).toBe('needs_review');
    expect(forkAgentJob).not.toHaveBeenCalled();
    expect(submitThreadMessage).not.toHaveBeenCalled();
  });

  it('keeps a failed fix actionable and retries with the same request id while preserving the draft', async () => {
    restoreReview();
    vi.mocked(submitThreadMessage).mockRejectedValueOnce(new Error('Fix response lost'));
    render(<SideChat {...props()} tab={reviewTab} />);
    await waitFor(() => expect(screen.getByRole('button', { name: 'Fix issues' })).toBeEnabled());
    fireEvent.change(screen.getByRole('textbox', { name: 'Message side chat' }), {
      target: { value: 'Draft remains mine' },
    });
    fireEvent.click(screen.getByRole('button', { name: 'Fix issues' }));
    expect(await screen.findByRole('alert')).toHaveTextContent('Fix response lost');
    expect(screen.getByRole('textbox', { name: 'Message side chat' })).toHaveValue(
      'Draft remains mine',
    );
    await waitFor(() => expect(screen.getByRole('button', { name: 'Fix issues' })).toBeEnabled());
    vi.mocked(submitThreadMessage).mockImplementationOnce(async () => {
      jobs.push(fixJob);
      return { pending: null, job: fixJob };
    });
    fireEvent.click(screen.getByRole('button', { name: 'Fix issues' }));
    await screen.findByRole('button', { name: 'Stop side chat' });
    expect(submitThreadMessage).toHaveBeenCalledTimes(2);
    expect(vi.mocked(submitThreadMessage).mock.calls[1]).toEqual(
      vi.mocked(submitThreadMessage).mock.calls[0],
    );
    expect(submitThreadMessage).toHaveBeenLastCalledWith(
      'side',
      { prompt: codeReviewFixPrompt, skill_ids: [] },
      { requestId: `review-fix-${reviewJob.id}` },
    );
    expect(screen.queryByRole('alert')).not.toBeInTheDocument();
    expect(screen.getByRole('textbox', { name: 'Message side chat' })).toHaveValue(
      'Draft remains mine',
    );
    expect(forkAgentJob).not.toHaveBeenCalled();
  });

  it('shows a queued fix and prevents another fix submission, including after refresh', async () => {
    restoreReview();
    vi.mocked(submitThreadMessage).mockImplementationOnce(async (threadId, body) => {
      const pending: PendingMessageApi = {
        id: 'queued-review-fix',
        thread_id: threadId,
        position: 1,
        state: 'queued',
        revision: 1,
        prompt: body.prompt,
      };
      waiting.push(pending);
      return { pending, job: null };
    });
    const first = render(<SideChat {...props()} tab={reviewTab} />);
    await waitFor(() => expect(screen.getByRole('button', { name: 'Fix issues' })).toBeEnabled());
    fireEvent.click(screen.getByRole('button', { name: 'Fix issues' }));
    expect(await screen.findByRole('region', { name: 'Queued messages' })).toHaveTextContent(
      'Fix the actionable issues',
    );
    expect(screen.getByRole('button', { name: 'Fix issues' })).toBeDisabled();
    first.unmount();
    render(<SideChat {...props()} tab={reviewTab} />);
    await screen.findByRole('region', { name: 'Queued messages' });
    expect(screen.getByRole('button', { name: 'Fix issues' })).toBeDisabled();
    fireEvent.click(screen.getByRole('button', { name: 'Fix issues' }));
    expect(submitThreadMessage).toHaveBeenCalledTimes(1);
    expect(forkAgentJob).not.toHaveBeenCalled();
  });

  it.each([
    ['lost context', { native_state: 'lost' }],
    ['archived', { archived_at: '2026-10-10T12:00:00Z' }],
    ['a conversation still running', { conversation_state: 'running' }],
  ])('disables fixing a completed review with %s', async (_name, overrides) => {
    restoreReview({ ...reviewJob, ...overrides } as AgentJobApi);
    render(<SideChat {...props()} tab={reviewTab} />);
    const button = await screen.findByRole('button', { name: 'Fix issues' });
    expect(button).toBeDisabled();
    fireEvent.click(button);
    expect(submitThreadMessage).not.toHaveBeenCalled();
  });

  it.each([
    ['copied history', { ...copied, task_prompt: reviewTab.reviewPrompt! }, reviewTab],
    ['an ordinary side chat', reviewJob, { ...reviewTab, reviewPrompt: undefined }],
    ['another side question', { ...reviewJob, task_prompt: 'Explain another change' }, reviewTab],
    ['a failed review', { ...reviewJob, state: 'failed', conversation_state: 'failed' }, reviewTab],
    ['a cancelled review', { ...reviewJob, state: 'cancelled' }, reviewTab],
  ])('does not offer fixes for %s', async (_name, job, tab) => {
    restoreReview(job as AgentJobApi, tab as SideChatTab);
    render(<SideChat {...props()} tab={tab as SideChatTab} />);
    fireEvent.change(screen.getByRole('textbox', { name: 'Message side chat' }), {
      target: { value: 'Unsent draft' },
    });
    await waitFor(() =>
      expect(screen.getByRole('button', { name: 'Send side chat message' })).toBeEnabled(),
    );
    expect(screen.queryByRole('button', { name: 'Fix issues' })).not.toBeInTheDocument();
    expect(submitThreadMessage).not.toHaveBeenCalled();
  });
});
