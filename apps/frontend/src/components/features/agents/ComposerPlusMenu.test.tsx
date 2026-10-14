// @vitest-environment jsdom
import '@testing-library/jest-dom/vitest';

import { cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';

import { ComposerPlusMenu } from './ComposerPlusMenu';

// Files and Goal share the Add group; Skills and MCP keep their own tool actions.

afterEach(cleanup);

function renderMenu(props: Partial<Parameters<typeof ComposerPlusMenu>[0]> = {}) {
  return render(
    <ComposerPlusMenu
      onOpenSkills={() => {}}
      skillSummary="2"
      goalSummary="Set or view the goal for a long-running task"
      onOpenGoal={() => {}}
      {...props}
    />,
  );
}

function open() {
  fireEvent.click(screen.getByRole('button', { name: 'Add to message' }));
  return screen.getByRole('menu');
}

describe('ComposerPlusMenu', () => {
  it('shows the goal this message would carry', () => {
    renderMenu({ goalSummary: 'make `pytest` pass' });
    expect(within(open()).getByRole('menuitem', { name: /Goal/ })).toHaveTextContent(
      'make `pytest` pass',
    );
  });

  it('hands the goal row to the caller rather than opening anything itself', () => {
    const onOpenGoal = vi.fn();
    renderMenu({ onOpenGoal });
    fireEvent.click(within(open()).getByRole('menuitem', { name: /Goal/ }));
    expect(onOpenGoal).toHaveBeenCalledTimes(1);
  });

  it('opens on arrow-down from the trigger and returns focus on Escape', async () => {
    renderMenu();
    const trigger = screen.getByRole('button', { name: 'Add to message' });
    fireEvent.keyDown(trigger, { key: 'ArrowDown' });
    expect(trigger).toHaveAttribute('aria-expanded', 'true');
    fireEvent.keyDown(screen.getByRole('menu'), { key: 'Escape' });
    await waitFor(() => expect(trigger).toHaveAttribute('aria-expanded', 'false'));
    expect(trigger).toHaveFocus();
  });

  it('omits the groups a caller has nothing for', () => {
    // The job view's follow-up offers no uploads and no MCP connections; the
    // menu shows what it does have instead of growing a second one beside it.
    renderMenu();
    const menu = within(open());
    expect(menu.queryByRole('menuitem', { name: /Upload files/ })).not.toBeInTheDocument();
    expect(menu.queryByRole('menuitem', { name: /MCP servers/ })).not.toBeInTheDocument();
    expect(menu.queryByRole('menuitem', { name: /Plugins/ })).not.toBeInTheDocument();
    expect(menu.queryByRole('menuitem', { name: /Environment/ })).not.toBeInTheDocument();
    expect(menu.getByRole('menuitem', { name: /Skills/ })).toBeInTheDocument();
    expect(menu.getByRole('menuitem', { name: /Goal/ })).toBeInTheDocument();
  });

  it('hands the skills row to the caller too', () => {
    const onOpenSkills = vi.fn();
    renderMenu({ onOpenSkills });
    fireEvent.click(within(open()).getByRole('menuitem', { name: /Skills/ }));
    expect(onOpenSkills).toHaveBeenCalledTimes(1);
  });

  it('offers plugins only when the caller provides a plugin action', () => {
    const onOpenPlugins = vi.fn();
    renderMenu({ onOpenPlugins, pluginSummary: '2' });
    const item = within(open()).getByRole('menuitem', { name: /Plugins/ });
    expect(item).toHaveTextContent('2');
    fireEvent.click(item);
    expect(onOpenPlugins).toHaveBeenCalledTimes(1);
    expect(screen.queryByRole('menu')).not.toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Add to message' })).toHaveFocus();
  });

  it('opens environment settings from the workspace group and keeps the opener focused', () => {
    const onOpenEnvironment = vi.fn();
    renderMenu({ onOpenEnvironment, environmentSummary: 'Development' });
    const item = within(open()).getByRole('menuitem', { name: /Environment/ });
    expect(item).toHaveTextContent('Development');
    expect(item).toHaveAttribute('aria-haspopup', 'dialog');
    fireEvent.click(item);
    expect(onOpenEnvironment).toHaveBeenCalledTimes(1);
    expect(screen.queryByRole('menu')).not.toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Add to message' })).toHaveFocus();
  });

  it('navigates the grouped rows without stopping on headings', () => {
    renderMenu({ onFiles: vi.fn() });
    const trigger = screen.getByRole('button', { name: 'Add to message' });
    fireEvent.keyDown(trigger, { key: 'ArrowUp' });
    expect(screen.getByRole('menuitem', { name: /Skills/ })).toHaveFocus();
    fireEvent.keyDown(screen.getByRole('menu'), { key: 'Home' });
    expect(screen.getByRole('menuitem', { name: 'Upload files' })).toHaveFocus();
    fireEvent.keyDown(screen.getByRole('menu'), { key: 'ArrowDown' });
    expect(screen.getByRole('menuitem', { name: /Goal/ })).toHaveFocus();
    fireEvent.keyDown(screen.getByRole('menu'), { key: 'End' });
    expect(screen.getByRole('menuitem', { name: /Skills/ })).toHaveFocus();
  });

  it('keeps MCP navigation inside the panel and resets it after dismissal', () => {
    renderMenu({
      mcp: { choices: [], loading: false, error: null, count: 0, reload: vi.fn(), toggle: vi.fn() },
    });
    fireEvent.click(within(open()).getByRole('menuitem', { name: 'MCP servers' }));
    const panel = screen.getByRole('dialog', { name: 'MCP servers' });
    expect(screen.queryByRole('menu')).not.toBeInTheDocument();
    const search = within(panel).getByRole('searchbox');
    expect(search).toHaveFocus();
    fireEvent.keyDown(search, { key: 'Escape' });
    expect(screen.queryByRole('dialog')).not.toBeInTheDocument();
    expect(screen.getByRole('menuitem', { name: 'MCP servers' })).toHaveFocus();

    fireEvent.click(screen.getByRole('menuitem', { name: 'MCP servers' }));
    fireEvent.pointerDown(document.body);
    expect(screen.queryByRole('dialog')).not.toBeInTheDocument();
    expect(within(open()).getByRole('menuitem', { name: /Goal/ })).toHaveFocus();
    fireEvent.keyDown(screen.getByRole('menu'), { key: 'Escape' });
    expect(screen.getByRole('button', { name: 'Add to message' })).toHaveFocus();
  });
});
