// @vitest-environment jsdom
import '@testing-library/jest-dom/vitest';

import { cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';

import { ComposerPlusMenu } from './ComposerPlusMenu';

// The menu is the composer's one attach point, and the goal row is its own
// group inside it: what this message carries, next to Skills and MCP.

afterEach(cleanup);

function renderMenu(props: Partial<Parameters<typeof ComposerPlusMenu>[0]> = {}) {
  return render(
    <ComposerPlusMenu
      onOpenSkills={() => {}}
      skillSummary="2"
      goalSummary="Keep going until…"
      onOpenGoal={() => {}}
      {...props}
    />,
  );
}

function open() {
  fireEvent.click(screen.getByRole('button', { name: 'Add files or skills' }));
  return screen.getByRole('menu');
}

describe('ComposerPlusMenu', () => {
  it('shows the goal this message would carry', () => {
    renderMenu({ goalSummary: 'make `pytest` pass' });
    expect(
      within(open()).getByRole('menuitem', { name: /Completion condition/ }),
    ).toHaveTextContent('make `pytest` pass');
  });

  it('reads as an invitation when nothing is set', () => {
    renderMenu();
    expect(
      within(open()).getByRole('menuitem', { name: /Completion condition/ }),
    ).toHaveTextContent('Keep going until…');
  });

  it('hands the goal row to the caller rather than opening anything itself', () => {
    const onOpenGoal = vi.fn();
    renderMenu({ onOpenGoal });
    fireEvent.click(within(open()).getByRole('menuitem', { name: /Completion condition/ }));
    expect(onOpenGoal).toHaveBeenCalledTimes(1);
  });

  it('opens on arrow-down from the trigger and returns focus on Escape', async () => {
    renderMenu();
    const trigger = screen.getByRole('button', { name: 'Add files or skills' });
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
    expect(menu.getByRole('menuitem', { name: /Skills/ })).toBeInTheDocument();
    expect(menu.getByRole('menuitem', { name: /Completion condition/ })).toBeInTheDocument();
  });

  it('hands the skills row to the caller too', () => {
    const onOpenSkills = vi.fn();
    renderMenu({ onOpenSkills });
    fireEvent.click(within(open()).getByRole('menuitem', { name: /Skills/ }));
    expect(onOpenSkills).toHaveBeenCalledTimes(1);
  });
});
