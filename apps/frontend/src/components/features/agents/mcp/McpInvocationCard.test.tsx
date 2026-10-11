// @vitest-environment jsdom
import '@testing-library/jest-dom/vitest';
import { act, cleanup, fireEvent, render, screen } from '@testing-library/react';
import { afterEach, expect, it, vi } from 'vitest';
import type { McpInvocationApi } from '@/lib/api/mcp';
import { McpInvocationCard } from './McpInvocationCard';

afterEach(() => {
  cleanup();
  vi.useRealTimers();
});

it('disables every decision when a held request reaches its deadline', async () => {
  vi.useFakeTimers();
  const now = Date.now();
  const invocation = {
    connection_id: 3,
    connection_label: 'Team wiki',
    capability_name: 'search',
    capability_kind: 'tool',
    capability_description: null,
    capability_annotations: {},
    arguments: {},
    created_at: new Date(now).toISOString(),
    expires_at: new Date(now + 1000).toISOString(),
  } as McpInvocationApi;
  const onDecision = vi.fn();
  const onAlwaysAllow = vi.fn();
  render(
    <McpInvocationCard
      invocation={invocation}
      busy={false}
      onDecision={onDecision}
      onAlwaysAllow={onAlwaysAllow}
    />,
  );
  expect(screen.getByRole('button', { name: 'Approve' })).toBeEnabled();
  await act(async () => vi.advanceTimersByTimeAsync(1000));
  expect(screen.getByText('Expired')).toBeInTheDocument();
  for (const button of screen.getAllByRole('button')) {
    expect(button).toBeDisabled();
    fireEvent.click(button);
  }
  expect(onDecision).not.toHaveBeenCalled();
  expect(onAlwaysAllow).not.toHaveBeenCalled();
});
