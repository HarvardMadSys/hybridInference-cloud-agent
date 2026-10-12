// @vitest-environment jsdom
import '@testing-library/jest-dom/vitest';

import { act, cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { FirstRunProvider, useFirstRun } from '@/components/features/agents/firstRun';
import { LocaleProvider } from '@/components/providers/LocaleProvider';
import {
  getAgentConfig,
  listAgentConversations,
  type AgentConversationApi,
  type AgentJobApi,
} from '@/lib/api/agents';
import { GettingStartedSettings } from './GettingStartedSettings';

vi.mock('next/navigation', () => ({ usePathname: () => '/integrations' }));
vi.mock('@/lib/api/agents', () => ({
  getAgentConfig: vi.fn(),
  listAgentConversations: vi.fn(),
}));

const SKIPPED = 'agents.gettingStarted.skipped.alice';

function SidebarSkip() {
  const firstRun = useFirstRun();
  return <button onClick={firstRun.skip}>Skip guide</button>;
}

function renderSettings(conversations: number) {
  vi.mocked(listAgentConversations).mockResolvedValue({
    conversations: Array.from(
      { length: conversations },
      (_, index): AgentConversationApi => ({
        job: { id: `job_${index}`, thread_id: `thread_${index}` } as AgentJobApi,
        title: 'A task',
        archived: false,
        last_activity_at: null,
      }),
    ),
    next_cursor: null,
  });
  vi.mocked(getAgentConfig).mockResolvedValue({
    repos: [],
    runtimes: ['claude-code'],
    models: ['model-a'],
    github_connected: false,
    github_install_url: '/agents/integrations',
  });
  return render(
    <LocaleProvider>
      <FirstRunProvider userId="alice">
        <GettingStartedSettings />
        <SidebarSkip />
      </FirstRunProvider>
    </LocaleProvider>,
  );
}

beforeEach(() => {
  window.localStorage.clear();
  vi.clearAllMocks();
});
afterEach(cleanup);

describe('getting started on the Setting page', () => {
  it('restores skipped guidance and keeps confirmation in place', async () => {
    window.localStorage.setItem(SKIPPED, '1');
    renderSettings(0);

    const section = await screen.findByRole('region', { name: 'Getting started' });
    fireEvent.click(within(section).getByRole('button', { name: 'Show again' }));

    expect(within(section).getByRole('status')).toHaveTextContent('Shown');
    expect(within(section).queryByRole('button', { name: 'Show again' })).not.toBeInTheDocument();
    expect(window.localStorage.getItem(SKIPPED)).toBeNull();
  });

  it('hands keyboard focus to the confirmation when Show again disappears', async () => {
    window.localStorage.setItem(SKIPPED, '1');
    renderSettings(0);

    const show = await screen.findByRole('button', { name: 'Show again' });
    show.focus();
    fireEvent.click(show);

    expect(screen.getByRole('status')).toHaveFocus();
  });

  it('can restore the guide again after it is skipped from the sidebar', async () => {
    window.localStorage.setItem(SKIPPED, '1');
    renderSettings(0);
    fireEvent.click(await screen.findByRole('button', { name: 'Show again' }));
    expect(screen.getByRole('status')).toHaveTextContent('Shown');

    fireEvent.click(screen.getByRole('button', { name: 'Skip guide' }));
    expect(screen.queryByRole('status')).not.toBeInTheDocument();
    const show = screen.getByRole('button', { name: 'Show again' });
    show.focus();
    fireEvent.click(show);

    expect(screen.getByRole('status')).toHaveFocus();
    expect(window.localStorage.getItem(SKIPPED)).toBeNull();
  });

  it('explains the guide from the help button', async () => {
    window.localStorage.setItem(SKIPPED, '1');
    renderSettings(0);

    const help = await screen.findByRole('button', { name: 'What is getting started?' });
    expect(help).toHaveAccessibleDescription(
      'Takes you from connecting GitHub to your first task.',
    );
    expect(help).toHaveAttribute('aria-expanded', 'false');
    fireEvent.click(help);
    expect(help).toHaveAttribute('aria-expanded', 'true');
  });

  it.each([
    ['a newcomer who has not skipped it', 0, false],
    ['someone with a conversation, even with Skip remembered', 1, true],
  ])('is not offered to %s', async (_case, conversations, skipped) => {
    if (skipped) window.localStorage.setItem(SKIPPED, '1');
    renderSettings(conversations);

    await waitFor(() => expect(listAgentConversations).toHaveBeenCalled());
    await act(() => new Promise((resolve) => setTimeout(resolve, 0)));
    expect(screen.queryByRole('region', { name: 'Getting started' })).not.toBeInTheDocument();
  });
});
