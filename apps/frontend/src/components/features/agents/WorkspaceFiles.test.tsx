// @vitest-environment jsdom

import '@testing-library/jest-dom/vitest';
import { cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { getAgentJobFiles, type AgentJobFileApi, type AgentJobFilesApi } from '@/lib/api/agents';
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

function respondWith(file: AgentJobFilesApi) {
  vi.mocked(getAgentJobFiles).mockImplementation(async (_jobId, path = '') =>
    path ? { ...file, path } : { path: '', kind: 'directory', entries: [] },
  );
}

beforeEach(() => {
  vi.mocked(getAgentJobFiles).mockReset();
  createObjectURL
    .mockReset()
    .mockImplementation(() => `blob:workspace-${createObjectURL.mock.calls.length}`);
  revokeObjectURL.mockReset();
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
