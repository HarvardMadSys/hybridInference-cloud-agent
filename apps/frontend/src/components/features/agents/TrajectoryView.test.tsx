// @vitest-environment jsdom
import '@testing-library/jest-dom/vitest';

import { act, cleanup, fireEvent, render, screen, within } from '@testing-library/react';
import { useMemo, useState } from 'react';
import { afterEach, describe, expect, it, vi } from 'vitest';

import type { AgentThreadEventApi } from '@/lib/api/agents';
import { TrajectoryView } from './TrajectoryView';
import { buildTrajectorySteps, type StoredEvent } from './trajectorySteps';

afterEach(() => {
  cleanup();
  vi.useRealTimers();
});

/** A pointer event built from a mouse event, for a jsdom without PointerEvent. */
function fallbackPointer(type: string, init: PointerEventInit): PointerEvent {
  const event = new MouseEvent(type, { bubbles: true, cancelable: true });
  for (const [key, value] of Object.entries(init)) {
    Object.defineProperty(event, key, { value, configurable: true });
  }
  return event as PointerEvent;
}

function event(over: Partial<AgentThreadEventApi> & { id: number }): AgentThreadEventApi {
  return {
    attempt_id: 1,
    seq: over.id,
    event_type: 'message',
    payload: {},
    created_at: '2026-08-19T10:00:00.000Z',
    job_id: 'ajob_1',
    turn_no: 1,
    ...over,
  } as AgentThreadEventApi;
}

const EVENTS = [
  event({
    id: 1,
    event_type: 'tool_use',
    payload: { id: 't1', name: 'Bash', input: { command: 'cargo build' } },
    created_at: '2026-08-19T10:00:00.000Z',
  }),
  event({
    id: 2,
    event_type: 'tool_result',
    payload: { tool_use_id: 't1', content: 'Compiling hybridinference' },
    created_at: '2026-08-19T10:00:15.000Z',
  }),
  event({ id: 3, event_type: 'message', payload: { text: 'the build passes' } }),
];

/** Query and selection live in the page, so the test drives them the same way. */
function Harness({
  events,
  initialQuery = '',
  onOpenRun = vi.fn(),
  onSelectStep,
}: {
  events: StoredEvent[];
  initialQuery?: string;
  onOpenRun?: (jobId: string, turnNo: number) => void;
  onSelectStep?: (key: string) => void;
}) {
  const [query, setQuery] = useState(initialQuery);
  const [selected, setSelected] = useState<string | null>(null);
  // Memoized the way the page memoizes them: a query change must not rebuild
  // the steps, or an effect that depends on them would hide the difference
  // between "the list changed" and "the filter changed".
  const steps = useMemo(() => buildTrajectorySteps(events), [events]);
  return (
    <TrajectoryView
      events={events}
      steps={steps}
      query={query}
      onQueryChange={setQuery}
      selectedKey={selected}
      onSelectStep={(key) => {
        setSelected(key);
        onSelectStep?.(key);
      }}
      onOpenRun={onOpenRun}
    />
  );
}

