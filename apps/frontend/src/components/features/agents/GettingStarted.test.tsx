// @vitest-environment jsdom
import '@testing-library/jest-dom/vitest';

import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import type { ReactNode } from 'react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import {
  getAgentConfig,
  listAgentConversations,
  type AgentConfigApi,
  type AgentConversationApi,
  type AgentJobApi,
} from '@/lib/api/agents';

import { FirstRunProvider, NEW_TASK_PROMPT_ID, useFirstRun } from './firstRun';
import { GettingStarted, UNDO_SKIP_MS } from './GettingStarted';
import { onGitHubAccessRequest } from './githubReturn';

const nav = vi.hoisted(() => ({ pathname: '/', push: vi.fn() }));

vi.mock('next/navigation', () => ({
  usePathname: () => nav.pathname,
  useRouter: () => ({ push: nav.push }),
}));

vi.mock('@/components/providers', async () => {
  const { translate } = await import('@/lib/i18n/translate');
  const t = (key: Parameters<typeof translate>[1], vars?: Parameters<typeof translate>[2]) =>
    translate('en', key, vars);
  return { useT: () => t };
});

vi.mock('@/lib/api/agents', () => ({
  getAgentConfig: vi.fn(),
  listAgentConversations: vi.fn(),
}));

// Leaving for GitHub is a real navigation; the test stands in for the browser.
import { githubAccessUrl, leaveFor } from './githubAccess';
vi.mock('./githubAccess', () => ({ githubAccessUrl: vi.fn(), leaveFor: vi.fn() }));

function config(overrides: Partial<AgentConfigApi> = {}): AgentConfigApi {
  return {
    repos: [],
    runtimes: ['claude-code'],
    models: ['model-a'],
    github_connected: false,
    github_install_url: '/agents/integrations',
    ...overrides,
  };
}

function conversation(archived = false): AgentConversationApi {
  return {
    job: { id: 'ajob_0', thread_id: 'thread_0' } as AgentJobApi,
    title: 'Earlier task',
    archived,
    last_activity_at: null,
  };
}

function newcomer(overrides: Partial<AgentConfigApi> = {}) {
  vi.mocked(listAgentConversations).mockResolvedValue({ conversations: [], next_cursor: null });
  vi.mocked(getAgentConfig).mockResolvedValue(config(overrides));
}

function sidebar(extra?: ReactNode) {
  return render(
    <FirstRunProvider userId="alice">
      {extra}
      <GettingStarted />
    </FirstRunProvider>,
  );
}

