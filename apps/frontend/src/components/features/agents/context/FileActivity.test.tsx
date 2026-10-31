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

  it('says which bounded window the totals describe', async () => {
    renderSection();
    await screen.findByText('app.ts');
    expect(
      screen.getByText(/Scanned 40 recorded events over 2 turns and 2 attempts/),
    ).toBeInTheDocument();
    expect(screen.getByText(/Net Git diff is not read here/)).toBeInTheDocument();
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
    expect(screen.getByText(/This is the newest part of a long history/)).toBeInTheDocument();
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

  it('keeps a long path wrappable and a lone name whole', async () => {
    renderSection();
    const name = await screen.findByText('app.ts');
    expect(name.closest('p')).toHaveAttribute('title', '/workspace/src/app.ts');
    const long = screen.getByText('notes 深い.md');
    expect(long.closest('p')?.textContent).toContain('/workspace/docs/');
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
    expect(await screen.findByText(/by a delegated agent/)).toBeInTheDocument();
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
    expect(await screen.findByText(/part 2 of 3 in one patch/)).toBeInTheDocument();
    const ops = within(
      screen.getByRole('list', { name: 'Recorded operations on /workspace/src/app.ts' }),
    ).getAllByRole('listitem');
    expect(ops).toHaveLength(1);
  });
});
