// @vitest-environment jsdom
import '@testing-library/jest-dom/vitest';

import { cleanup, fireEvent, render, screen, within } from '@testing-library/react';
import { afterEach, describe, expect, it } from 'vitest';
import { DelegationReplyDetails, ToolActivityDetails } from './ToolActivityDetails';
import { parseToolActivity } from './toolActivity';

afterEach(cleanup);

function view(tool: string, input: unknown, output?: string, isError?: boolean) {
  const activity = parseToolActivity(tool, input);
  if (!activity) throw new Error('Fixture must use a recognized activity tool.');
  return <ToolActivityDetails activity={activity} output={output} isError={isError} />;
}

describe('ToolActivityDetails', () => {
  it.each([
    { tool: 'spawn_agent', input: { prompt: 'Review' }, title: 'Start subagent' },
    { tool: 'wait', input: { ids: ['a'] }, title: 'Wait for subagents' },
    { tool: 'list_agents', input: {}, title: 'List subagents' },
    {
      tool: 'send_message',
      input: { target: 'a', message: 'Review' },
      title: 'Message subagent',
    },
    {
      tool: 'Task',
      input: { description: 'Delegate task', prompt: 'Review' },
      title: 'Delegate task',
    },
  ])(
    'renders the $tool heading once when there is no distinct description',
    ({ tool, input, title }) => {
      render(view(tool, input));
      expect(screen.getByRole('heading', { level: 3, name: title })).toBeInTheDocument();
    },
  );

  it('shows unfamiliar status strings literally and returned agent identities without inventing a state', () => {
    const rendered = render(
      view('TodoWrite', { todos: [{ content: 'Review', status: 'constructor' }] }),
    );
    expect(screen.getByText('constructor')).toBeInTheDocument();
    rendered.rerender(
      view('spawn_agent', { prompt: 'Review' }, JSON.stringify({ agent_id: 'new-child' })),
    );
    expect(screen.getByText('Agent: new-child')).toBeInTheDocument();
    expect(screen.queryByText('Running')).not.toBeInTheDocument();
    expect(screen.queryByText('Completed')).not.toBeInTheDocument();
  });

  it('renders recorded plan progress with pending, active, and unknown checklist states', () => {
    render(
      view('update_plan', {
        plan: [
          { step: 'Inspect', status: 'completed' },
          { step: 'Implement', status: 'in_progress' },
          { step: 'Review' },
        ],
      }),
    );
    const plan = screen.getByRole('region', { name: 'Plan' });
    expect(within(plan).getByText('1 of 3 marked completed')).toBeInTheDocument();
    expect(within(plan).getAllByRole('listitem')).toHaveLength(3);
    expect(within(plan).getByText('Completed')).toBeInTheDocument();
    expect(within(plan).getByText('In progress')).toBeInTheDocument();
    expect(within(plan).getByText('Status not recorded')).toBeInTheDocument();
  });

  it('shows task IDs, owner and dependencies and reads structured list results', () => {
    const rendered = render(
      view('TaskUpdate', {
        taskId: '4',
        owner: 'reviewer',
        addBlockedBy: ['1', '2'],
        addBlocks: ['5'],
      }),
    );
    expect(screen.getByText('Task #4')).toBeInTheDocument();
    expect(screen.getByText('reviewer')).toBeInTheDocument();
    expect(screen.getByText('#1, #2')).toBeInTheDocument();
    expect(screen.getByText('#5')).toBeInTheDocument();
    expect(screen.getByText('Status not recorded')).toBeInTheDocument();
    rendered.rerender(
      view(
        'TaskList',
        {},
        JSON.stringify({ tasks: [{ id: '4', subject: 'Review', status: 'completed' }] }),
      ),
    );
    expect(screen.getByText('Review')).toBeInTheDocument();
    expect(screen.getByText('1 of 1 marked completed')).toBeInTheDocument();
  });

  it('keeps delegation instructions collapsible and does not equate tool success with completion', () => {
    const prompt = '<script>alert("hello")</script>\nInspect all request fields.';
    render(
      view(
        'Task',
        {
          description: 'Review API',
          subagent_type: 'Explore',
          model: 'sonnet',
          prompt,
          run_in_background: true,
        },
        'Agent started successfully.',
      ),
    );
    expect(screen.getByText('Review API')).toBeInTheDocument();
    expect(
      screen.getByRole('heading', { level: 3, name: 'Review API Delegate task' }),
    ).toBeInTheDocument();
    expect(screen.getByText('Explore')).toBeInTheDocument();
    expect(screen.getByText('sonnet')).toBeInTheDocument();
    expect(screen.getByText('Background requested')).toBeInTheDocument();
    const disclosure = screen.getByText('Prompt').closest('details');
    expect(disclosure).not.toHaveAttribute('open');
    expect(disclosure?.querySelector('p')?.textContent).toBe(prompt);
    expect(disclosure?.querySelector('script')).toBeNull();
    expect(screen.queryByText('Completed')).not.toBeInTheDocument();
    expect(screen.queryByText('Running')).not.toBeInTheDocument();
  });

  it('displays only explicit agent states and their recorded messages', () => {
    render(
      view(
        'wait',
        { receiver_thread_ids: ['a', 'b'], agents_states: { a: { status: 'running' } } },
        JSON.stringify({
          agents_states: {
            a: { status: 'completed', message: 'Tests pass.' },
            b: { status: 'errored' },
          },
        }),
      ),
    );
    expect(screen.getByText('Agents: a, b')).toBeInTheDocument();
    expect(screen.getByText('Completed')).toBeInTheDocument();
    expect(screen.getByText('Errored')).toBeInTheDocument();
    expect(screen.queryByText('Running')).not.toBeInTheDocument();
    // A finished agent's message is the answer it returned, shown rather than folded away.
    const reply = screen.getByRole('region', { name: 'Subagent reply' });
    expect(reply).toHaveTextContent('Tests pass.');
    expect(reply.closest('details')).toBeNull();
    expect(screen.getAllByRole('region', { name: 'Subagent reply' })).toHaveLength(1);
  });

  it('shows an errored agent’s error as what it returned, with its state shown once', () => {
    render(
      view(
        'wait',
        { receiver_thread_ids: ['a'] },
        JSON.stringify({ agents_states: { a: { status: 'errored', message: 'Model refused.' } } }),
      ),
    );
    const reply = screen.getByRole('region', { name: 'Subagent reply' });
    expect(reply).toHaveTextContent('Model refused.');
    expect(screen.getAllByText('Errored')).toHaveLength(1);
    expect(screen.queryByText('Failed')).not.toBeInTheDocument();
  });

  it('does not turn failure text or missing task data into a completion', () => {
    const rendered = render(
      view('TaskCreate', { subject: 'Implement' }, 'Task created successfully.'),
    );
    expect(screen.getByText('Status not recorded')).toBeInTheDocument();
    expect(screen.queryByText('Completed')).not.toBeInTheDocument();
    rendered.rerender(
      view(
        'TaskList',
        {},
        JSON.stringify({ tasks: [{ subject: 'Implement', status: 'completed' }] }),
        true,
      ),
    );
    expect(screen.getByText('Task details were not recorded in this call.')).toBeInTheDocument();
    expect(screen.queryByText('Completed')).not.toBeInTheDocument();
  });

  it('lets long unbroken prompt text wrap inside the card', () => {
    render(view('Agent', { prompt: `Read apps/${'frontend/'.repeat(30)}compute.ts` }));
    const disclosure = screen.getByText('Prompt').closest('details');
    expect(disclosure).toHaveClass('w-fit', 'max-w-full');
    expect(disclosure?.querySelector('p')).toHaveClass('[overflow-wrap:anywhere]');
  });
});

