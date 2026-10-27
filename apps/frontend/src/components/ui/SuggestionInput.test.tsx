// @vitest-environment jsdom
import '@testing-library/jest-dom/vitest';

import { cleanup, fireEvent, render, screen, within } from '@testing-library/react';
import { createRef, useRef, useState, type ReactNode } from 'react';
import { createPortal } from 'react-dom';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { SuggestionInput } from './SuggestionInput';
import { useModalDialog } from './useModalDialog';

const MODELS = ['deepseek-v4-flash', 'deepseek-v4-pro', 'glm-5.1'];

function Field({
  initial = '',
  suggestions = MODELS,
  onChange = vi.fn(),
}: {
  initial?: string;
  suggestions?: readonly string[] | null;
  onChange?: (value: string) => void;
}) {
  const [value, setValue] = useState(initial);
  return (
    <>
      <label htmlFor="model">Model</label>
      <SuggestionInput
        id="model"
        name="model"
        value={value}
        suggestions={suggestions}
        onChange={(event) => {
          setValue(event.target.value);
          onChange(event.target.value);
        }}
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
        {children}
      </div>
    </div>,
    document.body,
  );
}

function field() {
  return screen.getByRole('combobox', { name: 'Model' });
}

function listbox() {
  return document.getElementById(field().getAttribute('aria-controls') ?? '');
}

function optionNames() {
  return within(listbox()!)
    .getAllByRole('option')
    .map((option) => option.textContent);
}

afterEach(() => {
  cleanup();
  document.body.style.overflow = '';
});

