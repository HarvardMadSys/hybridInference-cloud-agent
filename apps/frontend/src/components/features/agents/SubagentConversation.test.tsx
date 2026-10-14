// @vitest-environment jsdom
import '@testing-library/jest-dom/vitest';
import { fireEvent, render, screen } from '@testing-library/react';
import { describe, expect, it } from 'vitest';
import { buildActivityOverview } from './activityOverview';
import { SubagentConversation } from './SubagentConversation';
import type { AgentEvent } from './types';

const agent = buildActivityOverview([
  {
    kind: 'tool_use',
    tool: 'Agent',
    id: 'toolu_scan',
    detail: 'Scan the repository',
    input: { description: 'Scan the repository', prompt: 'List the dead code.' },
    resultState: 'waiting',
    attemptNo: 1,
  },
]).subagents[0];

function messages(count: number): AgentEvent[] {
  return Array.from({ length: count }, (_, index) => ({
    kind: 'message',
    text: `Finding ${index + 1}`,
    attemptNo: 1,
  }));
}

function View({ events }: { events: AgentEvent[] }) {
  return (
    <div role="region" aria-label="Task progress">
      <SubagentConversation agent={agent} events={events} jobId="ajob_1" onBack={() => {}} />
    </div>
  );
}

describe('SubagentConversation', () => {
  it('follows new records only while the reader is at the end', () => {
    const { rerender } = render(<View events={messages(1)} />);
    const region = screen.getByRole('region', { name: 'Task progress' });
    const geometry = { height: 100, top: 0 };
    Object.defineProperty(region, 'clientHeight', { configurable: true, value: 100 });
    Object.defineProperty(region, 'scrollHeight', {
      configurable: true,
      get: () => geometry.height,
    });
    Object.defineProperty(region, 'scrollTop', {
      configurable: true,
      get: () => geometry.top,
      set: (value: number) => {
        geometry.top = value;
      },
    });

    // Everything fit on screen, so the reader was at the end: follow.
    geometry.height = 500;
    rerender(<View events={messages(2)} />);
    expect(geometry.top).toBe(500);

    // The reader scrolls up to read; new records leave them there.
    geometry.top = 100;
    fireEvent.scroll(region);
    geometry.height = 800;
    rerender(<View events={messages(3)} />);
    expect(geometry.top).toBe(100);

    // Back at the end, the view follows again.
    geometry.top = 700;
    fireEvent.scroll(region);
    geometry.height = 900;
    rerender(<View events={messages(4)} />);
    expect(geometry.top).toBe(900);
    expect(screen.getByText('Finding 4')).toBeInTheDocument();
  });
});
