// @vitest-environment jsdom
import '@testing-library/jest-dom/vitest';

import { cleanup, fireEvent, render, screen } from '@testing-library/react';
import { afterEach, expect, it, vi } from 'vitest';

import { onPlatform } from '@/lib/platformTestSupport';
import { ComposerMessageActions } from './ConversationComposer';

afterEach(cleanup);

it('exposes separate queue and steer actions without submitting the form when steering', () => {
  const submit = vi.fn((event) => event.preventDefault());
  const steer = vi.fn();
  render(
    <form onSubmit={submit}>
      <ComposerMessageActions
        label="Queue message"
        title="Send after this turn"
        busy={false}
        disabled={false}
        steerLabel="Steer now"
        steerTitle="Send into this turn — Ctrl+Enter"
        onSteer={steer}
      />
    </form>,
  );
  fireEvent.click(screen.getByRole('button', { name: 'Steer now' }));
  expect(steer).toHaveBeenCalledOnce();
  expect(submit).not.toHaveBeenCalled();
  expect(screen.getByRole('button', { name: 'Steer now' })).toHaveAttribute(
    'aria-keyshortcuts',
    'Control+Enter',
  );
  fireEvent.click(screen.getByRole('button', { name: 'Queue message' }));
  expect(submit).toHaveBeenCalledOnce();
});

it('names Command+Enter as the steer chord on a Mac', () => {
  onPlatform('MacIntel');
  render(
    <ComposerMessageActions
      label="Queue message"
      title="Send after this turn"
      busy={false}
      disabled={false}
      steerLabel="Steer now"
      onSteer={() => {}}
    />,
  );
  expect(screen.getByRole('button', { name: 'Steer now' })).toHaveAttribute(
    'aria-keyshortcuts',
    'Meta+Enter',
  );
});

it.each([
  { busy: true, disabled: false },
  { busy: false, disabled: true },
])('prevents both actions while unavailable: %s', ({ busy, disabled }) => {
  const submit = vi.fn((event) => event.preventDefault());
  const steer = vi.fn();
  render(
    <form onSubmit={submit}>
      <ComposerMessageActions
        label="Queue message"
        title="Send after this turn"
        busy={busy}
        disabled={disabled}
        steerLabel="Steer now"
        onSteer={steer}
      />
    </form>,
  );
  for (const button of screen.getAllByRole('button')) {
    expect(button).toBeDisabled();
    fireEvent.click(button);
  }
  expect(submit).not.toHaveBeenCalled();
  expect(steer).not.toHaveBeenCalled();
});

it('shows the supplied localized send label and omits steering when it is unsupported', () => {
  render(
    <ComposerMessageActions
      label="发送"
      title="发送消息"
      steerLabel="立即引导"
      busy={false}
      disabled={false}
    />,
  );
  expect(screen.getByRole('button', { name: '发送' })).toHaveAttribute('title', '发送消息');
  expect(screen.queryByRole('button', { name: '立即引导' })).not.toBeInTheDocument();
});
