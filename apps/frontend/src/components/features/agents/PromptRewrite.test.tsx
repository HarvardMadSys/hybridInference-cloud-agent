// @vitest-environment jsdom
import '@testing-library/jest-dom/vitest';
import { act, cleanup, fireEvent, render, screen } from '@testing-library/react';
import { afterEach, beforeEach, expect, it, vi } from 'vitest';
import { rewritePrompt, type RewritePromptResult } from '@/lib/api/promptRewrite';
import { PromptRewrite } from './PromptRewrite';

vi.mock('@/lib/api/promptRewrite', () => ({ rewritePrompt: vi.fn() }));
const rewrite = vi.mocked(rewritePrompt);
const initial = { value: 'Build a dashboard', model: 'model-a' };
const questions: RewritePromptResult = {
  kind: 'questions',
  questions: ['Who will use it?', 'Which metrics matter?'],
};

function deferred() {
  let resolve!: (result: RewritePromptResult) => void;
  let reject!: (error: Error) => void;
  const promise = new Promise<RewritePromptResult>((yes, no) => {
    resolve = yes;
    reject = no;
  });
  return { promise, resolve, reject };
}

function start() {
  fireEvent.click(screen.getByRole('button', { name: 'Rewrite prompt' }));
}

async function ask() {
  rewrite.mockResolvedValueOnce(questions);
  fireEvent.click(screen.getByRole('checkbox', { name: 'Ask clarification questions first' }));
  await act(async () => start());
}

beforeEach(() => rewrite.mockReset());
afterEach(cleanup);

it('rewrites directly by default and applies the prompt without submitting the composer', async () => {
  const onApply = vi.fn();
  const onSubmit = vi.fn((event) => event.preventDefault());
  const onActiveChange = vi.fn();
  rewrite.mockResolvedValue({ kind: 'prompt', prompt: 'Build a dashboard for weekly revenue.' });
  render(
    <form onSubmit={onSubmit}>
      <PromptRewrite {...initial} onApply={onApply} onActiveChange={onActiveChange} />
    </form>,
  );
  expect(screen.getByRole('checkbox')).not.toBeChecked();
  await act(async () => start());
  expect(rewrite).toHaveBeenCalledWith(
    { prompt: initial.value, model: initial.model, clarify: false },
    expect.any(AbortSignal),
  );
  expect(onApply).toHaveBeenCalledWith('Build a dashboard for weekly revenue.');
  expect(onSubmit).not.toHaveBeenCalled();
  expect(onActiveChange.mock.calls.map(([active]) => active)).toEqual([false, true, false]);
});

it('collects every clarification answer before generating and applying a comprehensive prompt', async () => {
  const onApply = vi.fn();
  const onActiveChange = vi.fn();
  render(<PromptRewrite {...initial} onApply={onApply} onActiveChange={onActiveChange} />);
  await ask();
  expect(rewrite).toHaveBeenCalledWith(
    { prompt: initial.value, model: initial.model, clarify: true },
    expect.any(AbortSignal),
  );
  expect(screen.getByRole('textbox', { name: 'Who will use it?' })).toHaveFocus();
  expect(screen.getByRole('button', { name: 'Generate prompt' })).toBeDisabled();
  fireEvent.change(screen.getByRole('textbox', { name: 'Who will use it?' }), {
    target: { value: ' Product managers ' },
  });
  fireEvent.change(screen.getByRole('textbox', { name: 'Which metrics matter?' }), {
    target: { value: ' ' },
  });
  expect(screen.getByRole('button', { name: 'Generate prompt' })).toBeDisabled();
  fireEvent.change(screen.getByRole('textbox', { name: 'Which metrics matter?' }), {
    target: { value: 'Revenue and retention' },
  });
  rewrite.mockResolvedValueOnce({ kind: 'prompt', prompt: 'Detailed dashboard requirements' });
  await act(async () => {
    fireEvent.click(screen.getByRole('button', { name: 'Generate prompt' }));
  });
  expect(rewrite).toHaveBeenLastCalledWith(
    {
      prompt: initial.value,
      model: initial.model,
      clarify: false,
      answers: [
        { question: 'Who will use it?', answer: 'Product managers' },
        { question: 'Which metrics matter?', answer: 'Revenue and retention' },
      ],
    },
    expect.any(AbortSignal),
  );
  expect(onApply).toHaveBeenCalledWith('Detailed dashboard requirements');
  expect(onActiveChange).toHaveBeenLastCalledWith(false);
  expect(screen.queryByRole('textbox')).not.toBeInTheDocument();
});

