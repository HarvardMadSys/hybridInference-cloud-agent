// @vitest-environment jsdom
import '@testing-library/jest-dom/vitest';

import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import {
  getAgentConfig,
  listAgentConversations,
  type AgentConfigApi,
  type AgentConversationApi,
  type AgentJobApi,
} from '@/lib/api/agents';

import { FirstRunProvider, useFirstRun } from './firstRun';

const nav = vi.hoisted(() => ({ pathname: '/' }));

vi.mock('next/navigation', () => ({ usePathname: () => nav.pathname }));

vi.mock('@/lib/api/agents', () => ({
  getAgentConfig: vi.fn(),
  listAgentConversations: vi.fn(),
}));

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

function conversation(threadId: string): AgentConversationApi {
  return {
    job: { id: `job-${threadId}`, thread_id: threadId } as AgentJobApi,
    title: 'A task',
    archived: false,
    last_activity_at: null,
  };
}

function page(conversations: AgentConversationApi[]) {
  return { conversations, next_cursor: null };
}

// What each reader of the answer sees, laid out so a test can compare them.
function Reader({ name }: { name: string }) {
  const { newcomer, onlyThreadId, step } = useFirstRun();
  return <p data-testid={name}>{JSON.stringify({ newcomer, onlyThreadId, step })}</p>;
}

function app() {
  return (
    <FirstRunProvider userId="alice">
      <Reader name="sidebar" />
      <Reader name="page" />
    </FirstRunProvider>
  );
}

// The sidebar's Skip and the Settings row that undoes it.
function Switch() {
  const { hidden, step, skip, show } = useFirstRun();
  return (
    <>
      <p data-testid="row">{JSON.stringify({ hidden, next: step?.next ?? null })}</p>
      <button type="button" onClick={skip}>
        Skip
      </button>
      <button type="button" onClick={show}>
        Show again
      </button>
    </>
  );
}

function withSwitch() {
  return (
    <FirstRunProvider userId="alice">
      <Reader name="page" />
      <Switch />
    </FirstRunProvider>
  );
}

describe('FirstRunProvider', () => {
  beforeEach(() => {
    localStorage.clear();
    nav.pathname = '/';
    vi.mocked(listAgentConversations).mockReset();
    vi.mocked(getAgentConfig).mockReset();
    vi.mocked(getAgentConfig).mockResolvedValue(config());
  });

  afterEach(() => cleanup());

  it('asks once, and gives every reader the same answer', async () => {
    vi.mocked(listAgentConversations).mockResolvedValue(page([]));

    render(app());

    // The step waits on a second answer, `/config`, asked only of a newcomer.
    await waitFor(() =>
      expect(screen.getByTestId('sidebar')).toHaveTextContent('"next":"connect","done":0'),
    );
    expect(screen.getByTestId('sidebar')).toHaveTextContent('"newcomer":true');
    expect(screen.getByTestId('page')).toHaveTextContent(
      screen.getByTestId('sidebar').textContent ?? '',
    );
    // Archived conversations count: putting one away does not make its owner new.
    expect(listAgentConversations).toHaveBeenCalledTimes(1);
    expect(listAgentConversations).toHaveBeenCalledWith({ includeArchived: true, limit: 2 });
  });

  it('asks again on each page change, until the owner has two conversations', async () => {
    vi.mocked(listAgentConversations)
      .mockResolvedValueOnce(page([]))
      .mockResolvedValueOnce(page([conversation('thread_1')]))
      .mockResolvedValue(page([conversation('thread_2'), conversation('thread_1')]));
    const view = render(app());
    await waitFor(() => expect(screen.getByTestId('page')).toHaveTextContent('"newcomer":true'));

    // The first task is created on the new-task page and explained on its own.
    nav.pathname = '/job-thread_1';
    view.rerender(app());
    await waitFor(() =>
      expect(screen.getByTestId('page')).toHaveTextContent('"onlyThreadId":"thread_1"'),
    );
    expect(screen.getByTestId('sidebar')).toHaveTextContent('"step":null');

    nav.pathname = '/';
    view.rerender(app());
    await waitFor(() =>
      expect(screen.getByTestId('page')).toHaveTextContent('"onlyThreadId":null'),
    );
    expect(listAgentConversations).toHaveBeenCalledTimes(3);

    // Two settle it: nothing anyone is told depends on the answer any more.
    nav.pathname = '/job-thread_2';
    view.rerender(app());
    await new Promise((resolve) => setTimeout(resolve, 0));
    expect(listAgentConversations).toHaveBeenCalledTimes(3);
  });

  it('asks nothing more of an owner who has a conversation', async () => {
    vi.mocked(listAgentConversations).mockResolvedValue(page([conversation('thread_1')]));

    render(app());

    await waitFor(() =>
      expect(screen.getByTestId('page')).toHaveTextContent('"onlyThreadId":"thread_1"'),
    );
    expect(screen.getByTestId('page')).toHaveTextContent('"newcomer":false');
    expect(getAgentConfig).not.toHaveBeenCalled();
  });

  it('keeps the answer it has when asking again fails', async () => {
    vi.mocked(listAgentConversations)
      .mockResolvedValueOnce(page([conversation('thread_1')]))
      .mockRejectedValue(new Error('offline'));
    const view = render(app());
    await waitFor(() =>
      expect(screen.getByTestId('page')).toHaveTextContent('"onlyThreadId":"thread_1"'),
    );

    nav.pathname = '/somewhere';
    view.rerender(app());
    await waitFor(() => expect(listAgentConversations).toHaveBeenCalledTimes(2));
    expect(screen.getByTestId('page')).toHaveTextContent('"onlyThreadId":"thread_1"');
  });

  it('remembers Skip for the account, and brings the row back on Show again', async () => {
    vi.mocked(listAgentConversations).mockResolvedValue(page([]));
    const view = render(withSwitch());
    await waitFor(() =>
      expect(screen.getByTestId('row')).toHaveTextContent('{"hidden":false,"next":"connect"}'),
    );

    fireEvent.click(screen.getByRole('button', { name: 'Skip' }));
    expect(screen.getByTestId('row')).toHaveTextContent('{"hidden":true,"next":null}');

    // Another visit in this browser: still put away, and Settings can say so.
    view.unmount();
    render(withSwitch());
    await waitFor(() =>
      expect(screen.getByTestId('row')).toHaveTextContent('{"hidden":true,"next":null}'),
    );

    fireEvent.click(screen.getByRole('button', { name: 'Show again' }));
    await waitFor(() =>
      expect(screen.getByTestId('row')).toHaveTextContent('{"hidden":false,"next":"connect"}'),
    );
    expect(localStorage.getItem('agents.gettingStarted.skipped.alice')).toBeNull();
  });

  it('has nothing hidden from someone who is no longer new', async () => {
    localStorage.setItem('agents.gettingStarted.skipped.alice', '1');
    vi.mocked(listAgentConversations).mockResolvedValue(page([conversation('thread_1')]));

    render(withSwitch());

    await waitFor(() =>
      expect(screen.getByTestId('page')).toHaveTextContent('"onlyThreadId":"thread_1"'),
    );
    expect(screen.getByTestId('row')).toHaveTextContent('{"hidden":false,"next":null}');
  });

  it('tells nothing to anyone while the first answer is out', async () => {
    vi.mocked(listAgentConversations).mockReturnValue(new Promise(() => {}));

    render(app());

    await new Promise((resolve) => setTimeout(resolve, 0));
    expect(screen.getByTestId('page')).toHaveTextContent(
      '{"newcomer":false,"onlyThreadId":null,"step":null}',
    );
  });
});
