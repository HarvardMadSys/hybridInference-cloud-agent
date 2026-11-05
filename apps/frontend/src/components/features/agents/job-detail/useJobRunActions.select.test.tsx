// @vitest-environment jsdom
import '@testing-library/jest-dom/vitest';

import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { useState } from 'react';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { Select } from '@/components/ui/Select';
import { cancelAgentJob } from '@/lib/api/agents';
import type { AgentJob } from '../types';
import { useJobRunActions } from './useJobRunActions';

vi.mock('next/navigation', () => ({ useRouter: () => ({ push: vi.fn() }) }));
vi.mock('@/components/providers', () => ({ useT: () => (key: string) => key }));
vi.mock('react-hot-toast', () => ({ default: { success: vi.fn() } }));
vi.mock('@/lib/api/agents', () => ({
  forkAgentJob: vi.fn(),
  cancelAgentJob: vi.fn(),
  restoreAgentJob: vi.fn(),
}));

function RunningTurn() {
  useJobRunActions({ job: { id: 'turn', state: 'running' } as AgentJob, onError: vi.fn() });
  const [file, setFile] = useState('a.ts');
  return (
    <Select
      aria-label="Changed file"
      value={file}
      onValueChange={setFile}
      options={[
        { value: 'a.ts', label: 'a.ts' },
        { value: 'b.ts', label: 'b.ts' },
      ]}
    />
  );
}

afterEach(cleanup);

describe('Escape on a select during a running turn', () => {
  it('closes the menu, and is left to the select when closed, without stopping the run', async () => {
    vi.mocked(cancelAgentJob).mockResolvedValue({ id: 'turn', state: 'running' } as never);
    render(<RunningTurn />);
    const trigger = screen.getByRole('combobox', { name: 'Changed file' });

    fireEvent.click(trigger);
    fireEvent.keyDown(screen.getByRole('listbox'), { key: 'Escape' });
    expect(screen.queryByRole('listbox')).not.toBeInTheDocument();
    expect(trigger).toHaveFocus();
    fireEvent.keyDown(trigger, { key: 'Escape' });
    expect(cancelAgentJob).not.toHaveBeenCalled();

    fireEvent.keyDown(document.body, { key: 'Escape' });
    await waitFor(() => expect(cancelAgentJob).toHaveBeenCalledWith('turn'));
  });
});
