// @vitest-environment jsdom
import '@testing-library/jest-dom/vitest';

import { act, cleanup, fireEvent, render, screen, within } from '@testing-library/react';
import { useRef, useState, type ReactNode } from 'react';
import { createPortal } from 'react-dom';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { Select, type SelectItem } from './Select';
import { useModalDialog } from './useModalDialog';

const FRUIT: SelectItem[] = [
  { value: '', label: 'Any fruit' },
  { value: 'apple', label: 'Apple' },
  { value: 'apricot', label: 'Apricot' },
  { value: 'banana', label: 'Banana', disabled: true },
  { value: 'cherry', label: 'Cherry' },
];

function Controlled({
  initial = '',
  options = FRUIT,
  onValueChange = vi.fn(),
  ...rest
}: {
  initial?: string;
  options?: SelectItem[];
  onValueChange?: (value: string) => void;
  searchable?: boolean;
  disabled?: boolean;
  required?: boolean;
  placeholder?: string;
}) {
  const [value, setValue] = useState(initial);
  return (
    <>
      <label htmlFor="fruit">Fruit</label>
      <Select
        id="fruit"
        value={value}
        options={options}
        onValueChange={(next) => {
          setValue(next);
          onValueChange(next);
        }}
        {...rest}
      />
      <button type="button">After</button>
    </>
  );
}

function Modal({ onClose, children }: { onClose: () => void; children: ReactNode }) {
  const panel = useRef<HTMLDivElement>(null);
  useModalDialog(panel, { onClose });
  return createPortal(
    <div>
      <div ref={panel} role="dialog" aria-label="Edit" aria-modal="true">
        <button type="button">First</button>
        {children}
        <button type="button">Last</button>
      </div>
    </div>,
    document.body,
  );
}

const TABLES = Array.from({ length: 14 }, (_, index) => ({
  value: `table_${String(index).padStart(2, '0')}`,
  label: `table_${String(index).padStart(2, '0')}`,
}));

afterEach(() => {
  cleanup();
  document.body.style.overflow = '';
});

