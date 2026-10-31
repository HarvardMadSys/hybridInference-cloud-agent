// @vitest-environment jsdom
import '@testing-library/jest-dom/vitest';
import { cleanup, fireEvent, render, screen } from '@testing-library/react';
import { useState } from 'react';
import { afterEach, expect, it, vi } from 'vitest';
import type { ActivityOverview } from '../activityOverview';
import { ComposerOptionsButton, ComposerOptionsPanel, useComposerOptions } from './ComposerOptions';
import { ComposerTodos } from './ComposerTodos';

afterEach(cleanup);

function overview(statuses: Array<string | undefined>): ActivityOverview {
  return {
    subagents: [],
    taskActivity: {
      kind: 'tasks',
      title: 'Tasks',
      tasks: statuses.map((status, index) => ({
        subject: `Task ${index + 1}`,
        ...(status ? { status } : {}),
        blockedBy: [],
        blocks: [],
      })),
    },
  };
}

it('summarizes the recorded task statuses and toggles the tasks window', () => {
  const toggle = vi.fn();
  render(
    <ComposerTodos
      overview={overview(['in_progress', 'pending', 'pending', 'completed'])}
      open={false}
      toggle={toggle}
    />,
  );
  const strip = screen.getByRole('button', {
    name: 'To-dos: 1 in progress · 2 pending · 1 completed',
  });
  expect(strip).toHaveAttribute('aria-expanded', 'false');
  expect(strip).toHaveAttribute('aria-controls', 'job-activity-window');
  fireEvent.click(strip);
  expect(toggle).toHaveBeenCalledOnce();
});

it('counts tasks that recorded no status, and draws nothing without tasks', () => {
  const { rerender } = render(
    <ComposerTodos overview={overview([undefined, undefined])} open toggle={() => {}} />,
  );
  expect(screen.getByRole('button', { name: 'To-dos: 2 total' })).toHaveAttribute(
    'aria-expanded',
    'true',
  );
  rerender(<ComposerTodos overview={{ subagents: [] }} open={false} toggle={() => {}} />);
  expect(screen.queryByRole('button')).not.toBeInTheDocument();
});

function Options({ initiallyActive = false }: { initiallyActive?: boolean }) {
  const [active, setActive] = useState(initiallyActive);
  const options = useComposerOptions(active);
  return (
    <>
      <ComposerOptionsButton options={options} />
      <ComposerOptionsPanel options={options}>
        <button type="button" onClick={() => setActive(!active)}>
          Rewrite prompt
        </button>
      </ComposerOptionsPanel>
    </>
  );
}

it('keeps secondary controls mounted but hidden until the options are opened', () => {
  render(<Options />);
  const trigger = screen.getByRole('button', { name: 'Prompt options' });
  expect(trigger).toHaveAttribute('aria-expanded', 'false');
  expect(screen.queryByRole('group', { name: 'Prompt options' })).not.toBeInTheDocument();
  expect(screen.getByText('Rewrite prompt')).not.toBeVisible();

  fireEvent.click(trigger);
  expect(trigger).toHaveAttribute('aria-expanded', 'true');
  const panel = screen.getByRole('group', { name: 'Prompt options' });
  expect(trigger).toHaveAttribute('aria-controls', panel.id);
  const rewrite = screen.getByRole('button', { name: 'Rewrite prompt' });
  rewrite.focus();

  fireEvent.keyDown(rewrite, { key: 'Escape' });
  expect(trigger).toHaveAttribute('aria-expanded', 'false');
  expect(trigger).toHaveFocus();
});

it('stays open while its work is active, and closes again only once it ends', () => {
  render(<Options initiallyActive />);
  const trigger = screen.getByRole('button', { name: 'Prompt options' });
  expect(trigger).toHaveAttribute('aria-expanded', 'true');
  expect(trigger).toBeDisabled();
  const rewrite = screen.getByRole('button', { name: 'Rewrite prompt' });
  fireEvent.keyDown(rewrite, { key: 'Escape' });
  expect(screen.getByRole('group', { name: 'Prompt options' })).toBeVisible();

  fireEvent.click(rewrite);
  expect(trigger).toBeEnabled();
  expect(trigger).toHaveAttribute('aria-expanded', 'false');
});
