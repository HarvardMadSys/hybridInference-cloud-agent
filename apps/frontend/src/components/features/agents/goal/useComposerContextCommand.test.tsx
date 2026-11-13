// @vitest-environment jsdom
import '@testing-library/jest-dom/vitest';

import { useRef, useState } from 'react';
import { cleanup, fireEvent, render, screen } from '@testing-library/react';
import { afterEach, expect, it, vi } from 'vitest';

import { LocaleProvider } from '@/components/providers/LocaleProvider';
import { useContextCommandDialog } from '../context/ContextCommandDialog';
import { useGoalCommand } from './useGoalCommand';

afterEach(cleanup);

/** The new-task composer's wiring: `/context` explains itself in a dialog. */
function Composer({
  onApply = vi.fn(),
  send = vi.fn(),
  withContext = true,
}: {
  onApply?: (objective: string) => Promise<void>;
  send?: (text: string) => void;
  withContext?: boolean;
}) {
  const [value, setValue] = useState('');
  const box = useRef<HTMLTextAreaElement>(null);
  const context = useContextCommandDialog(() => box.current?.focus());
  const command = useGoalCommand({
    value,
    setValue,
    focusComposer: () => box.current?.focus(),
    onNeedsObjective: vi.fn(),
    onApply,
    onError: vi.fn(),
    onContext: withContext ? context.open : undefined,
  });
  return (
    <>
      <textarea
        ref={box}
        aria-label="Task"
        value={value}
        onChange={(event) => setValue(event.target.value)}
        onKeyDown={(event) => {
          if (command.onKeyDown(event)) return;
          if (event.key === 'Enter') {
            event.preventDefault();
            void command.submit().then((consumed) => {
              if (!consumed) send(value);
            });
          }
        }}
      />
      <ul aria-label="Commands">
        {command.entries.map((entry) => (
          <li key={entry.name}>{entry.name}</li>
        ))}
      </ul>
      {context.dialog}
    </>
  );
}

function show(props: Parameters<typeof Composer>[0] = {}) {
  render(
    <LocaleProvider>
      <Composer {...props} />
    </LocaleProvider>,
  );
  const box = screen.getByLabelText('Task');
  // The owner is typing in it.
  box.focus();
  return box;
}

it('/context explains there is no context yet, and sends and sets nothing', async () => {
  const send = vi.fn();
  const onApply = vi.fn();
  const box = show({ send, onApply });
  fireEvent.change(box, { target: { value: '/context' } });
  fireEvent.keyDown(box, { key: 'Enter' });
  const dialog = await screen.findByRole('dialog', { name: 'Context' });
  expect(dialog).toHaveTextContent('There is no context yet');
  expect(screen.getByRole('link', { name: 'Open Context insights' })).toHaveAttribute(
    'href',
    '/settings/usage#settings-context-insights',
  );
  expect(send).not.toHaveBeenCalled();
  expect(onApply).not.toHaveBeenCalled();
  expect(box).toHaveValue('');

  // Escape closes it and the box has the caret again.
  fireEvent.keyDown(document.activeElement!, { key: 'Escape' });
  expect(screen.queryByRole('dialog')).not.toBeInTheDocument();
  expect(box).toHaveFocus();
});

it('keeps what followed the token as the draft', async () => {
  const send = vi.fn();
  const box = show({ send });
  fireEvent.change(box, { target: { value: '/context and then write the tests' } });
  fireEvent.keyDown(box, { key: 'Enter' });
  await screen.findByRole('dialog');
  expect(box).toHaveValue('and then write the tests');
  expect(send).not.toHaveBeenCalled();
});

it('moves through both commands with the arrow keys', async () => {
  const box = show();
  fireEvent.change(box, { target: { value: '/' } });
  expect(screen.getAllByRole('listitem').map((item) => item.textContent)).toEqual([
    'goal',
    'context',
  ]);
  fireEvent.keyDown(box, { key: 'ArrowDown' });
  fireEvent.keyDown(box, { key: 'Enter' });
  expect(await screen.findByRole('dialog', { name: 'Context' })).toBeInTheDocument();
  expect(box).toHaveValue('');
});

it('is not offered, and is ordinary text, where nothing wires it', async () => {
  const send = vi.fn();
  const box = show({ send, withContext: false });
  fireEvent.change(box, { target: { value: '/' } });
  expect(screen.getAllByRole('listitem').map((item) => item.textContent)).toEqual(['goal']);
  fireEvent.change(box, { target: { value: '/context please' } });
  fireEvent.keyDown(box, { key: 'Enter' });
  await vi.waitFor(() => expect(send).toHaveBeenCalledWith('/context please'));
  expect(screen.queryByRole('dialog')).not.toBeInTheDocument();
});