it('blocks repeated requests while pending and cancels without applying a late response', async () => {
  const request = deferred();
  rewrite.mockReturnValueOnce(request.promise);
  const onApply = vi.fn();
  const onActiveChange = vi.fn();
  render(<PromptRewrite {...initial} onApply={onApply} onActiveChange={onActiveChange} />);
  start();
  expect(screen.getByRole('button', { name: 'Rewrite prompt' })).toBeDisabled();
  expect(screen.getByRole('checkbox')).toBeDisabled();
  expect(screen.getByRole('status')).toHaveTextContent('Rewriting…');
  start();
  expect(rewrite).toHaveBeenCalledTimes(1);
  const signal = rewrite.mock.calls[0][1];
  fireEvent.click(screen.getByRole('button', { name: 'Cancel' }));
  expect(signal?.aborted).toBe(true);
  expect(onActiveChange).toHaveBeenLastCalledWith(false);
  await act(async () => request.resolve({ kind: 'prompt', prompt: 'Obsolete rewrite' }));
  expect(onApply).not.toHaveBeenCalled();
  expect(screen.getByRole('button', { name: 'Rewrite prompt' })).toBeEnabled();
});

it('cancels clarification questions without changing the draft', async () => {
  const onApply = vi.fn();
  const onActiveChange = vi.fn();
  render(<PromptRewrite {...initial} onApply={onApply} onActiveChange={onActiveChange} />);
  await ask();
  expect(onActiveChange).toHaveBeenLastCalledWith(true);
  fireEvent.click(screen.getByRole('button', { name: 'Cancel' }));
  expect(screen.queryByRole('textbox')).not.toBeInTheDocument();
  expect(onApply).not.toHaveBeenCalled();
  expect(onActiveChange).toHaveBeenLastCalledWith(false);
  expect(screen.getByRole('button', { name: 'Rewrite prompt' })).toHaveFocus();
});

it('keeps the success notice when the parent applies the prompt and clears it after editing', async () => {
  const onApply = vi.fn();
  const result = 'Build a dashboard for weekly revenue';
  rewrite.mockResolvedValueOnce({ kind: 'prompt', prompt: result });
  const view = render(<PromptRewrite {...initial} onApply={onApply} />);
  await act(async () => start());
  view.rerender(<PromptRewrite {...initial} value={result} onApply={onApply} />);
  expect(screen.getByRole('status')).toHaveTextContent(
    'Prompt rewritten. Review it before sending.',
  );
  view.rerender(<PromptRewrite {...initial} value={`${result}.`} onApply={onApply} />);
  expect(screen.queryByRole('status')).not.toBeInTheDocument();
});

it.each([
  { value: 'A new draft', model: initial.model },
  { ...initial, model: 'model-b' },
])('discards an outstanding response after the draft or model changes: %j', async (next) => {
  const request = deferred();
  const onApply = vi.fn();
  const onActiveChange = vi.fn();
  rewrite.mockReturnValueOnce(request.promise);
  const view = render(
    <PromptRewrite {...initial} onApply={onApply} onActiveChange={onActiveChange} />,
  );
  start();
  const signal = rewrite.mock.calls[0][1];
  view.rerender(<PromptRewrite {...next} onApply={onApply} onActiveChange={onActiveChange} />);
  expect(signal?.aborted).toBe(true);
  expect(onActiveChange).toHaveBeenLastCalledWith(false);
  await act(async () => request.resolve({ kind: 'prompt', prompt: 'Old draft rewrite' }));
  expect(onApply).not.toHaveBeenCalled();
});

