// @vitest-environment jsdom
import '@testing-library/jest-dom/vitest';

import { useState } from 'react';
import { cleanup, fireEvent, render, screen } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { Picker } from './Picker';

const models = [
  'glm-5.3',
  'kimi-k2.7-code-highspeed',
  'deepseek-v4-flash',
  'qwen-next',
  'minimax-m3',
  'gemma-4-31b',
  'llama-3.3-70b',
  'gpt-oss-20b',
];

function ControlledPicker({
  options = models,
  onChange = vi.fn(),
}: {
  options?: string[];
  onChange?: (value: string) => void;
}) {
  const [value, setValue] = useState(options[0]);
  return (
    <Picker
      label="Model"
      value={value}
      options={options}
      onChange={(next) => {
        setValue(next);
        onChange(next);
      }}
    />
  );
}

describe('Picker', () => {
  afterEach(cleanup);

  it('renders nothing when there are no options', () => {
    const { container } = render(<Picker label="Model" value="" options={[]} onChange={vi.fn()} />);
    expect(container).toBeEmptyDOMElement();
  });

  it('sizes the trigger to its selected value and exposes its name and current choice', () => {
    render(<ControlledPicker />);
    expect(screen.getByText('glm-5.3')).toHaveClass('truncate');
    expect(screen.getByRole('button', { name: 'Model' })).toHaveAccessibleDescription('glm-5.3');
    expect(screen.getByRole('button', { name: 'Model' })).toHaveAttribute('aria-expanded', 'false');
  });

  it('selects from the themed list and returns focus to the trigger', () => {
    const onChange = vi.fn();
    render(<ControlledPicker options={models.slice(0, 2)} onChange={onChange} />);
    const trigger = screen.getByRole('button', { name: 'Model' });
    fireEvent.click(trigger);
    expect(screen.getAllByRole('option').map((option) => option.textContent)).toEqual(
      models.slice(0, 2),
    );
    expect(screen.getByRole('option', { name: 'glm-5.3' })).toHaveAttribute(
      'aria-selected',
      'true',
    );
    fireEvent.click(screen.getByRole('option', { name: 'kimi-k2.7-code-highspeed' }));
    expect(onChange).toHaveBeenCalledExactlyOnceWith('kimi-k2.7-code-highspeed');
    expect(trigger).toHaveValue('kimi-k2.7-code-highspeed');
    expect(trigger).toHaveFocus();
    expect(screen.queryByRole('dialog')).not.toBeInTheDocument();
  });

  it('supports arrows, Home, End and typeahead without committing until Enter', () => {
    const onChange = vi.fn();
    render(
      <ControlledPicker
        options={['claude-code', 'codex', 'kilo', 'opencode', 'pi']}
        onChange={onChange}
      />,
    );
    fireEvent.keyDown(screen.getByRole('button'), { key: 'ArrowDown' });
    const list = screen.getByRole('listbox');
    expect(list).toHaveFocus();
    fireEvent.keyDown(list, { key: 'End' });
    expect(list).toHaveAttribute(
      'aria-activedescendant',
      screen.getByRole('option', { name: 'pi' }).id,
    );
    fireEvent.keyDown(list, { key: 'Home' });
    fireEvent.keyDown(list, { key: 'ArrowDown' });
    expect(list).toHaveAttribute(
      'aria-activedescendant',
      screen.getByRole('option', { name: 'codex' }).id,
    );
    fireEvent.keyDown(list, { key: 'k' });
    expect(onChange).not.toHaveBeenCalled();
    fireEvent.keyDown(list, { key: 'Enter' });
    expect(onChange).toHaveBeenCalledExactlyOnceWith('kilo');
  });

  it('filters long lists case-insensitively and commits the keyboard choice', () => {
    const onChange = vi.fn();
    render(<ControlledPicker onChange={onChange} />);
    fireEvent.click(screen.getByRole('button'));
    const search = screen.getByRole('combobox', { name: 'Search…' });
    expect(search).toHaveFocus();
    fireEvent.change(search, { target: { value: '  KIMI  ' } });
    expect(screen.getAllByRole('option')).toHaveLength(1);
    fireEvent.keyDown(search, { key: 'Enter' });
    expect(onChange).toHaveBeenCalledExactlyOnceWith('kimi-k2.7-code-highspeed');
  });

  it('shows an empty result without selecting or submitting, and clears search on reopening', () => {
    const onChange = vi.fn();
    const submit = vi.fn();
    render(
      <form onSubmit={submit}>
        <ControlledPicker onChange={onChange} />
      </form>,
    );
    const trigger = screen.getByRole('button');
    fireEvent.click(trigger);
    const search = screen.getByRole('combobox');
    fireEvent.change(search, { target: { value: 'missing' } });
    expect(screen.getByRole('status')).toHaveTextContent('No matching options');
    fireEvent.keyDown(search, { key: 'Enter' });
    expect(onChange).not.toHaveBeenCalled();
    expect(submit).not.toHaveBeenCalled();
    fireEvent.keyDown(search, { key: 'Escape' });
    expect(trigger).toHaveFocus();
    fireEvent.click(trigger);
    expect(screen.getByRole('combobox')).toHaveValue('');
    expect(screen.getAllByRole('option')).toHaveLength(models.length);
  });

  it('leaves IME confirmation to the search input', () => {
    const onChange = vi.fn();
    render(<ControlledPicker onChange={onChange} />);
    fireEvent.click(screen.getByRole('button'));
    fireEvent.keyDown(screen.getByRole('combobox'), { key: 'Enter', isComposing: true });
    expect(onChange).not.toHaveBeenCalled();
    expect(screen.getByRole('dialog')).toBeInTheDocument();
  });

  it('dismisses on outside pointer or focus without moving focus back', () => {
    render(
      <>
        <ControlledPicker />
        <button>Next</button>
      </>,
    );
    const trigger = screen.getByRole('button', { name: 'Model' });
    fireEvent.click(trigger);
    fireEvent.pointerDown(document.body);
    expect(screen.queryByRole('dialog')).not.toBeInTheDocument();
    fireEvent.click(trigger);
    fireEvent.focusIn(screen.getByRole('button', { name: 'Next' }));
    expect(screen.queryByRole('dialog')).not.toBeInTheDocument();
  });

  it('removes an open panel when its options disappear', () => {
    const view = render(
      <Picker label="Model" value="glm-5.3" options={models} onChange={vi.fn()} />,
    );
    fireEvent.click(screen.getByRole('button'));
    view.rerender(<Picker label="Model" value="" options={[]} onChange={vi.fn()} />);
    expect(screen.queryByRole('dialog')).not.toBeInTheDocument();
    expect(screen.queryByRole('button')).not.toBeInTheDocument();
  });
});
