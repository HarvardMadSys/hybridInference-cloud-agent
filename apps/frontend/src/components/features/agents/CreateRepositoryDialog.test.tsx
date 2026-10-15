// @vitest-environment jsdom
import '@testing-library/jest-dom/vitest';
import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import {
  createGitHubRepository,
  getAgentConfig,
  listRepoBranches,
  type AgentConfigApi,
  type CreatedGitHubRepository,
  type RepoBranchesApi,
} from '@/lib/api/agents';
import { CreateRepositoryDialog } from './CreateRepositoryDialog';

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
const repository = (accessible = false): CreatedGitHubRepository => ({
  repo: 'alice/new-project',
  web_url: 'https://github.com/alice/new-project',
  default_branch: 'main',
  accessible,
});
function deferred<T>() {
  let resolve!: (value: T) => void;
  const promise = new Promise<T>((finish) => {
    resolve = finish;
  });
  return { promise, resolve };
}
function submit(name = 'new-project') {
  const input = screen.getByRole('textbox', { name: 'Repository name' });
  fireEvent.change(input, { target: { value: name } });
  fireEvent.submit(input.closest('form')!);
}

beforeEach(() => {
  vi.mocked(listRepoBranches).mockResolvedValue({ default: 'main', branches: ['main'] });
});

afterEach(() => {
  cleanup();
  vi.resetAllMocks();
});

