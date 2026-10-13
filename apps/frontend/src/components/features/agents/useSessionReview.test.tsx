// @vitest-environment jsdom
import { act, cleanup, fireEvent, render, screen } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import {
  decideMcpInvocation,
  listMcpInvocations,
  type McpInvocationApi,
  type McpInvocationsApi,
} from '@/lib/api/mcp';
import { listThreadQuestions, type AgentQuestionsApi } from '@/lib/api/questions';
import { resetAgentPush } from './agentPush';
import { McpApprovalsProvider, useMcpApprovals } from './mcp/McpApprovalsProvider';
import { QUESTION_POLL_MS } from './useNativeQuestions';
import { useSessionReview } from './useSessionReview';

const auth = vi.hoisted(() => ({ ownerId: 'owner-1' }));
vi.mock('@/components/providers', () => ({
  useAuth: () => ({
    state: {
      loading: false,
      isAuthenticated: true,
      user: {
        id: auth.ownerId,
        agent_access: { allowed: true, permissions: ['agent.use'] },
      },
    },
  }),
}));
vi.mock('@/lib/api/mcp', () => ({
  listMcpInvocations: vi.fn(),
  decideMcpInvocation: vi.fn(),
}));
vi.mock('@/lib/api/questions', () => ({
  listThreadQuestions: vi.fn(),
  answerThreadQuestion: vi.fn(),
}));

type ReviewJob = Parameters<typeof useSessionReview>[0];
const job: ReviewJob = {
  id: 'job-1',
  threadId: 'thread-1',
  state: 'running',
  currentAttemptId: 1,
};
const call: McpInvocationApi = {
  id: 11,
  job_id: 'job-1',
  attempt_id: 1,
  thread_id: 'thread-1',
  connection_id: 3,
  connection_label: 'Team wiki',
  capability_name: 'search',
  capability_kind: 'tool',
  capability_description: null,
  capability_annotations: {},
  status: 'pending',
  arguments_digest: 'digest',
  arguments: {},
  created_at: null,
  expires_at: null,
  decided_at: null,
  result_summary: null,
  guidance: '',
};
const list = vi.mocked(listMcpInvocations);
const questions = vi.mocked(listThreadQuestions);

function page(invocations: McpInvocationApi[], next: number | null = null): McpInvocationsApi {
  return { invocations, has_more: next !== null, next_before_id: next };
}
function questionSnapshot(overrides: Partial<AgentQuestionsApi> = {}): AgentQuestionsApi {
  return {
    job_id: 'job-1',
    attempt_id: 1,
    questions: [{ request_id: 'question-1', questions: [] }],
    ...overrides,
  };
}
function deferred<T>() {
  let resolve!: (value: T) => void;
  const promise = new Promise<T>((done) => {
    resolve = done;
  });
  return { promise, resolve };
}
function Review({ currentJob }: { currentJob: ReviewJob }) {
  return <span data-testid="review">{useSessionReview(currentJob) ?? 'none'}</span>;
}
function ApprovalControls() {
  const approvals = useMcpApprovals();
  return (
    <>
      <button onClick={approvals.refresh}>Refresh owner approvals</button>
      <button onClick={approvals.loadMore}>Load older approvals</button>
      <button
        onClick={() => {
          const target = approvals.invocations.find((invocation) => invocation.id === call.id);
          if (target) approvals.decide(target, true);
        }}
      >
        Approve session request
      </button>
    </>
  );
}
function App({
  currentJob = job,
  ownerId = auth.ownerId,
  enabled = true,
}: {
  currentJob?: ReviewJob;
  ownerId?: string;
  enabled?: boolean;
}) {
  return (
    <McpApprovalsProvider ownerId={ownerId} enabled={enabled}>
      <Review currentJob={currentJob} />
      <ApprovalControls />
    </McpApprovalsProvider>
  );
}
function expectReview(expected: 'question' | 'approval' | 'none') {
  expect(screen.getByTestId('review').textContent).toBe(expected);
}

beforeEach(() => {
  vi.useFakeTimers();
  auth.ownerId = 'owner-1';
  list.mockReset().mockResolvedValue(page([]));
  vi.mocked(decideMcpInvocation).mockReset();
  questions.mockReset().mockResolvedValue(questionSnapshot({ questions: [] }));
});
afterEach(() => {
  cleanup();
  resetAgentPush();
  vi.restoreAllMocks();
  vi.useRealTimers();
});

