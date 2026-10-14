// @vitest-environment jsdom
import '@testing-library/jest-dom/vitest';

import { useState } from 'react';
import { cleanup, fireEvent, render, screen, within } from '@testing-library/react';
import { afterEach, expect, it } from 'vitest';
import { WorkspaceTabBar } from './WorkspaceTabBar';
import type { SideChatTab, WorkspaceTab } from './useWorkspaceTabs';

afterEach(cleanup);

function WorkspaceTabs({ initialChats = [] }: { initialChats?: SideChatTab[] }) {
  const [selected, setSelected] = useState<WorkspaceTab>('git');
  const [chats, setChats] = useState(initialChats);
  return (
    <>
      <WorkspaceTabBar
        selected={selected}
        chats={chats}
        diffCount={3}
        onSelect={setSelected}
        onAdd={() => {
          setChats([{ id: 'side-chat', number: 1 }]);
          setSelected('side-chat');
          return 'side-chat';
        }}
        onClose={(id) => {
          setChats((current) => current.filter((chat) => chat.id !== id));
          setSelected('git');
          return 'git';
        }}
      />
      <div id={`workspace-panel-${selected}`} role="tabpanel" />
    </>
  );
}

it('keeps one tab stop and moves selection with arrows, Home, and End', () => {
  render(<WorkspaceTabs />);
  const tabs = within(screen.getByRole('tablist', { name: 'Workspace views' })).getAllByRole('tab');
  tabs[0].focus();
  expect(tabs.filter((tab) => tab.tabIndex === 0)).toEqual([tabs[0]]);
  expect(tabs[0]).toHaveAttribute('aria-controls', screen.getByRole('tabpanel').id);

  fireEvent.keyDown(tabs[0], { key: 'ArrowLeft' });
  expect(tabs[3]).toHaveFocus();
  expect(tabs[3]).toHaveAttribute('aria-selected', 'true');
  expect(tabs.filter((tab) => tab.tabIndex === 0)).toEqual([tabs[3]]);

  fireEvent.keyDown(tabs[3], { key: 'ArrowRight' });
  expect(tabs[0]).toHaveFocus();
  fireEvent.keyDown(tabs[0], { key: 'End' });
  expect(tabs[3]).toHaveFocus();
  fireEvent.keyDown(tabs[3], { key: 'Home' });
  expect(tabs[0]).toHaveFocus();
});

it('focuses a newly opened chat and returns focus to a surviving tab when closed', () => {
  render(<WorkspaceTabs />);
  fireEvent.click(screen.getByRole('button', { name: /New side chat/i }));
  const chat = screen.getByRole('tab', { name: /Side chat/i });
  expect(chat).toHaveFocus();
  expect(chat).toHaveAttribute('aria-selected', 'true');
  expect(chat).toHaveAttribute('aria-controls', screen.getByRole('tabpanel').id);

  fireEvent.click(screen.getByRole('button', { name: /Close Side chat/i }));
  expect(screen.queryByRole('tab', { name: /Side chat/i })).not.toBeInTheDocument();
  expect(screen.getByRole('tab', { name: /Git/ })).toHaveFocus();
});

it('always offers the environment entry without a feature flag', () => {
  render(
    <WorkspaceTabBar
      selected="git"
      chats={[]}
      diffCount={0}
      onSelect={() => {}}
      onAdd={() => 'git'}
      onClose={() => 'git'}
    />,
  );
  expect(screen.getByRole('tab', { name: 'Environment' })).toBeInTheDocument();
  expect(screen.getAllByRole('tab')).toHaveLength(4);
});
