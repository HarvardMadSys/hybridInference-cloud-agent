// @vitest-environment jsdom

import '@testing-library/jest-dom/vitest';
import { act, cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { getAgentJobFiles, type AgentJobFileApi, type AgentJobFilesApi } from '@/lib/api/agents';
import { LocaleProvider } from '@/components/providers/LocaleProvider';
import { LOCALE_STORAGE_KEY } from '@/lib/i18n/locales';
import { APIError } from '@/lib/utils/errors';
import { toDiffFileDetails, toDiffFiles } from './adapt';
import type { AgentJob } from './types';
import { WorkspaceFiles } from './WorkspaceFiles';

vi.mock('@/lib/api/agents', () => ({ getAgentJobFiles: vi.fn() }));

const JOB: AgentJob = {
  id: 'ajob_workspace',
  title: 'Generate artifacts',
  state: 'done',
  repo: 'owner/repo',
  baseSha: 'abcdef1',
  branch: 'agent/ajob_workspace',
  runtime: 'claude-code',
  model: 'model',
  spentUsd: 0,
  timeoutLabel: '',
  sandbox: '',
  attempts: [],
  events: [],
  diffFiles: [],
  gates: [],
  usage: { tokensIn: '', tokensOut: '', cachePct: 0, turns: 0 },
};

const binaryFile = (overrides: Partial<AgentJobFileApi> = {}): AgentJobFileApi => ({
  path: 'artifact.png',
  kind: 'file',
  content: null,
  content_base64: 'iVBORw0KGgoAaW1hZ2U=',
  media_type: 'image/png',
  size: 14,
  binary: true,
  truncated: false,
  ...overrides,
});

const createObjectURL = vi.fn<(blob: Blob) => string>();
const revokeObjectURL = vi.fn<(url: string) => void>();
const writeClipboard = vi.fn<(value: string) => Promise<void>>();

const textFile = (content: string, overrides: Partial<AgentJobFileApi> = {}): AgentJobFileApi => ({
  path: 'notes.txt',
  kind: 'file',
  content,
  size: content.length,
  binary: false,
  truncated: false,
  ...overrides,
});

function openPath(path: string) {
  const input = screen.getByRole('textbox', { name: 'Repository-relative file or folder path' });
  fireEvent.change(input, { target: { value: path } });
  fireEvent.submit(input.closest('form')!);
  return input;
}

function respondWith(file: AgentJobFilesApi) {
  vi.mocked(getAgentJobFiles).mockImplementation(async (_jobId, path = '') =>
    path ? { ...file, path } : { path: '', kind: 'directory', entries: [] },
  );
}

function deferred() {
  let resolve!: (value: AgentJobFilesApi) => void;
  let reject!: (reason: Error) => void;
  const promise = new Promise<AgentJobFilesApi>((accept, fail) => {
    resolve = accept;
    reject = fail;
  });
  return { promise, resolve, reject };
}

beforeEach(() => {
  vi.mocked(getAgentJobFiles).mockReset();
  createObjectURL
    .mockReset()
    .mockImplementation(() => `blob:workspace-${createObjectURL.mock.calls.length}`);
  revokeObjectURL.mockReset();
  writeClipboard.mockReset().mockResolvedValue(undefined);
  localStorage.clear();
  vi.stubGlobal('navigator', {
    clipboard: { writeText: writeClipboard },
    languages: ['en-US'],
    language: 'en-US',
  });
  vi.stubGlobal(
    'URL',
    class extends URL {
      static createObjectURL = createObjectURL;
      static revokeObjectURL = revokeObjectURL;
    },
  );
});

afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
  localStorage.clear();
});

