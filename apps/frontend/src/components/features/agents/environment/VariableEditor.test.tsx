// @vitest-environment jsdom
import '@testing-library/jest-dom/vitest';
import { cleanup, fireEvent, render, screen } from '@testing-library/react';
import { useState } from 'react';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { VariableEditor, variableEdits } from './VariableEditor';
import type { EnvironmentVariable, VariableEdit } from '@/lib/api/environments';

vi.mock('@/components/providers', () => ({ useT: () => (key: string) => key }));
afterEach(cleanup);

function harness(saved: EnvironmentVariable[]) {
  const latest: { edits: VariableEdit[] } = { edits: variableEdits(saved) };
  function Harness() {
    const [edits, setEdits] = useState(latest.edits);
    return (
      <VariableEditor
        values={edits}
        onChange={(next) => {
          latest.edits = next;
          setEdits(next);
        }}
      />
    );
  }
  render(<Harness />);
  return latest;
}

const secret = { name: 'API_KEY', secret: true, phase: 'both' as const, value: null };
const ordinary = { name: 'LOG_LEVEL', secret: false, phase: 'both' as const, value: 'debug' };

describe('a saved variable the owner returns to its saved state', () => {
  it('keeps a Secret switched to Ordinary and back, instead of saving it empty', () => {
    const latest = harness([secret]);
    const type = screen.getByLabelText('environment.type API_KEY');
    fireEvent.change(type, { target: { value: 'ordinary' } });
    expect(latest.edits[0]).toMatchObject({ operation: 'replace', value: '' });
    fireEvent.change(type, { target: { value: 'secret' } });
    expect(latest.edits[0]).toMatchObject({ secret: true, operation: 'keep' });
    expect(latest.edits[0].value).toBeUndefined();
  });

  it('keeps a Secret whose field was typed in and cleared again', () => {
    const latest = harness([secret]);
    const field = screen.getByLabelText('environment.value API_KEY');
    fireEvent.change(field, { target: { value: 'x' } });
    expect(latest.edits[0]).toMatchObject({ operation: 'replace', value: 'x' });
    fireEvent.change(field, { target: { value: '' } });
    expect(latest.edits[0]).toMatchObject({ operation: 'keep' });
    expect(latest.edits[0].value).toBeUndefined();
  });

  it('keeps an ordinary value marked Secret and then Ordinary again', () => {
    const latest = harness([ordinary]);
    const type = screen.getByLabelText('environment.type LOG_LEVEL');
    fireEvent.change(type, { target: { value: 'secret' } });
    fireEvent.change(type, { target: { value: 'ordinary' } });
    expect(latest.edits[0]).toMatchObject({ secret: false, operation: 'keep', value: 'debug' });
  });

  it('still clears a typed Secret rather than show it in a plain field', () => {
    const latest = harness([]);
    fireEvent.click(screen.getByText('environment.addVariable'));
    fireEvent.change(screen.getByLabelText('environment.value'), {
      target: { value: 'typed-secret-value' },
    });
    fireEvent.change(screen.getByLabelText('environment.type'), {
      target: { value: 'ordinary' },
    });
    expect(latest.edits[0]).toMatchObject({ secret: false, operation: 'replace', value: '' });
  });
});
