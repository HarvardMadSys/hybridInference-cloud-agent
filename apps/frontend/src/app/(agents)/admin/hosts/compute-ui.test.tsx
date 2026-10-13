// @vitest-environment jsdom
import '@testing-library/jest-dom/vitest';

import { cleanup, fireEvent, render, screen } from '@testing-library/react';
import { useState } from 'react';
import { afterEach, describe, expect, it, vi } from 'vitest';

import type { ComputeField } from '@/lib/api/compute';

import { initialFields, parseFields, ProviderFields } from './compute-ui';

const fields: ComputeField[] = [
  {
    key: 'node_types',
    label: 'Preferred node types (top to bottom)',
    type: 'string_list',
    default: ['r6615', 'c6620', 'r650'],
  },
];

function ProviderForm({ onSave }: { onSave: (configuration: Record<string, unknown>) => void }) {
  const [values, setValues] = useState(initialFields(fields));
  return (
    <form
      onSubmit={(event) => {
        event.preventDefault();
        onSave(parseFields(fields, values));
      }}
    >
      <ProviderFields fields={fields} values={values} onChange={setValues} />
      <button type="submit">Save</button>
    </form>
  );
}

afterEach(() => cleanup());

describe('ordered provider string lists', () => {
  it('shows catalog defaults one per line and submits their order', () => {
    const save = vi.fn();
    render(<ProviderForm onSave={save} />);

    const preferences = screen.getByLabelText('Preferred node types (top to bottom)');
    expect(preferences.tagName).toBe('TEXTAREA');
    expect(preferences).toHaveValue('r6615\nc6620\nr650');
    fireEvent.click(screen.getByRole('button', { name: 'Save' }));

    expect(save).toHaveBeenCalledWith({ node_types: ['r6615', 'c6620', 'r650'] });
  });

  it('accepts comma or newline entries and keeps the first occurrence in order', () => {
    const save = vi.fn();
    render(<ProviderForm onSave={save} />);

    fireEvent.change(screen.getByLabelText('Preferred node types (top to bottom)'), {
      target: { value: ' r650, r6615\nc6620\n\nr650, c6620 ' },
    });
    fireEvent.click(screen.getByRole('button', { name: 'Save' }));

    expect(save).toHaveBeenCalledWith({ node_types: ['r650', 'r6615', 'c6620'] });
  });

  it('submits an explicit empty list when the preferred field is cleared', () => {
    const save = vi.fn();
    render(<ProviderForm onSave={save} />);

    fireEvent.change(screen.getByLabelText('Preferred node types (top to bottom)'), {
      target: { value: ' \n ' },
    });
    fireEvent.click(screen.getByRole('button', { name: 'Save' }));

    expect(save).toHaveBeenCalledWith({ node_types: [] });
  });
});