describe('GettingStarted', () => {
  beforeEach(() => {
    localStorage.clear();
    sessionStorage.clear();
    nav.pathname = '/';
    nav.push.mockReset();
    vi.mocked(getAgentConfig).mockReset();
    vi.mocked(listAgentConversations).mockReset();
    vi.mocked(githubAccessUrl).mockReset();
    vi.mocked(leaveFor).mockReset();
  });

  afterEach(() => cleanup());

  it('shows someone with no task the next step, and goes to GitHub for it', async () => {
    newcomer();
    vi.mocked(githubAccessUrl).mockResolvedValue('https://github.test/login/oauth/authorize');

    sidebar();

    const section = await screen.findByRole('region', { name: 'Getting started' });
    expect(section).toHaveTextContent('0/3');
    // One step at a time: the later ones are not listed yet.
    expect(screen.queryByText('Grant a repository')).not.toBeInTheDocument();
    fireEvent.click(screen.getByRole('button', { name: 'Connect GitHub' }));

    await waitFor(() =>
      expect(leaveFor).toHaveBeenCalledWith('https://github.test/login/oauth/authorize'),
    );
    expect(githubAccessUrl).toHaveBeenCalledWith('connect');
    // Back to the new-task page, where the next step is.
    expect(JSON.parse(sessionStorage.getItem('agents.github.return')!).value).toBe('/');
  });

  it('hands the trip to the new-task box when one is on screen', async () => {
    newcomer();
    const take = vi.fn();
    const stop = onGitHubAccessRequest(take);
    try {
      sidebar();
      fireEvent.click(await screen.findByRole('button', { name: 'Connect GitHub' }));

      expect(take).toHaveBeenCalledWith('connect');
      expect(githubAccessUrl).not.toHaveBeenCalled();
    } finally {
      stop();
    }
  });

  it('asks for repository access once GitHub is connected', async () => {
    newcomer({ github_connected: true, repos: [] });
    vi.mocked(githubAccessUrl).mockResolvedValue(
      'https://github.test/apps/agent/installations/new',
    );

    sidebar();

    expect(await screen.findByRole('region', { name: 'Getting started' })).toHaveTextContent('1/3');
    fireEvent.click(screen.getByRole('button', { name: 'Grant a repository' }));
    await waitFor(() => expect(githubAccessUrl).toHaveBeenCalledWith('manage'));
  });

  it('ends at the first task, handing the new-task box the caret', async () => {
    newcomer({ github_connected: true, repos: ['owner/repository'] });

    sidebar(<textarea id={NEW_TASK_PROMPT_ID} aria-label="task" />);

    expect(await screen.findByRole('region', { name: 'Getting started' })).toHaveTextContent('2/3');
    const step = screen.getByRole('link', { name: 'Run your first task' });
    expect(step).toHaveAttribute('href', '/');
    fireEvent.click(step);
    expect(screen.getByRole('textbox', { name: 'task' })).toHaveFocus();
  });

  it('stays away for someone with a conversation, even an archived one', async () => {
    vi.mocked(listAgentConversations).mockResolvedValue({
      conversations: [conversation(true)],
      next_cursor: null,
    });

    sidebar();

    await waitFor(() => expect(listAgentConversations).toHaveBeenCalled());
    expect(screen.queryByRole('region', { name: 'Getting started' })).not.toBeInTheDocument();
    expect(getAgentConfig).not.toHaveBeenCalled();
  });

  it('is put away by Skip, says where it went, and stays away', async () => {
    newcomer();
    const view = sidebar();

    fireEvent.click(await screen.findByRole('button', { name: 'Skip' }), { detail: 1 });
    expect(screen.queryByRole('region', { name: 'Getting started' })).not.toBeInTheDocument();
    expect(screen.getByRole('status')).toHaveTextContent('Hidden. Bring it back in Settings.');

    view.unmount();
    sidebar();
    await waitFor(() => expect(listAgentConversations).toHaveBeenCalledTimes(2));
    expect(screen.queryByRole('region', { name: 'Getting started' })).not.toBeInTheDocument();
    // The note is for the moment of skipping, not for every visit after it.
    expect(screen.queryByRole('status')).not.toBeInTheDocument();
  });

  it('comes straight back on Undo', async () => {
    newcomer();
    const view = sidebar();

    fireEvent.click(await screen.findByRole('button', { name: 'Skip' }), { detail: 1 });
    fireEvent.click(screen.getByRole('button', { name: 'Undo' }), { detail: 1 });

    expect(screen.getByRole('region', { name: 'Getting started' })).toHaveTextContent('0/3');
    expect(screen.queryByRole('status')).not.toBeInTheDocument();

    view.unmount();
    sidebar();
    expect(await screen.findByRole('region', { name: 'Getting started' })).toBeInTheDocument();
  });

  it('lets the note go after a few seconds, but not while it is pointed at', async () => {
    newcomer();
    sidebar();
    const skip = await screen.findByRole('button', { name: 'Skip' });

    vi.useFakeTimers();
    try {
      fireEvent.click(skip, { detail: 1 });
      const note = screen.getByRole('status');

      fireEvent.mouseEnter(note);
      act(() => vi.advanceTimersByTime(UNDO_SKIP_MS * 2));
      expect(note).toBeInTheDocument();

      fireEvent.mouseLeave(note);
      act(() => vi.advanceTimersByTime(UNDO_SKIP_MS - 1));
      expect(note).toBeInTheDocument();
      act(() => vi.advanceTimersByTime(1));
      expect(note).not.toBeInTheDocument();
    } finally {
      vi.useRealTimers();
    }
    // Only the note has gone: the row is still put away.
    expect(screen.queryByRole('region', { name: 'Getting started' })).not.toBeInTheDocument();
  });

  it('keeps a keyboard user in place: Skip hands focus to Undo, and Undo back', async () => {
    newcomer();
    sidebar();
    const skip = await screen.findByRole('button', { name: 'Skip' });

    // A press from the keyboard carries no click count.
    skip.focus();
    fireEvent.click(skip, { detail: 0 });
    const undo = screen.getByRole('button', { name: 'Undo' });
    expect(undo).toHaveFocus();

    fireEvent.click(undo, { detail: 0 });
    expect(screen.getByRole('button', { name: 'Skip' })).toHaveFocus();
  });

  it('drops the note when the row is brought back from Settings', async () => {
    newcomer();
    function Settings() {
      const { show } = useFirstRun();
      return (
        <button type="button" onClick={show}>
          Show again
        </button>
      );
    }
    sidebar(<Settings />);

    fireEvent.click(await screen.findByRole('button', { name: 'Skip' }), { detail: 1 });
    fireEvent.click(screen.getByRole('button', { name: 'Show again' }));

    expect(screen.getByRole('region', { name: 'Getting started' })).toBeInTheDocument();
    expect(screen.queryByRole('status')).not.toBeInTheDocument();
  });

  it.each([
    ['GitHub is temporarily unavailable', { github_load_status: 'unavailable' as const }],
    ['the deployment has no GitHub App', { github_install_url: null }],
  ])('offers no step when %s', async (_case, overrides) => {
    newcomer(overrides);

    sidebar();

    await waitFor(() => expect(getAgentConfig).toHaveBeenCalled());
    expect(screen.queryByRole('region', { name: 'Getting started' })).not.toBeInTheDocument();
  });

  it('shows nothing outside the app shell', async () => {
    newcomer();

    render(<GettingStarted />);

    await new Promise((resolve) => setTimeout(resolve, 0));
    expect(screen.queryByRole('region', { name: 'Getting started' })).not.toBeInTheDocument();
    expect(listAgentConversations).not.toHaveBeenCalled();
  });
});
