// @vitest-environment jsdom
import '@testing-library/jest-dom/vitest';

import { cleanup, render, screen, within } from '@testing-library/react';
import { afterEach, describe, expect, it } from 'vitest';
import { ToolActivityDetails } from './ToolActivityDetails';
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
    expect(screen.getByText('Message').closest('details')).toHaveTextContent('Tests pass.');
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
});