describe('workspace navigation and copying', () => {
  it('submits an exact repository path without escaping or rewriting its characters', async () => {
    respondWith(textFile('const answer = 42;'));
    render(<WorkspaceFiles job={JOB} active />);
    const path = 'src/a #%.ts';
    const input = openPath(path);

    await screen.findByRole('button', { name: 'Copy contents' });
    expect(input).toHaveValue(path);
    expect(vi.mocked(getAgentJobFiles).mock.calls.filter(([, value]) => value === path)).toEqual([
      [JOB.id, path],
    ]);
    fireEvent.click(screen.getByRole('button', { name: 'Copy path' }));
    expect(await screen.findByRole('status')).toHaveTextContent('Path copied');
    expect(writeClipboard).toHaveBeenCalledWith(path);
  });

  it('reveals a directly opened nested folder even when its tree was hidden', async () => {
    vi.mocked(getAgentJobFiles).mockImplementation(async (_jobId, path = '') => ({
      path,
      kind: 'directory',
      entries:
        path === ''
          ? [{ path: 'src', name: 'src', kind: 'directory' }]
          : path === 'src'
            ? [{ path: 'src/nested', name: 'nested', kind: 'directory' }]
            : [{ path: 'src/nested/notes.txt', name: 'notes.txt', kind: 'file' }],
    }));
    render(<WorkspaceFiles job={JOB} active />);
    fireEvent.click(screen.getByRole('button', { name: 'Hide file tree' }));
    openPath('src/nested');

    expect(await screen.findByRole('button', { name: 'src/nested/notes.txt' })).toBeVisible();
    expect(screen.getByText('src/nested')).toBeVisible();
    fireEvent.click(screen.getByRole('button', { name: 'Workspace root' }));
    expect(screen.getByRole('button', { name: 'src' })).toHaveAttribute('aria-expanded', 'true');
    expect(screen.getByRole('button', { name: 'src/nested' })).toHaveAttribute(
      'aria-expanded',
      'true',
    );
  });

  it.each(['empty root', 'omitted root entry', 'omitted ancestor entry'] as const)(
    'shows directly opened folder contents despite an %s',
    async (listing) => {
      vi.mocked(getAgentJobFiles).mockImplementation(async (_jobId, path = '') => ({
        path,
        kind: 'directory',
        source: 'workspace',
        entries:
          path === 'new/child'
            ? [{ path: 'new/child/created.txt', name: 'created.txt', kind: 'file' }]
            : path === '' && listing === 'omitted ancestor entry'
              ? [{ path: 'new', name: 'new', kind: 'directory' }]
              : listing === 'empty root'
                ? []
                : [{ path: `${path ? `${path}/` : ''}other`, name: 'other', kind: 'directory' }],
      }));
      render(<WorkspaceFiles job={JOB} active />);
      await waitFor(() => expect(getAgentJobFiles).toHaveBeenCalledWith(JOB.id, ''));
      openPath('new/child');

      expect(await screen.findByRole('button', { name: 'new/child/created.txt' })).toBeVisible();
      expect(screen.getByText('new/child')).toBeVisible();
      expect(screen.getByRole('button', { name: 'Workspace root' })).toBeVisible();
    },
  );

  it.each(['directory', 'file', 'error'] as const)(
    'keeps the root tree after cancelling an unfinished path that returns a %s',
    async (outcome) => {
      const pending = deferred();
      vi.mocked(getAgentJobFiles).mockImplementation(async (_jobId, path = '') => {
        if (path === 'next') return pending.promise;
        return {
          path,
          kind: 'directory',
          entries:
            path === ''
              ? [{ path: 'first', name: 'first', kind: 'directory' }]
              : [{ path: 'first/notes.txt', name: 'notes.txt', kind: 'file' }],
        };
      });
      render(<WorkspaceFiles job={JOB} active />);
      openPath('first');
      await screen.findByRole('button', { name: 'first/notes.txt' });
      openPath('next');
      expect(screen.getByText('Loading file…')).toBeVisible();

      fireEvent.click(screen.getByRole('button', { name: 'Workspace root' }));

      expect(screen.getByRole('button', { name: 'first' })).toBeVisible();
      expect(screen.queryByText('Loading file…')).not.toBeInTheDocument();
      expect(screen.queryByRole('button', { name: 'Copy path' })).not.toBeInTheDocument();
      await act(async () => {
        if (outcome === 'error') pending.reject(new Error('Cancelled read failed'));
        else if (outcome === 'file') pending.resolve(textFile('Cancelled file', { path: 'next' }));
        else
          pending.resolve({
            path: 'next',
            kind: 'directory',
            entries: [{ path: 'next/late.txt', name: 'late.txt', kind: 'file' }],
          });
      });

      expect(screen.getByRole('button', { name: 'first' })).toBeVisible();
      expect(screen.queryByRole('button', { name: 'Workspace root' })).not.toBeInTheDocument();
      expect(screen.queryByRole('button', { name: 'next/late.txt' })).not.toBeInTheDocument();
      expect(screen.queryByRole('button', { name: 'Copy path' })).not.toBeInTheDocument();
      expect(screen.queryByRole('alert')).not.toBeInTheDocument();
      vi.mocked(getAgentJobFiles).mockClear();
      fireEvent.click(screen.getByRole('button', { name: 'Refresh files' }));
      await waitFor(() => expect(getAgentJobFiles).toHaveBeenCalledWith(JOB.id, ''));
      expect(getAgentJobFiles).not.toHaveBeenCalledWith(JOB.id, 'next');
    },
  );

  it('preserves a loaded file preview when returning from its folder to the root tree', async () => {
    vi.mocked(getAgentJobFiles).mockImplementation(async (_jobId, path = '') => {
      if (path === 'src/notes.txt') return textFile('Keep this preview', { path });
      return {
        path,
        kind: 'directory',
        entries:
          path === ''
            ? [{ path: 'src', name: 'src', kind: 'directory' }]
            : [{ path: 'src/notes.txt', name: 'notes.txt', kind: 'file' }],
      };
    });
    render(<WorkspaceFiles job={JOB} active />);
    openPath('src');
    fireEvent.click(await screen.findByRole('button', { name: 'src/notes.txt' }));
    await screen.findByRole('button', { name: 'Copy contents' });

    fireEvent.click(screen.getByRole('button', { name: 'Workspace root' }));

    expect(screen.getByRole('button', { name: 'src' })).toBeVisible();
    expect(screen.getByRole('button', { name: 'Copy contents' })).toBeVisible();
    expect(screen.getByText('Keep this preview')).toBeVisible();
  });

  it('keeps focused-folder navigation associated with the Files tab', async () => {
    vi.mocked(getAgentJobFiles).mockImplementation(async (_jobId, path = '') => ({
      path,
      kind: 'directory',
      entries: path
        ? [{ path: 'src/notes.txt', name: 'notes.txt', kind: 'file' }]
        : [{ path: 'src', name: 'src', kind: 'directory' }],
    }));
    render(<WorkspaceFiles job={{ ...JOB, diffFiles: ['changed.txt'] }} active />);
    openPath('src');
    await screen.findByRole('button', { name: 'src/notes.txt' });
    const files = screen.getByRole('tab', { name: 'Files' });
    expect(screen.getByRole('tabpanel')).toHaveAttribute('id', files.getAttribute('aria-controls'));

    fireEvent.click(screen.getByRole('tab', { name: /Changes/ }));

    expect(screen.getByRole('button', { name: 'changed.txt' })).toBeVisible();
    expect(screen.queryByRole('button', { name: 'Workspace root' })).not.toBeInTheDocument();
    expect(screen.queryByRole('button', { name: 'src/notes.txt' })).not.toBeInTheDocument();
    fireEvent.click(files);
    expect(screen.getByRole('button', { name: 'src/notes.txt' })).toBeVisible();
    fireEvent.click(screen.getByRole('button', { name: 'Workspace root' }));
    expect(screen.getByRole('button', { name: 'src' })).toBeVisible();
  });

  it('focuses typed files and restores the focused folder in a narrow pane', async () => {
    vi.spyOn(HTMLElement.prototype, 'clientWidth', 'get').mockReturnValue(420);
    vi.mocked(getAgentJobFiles).mockImplementation(async (_jobId, path = '') => {
      if (path === 'src/notes.txt') return textFile('Narrow preview', { path });
      return {
        path,
        kind: 'directory',
        entries: path
          ? [{ path: 'src/notes.txt', name: 'notes.txt', kind: 'file' }]
          : [{ path: 'src', name: 'src', kind: 'directory' }],
      };
    });
    render(<WorkspaceFiles job={JOB} active />);
    const input = screen.getByRole('textbox', { name: 'Repository-relative file or folder path' });
    input.focus();
    openPath('src');
    await screen.findByRole('button', { name: 'src/notes.txt' });
    expect(screen.getByRole('tab', { name: 'Files' })).toHaveFocus();
    expect(screen.getByRole('button', { name: 'Workspace root' })).toBeVisible();

    openPath('src/notes.txt');

    await screen.findByRole('button', { name: 'Copy contents' });
    expect(screen.getByRole('region', { name: 'Preview of src/notes.txt' })).toHaveFocus();
    fireEvent.click(screen.getByRole('button', { name: 'Back to file list' }));
    expect(screen.getByRole('button', { name: 'src/notes.txt' })).toHaveFocus();
    expect(screen.getByRole('button', { name: 'Workspace root' })).toBeVisible();
    fireEvent.click(screen.getByRole('button', { name: 'Workspace root' }));
    expect(screen.getByRole('button', { name: 'src' })).toBeVisible();
  });

  it('keeps a refused path editable and retries just that file', async () => {
    let fileReads = 0;
    vi.mocked(getAgentJobFiles).mockImplementation(async (_jobId, path = '') => {
      if (!path) return { path: '', kind: 'directory', entries: [] };
      if (++fileReads === 1) throw new Error('File not found');
      return textFile('Recovered', { path });
    });
    render(<WorkspaceFiles job={JOB} active />);
    const input = openPath('missing.txt');
    expect(await screen.findByRole('alert')).toHaveTextContent('File not found');
    expect(input).toHaveValue('missing.txt');
    vi.mocked(getAgentJobFiles).mockClear();
    fireEvent.click(screen.getByRole('button', { name: 'Retry loading file' }));

    await screen.findByRole('button', { name: 'Copy contents' });
    expect(getAgentJobFiles).toHaveBeenCalledExactlyOnceWith(JOB.id, 'missing.txt');
    expect(screen.queryByRole('alert')).not.toBeInTheDocument();
  });

  it('says a changed file the run deleted was deleted, rather than failing to read it', async () => {
    vi.mocked(getAgentJobFiles).mockImplementation(async (_jobId, path = '') => {
      if (!path) return { path: '', kind: 'directory', entries: [] };
      // What the live workspace answers for a path that is no longer there.
      throw new APIError('NOT_FOUND', 'workspace path does not exist', 404);
    });
    const patch = [
      'diff --git a/kept.ts b/kept.ts',
      'index 1111111..2222222 100644',
      '--- a/kept.ts',
      '+++ b/kept.ts',
      '@@ -1 +1 @@',
      '-old',
      '+new',
      'diff --git a/old.ts b/old.ts',
      'deleted file mode 100644',
      'index 3333333..0000000',
      '--- a/old.ts',
      '+++ /dev/null',
      '@@ -1 +0,0 @@',
      '-gone',
      '',
    ].join('\n');
    const job = {
      ...JOB,
      diffFiles: toDiffFiles(patch),
      diffFileDetails: toDiffFileDetails(patch),
    };
    render(<WorkspaceFiles job={job} active />);
    fireEvent.click(screen.getByRole('tab', { name: /^Changes/ }));

    fireEvent.click(await screen.findByRole('button', { name: 'old.ts' }));
    expect(await screen.findByText('This file was deleted by the run.')).toBeVisible();
    expect(screen.queryByRole('alert')).not.toBeInTheDocument();
    expect(screen.queryByRole('button', { name: 'Retry loading file' })).not.toBeInTheDocument();

    // A file the diff does not delete still reports that it could not be read.
    fireEvent.click(screen.getByRole('button', { name: 'kept.ts' }));
    expect(await screen.findByRole('alert')).toHaveTextContent('workspace path does not exist');
    expect(screen.getByRole('button', { name: 'Retry loading file' })).toBeVisible();
  });

  it('does not call a file that became a symlink deleted', async () => {
    vi.mocked(getAgentJobFiles).mockImplementation(async (_jobId, path = '') => {
      if (!path) return { path: '', kind: 'directory', entries: [] };
      // A link whose target is not there reads as nothing at its path.
      throw new APIError('NOT_FOUND', 'workspace path does not exist', 404);
    });
    // A change of file type deletes the path and creates it again.
    const patch = [
      'diff --git a/config.yml b/config.yml',
      'deleted file mode 100644',
      'index a52e282..0000000',
      '--- a/config.yml',
      '+++ /dev/null',
      '@@ -1 +0,0 @@',
      '-plain text',
      'diff --git a/config.yml b/config.yml',
      'new file mode 120000',
      'index 0000000..0190743',
      '--- /dev/null',
      '+++ b/config.yml',
      '@@ -0,0 +1 @@',
      '+shared/config.yml',
      '\\ No newline at end of file',
      '',
    ].join('\n');
    const job = {
      ...JOB,
      diffFiles: toDiffFiles(patch),
      diffFileDetails: toDiffFileDetails(patch),
    };
    render(<WorkspaceFiles job={job} active />);
    fireEvent.click(screen.getByRole('tab', { name: /^Changes/ }));

    const changed = await screen.findByRole('button', { name: 'config.yml' });
    expect(changed).toHaveTextContent('+1 −1');
    fireEvent.click(changed);
    expect(await screen.findByRole('alert')).toHaveTextContent('workspace path does not exist');
    expect(screen.queryByText('This file was deleted by the run.')).not.toBeInTheDocument();
  });

  it('retries a failed folder beside its error without refreshing the rest of the tree', async () => {
    let folderReads = 0;
    vi.mocked(getAgentJobFiles).mockImplementation(async (_jobId, path = '') => {
      if (!path)
        return {
          path: '',
          kind: 'directory',
          entries: [{ path: 'src', name: 'src', kind: 'directory' }],
        };
      if (++folderReads === 1) throw new Error('Folder unavailable');
      return {
        path: 'src',
        kind: 'directory',
        entries: [{ path: 'src/notes.txt', name: 'notes.txt', kind: 'file' }],
      };
    });
    render(<WorkspaceFiles job={JOB} active />);
    fireEvent.click(await screen.findByRole('button', { name: 'src' }));
    await screen.findByRole('alert');
    vi.mocked(getAgentJobFiles).mockClear();
    fireEvent.click(screen.getByRole('button', { name: 'Retry loading folder src' }));

    expect(await screen.findByRole('button', { name: 'src/notes.txt' })).toBeVisible();
    expect(getAgentJobFiles).toHaveBeenCalledExactlyOnceWith(JOB.id, 'src');
  });

  it('says which folders list only some of their entries', async () => {
    vi.mocked(getAgentJobFiles).mockImplementation(async (_jobId, path = '') => {
      if (!path)
        return {
          path: '',
          kind: 'directory',
          entries: [
            { path: 'src', name: 'src', kind: 'directory' },
            { path: 'docs', name: 'docs', kind: 'directory' },
          ],
          truncated: false,
        };
      return {
        path,
        kind: 'directory',
        entries: [{ path: `${path}/kept.txt`, name: 'kept.txt', kind: 'file' }],
        // The host leaves out names past its entry limit, and names that are
        // not UTF-8, and says so.
        truncated: path === 'src',
      };
    });
    render(<WorkspaceFiles job={JOB} active />);
    fireEvent.click(await screen.findByRole('button', { name: 'docs' }));
    await screen.findByRole('button', { name: 'docs/kept.txt' });
    expect(screen.queryByText('Some entries in this folder are not listed.')).toBeNull();

    fireEvent.click(screen.getByRole('button', { name: 'src' }));
    await screen.findByRole('button', { name: 'src/kept.txt' });
    expect(screen.getAllByText('Some entries in this folder are not listed.')).toHaveLength(1);

    // A folder opened by its path reads the same flag.
    openPath('lib');
    await screen.findByRole('button', { name: 'lib/kept.txt' });
    expect(screen.queryByText('Some entries in this folder are not listed.')).toBeNull();
    openPath('src');
    await screen.findByRole('button', { name: 'src/kept.txt' });
    expect(screen.getByText('Some entries in this folder are not listed.')).toBeVisible();
  });

  it('does not call a folder empty when every entry was left out of its listing', async () => {
    vi.mocked(getAgentJobFiles).mockResolvedValue({
      path: '',
      kind: 'directory',
      entries: [],
      truncated: true,
    });
    render(<WorkspaceFiles job={JOB} active />);

    expect(await screen.findByText('Some entries in this folder are not listed.')).toBeVisible();
    expect(screen.queryByText('Empty')).not.toBeInTheDocument();
  });

  it.each(['', Array.from({ length: 4002 }, (_, index) => `line ${index + 1}`).join('\n')])(
    'copies all authorized text, including empty files and lines beyond the display limit',
    async (content) => {
      respondWith(textFile(content));
      render(<WorkspaceFiles job={JOB} active requestedPath="notes.txt" />);
      fireEvent.click(await screen.findByRole('button', { name: 'Copy contents' }));

      expect(await screen.findByRole('status')).toHaveTextContent('File contents copied');
      expect(writeClipboard).toHaveBeenCalledExactlyOnceWith(content);
    },
  );

  it.each([
    ['binary', binaryFile()],
    ['truncated', textFile('Partial', { truncated: true })],
    ['deleted', textFile('Old contents', { status: 'deleted' })],
    ['symlink', { path: 'link', kind: 'symlink', omitted_reason: 'symlink' }],
  ] as const)('does not offer to copy %s file contents', async (_kind, file) => {
    respondWith(file);
    render(<WorkspaceFiles job={JOB} active requestedPath={file.path} />);
    await waitFor(() => expect(screen.queryByText('Loading file…')).not.toBeInTheDocument());
    expect(screen.queryByRole('button', { name: 'Copy contents' })).not.toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Copy path' })).toBeVisible();
  });

  it('announces clipboard failure in the selected language', async () => {
    localStorage.setItem(LOCALE_STORAGE_KEY, 'zh-CN');
    writeClipboard.mockRejectedValue(new Error('Clipboard denied'));
    respondWith(textFile('中文内容'));
    render(
      <LocaleProvider>
        <WorkspaceFiles job={JOB} active requestedPath="notes.txt" />
      </LocaleProvider>,
    );
    fireEvent.click(await screen.findByRole('button', { name: '复制内容' }));

    expect(await screen.findByRole('alert')).toHaveTextContent('无法复制。请选中文本后手动复制。');
  });

  it('discards clipboard feedback after another file is selected', async () => {
    let finishCopy!: () => void;
    writeClipboard.mockReturnValue(
      new Promise<void>((resolve) => {
        finishCopy = resolve;
      }),
    );
    respondWith(textFile('Text'));
    render(<WorkspaceFiles job={JOB} active requestedPath="first.txt" />);
    fireEvent.click(await screen.findByRole('button', { name: 'Copy contents' }));
    openPath('second.txt');
    await waitFor(() =>
      expect(screen.getByRole('button', { name: 'Copy contents' })).toBeEnabled(),
    );
    await act(async () => finishCopy());

    expect(screen.queryByRole('status')).not.toBeInTheDocument();
    expect(screen.queryByRole('alert')).not.toBeInTheDocument();
  });
});

