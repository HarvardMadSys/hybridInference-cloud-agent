// @vitest-environment jsdom
import '@testing-library/jest-dom/vitest';

import { cleanup, fireEvent, render, screen } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';

import { FieldActionsMenu } from './ApplicationSettingGroup';

afterEach(cleanup);

function renderMenu(overrides: Partial<Parameters<typeof FieldActionsMenu>[0]> = {}) {
  const onAction = vi.fn();
  render(
    <FieldActionsMenu
      label="More actions for Product name"
      actionLabel="Restore default for Product name"
      actionText="Restore default…"
      disabled={false}
      onAction={onAction}
      {...overrides}
    />,
  );
  return { onAction };
}

describe('FieldActionsMenu', () => {
  it('opens from the trigger with ArrowDown and closes on Escape with focus restored', () => {
    renderMenu();
    const trigger = screen.getByLabelText('More actions for Product name');
    fireEvent.keyDown(trigger, { key: 'ArrowDown' });
    const menu = screen.getByRole('menu', { name: 'More actions for Product name' });
    expect(menu).toBeInTheDocument();
    const action = screen.getByRole('menuitem', { name: 'Restore default for Product name' });
    expect(action).toHaveFocus();
    expect(action).toHaveTextContent('Restore default…');

    fireEvent.keyDown(menu, { key: 'Escape' });
    expect(screen.queryByRole('menu')).not.toBeInTheDocument();
    expect(trigger).toHaveFocus();
  });

  it('hands the trigger to the confirmation flow and returns focus', () => {
    const { onAction } = renderMenu();
    const trigger = screen.getByLabelText('More actions for Product name');
    fireEvent.click(trigger);
    fireEvent.click(screen.getByRole('menuitem', { name: 'Restore default for Product name' }));
    expect(onAction).toHaveBeenCalledWith(trigger);
    expect(screen.queryByRole('menu')).not.toBeInTheDocument();
    expect(trigger).toHaveFocus();
  });

  it('keeps a disabled action inert and visually unavailable', () => {
    renderMenu({ disabled: true });
    const trigger = screen.getByLabelText('More actions for Product name');
    expect(trigger).toBeDisabled();
    fireEvent.click(trigger);
    expect(screen.queryByRole('menu')).not.toBeInTheDocument();
  });
});
