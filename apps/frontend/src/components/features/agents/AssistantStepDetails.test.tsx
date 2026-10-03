// @vitest-environment jsdom
import '@testing-library/jest-dom/vitest';

import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { afterEach, beforeEach, expect, it, vi } from 'vitest';
import { getAgentMessageRequest, type AgentMessageRequestApi } from '@/lib/api/agents';
import { AssistantStepDetails } from './AssistantStepDetails';
import { buildTrajectorySteps, type StoredEvent } from './trajectorySteps';

vi.mock('@/lib/api/agents', () => ({ getAgentMessageRequest: vi.fn() }));
afterEach(cleanup);

const MEASURED: AgentMessageRequestApi = {
  status: 'available',
  reason: null,
  request: {
    request_id: 'req_1',
    response_id: 'msg_1',
    request_started_at: '2026-09-27T10:00:00Z',
    tokens_in: 100,
    tokens_out: 20,
    reasoning: null,
    spent_usd: '0.001',
    ttft_ms: 1000,
    latency_ms: 3000,
    usage_estimated: false,
  },
};

beforeEach(() => vi.mocked(getAgentMessageRequest).mockReset());

function view(messageId = 'msg_1') {
  const events = [1, 2].map((id) => ({
    id,
    seq: id,
    attempt_id: 7,
    job_id: 'job_1',
    turn_no: 1,
    event_type: 'message',
    // The 23 ms event-ingest interval must never become model latency.
    created_at: id === 1 ? '2026-09-27T10:00:03.000Z' : '2026-09-27T10:00:03.023Z',
    payload: { text: 'hello', usage_id: messageId, usage: { output_tokens: 0 } },
  })) as StoredEvent[];
  return (
    <AssistantStepDetails
      step={buildTrajectorySteps(events)[0]}
      tab="summary"
      tools={[]}
      onSelectTab={vi.fn()}
    />
  );
}

it('replaces provisional usage and ingest time with the matched gateway measurements', async () => {
  vi.mocked(getAgentMessageRequest).mockResolvedValue(MEASURED);
  render(view());
  await screen.findByText('req_1');
  expect(screen.getByText('Output tokens').nextElementSibling).toHaveTextContent('20');
  expect(screen.getByText('Total duration').nextElementSibling).toHaveTextContent('3s');
  expect(screen.getByText('TTFT').nextElementSibling).toHaveTextContent('1s');
  expect(screen.getByText('Generation').nextElementSibling).toHaveTextContent('2s');
  expect(screen.getByText('Throughput').nextElementSibling).toHaveTextContent('10 tok/s');
  expect(screen.queryByText('23ms')).not.toBeInTheDocument();
  expect(screen.queryByText('Reasoning tokens')).not.toBeInTheDocument();
  expect(screen.queryByText('Content tokens')).not.toBeInTheDocument();
  expect(getAgentMessageRequest).toHaveBeenCalledWith('job_1', 7, 'msg_1', expect.any(AbortSignal));
});

it('keeps non-streaming timing and real zero usage without inventing a TTFT or rate', async () => {
  vi.mocked(getAgentMessageRequest).mockResolvedValue({
    ...MEASURED,
    request: { ...MEASURED.request!, tokens_out: 0, ttft_ms: null },
  });
  render(view());
  await screen.findByText('req_1');
  expect(screen.getByText('Output tokens').nextElementSibling).toHaveTextContent('0');
  expect(screen.queryByText('TTFT')).not.toBeInTheDocument();
  expect(screen.queryByText('Generation')).not.toBeInTheDocument();
  expect(screen.queryByText('Throughput')).not.toBeInTheDocument();
});

it('lets a reader retry late logs without displaying permanently empty timing rows', async () => {
  vi.mocked(getAgentMessageRequest)
    .mockResolvedValueOnce({ status: 'not_recorded', request: null, reason: null })
    .mockResolvedValue(MEASURED);
  render(view());
  fireEvent.click(await screen.findByRole('button', { name: 'Retry request statistics' }));
  await screen.findByText('req_1');
  expect(getAgentMessageRequest).toHaveBeenCalledTimes(2);
});

it('refuses to show statistics when the gateway cannot establish one matching request', async () => {
  vi.mocked(getAgentMessageRequest).mockResolvedValue({
    status: 'ambiguous',
    request: null,
    reason: null,
  });
  render(view());
  await screen.findByText(/Multiple requests share/);
  expect(screen.queryByText('Request timing')).not.toBeInTheDocument();
  expect(screen.getByText('Usage source').nextElementSibling).toHaveTextContent('Runtime report');
});

it('does not put an old selection response on the newly selected message', async () => {
  let finishFirst!: (value: AgentMessageRequestApi) => void;
  vi.mocked(getAgentMessageRequest).mockImplementationOnce(
    () =>
      new Promise((resolve) => {
        finishFirst = resolve;
      }),
  );
  vi.mocked(getAgentMessageRequest).mockResolvedValueOnce({
    ...MEASURED,
    request: { ...MEASURED.request!, request_id: 'req_2', response_id: 'msg_2', tokens_out: 50 },
  });
  const rendered = render(view());
  await waitFor(() => expect(getAgentMessageRequest).toHaveBeenCalledTimes(1));
  rendered.rerender(view('msg_2'));
  await screen.findByText('req_2');
  await act(async () => finishFirst(MEASURED));
  expect(screen.queryByText('req_1')).not.toBeInTheDocument();
  expect(screen.getByText('Output tokens').nextElementSibling).toHaveTextContent('50');
});
