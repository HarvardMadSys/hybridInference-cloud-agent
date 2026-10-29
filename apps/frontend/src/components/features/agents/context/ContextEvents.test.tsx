// @vitest-environment jsdom
import '@testing-library/jest-dom/vitest';
import { cleanup, fireEvent, render, screen, within } from '@testing-library/react';
import { afterEach, expect, it, vi } from 'vitest';

import type { ContextEventApi, ThreadContextApi } from '@/lib/api/agents';
import { ContextEvents } from './ContextEvents';
import { eventSourceKey, supportReasonKey } from './contextEventsPresent';

afterEach(cleanup);

function rowsOf(): HTMLElement[] {
  return within(screen.getByRole('list', { name: 'Context events' })).getAllByRole('listitem');
}

function event(overrides: Partial<ContextEventApi>): ContextEventApi {
  return {
    kind: 'injection',
    source: 'platform_memory',
    label: null,
    names: [],
    items: null,
    job_id: 'j1',
    turn_no: 1,
    attempt_id: 1,
    seq: 1,
    event_id: 11,
    at: '2026-10-29T10:00:00Z',
    chars: null,
    tokens: null,
    tokens_basis: null,
    before_tokens: null,
    after_tokens: null,
    from_value: null,
    to_value: null,
    superseded: false,
    copied: false,
    ...overrides,
  };
}

const EVENTS: ContextEventApi[] = [
  event({ items: 2, chars: 120, tokens: 30, tokens_basis: 'estimate' }),
  event({ source: 'platform_memory', superseded: true, attempt_id: 0, seq: 1 }),
  event({
    kind: 'compaction',
    source: 'cli_compact_boundary',
    label: 'auto',
    seq: 5,
    event_id: 15,
    before_tokens: 90_000,
    after_tokens: 9_000,
    tokens: -81_000,
    tokens_basis: 'cli',
  }),
  event({ kind: 'compaction', source: 'codex_context_compaction', seq: 6, event_id: 16 }),
  event({
    kind: 'model_switch',
    source: 'turn_settings',
    job_id: 'j2',
    turn_no: 2,
    attempt_id: null,
    seq: null,
    event_id: null,
    from_value: 'm1',
    to_value: 'm2',
  }),
  event({
    kind: 'mode',
    source: 'claude_plan_tool',
    job_id: 'j2',
    turn_no: 2,
    seq: 3,
    event_id: 23,
    to_value: 'plan_entered',
  }),
];

function view(): ThreadContextApi {
  return {
    events: EVENTS,
    event_counts: { injection: 1, compaction: 2, prune: 0, model_switch: 1, mode: 1 },
    events_omitted: 3,
    event_capabilities: [
      {
        runtime: 'claude-code',
        kind: 'prune',
        support: 'not_reported',
        reason: 'claude_microcompact_unrecorded',
      },
      { runtime: 'claude-code', kind: 'compaction', support: 'reported', reason: null },
    ],
  } as unknown as ThreadContextApi;
}

it('lists every kind with its count and filters to one kind', () => {
  render(
    <ContextEvents
      view={view()}
      nowMs={Date.parse('2026-10-29T12:00:00Z')}
      onOpenPoint={vi.fn()}
    />,
  );
  const filters = screen.getByRole('group', { name: 'Show events of one kind' });
  expect(within(filters).getByRole('button', { name: /All 5/ })).toHaveAttribute(
    'aria-pressed',
    'true',
  );
  expect(within(filters).getByRole('button', { name: /Prunes 0/ })).toBeInTheDocument();
  expect(rowsOf()).toHaveLength(EVENTS.length);

  fireEvent.click(within(filters).getByRole('button', { name: /Compactions 2/ }));
  const rows = rowsOf();
  expect(rows).toHaveLength(2);
  expect(rows[0]).toHaveTextContent('The CLI reported 90K → 9,000 tokens');
  // A compaction whose CLI gave no sizes says so; no size is made up.
  expect(rows[1]).toHaveTextContent('The CLI reported no sizes.');

  fireEvent.click(within(filters).getByRole('button', { name: /Prunes 0/ }));
  expect(screen.getByText('None of this kind are recorded.')).toBeInTheDocument();
  expect(screen.getByText('3 more events are counted but not listed.')).toBeInTheDocument();
});

it('marks estimates, retried attempts, and changes, and opens the step or turn', () => {
  const onOpenPoint = vi.fn();
  render(
    <ContextEvents
      view={view()}
      nowMs={Date.parse('2026-10-29T12:00:00Z')}
      onOpenPoint={onOpenPoint}
    />,
  );
  const [injection, retried, , , modelSwitch, mode] = rowsOf();
  expect(injection).toHaveTextContent('Platform: memory');
  expect(injection).toHaveTextContent('≈30 tokens, estimated from 120 characters');
  expect(retried).toHaveTextContent('From an attempt that was retried; not counted.');
  // An injection whose size the platform cannot see is not given one.
  expect(retried).toHaveTextContent('Its size is not visible to the platform');
  expect(modelSwitch).toHaveTextContent('m1 → m2');
  expect(mode).toHaveTextContent('Now Entered plan mode');

  fireEvent.click(within(injection).getByRole('button', { name: 'Open step' }));
  expect(onOpenPoint).toHaveBeenLastCalledWith({ jobId: 'j1', turnNo: 1, eventId: 11 });
  // A turn-level change has no stored event: it opens the turn.
  fireEvent.click(within(modelSwitch).getByRole('button', { name: 'Open turn' }));
  expect(onOpenPoint).toHaveBeenLastCalledWith({ jobId: 'j2', turnNo: 2, eventId: null });
});

it('says why a runtime cannot show a kind of event', () => {
  render(<ContextEvents view={view()} nowMs={0} onOpenPoint={vi.fn()} />);
  fireEvent.click(screen.getByText('What each agent can show'));
  expect(
    screen.getByText(
      /Claude Code clears old tool results inside the CLI and writes no record of it/,
    ),
  ).toBeInTheDocument();
  // Only the kinds that are not fully shown are explained.
  expect(screen.queryByText(/claude-code · Compactions/)).not.toBeInTheDocument();
});

it('names known producers and reasons, and falls back for unknown ones', () => {
  expect(eventSourceKey('platform_goal')).toBe('context.events.source.platform_goal');
  expect(eventSourceKey('something_new')).toBeNull();
  expect(supportReasonKey('opencode_prune_off')).toBe('context.events.reason.opencode_prune_off');
  expect(supportReasonKey('nonsense')).toBe('context.events.reason.unknown_runtime');
});
