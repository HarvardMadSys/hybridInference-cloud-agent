// @vitest-environment jsdom
import '@testing-library/jest-dom/vitest';
import { cleanup, fireEvent, render, screen, within } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';

import { LocaleProvider } from '@/components/providers/LocaleProvider';
import type {
  ContextCompactionApi,
  ContextEventApi,
  ContextPointApi,
  ThreadContextApi,
} from '@/lib/api/agents';
import { LOCALE_STORAGE_KEY } from '@/lib/i18n/locales';
import { ContextEvents } from './ContextEvents';
import {
  compactionRequest,
  eventSourceKey,
  mergeCompactions,
  supportReasonKey,
} from './contextEventsPresent';

afterEach(() => {
  cleanup();
  window.localStorage.clear();
});

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
  expect(rows[0]).toHaveTextContent('Trigger: Automatic');
  expect(rows[0]).toHaveTextContent('The CLI reported 90K → 9,000 tokens');
  // No trigger reported: said so, not guessed.
  expect(rows[1]).toHaveTextContent('Trigger not reported');
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
  expect(mode).toHaveTextContent('Entered plan mode');
  expect(mode).not.toHaveTextContent('Now');

  // What the platform added at claim is no trajectory step: it opens the turn.
  fireEvent.click(within(injection).getByRole('button', { name: 'Open turn' }));
  expect(onOpenPoint).toHaveBeenLastCalledWith({ jobId: 'j1', turnNo: 1, eventId: null });
  fireEvent.click(within(mode).getByRole('button', { name: 'Open step' }));
  expect(onOpenPoint).toHaveBeenLastCalledWith({ jobId: 'j2', turnNo: 2, eventId: 23 });
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

function compaction(overrides: Partial<ContextCompactionApi> = {}): ContextCompactionApi {
  return {
    job_id: 'j1',
    turn_no: 1,
    attempt_id: 1,
    seq: 5,
    event_id: 15,
    at: '2026-10-29T10:05:00Z',
    source: 'cli_compact_boundary',
    trigger: 'auto',
    pre_tokens: 90_000,
    post_tokens: 9_000,
    completed: true,
    observed_before: 91_000,
    observed_after: 12_000,
    ...overrides,
  };
}

function request(index: number, overrides: Partial<ContextPointApi> = {}): ContextPointApi {
  return {
    index,
    job_id: 'j1',
    turn_no: 1,
    attempt_id: 1,
    seq: index * 2 + 1,
    event_id: 200 + index,
    at: null,
    prompt_tokens: 1000,
    cache_read_tokens: null,
    cache_write_tokens: null,
    uncached_input_tokens: null,
    output_tokens: null,
    context_window: null,
    model: null,
    delta_tokens: null,
    after_compaction: false,
    ...overrides,
  };
}

// Requests at seq 1, 3, 5 and 7; the last follows a compaction.
const REQUESTS = [0, 1, 2, 3].map((i) => request(i, { after_compaction: i === 3 }));