describe('workspace file navigation', () => {
  function respondWithFiles(paths: string[]) {
    vi.mocked(getAgentJobFiles).mockImplementation(async (_jobId, path = '') => {
      if (path) {
        return binaryFile({
          path,
          binary: false,
          content: 'export const ready = true;',
          content_base64: null,
          media_type: null,
        });
      }
      return {
        path: '',
        kind: 'directory',
        entries: paths.map((filePath) => ({
          path: filePath,
          name: filePath.split('/').pop() ?? filePath,
          kind: 'file' as const,
        })),
      };
    });
  }

  it('uses one list-tab stop with arrow, Home and End navigation and associated panels', async () => {
    respondWithFiles(['README.md']);
    render(<WorkspaceFiles job={JOB} active />);
    await screen.findByRole('button', { name: 'README.md' });
    const list = screen.getByRole('tablist', { name: 'File list' });
    const tabs = within(list).getAllByRole('tab');
    tabs[0].focus();
    expect(tabs.filter((tab) => tab.tabIndex === 0)).toEqual([tabs[0]]);
    expect(tabs[0]).toHaveAttribute('aria-controls', screen.getByRole('tabpanel').id);
    expect(screen.getByRole('tabpanel')).toHaveAttribute('aria-labelledby', tabs[0].id);
    expect(within(list).queryByRole('button', { name: 'Refresh files' })).not.toBeInTheDocument();

    fireEvent.keyDown(tabs[0], { key: 'ArrowLeft' });
    expect(tabs[1]).toHaveFocus();
    expect(tabs[1]).toHaveAttribute('aria-selected', 'true');
    expect(tabs.filter((tab) => tab.tabIndex === 0)).toEqual([tabs[1]]);
    expect(tabs[1]).toHaveAttribute('aria-controls', screen.getByRole('tabpanel').id);
    fireEvent.keyDown(tabs[1], { key: 'ArrowRight' });
    expect(tabs[0]).toHaveFocus();
    fireEvent.keyDown(tabs[0], { key: 'End' });
    expect(tabs[1]).toHaveFocus();
    fireEvent.keyDown(tabs[1], { key: 'Home' });
    expect(tabs[0]).toHaveFocus();
  });

  it.each(['Back', 'Escape'])(
    'focuses narrow previews and restores the file row with %s',
    async (action) => {
      vi.spyOn(HTMLElement.prototype, 'clientWidth', 'get').mockReturnValue(420);
      respondWithFiles(['src/auth/index.ts', 'src/ui/index.ts']);
      render(
        <WorkspaceFiles
          job={{ ...JOB, diffFiles: ['src/auth/index.ts', 'src/ui/index.ts'] }}
          active
        />,
      );
      fireEvent.click(screen.getByRole('tab', { name: /Changes/ }));
      const row = screen.getByRole('button', { name: 'src/auth/index.ts' });
      expect(within(row).getByText('src/auth')).toBeVisible();
      expect(
        within(screen.getByRole('button', { name: 'src/ui/index.ts' })).getByText('src/ui'),
      ).toBeVisible();
      row.focus();
      fireEvent.click(row);

      const preview = screen.getByRole('region', { name: 'Preview of src/auth/index.ts' });
      await waitFor(() => expect(preview).toHaveAttribute('aria-busy', 'false'));
      expect(preview).toHaveFocus();
      expect(screen.queryByRole('tablist', { name: 'File list' })).not.toBeInTheDocument();
      expect(screen.getByText('src/auth')).toBeVisible();
      const back = screen.getByRole('button', { name: 'Back to file list' });
      expect(back).toHaveTextContent('Back');

      if (action === 'Back') fireEvent.click(back);
      else fireEvent.keyDown(preview, { key: 'Escape' });

      expect(screen.getByRole('button', { name: 'src/auth/index.ts' })).toHaveFocus();
      expect(screen.getByRole('tab', { name: /Changes/ })).toHaveAttribute('aria-selected', 'true');
      expect(screen.queryByRole('button', { name: 'Back to file list' })).not.toBeInTheDocument();
    },
  );

  it('exposes the selected full path and a keyboard-scrollable preview in a wide pane', async () => {
    respondWithFiles(['src/auth/index.ts']);
    render(<WorkspaceFiles job={JOB} active />);
    const row = await screen.findByRole('button', { name: 'src/auth/index.ts' });
    row.focus();
    fireEvent.click(row);
    await waitFor(() =>
      expect(screen.getByRole('region', { name: 'Preview of src/auth/index.ts' })).toHaveAttribute(
        'aria-busy',
        'false',
      ),
    );
    expect(row).toHaveFocus();
    expect(row).toHaveAttribute('aria-current', 'true');
    expect(screen.getByRole('region', { name: 'Preview of src/auth/index.ts' })).toHaveAttribute(
      'tabindex',
      '0',
    );
    expect(screen.getByText('src/auth')).toBeVisible();
  });
});