describe('create repository dialog', () => {
  it('defaults to private and submits once while creation is pending', async () => {
    const pending = deferred<CreatedGitHubRepository>();
    vi.mocked(createGitHubRepository).mockReturnValue(pending.promise);
    vi.mocked(getAgentConfig).mockResolvedValue(config());
    const close = vi.fn();
    render(<CreateRepositoryDialog open onClose={close} onAvailable={vi.fn()} />);

    expect(screen.getByRole('radio', { name: /^Private/ })).toBeChecked();
    expect(screen.getByRole('button', { name: 'Create repository' })).toBeDisabled();
    submit();
    fireEvent.submit(screen.getByRole('textbox', { name: 'Repository name' }).closest('form')!);
    expect(createGitHubRepository).toHaveBeenCalledExactlyOnceWith({
      name: 'new-project',
      private: true,
    });
    expect(screen.getByRole('button', { name: 'Creating…' })).toBeDisabled();
    const dialog = screen.getByRole('dialog');
    expect(dialog).toHaveFocus();
    fireEvent.keyDown(dialog, { key: 'Tab' });
    expect(dialog).toHaveFocus();
    fireEvent.keyDown(dialog, { key: 'Escape' });
    expect(close).not.toHaveBeenCalled();
    await act(async () => pending.resolve(repository()));
    expect(await screen.findByRole('status')).toHaveTextContent('not available');
  });

  it('allows public visibility and sends the optional description', async () => {
    vi.mocked(createGitHubRepository).mockResolvedValue(repository());
    vi.mocked(getAgentConfig).mockResolvedValue(config());
    render(<CreateRepositoryDialog open onClose={vi.fn()} onAvailable={vi.fn()} />);

    fireEvent.click(screen.getByRole('radio', { name: /^Public/ }));
    fireEvent.change(screen.getByRole('textbox', { name: 'Description (optional)' }), {
      target: { value: '  A new project  ' },
    });
    submit('new-project');
    await screen.findByRole('status');
    expect(createGitHubRepository).toHaveBeenCalledExactlyOnceWith({
      name: 'new-project',
      description: 'A new project',
      private: false,
    });
  });

  it('shows a creation error without automatically retrying or checking access', async () => {
    vi.mocked(createGitHubRepository).mockRejectedValue(new Error('Repository already exists'));
    const available = vi.fn();
    render(<CreateRepositoryDialog open onClose={vi.fn()} onAvailable={available} />);

    submit();
    expect(await screen.findByRole('alert')).toHaveTextContent('Repository already exists');
    expect(screen.getByRole('button', { name: 'Create repository' })).toBeEnabled();
    expect(createGitHubRepository).toHaveBeenCalledTimes(1);
    expect(getAgentConfig).not.toHaveBeenCalled();
    expect(available).not.toHaveBeenCalled();
  });

  it('keeps a created repository while access is unavailable and refreshes until it is usable', async () => {
    vi.mocked(createGitHubRepository).mockResolvedValue(repository());
    vi.mocked(getAgentConfig)
      .mockResolvedValueOnce(config())
      .mockResolvedValueOnce(config(['alice/new-project']));
    const available = vi.fn();
    const close = vi.fn();
    render(<CreateRepositoryDialog open onClose={close} onAvailable={available} />);

    submit();
    await screen.findByRole('status');
    const configure = screen.getByRole('link', { name: 'Configure repository access' });
    expect(configure).toHaveAttribute('href', '/settings/integrations');
    expect(configure).toHaveAttribute('target', '_blank');
    expect(configure).toHaveAttribute('rel', 'noopener noreferrer');
    expect(available).not.toHaveBeenCalled();
    fireEvent.click(screen.getByRole('button', { name: 'Refresh availability' }));
    await waitFor(() =>
      expect(available).toHaveBeenCalledExactlyOnceWith(
        'alice/new-project',
        config(['alice/new-project']),
      ),
    );
    expect(close).toHaveBeenCalledTimes(1);
    expect(getAgentConfig).toHaveBeenNthCalledWith(1, { fresh: true });
    expect(getAgentConfig).toHaveBeenNthCalledWith(2, { fresh: true });
    expect(createGitHubRepository).toHaveBeenCalledTimes(1);
  });

  it('preserves success through refresh failures and closing, so retries never create again', async () => {
    vi.mocked(createGitHubRepository).mockResolvedValue(repository());
    vi.mocked(getAgentConfig)
      .mockRejectedValueOnce(new Error('Availability check failed'))
      .mockRejectedValueOnce(new Error('Still unavailable'))
      .mockResolvedValueOnce(config(['alice/new-project']));
    const available = vi.fn();
    const close = vi.fn();
    const props = { onClose: close, onAvailable: available };
    const { rerender } = render(<CreateRepositoryDialog open {...props} />);

    submit();
    expect(await screen.findByRole('alert')).toHaveTextContent('Availability check failed');
    expect(screen.getByRole('heading', { name: 'Repository created' })).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: 'Create repository' })).not.toBeInTheDocument();
    fireEvent.keyDown(screen.getByRole('dialog'), { key: 'Escape' });
    expect(close).toHaveBeenCalledTimes(1);
    rerender(<CreateRepositoryDialog open={false} {...props} />);
    rerender(<CreateRepositoryDialog open {...props} />);
    expect(screen.getByRole('heading', { name: 'Repository created' })).toBeInTheDocument();
    fireEvent.click(screen.getByRole('button', { name: 'Refresh availability' }));
    expect(await screen.findByRole('alert')).toHaveTextContent('Still unavailable');
    fireEvent.click(screen.getByRole('button', { name: 'Refresh availability' }));
    await waitFor(() => expect(available).toHaveBeenCalledTimes(1));
    expect(createGitHubRepository).toHaveBeenCalledTimes(1);
    expect(getAgentConfig).toHaveBeenCalledTimes(3);
  });

  it('adds an authoritatively accessible repository to a picker list that has not caught up', async () => {
    vi.mocked(createGitHubRepository).mockResolvedValue(repository(true));
    vi.mocked(getAgentConfig).mockResolvedValue(config(['alice/existing', 'alice/existing']));
    const available = vi.fn();
    render(<CreateRepositoryDialog open onClose={vi.fn()} onAvailable={available} />);

    submit();
    await waitFor(() =>
      expect(available).toHaveBeenCalledExactlyOnceWith(
        'alice/new-project',
        config(['alice/existing', 'alice/new-project']),
      ),
    );
    expect(listRepoBranches).toHaveBeenCalledExactlyOnceWith('alice/new-project');
  });

  it('requires a usable branch even when the repository is already in the configured allowlist', async () => {
    vi.mocked(createGitHubRepository).mockResolvedValue(repository());
    vi.mocked(getAgentConfig).mockResolvedValue(config(['alice/new-project']));
    vi.mocked(listRepoBranches)
      .mockResolvedValueOnce({ default: null, branches: [] })
      .mockResolvedValueOnce({ default: 'main', branches: ['main'] });
    const available = vi.fn();
    render(<CreateRepositoryDialog open onClose={vi.fn()} onAvailable={available} />);

    submit();
    expect(await screen.findByRole('status')).toHaveTextContent('not available');
    expect(available).not.toHaveBeenCalled();
    fireEvent.click(screen.getByRole('button', { name: 'Refresh availability' }));
    await waitFor(() => expect(available).toHaveBeenCalledTimes(1));
    expect(listRepoBranches).toHaveBeenCalledTimes(2);
    expect(createGitHubRepository).toHaveBeenCalledTimes(1);
  });

  it('preserves creation when previously accessible repository access has been revoked', async () => {
    vi.mocked(createGitHubRepository).mockResolvedValue(repository(true));
    vi.mocked(getAgentConfig).mockResolvedValue(config());
    vi.mocked(listRepoBranches)
      .mockRejectedValueOnce(new Error('Repository access was revoked'))
      .mockResolvedValueOnce({ default: 'main', branches: ['main'] });
    const available = vi.fn();
    render(<CreateRepositoryDialog open onClose={vi.fn()} onAvailable={available} />);

    submit();
    expect(await screen.findByRole('alert')).toHaveTextContent('Repository access was revoked');
    expect(screen.getByRole('heading', { name: 'Repository created' })).toBeInTheDocument();
    expect(available).not.toHaveBeenCalled();
    fireEvent.click(screen.getByRole('button', { name: 'Refresh availability' }));
    await waitFor(() => expect(available).toHaveBeenCalledTimes(1));
    expect(listRepoBranches).toHaveBeenCalledTimes(2);
    expect(createGitHubRepository).toHaveBeenCalledTimes(1);
  });

  it('focuses and traps the form, restores focus on close, and focuses the created repository', async () => {
    vi.mocked(createGitHubRepository).mockResolvedValue(repository());
    vi.mocked(getAgentConfig).mockResolvedValue(config());
    const close = vi.fn();
    const props = { onClose: close, onAvailable: vi.fn() };
    const { rerender } = render(
      <>
        <button>Open creation</button>
        <CreateRepositoryDialog open={false} {...props} />
      </>,
    );
    const opener = screen.getByRole('button', { name: 'Open creation' });
    opener.focus();
    const contents = (open: boolean) => (
      <>
        <button>Open creation</button>
        <CreateRepositoryDialog open={open} {...props} />
      </>
    );
    rerender(contents(true));
    const input = screen.getByRole('textbox', { name: 'Repository name' });
    expect(input).toHaveFocus();
    fireEvent.change(input, { target: { value: 'new-project' } });
    const submitButton = screen.getByRole('button', { name: 'Create repository' });
    const closeButton = screen.getAllByRole('button', { name: 'Close repository dialog' })[1];
    submitButton.focus();
    fireEvent.keyDown(submitButton, { key: 'Tab' });
    expect(closeButton).toHaveFocus();
    fireEvent.keyDown(closeButton, { key: 'Tab', shiftKey: true });
    expect(submitButton).toHaveFocus();
    fireEvent.keyDown(input, { key: 'Escape' });
    expect(close).toHaveBeenCalledTimes(1);
    rerender(contents(false));
    expect(opener).toHaveFocus();
    rerender(contents(true));
    submit();
    await screen.findByRole('status');
    expect(screen.getByRole('link', { name: 'alice/new-project' })).toHaveFocus();
    const refresh = screen.getByRole('button', { name: 'Refresh availability' });
    refresh.focus();
    fireEvent.keyDown(refresh, { key: 'Tab' });
    expect(screen.getAllByRole('button', { name: 'Close repository dialog' })[1]).toHaveFocus();
  });

  it('ignores a creation response after the dialog is unmounted', async () => {
    const pending = deferred<CreatedGitHubRepository>();
    vi.mocked(createGitHubRepository).mockReturnValue(pending.promise);
    const available = vi.fn();
    const close = vi.fn();
    const { unmount } = render(
      <CreateRepositoryDialog open onClose={close} onAvailable={available} />,
    );
    submit();
    unmount();
    await act(async () => pending.resolve(repository(true)));
    expect(getAgentConfig).not.toHaveBeenCalled();
    expect(available).not.toHaveBeenCalled();
    expect(close).not.toHaveBeenCalled();
  });

  it('ignores an availability response after the dialog is unmounted', async () => {
    const pending = deferred<AgentConfigApi>();
    vi.mocked(createGitHubRepository).mockResolvedValue(repository());
    vi.mocked(getAgentConfig).mockReturnValue(pending.promise);
    const available = vi.fn();
    const close = vi.fn();
    const { unmount } = render(
      <CreateRepositoryDialog open onClose={close} onAvailable={available} />,
    );
    submit();
    await waitFor(() => expect(getAgentConfig).toHaveBeenCalledTimes(1));
    unmount();
    await act(async () => pending.resolve(config(['alice/new-project'])));
    expect(available).not.toHaveBeenCalled();
    expect(close).not.toHaveBeenCalled();
  });

  it('ignores a branch response after the dialog is unmounted', async () => {
    const pending = deferred<RepoBranchesApi>();
    vi.mocked(createGitHubRepository).mockResolvedValue(repository(true));
    vi.mocked(getAgentConfig).mockResolvedValue(config());
    vi.mocked(listRepoBranches).mockReturnValue(pending.promise);
    const available = vi.fn();
    const close = vi.fn();
    const { unmount } = render(
      <CreateRepositoryDialog open onClose={close} onAvailable={available} />,
    );
    submit();
    await waitFor(() => expect(listRepoBranches).toHaveBeenCalledTimes(1));
    unmount();
    await act(async () => pending.resolve({ default: 'main', branches: ['main'] }));
    expect(available).not.toHaveBeenCalled();
    expect(close).not.toHaveBeenCalled();
  });
});
