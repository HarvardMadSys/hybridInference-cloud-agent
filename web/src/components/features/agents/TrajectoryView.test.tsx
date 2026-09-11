// @vitest-environment jsdom
import '@testing-library/jest-dom/vitest';

import { act, cleanup, fireEvent, render, screen, within } from '@testing-library/react';
import { useState } from 'react';
import { afterEach, describe, expect, it, vi } from 'vitest';

import type { AgentThreadEventApi } from '@/lib/api/agents';
import { TrajectoryView } from './TrajectoryView';
import { buildTrajectorySteps, type StoredEvent } from './trajectorySteps';

afterEach(cleanup);

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
  const steps = buildTrajectorySteps(events);
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
  it('lists every stored event as a step, including the one chat folds away', () => {
    render(<Harness events={EVENTS} />);

    expect(screen.getByText('2 steps · 3 events')).toBeInTheDocument();
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

describe('TrajectoryView zoom', () => {
  /** The track is the zoomed surface: fitted it sets a minimum, zoomed a width. */
  function trackWidth(): number {
    const scroller = screen.getByRole('group', { name: /execution overview/i });
    const track = scroller.firstElementChild as HTMLElement;
    return Number.parseFloat(track.style.width || track.style.minWidth);
  }

  /** Dispatch a wheel step and report whether the overview took it. */
  function wheel(deltaY: number): boolean {
    const scroller = screen.getByRole('group', { name: /execution overview/i });
    const event = new WheelEvent('wheel', { deltaY, bubbles: true, cancelable: true });
    act(() => {
      scroller.dispatchEvent(event);
    });
    return event.defaultPrevented;
  }

  it('lays the overview out at the pitch it has always used while fitted', () => {
    render(<Harness events={EVENTS} />);
    const scroller = screen.getByRole('group', { name: /execution overview/i });
    const track = scroller.firstElementChild as HTMLElement;

    // Two steps at twelve pixels each, plus the track's padding. A minimum and
    // not a width: the card decides how wide it ends up, and the server paints
    // that same layout before anything is measured.
    expect(track.style.minWidth).toBe('30px');
    expect(track.style.width).toBe('');
  });

  it('gives the wheel to the page when there is nothing left to zoom out to', () => {
    render(<Harness events={EVENTS} />);

    expect(wheel(100)).toBe(false);
    expect(trackWidth()).toBe(30);
  });

  it('opens the columns apart on a wheel-up and closes them again', () => {
    render(<Harness events={EVENTS} />);

    expect(wheel(-100)).toBe(true);
    const zoomed = trackWidth();
    expect(zoomed).toBeGreaterThan(30);

    expect(wheel(100)).toBe(true);
    expect(trackWidth()).toBe(30);
  });

  it('steps with the buttons, and fits every step again', () => {
    render(<Harness events={EVENTS} />);
    const zoomIn = screen.getByRole('button', { name: 'Zoom in' });
    const zoomOut = screen.getByRole('button', { name: 'Zoom out' });
    const fit = screen.getByRole('button', { name: 'Fit every step' });

    expect(zoomOut).toBeDisabled();
    expect(fit).toBeDisabled();

    fireEvent.click(zoomIn);
    const zoomed = trackWidth();
    expect(zoomed).toBeGreaterThan(30);
    expect(zoomOut).toBeEnabled();
    expect(fit).toBeEnabled();

    fireEvent.click(zoomIn);
    expect(trackWidth()).toBeGreaterThan(zoomed);

    fireEvent.click(fit);
    expect(trackWidth()).toBe(30);
    expect(zoomOut).toBeDisabled();
  });

  it('fits every step again on Escape and on a double click', () => {
    render(<Harness events={EVENTS} />);
    const scroller = screen.getByRole('group', { name: /execution overview/i });

    fireEvent.click(screen.getByRole('button', { name: 'Zoom in' }));
    expect(trackWidth()).toBeGreaterThan(30);
    fireEvent.keyDown(scroller, { key: 'Escape' });
    expect(trackWidth()).toBe(30);

    fireEvent.click(screen.getByRole('button', { name: 'Zoom in' }));
    expect(trackWidth()).toBeGreaterThan(30);
    fireEvent.doubleClick(scroller);
    expect(trackWidth()).toBe(30);
  });

  it('keeps the step a mark names selected while the columns move', () => {
    render(<Harness events={EVENTS} />);
    const mark = screen.getByRole('button', { name: 'Bash (tool, step 1)' });

    fireEvent.click(mark);
    fireEvent.click(screen.getByRole('button', { name: 'Zoom in' }));

    expect(mark).toHaveAttribute('aria-pressed', 'true');
  });
});