describe('session human review markers', () => {
  it('marks native questions and clears the marker when the request is answered elsewhere', async () => {
    questions
      .mockResolvedValueOnce(questionSnapshot())
      .mockResolvedValue(questionSnapshot({ questions: [] }));
    await act(async () => render(<App />));
    expectReview('question');
    await act(async () => vi.advanceTimersByTimeAsync(QUESTION_POLL_MS));
    expectReview('none');
  });

  it.each([{ job_id: 'old-job' }, { attempt_id: 0 }])(
    'ignores a native question from a different live generation: %j',
    async (generation) => {
      questions.mockResolvedValue(questionSnapshot(generation));
      await act(async () => render(<App />));
      expectReview('none');
    },
  );

  it('clears a question immediately when the attempt changes and ignores its delayed poll', async () => {
    const oldPoll = deferred<AgentQuestionsApi>();
    questions
      .mockResolvedValueOnce(questionSnapshot())
      .mockReturnValueOnce(oldPoll.promise)
      .mockResolvedValue(questionSnapshot({ attempt_id: 2, questions: [] }));
    let view!: ReturnType<typeof render>;
    await act(async () => {
      view = render(<App />);
    });
    expectReview('question');
    await act(async () => vi.advanceTimersByTimeAsync(QUESTION_POLL_MS));
    view.rerender(<App currentJob={{ ...job, currentAttemptId: 2 }} />);
    expectReview('none');
    await act(async () => oldPoll.resolve(questionSnapshot()));
    expectReview('none');
  });

  it.each([
    { state: 'done' as const },
    { state: 'queued' as const },
    { state: 'failed' as const },
    { state: 'cancelled' as const },
    { cancelRequested: true },
  ])('clears review when the live attempt stops accepting input: %j', async (transition) => {
    questions.mockResolvedValue(questionSnapshot());
    list.mockResolvedValue(page([call]));
    let view!: ReturnType<typeof render>;
    await act(async () => {
      view = render(<App />);
    });
    expectReview('question');
    view.rerender(<App currentJob={{ ...job, ...transition }} />);
    expectReview('none');
    questions.mockClear();
    await act(async () => vi.advanceTimersByTimeAsync(QUESTION_POLL_MS * 2));
    expect(questions).not.toHaveBeenCalled();
    expectReview('none');
  });

  it('marks approvals only for this job and thread, while prioritizing an open question', async () => {
    list.mockResolvedValue(page([call]));
    questions
      .mockResolvedValueOnce(questionSnapshot())
      .mockResolvedValue(questionSnapshot({ questions: [] }));
    await act(async () => render(<App />));
    expectReview('question');
    await act(async () => vi.advanceTimersByTimeAsync(QUESTION_POLL_MS));
    expectReview('approval');
  });

  it.each([
    { job_id: 'other-job' },
    { thread_id: 'other-thread' },
    { status: 'approved' as const },
    { expires_at: '2000-01-01T00:00:00Z' },
  ])('ignores an approval that cannot be decided for this session: %j', async (unavailable) => {
    list.mockResolvedValue(page([{ ...call, ...unavailable }]));
    await act(async () => render(<App />));
    expectReview('none');
  });

  it.each([
    ['owner queue', 1, 'approval'],
    ['owner queue', 0, 'none'],
    ['owner queue', null, 'none'],
    ['owner queue', undefined, 'none'],
    ['older-page lookup', 1, 'approval'],
    ['older-page lookup', 0, 'none'],
    ['older-page lookup', null, 'none'],
    ['older-page lookup', undefined, 'none'],
  ] as const)(
    'scopes an approval from the %s to attempt %s',
    async (source, attemptId, expected) => {
      const approval = { ...call, attempt_id: attemptId };
      if (source === 'owner queue') list.mockResolvedValue(page([approval]));
      else
        list.mockImplementation(async ({ jobId }) =>
          jobId
            ? page([approval])
            : page([{ ...call, id: 100, job_id: 'other-job', thread_id: 'other-thread' }], 100),
        );
      await act(async () => render(<App />));
      expectReview(expected);
    },
  );

  it.each([null, undefined])(
    'does not mark review without a current attempt: %s',
    async (attemptId) => {
      list.mockResolvedValue(page([call]));
      questions.mockResolvedValue(questionSnapshot());
      await act(async () => render(<App currentJob={{ ...job, currentAttemptId: attemptId }} />));
      expectReview('none');
      expect(questions).not.toHaveBeenCalled();
    },
  );

  it('removes an approval marker at its deadline even if a stale server read still lists it', async () => {
    list.mockResolvedValue(
      page([{ ...call, expires_at: new Date(Date.now() + 1000).toISOString() }]),
    );
    await act(async () => render(<App />));
    expectReview('approval');
    await act(async () => vi.advanceTimersByTimeAsync(1005));
    expectReview('none');
  });

  it('finds older approvals for this session beyond the loaded owner queue', async () => {
    list.mockImplementation(async ({ jobId }) =>
      jobId === job.id
        ? page([call])
        : page([{ ...call, id: 100, job_id: 'other-job', thread_id: 'other-thread' }], 100),
    );
    await act(async () => render(<App />));
    expectReview('approval');
    expect(list).toHaveBeenCalledWith({ jobId: job.id, pendingOnly: true, limit: 1 });
  });

  it('does not perform per-session approval reads when the owner queue is complete', async () => {
    await act(async () => render(<App />));
    expectReview('none');
    expect(list).toHaveBeenCalledOnce();
    expect(list).toHaveBeenCalledWith({ pendingOnly: true, limit: 50 });
  });

  it('does not revive a prior fallback when the owner queue becomes incomplete again', async () => {
    const freshLookup = deferred<McpInvocationsApi>();
    const head = page([{ ...call, id: 100, job_id: 'other-job', thread_id: 'other-thread' }], 100);
    let stage = 0;
    list.mockImplementation(async ({ jobId }) => {
      if (jobId) return stage === 2 ? freshLookup.promise : page([call]);
      return stage === 1 ? page([]) : head;
    });
    await act(async () => render(<App />));
    expectReview('approval');
    stage = 1;
    await act(async () =>
      fireEvent.click(screen.getByRole('button', { name: 'Refresh owner approvals' })),
    );
    expectReview('none');
    stage = 2;
    await act(async () =>
      fireEvent.click(screen.getByRole('button', { name: 'Refresh owner approvals' })),
    );
    expectReview('none');
    await act(async () => freshLookup.resolve(page([])));
    expectReview('none');
  });

  it('does not revive an approved request from a cached or delayed older-page lookup', async () => {
    const ownerRefresh = deferred<McpInvocationsApi>();
    const staleLookup = deferred<McpInvocationsApi>();
    const head = page([{ ...call, id: 100, job_id: 'other-job', thread_id: 'other-thread' }], 100);
    let holdReads = false;
    list.mockImplementation(async ({ jobId, beforeId }) => {
      if (jobId) return holdReads ? staleLookup.promise : page([call]);
      if (holdReads) return ownerRefresh.promise;
      return beforeId === 100 ? page([call], call.id) : head;
    });
    vi.mocked(decideMcpInvocation).mockResolvedValue({ ...call, status: 'approved' });
    await act(async () => render(<App />));
    expectReview('approval');
    await act(async () =>
      fireEvent.click(screen.getByRole('button', { name: 'Load older approvals' })),
    );
    expectReview('approval');
    holdReads = true;
    await act(async () =>
      fireEvent.click(screen.getByRole('button', { name: 'Approve session request' })),
    );
    expect(decideMcpInvocation).toHaveBeenCalledWith(call.id, true);
    expectReview('none');
    await act(async () => staleLookup.resolve(page([call])));
    expectReview('none');
    await act(async () => ownerRefresh.resolve(page([])));
    expectReview('none');
  });

  it('discards question and older-page approval reads after switching accounts', async () => {
    const oldQuestion = deferred<AgentQuestionsApi>();
    const oldApproval = deferred<McpInvocationsApi>();
    questions.mockReturnValueOnce(oldQuestion.promise);
    let jobReads = 0;
    list.mockImplementation(async ({ jobId }) => {
      if (!jobId)
        return page([{ ...call, id: 100, job_id: 'other-job', thread_id: 'other-thread' }], 100);
      jobReads += 1;
      return jobReads === 1 ? oldApproval.promise : page([]);
    });
    let view!: ReturnType<typeof render>;
    await act(async () => {
      view = render(<App />);
    });
    expect(jobReads).toBe(1);
    auth.ownerId = 'owner-2';
    await act(async () => view.rerender(<App ownerId="owner-2" />));
    expectReview('none');
    await act(async () => {
      oldQuestion.resolve(questionSnapshot());
      oldApproval.resolve(page([call]));
    });
    expectReview('none');
  });
});