describe('SuggestionInput', () => {
  it('stays a free-text input that filters its suggestions while typing', () => {
    const onChange = vi.fn();
    render(<Field onChange={onChange} />);
    const input = field();
    expect(input).toHaveAttribute('aria-expanded', 'false');
    expect(input).toHaveAttribute('aria-autocomplete', 'list');
    expect(input).toHaveAttribute('name', 'model');

    fireEvent.change(input, { target: { value: 'FLASH' } });
    expect(input).toHaveAttribute('aria-expanded', 'true');
    expect(screen.getByRole('listbox', { name: 'Model' })).toBe(listbox());
    expect(optionNames()).toEqual(['deepseek-v4-flash']);
    // Nothing is highlighted while typing, so Enter keeps its native meaning.
    expect(input).not.toHaveAttribute('aria-activedescendant');
    expect(fireEvent.keyDown(input, { key: 'Enter' })).toBe(true);
    expect(listbox()).toBeNull();

    fireEvent.change(input, { target: { value: 'my-own-model' } });
    expect(listbox()).toBeNull();
    fireEvent.change(input, { target: { value: '' } });
    expect(onChange.mock.calls.map(([value]) => value)).toEqual(['FLASH', 'my-own-model', '']);
    expect(input).toHaveValue('');
  });

  it('opens on an arrow at the current value and enters a suggestion with Enter', () => {
    const onChange = vi.fn();
    render(<Field initial="deepseek-v4-pro" onChange={onChange} />);
    const input = field();
    input.focus();
    fireEvent.keyDown(input, { key: 'ArrowDown' });
    // An arrow offers every suggestion, not only those matching the value.
    expect(optionNames()).toEqual(MODELS);
    const active = () => document.getElementById(input.getAttribute('aria-activedescendant')!);
    expect(active()).toHaveTextContent('deepseek-v4-pro');
    expect(active()).toHaveAttribute('aria-selected', 'true');

    fireEvent.keyDown(input, { key: 'ArrowDown' });
    fireEvent.keyDown(input, { key: 'ArrowDown' });
    expect(active()).toHaveTextContent('glm-5.1');
    fireEvent.keyDown(input, { key: 'ArrowUp' });
    expect(active()).toHaveTextContent('deepseek-v4-pro');
    fireEvent.keyDown(input, { key: 'ArrowUp' });
    const outer = vi.fn();
    document.addEventListener('keydown', outer);
    expect(fireEvent.keyDown(input, { key: 'Enter' })).toBe(false);
    document.removeEventListener('keydown', outer);
    expect(outer).not.toHaveBeenCalled();
    expect(onChange).toHaveBeenCalledExactlyOnceWith('deepseek-v4-flash');
    expect(input).toHaveValue('deepseek-v4-flash');
    expect(input).toHaveFocus();
    expect(listbox()).toBeNull();
  });

  it('closes on Escape, Tab and blur without changing the value', () => {
    const onChange = vi.fn();
    render(<Field initial="glm" onChange={onChange} />);
    const input = field();
    fireEvent.keyDown(input, { key: 'ArrowDown' });
    fireEvent.keyDown(input, { key: 'ArrowDown' });
    const outer = vi.fn();
    document.addEventListener('keydown', outer);
    expect(fireEvent.keyDown(input, { key: 'Escape' })).toBe(false);
    expect(listbox()).toBeNull();
    expect(outer).not.toHaveBeenCalled();
    // A closed list leaves Escape to whatever handles it outside.
    fireEvent.keyDown(input, { key: 'Escape' });
    expect(outer).toHaveBeenCalledTimes(1);
    document.removeEventListener('keydown', outer);

    fireEvent.keyDown(input, { key: 'ArrowDown' });
    expect(fireEvent.keyDown(input, { key: 'Tab' })).toBe(true);
    expect(listbox()).toBeNull();
    fireEvent.keyDown(input, { key: 'ArrowDown' });
    fireEvent.blur(input);
    expect(listbox()).toBeNull();
    expect(onChange).not.toHaveBeenCalled();
    expect(input).toHaveValue('glm');
  });

  it('enters a clicked suggestion without taking focus from the field', () => {
    const onChange = vi.fn();
    render(<Field onChange={onChange} />);
    const input = field();
    input.focus();
    fireEvent.click(input);
    const option = within(listbox()!).getByRole('option', { name: 'glm-5.1' });
    fireEvent.pointerMove(option);
    expect(input.getAttribute('aria-activedescendant')).toBe(option.id);
    // Default prevented: the press does not move focus out of the input.
    expect(fireEvent.mouseDown(option)).toBe(false);
    fireEvent.click(option);
    expect(onChange).toHaveBeenCalledExactlyOnceWith('glm-5.1');
    expect(input).toHaveFocus();
    expect(listbox()).toBeNull();
  });

  it('follows catalog updates, and is a plain input without suggestions', () => {
    const { rerender } = render(<Field suggestions={null} />);
    const input = screen.getByRole('textbox', { name: 'Model' });
    expect(input).not.toHaveAttribute('aria-expanded');
    fireEvent.keyDown(input, { key: 'ArrowDown' });
    expect(screen.queryByRole('listbox')).not.toBeInTheDocument();

    rerender(<Field suggestions={['qwen-3']} />);
    expect(field()).toBe(input);
    fireEvent.keyDown(input, { key: 'ArrowDown' });
    expect(optionNames()).toEqual(['qwen-3']);
    rerender(<Field suggestions={['qwen-3', 'kimi-k2']} />);
    expect(optionNames()).toEqual(['qwen-3', 'kimi-k2']);
  });

  it('forwards its ref and native attributes to the input', () => {
    const ref = createRef<HTMLInputElement>();
    render(
      <form aria-label="Settings">
        <SuggestionInput ref={ref} name="model" required defaultValue="" suggestions={MODELS} />
      </form>,
    );
    expect(ref.current).toBeInstanceOf(HTMLInputElement);
    expect(ref.current).toBeInvalid();
    expect(ref.current?.id).toBeTruthy();
  });
});

describe('SuggestionInput inside a modal dialog', () => {
  it('keeps the list in the panel and takes Escape before the dialog', () => {
    const onClose = vi.fn();
    render(
      <Modal onClose={onClose}>
        <Field />
      </Modal>,
    );
    const dialog = screen.getByRole('dialog');
    const input = field();
    input.focus();
    fireEvent.keyDown(input, { key: 'ArrowDown' });
    expect(dialog).toContainElement(listbox());

    fireEvent.keyDown(input, { key: 'Escape' });
    expect(listbox()).toBeNull();
    expect(onClose).not.toHaveBeenCalled();
    fireEvent.keyDown(input, { key: 'Escape' });
    expect(onClose).toHaveBeenCalledTimes(1);
  });

  it('moves Tab on from the field and closes the list', () => {
    render(
      <Modal onClose={vi.fn()}>
        <Field />
      </Modal>,
    );
    const input = field();
    input.focus();
    fireEvent.keyDown(input, { key: 'ArrowDown' });
    fireEvent.keyDown(input, { key: 'Tab' });
    expect(screen.getByRole('button', { name: 'After' })).toHaveFocus();
    expect(listbox()).toBeNull();
  });
});