describe('compactions in the events', () => {
  it('joins a compaction event to the view’s record of it, never listing it twice', () => {
    const merged = mergeCompactions(EVENTS, [compaction()]);
    expect(merged.items).toHaveLength(EVENTS.length);
    const joined = merged.items.find((item) => item.event.event_id === 15)!;
    expect(joined.compaction?.observed_before).toBe(91_000);
    // The codex compaction has no record in the view: still listed, without one.
    expect(merged.items.find((item) => item.event.event_id === 16)!.compaction).toBeNull();
    expect(merged.compactions).toBe(2);
  });

  it('lists a recorded compaction no event carries, once, in conversation order', () => {
    const extra = compaction({ job_id: 'j2', turn_no: 2, attempt_id: 1, seq: 2, event_id: 40 });
    const merged = mergeCompactions(EVENTS, [compaction(), extra]);
    expect(merged.items).toHaveLength(EVENTS.length + 1);
    expect(merged.compactions).toBe(3);
    const at = merged.items.findIndex((item) => item.event.event_id === 40);
    // After turn 2's turn-level model switch, before its plan-mode step at seq 3.
    expect(merged.items[at - 1].event.kind).toBe('model_switch');
    expect(merged.items[at + 1].event.kind).toBe('mode');
    expect(merged.items[at].event).toMatchObject({
      kind: 'compaction',
      label: 'auto',
      before_tokens: 90_000,
      after_tokens: 9_000,
      tokens_basis: 'cli',
    });
    // The same evidence recorded twice is listed once.
    const twice = mergeCompactions(EVENTS, [compaction(), compaction(), extra, { ...extra }]);
    expect(twice.items).toHaveLength(EVENTS.length + 1);
    expect(twice.compactions).toBe(3);
  });

  it('does not make a compaction out of a request’s drop', () => {
    expect(mergeCompactions([], []).items).toHaveLength(0);
    expect(mergeCompactions([], []).compactions).toBe(0);
  });

  it('finds the request a compaction is shown at on the trend', () => {
    // The first request after it in its attempt.
    expect(compactionRequest(REQUESTS, { job_id: 'j1', turn_no: 1, attempt_id: 1, seq: 6 })).toBe(
      REQUESTS[3],
    );
    // After the attempt's last request: the next one, else that last one.
    expect(
      compactionRequest(REQUESTS.slice(0, 3), {
        job_id: 'j1',
        turn_no: 1,
        attempt_id: 1,
        seq: 9,
      }),
    ).toBe(REQUESTS[2]);
    // No request in its attempt: the first of a later turn.
    const later = [...REQUESTS, request(4, { job_id: 'j2', turn_no: 2, attempt_id: 2 })];
    expect(compactionRequest(later, { job_id: 'j1', turn_no: 1, attempt_id: 9, seq: 1 })).toBe(
      later[4],
    );
    expect(compactionRequest([], { job_id: 'j1', turn_no: 1, attempt_id: 1, seq: 1 })).toBeNull();
  });

  it('lists an older recording’s compactions with both sizes, and selects their request', () => {
    const onSelect = vi.fn();
    const onOpenPoint = vi.fn();
    const older = {
      ...view(),
      events: [],
      event_counts: {},
      events_omitted: 0,
      requests: REQUESTS,
      compactions: [
        compaction({ seq: 6, event_id: 16, trigger: 'manual', post_tokens: null }),
        // The same record again: one row.
        compaction({ seq: 6, event_id: 16, trigger: 'manual', post_tokens: null }),
        compaction({
          seq: 8,
          event_id: null,
          trigger: null,
          pre_tokens: null,
          completed: null,
          observed_after: null,
        }),
      ],
    } as unknown as ThreadContextApi;
    render(<ContextEvents view={older} nowMs={0} onOpenPoint={onOpenPoint} onSelect={onSelect} />);
    const filters = screen.getByRole('group', { name: 'Show events of one kind' });
    expect(within(filters).getByRole('button', { name: /Compactions 2/ })).toBeInTheDocument();
    expect(within(filters).getByRole('button', { name: /All 2/ })).toBeInTheDocument();
    const [manual, unknown] = rowsOf();
    expect(manual).toHaveTextContent('Trigger: Manual');
    expect(manual).toHaveTextContent('The CLI reported 90K tokens before');
    expect(manual).toHaveTextContent('Requests observed around it: 91K → 12K');
    expect(unknown).toHaveTextContent('Trigger not reported');
    expect(unknown).toHaveTextContent('The CLI reported no sizes.');
    expect(unknown).toHaveTextContent('Requests observed around it: 91K → —');
    expect(unknown).toHaveTextContent('whether it finished is not known');

    fireEvent.click(within(manual).getByRole('button', { name: 'Show in trend' }));
    expect(onSelect).toHaveBeenLastCalledWith(3);
    fireEvent.click(within(manual).getByRole('button', { name: 'Open step' }));
    expect(onOpenPoint).toHaveBeenLastCalledWith({ jobId: 'j1', turnNo: 1, eventId: 16 });
    // No stored event recorded for it: its turn opens.
    fireEvent.click(within(unknown).getByRole('button', { name: 'Open turn' }));
    expect(onOpenPoint).toHaveBeenLastCalledWith({ jobId: 'j1', turnNo: 1, eventId: null });
  });

  it('names compactions in Chinese', () => {
    window.localStorage.setItem(LOCALE_STORAGE_KEY, 'zh-CN');
    render(
      <LocaleProvider>
        <ContextEvents
          view={{ ...view(), requests: REQUESTS, compactions: [compaction()] } as ThreadContextApi}
          nowMs={0}
          onOpenPoint={vi.fn()}
          onSelect={vi.fn()}
        />
      </LocaleProvider>,
    );
    expect(screen.getByRole('heading', { name: '上下文事件' })).toBeInTheDocument();
    const list = screen.getByRole('list', { name: '上下文事件' });
    const row = within(list)
      .getAllByRole('listitem')
      .find((item) => item.textContent?.includes('91K → 12K'))!;
    expect(row).toHaveTextContent('触发方式：自动');
    expect(within(row).getByRole('button', { name: '在趋势中查看' })).toBeInTheDocument();
  });
});
