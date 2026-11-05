// @vitest-environment jsdom
import '@testing-library/jest-dom/vitest';
import { act, cleanup, fireEvent, render, renderHook, screen } from '@testing-library/react';
import { afterEach, expect, it, vi } from 'vitest';
import { useJobDetailPanels } from '@/components/features/agents/job-detail/useJobDetailPanels';
import type { AgentJob } from '@/components/features/agents/types';
import { Select } from './Select';

const job = { id: 'active-turn', threadId: 'chat', state: 'running' } as AgentJob;
function renderSelect() {
  render(
    <Select
      aria-label="Agent"
      value="all"
      onValueChange={vi.fn()}
      options={[
        { value: 'all', label: 'All agents' },
        { value: 'child', label: 'Child agent' },
      ]}
    />,
  );
  return screen.getByRole('combobox', { name: 'Agent' });
}
afterEach(cleanup);

it('keeps the step inspector open when its capture handler receives a select Escape', () => {
  const { result } = renderHook(() => useJobDetailPanels({ job, events: [], threadJobs: [] }));
  act(() => result.current.setTab('trajectory'));
  act(() => result.current.selectStep('selected-step'));
  const trigger = renderSelect();
  fireEvent.keyDown(trigger, { key: 'Escape' });
  expect(result.current.inspector?.kind).toBe('step');
  fireEvent.click(trigger);
  fireEvent.keyDown(screen.getByRole('listbox'), { key: 'Escape' });
  expect(screen.queryByRole('listbox')).not.toBeInTheDocument();
  expect(result.current.inspector?.kind).toBe('step');

  fireEvent.keyDown(window, { key: 'Escape' });
  expect(result.current.inspector).toBeNull();
});
