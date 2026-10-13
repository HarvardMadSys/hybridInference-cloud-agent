// @vitest-environment jsdom
import '@testing-library/jest-dom/vitest';

import { cleanup, fireEvent, render, screen, within } from '@testing-library/react';
import { useState } from 'react';
import { afterEach, describe, expect, it, vi } from 'vitest';

import { EventList, EventRow } from './ChatMessages';
import type { AgentEvent } from './types';
import type { TranscriptPresentation } from './transcriptPresentation';

afterEach(cleanup);

function toolsFixture(count = 300) {
  const serialize = vi.fn((command: string) => ({ command, timeout: 0 }));
  const joinOutput = vi.fn((output: string[], separator?: string) =>
    Array.prototype.join.call(output, separator),
  );
  const events: AgentEvent[] = Array.from({ length: count }, (_, index) => {
    const command = `inspect-file-${index}`;
    const output = [`File ${index}: ${'x'.repeat(8192)}`, 'Complete output preserved.'];
    output.join = (separator) => joinOutput(output, separator);
    return {
      kind: 'tool_use',
      tool: 'Bash',
      detail: command,
      sourceEventId: index + 1,
      resultState: 'received',
      input: { command, toJSON: () => serialize(command) },
      output,
    };
  });
  return { events, serialize, joinOutput };
}

const renderEvent = (event: AgentEvent, _index: number, blockKey: string) => (
  <EventRow event={event} blockKey={blockKey} />
);

function Conversation({
  events,
  presentation,
  completed = false,
}: {
  events: AgentEvent[];
  presentation: TranscriptPresentation;
  completed?: boolean;
}) {
  const [draft, setDraft] = useState('');
  return (
    <>
      <EventList {...{ events, presentation, completed, renderEvent }} jobId="conversation" />
      <textarea
        aria-label="Draft"
        value={draft}
        onChange={(event) => setDraft(event.target.value)}
      />
    </>
  );
}

function typeDraft() {
  for (const value of ['h', 'he', 'hel', 'hell', 'hello']) {
    fireEvent.change(screen.getByRole('textbox', { name: 'Draft' }), { target: { value } });
  }
}

