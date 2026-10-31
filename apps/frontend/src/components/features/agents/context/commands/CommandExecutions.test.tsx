// @vitest-environment jsdom
import '@testing-library/jest-dom/vitest';
import { cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { LocaleProvider } from '@/components/providers/LocaleProvider';
import type { CommandExecutionApi, ThreadCommandsApi } from '@/lib/api/agents';
import { getEventContent, getThreadContextCommands } from '@/lib/api/agents';
import { LOCALE_STORAGE_KEY } from '@/lib/i18n/locales';
import { CommandExecutions } from './CommandExecutions';

vi.mock('@/lib/api/agents', async (importOriginal) => ({
  ...(await importOriginal<typeof import('@/lib/api/agents')>()),
  getThreadContextCommands: vi.fn(),
  getEventContent: vi.fn(),
}));

const mocked = vi.mocked(getThreadContextCommands);
const mockedContent = vi.mocked(getEventContent);

function execution(overrides: Partial<CommandExecutionApi> = {}): CommandExecutionApi {
  return {
    id: '1:c1',
    job_id: 'j1',
    turn_no: 1,
    attempt_id: 1,
    attempt_no: 1,
    runtime: 'claude-code',
    tool: 'Bash',
    command: 'pytest -q',
    command_chars: 9,
    command_truncated: false,
    cwd: '/work',
    status: 'completed',
    exit_code: 0,
    tool_error: false,
    background: false,
    superseded: false,
    duration_ms: 1_200,
    duration_basis: 'recorded',
    at: '2026-10-30T09:00:00Z',
    result_at: '2026-10-30T09:00:01Z',
    event_id: 1,
    result_event_id: 2,
    output_preview: 'ok',
    output_chars: 2,
    output_truncated: false,
    program: 'pytest',
    parse_basis: 'executable',
    ...overrides,
  };
}

const ROWS: CommandExecutionApi[] = [
  execution({ id: '1:c1', event_id: 1 }),
  execution({
    id: '1:c2',
    event_id: 2,
    command: 'make build',
    program: 'make',
    status: 'failed',
    exit_code: 2,
    tool_error: true,
    duration_ms: null,
    duration_basis: null,
  }),
  execution({
    id: '1:c3',
    event_id: 3,
    command: 'npm run dev',
    program: 'npm',
    status: 'running',
    exit_code: null,
    background: true,
    duration_ms: 12_500,
    duration_basis: 'events',
  }),
];

function page(overrides: Partial<ThreadCommandsApi> = {}): ThreadCommandsApi {
  return {
    thread_id: 't1',
    generated_at: '2026-10-30T09:10:00Z',
    slow_cutoff_ms: 10_000,
    summary: {
      total: 3,
      settled: 2,
      failed: 1,
      failure_rate: 0.5,
      running: 1,
      unknown: 0,
      cumulative_ms: 13_700,
      durations_known: 2,
      durations_missing: 1,
      duration_basis: 'mixed',
      background: 1,
    },
    coverage: {
      executions_total: 3,
      executions_covered: 3,
      truncated: false,
      turns: 1,
      attempts: 1,
      copied_jobs_excluded: 0,
      unsupported_runtimes: [],
      results_partial: false,
    },
    counts: { all: 3, failed: 1, slow: 1, background: 1 },
    attention: {
      slowest: ROWS[2],
      failing: { name: 'make', count: 1, share: 1, fallback: false },
      repeats: { command: 'pytest -q', count: 2 },
      background: 1,
    },
    programs: [
      { name: 'pytest', count: 2, share: 2 / 3, fallback: false },
      { name: 'make', count: 1, share: 1 / 3, fallback: false },
    ],
    executions: ROWS,
    page: { total: 3, returned: 3, limit: 100, next_cursor: null },
    filter: 'all',
    sort: 'latest',
    q: '',
    program: null,
    ...overrides,
  };
}

function renderSection(props: Partial<Parameters<typeof CommandExecutions>[0]> = {}) {
  return render(
    <LocaleProvider>
      <CommandExecutions
        jobId="j1"
        conversationKey="t1"
        onOpenPoint={vi.fn()}
        onOpenTurn={vi.fn()}
        {...props}
      />
    </LocaleProvider>,
  );
}

beforeEach(() => {
  localStorage.clear();
  mocked.mockReset();
  mockedContent.mockReset();
});

afterEach(cleanup);

describe('CommandExecutions', () => {
  it('shows the cumulative summary, the programs and a row per execution', async () => {
    mocked.mockResolvedValue(page());
    renderSection();

    expect(await screen.findByRole('heading', { name: 'Command Executions' })).toBeInTheDocument();
    const section = screen.getByRole('heading', { name: 'Command Executions' }).closest('section')!;
    expect(within(section).getByText('settled executions')).toBeInTheDocument();
    expect(within(section).getByText('cumulative duration')).toBeInTheDocument();
    expect(within(section).getByText('1 · 50%')).toBeInTheDocument();
    expect(within(section).getByText('14s')).toBeInTheDocument();
    // A missing duration says so rather than showing 0.
    expect(within(section).getAllByText('no timing recorded').length).toBeGreaterThan(0);
    // Programs carry counts and shares.
    expect(within(section).getByTitle('Filter the list to pytest')).toBeInTheDocument();
    expect(within(section).getByText('67%')).toBeInTheDocument();
    const list = within(section).getByRole('list', { name: 'Command Executions' });
    expect(within(list).getAllByRole('listitem')).toHaveLength(3);
  });

  it('labels the failure rate with its denominator and keeps running out of it', async () => {
    mocked.mockResolvedValue(page());
    renderSection();
    const section = (await screen.findByRole('heading', { name: 'Command Executions' })).closest(
      'section',
    )!;
    // The denominator is a tooltip on the Failed value, not a paragraph.
    expect(within(section).getByTitle(/failed ÷ settled/)).toBeInTheDocument();
    expect(within(section).getByText('Running')).toBeInTheDocument();
    expect(within(section).getAllByText('13s').length).toBeGreaterThan(0);
  });

  it('expands a row to its command, cwd, result and navigation', async () => {
    mocked.mockResolvedValue(page());
    const onOpenPoint = vi.fn();
    const onOpenTurn = vi.fn();
    renderSection({ onOpenPoint, onOpenTurn });
    const list = await screen.findByRole('list', { name: 'Command Executions' });
    const first = within(list).getAllByRole('listitem')[0];
    fireEvent.click(within(first).getByRole('button', { expanded: false }));

    const details = within(first).getByLabelText('Execution details');
    expect(within(details).getByText('/work')).toBeInTheDocument();
    expect(within(details).getByText(/exit 0/)).toBeInTheDocument();
    expect(within(details).getByText('ok')).toBeInTheDocument();

    fireEvent.click(within(details).getByRole('button', { name: 'Open step' }));
    expect(onOpenPoint).toHaveBeenCalledWith({ jobId: 'j1', turnNo: 1, eventId: 1 });
    fireEvent.click(within(details).getByRole('button', { name: 'Open turn' }));
    expect(onOpenTurn).toHaveBeenCalledWith('j1', 1);
  });

  it('filters by state through the server and keeps the chip counts', async () => {
    mocked.mockResolvedValue(page());
    renderSection();
    const filters = await screen.findByRole('group', { name: 'Show one kind of execution' });
    expect(within(filters).getByRole('button', { name: /All 3/ })).toHaveAttribute(
      'aria-pressed',
      'true',
    );
    mocked.mockResolvedValue(
      page({
        filter: 'failed',
        executions: [ROWS[1]],
        counts: { all: 3, failed: 1, slow: 1, background: 1 },
        page: { total: 1, returned: 1, limit: 100, next_cursor: null },
      }),
    );
    fireEvent.click(within(filters).getByRole('button', { name: /Failed 1/ }));
    await waitFor(() =>
      expect(mocked).toHaveBeenLastCalledWith(
        'j1',
        expect.objectContaining({ filter: 'failed' }),
        expect.anything(),
      ),
    );
  });

  it('sorts by slowest and by latest through the server', async () => {
    mocked.mockResolvedValue(page());
    renderSection();
    const sorts = await screen.findByRole('group', { name: 'Sort the list' });
    fireEvent.click(within(sorts).getByRole('button', { name: 'Slowest' }));
    await waitFor(() =>
      expect(mocked).toHaveBeenLastCalledWith(
        'j1',
        expect.objectContaining({ sort: 'slowest' }),
        expect.anything(),
      ),
    );
  });

  it('publishes the slow cutoff and makes the chip explain it', async () => {
    mocked.mockResolvedValue(page());
    renderSection();
    const filters = await screen.findByRole('group', { name: 'Show one kind of execution' });
    expect(within(filters).getByRole('button', { name: /Slow 1/ })).toHaveAttribute(
      'title',
      'A command at or over 10s is slow.',
    );
  });

  it('searches the command text through the server after typing', async () => {
    mocked.mockResolvedValue(page());
    renderSection();
    const search = await screen.findByRole('searchbox', {
      name: 'Filter commands by text, working directory or program',
    });
    fireEvent.change(search, { target: { value: 'make' } });
    await waitFor(
      () =>
        expect(mocked).toHaveBeenLastCalledWith(
          'j1',
          expect.objectContaining({ q: 'make' }),
          expect.anything(),
        ),
      { timeout: 2_000 },
    );
  });

  it('narrows by program when a histogram row is chosen', async () => {
    mocked.mockResolvedValue(page());
    renderSection();
    const program = await screen.findByTitle('Filter the list to pytest');
    fireEvent.click(program);
    await waitFor(() =>
      expect(mocked).toHaveBeenLastCalledWith(
        'j1',
        expect.objectContaining({ program: 'pytest' }),
        expect.anything(),
      ),
    );
  });

  it('appends the next page and never repeats a row', async () => {
    mocked.mockResolvedValue(
      page({
        page: { total: 3, returned: 2, limit: 2, next_cursor: 'next' },
        executions: ROWS.slice(0, 2),
      }),
    );
    renderSection();
    const more = await screen.findByRole('button', { name: 'Show more' });
    mocked.mockResolvedValue(
      page({
        page: { total: 3, returned: 1, limit: 2, next_cursor: null },
        executions: [ROWS[1], ROWS[2]],
      }),
    );
    fireEvent.click(more);
    const list = await screen.findByRole('list', { name: 'Command Executions' });
    await waitFor(() => expect(within(list).getAllByRole('listitem')).toHaveLength(3));
  });

  it('says how much of the conversation this read covered', async () => {
    mocked.mockResolvedValue(
      page({
        coverage: {
          executions_total: 900,
          executions_covered: 500,
          truncated: true,
          turns: 12,
          attempts: 15,
          copied_jobs_excluded: 2,
          unsupported_runtimes: ['goose'],
          results_partial: false,
        },
      }),
    );
    renderSection();
    const section = (await screen.findByRole('heading', { name: 'Command Executions' })).closest(
      'section',
    )!;
    expect(
      within(section).getByText('Covering the newest 500 of 900 recorded executions.'),
    ).toBeInTheDocument();
    expect(within(section).getByText(/2 fork-copied turns/)).toBeInTheDocument();
    expect(
      within(section).getByText(/Command format not recognized for: goose/),
    ).toBeInTheDocument();
  });

  it('is empty, and says so, when the conversation has no commands', async () => {
    mocked.mockResolvedValue(
      page({
        summary: {
          ...page().summary,
          total: 0,
          settled: 0,
          failed: 0,
          failure_rate: null,
          cumulative_ms: 0,
          durations_known: 0,
          durations_missing: 0,
          duration_basis: 'none',
          background: 0,
          running: 0,
        },
        counts: { all: 0, failed: 0, slow: 0, background: 0 },
        attention: { slowest: null, failing: null, repeats: null, background: 0 },
        programs: [],
        executions: [],
        page: { total: 0, returned: 0, limit: 100, next_cursor: null },
      }),
    );
    renderSection();
    expect(
      await screen.findByText('No commands are recorded for this conversation yet.'),
    ).toBeInTheDocument();
  });

  it('answers in Chinese when the locale is Chinese', async () => {
    localStorage.setItem(LOCALE_STORAGE_KEY, 'zh-CN');
    mocked.mockResolvedValue(page());
    renderSection();
    expect(await screen.findByRole('heading', { name: '命令执行' })).toBeInTheDocument();
    expect(screen.getByText('已结算执行')).toBeInTheDocument();
    expect(screen.getByText('合计耗时')).toBeInTheDocument();
    expect(screen.getAllByText('未记录耗时').length).toBeGreaterThan(0);
  });

  it('asks for the conversation it was given, not whichever page is open', async () => {
    mocked.mockResolvedValue(page());
    renderSection({ jobId: 'job-other' });
    await waitFor(() =>
      expect(mocked).toHaveBeenCalledWith('job-other', expect.anything(), expect.anything()),
    );
  });

  it('loads a longer result only when asked, and renders it as text', async () => {
    const long = execution({
      output_preview: 'x'.repeat(3_000),
      output_chars: 9_000,
      output_truncated: true,
    });
    mocked.mockResolvedValue(
      page({ executions: [long], page: { total: 1, returned: 1, limit: 100, next_cursor: null } }),
    );
    mockedContent.mockResolvedValue({
      event_id: 2,
      event_type: 'tool_result',
      phase: null,
      offset: 0,
      text: '<script>alert(1)</script>',
      total_chars: 25,
      truncated: false,
      next_offset: null,
    });
    renderSection();
    const list = await screen.findByRole('list', { name: 'Command Executions' });
    const item = within(list).getAllByRole('listitem')[0];
    fireEvent.click(within(item).getByRole('button', { expanded: false }));
    const details = within(item).getByLabelText('Execution details');
    fireEvent.click(within(details).getByRole('button', { name: 'Load the full result' }));
    await waitFor(() =>
      expect(within(details).getByText('<script>alert(1)</script>')).toBeInTheDocument(),
    );
    expect(details.querySelector('script')).toBeNull();
  });

  it('shows a failure to load the longer result without losing the row', async () => {
    const long = execution({ output_preview: 'x', output_chars: 9_000, output_truncated: true });
    mocked.mockResolvedValue(
      page({ executions: [long], page: { total: 1, returned: 1, limit: 100, next_cursor: null } }),
    );
    mockedContent.mockRejectedValue(new Error('offline'));
    renderSection();
    const list = await screen.findByRole('list', { name: 'Command Executions' });
    const item = within(list).getAllByRole('listitem')[0];
    fireEvent.click(within(item).getByRole('button', { expanded: false }));
    const details = within(item).getByLabelText('Execution details');
    fireEvent.click(within(details).getByRole('button', { name: 'Load the full result' }));
    expect(await within(details).findByRole('alert')).toHaveTextContent(
      'The full result could not be loaded.',
    );
  });

  it('shows a read error with a way to retry', async () => {
    mocked.mockRejectedValue(new Error('backend down'));
    renderSection();
    expect(await screen.findByRole('alert')).toHaveTextContent(
      'Command executions could not be read.',
    );
    expect(screen.getByRole('button', { name: 'Try again' })).toBeInTheDocument();
  });

  it('shows nothing that claims a missing exit code is zero', async () => {
    mocked.mockResolvedValue(
      page({
        executions: [execution({ exit_code: null, status: 'unknown', duration_ms: null })],
        page: { total: 1, returned: 1, limit: 100, next_cursor: null },
      }),
    );
    renderSection();
    const list = await screen.findByRole('list', { name: 'Command Executions' });
    const item = within(list).getAllByRole('listitem')[0];
    fireEvent.click(within(item).getByRole('button', { expanded: false }));
    const details = within(item).getByLabelText('Execution details');
    expect(within(details).getByText(/no exit code recorded/)).toBeInTheDocument();
    expect(within(details).queryByText('exit 0')).toBeNull();
  });
});

it('labels the retry attempt as an attempt, not a tool step', async () => {
  mocked.mockResolvedValue(page());
  renderSection();
  const list = await screen.findByRole('list', { name: 'Command Executions' });
  const item = within(list).getAllByRole('listitem')[0];
  expect(within(item).getByText('T1 · A1')).toBeInTheDocument();
  expect(within(item).getByText('T1 · A1').closest('[title]')).toHaveAttribute(
    'title',
    'Turn 1, attempt 1 (a retry is another attempt).',
  );
});

it('refreshes a running turn with a request whose signal stays live, and renders the change', async () => {
  let signal: AbortSignal | undefined;
  let resolveSecond: ((value: ThreadCommandsApi) => void) | undefined;
  mocked.mockImplementationOnce(async (_job, _query, given) => {
    signal = given;
    return page();
  });
  mocked.mockImplementationOnce(
    (_job, _query, given) =>
      new Promise<ThreadCommandsApi>((resolve) => {
        signal = given;
        resolveSecond = resolve;
      }),
  );
  vi.useFakeTimers({ shouldAdvanceTime: true });
  try {
    render(
      <LocaleProvider>
        <CommandExecutions
          jobId="j1"
          conversationKey="t1"
          active
          onOpenPoint={vi.fn()}
          onOpenTurn={vi.fn()}
        />
      </LocaleProvider>,
    );
    await waitFor(() => expect(mocked).toHaveBeenCalledTimes(1));
    await vi.advanceTimersByTimeAsync(5_100);
    await waitFor(() => expect(mocked).toHaveBeenCalledTimes(2));
    // The refresh's own request must not have been aborted by the effect.
    expect(signal?.aborted).toBe(false);
    resolveSecond!(
      page({
        executions: [execution({ id: '1:c1', status: 'completed', output_preview: 'finished' })],
      }),
    );
    const list = await screen.findByRole('list', { name: 'Command Executions' });
    await waitFor(() =>
      expect(
        within(within(list).getAllByRole('listitem')[0]).getByText('Completed'),
      ).toBeInTheDocument(),
    );
  } finally {
    vi.useRealTimers();
  }
});

it('reads once more when a turn stops running, so a settled command is not left Running', async () => {
  const props = {
    jobId: 'j1',
    conversationKey: 't1',
    onOpenPoint: vi.fn(),
    onOpenTurn: vi.fn(),
  };
  mocked.mockResolvedValue(page());
  const { rerender } = render(
    <LocaleProvider>
      <CommandExecutions {...props} active />
    </LocaleProvider>,
  );
  await waitFor(() => expect(mocked).toHaveBeenCalledTimes(1));
  mocked.mockResolvedValue(page({ executions: [execution({ status: 'completed' })] }));
  rerender(
    <LocaleProvider>
      <CommandExecutions {...props} active={false} />
    </LocaleProvider>,
  );
  await waitFor(() => expect(mocked.mock.calls.length).toBeGreaterThanOrEqual(2));
});

it('drops detail state when a refreshed result replaces the old one', async () => {
  const first = execution({
    result_event_id: 2,
    output_preview: 'old',
    output_chars: 3,
    output_truncated: true,
  });
  mocked.mockResolvedValueOnce(
    page({ executions: [first], page: { total: 1, returned: 1, limit: 100, next_cursor: null } }),
  );
  let resolveContent: ((value: Awaited<ReturnType<typeof getEventContent>>) => void) | undefined;
  mockedContent.mockImplementation(
    () =>
      new Promise((resolve) => {
        resolveContent = resolve;
      }),
  );
  vi.useFakeTimers({ shouldAdvanceTime: true });
  try {
    render(
      <LocaleProvider>
        <CommandExecutions
          jobId="j1"
          conversationKey="t1"
          active
          onOpenPoint={vi.fn()}
          onOpenTurn={vi.fn()}
        />
      </LocaleProvider>,
    );
    const list = await screen.findByRole('list', { name: 'Command Executions' });
    const item = within(list).getAllByRole('listitem')[0];
    fireEvent.click(within(item).getByRole('button', { expanded: false }));
    const details = within(item).getByLabelText('Execution details');
    fireEvent.click(within(details).getByRole('button', { name: 'Load the full result' }));

    // A periodic refresh moves the same execution to a new result while the
    // old content fetch is still in flight.
    mocked.mockResolvedValue(
      page({
        executions: [execution({ result_event_id: 9, output_preview: 'new', output_chars: 3 })],
        page: { total: 1, returned: 1, limit: 100, next_cursor: null },
      }),
    );
    await vi.advanceTimersByTimeAsync(5_100);
    await waitFor(() => expect(screen.getByText('new')).toBeInTheDocument());
    resolveContent?.({
      event_id: 2,
      event_type: 'tool_result',
      phase: null,
      offset: 0,
      text: 'stale-old-text',
      total_chars: 3,
      truncated: false,
      next_offset: null,
    });
    // The stale page is ignored: no old text under the new result.
    await waitFor(() => expect(screen.queryByText('stale-old-text')).toBeNull());
  } finally {
    vi.useRealTimers();
  }
});

it('shows an unknown cumulative duration when nothing was measured', async () => {
  mocked.mockResolvedValue(
    page({
      summary: {
        ...page().summary,
        cumulative_ms: 0,
        durations_known: 0,
        durations_missing: 19,
        duration_basis: 'none',
      },
    }),
  );
  renderSection();
  const section = (await screen.findByRole('heading', { name: 'Command Executions' })).closest(
    'section',
  )!;
  const facts = within(section).getAllByText('no timing recorded');
  expect(facts.length).toBeGreaterThan(0);
  expect(within(section).queryByText('0ms')).toBeNull();
});

it('continues a long result by offset instead of hiding the rest', async () => {
  const long = execution({
    output_preview: 'x'.repeat(3_000),
    output_chars: 25_000,
    output_truncated: true,
  });
  mocked.mockResolvedValue(
    page({ executions: [long], page: { total: 1, returned: 1, limit: 100, next_cursor: null } }),
  );
  mockedContent.mockImplementation(async (_job, _event, offset = 0) => {
    if (offset === 0) {
      return {
        event_id: 2,
        event_type: 'tool_result',
        phase: null,
        offset: 0,
        text: 'first-page',
        total_chars: 25_000,
        truncated: false,
        next_offset: 10,
      };
    }
    return {
      event_id: 2,
      event_type: 'tool_result',
      phase: null,
      offset: 10,
      text: '-second-page',
      total_chars: 25_000,
      truncated: false,
      next_offset: null,
    };
  });
  renderSection();
  const list = await screen.findByRole('list', { name: 'Command Executions' });
  const item = within(list).getAllByRole('listitem')[0];
  fireEvent.click(within(item).getByRole('button', { expanded: false }));
  const details = within(item).getByLabelText('Execution details');
  fireEvent.click(within(details).getByRole('button', { name: 'Load the full result' }));
  expect(await within(details).findByText('first-page')).toBeInTheDocument();
  expect(mockedContent).toHaveBeenLastCalledWith('j1', 2, 0);
  fireEvent.click(within(details).getByRole('button', { name: 'Load more' }));
  await waitFor(() =>
    expect(within(details).getByText('first-page-second-page')).toBeInTheDocument(),
  );
  expect(mockedContent).toHaveBeenLastCalledWith('j1', 2, 10);
});
