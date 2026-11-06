// @vitest-environment jsdom
import '@testing-library/jest-dom/vitest';
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { getAgentThreadMessage } from '@/lib/api/agents/threads';
import { ThreadTurn } from './ThreadTurn';

vi.mock('@/lib/api/agents/threads', () => ({ getAgentThreadMessage: vi.fn() }));
afterEach(() => {
  cleanup();
  vi.clearAllMocks();
});

describe('explicit message preview expansion', () => {
  it('fetches bounded chunks only on request and rewinds only with the complete prompt', async () => {
    const onRewind = vi.fn();
    vi.mocked(getAgentThreadMessage)
      .mockResolvedValueOnce({
        id: 7,
        job_id: 'job',
        role: 'user',
        content: 'middle',
        next_offset: 12,
        content_truncated: true,
        created_at: null,
      })
      .mockResolvedValueOnce({
        id: 7,
        job_id: 'job',
        role: 'user',
        content: 'end',
        next_offset: null,
        created_at: null,
      });
    render(
      <ThreadTurn
        message={{
          id: 7,
          jobId: 'job',
          role: 'user',
          content: 'start🙂',
          contentTruncated: true,
          nextOffset: 6,
          createdAt: null,
        }}
        onRewind={onRewind}
      />,
    );
    expect(getAgentThreadMessage).not.toHaveBeenCalled();
    expect(screen.queryByRole('button', { name: 'Edit & rewind' })).not.toBeInTheDocument();
    fireEvent.click(screen.getByRole('button', { name: 'Load more of this message' }));
    await waitFor(() => expect(screen.getByText('start🙂middle')).toBeInTheDocument());
    expect(getAgentThreadMessage).toHaveBeenLastCalledWith('job', 7, 6);
    fireEvent.click(screen.getByRole('button', { name: 'Load more of this message' }));
    await waitFor(() => expect(screen.getByText('start🙂middleend')).toBeInTheDocument());
    expect(getAgentThreadMessage).toHaveBeenLastCalledWith('job', 7, 12);
    expect(screen.queryByText('Message preview.')).not.toBeInTheDocument();
    fireEvent.click(screen.getByRole('button', { name: 'Edit & rewind' }));
    expect(onRewind).toHaveBeenCalledWith('start🙂middleend');
  });

  it('keeps a failed expansion retryable without discarding the shown prefix', async () => {
    vi.mocked(getAgentThreadMessage)
      .mockRejectedValueOnce(new Error('Offline'))
      .mockResolvedValueOnce({
        id: 8,
        job_id: 'job',
        role: 'assistant',
        content: ' tail',
        next_offset: null,
        created_at: null,
      });
    render(
      <ThreadTurn
        message={{
          id: 8,
          jobId: 'job',
          role: 'assistant',
          content: 'prefix',
          contentTruncated: true,
          nextOffset: 6,
          createdAt: null,
        }}
      />,
    );
    fireEvent.click(screen.getByRole('button', { name: 'Load more of this message' }));
    await screen.findByRole('alert');
    expect(screen.getByText('prefix')).toBeInTheDocument();
    fireEvent.click(screen.getByRole('button', { name: 'Load more of this message' }));
    await screen.findByText('prefix tail');
    expect(getAgentThreadMessage).toHaveBeenNthCalledWith(2, 'job', 8, 6);
  });
});