describe('transcript disclosure rendering work', () => {
  it('does not serialize or mount closed tool details while the surrounding draft changes', () => {
    const { events, serialize, joinOutput } = toolsFixture();
    render(<Conversation events={events} presentation="standard" />);
    expect(screen.getAllByRole('listitem')).toHaveLength(events.length);
    const show = screen.getByRole('button', { name: 'Show tool calls' });
    const detailsId = show.getAttribute('aria-controls')!;
    expect(document.getElementById(detailsId)).toHaveAttribute('hidden');
    expect(document.querySelectorAll('pre')).toHaveLength(0);
    typeDraft();
    expect(serialize).not.toHaveBeenCalled();
    expect(joinOutput).not.toHaveBeenCalled();

    fireEvent.click(show);
    expect(screen.getByRole('button', { name: 'Hide tool calls' })).toHaveAttribute(
      'aria-controls',
      detailsId,
    );
    expect(document.querySelectorAll('pre')).toHaveLength(events.length * 2);
    expect(serialize).toHaveBeenCalledTimes(events.length);
    expect(joinOutput).toHaveBeenCalledTimes(events.length);
    const details = document.getElementById(detailsId)!;
    expect(details).toHaveTextContent('"command": "inspect-file-299"');
    expect(details).toHaveTextContent('"timeout": 0');
    expect(details.querySelector('pre:nth-of-type(2)')?.textContent).toBe(
      `File 0: ${'x'.repeat(8192)}\nComplete output preserved.`,
    );
    typeDraft();
    expect(serialize).toHaveBeenCalledTimes(events.length);
    expect(joinOutput).toHaveBeenCalledTimes(events.length);

    fireEvent.click(screen.getByRole('button', { name: 'Hide tool calls' }));
    expect(document.querySelectorAll('pre')).toHaveLength(0);
    typeDraft();
    expect(serialize).toHaveBeenCalledTimes(events.length);
    expect(joinOutput).toHaveBeenCalledTimes(events.length);
  });

  it('unmounts folded compact process and restores open reasoning and tools when reopened', () => {
    const { events, serialize, joinOutput } = toolsFixture();
    const allEvents: AgentEvent[] = [
      { kind: 'thinking', text: 'Review **each file**.', sourceEventId: 1000 },
      ...events,
      { kind: 'message', text: 'Review complete.', sourceEventId: 1001 },
    ];
    render(<Conversation events={allEvents} presentation="compact" completed />);
    const showProcess = screen.getByRole('button', { name: 'Show process (301)' });
    const processId = showProcess.getAttribute('aria-controls')!;
    expect(document.getElementById(processId)).toBeEmptyDOMElement();
    expect(screen.queryByLabelText('Agent reasoning')).not.toBeInTheDocument();
    expect(screen.queryByRole('list', { name: 'Tool calls' })).not.toBeInTheDocument();
    expect(screen.getByText('Review complete.')).toBeVisible();
    typeDraft();
    expect(serialize).not.toHaveBeenCalled();
    expect(joinOutput).not.toHaveBeenCalled();

    fireEvent.click(showProcess);
    const reasoning = screen.getByLabelText('Agent reasoning') as HTMLDetailsElement;
    reasoning.open = true;
    fireEvent.click(screen.getByRole('button', { name: 'Show tool calls' }));
    expect(serialize).toHaveBeenCalledTimes(events.length);
    expect(within(reasoning).getByText('each file')).toBeVisible();
    const hideProcess = screen.getByRole('button', { name: 'Hide process (301)' });
    hideProcess.focus();
    fireEvent.click(hideProcess);
    expect(screen.getByRole('button', { name: 'Show process (301)' })).toHaveFocus();
    expect(document.getElementById(processId)).toBeEmptyDOMElement();
    expect(document.querySelectorAll('pre')).toHaveLength(0);
    typeDraft();
    expect(serialize).toHaveBeenCalledTimes(events.length);

    fireEvent.click(screen.getByRole('button', { name: 'Show process (301)' }));
    expect(screen.getByLabelText('Agent reasoning')).toHaveAttribute('open');
    expect(screen.getByRole('button', { name: 'Hide tool calls' })).toHaveAttribute(
      'aria-expanded',
      'true',
    );
    expect(
      screen.getByText(`File 299: ${'x'.repeat(8192)} Complete output preserved.`),
    ).toBeVisible();
    expect(serialize).toHaveBeenCalledTimes(events.length * 2);
  });

  it('defers raw activity-card arguments until the native disclosure opens', () => {
    const serialize = vi.fn(() => ({ plan: [{ step: 'Review', status: 'completed' }] }));
    const event: AgentEvent = {
      kind: 'tool_use',
      tool: 'update_plan',
      detail: 'Review',
      input: { plan: [{ step: 'Review', status: 'completed' }], toJSON: serialize },
      output: ['Plan recorded.'],
      resultState: 'received',
      sourceEventId: 1,
    };
    const { rerender } = render(
      <EventRow event={event} blockKey="activity" presentation="standard" />,
    );
    const details = screen.getByText('Call details').closest('details')!;
    expect(details.querySelectorAll('pre')).toHaveLength(0);
    rerender(<EventRow event={event} blockKey="activity" presentation="standard" />);
    expect(serialize).not.toHaveBeenCalled();

    details.open = true;
    fireEvent(details, new Event('toggle'));
    expect(serialize).toHaveBeenCalledTimes(1);
    expect(details).toHaveTextContent('"status": "completed"');
    expect(within(details).getByText('Plan recorded.')).toBeVisible();
    details.open = false;
    fireEvent(details, new Event('toggle'));
    expect(details.querySelectorAll('pre')).toHaveLength(0);
  });

  it('shows updated results and uses the current inspection action after a memoized group changes', () => {
    const { events } = toolsFixture(1);
    const firstInspect = vi.fn();
    const latestInspect = vi.fn();
    const props = { events, jobId: 'conversation', renderEvent, presentation: 'standard' as const };
    const { rerender } = render(<EventList {...props} onInspectEvent={firstInspect} />);
    fireEvent.click(screen.getByRole('button', { name: 'Inspect Bash: inspect-file-0' }));
    expect(firstInspect).toHaveBeenCalledWith(events[0], 'conversation');
    fireEvent.click(screen.getByRole('button', { name: 'Show tool call' }));

    const latest = { ...events[0], output: ['Latest complete result.'] } as AgentEvent;
    rerender(<EventList {...props} events={[latest]} onInspectEvent={latestInspect} />);
    expect(screen.getByRole('button', { name: 'Hide tool call' })).toHaveAttribute(
      'aria-expanded',
      'true',
    );
    expect(screen.getByText('Latest complete result.')).toBeVisible();
    fireEvent.click(screen.getByRole('button', { name: 'Inspect Bash: inspect-file-0' }));
    expect(firstInspect).toHaveBeenCalledTimes(1);
    expect(latestInspect).toHaveBeenCalledWith(latest, 'conversation');
  });
});
