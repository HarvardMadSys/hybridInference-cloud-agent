// @vitest-environment jsdom
import '@testing-library/jest-dom/vitest';
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import {
  createGitHubRepository,
  getAgentConfig,
  listRepoBranches,
  type AgentConfigApi,
} from '@/lib/api/agents';
import { DeferredCreateRepositoryDialog } from './DeferredCreateRepositoryDialog';

vi.mock('@/lib/api/agents', () => ({
  createGitHubRepository: vi.fn(),
  getAgentConfig: vi.fn(),
  listRepoBranches: vi.fn(),
}));
vi.mock('@/components/providers', async () => {
  const { translate } = await import('@/lib/i18n/translate');
  return { useT: () => (key: Parameters<typeof translate>[1]) => translate('en', key) };
});

const config = (repos: string[] = []): AgentConfigApi => ({
  repos,
  github_connected: true,
  github_install_url: null,
  models: [],
  runtimes: [],
});

beforeEach(() => {
  vi.mocked(createGitHubRepository).mockResolvedValue({
    repo: 'alice/new-project',
    web_url: 'https://github.com/alice/new-project',
    default_branch: 'main',
    accessible: false,
  });
  vi.mocked(listRepoBranches).mockResolvedValue({ default: 'main', branches: ['main'] });
});

afterEach(() => {
  cleanup();
  vi.resetAllMocks();
});

describe('deferred repository creation', () => {
  it('renders nothing while closed and opens the real creation form on first use', async () => {
    const props = { onClose: vi.fn(), onAvailable: vi.fn() };
    const { container, rerender } = render(
      <DeferredCreateRepositoryDialog open={false} {...props} />,
    );
    expect(container).toBeEmptyDOMElement();
    expect(screen.queryByRole('dialog')).not.toBeInTheDocument();
    expect(screen.queryByRole('status')).not.toBeInTheDocument();
    expect(getAgentConfig).not.toHaveBeenCalled();

    rerender(<DeferredCreateRepositoryDialog open {...props} />);
    const input = await screen.findByRole('textbox', { name: 'Repository name' });
    expect(screen.getByRole('dialog', { name: 'Create repository' })).toBeInTheDocument();
    expect(input).toHaveFocus();
    expect(screen.getByRole('radio', { name: /^Private/ })).toBeChecked();
    expect(createGitHubRepository).not.toHaveBeenCalled();
  });

  it('retains a creation receipt when closed and retries access without creating twice', async () => {
    vi.mocked(getAgentConfig)
      .mockResolvedValueOnce(config())
      .mockRejectedValueOnce(new Error('Availability check failed'))
      .mockResolvedValueOnce(config(['alice/new-project']));
    const available = vi.fn();
    const close = vi.fn();
    const props = { onClose: close, onAvailable: available };
    const { rerender } = render(<DeferredCreateRepositoryDialog open={false} {...props} />);
    rerender(<DeferredCreateRepositoryDialog open {...props} />);
    const input = await screen.findByRole('textbox', { name: 'Repository name' });
    fireEvent.change(input, { target: { value: 'new-project' } });
    fireEvent.submit(input.closest('form')!);

    expect(await screen.findByRole('status')).toHaveTextContent('not available');
    expect(screen.getByRole('heading', { name: 'Repository created' })).toBeInTheDocument();
    expect(screen.getByRole('link', { name: 'alice/new-project' })).toHaveAttribute(
      'href',
      'https://github.com/alice/new-project',
    );
    expect(available).not.toHaveBeenCalled();
    expect(listRepoBranches).not.toHaveBeenCalled();
    expect(createGitHubRepository).toHaveBeenCalledExactlyOnceWith({
      name: 'new-project',
      private: true,
    });

    fireEvent.keyDown(screen.getByRole('dialog'), { key: 'Escape' });
    expect(close).toHaveBeenCalledTimes(1);
    rerender(<DeferredCreateRepositoryDialog open={false} {...props} />);
    expect(screen.queryByRole('dialog')).not.toBeInTheDocument();
    rerender(<DeferredCreateRepositoryDialog open {...props} />);
    expect(await screen.findByRole('heading', { name: 'Repository created' })).toBeInTheDocument();
    expect(screen.queryByRole('textbox', { name: 'Repository name' })).not.toBeInTheDocument();
    expect(getAgentConfig).toHaveBeenCalledTimes(1);

    fireEvent.click(screen.getByRole('button', { name: 'Refresh availability' }));
    expect(await screen.findByRole('alert')).toHaveTextContent('Availability check failed');
    expect(screen.getByRole('heading', { name: 'Repository created' })).toBeInTheDocument();
    fireEvent.click(screen.getByRole('button', { name: 'Refresh availability' }));
    await waitFor(() =>
      expect(available).toHaveBeenCalledExactlyOnceWith(
        'alice/new-project',
        config(['alice/new-project']),
      ),
    );
    expect(close).toHaveBeenCalledTimes(2);
    expect(createGitHubRepository).toHaveBeenCalledTimes(1);
    expect(vi.mocked(getAgentConfig).mock.calls).toEqual(Array(3).fill([{ fresh: true }]));
    expect(listRepoBranches).toHaveBeenCalledExactlyOnceWith('alice/new-project');
  });
});