describe('DelegationReplyDetails', () => {
  it('renders the returned answer as Markdown with the status the runtime recorded', () => {
    render(
      <DelegationReplyDetails
        reply={{ state: 'received', status: 'completed', text: '## Findings\n\n- inspected' }}
      />,
    );
    const reply = screen.getByRole('region', { name: 'Subagent reply' });
    expect(within(reply).getByText('Returned to the main agent')).toBeInTheDocument();
    expect(within(reply).getByText('Completed')).toBeInTheDocument();
    expect(within(reply).getByRole('heading', { name: 'Findings' })).toBeInTheDocument();
    expect(within(reply).getByRole('listitem')).toHaveTextContent('inspected');
    expect(within(reply).queryByRole('button')).not.toBeInTheDocument();
  });

  it('says when the stored reply is not the whole answer', () => {
    const rendered = render(
      <DelegationReplyDetails reply={{ state: 'received', text: 'Partial', truncated: true }} />,
    );
    expect(screen.getByRole('note')).toHaveTextContent(
      'The stored reply was cut short; this is not the whole answer.',
    );
    rendered.rerender(<DelegationReplyDetails reply={{ state: 'received', text: 'Whole' }} />);
    expect(screen.queryByRole('note')).not.toBeInTheDocument();
  });

  it('claims no status for an answer whose result recorded none', () => {
    render(<DelegationReplyDetails reply={{ state: 'received', text: 'Done.' }} />);
    expect(screen.getByText('Done.')).toBeInTheDocument();
    expect(screen.queryByText('Completed')).not.toBeInTheDocument();
    expect(screen.queryByText('Status not recorded')).not.toBeInTheDocument();
  });

  it('collapses a long answer until the reader expands it', () => {
    const text = Array.from({ length: 12 }, (_, index) => `Finding ${index + 1}`).join('\n\n');
    render(<DelegationReplyDetails reply={{ state: 'received', text }} />);
    const toggle = screen.getByRole('button', { name: 'Show full reply' });
    const body = document.getElementById(toggle.getAttribute('aria-controls') ?? '');
    expect(toggle).toHaveAttribute('aria-expanded', 'false');
    expect(body).toHaveClass('max-h-40', 'overflow-hidden');
    expect(body).toHaveTextContent('Finding 12');

    fireEvent.click(toggle);
    expect(screen.getByRole('button', { name: 'Show less' })).toHaveAttribute(
      'aria-expanded',
      'true',
    );
    expect(body).not.toHaveClass('max-h-40');
  });

  it('shows what a failed or interrupted delegation returned', () => {
    const rendered = render(
      <DelegationReplyDetails
        reply={{ state: 'failed', status: 'completed', text: 'Agent type not found' }}
      />,
    );
    expect(screen.getByText('Failed')).toBeInTheDocument();
    expect(screen.queryByText('Completed')).not.toBeInTheDocument();
    expect(screen.getByText('Agent type not found')).toBeInTheDocument();

    rendered.rerender(<DelegationReplyDetails reply={{ state: 'failed' }} />);
    expect(screen.getByText('Failed')).toBeInTheDocument();
    expect(screen.getByText('The delegation call failed.')).toBeInTheDocument();
    expect(screen.queryByText('Returned to the main agent')).not.toBeInTheDocument();

    rendered.rerender(<DelegationReplyDetails reply={{ state: 'cancelled' }} />);
    expect(screen.getByText('Cancelled')).toBeInTheDocument();
    expect(screen.getByText('The delegation call was interrupted.')).toBeInTheDocument();
  });

  it.each([
    [{ state: 'waiting' } as const, "Waiting for the agent's reply…"],
    [{ state: 'not_recorded' } as const, "The agent's reply was not recorded."],
    [
      { state: 'background', status: 'async_launched' } as const,
      "Started in the background. This call returned only a launch notice, not the agent's reply.",
    ],
    [{ state: 'received' } as const, 'No text was returned.'],
  ])('says plainly when there is no answer to show: %o', (reply, note) => {
    render(<DelegationReplyDetails reply={reply} />);
    expect(screen.getByText(note)).toBeInTheDocument();
    expect(screen.queryByText('Completed')).not.toBeInTheDocument();
  });
});
