// @vitest-environment jsdom
import '@testing-library/jest-dom/vitest';
import { cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { LocaleProvider } from '@/components/providers/LocaleProvider';
import {
  addMemory,
  deleteMemory,
  getMemories,
  restoreMemory,
  updateMemory,
  type AgentMemory,
  type AgentMemoryScope,
} from '@/lib/api/agentMemory';
import { LOCALE_STORAGE_KEY } from '@/lib/i18n/locales';
import { APIError } from '@/lib/utils/errors';
import { MemoryScopePanel } from './MemoryScopePanel';

vi.mock('@/lib/api/agentMemory', () => ({
  getMemories: vi.fn(),
  addMemory: vi.fn(),
  updateMemory: vi.fn(),
  deleteMemory: vi.fn(),
  restoreMemory: vi.fn(),
  clearMemories: vi.fn(),
  setMemoryEnabled: vi.fn(),
}));

function memory(changes: Partial<AgentMemory> = {}): AgentMemory {
  return {
    id: 'm1',
    repo: 'alice/api',
    content: 'Use pnpm, not npm.',
    description: 'Use pnpm, not npm.',
    status: 'active',
    origin: 'agent',
    source_job_ids: ['job-1'],
    version: 2,
    created_at: '2026-10-14T00:00:00Z',
    updated_at: '2026-10-14T00:00:00Z',
    last_used_at: '2026-10-14T08:00:00Z',
    archived_at: null,
    ...changes,
  };
}

const saved = memory();
const written = memory({
  id: 'm2',
  content: 'Reply in Chinese.',
  origin: 'manual',
  last_used_at: null,
});
const old = memory({
  id: 'm0',
  content: 'Use npm.',
  status: 'archived',
  archived_at: '2026-10-13T00:00:00Z',
});
const scope: AgentMemoryScope = {
  repo: 'alice/api',
  enabled: true,
  memories: [saved, written],
  archived: [old],
};

function panel() {
  return (
    <LocaleProvider>
      <MemoryScopePanel repo="alice/api" />
    </LocaleProvider>
  );
}

beforeEach(() => {
  vi.resetAllMocks();
  localStorage.clear();
  localStorage.setItem(LOCALE_STORAGE_KEY, 'en');
  vi.mocked(getMemories).mockResolvedValue(scope);
});
afterEach(cleanup);

describe('one scope of durable memories', () => {
  it('lists memories with who saved them and when they were last used', async () => {
    render(panel());
    const agentRow = (await screen.findByText(saved.content)).closest('li')!;
    expect(within(agentRow).getByText(/Saved by an agent · Last used/)).toBeInTheDocument();
    const ownRow = screen.getByText(written.content).closest('li')!;
    expect(within(ownRow).getByText('Written by you')).toBeInTheDocument();
    expect(screen.getByText('2/30')).toBeInTheDocument();
    expect(getMemories).toHaveBeenCalledWith('alice/api', expect.any(AbortSignal));
  });

  it('adds a memory to this scope and clears the draft once saved', async () => {
    vi.mocked(addMemory).mockResolvedValue(memory({ id: 'm3', content: 'Run make check.' }));
    render(panel());
    await screen.findByText(saved.content);
    const input = screen.getByRole('textbox', { name: 'New memory' });
    fireEvent.change(input, { target: { value: '  Run make check.  ' } });
    fireEvent.click(screen.getByRole('button', { name: 'Add' }));
    await waitFor(() => expect(input).toHaveValue(''));
    expect(addMemory).toHaveBeenCalledWith('alice/api', 'Run make check.', expect.any(AbortSignal));
    expect(getMemories).toHaveBeenCalledTimes(2);
  });

  it('edits a memory at the version shown and keeps the draft when it changed meanwhile', async () => {
    vi.mocked(updateMemory).mockRejectedValueOnce(
      new APIError('memory_changed', 'Memory changed.', 409),
    );
    render(panel());
    const row = (await screen.findByText(saved.content)).closest('li')!;
    fireEvent.click(within(row).getByRole('button', { name: 'Edit' }));
    const editor = within(row).getByRole('textbox', { name: 'Memory text' });
    fireEvent.change(editor, { target: { value: 'Use yarn.' } });
    fireEvent.click(within(row).getByRole('button', { name: 'Save' }));
    expect(await screen.findByRole('alert')).toHaveTextContent('This memory changed');
    expect(within(row).getByRole('textbox', { name: 'Memory text' })).toHaveValue('Use yarn.');
    expect(updateMemory).toHaveBeenCalledWith(
      saved,
      'Use yarn.',
      'alice/api',
      expect.any(AbortSignal),
    );

    vi.mocked(updateMemory).mockResolvedValueOnce(memory({ content: 'Use yarn.' }));
    fireEvent.click(within(row).getByRole('button', { name: 'Save' }));
    await waitFor(() =>
      expect(within(row).queryByRole('textbox', { name: 'Memory text' })).not.toBeInTheDocument(),
    );
  });

  it('asks before deleting and restores archived memories', async () => {
    render(panel());
    const row = (await screen.findByText(saved.content)).closest('li')!;
    fireEvent.click(within(row).getByRole('button', { name: 'Delete' }));
    expect(deleteMemory).not.toHaveBeenCalled();
    fireEvent.click(within(row).getByRole('button', { name: 'Yes, delete' }));
    await waitFor(() => expect(deleteMemory).toHaveBeenCalledWith(saved, expect.any(AbortSignal)));

    fireEvent.click(screen.getByText('Archived (1)'));
    fireEvent.click(screen.getByRole('button', { name: 'Restore' }));
    await waitFor(() => expect(restoreMemory).toHaveBeenCalledWith(old, expect.any(AbortSignal)));
  });

  it('names the reason a change was refused', async () => {
    vi.mocked(addMemory).mockRejectedValueOnce(new APIError('memory_scope_full', 'Full.', 409));
    vi.mocked(addMemory).mockRejectedValueOnce(new APIError('memory_invalid', 'Invalid.', 422));
    render(panel());
    await screen.findByText(saved.content);
    const input = screen.getByRole('textbox', { name: 'New memory' });
    fireEvent.change(input, { target: { value: 'Another rule' } });
    fireEvent.click(screen.getByRole('button', { name: 'Add' }));
    expect(await screen.findByRole('alert')).toHaveTextContent('holds 30 memories you wrote');
    expect(input).toHaveValue('Another rule');
    fireEvent.click(screen.getByRole('button', { name: 'Add' }));
    await waitFor(() =>
      expect(screen.getByRole('alert')).toHaveTextContent('at most 240 characters'),
    );
  });

  it('explains a paused scope and retries a failed load', async () => {
    vi.mocked(getMemories)
      .mockRejectedValueOnce(new Error('offline'))
      .mockResolvedValueOnce({ ...scope, enabled: false, memories: [], archived: [] });
    render(panel());
    fireEvent.click(await screen.findByRole('button', { name: 'Retry' }));
    expect(await screen.findByRole('switch', { name: 'Use memory' })).not.toBeChecked();
    expect(screen.getByText(/tasks do not see these memories/)).toBeInTheDocument();
    expect(screen.getByText(/No memories yet/)).toBeInTheDocument();
  });

  it('speaks Chinese', async () => {
    localStorage.setItem(LOCALE_STORAGE_KEY, 'zh-CN');
    render(panel());
    const row = (await screen.findByText(saved.content)).closest('li')!;
    expect(within(row).getByText(/Agent 记下 · 上次使用/)).toBeInTheDocument();
    expect(screen.getByRole('switch', { name: '启用记忆' })).toBeChecked();
    expect(screen.getByText('已归档（1）')).toBeInTheDocument();
  });
});
