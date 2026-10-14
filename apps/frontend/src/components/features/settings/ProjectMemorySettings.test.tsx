// @vitest-environment jsdom
import '@testing-library/jest-dom/vitest';
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { LocaleProvider } from '@/components/providers/LocaleProvider';
import { getMemories, getMemoryProjects } from '@/lib/api/agentMemory';
import { LOCALE_STORAGE_KEY } from '@/lib/i18n/locales';
import { ProjectMemorySettings } from './ProjectMemorySettings';

vi.mock('@/components/providers/AuthProvider', () => ({
  useAuth: () => ({ state: { user: { id: 'alice' } } }),
}));
vi.mock('@/lib/api/agentMemory', () => ({
  getMemories: vi.fn(),
  getMemoryProjects: vi.fn(),
  addMemory: vi.fn(),
  updateMemory: vi.fn(),
  deleteMemory: vi.fn(),
  restoreMemory: vi.fn(),
  clearMemories: vi.fn(),
  setMemoryEnabled: vi.fn(),
}));

function project() {
  return (
    <LocaleProvider>
      <ProjectMemorySettings />
    </LocaleProvider>
  );
}

beforeEach(() => {
  vi.resetAllMocks();
  localStorage.clear();
  localStorage.setItem(LOCALE_STORAGE_KEY, 'en');
  vi.mocked(getMemories).mockImplementation(async (repo) => ({
    repo,
    enabled: true,
    memories: [],
    archived: [],
  }));
});
afterEach(cleanup);

describe('project memory', () => {
  it('loads the first repository and follows the selection', async () => {
    vi.mocked(getMemoryProjects).mockResolvedValue(['alice/api', 'alice/web']);
    render(project());
    const select = await screen.findByRole('combobox', { name: 'Repository' });
    await waitFor(() =>
      expect(getMemories).toHaveBeenCalledWith('alice/api', expect.any(AbortSignal)),
    );
    fireEvent.change(select, { target: { value: 'alice/web' } });
    await waitFor(() =>
      expect(getMemories).toHaveBeenCalledWith('alice/web', expect.any(AbortSignal)),
    );
  });

  it('shows an empty repository list without loading any project', async () => {
    vi.mocked(getMemoryProjects).mockResolvedValue([]);
    render(project());
    expect(await screen.findByText('Repositories you work in appear here.')).toBeInTheDocument();
    expect(getMemories).not.toHaveBeenCalled();
  });

  it('retries a failed repository list', async () => {
    vi.mocked(getMemoryProjects)
      .mockRejectedValueOnce(new Error('offline'))
      .mockResolvedValueOnce(['alice/api']);
    render(project());
    expect(await screen.findByRole('alert')).toHaveTextContent('Could not load repositories.');
    fireEvent.click(screen.getByRole('button', { name: 'Refresh repositories' }));
    await waitFor(() =>
      expect(getMemories).toHaveBeenCalledWith('alice/api', expect.any(AbortSignal)),
    );
  });
});