it('clears clarification answers when the source draft changes', async () => {
  const onApply = vi.fn();
  const view = render(<PromptRewrite {...initial} onApply={onApply} />);
  await ask();
  view.rerender(<PromptRewrite {...initial} value="Edited dashboard" onApply={onApply} />);
  expect(screen.queryByRole('textbox')).not.toBeInTheDocument();
  expect(screen.getByRole('button', { name: 'Rewrite prompt' })).toBeEnabled();
  expect(onApply).not.toHaveBeenCalled();
});

it('keeps the draft unchanged after an error and allows retrying', async () => {
  const onApply = vi.fn();
  const onActiveChange = vi.fn();
  rewrite.mockRejectedValueOnce(new Error('Network unavailable'));
  render(<PromptRewrite {...initial} onApply={onApply} onActiveChange={onActiveChange} />);
  await act(async () => start());
  expect(screen.getByRole('alert')).toHaveTextContent('Could not rewrite your prompt.');
  expect(onApply).not.toHaveBeenCalled();
  expect(onActiveChange).toHaveBeenLastCalledWith(false);
  rewrite.mockResolvedValueOnce({ kind: 'prompt', prompt: 'Retried prompt' });
  await act(async () => start());
  expect(onApply).toHaveBeenCalledWith('Retried prompt');
  expect(screen.queryByRole('alert')).not.toBeInTheDocument();
});

it('preserves clarification answers after generation fails', async () => {
  const onApply = vi.fn();
  const onActiveChange = vi.fn();
  render(<PromptRewrite {...initial} onApply={onApply} onActiveChange={onActiveChange} />);
  await ask();
  fireEvent.change(screen.getByRole('textbox', { name: 'Who will use it?' }), {
    target: { value: 'Managers' },
  });
  fireEvent.change(screen.getByRole('textbox', { name: 'Which metrics matter?' }), {
    target: { value: 'Revenue' },
  });
  rewrite.mockRejectedValueOnce(new Error('Try later'));
  await act(async () => {
    fireEvent.click(screen.getByRole('button', { name: 'Generate prompt' }));
  });
  expect(screen.getByRole('textbox', { name: 'Who will use it?' })).toHaveValue('Managers');
  expect(screen.getByRole('textbox', { name: 'Which metrics matter?' })).toHaveValue('Revenue');
  expect(onActiveChange).toHaveBeenLastCalledWith(true);
  expect(screen.getByRole('button', { name: 'Generate prompt' })).toBeEnabled();
  expect(onApply).not.toHaveBeenCalled();
});

it('aborts on unmount and ignores a completed response', async () => {
  const request = deferred();
  const onApply = vi.fn();
  const onActiveChange = vi.fn();
  rewrite.mockReturnValueOnce(request.promise);
  const view = render(
    <PromptRewrite {...initial} onApply={onApply} onActiveChange={onActiveChange} />,
  );
  start();
  const signal = rewrite.mock.calls[0][1];
  view.unmount();
  expect(signal?.aborted).toBe(true);
  await act(async () => request.resolve({ kind: 'prompt', prompt: 'Too late' }));
  expect(onApply).not.toHaveBeenCalled();
  expect(onActiveChange).toHaveBeenLastCalledWith(false);
});

it('requires a draft, a model, and enabled controls to start rewriting', () => {
  const onApply = vi.fn();
  const view = render(<PromptRewrite value="   " model="model-a" onApply={onApply} />);
  expect(screen.getByRole('button', { name: 'Rewrite prompt' })).toBeDisabled();
  view.rerender(<PromptRewrite value="Task" model="" onApply={onApply} />);
  expect(screen.getByRole('button', { name: 'Rewrite prompt' })).toBeDisabled();
  view.rerender(<PromptRewrite {...initial} disabled onApply={onApply} />);
  expect(screen.getByRole('button', { name: 'Rewrite prompt' })).toBeDisabled();
  expect(screen.getByRole('checkbox')).toBeDisabled();
  expect(rewrite).not.toHaveBeenCalled();
});
