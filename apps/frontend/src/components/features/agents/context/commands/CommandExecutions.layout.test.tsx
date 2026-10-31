// @vitest-environment jsdom
import '@testing-library/jest-dom/vitest';
import { cleanup, fireEvent, render, screen, within } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { LocaleProvider } from '@/components/providers/LocaleProvider';
import type { CommandExecutionApi, ThreadCommandsApi } from '@/lib/api/agents';
import { getEventContent, getThreadContextCommands } from '@/lib/api/agents';
import { CommandExecutions } from './CommandExecutions';

vi.mock('@/lib/api/agents', async (importOriginal) => ({
  ...(await importOriginal<typeof import('@/lib/api/agents')>()),
  getThreadContextCommands: vi.fn(),
  getEventContent: vi.fn(),
}));

const mocked = vi.mocked(getThreadContextCommands);

function execution(
  index: number,
  overrides: Partial<CommandExecutionApi> = {},
): CommandExecutionApi {
  return {
    id: `1:c${index}`,
    job_id: 'j1',
    turn_no: 1,
    attempt_id: 1,
    attempt_no: 1,
    runtime: 'claude-code',
    tool: 'Bash',
    command: `grep -rn needle src/module-${index}.ts`,
    command_chars: 30,
    command_truncated: false,
    cwd: '/srv/app',
    status: index % 5 === 0 ? 'failed' : 'completed',
    exit_code: index % 5 === 0 ? 1 : 0,
    tool_error: false,
    background: false,
    superseded: false,
    duration_ms: index * 250,
    duration_basis: 'recorded',
    at: '2026-10-30T09:00:00Z',
    result_at: '2026-10-30T09:00:01Z',
    event_id: 100 + index,
    result_event_id: 500 + index,
    output_preview: 'match '.repeat(40),
    output_chars: 240,
    output_truncated: false,
    program: 'grep',
    parse_basis: 'executable',
    ...overrides,
  };
}

function page(rows: CommandExecutionApi[], next: string | null = null): ThreadCommandsApi {
  return {
    thread_id: 't1',
    generated_at: '2026-10-30T09:10:00Z',
    slow_cutoff_ms: 10_000,
    summary: {
      total: rows.length,
      settled: rows.length,
      failed: 1,
      failure_rate: 1 / rows.length,
      running: 0,
      unknown: 0,
      cumulative_ms: rows.reduce((sum, row) => sum + (row.duration_ms ?? 0), 0),
      durations_known: rows.length,
      durations_missing: 0,
      duration_basis: 'recorded',
      background: 0,
    },
    coverage: {
      executions_total: rows.length,
      executions_covered: rows.length,
      truncated: false,
      turns: 1,
      attempts: 1,
      copied_jobs_excluded: 0,
      unsupported_runtimes: [],
      results_partial: false,
    },
    counts: { all: rows.length, failed: 1, slow: 1, background: 0 },
    attention: {
      slowest: rows.at(-1) ?? null,
      failing: { name: 'grep', count: 1, share: 1, fallback: false },
      repeats: { command: rows[0].command, count: 2 },
      background: 0,
    },
    programs: Array.from({ length: 14 }, (_, index) => ({
      name: `program-${index}`,
      count: 14 - index,
      share: (14 - index) / 14,
      fallback: false,
    })),
    executions: rows,
    page: { total: rows.length, returned: rows.length, limit: 100, next_cursor: next },
    filter: 'all',
    sort: 'latest',
    q: '',
    program: null,
  };
}

function renderSection() {
  return render(
    <LocaleProvider>
      <CommandExecutions
        jobId="j1"
        conversationKey="t1"
        onOpenPoint={vi.fn()}
        onOpenTurn={vi.fn()}
      />
    </LocaleProvider>,
  );
}

/** The card: the bordered box that bounds the whole section. */
function card(): HTMLElement {
  const heading = screen.getByRole('heading', { name: 'Command Executions' });
  return heading.closest('.ui-card') as HTMLElement;
}

beforeEach(() => {
  localStorage.clear();
  mocked.mockReset();
  vi.mocked(getEventContent).mockReset();
});

afterEach(cleanup);