describe('connected workspace previews', () => {
  it('opens a requested file and reopens the same file when its request token changes', async () => {
    respondWith(binaryFile());
    const { rerender } = render(
      <WorkspaceFiles job={JOB} active requestedPath="artifact.png" requestToken={1} />,
    );
    await screen.findByAltText('Preview of artifact.png');
    expect(getAgentJobFiles).toHaveBeenCalledWith(JOB.id, 'artifact.png');

    rerender(<WorkspaceFiles job={JOB} active requestedPath="artifact.png" requestToken={2} />);
    await waitFor(() =>
      expect(
        vi.mocked(getAgentJobFiles).mock.calls.filter(([, path]) => path === 'artifact.png'),
      ).toHaveLength(2),
    );
  });

  it('waits until the Files tab is active before opening a requested path', async () => {
    respondWith(binaryFile());
    const { rerender } = render(
      <WorkspaceFiles job={JOB} active={false} requestedPath="artifact.png" requestToken={1} />,
    );
    expect(getAgentJobFiles).not.toHaveBeenCalled();

    rerender(<WorkspaceFiles job={JOB} active requestedPath="artifact.png" requestToken={1} />);
    await screen.findByAltText('Preview of artifact.png');
  });

  it('preserves the file chosen from the tree when returning to the Files tab', async () => {
    vi.mocked(getAgentJobFiles).mockImplementation(async (_jobId, path = '') => {
      if (path) return binaryFile({ path });
      return {
        path: '',
        kind: 'directory',
        entries: [{ path: 'other.png', name: 'other.png', kind: 'file' }],
      };
    });
    const { rerender } = render(
      <WorkspaceFiles job={JOB} active requestedPath="artifact.png" requestToken={1} />,
    );
    await screen.findByAltText('Preview of artifact.png');
    fireEvent.click(screen.getByRole('button', { name: 'other.png' }));
    await screen.findByAltText('Preview of other.png');
    rerender(
      <WorkspaceFiles job={JOB} active={false} requestedPath="artifact.png" requestToken={1} />,
    );
    rerender(<WorkspaceFiles job={JOB} active requestedPath="artifact.png" requestToken={1} />);

    await screen.findByAltText('Preview of other.png');
    expect(
      vi.mocked(getAgentJobFiles).mock.calls.filter(([, path]) => path === 'artifact.png'),
    ).toHaveLength(1);
  });

  it('creates a passive image preview and a separate attachment download, then releases both', async () => {
    respondWith(binaryFile());
    const { unmount } = render(<WorkspaceFiles job={JOB} active requestedPath="artifact.png" />);
    const image = await screen.findByAltText('Preview of artifact.png');
    const link = screen.getByRole('link', { name: 'Download file' });

    expect(image).toHaveAttribute('src', 'blob:workspace-2');
    expect(link).toHaveAttribute('href', 'blob:workspace-1');
    expect(link).toHaveAttribute('download', 'artifact.png');
    expect(createObjectURL.mock.calls.map(([blob]) => [blob.type, blob.size])).toEqual([
      ['application/octet-stream', 14],
      ['image/png', 14],
    ]);
    unmount();
    expect(revokeObjectURL.mock.calls.map(([url]) => url)).toEqual([
      'blob:workspace-1',
      'blob:workspace-2',
    ]);
  });

  it('offers a download for unsupported binary formats without rendering them', async () => {
    respondWith(binaryFile({ media_type: null, path: 'archive.bin' }));
    render(<WorkspaceFiles job={JOB} active requestedPath="archive.bin" />);

    expect(await screen.findByRole('link', { name: 'Download file' })).toHaveAttribute(
      'download',
      'archive.bin',
    );
    expect(screen.queryByRole('img')).not.toBeInTheDocument();
    expect(
      screen.getByText('Preview is unavailable for this file type. Download the file to open it.'),
    ).toBeVisible();
  });

  it('shows SVG and HTML as escaped code with an inert download', async () => {
    const content = '<svg onload="alert(1)"><script>alert(1)</script></svg>';
    respondWith(
      binaryFile({
        path: 'drawing.svg',
        content,
        binary: false,
        media_type: null,
        content_base64: null,
      }),
    );
    const { container } = render(<WorkspaceFiles job={JOB} active requestedPath="drawing.svg" />);

    await screen.findByRole('link', { name: 'Download file' });
    expect(container.querySelector('script')).toBeNull();
    expect(container.querySelector('svg[onload]')).toBeNull();
    expect(screen.queryByRole('img')).not.toBeInTheDocument();
    expect(container.textContent).toContain(content);
    expect(createObjectURL.mock.calls[0][0].type).toBe('application/octet-stream');
  });

  it('does not create partial downloads for files omitted by the size bound', async () => {
    respondWith(binaryFile({ truncated: true, content_base64: null, omitted_reason: 'too_large' }));
    render(<WorkspaceFiles job={JOB} active requestedPath="large.png" />);

    expect(await screen.findByText(/This file exceeds the workspace preview limit/)).toBeVisible();
    expect(screen.queryByRole('link', { name: 'Download file' })).not.toBeInTheDocument();
    expect(createObjectURL).not.toHaveBeenCalled();
  });

  it('never previews or offers downloads for symlinks', async () => {
    respondWith({ path: 'linked.png', kind: 'symlink', omitted_reason: 'symlink' });
    render(<WorkspaceFiles job={JOB} active requestedPath="linked.png" />);

    expect(await screen.findByText('Symlinks are not previewed.')).toBeVisible();
    expect(screen.queryByRole('link', { name: 'Download file' })).not.toBeInTheDocument();
    expect(createObjectURL).not.toHaveBeenCalled();
  });

  it('keeps the download usable if the raster image cannot be decoded', async () => {
    respondWith(binaryFile());
    render(<WorkspaceFiles job={JOB} active requestedPath="artifact.png" />);
    fireEvent.error(await screen.findByAltText('Preview of artifact.png'));

    expect(
      screen.getByText('This image could not be decoded. You can still download the file.'),
    ).toBeVisible();
    expect(screen.getByRole('link', { name: 'Download file' })).toBeVisible();
  });
});