describe('TrajectoryView', () => {
  it('reads as user, assistant tool request, tool input and output with a recorded hierarchy', () => {
    const events = [
      event({
        id: 1,
        event_type: 'raw',
        payload: {
          text: JSON.stringify({
            method: 'item/completed',
            params: {
              item: {
                id: 'u',
                type: 'userMessage',
                content: [{ type: 'text', text: 'Summarize this repo' }],
              },
            },
          }),
        },
      }),
      event({
        id: 2,
        event_type: 'tool_use',
        payload: {
          id: 'c',
          name: 'commandExecution',
          input: 'cat README.md',
          usage_id: 'message-with-tool',
        },
      }),
      event({
        id: 3,
        event_type: 'tool_result',
        payload: { tool_use_id: 'c', content: 'Cloud Agent control plane' },
      }),
      event({
        id: 4,
        event_type: 'message',
        payload: { text: 'The repository runs coding agents.' },
      }),
    ];
    render(<Harness events={events} />);
    expect(screen.getByText('Summarize this repo')).toBeInTheDocument();
    const request = screen.getByRole('button', { name: 'assistant' });
    const tool = screen.getByText('Shell').closest('button')!;
    expect(within(tool).getByText('cat README.md')).toBeInTheDocument();
    expect(within(tool).getByText('→')).toBeInTheDocument();
    expect(within(tool).getByText('Cloud Agent control plane')).toBeInTheDocument();
    expect(tool).toHaveAttribute('data-parent-key', request.dataset.stepKey);
    expect(tool).toHaveClass('pl-5');
    expect(request.querySelector('[data-role-slot]')).toHaveClass('w-[76px]', 'justify-end');
    expect(tool.querySelector('[data-role-slot]')).toHaveClass('w-[76px]', 'justify-end');
    expect(screen.queryByText('Assistant message')).not.toBeInTheDocument();
    expect(screen.getByTestId('trajectory-overview-toolbar')).toHaveClass('sticky', 'top-0');
    fireEvent.change(screen.getByLabelText('Search steps'), { target: { value: 'control plane' } });
    expect(screen.getByText('Shell')).toBeInTheDocument();
    expect(screen.getByText('1 of 4')).toBeInTheDocument();
  });

  it('keeps a runtime-only attempt out of the steps and says where its records are', () => {
    render(
      <Harness
        events={[
          event({ id: 1, event_type: 'lifecycle', payload: { phase: 'workspace_preparing' } }),
        ]}
      />,
    );
    expect(screen.getByText('0 steps')).toBeInTheDocument();
    expect(screen.getByText(/No execution steps recorded yet/)).toBeInTheDocument();
    expect(screen.getByText(/Runtime records for a turn are in Run details/)).toBeInTheDocument();
    expect(screen.queryByText(/workspace_preparing/)).not.toBeInTheDocument();
  });

  it('keeps protocol chatter from inflating the overview and step list', () => {
    const notifications = Array.from({ length: 400 }, (_, index) =>
      event({
        id: index + 10,
        event_type: 'raw',
        payload: {
          text: JSON.stringify({ method: 'item/agentMessage/delta', params: { delta: 'x' } }),
        },
      }),
    );
    render(<Harness events={[...notifications, ...EVENTS]} />);

    expect(screen.getByText('2 steps')).toBeInTheDocument();
    expect(screen.queryByText(/runtime record/)).not.toBeInTheDocument();
    expect(
      screen.getAllByRole('button').filter((button) => button.hasAttribute('data-step-key')),
    ).toHaveLength(2);
    expect(screen.getByText('Bash')).toBeInTheDocument();
    expect(screen.getByText('the build passes')).toBeInTheDocument();
  });

  it('keeps Claude counters out of the Other lane and out of the step list', () => {
    const counters = [10, 11].map((id) =>
      event({
        id,
        event_type: 'raw',
        payload: {
          text: JSON.stringify({
            type: 'system',
            subtype: 'thinking_tokens',
            estimated_tokens: id,
          }),
          reason: "unclassified system subtype 'thinking_tokens'",
        },
      }),
    );
    render(<Harness events={[...counters, ...EVENTS]} />);
    expect(screen.getByText('2 steps')).toBeInTheDocument();
    expect(screen.queryByText('Other')).not.toBeInTheDocument();
    expect(screen.queryByText('runtime')).not.toBeInTheDocument();
  });

  it('lists every stored event as a step, including the one chat folds away', () => {
    render(<Harness events={EVENTS} />);

    expect(screen.getByText('2 steps')).toBeInTheDocument();
    // The card counts what it shows; the stored stream is the download button's
    // business, and its hover text says only that.
    expect(screen.getByRole('button', { name: 'Session log' })).toHaveAttribute(
      'title',
      'Download session log',
    );
    expect(screen.getByText('Bash')).toBeInTheDocument();
    expect(screen.getByText('the build passes')).toBeInTheDocument();
  });

  it('selects a step without opening an inline panel', () => {
    const onSelect = vi.fn();
    render(<Harness events={EVENTS} onSelectStep={onSelect} />);

    fireEvent.click(screen.getByText('Bash'));

    expect(onSelect).toHaveBeenCalledTimes(1);
    // The detail lives in the right-hand inspector, not under the row.
    expect(screen.queryByText('Payload')).toBeNull();
    expect(screen.getByText('Bash').closest('button')).toHaveAttribute('aria-current', 'true');
  });

  it('filters to the steps a query matches, and says so', () => {
    render(<Harness events={EVENTS} />);

    fireEvent.change(screen.getByLabelText('Search steps'), { target: { value: 'cargo' } });

    expect(screen.getByText('1 of 2')).toBeInTheDocument();
    expect(screen.queryByText('the build passes')).toBeNull();
  });

  it('says plainly when a query matches nothing', () => {
    render(<Harness events={EVENTS} />);

    fireEvent.change(screen.getByLabelText('Search steps'), { target: { value: 'zzz' } });

    expect(screen.getByText(/No step matches/)).toBeInTheDocument();
  });

  it('offers the stored stream as a download rather than a rendering of it', () => {
    const created = vi.fn(() => 'blob:x');
    vi.stubGlobal('URL', { createObjectURL: created, revokeObjectURL: vi.fn() });
    const click = vi
      .spyOn(HTMLAnchorElement.prototype, 'click')
      .mockImplementation(() => undefined);

    render(<Harness events={EVENTS} />);
    fireEvent.click(screen.getByRole('button', { name: 'Session log' }));

    expect(created).toHaveBeenCalledTimes(1);
    expect(click).toHaveBeenCalledTimes(1);
    click.mockRestore();
    vi.unstubAllGlobals();
  });

  it('gives the minimap one mark per step, so a long turn has a shape', () => {
    render(<Harness events={EVENTS} />);

    const marks = screen.getAllByRole('button', { name: /, step \d+\)$/ });

    expect(marks).toHaveLength(2);
  });

  it('selects a step when its minimap mark is clicked', () => {
    render(<Harness events={EVENTS} />);

    const mark = screen.getByRole('button', { name: 'Bash (tool, step 1)' });
    expect(mark).toHaveAttribute('aria-pressed', 'false');

    fireEvent.click(mark);

    expect(mark).toHaveAttribute('aria-pressed', 'true');
    expect(
      screen.getByRole('button', { name: 'Assistant message (assistant, step 2)' }),
    ).toHaveAttribute('aria-pressed', 'false');
  });

  it('shows the measured call duration on the row without inventing one', () => {
    render(<Harness events={EVENTS} />);

    const row = screen.getByText('Bash').closest('button') as HTMLElement;
    expect(within(row).getByText('15s')).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Bash (tool, step 1)' }).getAttribute('title')).toBe(
      'Bash (tool) · 15s',
    );
  });

  it('offers Run details for each turn and reports which turn it belongs to', () => {
    const onOpenRun = vi.fn();
    const stored = [
      event({ id: 1, job_id: 'ajob_0', turn_no: 1, payload: { text: 'first turn' } }),
      event({ id: 2, job_id: 'ajob_1', turn_no: 2, payload: { text: 'second turn' } }),
    ];
    render(<Harness events={stored} onOpenRun={onOpenRun} />);

    const buttons = screen.getAllByRole('button', { name: 'Run details ›' });
    expect(buttons).toHaveLength(2);

    fireEvent.click(buttons[0]);

    expect(onOpenRun).toHaveBeenCalledWith('ajob_0', 1);
  });

  it('explains an empty stream rather than rendering a bare panel', () => {
    render(<Harness events={[]} />);

    expect(screen.getByText(/stored no events/)).toBeInTheDocument();
  });

  it('shows a raw row, which is all a Tier-2 runtime produces', () => {
    render(<Harness events={[event({ id: 9, event_type: 'raw', payload: { line: 'x=1' } })]} />);

    const row = screen.getByText('raw').closest('button');
    expect(within(row as HTMLElement).getByText('x=1')).toBeInTheDocument();
  });

  it('scrolls to a new selection without dragging a reader back on later events', () => {
    const steps = buildTrajectorySteps(EVENTS);
    const props = {
      query: '',
      onQueryChange: vi.fn(),
      selectedKey: steps[0].key,
      onSelectStep: vi.fn(),
      onOpenRun: vi.fn(),
    };
    const view = render(<TrajectoryView {...props} events={EVENTS} steps={steps} />);
    const selected = screen.getByText('Bash').closest('button') as HTMLElement;
    const moved = screen.getByText('the build passes').closest('button') as HTMLElement;
    const firstScroll = vi.fn();
    const secondScroll = vi.fn();
    selected.scrollIntoView = firstScroll;
    moved.scrollIntoView = secondScroll;

    // A selection made from the minimap, a hierarchy link or a chat footer has
    // to reach the row it names.
    view.rerender(
      <TrajectoryView {...props} selectedKey={steps[1].key} events={EVENTS} steps={steps} />,
    );
    expect(secondScroll).toHaveBeenCalledTimes(1);

    // A live event appends a row; the reader keeps the place they scrolled to.
    const grown = [...EVENTS, event({ id: 4, payload: { text: 'and still passing' } })];
    view.rerender(
      <TrajectoryView
        {...props}
        selectedKey={steps[1].key}
        events={grown}
        steps={buildTrajectorySteps(grown)}
      />,
    );
    expect(secondScroll).toHaveBeenCalledTimes(1);
    expect(firstScroll).not.toHaveBeenCalled();
  });
});

