// @vitest-environment jsdom
import '@testing-library/jest-dom/vitest';

import { cleanup, fireEvent, render, screen, within } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';

import type { AgentJobEventApi } from '@/lib/api/agents';
import { TrajectoryView } from './TrajectoryView';

afterEach(cleanup);

function event(over: Partial<AgentJobEventApi> & { id: number }): AgentJobEventApi {
  return {
    attempt_id: 1,
    seq: over.id,
    event_type: 'message',
    payload: {},
    created_at: '2026-08-19T10:00:00.000Z',
    ...over,
  } as AgentJobEventApi;
}

const EVENTS = [
  event({
    id: 1,
    event_type: 'tool_use',
    payload: { id: 't1', name: 'Bash', detail: 'cargo build' },
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

describe('TrajectoryView', () => {
  it('lists every stored event, including the one chat folds away', () => {
    render(<TrajectoryView events={EVENTS} />);

    expect(screen.getByText('3 events')).toBeInTheDocument();
    expect(screen.getByText('Bash')).toBeInTheDocument();
    expect(screen.getByText('tool_result')).toBeInTheDocument();
  });

  it('opens a call to show its payload, its result, and how the time was measured', () => {
    render(<TrajectoryView events={EVENTS} />);

    fireEvent.click(screen.getByText('Bash'));

    expect(screen.getByText('Payload')).toBeInTheDocument();
    expect(screen.getByText(/includes the model/)).toBeInTheDocument();
    // The result of the call is shown beside the call, which is the thing the
    // chat view cannot do: there, the result no longer exists as a row.
    const result = screen.getByText('Result').parentElement as HTMLElement;
    expect(within(result).getByText(/Compiling hybridinference/)).toBeInTheDocument();
  });

  it('filters to the rows a query matches, and says so', () => {
    render(<TrajectoryView events={EVENTS} />);

    fireEvent.change(screen.getByLabelText('Search events'), { target: { value: 'cargo' } });

    expect(screen.getByText('1 of 3')).toBeInTheDocument();
    expect(screen.queryByText('the build passes')).toBeNull();
  });

  it('says plainly when a query matches nothing', () => {
    render(<TrajectoryView events={EVENTS} />);

    fireEvent.change(screen.getByLabelText('Search events'), { target: { value: 'zzz' } });

    expect(screen.getByText(/No event matches/)).toBeInTheDocument();
  });

  it('offers the stored stream as a download rather than a rendering of it', () => {
    const created = vi.fn(() => 'blob:x');
    vi.stubGlobal('URL', { createObjectURL: created, revokeObjectURL: vi.fn() });
    const click = vi
      .spyOn(HTMLAnchorElement.prototype, 'click')
      .mockImplementation(() => undefined);

    render(<TrajectoryView events={EVENTS} />);
    fireEvent.click(screen.getByRole('button', { name: 'Session log' }));

    expect(created).toHaveBeenCalledTimes(1);
    expect(click).toHaveBeenCalledTimes(1);
    click.mockRestore();
    vi.unstubAllGlobals();
  });

  it('gives the minimap one mark per event, so a long turn has a shape', () => {
    render(<TrajectoryView events={EVENTS} />);

    const marks = screen.getAllByRole('button', { name: /\((tool_use|tool_result|message)\)/ });

    expect(marks).toHaveLength(3);
  });

  it('selects a row when its minimap mark is clicked', () => {
    render(<TrajectoryView events={EVENTS} />);

    fireEvent.click(screen.getByRole('button', { name: 'Bash (tool_use)' }));

    expect(screen.getByText('Payload')).toBeInTheDocument();
  });

  it('explains an empty stream rather than rendering a bare panel', () => {
    render(<TrajectoryView events={[]} />);

    expect(screen.getByText(/stored no events/)).toBeInTheDocument();
  });

  it('shows a raw row, which is all a Tier-2 runtime produces', () => {
    render(
      <TrajectoryView events={[event({ id: 9, event_type: 'raw', payload: { line: 'x=1' } })]} />,
    );

    const rows = screen.getByText('raw').closest('button');
    expect(within(rows as HTMLElement).getByText('x=1')).toBeInTheDocument();
  });
});