describe('compact Command Executions card', () => {
  it('bounds the card and scrolls the list inside it, not the page', async () => {
    mocked.mockResolvedValue(page(Array.from({ length: 40 }, (_, index) => execution(index))));
    renderSection();
    await screen.findByRole('heading', { name: 'Command Executions' });
    const panel = card();
    // The card owns a bounded height (DSH's 560px) and hides overflow.
    expect(panel.className).toContain('max-h-[560px]');
    expect(panel.className).toContain('overflow-hidden');
    // The list is its own scroll container with overscroll containment.
    const list = screen.getByRole('list', { name: 'Command Executions' });
    expect(list.className).toContain('overflow-y-auto');
    expect(list.className).toContain('overscroll-contain');
    expect(list.className).toContain('flex-1');
    expect(list.className).toContain('min-h-[120px]');
  });

  it('keeps the programs list independently scrollable and bounded', async () => {
    mocked.mockResolvedValue(page(Array.from({ length: 6 }, (_, index) => execution(index))));
    renderSection();
    await screen.findByRole('heading', { name: 'Command Executions' });
    const programs = screen.getByRole('list', {
      name: /Programs/,
    });
    expect(programs.className).toContain('overflow-y-auto');
    expect(programs.className).toContain('overscroll-contain');
    expect(programs.className).toContain('min-h-0');
  });

  it('puts the filter chips in the same header as the title, before the toolbar', async () => {
    mocked.mockResolvedValue(page([execution(0)]));
    renderSection();
    const heading = await screen.findByRole('heading', { name: 'Command Executions' });
    const filters = screen.getByRole('group', { name: 'Show one kind of execution' });
    // Both live in the card's first row.
    expect(heading.parentElement).toBe(filters.parentElement);
    const search = screen.getByRole('searchbox', {
      name: 'Filter commands by text, working directory or program',
    });
    // The search sits after the header, in the list column.
    expect(heading.parentElement!.contains(search)).toBe(false);
  });

  it('keeps Attention before Programs in the rail', async () => {
    mocked.mockResolvedValue(page([execution(0)]));
    renderSection();
    await screen.findByRole('heading', { name: 'Command Executions' });
    const attention = screen.getByRole('heading', { name: 'Attention' });
    const programs = screen.getByRole('heading', { name: /Programs ·/ });
    expect(
      attention.compareDocumentPosition(programs) & Node.DOCUMENT_POSITION_FOLLOWING,
    ).toBeTruthy();
  });

  it('keeps the expanded output inside the bounded list', async () => {
    mocked.mockResolvedValue(
      page([
        execution(0, {
          output_preview: 'x'.repeat(4_000),
          output_chars: 40_000,
          output_truncated: true,
        }),
      ]),
    );
    renderSection();
    const list = await screen.findByRole('list', { name: 'Command Executions' });
    const item = within(list).getAllByRole('listitem')[0];
    fireEvent.click(within(item).getByRole('button', { expanded: false }));
    // The expanded detail is a child of the scrolling list, so it can never
    // grow the page; its own output panes are bounded too.
    expect(list.contains(within(item).getByLabelText('Execution details'))).toBe(true);
    const panes = item.querySelectorAll('pre');
    expect(panes.length).toBeGreaterThan(0);
    for (const pane of panes) {
      expect(pane.className).toMatch(/max-h-\d+/);
      expect(pane.className).toContain('overflow-auto');
    }
  });

  it('reaches every control and the show-more action by keyboard', async () => {
    mocked.mockResolvedValue(
      page(
        Array.from({ length: 3 }, (_, index) => execution(index)),
        'cursor-next',
      ),
    );
    renderSection();
    await screen.findByRole('heading', { name: 'Command Executions' });
    const controls = [
      screen.getByRole('button', { name: /^All\s*3$/ }),
      screen.getByRole('searchbox', {
        name: 'Filter commands by text, working directory or program',
      }),
      screen.getByRole('button', { name: 'Latest' }),
      screen.getByRole('button', { name: 'Show more' }),
    ];
    for (const control of controls) {
      control.focus();
      expect(control).toHaveFocus();
      // Every one is a native focusable with a visible focus style.
      expect(control.tagName === 'BUTTON' || control.tagName === 'INPUT').toBe(true);
    }
    const list = screen.getByRole('list', { name: 'Command Executions' });
    expect(list.contains(screen.getByRole('button', { name: 'Show more' }))).toBe(true);
  });

  it('renders each execution as one dense line with inline status and duration', async () => {
    mocked.mockResolvedValue(page([execution(4)]));
    renderSection();
    const list = await screen.findByRole('list', { name: 'Command Executions' });
    const item = within(list).getAllByRole('listitem')[0];
    const row = within(item).getByRole('button', { expanded: false });
    expect(row.className).toContain('items-center');
    expect(row.textContent).toContain('Completed 0');
    expect(row.textContent).toContain('1s');
    expect(row.textContent).toContain('grep');
  });

  it('collapses the rail on a narrow card and keeps the list primary', async () => {
    mocked.mockResolvedValue(page([execution(0)]));
    renderSection();
    await screen.findByRole('heading', { name: 'Command Executions' });
    const toggle = screen.getByRole('button', { name: 'Details' });
    const rail = document.getElementById(toggle.getAttribute('aria-controls')!)!;
    expect(toggle).toHaveAttribute('aria-expanded', 'false');
    expect(rail.className).toContain('hidden');
    // The list is still shown with the rail collapsed.
    expect(screen.getByRole('list', { name: 'Command Executions' })).toBeInTheDocument();
    fireEvent.click(toggle);
    expect(toggle).toHaveAttribute('aria-expanded', 'true');
    expect(rail.className).toContain('flex');
    expect(screen.getByRole('heading', { name: 'Attention' })).toBeInTheDocument();
  });
});
