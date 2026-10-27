import { fireEvent, within } from '@testing-library/react';

/** Open a shared Select's menu and return its listbox. */
export function openSelect(trigger: HTMLElement): HTMLElement {
  if (trigger.getAttribute('aria-expanded') !== 'true') fireEvent.click(trigger);
  const listbox = document.getElementById(trigger.getAttribute('aria-controls') ?? '');
  if (!listbox) throw new Error('The select did not open a listbox.');
  return listbox;
}

/** Choose an option of a shared Select by its name, the way a pointer user does. */
export function chooseOption(trigger: HTMLElement, name: string | RegExp): void {
  fireEvent.click(within(openSelect(trigger)).getByRole('option', { name }));
}

/** The option names a shared Select offers, read from its menu, which is closed again. */
export function optionNames(trigger: HTMLElement): string[] {
  const listbox = openSelect(trigger);
  const names = within(listbox)
    .queryAllByRole('option')
    .map((option) => option.textContent ?? '');
  fireEvent.keyDown(listbox, { key: 'Escape' });
  return names;
}