describe('Select', () => {
  it('is labelled by its field label and shows the chosen option', () => {
    render(<Controlled initial="apple" />);
    const trigger = screen.getByRole('combobox', { name: 'Fruit' });
    expect(trigger).toHaveTextContent('Apple');
    expect(trigger).toHaveAttribute('aria-expanded', 'false');
    expect(trigger).toHaveAttribute('aria-haspopup', 'listbox');
  });

  it('opens a themed listbox that marks the selection and reports a clean value', () => {
    const onValueChange = vi.fn();
    render(<Controlled initial="apple" onValueChange={onValueChange} />);
    const trigger = screen.getByRole('combobox', { name: 'Fruit' });
    fireEvent.click(trigger);

    const listbox = screen.getByRole('listbox', { name: 'Fruit' });
    expect(trigger).toHaveAttribute('aria-expanded', 'true');
    expect(trigger).toHaveAttribute('aria-controls', listbox.id);
    expect(listbox).toHaveFocus();
    expect(within(listbox).getByRole('option', { name: 'Apple' })).toHaveAttribute(
      'aria-selected',
      'true',
    );
    expect(within(listbox).getByRole('option', { name: 'Banana' })).toHaveAttribute(
      'aria-disabled',
      'true',
    );
    expect(document.querySelector('select')).toBeNull();

    fireEvent.click(within(listbox).getByRole('option', { name: 'Banana' }));
    expect(onValueChange).not.toHaveBeenCalled();
    expect(screen.getByRole('listbox')).toBeInTheDocument();

    fireEvent.click(within(listbox).getByRole('option', { name: 'Cherry' }));
    expect(onValueChange).toHaveBeenCalledWith('cherry');
    expect(screen.queryByRole('listbox')).not.toBeInTheDocument();
    expect(trigger).toHaveTextContent('Cherry');
    expect(trigger).toHaveFocus();
  });

  it('selects the empty-string option and does not report an unchanged value', () => {
    const onValueChange = vi.fn();
    render(<Controlled initial="cherry" onValueChange={onValueChange} />);
    const trigger = screen.getByRole('combobox', { name: 'Fruit' });
    fireEvent.click(trigger);
    fireEvent.click(screen.getByRole('option', { name: 'Cherry' }));
    expect(onValueChange).not.toHaveBeenCalled();

    fireEvent.click(trigger);
    fireEvent.click(screen.getByRole('option', { name: 'Any fruit' }));
    expect(onValueChange).toHaveBeenCalledWith('');
    expect(trigger).toHaveTextContent('Any fruit');
  });

  it('navigates with arrows, Home, End and typeahead, skipping disabled options', () => {
    const onValueChange = vi.fn();
    render(<Controlled initial="apple" onValueChange={onValueChange} />);
    const trigger = screen.getByRole('combobox', { name: 'Fruit' });
    trigger.focus();
    fireEvent.keyDown(trigger, { key: 'ArrowDown' });
    const listbox = screen.getByRole('listbox');
    const active = () => document.getElementById(listbox.getAttribute('aria-activedescendant')!);
    expect(active()).toHaveTextContent('Apple');

    fireEvent.keyDown(listbox, { key: 'ArrowDown' });
    expect(active()).toHaveTextContent('Apricot');
    fireEvent.keyDown(listbox, { key: 'ArrowDown' });
    expect(active()).toHaveTextContent('Cherry');
    fireEvent.keyDown(listbox, { key: 'ArrowDown' });
    expect(active()).toHaveTextContent('Cherry');
    fireEvent.keyDown(listbox, { key: 'Home' });
    expect(active()).toHaveTextContent('Any fruit');
    fireEvent.keyDown(listbox, { key: 'End' });
    expect(active()).toHaveTextContent('Cherry');
    fireEvent.keyDown(listbox, { key: 'a' });
    expect(active()).toHaveTextContent('Any fruit');
    fireEvent.keyDown(listbox, { key: 'a' });
    expect(active()).toHaveTextContent('Apple');
    fireEvent.keyDown(listbox, { key: 'a' });
    expect(active()).toHaveTextContent('Apricot');

    fireEvent.keyDown(listbox, { key: 'Enter' });
    expect(onValueChange).toHaveBeenCalledWith('apricot');
    expect(trigger).toHaveFocus();

    fireEvent.keyDown(trigger, { key: 'End' });
    fireEvent.keyDown(screen.getByRole('listbox'), { key: ' ' });
    expect(onValueChange).toHaveBeenLastCalledWith('cherry');
  });

  it('opens from typing on the closed trigger at the matching option', () => {
    render(<Controlled initial="apple" />);
    const trigger = screen.getByRole('combobox', { name: 'Fruit' });
    fireEvent.keyDown(trigger, { key: 'c' });
    const listbox = screen.getByRole('listbox');
    expect(
      document.getElementById(listbox.getAttribute('aria-activedescendant')!),
    ).toHaveTextContent('Cherry');
  });

  it('dismisses on Escape and Tab without changing the value', () => {
    const onValueChange = vi.fn();
    render(<Controlled initial="apple" onValueChange={onValueChange} />);
    const trigger = screen.getByRole('combobox', { name: 'Fruit' });
    fireEvent.click(trigger);
    fireEvent.keyDown(screen.getByRole('listbox'), { key: 'ArrowDown' });
    const escape = new KeyboardEvent('keydown', { key: 'Escape', bubbles: true, cancelable: true });
    const parent = vi.fn();
    window.addEventListener('keydown', parent);
    act(() => {
      screen.getByRole('listbox').dispatchEvent(escape);
    });
    window.removeEventListener('keydown', parent);
    expect(escape.defaultPrevented).toBe(true);
    expect(parent).not.toHaveBeenCalled();
    expect(screen.queryByRole('listbox')).not.toBeInTheDocument();
    expect(trigger).toHaveFocus();

    fireEvent.click(trigger);
    fireEvent.keyDown(screen.getByRole('listbox'), { key: 'ArrowDown' });
    fireEvent.keyDown(screen.getByRole('listbox'), { key: 'Tab' });
    expect(screen.queryByRole('listbox')).not.toBeInTheDocument();
    // The browser's own Tab then moves on from the trigger.
    expect(trigger).toHaveFocus();
    expect(onValueChange).not.toHaveBeenCalled();
  });

  it('closes when focus or a pointer goes elsewhere, but toggles from its own label', () => {
    render(<Controlled initial="apple" />);
    const trigger = screen.getByRole('combobox', { name: 'Fruit' });
    fireEvent.click(trigger);
    fireEvent.pointerDown(screen.getByText('Fruit'));
    expect(screen.getByRole('listbox')).toBeInTheDocument();
    fireEvent.click(screen.getByText('Fruit'));
    expect(screen.queryByRole('listbox')).not.toBeInTheDocument();

    fireEvent.click(screen.getByText('Fruit'));
    expect(screen.getByRole('listbox')).toBeInTheDocument();
    fireEvent.pointerDown(document.body);
    expect(screen.queryByRole('listbox')).not.toBeInTheDocument();

    fireEvent.click(trigger);
    act(() => screen.getByRole('button', { name: 'After' }).focus());
    expect(screen.queryByRole('listbox')).not.toBeInTheDocument();
  });

  it('searches long menus and keeps short ones free of a search field', () => {
    const { unmount } = render(<Controlled initial="apple" />);
    fireEvent.click(screen.getByRole('combobox', { name: 'Fruit' }));
    expect(screen.queryByRole('combobox', { name: 'Search…' })).not.toBeInTheDocument();
    unmount();

    const onValueChange = vi.fn();
    render(<Controlled initial="table_03" options={TABLES} onValueChange={onValueChange} />);
    fireEvent.click(screen.getByRole('combobox', { name: 'Fruit' }));
    const search = screen.getByRole('combobox', { name: 'Search…' });
    expect(search).toHaveFocus();
    expect(screen.getAllByRole('option')).toHaveLength(14);

    fireEvent.change(search, { target: { value: '1' } });
    expect(screen.getAllByRole('option').map((option) => option.textContent)).toEqual([
      'table_01',
      'table_10',
      'table_11',
      'table_12',
      'table_13',
    ]);
    fireEvent.keyDown(search, { key: 'End' });
    fireEvent.keyDown(search, { key: 'ArrowUp' });
    expect(
      document.getElementById(search.getAttribute('aria-activedescendant')!),
    ).toHaveTextContent('table_12');
    fireEvent.keyDown(search, { key: 'Enter' });
    expect(onValueChange).toHaveBeenCalledWith('table_12');

    fireEvent.click(screen.getByRole('combobox', { name: 'Fruit' }));
    fireEvent.change(screen.getByRole('combobox', { name: 'Search…' }), {
      target: { value: 'zzz' },
    });
    expect(screen.getByRole('status')).toHaveTextContent('No matching options');
    fireEvent.keyDown(screen.getByRole('combobox', { name: 'Search…' }), { key: 'Enter' });
    expect(onValueChange).toHaveBeenCalledTimes(1);
  });

  it('starts a search from a letter typed on the closed trigger', () => {
    render(<Controlled initial="table_03" options={TABLES} />);
    fireEvent.keyDown(screen.getByRole('combobox', { name: 'Fruit' }), { key: '9' });
    expect(screen.getByRole('combobox', { name: 'Search…' })).toHaveValue('9');
    expect(screen.getAllByRole('option')).toHaveLength(1);
  });

  it('renders option groups with their headings', () => {
    render(
      <Controlled
        initial="b"
        options={[
          { value: '', label: 'Default' },
          { label: 'On servers', options: [{ value: 'a', label: '1.0.0' }] },
          { label: 'From npm', options: [{ value: 'b', label: '2.0.0' }] },
        ]}
      />,
    );
    fireEvent.click(screen.getByRole('combobox', { name: 'Fruit' }));
    const group = screen.getByRole('group', { name: 'From npm' });
    expect(within(group).getByRole('option', { name: '2.0.0' })).toHaveAttribute(
      'aria-selected',
      'true',
    );
  });

  it('shows a placeholder for an unmatched value and cannot open while disabled', () => {
    const { rerender } = render(<Controlled initial="gone" placeholder="Choose…" />);
    const trigger = screen.getByRole('combobox', { name: 'Fruit' });
    expect(trigger).toHaveTextContent('Choose…');
    fireEvent.click(trigger);
    expect(screen.getByRole('listbox')).toBeInTheDocument();

    rerender(<Controlled initial="gone" placeholder="Choose…" disabled />);
    expect(screen.queryByRole('listbox')).not.toBeInTheDocument();
    expect(trigger).toBeDisabled();
    fireEvent.click(trigger);
    fireEvent.keyDown(trigger, { key: 'ArrowDown' });
    expect(screen.queryByRole('listbox')).not.toBeInTheDocument();
  });

  it('takes part in native form validation when required', () => {
    const submit = vi.fn((event: { preventDefault: () => void }) => event.preventDefault());
    function Form() {
      const [value, setValue] = useState('');
      return (
        <form onSubmit={submit}>
          <Select
            aria-label="Key"
            required
            value={value}
            onValueChange={setValue}
            options={[
              { value: '', label: 'Select…' },
              { value: 'k1', label: 'Key one' },
            ]}
          />
          <button type="submit">Save</button>
        </form>
      );
    }
    render(<Form />);
    const trigger = screen.getByRole('combobox', { name: 'Key' });
    expect(trigger).toBeRequired();
    const form = trigger.closest('form')!;
    expect(form.checkValidity()).toBe(false);
    fireEvent.click(trigger);
    fireEvent.click(screen.getByRole('option', { name: 'Key one' }));
    expect(form.checkValidity()).toBe(true);
  });

  it('forwards its ref to the trigger', () => {
    function Focusing() {
      const ref = useRef<HTMLButtonElement>(null);
      return (
        <>
          <Select ref={ref} aria-label="Fruit" value="" onValueChange={() => {}} options={FRUIT} />
          <button type="button" onClick={() => ref.current?.focus()}>
            Focus it
          </button>
        </>
      );
    }
    render(<Focusing />);
    fireEvent.click(screen.getByRole('button', { name: 'Focus it' }));
    expect(screen.getByRole('combobox', { name: 'Fruit' })).toHaveFocus();
  });
});