/** Eight distinct assistant messages: eight steps, with no pairing to reason about. */
const MANY = Array.from({ length: 8 }, (_, index) =>
  event({ id: index + 1, payload: { text: `step ${index + 1}` } }),
);

/** A call with a recorded duration, answered a moment after it finished. */
const TIMED_REPLY = [
  event({
    id: 1,
    event_type: 'tool_use',
    payload: { id: 't1', name: 'Bash', input: { command: 'cargo build' } },
    created_at: '2026-08-19T10:00:00.000Z',
  }),
  event({
    id: 2,
    event_type: 'tool_result',
    payload: { tool_use_id: 't1', content: 'Compiling' },
    created_at: '2026-08-19T10:00:15.000Z',
  }),
  event({
    id: 3,
    payload: { text: 'the build passes' },
    created_at: '2026-08-19T10:00:16.000Z',
  }),
];

describe('TrajectoryView overview', () => {
  /** The projected layer: its width against the track is the zoom. */
  function domain(): HTMLElement {
    const node = document.querySelector<HTMLElement>('[data-timeline-domain]');
    if (node === null) throw new Error('the overview rendered no domain layer');
    return node;
  }

  function track(): HTMLElement {
    return screen.getByRole('group', { name: /execution overview/i });
  }

  /** jsdom lays nothing out, so the track is given the width a card would have. */
  function withTrackWidth(width = 400): void {
    track().getBoundingClientRect = () =>
      ({ left: 0, right: width, top: 0, bottom: 40, width, height: 40, x: 0, y: 0 }) as DOMRect;
  }

  /** Dispatch a wheel step and report whether the overview took it. */
  function wheel(deltaY: number): boolean {
    const event = new WheelEvent('wheel', { deltaY, bubbles: true, cancelable: true });
    act(() => {
      track().dispatchEvent(event);
    });
    return event.defaultPrevented;
  }

  /** A pointer event, whether or not the environment has a PointerEvent class. */
  function pointer(type: string, init: PointerEventInit): void {
    const event =
      typeof PointerEvent === 'function'
        ? new PointerEvent(type, { bubbles: true, cancelable: true, ...init })
        : fallbackPointer(type, init);
    act(() => {
      track().dispatchEvent(event);
    });
  }

  /** A left-button drag across the track, in client pixels. */
  function drag(fromX: number, toX: number): void {
    pointer('pointerdown', { button: 0, pointerId: 1, clientX: fromX, clientY: 10 });
    pointer('pointermove', { pointerId: 1, clientX: toX, clientY: 10 });
    pointer('pointerup', { pointerId: 1, clientX: toX, clientY: 10 });
  }

  it('projects the whole run into the track while nothing is zoomed', () => {
    render(<Harness events={EVENTS} />);

    expect(domain().style.left).toBe('0%');
    expect(domain().style.width).toBe('100%');
  });

  it('gives every step its own slot, so marks meet and zoom widens them', () => {
    render(<Harness events={MANY} />);
    withTrackWidth();
    const mark = screen.getByRole('button', { name: 'Assistant message (assistant, step 1)' });

    // One operation out of eight: the mark owns its whole slot, and the hairline
    // against its neighbour is the transparent border, not a gap in the layout.
    expect(mark.style.width).toBe('12.5%');
    expect(mark.style.minWidth).toBe('2px');
    expect(mark.className).toContain('bg-clip-padding');

    wheel(-100);

    // The projection scales, and the mark keeps its share of it, so the mark
    // itself gets wider rather than being left behind at a fixed size.
    expect(Number.parseFloat(domain().style.width)).toBeGreaterThan(100);
    expect(mark.style.width).toBe('12.5%');
  });

  it('gives the wheel to the page when there is nothing left to zoom out to', () => {
    render(<Harness events={MANY} />);

    expect(wheel(100)).toBe(false);
    expect(domain().style.width).toBe('100%');
  });

  it('zooms in around the pointer, and back out to the whole run', () => {
    render(<Harness events={MANY} />);

    expect(wheel(-100)).toBe(true);
    expect(Number.parseFloat(domain().style.width)).toBeGreaterThan(100);

    expect(wheel(100)).toBe(true);
    expect(domain().style.width).toBe('100%');
  });

  it('selects an interval on a drag and steps the rows outside it back', () => {
    const { container } = render(<Harness events={MANY} />);
    withTrackWidth();

    drag(0, 100);

    expect(container.querySelectorAll('[data-timeline-focus="inside"]').length).toBeGreaterThan(0);
    expect(container.querySelectorAll('[data-timeline-focus="outside"]').length).toBeGreaterThan(0);
    expect(screen.getByRole('button', { name: /of 8 · Clear/ })).toBeInTheDocument();
  });

  it('clears the interval on Escape, and from the control that counts it', () => {
    const { container } = render(<Harness events={MANY} />);
    withTrackWidth();

    drag(0, 100);
    expect(container.querySelector('[data-timeline-focus="outside"]')).not.toBeNull();

    fireEvent.keyDown(track(), { key: 'Escape' });
    expect(container.querySelector('[data-timeline-focus="outside"]')).toBeNull();

    drag(0, 100);
    fireEvent.click(screen.getByRole('button', { name: /of 8 · Clear/ }));
    expect(container.querySelector('[data-timeline-focus="outside"]')).toBeNull();
  });

  it('draws a boundary where a turn starts', () => {
    const twoTurns = [
      event({ id: 1, job_id: 'ajob_0', turn_no: 1, payload: { text: 'first turn' } }),
      event({ id: 2, job_id: 'ajob_1', turn_no: 2, payload: { text: 'second turn' } }),
    ];
    const { container } = render(<Harness events={twoTurns} />);

    expect(container.querySelectorAll('[data-turn-boundary]')).toHaveLength(2);
  });

  it('projects recorded time when the reader asks for it', () => {
    render(<Harness events={EVENTS} />);

    const steps = screen.getByRole('button', { name: 'Steps' });
    const time = screen.getByRole('button', { name: 'Time' });
    expect(steps).toHaveAttribute('aria-pressed', 'true');

    fireEvent.click(time);

    expect(time).toHaveAttribute('aria-pressed', 'true');
    expect(steps).toHaveAttribute('aria-pressed', 'false');
    // A recorded interval is as wide as it was long, so the mark is a share of
    // the domain rather than one operation. (The width itself is a `max()`
    // expression jsdom's CSS parser drops, hence the data attribute.)
    const bash = screen.getByRole('button', { name: 'Bash (tool, step 1)' });
    expect(bash).toHaveAttribute('data-recorded-width', 'true');
    expect(bash).not.toHaveAttribute('data-equal-duration');
  });

  it('says so when the run recorded no timing to project', () => {
    render(<Harness events={MANY.map((item) => ({ ...item, created_at: '' }))} />);

    expect(screen.getByRole('button', { name: 'Time' })).toBeDisabled();
  });

  it('keeps the step a mark names selected while the window moves', () => {
    render(<Harness events={MANY} />);
    const mark = screen.getByRole('button', { name: 'Assistant message (assistant, step 1)' });

    fireEvent.click(mark);
    wheel(-100);

    expect(mark).toHaveAttribute('aria-pressed', 'true');
  });

  it('pans from the window at pointer-down, never from the last move', () => {
    render(<Harness events={MANY} />);
    withTrackWidth();
    wheel(-100);

    pointer('pointerdown', { button: 2, pointerId: 1, clientX: 200, clientY: 10 });
    pointer('pointermove', { pointerId: 1, clientX: 220, clientY: 10 });
    const panned = domain().style.left;
    // The same coordinate again is no further movement, not the same
    // displacement applied a second time.
    pointer('pointermove', { pointerId: 1, clientX: 220, clientY: 10 });

    expect(panned).not.toBe('-0%');
    expect(domain().style.left).toBe(panned);
  });

  it('does not carry an interval from one projection into the other', () => {
    const { container } = render(<Harness events={MANY} />);
    withTrackWidth();

    drag(0, 100);
    expect(container.querySelectorAll('[data-timeline-focus="outside"]').length).toBeGreaterThan(0);

    fireEvent.click(screen.getByRole('button', { name: 'Time' }));

    // Operations are not milliseconds: the interval that meant five steps must
    // not be read as five milliseconds.
    expect(container.querySelectorAll('[data-timeline-focus="outside"]')).toHaveLength(0);
    expect(screen.queryByRole('button', { name: /of 8 · Clear/ })).toBeNull();
  });

  it('brings a selected mark inside the right edge of the track', () => {
    render(<Harness events={MANY} />);
    withTrackWidth();
    wheel(-100);
    wheel(-100);
    const rows = document.querySelectorAll<HTMLElement>('[data-step-key]');

    fireEvent.click(rows[rows.length - 1]);

    const mark = document.querySelector<HTMLElement>('[data-span-key][aria-pressed="true"]');
    expect(mark).not.toBeNull();
    const left = Number.parseFloat(domain().style.left);
    const width = Number.parseFloat(domain().style.width);
    const markLeft = Number.parseFloat((mark as HTMLElement).style.left);
    // Where the mark lands on the track once the layer's own projection is
    // applied. On the edge it has no visible width at all.
    expect(left + markLeft * (width / 100)).toBeLessThan(100);
  });

  it('keeps the last zero-duration mark inside the track', () => {
    render(<Harness events={TIMED_REPLY} />);

    fireEvent.click(screen.getByRole('button', { name: 'Time' }));

    // The reply follows the call with no recorded duration, so once the idle
    // time is compressed it sits exactly at the end of the domain, where a mark
    // drawn rightward from that point is clipped away entirely.
    const reply = screen.getByRole('button', { name: 'Assistant message (assistant, step 2)' });
    expect(reply).toHaveAttribute('data-edge-anchored', 'true');
    expect(reply.style.transform).toBe('translateX(-100%)');
  });

  it('does not pull an unchanged selection back when a step arrives', () => {
    const { rerender } = render(<Harness events={MANY} />);
    withTrackWidth();
    wheel(-100);
    fireEvent.click(document.querySelectorAll<HTMLElement>('[data-step-key]')[0]);
    // The reader then pans somewhere else entirely.
    pointer('pointerdown', { button: 2, pointerId: 1, clientX: 200, clientY: 10 });
    pointer('pointermove', { pointerId: 1, clientX: 60, clientY: 10 });
    pointer('pointerup', { pointerId: 1, clientX: 60, clientY: 10 });
    const parked = domain().style.left;

    rerender(<Harness events={[...MANY, event({ id: 99, payload: { text: 'new step' } })]} />);

    expect(domain().style.left).toBe(parked);
  });

  it('does not scroll back to an unchanged interval when a step arrives', () => {
    const { rerender } = render(<Harness events={MANY} />);
    withTrackWidth();
    const first = document.querySelectorAll<HTMLElement>('[data-step-key]')[0];
    const scroll = vi.fn();
    first.scrollIntoView = scroll;

    drag(0, 100);
    expect(scroll).toHaveBeenCalledTimes(1);
    scroll.mockClear();

    rerender(<Harness events={[...MANY, event({ id: 99, payload: { text: 'new step' } })]} />);

    expect(scroll).not.toHaveBeenCalled();
  });

  it('keeps walking the window while the pointer is held past an edge', () => {
    vi.useFakeTimers();
    render(<Harness events={MANY} />);
    withTrackWidth();
    wheel(-100);

    pointer('pointerdown', { button: 0, pointerId: 1, clientX: 200, clientY: 10 });
    pointer('pointermove', { pointerId: 1, clientX: 410, clientY: 10 });
    const first = domain().style.left;
    act(() => {
      vi.advanceTimersByTime(500);
    });

    expect(domain().style.left).not.toBe(first);
  });

  it('brings the interval into view once the search stops hiding its first row', () => {
    render(<Harness events={MANY} />);
    withTrackWidth();
    const original = HTMLElement.prototype.scrollIntoView;
    const scroll = vi.fn();
    HTMLElement.prototype.scrollIntoView = scroll;
    try {
      // The interval covers the first steps and the search hides all of them,
      // so there is no row to bring into view yet.
      fireEvent.change(screen.getByLabelText('Search steps'), { target: { value: 'step 8' } });
      drag(0, 100);
      expect(screen.getByRole('button', { name: /of 8 · Clear/ })).toBeInTheDocument();

      scroll.mockClear();
      fireEvent.change(screen.getByLabelText('Search steps'), { target: { value: '' } });

      // Clearing the query rebuilds the visible rows, not the steps.
      expect(scroll).toHaveBeenCalled();
    } finally {
      HTMLElement.prototype.scrollIntoView = original;
    }
  });

  it('cancels the gesture in flight on Escape, edge pan included', () => {
    vi.useFakeTimers();
    const { container } = render(<Harness events={MANY} />);
    withTrackWidth();
    wheel(-100);
    drag(0, 100);
    expect(container.querySelector('[data-timeline-focus="outside"]')).not.toBeNull();

    pointer('pointerdown', { button: 0, pointerId: 1, clientX: 200, clientY: 10 });
    pointer('pointermove', { pointerId: 1, clientX: 410, clientY: 10 });
    act(() => {
      vi.advanceTimersByTime(100);
    });
    fireEvent.keyDown(track(), { key: 'Escape' });

    const parked = domain().style.left;
    expect(container.querySelector('[data-timeline-selection][data-dragging="true"]')).toBeNull();
    act(() => {
      vi.advanceTimersByTime(300);
    });
    expect(domain().style.left).toBe(parked);

    // The release must not commit the selection the reader just cancelled.
    pointer('pointerup', { pointerId: 1, clientX: 410, clientY: 10 });
    expect(container.querySelector('[data-timeline-focus="outside"]')).toBeNull();
  });

  it.each([
    ['a drag', 0],
    ['a pan', 2],
  ])('claims the Escape that cancels %s, so it cannot also stop the run', (_gesture, button) => {
    render(<Harness events={MANY} />);
    withTrackWidth();

    // Nothing is selected or zoomed yet: cancelling the gesture is all Escape does.
    pointer('pointerdown', { button, pointerId: 1, clientX: 100, clientY: 10 });
    pointer('pointermove', { pointerId: 1, clientX: 250, clientY: 10 });

    expect(fireEvent.keyDown(track(), { key: 'Escape' })).toBe(false);
  });

  it('walks into steps appended while the pointer is held at an edge', () => {
    vi.useFakeTimers();
    const { rerender } = render(<Harness events={MANY} />);
    withTrackWidth();
    wheel(-100);

    pointer('pointerdown', { button: 0, pointerId: 1, clientX: 200, clientY: 10 });
    pointer('pointermove', { pointerId: 1, clientX: 410, clientY: 10 });
    act(() => {
      vi.advanceTimersByTime(100);
    });

    const grown = [
      ...MANY,
      ...MANY.map((item, index) =>
        event({ id: index + 9, payload: { text: `new step ${index + 9}` } }),
      ),
    ];
    rerender(<Harness events={grown} />);
    act(() => {
      vi.advanceTimersByTime(4000);
    });
    pointer('pointerup', { pointerId: 1, clientX: 410, clientY: 10 });

    // A gesture that had reached the old end carries on into the steps that
    // arrived while it was held.
    expect(document.querySelectorAll('[data-timeline-focus="inside"]').length).toBeGreaterThan(8);
  });
});
