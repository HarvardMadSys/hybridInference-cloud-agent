// @vitest-environment jsdom
// File Activity as a reader uses it: the filters show the server's counts, a
// row expands into its recorded operations with their turn and source step,
// and the section says which bounded window its totals describe.

import '@testing-library/jest-dom/vitest';
import { cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { LocaleProvider } from '@/components/providers/LocaleProvider';
import {
  getAgentFileActivity,
  getAgentFileOperations,
  type FileActivityApi,
  type FileOperationsPageApi,
} from '@/lib/api/agents';

import { FileActivity, FILE_SEARCH_DEBOUNCE_MS } from './FileActivity';

vi.mock('@/lib/api/agents', async (importOriginal) => ({
  ...(await importOriginal<typeof import('@/lib/api/agents')>()),
  getAgentFileActivity: vi.fn(),
  getAgentFileOperations: vi.fn(),
}));

const mockedActivity = vi.mocked(getAgentFileActivity);
const mockedOperations = vi.mocked(getAgentFileOperations);

function view(overrides: Partial<FileActivityApi> = {}): FileActivityApi {
  return {
    thread_id: 'thread-1',
    generated_at: '2026-10-31T10:00:00Z',
    sort: 'active',
    filter: 'all',
    q: '',
    totals: {
      files: 2,
      operations: 4,
      reads: 2,
      writes: 1,
      searches: 1,
      images: 0,
      added: 14,
      removed: 5,
      added_unknown_files: 0,
      removed_unknown_files: 1,
      failed: 0,
      pending: 0,
      not_recorded: 0,
      unknown_path_operations: 0,
      search_unknown_scope: 1,
      subagent_operations: 0,
    },
    filter_counts: { all: 2, read: 1, written: 2, searched: 1, images: 0 },
    coverage: {
      turns: 2,
      attempts: 2,
      events_scanned: 40,
      events_capped: false,
      window_first_at: '2026-10-31T08:00:00Z',
      window_last_at: '2026-10-31T09:59:00Z',
    },
    semantics: {
      line_changes: 'cumulative_recorded',
      net_git_diff: 'not_read',
      search_scope: 'explicit_path_only',
      image_evidence: 'explicit_only',
      counts: 'successful_operations',
    },
    files: [
      {
        path: '/workspace/src/app.ts',
        name: 'app.ts',
        type_label: 'TS',
        file_kind: 'code',
        reads: 2,
        writes: 1,
        searches: 0,
        images: 0,
        failed: 0,
        pending: 0,
        not_recorded: 0,
        added: 12,
        removed: 5,
        added_unknown_ops: 0,
        removed_unknown_ops: 0,
        operations: 3,
        events: 6,
        subagent_operations: 0,
        attempts: 2,
        first_at: '2026-10-31T08:00:00Z',
        last_at: '2026-10-31T09:59:00Z',
      },
      {
        path: '/workspace/docs/notes 深い.md',
        name: 'notes 深い.md',
        type_label: 'MD',
        file_kind: 'markup',
        reads: 0,
        writes: 1,
        searches: 0,
        images: 0,
        failed: 0,
        pending: 0,
        not_recorded: 0,
        added: 2,
        removed: 0,
        added_unknown_ops: 0,
        removed_unknown_ops: 1,
        operations: 1,
        events: 2,
        subagent_operations: 0,
        attempts: 1,
        first_at: '2026-10-31T08:30:00Z',
        last_at: '2026-10-31T08:30:00Z',
      },
    ],
    has_more: false,
    next_cursor: null,
    ...overrides,
  };
}

function page(): FileOperationsPageApi {
  return {
    thread_id: 'thread-1',
    path: '/workspace/src/app.ts',
    total: 2,
    operations: [
      {
        call_id: 'call-2',
        event_id: 9,
        result_event_id: 10,
        attempt_id: 2,
        attempt_no: 2,
        job_id: 'job-2',
        turn_no: 2,
        seq: 9,
        at: '2026-10-31T09:59:00Z',
        tool: 'Write',
        action: 'write',
        kind: 'write',
        status: 'succeeded',
        path: '/workspace/src/app.ts',
        name: 'app.ts',
        path_basis: 'cwd_relative',
        added: 12,
        removed: null,
        patch_index: null,
        patch_files: null,
        renamed_to: null,
        subagent: false,
        superseded: false,
        clipped: false,
      },
      {
        call_id: 'call-1',
        event_id: 3,
        result_event_id: null,
        attempt_id: 2,
        attempt_no: 2,
        job_id: 'job-2',
        turn_no: 2,
        seq: 3,
        at: null,
        tool: 'Read',
        action: 'read',
        kind: 'read',
        status: 'not_recorded',
        path: '/workspace/src/app.ts',
        name: 'app.ts',
        path_basis: 'cwd_relative',
        added: null,
        removed: null,
        patch_index: null,
        patch_files: null,
        renamed_to: null,
        subagent: false,
        superseded: false,
        clipped: false,
      },
    ],
    has_earlier: false,
    next_cursor: null,
  };
}

function renderSection(onOpenPoint = vi.fn(), onOpenTurn = vi.fn()) {
  render(
    <LocaleProvider>
      <FileActivity
        jobId="job-2"
        conversationKey="thread-1"
        onOpenPoint={onOpenPoint}
        onOpenTurn={onOpenTurn}
      />
    </LocaleProvider>,
  );
  return { onOpenPoint, onOpenTurn };
}

beforeEach(() => {
  vi.clearAllMocks();
  window.localStorage.clear();
  mockedActivity.mockResolvedValue(view());
  mockedOperations.mockResolvedValue(page());
});

afterEach(cleanup);

describe('FileActivity', () => {
  it('draws the recorded files with their counts and churn', async () => {
    renderSection();
    expect(await screen.findByText('app.ts')).toBeInTheDocument();
    expect(screen.getByText('notes 深い.md')).toBeInTheDocument();
    // The badge is from the extension, not evidence of activity.
    expect(screen.getByText('TS')).toBeInTheDocument();
    expect(screen.getByText('MD')).toBeInTheDocument();
    // One known and one unknown removed side: a dash, never 0. The headline
    // total repeats the known figure, so the row's own pair is located first.
    expect(screen.getAllByText('−5').length).toBeGreaterThan(0);
    // The second file's only write recorded no removed lines: a dash, not 0.
    expect(screen.getAllByText('—').length).toBeGreaterThan(0);
    // A read-only file shows no churn at all, rather than a measured +0 −0.
    expect(screen.queryByText('+0')).not.toBeInTheDocument();
    expect(mockedActivity).toHaveBeenCalledWith(
      'job-2',
      expect.objectContaining({ sort: 'active', filter: 'all', limit: 25 }),
      expect.anything(),
    );
  });

  it('shows the filter counts the server returned', async () => {
    renderSection();
    await screen.findByText('app.ts');
    expect(screen.getByTestId('file-filter-all')).toHaveTextContent('2');
    expect(screen.getByTestId('file-filter-read')).toHaveTextContent('1');
    expect(screen.getByTestId('file-filter-written')).toHaveTextContent('2');
    expect(screen.getByTestId('file-filter-searched')).toHaveTextContent('1');
    expect(screen.getByTestId('file-filter-images')).toHaveTextContent('0');
  });

  it('asks the server for the chosen filter and sort', async () => {
    renderSection();
    await screen.findByText('app.ts');
    fireEvent.click(screen.getByTestId('file-filter-written'));
    await waitFor(() =>
      expect(mockedActivity).toHaveBeenCalledWith(
        'job-2',
        expect.objectContaining({ filter: 'written' }),
        expect.anything(),
      ),
    );
  });

  it('sends the path search after typing pauses', async () => {
    renderSection();
    await screen.findByText('app.ts');
    const input = screen.getByPlaceholderText('Filter by path…');
    fireEvent.change(input, { target: { value: 'app' } });
    await waitFor(
      () =>
        expect(mockedActivity).toHaveBeenCalledWith(
          'job-2',
          expect.objectContaining({ q: 'app' }),
          expect.anything(),
        ),
      { timeout: FILE_SEARCH_DEBOUNCE_MS + 1_000 },
    );
  });

  it('expands a row into its recorded operations with source navigation', async () => {
    const { onOpenPoint, onOpenTurn } = renderSection();
    await screen.findByText('app.ts');
    fireEvent.click(screen.getByLabelText('Show operations on app.ts'));
    expect(await screen.findByText(/of 2 recorded operations shown/)).toBeInTheDocument();
    expect(screen.getAllByText('Write').length).toBeGreaterThan(0);
    // An unanswered call is listed as having no recorded result, never as a write.
    expect(screen.getByText(/no result recorded/)).toBeInTheDocument();
    const steps = screen.getAllByRole('button', { name: 'Step' });
    fireEvent.click(steps[0]);
    expect(onOpenPoint).toHaveBeenCalledWith({ jobId: 'job-2', turnNo: 2, eventId: 10 });
    fireEvent.click(screen.getAllByRole('button', { name: 'Turn' })[0]);
    expect(onOpenTurn).toHaveBeenCalledWith('job-2', 2);
  });

  it('says which bounded window the totals describe, in a tooltip', async () => {
    renderSection();
    await screen.findByText('app.ts');
    expect(screen.getByText('40 events scanned')).toBeInTheDocument();
    // The long explanation is available on hover, not printed in the panel.
    const detail = screen.getByTitle(
      /Scanned 40 recorded events over 2 turns and 2 attempts\. Net Git diff is not read here\./,
    );
    expect(detail).toBeInTheDocument();
    expect(screen.queryByText(/Net Git diff is not read here/)).not.toBeInTheDocument();
  });

  it('discloses a capped scan instead of calling it the whole history', async () => {
    mockedActivity.mockResolvedValue(
      view({
        coverage: {
          turns: 2,
          attempts: 2,
          events_scanned: 20_000,
          events_capped: true,
          window_first_at: null,
          window_last_at: null,
        },
      }),
    );
    renderSection();
    await screen.findByText('app.ts');
    expect(screen.getByText('newest part of a long history')).toBeInTheDocument();
  });

  it('explains an empty conversation without inventing files', async () => {
    mockedActivity.mockResolvedValue(
      view({
        files: [],
        filter_counts: { all: 0, read: 0, written: 0, searched: 0, images: 0 },
      }),
    );
    renderSection();
    expect(
      await screen.findByText('No recorded file operations in this conversation yet.'),
    ).toBeInTheDocument();
    expect(screen.queryByText('app.ts')).not.toBeInTheDocument();
  });

  it('distinguishes no match from an empty conversation', async () => {
    mockedActivity.mockResolvedValueOnce(view());
    mockedActivity.mockResolvedValue(
      view({
        files: [],
        q: 'zzz',
        filter_counts: { all: 0, read: 0, written: 0, searched: 0, images: 0 },
      }),
    );
    renderSection();
    await screen.findByText('app.ts');
    const input = screen.getByPlaceholderText('Filter by path…');
    fireEvent.change(input, { target: { value: 'zzz' } });
    expect(await screen.findByText('No file matches this search and filter.')).toBeInTheDocument();
  });

  it('reports a read failure without claiming the conversation changed', async () => {
    mockedActivity.mockRejectedValue(new Error('boom'));
    renderSection();
    expect(
      await screen.findByText('File activity could not be read. The conversation is unchanged.'),
    ).toBeInTheDocument();
  });

  it('keeps a long basename inside the row instead of overflowing it', async () => {
    const longName = `${'extremely_long_generated_module_name_'.repeat(3)}.tsx`;
    mockedActivity.mockResolvedValue(
      view({
        files: [
          {
            ...view().files[0],
            path: `/workspace/src/deep/nested/${longName}`,
            name: longName,
          },
        ],
      }),
    );
    renderSection();
    const name = await screen.findByText(longName);
    // The name is allowed to truncate and shrink within the row, never to
    // force the row (and the page) wider than the panel.
    expect(name.className).toContain('truncate');
    // The bound is what keeps one huge name from widening the row.
    expect(name.className).toContain('max-w-full');
    expect(name.className).toContain('shrink-0');
    const row = name.closest('button');
    expect(row).not.toBeNull();
    expect(row?.getAttribute('title')).toBe(`/workspace/src/deep/nested/${longName}`);
  });

  it('keeps directory and name adjacent rather than stretching the directory', async () => {
    renderSection();
    const name = await screen.findByText('app.ts');
    const directory = name.previousElementSibling as HTMLElement | null;
    // A full-width directory would insert a gap before the name on desktop.
    expect(directory?.textContent).toBe('/workspace/src/');
    expect(directory?.className).toContain('truncate');
    expect(directory?.className).not.toContain('flex-1');
  });

  it('keeps the full path addressable while the row stays one line', async () => {
    renderSection();
    const name = await screen.findByText('app.ts');
    const row = name.closest('button');
    // The toggle carries the whole path for hover, a long-press and a reader.
    expect(row).toHaveAttribute('title', '/workspace/src/app.ts');
    expect(row).toHaveAttribute('aria-label', 'Show operations on app.ts');
    const long = screen.getByText('notes 深い.md');
    expect(long.closest('button')?.textContent).toContain('/workspace/docs/');
    // A directory is dimmed and the name is what stands out.
    expect(long.className).toContain('font-medium');
  });

  it('draws the Chinese strings under the Chinese locale', async () => {
    window.localStorage.setItem('agents.locale', 'zh-CN');
    renderSection();
    expect(await screen.findByText('文件活动')).toBeInTheDocument();
    expect(screen.getByTestId('file-filter-written')).toHaveTextContent('写入');
  });

  it('marks a delegated operation rather than folding it into the conversation', async () => {
    mockedOperations.mockResolvedValue({
      ...page(),
      operations: [{ ...page().operations[0], subagent: true }],
    });
    renderSection();
    await screen.findByText('app.ts');
    fireEvent.click(screen.getByLabelText('Show operations on app.ts'));
    const detail = await screen.findByTitle(/by a delegated agent/);
    expect(detail).toBeInTheDocument();
    expect(screen.queryByText(/by a delegated agent/)).not.toBeInTheDocument();
  });

  it('groups one multi-file patch by its section inside the file', async () => {
    mockedOperations.mockResolvedValue({
      ...page(),
      operations: [
        {
          ...page().operations[0],
          tool: 'apply_patch',
          action: 'apply_patch',
          patch_index: 2,
          patch_files: 3,
        },
      ],
    });
    renderSection();
    await screen.findByText('app.ts');
    fireEvent.click(screen.getByLabelText('Show operations on app.ts'));
    expect(await screen.findByTitle(/part 2 of 3 in one patch/)).toBeInTheDocument();
    const ops = within(
      screen.getByRole('list', { name: 'Recorded operations on /workspace/src/app.ts' }),
    ).getAllByRole('listitem');
    expect(ops).toHaveLength(1);
  });
});

describe('pagination and refresh', () => {
  function threePages() {
    return [0, 1, 2].map((index) => ({
      ...page(),
      path: '/workspace/src/app.ts',
      operations: [
        {
          ...page().operations[0],
          call_id: `c${index}`,
          event_id: 100 + index,
          at: `2026-10-31T09:0${index}:00Z`,
        },
      ],
      total: 3,
      has_earlier: index < 2,
      next_cursor: index < 2 ? `cursor-${index}` : null,
    }));
  }

  it('reaches a third page without repeating or losing an operation', async () => {
    const [first, second, third] = threePages();
    mockedOperations.mockResolvedValueOnce(first);
    mockedOperations.mockResolvedValueOnce(second);
    mockedOperations.mockResolvedValueOnce(third);
    renderSection();
    await screen.findByText('app.ts');
    fireEvent.click(screen.getByLabelText('Show operations on app.ts'));
    await screen.findByText(/of 3 recorded operations shown/);
    fireEvent.click(screen.getByRole('button', { name: 'Show earlier operations' }));
    await waitFor(() => expect(mockedOperations).toHaveBeenCalledTimes(2));
    // The second page's cursor must still be offered, or page three is lost.
    fireEvent.click(await screen.findByRole('button', { name: 'Show earlier operations' }));
    await waitFor(() => expect(mockedOperations).toHaveBeenCalledTimes(3));
    expect(mockedOperations.mock.calls[2][2]).toMatchObject({ cursor: 'cursor-1' });
    const items = within(
      screen.getByRole('list', { name: 'Recorded operations on /workspace/src/app.ts' }),
    ).getAllByRole('listitem');
    expect(items).toHaveLength(3);
  });

  it('offers a retry when a later operations page fails', async () => {
    const [first, second] = threePages();
    mockedOperations.mockResolvedValueOnce(first);
    mockedOperations.mockRejectedValueOnce(new Error('offline'));
    mockedOperations.mockResolvedValueOnce(second);
    renderSection();
    await screen.findByText('app.ts');
    fireEvent.click(screen.getByLabelText('Show operations on app.ts'));
    fireEvent.click(await screen.findByRole('button', { name: 'Show earlier operations' }));
    expect(await screen.findByText('The rest of the list could not be read.')).toBeInTheDocument();
    fireEvent.click(screen.getByRole('button', { name: 'Retry' }));
    await waitFor(() => expect(mockedOperations).toHaveBeenCalledTimes(3));
  });

  it('drops a file page that resolves after the search changed', async () => {
    let release: (value: FileActivityApi | null) => void = () => {};
    const delayed = new Promise<FileActivityApi | null>((resolve) => {
      release = resolve;
    });
    mockedActivity.mockResolvedValueOnce(view());
    mockedActivity.mockReturnValueOnce(delayed);
    renderSection();
    await screen.findByText('app.ts');
    const input = screen.getByPlaceholderText('Filter by path…');
    fireEvent.change(input, { target: { value: 'zzz' } });
    // Answer the *second* read (the searched one) before the first resolves.
    mockedActivity.mockResolvedValueOnce(
      view({
        files: [],
        q: 'zzz',
        filter_counts: { all: 0, read: 0, written: 0, searched: 0, images: 0 },
      }),
    );
    release(view({ files: [view().files[1]], q: '' }));
    await waitFor(() => expect(screen.queryByText('notes 深い.md')).not.toBeInTheDocument());
  });

  it('re-reads when the conversation view asks it to refresh', async () => {
    // Independent of the queue earlier cases left behind.
    mockedActivity.mockReset();
    mockedActivity.mockResolvedValue(view());
    const { rerender } = render(
      <LocaleProvider>
        <FileActivity
          jobId="job-2"
          conversationKey="thread-1"
          onOpenPoint={vi.fn()}
          onOpenTurn={vi.fn()}
          refreshTick={1}
        />
      </LocaleProvider>,
    );
    await screen.findByText('app.ts');
    const before = mockedActivity.mock.calls.length;
    rerender(
      <LocaleProvider>
        <FileActivity
          jobId="job-2"
          conversationKey="thread-1"
          onOpenPoint={vi.fn()}
          onOpenTurn={vi.fn()}
          refreshTick={2}
        />
      </LocaleProvider>,
    );
    await waitFor(() => expect(mockedActivity.mock.calls.length).toBeGreaterThan(before));
  });

  it('offers a retry when the first page fails', async () => {
    mockedActivity.mockRejectedValueOnce(new Error('offline'));
    mockedActivity.mockResolvedValueOnce(view());
    renderSection();
    expect(
      await screen.findByText('File activity could not be read. The conversation is unchanged.'),
    ).toBeInTheDocument();
    fireEvent.click(screen.getByRole('button', { name: 'Retry' }));
    expect(await screen.findByText('app.ts')).toBeInTheDocument();
  });
});

describe('fencing across queries and settlement', () => {
  function pageWith(path: string, extra = {}) {
    return {
      ...page(),
      operations: [{ ...page().operations[0], path, call_id: `call-${path}` }],
      ...extra,
    };
  }

  it('drops a delayed page 2 of query A that resolves after query B started', async () => {
    let releasePage2: (value: FileOperationsPageApi | null) => void = () => {};
    const delayed = new Promise<FileOperationsPageApi | null>((resolve) => {
      releasePage2 = resolve;
    });
    // First expansion page of query A, with a next cursor.
    mockedOperations.mockResolvedValueOnce(
      pageWith('/workspace/src/app.ts', { has_earlier: true, next_cursor: 'a-2' }),
    );
    mockedOperations.mockReturnValueOnce(delayed);
    renderSection();
    await screen.findByText('app.ts');
    fireEvent.click(screen.getByLabelText('Show operations on app.ts'));
    await screen.findByText(/recorded operations shown/);
    fireEvent.click(screen.getByRole('button', { name: 'Show earlier operations' }));
    await waitFor(() => expect(mockedOperations).toHaveBeenCalledTimes(2));

    // The reader changes the search while page 2 is still in flight.
    mockedActivity.mockResolvedValue(view({ q: 'zzz', files: [] }));
    fireEvent.change(screen.getByPlaceholderText('Filter by path…'), {
      target: { value: 'zzz' },
    });
    await waitFor(() =>
      expect(mockedActivity).toHaveBeenCalledWith(
        'job-2',
        expect.objectContaining({ q: 'zzz' }),
        expect.anything(),
      ),
    );
    // The old page 2 now resolves: it must not appear under the new list.
    releasePage2(pageWith('/workspace/src/app.ts'));
    await waitFor(() =>
      expect(screen.queryByText(/of 2 recorded operations/)).not.toBeInTheDocument(),
    );
  });

  it('follows a settlement that keeps the same turn count', async () => {
    // A running turn: one turn, one request, a pending write with no result.
    const running = {
      ...page(),
      operations: [
        {
          ...page().operations[0],
          call_id: 'pending-write',
          status: 'not_recorded' as const,
          result_event_id: null,
        },
      ],
    };
    const settled = {
      ...page(),
      operations: [
        {
          ...page().operations[0],
          call_id: 'pending-write',
          status: 'succeeded' as const,
          result_event_id: 42,
          added: 4,
        },
      ],
    };
    mockedOperations.mockResolvedValueOnce(running);
    mockedOperations.mockResolvedValueOnce(settled);
    const { rerender } = render(
      <LocaleProvider>
        <FileActivity
          jobId="job-2"
          conversationKey="thread-1"
          onOpenPoint={vi.fn()}
          onOpenTurn={vi.fn()}
          refreshTick={1}
          active
        />
      </LocaleProvider>,
    );
    await screen.findByText('app.ts');
    fireEvent.click(screen.getByLabelText('Show operations on app.ts'));
    expect(await screen.findByText(/no result recorded/)).toBeInTheDocument();
    // The turn count is unchanged; only the revision (settlement) moved.
    rerender(
      <LocaleProvider>
        <FileActivity
          jobId="job-2"
          conversationKey="thread-1"
          onOpenPoint={vi.fn()}
          onOpenTurn={vi.fn()}
          refreshTick={2}
          active={false}
        />
      </LocaleProvider>,
    );
    await waitFor(() => expect(screen.queryByText(/no result recorded/)).not.toBeInTheDocument());
    expect(mockedOperations).toHaveBeenCalledTimes(2);
  });
});

describe('Operations ownership and terminal settlement', () => {
  it('keeps page 2 usable when a revision arrives while it is loading', async () => {
    let releasePage2: (value: FileOperationsPageApi | null) => void = () => {};
    const delayed = new Promise<FileOperationsPageApi | null>((resolve) => {
      releasePage2 = resolve;
    });
    const first = page();
    const second = {
      ...page(),
      operations: [{ ...page().operations[0], call_id: 'c2', event_id: 500 }],
    };
    const third = {
      ...page(),
      operations: [{ ...page().operations[0], call_id: 'c3', event_id: 501 }],
    };
    mockedOperations.mockResolvedValueOnce({ ...first, has_earlier: true, next_cursor: 'p2' });
    mockedOperations.mockReturnValueOnce(delayed);
    const onOpenPoint = vi.fn();
    const onOpenTurn = vi.fn();
    const { rerender } = render(
      <LocaleProvider>
        <FileActivity
          jobId="job-2"
          conversationKey="thread-1"
          onOpenPoint={onOpenPoint}
          onOpenTurn={onOpenTurn}
          refreshTick={1}
        />
      </LocaleProvider>,
    );
    await screen.findByText('app.ts');
    fireEvent.click(screen.getByLabelText('Show operations on app.ts'));
    await screen.findByText(/recorded operations shown/);
    fireEvent.click(screen.getByRole('button', { name: 'Show earlier operations' }));
    await waitFor(() => expect(mockedOperations).toHaveBeenCalledTimes(2));

    // A refresh replaces the list: the first page is read again.
    mockedOperations.mockResolvedValueOnce({ ...first, has_earlier: true, next_cursor: 'p2' });
    rerender(
      <LocaleProvider>
        <FileActivity
          jobId="job-2"
          conversationKey="thread-1"
          onOpenPoint={onOpenPoint}
          onOpenTurn={onOpenTurn}
          refreshTick={2}
        />
      </LocaleProvider>,
    );
    await waitFor(() => expect(mockedOperations).toHaveBeenCalledTimes(3));
    // The stale page resolves now: it must not append, and loading must clear.
    releasePage2(second);
    await waitFor(() =>
      expect(screen.getByRole('button', { name: 'Show earlier operations' })).toBeEnabled(),
    );
    // Page 2 is still usable afterwards.
    mockedOperations.mockResolvedValueOnce(third);
    fireEvent.click(screen.getByRole('button', { name: 'Show earlier operations' }));
    await waitFor(() => expect(mockedOperations).toHaveBeenCalledTimes(4));
  });

  it('cancels an in-flight page when the row collapses', async () => {
    let release: (value: FileOperationsPageApi | null) => void = () => {};
    const delayed = new Promise<FileOperationsPageApi | null>((resolve) => {
      release = resolve;
    });
    mockedOperations.mockResolvedValueOnce({ ...page(), has_earlier: true, next_cursor: 'p2' });
    mockedOperations.mockReturnValueOnce(delayed);
    renderSection();
    await screen.findByText('app.ts');
    fireEvent.click(screen.getByLabelText('Show operations on app.ts'));
    fireEvent.click(await screen.findByRole('button', { name: 'Show earlier operations' }));
    await waitFor(() => expect(mockedOperations).toHaveBeenCalledTimes(2));
    const signal = mockedOperations.mock.calls[1][3];
    expect(signal).toBeInstanceOf(AbortSignal);
    fireEvent.click(screen.getByLabelText('Hide operations on app.ts'));
    await waitFor(() => expect(signal?.aborted).toBe(true));
    release(page());
  });
});

describe('compact bounded panel', () => {
  /** Many files, so the list must scroll instead of growing the page. */
  function manyFiles(count: number) {
    const base = view().files[0];
    return view({
      files: Array.from({ length: count }, (_, index) => ({
        ...base,
        path: `/workspace/src/module_${String(index).padStart(2, '0')}.ts`,
        name: `module_${String(index).padStart(2, '0')}.ts`,
        last_at: '2026-10-31T08:00:00Z',
      })),
      totals: { ...view().totals, files: count, operations: count },
      has_more: true,
      next_cursor: 'next-page',
    });
  }

  it('keeps the list in one bounded scroll area with every row inside it', async () => {
    mockedActivity.mockResolvedValue(manyFiles(40));
    renderSection();
    await screen.findByText('module_00.ts');
    const list = screen.getByTestId('file-list');
    // The panel scrolls internally, so a long conversation cannot grow the page.
    expect(list.className).toContain('overflow-y-auto');
    expect(list.className).toContain('max-h-80');
    // Every loaded row is a child of that one scroller, and none is dropped.
    const rows = within(list).getAllByRole('listitem');
    expect(rows.length).toBeGreaterThanOrEqual(40);
    for (const row of rows) expect(list.contains(row)).toBe(true);
  });

  it('keeps the show-more control inside the scroll area and reachable', async () => {
    mockedActivity.mockResolvedValue(manyFiles(40));
    renderSection();
    await screen.findByText('module_00.ts');
    const list = screen.getByTestId('file-list');
    const more = screen.getByTestId('file-show-more');
    // Reachable by a reader and by the keyboard, without leaving the panel.
    expect(list.contains(more)).toBe(true);
    expect(more).toBeEnabled();
    more.focus();
    expect(more).toHaveFocus();
  });

  it('loads a later page from inside the same scroller', async () => {
    mockedActivity.mockResolvedValue(manyFiles(2));
    const second = manyFiles(2);
    mockedActivity.mockResolvedValueOnce(manyFiles(2)).mockResolvedValueOnce({
      ...second,
      files: [{ ...second.files[0], path: '/workspace/src/later.ts', name: 'later.ts' }],
      has_more: false,
      next_cursor: null,
    });
    renderSection();
    await screen.findByText('module_00.ts');
    fireEvent.click(screen.getByTestId('file-show-more'));
    expect(await screen.findByText('later.ts')).toBeInTheDocument();
    expect(screen.getByTestId('file-list').contains(screen.getByText('later.ts'))).toBe(true);
  });

  it('puts the filters on the title row and the search with the sort below', async () => {
    renderSection();
    await screen.findByText('app.ts');
    const title = screen.getByRole('heading', { name: 'File Activity' });
    const filters = screen.getByTestId('file-filter-all').closest('[role="group"]');
    const search = screen.getByPlaceholderText('Filter by path…');
    const sort = screen.getByTestId('file-sort-active').closest('[role="group"]');
    // The title and its chips share the first row; search and sort the second.
    expect(title.parentElement?.parentElement?.contains(filters)).toBe(true);
    expect(search.parentElement?.contains(sort)).toBe(true);
    expect(filters?.contains(sort as Node)).toBe(false);
  });

  it('offers the sort as pressed buttons, not a native select', async () => {
    renderSection();
    await screen.findByText('app.ts');
    const active = screen.getByTestId('file-sort-active');
    expect(active).toHaveAttribute('aria-pressed', 'true');
    const byLines = screen.getByTestId('file-sort-lines');
    fireEvent.click(byLines);
    await waitFor(() =>
      expect(mockedActivity).toHaveBeenLastCalledWith(
        'job-2',
        expect.objectContaining({ sort: 'lines' }),
        expect.anything(),
      ),
    );
    // A selection is announced, and the control is a group rather than a menu.
    expect(screen.queryByRole('combobox')).not.toBeInTheDocument();
  });

  it('keeps an expanded long operation list inside the same bounded scroller', async () => {
    mockedActivity.mockResolvedValue(manyFiles(6));
    const long = Array.from({ length: 30 }, (_, index) => ({
      ...page().operations[0],
      call_id: `op-${index}`,
      event_id: 900 + index,
      patch_index: index + 1,
      patch_files: 40,
    }));
    mockedOperations.mockResolvedValue({ ...page(), operations: long, total: 40 });
    renderSection();
    await screen.findByText('module_00.ts');
    fireEvent.click(screen.getByLabelText('Show operations on module_00.ts'));
    await screen.findByText(/30 of 40 recorded operations shown/);
    const list = screen.getByTestId('file-list');
    const ops = screen.getByRole('list', {
      name: 'Recorded operations on /workspace/src/module_00.ts',
    });
    // Operations sit inside the panel's scroller and stay compact.
    expect(list.contains(ops)).toBe(true);
    expect(within(ops).getAllByRole('listitem')).toHaveLength(30);
    expect(ops.querySelector('li')?.className).toContain('py-0.5');
  });

  it('keeps the whole row focusable and the toggle keyboard-operable', async () => {
    renderSection();
    await screen.findByText('app.ts');
    const toggle = screen.getByLabelText('Show operations on app.ts');
    toggle.focus();
    expect(toggle).toHaveFocus();
    fireEvent.keyDown(toggle, { key: 'Enter', code: 'Enter' });
    fireEvent.click(toggle);
    expect(
      await screen.findByRole('list', {
        name: 'Recorded operations on /workspace/src/app.ts',
      }),
    ).toBeInTheDocument();
    expect(screen.getByLabelText('Hide operations on app.ts')).toHaveAttribute(
      'aria-expanded',
      'true',
    );
  });
});

describe('a failed later page stays retryable', () => {
  function paged(count: number, cursor: string | null) {
    const base = view().files[0];
    return view({
      files: Array.from({ length: count }, (_, index) => ({
        ...base,
        path: `/workspace/src/page_${String(index).padStart(2, '0')}.ts`,
        name: `page_${String(index).padStart(2, '0')}.ts`,
      })),
      has_more: cursor !== null,
      next_cursor: cursor,
    });
  }

  it('re-enables loading after page 2 fails, then loads it', async () => {
    mockedActivity
      .mockResolvedValueOnce(paged(25, 'page-2'))
      .mockRejectedValueOnce(new Error('offline'))
      .mockResolvedValueOnce(
        view({
          files: [
            { ...view().files[0], path: '/workspace/src/recovered.ts', name: 'recovered.ts' },
          ],
          has_more: false,
          next_cursor: null,
        }),
      );
    renderSection();
    await screen.findByText('page_00.ts');
    fireEvent.click(screen.getByTestId('file-show-more'));

    // The failure is shown with a control that is actually pressable.
    const retry = await screen.findByTestId('file-show-more-retry');
    expect(retry).toBeEnabled();
    expect(screen.getByText('The rest of the list could not be read.')).toBeInTheDocument();

    fireEvent.click(retry);
    expect(await screen.findByText('recovered.ts')).toBeInTheDocument();
    expect(mockedActivity).toHaveBeenCalledTimes(3);
    // The retry read the failed page's cursor, not the first page again.
    expect(mockedActivity.mock.calls[2][1]).toMatchObject({ cursor: 'page-2' });
  });
});