describe('Select inside a modal dialog', () => {
  function Dialog({
    onClose,
    onValueChange,
    options = FRUIT,
  }: {
    onClose: () => void;
    onValueChange: (value: string) => void;
    options?: SelectItem[];
  }) {
    const [value, setValue] = useState('apple');
    return (
      <Modal onClose={onClose}>
        <label>
          Fruit
          <Select
            value={value}
            options={options}
            onValueChange={(next) => {
              setValue(next);
              onValueChange(next);
            }}
          />
        </label>
      </Modal>
    );
  }

  it('keeps the menu inside the panel so it is not inert, and selects from it', () => {
    const onValueChange = vi.fn();
    const onClose = vi.fn();
    render(
      <>
        <button type="button">Background</button>
        <Dialog onClose={onClose} onValueChange={onValueChange} />
      </>,
    );
    const dialog = screen.getByRole('dialog', { name: 'Edit' });
    const trigger = within(dialog).getByRole('combobox', { name: 'Fruit' });
    fireEvent.click(trigger);

    const listbox = screen.getByRole('listbox', { name: /Fruit/ });
    expect(dialog).toContainElement(listbox);
    expect(listbox.closest('[inert]')).toBeNull();
    expect(listbox).toHaveFocus();

    fireEvent.keyDown(listbox, { key: 'ArrowDown' });
    fireEvent.keyDown(listbox, { key: 'Enter' });
    expect(onValueChange).toHaveBeenCalledWith('apricot');
    expect(trigger).toHaveFocus();
    expect(trigger).toHaveTextContent('Apricot');
    expect(onClose).not.toHaveBeenCalled();

    fireEvent.click(trigger);
    fireEvent.click(screen.getByRole('option', { name: 'Cherry' }));
    expect(onValueChange).toHaveBeenLastCalledWith('cherry');
    expect(screen.getByRole('dialog', { name: 'Edit' })).toBeInTheDocument();
  });

  it('closes the menu on Escape before the dialog, then the dialog', () => {
    const onClose = vi.fn();
    render(<Dialog onClose={onClose} onValueChange={vi.fn()} />);
    const trigger = screen.getByRole('combobox', { name: 'Fruit' });
    fireEvent.click(trigger);
    fireEvent.keyDown(screen.getByRole('listbox'), { key: 'Escape' });
    expect(screen.queryByRole('listbox')).not.toBeInTheDocument();
    expect(onClose).not.toHaveBeenCalled();
    expect(trigger).toHaveFocus();

    fireEvent.keyDown(trigger, { key: 'Escape' });
    expect(onClose).toHaveBeenCalledTimes(1);
  });

  it('moves Tab on from the trigger and closes the menu, in both directions', () => {
    render(<Dialog onClose={vi.fn()} onValueChange={vi.fn()} options={TABLES} />);
    const trigger = screen.getByRole('combobox', { name: 'Fruit' });
    fireEvent.click(trigger);
    const search = screen.getByRole('combobox', { name: 'Search…' });
    expect(search).toHaveFocus();
    expect(screen.getByRole('dialog')).toContainElement(search);

    fireEvent.keyDown(search, { key: 'Tab' });
    expect(screen.getByRole('button', { name: 'Last' })).toHaveFocus();
    expect(screen.queryByRole('listbox')).not.toBeInTheDocument();

    fireEvent.click(trigger);
    fireEvent.keyDown(screen.getByRole('combobox', { name: 'Search…' }), {
      key: 'Tab',
      shiftKey: true,
    });
    expect(screen.getByRole('button', { name: 'First' })).toHaveFocus();
    expect(screen.queryByRole('listbox')).not.toBeInTheDocument();
  });

  it('keeps an open menu out of the tab order of the dialog', () => {
    render(<Dialog onClose={vi.fn()} onValueChange={vi.fn()} />);
    fireEvent.click(screen.getByRole('combobox', { name: 'Fruit' }));
    act(() => screen.getByRole('button', { name: 'First' }).focus());
    expect(screen.queryByRole('listbox')).not.toBeInTheDocument();
    fireEvent.keyDown(screen.getByRole('button', { name: 'First' }), { key: 'Tab' });
    expect(screen.getByRole('combobox', { name: 'Fruit' })).toHaveFocus();
  });
});
