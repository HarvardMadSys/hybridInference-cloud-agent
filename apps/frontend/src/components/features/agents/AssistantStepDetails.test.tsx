// @vitest-environment jsdom
import '@testing-library/jest-dom/vitest';

import { cleanup, render, screen } from '@testing-library/react';
import { afterEach, expect, it, vi } from 'vitest';
import { AssistantStepDetails } from './AssistantStepDetails';
import { buildTrajectorySteps, type StoredEvent } from './trajectorySteps';

afterEach(cleanup);

function view(usage: Record<string, number>) {
  const events = [
    {
      id: 1,
      seq: 1,
      attempt_id: 7,
      job_id: 'job_1',
      turn_no: 1,
      event_type: 'message',
      created_at: '2026-09-27T10:00:03.000Z',
      payload: { text: 'hello', usage_id: 'msg_1', usage },
    },
  ] as StoredEvent[];
  return (
    <AssistantStepDetails
      step={buildTrajectorySteps(events)[0]}
      tab="summary"
      tools={[]}
      onSelectTab={vi.fn()}
    />
  );
}

it('shows what the agent reported for a message, and says so', () => {
  render(view({ output_tokens: 20 }));
  expect(screen.getByText('Runtime report')).toBeInTheDocument();
  expect(screen.getByText('20')).toBeInTheDocument();
  // No provider request is ever read: there is nothing to time or price.
  expect(screen.queryByText('Request timing')).toBeNull();
  expect(screen.queryByText(/\$/)).toBeNull();
});

it('keeps a reported zero as a zero', () => {
  render(view({ output_tokens: 0 }));
  expect(screen.getByText('0')).toBeInTheDocument();
});
